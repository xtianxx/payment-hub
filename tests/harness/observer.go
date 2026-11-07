package harness

import (
	"context"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"payment-hub/internal/testcontrol"
)

const (
	observerDefaultBarrierTimeout = 10 * time.Second
	observerIOTimeout             = 10 * time.Second
)

var errObserverRequestTooLarge = errors.New("observer request exceeds frame limit")

type ObserverConfig struct {
	Socket         string
	RunID          string
	Epoch          string
	BarrierTimeout time.Duration
}

type Counts struct {
	Payment uint64
	Query   uint64
}

type observerGenerationStatus int

const (
	observerGenerationExpected observerGenerationStatus = iota
	observerGenerationOpen
	observerGenerationSealed
)

type observerEvent struct {
	typ     testcontrol.Type
	seq     uint64
	kind    testcontrol.Kind
	point   testcontrol.Point
	orderID string
	eventID string
	ack     testcontrol.Frame
	barrier *Barrier
}

type observerGeneration struct {
	status          observerGenerationStatus
	beginID         string
	events          map[uint64]*observerEvent
	expectedLastSeq uint64
}

type observerBarrierKey struct {
	generation uint64
	point      testcontrol.Point
	orderID    string
}

type Observer struct {
	cfg            ObserverConfig
	barrierTimeout time.Duration
	listener       *net.UnixListener
	socketPath     string
	socketInfo     os.FileInfo
	ctx            context.Context
	cancel         context.CancelFunc
	wg             sync.WaitGroup

	mu          sync.Mutex
	closing     bool
	closeOnce   sync.Once
	closeErr    error
	conns       map[net.Conn]bool
	generations map[uint64]*observerGeneration
	beginIDs    map[string]uint64
	barriers    map[observerBarrierKey]*Barrier
	records     int
	counts      Counts
	uncertain   bool
}

type Barrier struct {
	observer *Observer
	key      observerBarrierKey
	gen      uint64
	point    testcontrol.Point
	orderID  string

	arrivedCh chan struct{}
	releaseCh chan struct{}
	cancelCh  chan struct{}

	arrived  bool
	released bool
	canceled bool
	seq      uint64
	action   testcontrol.Action
	receipt  testcontrol.Receipt
	event    *observerEvent
	reach    testcontrol.Frame
}

func StartObserver(ctx context.Context, cfg ObserverConfig) (*Observer, error) {
	if err := ctx.Err(); err != nil {
		return nil, testcontrol.ErrControlFault
	}
	if !filepath.IsAbs(cfg.Socket) {
		return nil, testcontrol.ErrControlFault
	}
	if !observerValidLowerHex32(cfg.RunID) || !observerValidLowerHex32(cfg.Epoch) {
		return nil, testcontrol.ErrControlFault
	}
	barrierTimeout := cfg.BarrierTimeout
	if barrierTimeout < 0 {
		return nil, testcontrol.ErrControlFault
	}
	if barrierTimeout == 0 {
		barrierTimeout = observerDefaultBarrierTimeout
	}
	if err := observerValidateParent(filepath.Dir(cfg.Socket)); err != nil {
		return nil, testcontrol.ErrControlFault
	}
	if _, err := os.Lstat(cfg.Socket); err == nil {
		return nil, testcontrol.ErrControlFault
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, testcontrol.ErrControlFault
	}

	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: cfg.Socket, Net: "unix"})
	if err != nil {
		return nil, testcontrol.ErrControlFault
	}
	listener.SetUnlinkOnClose(false)

	var socketInfo os.FileInfo
	cleanup := func() {
		_ = listener.Close()
		if socketInfo == nil {
			return
		}
		current, err := os.Lstat(cfg.Socket)
		if err == nil && os.SameFile(current, socketInfo) {
			_ = os.Remove(cfg.Socket)
		}
	}

	socketInfo, err = os.Lstat(cfg.Socket)
	if err != nil {
		cleanup()
		return nil, testcontrol.ErrControlFault
	}
	if err := os.Chmod(cfg.Socket, 0o600); err != nil {
		cleanup()
		return nil, testcontrol.ErrControlFault
	}
	if err := ctx.Err(); err != nil {
		cleanup()
		return nil, testcontrol.ErrControlFault
	}

	observerCtx, cancel := context.WithCancel(context.Background())
	observer := &Observer{
		cfg:            cfg,
		barrierTimeout: barrierTimeout,
		listener:       listener,
		socketPath:     cfg.Socket,
		socketInfo:     socketInfo,
		ctx:            observerCtx,
		cancel:         cancel,
		conns:          make(map[net.Conn]bool),
		generations:    make(map[uint64]*observerGeneration),
		beginIDs:       make(map[string]uint64),
		barriers:       make(map[observerBarrierKey]*Barrier),
	}
	observer.wg.Add(1)
	go observer.acceptLoop()
	return observer, nil
}

func observerValidateParent(path string) error {
	info, err := os.Lstat(path)
	if err != nil {
		return testcontrol.ErrControlFault
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		return testcontrol.ErrControlFault
	}
	if info.Mode().Perm() != 0o700 {
		return testcontrol.ErrControlFault
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != uint32(os.Getuid()) {
		return testcontrol.ErrControlFault
	}
	return nil
}

func (o *Observer) acceptLoop() {
	defer o.wg.Done()
	for {
		conn, err := o.listener.AcceptUnix()
		if err != nil {
			if o.isClosing() {
				return
			}
			select {
			case <-o.ctx.Done():
				return
			default:
			}
			o.latchFault()
			return
		}

		o.mu.Lock()
		if o.closing {
			o.latchFaultLocked()
			o.mu.Unlock()
			_ = conn.Close()
			return
		}
		o.conns[conn] = false
		o.wg.Add(1)
		o.mu.Unlock()
		go o.handleConnection(conn)
	}
}

func (o *Observer) handleConnection(conn *net.UnixConn) {
	defer o.wg.Done()
	defer func() {
		o.mu.Lock()
		delete(o.conns, conn)
		o.mu.Unlock()
		_ = conn.Close()
	}()

	payload, err := o.readRequest(conn)
	if err != nil {
		if !o.isClosing() {
			if errors.Is(err, errObserverRequestTooLarge) {
				o.latchFault()
				o.writeResponse(conn, observerFault(testcontrol.FaultProtocol))
			} else {
				o.latchFault()
			}
		}
		return
	}

	frame, err := testcontrol.DecodeFrame(payload)
	if err != nil {
		o.latchFault()
		o.writeResponse(conn, observerFault(testcontrol.FaultProtocol))
		o.markConnectionComplete(conn)
		return
	}

	response, waitBarrier := o.processFrame(frame)
	if waitBarrier != nil {
		response = o.waitBarrierRelease(waitBarrier)
	}
	o.writeResponse(conn, response)
	o.markConnectionComplete(conn)
}

func (o *Observer) markConnectionComplete(conn net.Conn) {
	o.mu.Lock()
	if _, ok := o.conns[conn]; ok {
		o.conns[conn] = true
	}
	o.mu.Unlock()
}

func (o *Observer) readRequest(conn net.Conn) ([]byte, error) {
	if err := conn.SetReadDeadline(time.Now().Add(observerIOTimeout)); err != nil {
		return nil, err
	}
	limit := int64(testcontrol.MaxFrameBytes) + 1
	reader := &io.LimitedReader{R: conn, N: limit}
	buf := make([]byte, 0, 256)
	tmp := make([]byte, 512)
	for {
		n, err := reader.Read(tmp)
		if n > 0 {
			buf = append(buf, tmp[:n]...)
			if len(buf) > testcontrol.MaxFrameBytes {
				return nil, errObserverRequestTooLarge
			}
		}
		if err != nil {
			if errors.Is(err, io.EOF) {
				return buf, nil
			}
			return nil, err
		}
	}
}

func (o *Observer) writeResponse(conn net.Conn, frame testcontrol.Frame) {
	payload, err := testcontrol.EncodeFrame(frame)
	if err != nil {
		o.latchFault()
		return
	}
	if err := conn.SetWriteDeadline(time.Now().Add(observerIOTimeout)); err != nil {
		if !o.isClosing() {
			o.latchFault()
		}
		return
	}
	n, err := conn.Write(payload)
	if err != nil || n != len(payload) {
		if !o.isClosing() {
			o.latchFault()
		}
	}
}

func (o *Observer) processFrame(frame testcontrol.Frame) (testcontrol.Frame, *Barrier) {
	o.mu.Lock()
	defer o.mu.Unlock()

	if o.closing {
		return observerFault(testcontrol.FaultClosed), nil
	}
	if o.uncertain {
		return observerFault(testcontrol.FaultProtocol), nil
	}
	switch frame.Type {
	case testcontrol.TypeFault, testcontrol.TypeAck:
		o.latchFaultLocked()
		return observerFault(testcontrol.FaultProtocol), nil
	}
	if frame.RunID != o.cfg.RunID || frame.Epoch != o.cfg.Epoch {
		o.latchFaultLocked()
		return observerFault(testcontrol.FaultProtocol), nil
	}

	switch frame.Type {
	case testcontrol.TypeBegin:
		return o.processBeginLocked(frame)
	case testcontrol.TypeCall:
		return o.processCallLocked(frame), nil
	case testcontrol.TypeReach:
		return o.processReachLocked(frame)
	case testcontrol.TypeSeal:
		return o.processSealLocked(frame), nil
	default:
		o.latchFaultLocked()
		return observerFault(testcontrol.FaultProtocol), nil
	}
}

func (o *Observer) processBeginLocked(frame testcontrol.Frame) (testcontrol.Frame, *Barrier) {
	state, ok := o.generations[frame.Generation]
	if !ok {
		o.latchFaultLocked()
		return observerFault(testcontrol.FaultProtocol), nil
	}
	switch state.status {
	case observerGenerationExpected:
		if _, exists := o.beginIDs[frame.EventID]; exists {
			o.latchFaultLocked()
			return observerFault(testcontrol.FaultProtocol), nil
		}
		state.status = observerGenerationOpen
		state.beginID = frame.EventID
		o.beginIDs[frame.EventID] = frame.Generation
		return observerAckBegin(frame), nil
	case observerGenerationOpen:
		if frame.EventID != state.beginID {
			o.latchFaultLocked()
			return observerFault(testcontrol.FaultProtocol), nil
		}
		return observerAckBegin(frame), nil
	default:
		o.latchFaultLocked()
		return observerFault(testcontrol.FaultProtocol), nil
	}
}

func (o *Observer) processCallLocked(frame testcontrol.Frame) testcontrol.Frame {
	state, ok := o.generations[frame.Generation]
	if !ok || state.status != observerGenerationOpen || !observerEventIDBelongsTo(frame.EventID, state.beginID) {
		o.latchFaultLocked()
		return observerFault(testcontrol.FaultProtocol)
	}
	if event, exists := state.events[frame.Seq]; exists {
		if event.typ != testcontrol.TypeCall || event.kind != frame.Kind ||
			event.orderID != frame.OrderID || event.eventID != frame.EventID {
			o.latchFaultLocked()
			return observerFault(testcontrol.FaultProtocol)
		}
		return event.ack
	}
	if o.records >= testcontrol.MaxRecords {
		o.latchFaultLocked()
		return observerFault(testcontrol.FaultProtocol)
	}
	event := &observerEvent{
		typ:     testcontrol.TypeCall,
		seq:     frame.Seq,
		kind:    frame.Kind,
		orderID: frame.OrderID,
		eventID: frame.EventID,
		ack:     observerAckCall(frame),
	}
	state.events[frame.Seq] = event
	o.records++
	switch frame.Kind {
	case testcontrol.KindPayment:
		o.counts.Payment++
	case testcontrol.KindQuery:
		o.counts.Query++
	}
	return event.ack
}

func (o *Observer) processReachLocked(frame testcontrol.Frame) (testcontrol.Frame, *Barrier) {
	state, ok := o.generations[frame.Generation]
	if !ok || state.status != observerGenerationOpen || !observerEventIDBelongsTo(frame.EventID, state.beginID) {
		o.latchFaultLocked()
		return observerFault(testcontrol.FaultProtocol), nil
	}
	if event, exists := state.events[frame.Seq]; exists {
		if event.typ != testcontrol.TypeReach || event.point != frame.Point ||
			event.orderID != frame.OrderID || event.eventID != frame.EventID {
			o.latchFaultLocked()
			return observerFault(testcontrol.FaultProtocol), nil
		}
		if event.barrier != nil && !event.barrier.released && !event.barrier.canceled {
			return testcontrol.Frame{}, event.barrier
		}
		return event.ack, nil
	}
	if o.records >= testcontrol.MaxRecords {
		o.latchFaultLocked()
		return observerFault(testcontrol.FaultProtocol), nil
	}

	event := &observerEvent{
		typ:     testcontrol.TypeReach,
		seq:     frame.Seq,
		point:   frame.Point,
		orderID: frame.OrderID,
		eventID: frame.EventID,
	}
	state.events[frame.Seq] = event
	o.records++

	key := observerBarrierKey{generation: frame.Generation, point: frame.Point, orderID: frame.OrderID}
	if barrier, exists := o.barriers[key]; exists && !barrier.arrived && !barrier.canceled && !barrier.released {
		barrier.arrived = true
		barrier.seq = frame.Seq
		barrier.receipt = testcontrol.Receipt{
			RunID:      o.cfg.RunID,
			Epoch:      o.cfg.Epoch,
			Generation: frame.Generation,
			LastSeq:    frame.Seq,
		}
		barrier.event = event
		barrier.reach = frame
		event.barrier = barrier
		close(barrier.arrivedCh)
		return testcontrol.Frame{}, barrier
	}

	event.ack = observerAckReach(frame, testcontrol.ActionContinue)
	return event.ack, nil
}

func (o *Observer) processSealLocked(frame testcontrol.Frame) testcontrol.Frame {
	state, ok := o.generations[frame.Generation]
	if !ok || state.beginID == "" || frame.EventID != state.beginID+":seal" || frame.ExpectedLastSeq == nil {
		o.latchFaultLocked()
		return observerFault(testcontrol.FaultProtocol)
	}
	if _, err := o.sealLocked(frame.Generation, *frame.ExpectedLastSeq); err != nil {
		o.latchFaultLocked()
		return observerFault(testcontrol.FaultProtocol)
	}
	return observerAckSeal(frame)
}

func (o *Observer) sealLocked(generation, expected uint64) (testcontrol.Receipt, error) {
	state, ok := o.generations[generation]
	if !ok {
		return testcontrol.Receipt{}, testcontrol.ErrControlFault
	}
	if state.status == observerGenerationSealed {
		if expected == state.expectedLastSeq {
			return observerReceipt(o.cfg, generation, expected), nil
		}
		return testcontrol.Receipt{}, testcontrol.ErrControlFault
	}
	if state.status != observerGenerationOpen {
		return testcontrol.Receipt{}, testcontrol.ErrControlFault
	}
	if expected > testcontrol.MaxRecords {
		return testcontrol.Receipt{}, testcontrol.ErrControlFault
	}
	for _, barrier := range o.barriers {
		if barrier.gen == generation && !barrier.released && !barrier.canceled {
			return testcontrol.Receipt{}, testcontrol.ErrControlFault
		}
	}
	if uint64(len(state.events)) != expected {
		return testcontrol.Receipt{}, testcontrol.ErrControlFault
	}
	for seq := uint64(1); seq <= expected; seq++ {
		if _, ok := state.events[seq]; !ok {
			return testcontrol.Receipt{}, testcontrol.ErrControlFault
		}
	}
	state.status = observerGenerationSealed
	state.expectedLastSeq = expected
	return observerReceipt(o.cfg, generation, expected), nil
}

func (o *Observer) waitBarrierRelease(barrier *Barrier) testcontrol.Frame {
	timer := time.NewTimer(o.barrierTimeout)
	defer timer.Stop()
	select {
	case <-barrier.releaseCh:
		return o.barrierReleasedAck(barrier)
	case <-barrier.cancelCh:
		return observerFault(testcontrol.FaultClosed)
	case <-timer.C:
		o.mu.Lock()
		if barrier.released {
			ack := o.barrierReleasedAckLocked(barrier)
			o.mu.Unlock()
			return ack
		}
		o.latchFaultLocked()
		o.mu.Unlock()
		return observerFault(testcontrol.FaultTimeout)
	case <-o.ctx.Done():
		return observerFault(testcontrol.FaultClosed)
	}
}

func (o *Observer) barrierReleasedAck(barrier *Barrier) testcontrol.Frame {
	o.mu.Lock()
	defer o.mu.Unlock()
	return o.barrierReleasedAckLocked(barrier)
}

func (o *Observer) barrierReleasedAckLocked(barrier *Barrier) testcontrol.Frame {
	if barrier.event != nil {
		return barrier.event.ack
	}
	return observerAckReach(barrier.reach, barrier.action)
}

func (o *Observer) Expect(generation uint64) error {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.closing {
		return testcontrol.ErrControlFault
	}
	if o.uncertain {
		return testcontrol.ErrUncertain
	}
	if generation == 0 {
		return testcontrol.ErrProtocol
	}
	if _, exists := o.generations[generation]; exists {
		return testcontrol.ErrProtocol
	}
	if len(o.generations) >= testcontrol.MaxGenerations {
		return testcontrol.ErrProtocol
	}
	o.generations[generation] = &observerGeneration{
		status: observerGenerationExpected,
		events: make(map[uint64]*observerEvent),
	}
	return nil
}

func (o *Observer) Arm(generation uint64, point testcontrol.Point, orderID string) (*Barrier, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.closing {
		return nil, testcontrol.ErrControlFault
	}
	if o.uncertain {
		return nil, testcontrol.ErrUncertain
	}
	if !observerValidPoint(point) || !observerValidUUIDv4(orderID) {
		return nil, testcontrol.ErrProtocol
	}
	state, ok := o.generations[generation]
	if !ok || state.status == observerGenerationSealed {
		return nil, testcontrol.ErrProtocol
	}
	key := observerBarrierKey{generation: generation, point: point, orderID: orderID}
	if _, exists := o.barriers[key]; exists {
		return nil, testcontrol.ErrProtocol
	}
	barrier := &Barrier{
		observer:  o,
		key:       key,
		gen:       generation,
		point:     point,
		orderID:   orderID,
		arrivedCh: make(chan struct{}),
		releaseCh: make(chan struct{}),
		cancelCh:  make(chan struct{}),
	}
	o.barriers[key] = barrier
	return barrier, nil
}

func (b *Barrier) Wait(ctx context.Context) (testcontrol.Receipt, error) {
	o := b.observer
	if ctx.Err() != nil {
		o.mu.Lock()
		o.latchFaultLocked()
		o.mu.Unlock()
		return testcontrol.Receipt{}, testcontrol.ErrControlFault
	}

	timer := time.NewTimer(o.barrierTimeout)
	defer timer.Stop()

	select {
	case <-b.arrivedCh:
		o.mu.Lock()
		if b.canceled {
			o.mu.Unlock()
			return testcontrol.Receipt{}, testcontrol.ErrUncertain
		}
		if ctx.Err() != nil {
			o.latchFaultLocked()
			o.mu.Unlock()
			return testcontrol.Receipt{}, testcontrol.ErrControlFault
		}
		receipt := b.receipt
		o.mu.Unlock()
		return receipt, nil
	case <-b.cancelCh:
		return testcontrol.Receipt{}, testcontrol.ErrUncertain
	case <-ctx.Done():
		o.mu.Lock()
		o.latchFaultLocked()
		o.mu.Unlock()
		return testcontrol.Receipt{}, testcontrol.ErrControlFault
	case <-timer.C:
		o.mu.Lock()
		o.latchFaultLocked()
		o.mu.Unlock()
		return testcontrol.Receipt{}, testcontrol.ErrControlFault
	case <-o.ctx.Done():
		return testcontrol.Receipt{}, testcontrol.ErrUncertain
	}
}

func (b *Barrier) Release(action testcontrol.Action) error {
	if action != testcontrol.ActionContinue && action != testcontrol.ActionReplyHidden {
		return testcontrol.ErrProtocol
	}
	if action == testcontrol.ActionReplyHidden && b.point == testcontrol.PointSavingResult {
		return testcontrol.ErrProtocol
	}
	o := b.observer
	o.mu.Lock()
	defer o.mu.Unlock()
	if b.canceled {
		return testcontrol.ErrUncertain
	}
	if !b.arrived {
		return testcontrol.ErrProtocol
	}
	if b.released {
		return testcontrol.ErrProtocol
	}
	b.action = action
	b.released = true
	if b.event != nil {
		b.event.ack = observerAckReach(b.reach, action)
	}
	close(b.releaseCh)
	return nil
}

func (o *Observer) Seal(generation, expectedLastSeq uint64) (testcontrol.Receipt, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.closing {
		return testcontrol.Receipt{}, testcontrol.ErrControlFault
	}
	if o.uncertain {
		return testcontrol.Receipt{}, testcontrol.ErrUncertain
	}
	receipt, err := o.sealLocked(generation, expectedLastSeq)
	if err != nil {
		o.latchFaultLocked()
		return testcontrol.Receipt{}, testcontrol.ErrUncertain
	}
	return receipt, nil
}

func (o *Observer) Snapshot() (Counts, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.uncertain {
		return Counts{}, testcontrol.ErrUncertain
	}
	if len(o.generations) == 0 {
		return Counts{}, testcontrol.ErrUncertain
	}
	for _, state := range o.generations {
		if state.status != observerGenerationSealed {
			return Counts{}, testcontrol.ErrUncertain
		}
	}
	return o.counts, nil
}

func (o *Observer) Close(ctx context.Context) error {
	o.closeOnce.Do(func() {
		o.closeErr = o.close(ctx)
	})
	return o.closeErr
}

func (o *Observer) close(ctx context.Context) error {
	o.mu.Lock()
	o.closing = true
	o.cancel()

	incomplete := false
	for _, completed := range o.conns {
		if !completed {
			incomplete = true
		}
	}
	for _, barrier := range o.barriers {
		if !barrier.released && !barrier.canceled {
			incomplete = true
			barrier.canceled = true
			close(barrier.cancelCh)
		}
	}
	if incomplete {
		o.uncertain = true
	}
	connections := make([]net.Conn, 0, len(o.conns))
	for conn := range o.conns {
		connections = append(connections, conn)
	}
	o.mu.Unlock()

	if o.listener != nil {
		_ = o.listener.Close()
	}

	for _, conn := range connections {
		_ = conn.Close()
	}

	done := make(chan struct{})
	go func() {
		o.wg.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-ctx.Done():
		return testcontrol.ErrControlFault
	}
	if err := o.removeOwnSocket(); err != nil {
		return err
	}
	return nil
}

func (o *Observer) removeOwnSocket() error {
	if o.socketInfo == nil {
		return testcontrol.ErrControlFault
	}
	current, err := os.Lstat(o.socketPath)
	if err != nil {
		return testcontrol.ErrControlFault
	}
	if !os.SameFile(current, o.socketInfo) {
		return testcontrol.ErrControlFault
	}
	if err := os.Remove(o.socketPath); err != nil {
		return testcontrol.ErrControlFault
	}
	return nil
}

func (o *Observer) isClosing() bool {
	o.mu.Lock()
	defer o.mu.Unlock()
	return o.closing
}

func (o *Observer) latchFault() {
	o.mu.Lock()
	defer o.mu.Unlock()
	o.latchFaultLocked()
}

func (o *Observer) latchFaultLocked() {
	o.uncertain = true
	for _, barrier := range o.barriers {
		if !barrier.released && !barrier.canceled {
			barrier.canceled = true
			close(barrier.cancelCh)
		}
	}
}

func observerReceipt(cfg ObserverConfig, generation, lastSeq uint64) testcontrol.Receipt {
	return testcontrol.Receipt{
		RunID:      cfg.RunID,
		Epoch:      cfg.Epoch,
		Generation: generation,
		LastSeq:    lastSeq,
	}
}

func observerAckBegin(req testcontrol.Frame) testcontrol.Frame {
	return testcontrol.Frame{
		Version:    testcontrol.Version,
		Type:       testcontrol.TypeAck,
		RunID:      req.RunID,
		Epoch:      req.Epoch,
		Generation: req.Generation,
		EventID:    req.EventID,
		For:        testcontrol.TypeBegin,
	}
}

func observerAckCall(req testcontrol.Frame) testcontrol.Frame {
	return testcontrol.Frame{
		Version:    testcontrol.Version,
		Type:       testcontrol.TypeAck,
		RunID:      req.RunID,
		Epoch:      req.Epoch,
		Generation: req.Generation,
		Seq:        req.Seq,
		EventID:    req.EventID,
		For:        testcontrol.TypeCall,
	}
}

func observerAckReach(req testcontrol.Frame, action testcontrol.Action) testcontrol.Frame {
	return testcontrol.Frame{
		Version:    testcontrol.Version,
		Type:       testcontrol.TypeAck,
		RunID:      req.RunID,
		Epoch:      req.Epoch,
		Generation: req.Generation,
		Seq:        req.Seq,
		EventID:    req.EventID,
		For:        testcontrol.TypeReach,
		Action:     action,
	}
}

func observerAckSeal(req testcontrol.Frame) testcontrol.Frame {
	frame := testcontrol.Frame{
		Version:    testcontrol.Version,
		Type:       testcontrol.TypeAck,
		RunID:      req.RunID,
		Epoch:      req.Epoch,
		Generation: req.Generation,
		EventID:    req.EventID,
		For:        testcontrol.TypeSeal,
	}
	if req.ExpectedLastSeq != nil {
		expected := *req.ExpectedLastSeq
		frame.ExpectedLastSeq = &expected
	}
	return frame
}

func observerFault(code testcontrol.FaultCode) testcontrol.Frame {
	return testcontrol.Frame{
		Version: testcontrol.Version,
		Type:    testcontrol.TypeFault,
		Code:    code,
	}
}

func observerEventIDBelongsTo(eventID, beginID string) bool {
	if beginID == "" {
		return false
	}
	index := strings.LastIndexByte(eventID, ':')
	return index == len(beginID) && eventID[:index] == beginID
}

func observerValidPoint(value testcontrol.Point) bool {
	switch value {
	case testcontrol.PointRegistered, testcontrol.PointMockCommitted,
		testcontrol.PointSavingResult, testcontrol.PointResultCommitted:
		return true
	default:
		return false
	}
}

func observerValidLowerHex32(value string) bool {
	if len(value) != 32 {
		return false
	}
	for i := 0; i < len(value); i++ {
		c := value[i]
		if (c < '0' || c > '9') && (c < 'a' || c > 'f') {
			return false
		}
	}
	return true
}

func observerValidUUIDv4(value string) bool {
	if len(value) != 36 {
		return false
	}
	for i := 0; i < len(value); i++ {
		switch i {
		case 8, 13, 18, 23:
			if value[i] != '-' {
				return false
			}
		default:
			if !observerIsHexDigit(value[i]) {
				return false
			}
		}
	}
	if value[14] != '4' {
		return false
	}
	switch value[19] {
	case '8', '9', 'a', 'b', 'A', 'B':
		return true
	default:
		return false
	}
}

func observerIsHexDigit(c byte) bool {
	return c >= '0' && c <= '9' || c >= 'a' && c <= 'f' || c >= 'A' && c <= 'F'
}
