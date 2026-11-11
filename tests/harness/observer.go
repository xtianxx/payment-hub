package harness

import (
	"context"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	"payment-hub/internal/testcontrol"
)

const (
	observerDefaultBarrierTimeout = 10 * time.Second
	observerIOTimeout             = 10 * time.Second
	observerPaymentHoldTimeout    = 2 * time.Second
)

var errObserverRequestTooLarge = errors.New("observer request exceeds frame limit")

type ObserverConfig struct {
	Socket         string
	RunID          string
	Epoch          string
	BarrierTimeout time.Duration
	// Version is the explicit protocol opt-in. Zero keeps the legacy v1
	// boundary; v2 enables query-control.
	Version int
}

type Counts struct {
	Payment uint64
	Query   uint64
}

// ObserverFinalSnapshot is the immutable final observation produced once a
// successful Observer.Close has drained the listener and all connections.
type ObserverFinalSnapshot struct {
	RunID    string
	Epoch    string
	Counts   Counts
	Receipts []testcontrol.Receipt
	Trusted  bool
}

type observerGenerationStatus int

const (
	observerGenerationExpected observerGenerationStatus = iota
	observerGenerationOpen
	observerGenerationSealed
	observerGenerationTruncated
)

type observerEvent struct {
	typ                 testcontrol.Type
	seq                 uint64
	kind                testcontrol.Kind
	point               testcontrol.Point
	orderID             string
	eventID             string
	ack                 testcontrol.Frame
	barrier             *Barrier
	paymentHold         *PaymentHold
	queryHold           *QueryHold
	paymentCallSeq      *uint64
	appliedResult       *testcontrol.Directive
	queryCallSeq        *uint64
	queryOutcome        *testcontrol.QueryOutcome
	queryApplied        bool
	queryAppliedSeq     uint64
	queryAppliedEventID string
	queryAppliedOutcome testcontrol.QueryOutcome
}

type observerGeneration struct {
	status          observerGenerationStatus
	beginID         string
	events          map[uint64]*observerEvent
	expectedLastSeq uint64
	counts          Counts
	truncated       *ControlledTerminationBinding
}

type observerBarrierKey struct {
	generation uint64
	point      testcontrol.Point
	orderID    string
}

type observerNextBarrierKey struct {
	generation uint64
	point      testcontrol.Point
}

type Observer struct {
	cfg            ObserverConfig
	version        int
	barrierTimeout time.Duration
	listener       *net.UnixListener
	socketPath     string
	socketInfo     os.FileInfo
	ctx            context.Context
	cancel         context.CancelFunc
	wg             sync.WaitGroup

	mu        sync.Mutex
	closing   bool
	closeOnce sync.Once
	closeErr  error
	conns     map[net.Conn]bool
	connDone  map[net.Conn]chan struct{}
	// testBarrierBind is a nil-default, package-private determinism seam. It is
	// set under mu before traffic and invoked after the bind decision.
	testBarrierBind func(*Barrier, net.Conn, bool)
	generations     map[uint64]*observerGeneration
	beginIDs        map[string]uint64
	barriers        map[observerBarrierKey]*Barrier
	nextBarriers    map[observerNextBarrierKey]*Barrier
	paymentHolds    map[uint64]*PaymentHold
	queryHolds      map[uint64]*QueryHold
	expectedTerm    map[net.Conn]bool
	records         int
	counts          Counts
	uncertain       bool

	finalSnapshot ObserverFinalSnapshot
	finalReady    bool
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
	conn     net.Conn
	frozen   bool
}

// PaymentBinding is the immutable payment call identity captured when an armed
// PaymentHold binds to the generation's payment entry call.
type PaymentBinding struct {
	RunID      string
	Epoch      string
	Generation uint64
	CallSeq    uint64
	EventID    string
	OrderID    string
}

// QueryBinding is the immutable query call identity captured when an armed
// QueryHold binds to the generation's next query entry call.
type QueryBinding struct {
	RunID      string
	Epoch      string
	Generation uint64
	CallSeq    uint64
	EventID    string
	OrderID    string
}

// QueryHold is a one-per-generation fixture for selecting a query scenario for
// the next query call and proving its later QUERY_APPLIED consumption.
type QueryHold struct {
	observer *Observer
	gen      uint64
	deadline time.Time

	arrivedCh chan struct{}
	releaseCh chan struct{}
	cancelCh  chan struct{}

	status   queryHoldStatus
	binding  QueryBinding
	scenario testcontrol.QueryScenario
	event    *observerEvent
	call     testcontrol.Frame
	ack      testcontrol.Frame
	canceled bool
}

type queryHoldStatus int

const (
	queryHoldArmed queryHoldStatus = iota
	queryHoldBound
	queryHoldReleased
	queryHoldConsumed
)

// HoldQueryEntry arms the single query scenario fixture for one expected or
// open generation whose next query entry has not occurred yet.
func (o *Observer) HoldQueryEntry(generation uint64) (*QueryHold, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.version != testcontrol.Version2 {
		return nil, testcontrol.ErrProtocol
	}
	if o.closing {
		return nil, testcontrol.ErrControlFault
	}
	if o.uncertain {
		return nil, testcontrol.ErrUncertain
	}
	state, ok := o.generations[generation]
	if !ok || state.status == observerGenerationSealed || state.status == observerGenerationTruncated {
		return nil, testcontrol.ErrProtocol
	}
	if _, exists := o.queryHolds[generation]; exists {
		return nil, testcontrol.ErrProtocol
	}
	for _, event := range state.events {
		if event.typ == testcontrol.TypeCall && event.kind == testcontrol.KindQuery {
			return nil, testcontrol.ErrProtocol
		}
	}
	hold := &QueryHold{
		observer:  o,
		gen:       generation,
		deadline:  time.Now().Add(observerPaymentHoldTimeout),
		arrivedCh: make(chan struct{}),
		releaseCh: make(chan struct{}),
		cancelCh:  make(chan struct{}),
	}
	o.queryHolds[generation] = hold
	return hold, nil
}

// Wait waits for the generation's query entry call to bind this hold.
func (h *QueryHold) Wait(ctx context.Context) (QueryBinding, error) {
	if h == nil || h.observer == nil {
		return QueryBinding{}, testcontrol.ErrControlFault
	}
	o := h.observer
	if ctx == nil || ctx.Err() != nil {
		o.mu.Lock()
		o.latchFaultLocked()
		o.mu.Unlock()
		return QueryBinding{}, testcontrol.ErrControlFault
	}

	o.mu.Lock()
	if h.canceled {
		o.mu.Unlock()
		return QueryBinding{}, testcontrol.ErrUncertain
	}
	if h.status != queryHoldArmed {
		binding := h.binding
		o.mu.Unlock()
		return binding, nil
	}
	deadline := h.deadline
	o.mu.Unlock()

	timer := time.NewTimer(time.Until(deadline))
	defer timer.Stop()
	select {
	case <-h.arrivedCh:
		o.mu.Lock()
		if h.canceled {
			o.mu.Unlock()
			return QueryBinding{}, testcontrol.ErrUncertain
		}
		if ctx.Err() != nil {
			o.latchFaultLocked()
			o.mu.Unlock()
			return QueryBinding{}, testcontrol.ErrControlFault
		}
		binding := h.binding
		o.mu.Unlock()
		return binding, nil
	case <-h.cancelCh:
		return QueryBinding{}, testcontrol.ErrUncertain
	case <-ctx.Done():
		o.mu.Lock()
		canceled := h.canceled
		if !canceled {
			o.latchFaultLocked()
		}
		o.mu.Unlock()
		if canceled {
			return QueryBinding{}, testcontrol.ErrUncertain
		}
		return QueryBinding{}, testcontrol.ErrControlFault
	case <-timer.C:
		o.mu.Lock()
		if h.canceled {
			o.mu.Unlock()
			return QueryBinding{}, testcontrol.ErrUncertain
		}
		if h.status != queryHoldArmed {
			binding := h.binding
			o.mu.Unlock()
			return binding, nil
		}
		o.latchFaultLocked()
		o.mu.Unlock()
		return QueryBinding{}, testcontrol.ErrUncertain
	case <-o.ctx.Done():
		return QueryBinding{}, testcontrol.ErrUncertain
	}
}

// Release selects the query scenario for the bound query entry.
func (h *QueryHold) Release(scenario testcontrol.QueryScenario) error {
	if h == nil || h.observer == nil {
		return testcontrol.ErrControlFault
	}
	if !observerValidQueryScenario(scenario) {
		return testcontrol.ErrProtocol
	}
	o := h.observer
	o.mu.Lock()
	defer o.mu.Unlock()
	if h.canceled {
		return testcontrol.ErrUncertain
	}
	if h.status != queryHoldBound || h.event == nil {
		return testcontrol.ErrProtocol
	}
	if !time.Now().Before(h.deadline) {
		o.latchFaultLocked()
		return testcontrol.ErrUncertain
	}
	h.scenario = scenario
	h.status = queryHoldReleased
	ack := observerAckCall(h.call)
	value := scenario
	ack.QueryDirective = &value
	h.event.ack = ack
	h.ack = ack
	close(h.releaseCh)
	return nil
}

func (o *Observer) waitQueryHold(hold *QueryHold) testcontrol.Frame {
	if hold == nil {
		return observerFault(testcontrol.FaultProtocol)
	}
	timer := time.NewTimer(time.Until(hold.deadline))
	defer timer.Stop()
	select {
	case <-hold.releaseCh:
		o.mu.Lock()
		if hold.canceled {
			o.mu.Unlock()
			return observerFault(testcontrol.FaultClosed)
		}
		ack := hold.ack
		o.mu.Unlock()
		return ack
	case <-hold.cancelCh:
		return observerFault(testcontrol.FaultClosed)
	case <-timer.C:
		o.mu.Lock()
		if hold.canceled {
			o.mu.Unlock()
			return observerFault(testcontrol.FaultClosed)
		}
		if hold.status == queryHoldReleased || hold.status == queryHoldConsumed {
			ack := hold.ack
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

func (h *QueryHold) cancelLocked() {
	if h.canceled {
		return
	}
	h.canceled = true
	close(h.cancelCh)
}

// PaymentHold is a one-per-generation fixture for selecting a result directive
// and proving its consumption on the existing MOCK_COMMITTED reach.
type PaymentHold struct {
	observer *Observer
	gen      uint64
	deadline time.Time

	arrivedCh chan struct{}
	releaseCh chan struct{}
	cancelCh  chan struct{}

	status    paymentHoldStatus
	binding   PaymentBinding
	directive testcontrol.Directive
	event     *observerEvent
	call      testcontrol.Frame
	ack       testcontrol.Frame
	canceled  bool
}

type paymentHoldStatus int

const (
	paymentHoldArmed paymentHoldStatus = iota
	paymentHoldBound
	paymentHoldReleased
	paymentHoldConsumed
)

// HoldPaymentEntry arms the single payment result directive fixture for one
// expected or open generation whose payment entry has not occurred yet.
func (o *Observer) HoldPaymentEntry(generation uint64) (*PaymentHold, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.closing {
		return nil, testcontrol.ErrControlFault
	}
	if o.uncertain {
		return nil, testcontrol.ErrUncertain
	}
	state, ok := o.generations[generation]
	if !ok || state.status == observerGenerationSealed {
		return nil, testcontrol.ErrProtocol
	}
	if _, exists := o.paymentHolds[generation]; exists {
		return nil, testcontrol.ErrProtocol
	}
	for _, event := range state.events {
		if event.typ == testcontrol.TypeCall && event.kind == testcontrol.KindPayment {
			return nil, testcontrol.ErrProtocol
		}
	}
	hold := &PaymentHold{
		observer:  o,
		gen:       generation,
		deadline:  time.Now().Add(observerPaymentHoldTimeout),
		arrivedCh: make(chan struct{}),
		releaseCh: make(chan struct{}),
		cancelCh:  make(chan struct{}),
	}
	o.paymentHolds[generation] = hold
	return hold, nil
}

// Wait waits for the generation's payment entry call to bind this hold.
func (h *PaymentHold) Wait(ctx context.Context) (PaymentBinding, error) {
	if h == nil || h.observer == nil {
		return PaymentBinding{}, testcontrol.ErrControlFault
	}
	o := h.observer
	if ctx == nil || ctx.Err() != nil {
		o.mu.Lock()
		o.latchFaultLocked()
		o.mu.Unlock()
		return PaymentBinding{}, testcontrol.ErrControlFault
	}

	o.mu.Lock()
	if h.canceled {
		o.mu.Unlock()
		return PaymentBinding{}, testcontrol.ErrUncertain
	}
	if h.status != paymentHoldArmed {
		binding := h.binding
		o.mu.Unlock()
		return binding, nil
	}
	deadline := h.deadline
	o.mu.Unlock()

	timer := time.NewTimer(time.Until(deadline))
	defer timer.Stop()
	select {
	case <-h.arrivedCh:
		o.mu.Lock()
		if h.canceled {
			o.mu.Unlock()
			return PaymentBinding{}, testcontrol.ErrUncertain
		}
		if ctx.Err() != nil {
			o.latchFaultLocked()
			o.mu.Unlock()
			return PaymentBinding{}, testcontrol.ErrControlFault
		}
		binding := h.binding
		o.mu.Unlock()
		return binding, nil
	case <-h.cancelCh:
		return PaymentBinding{}, testcontrol.ErrUncertain
	case <-ctx.Done():
		o.mu.Lock()
		canceled := h.canceled
		if !canceled {
			o.latchFaultLocked()
		}
		o.mu.Unlock()
		if canceled {
			return PaymentBinding{}, testcontrol.ErrUncertain
		}
		return PaymentBinding{}, testcontrol.ErrControlFault
	case <-timer.C:
		o.mu.Lock()
		if h.canceled {
			o.mu.Unlock()
			return PaymentBinding{}, testcontrol.ErrUncertain
		}
		if h.status != paymentHoldArmed {
			binding := h.binding
			o.mu.Unlock()
			return binding, nil
		}
		o.latchFaultLocked()
		o.mu.Unlock()
		return PaymentBinding{}, testcontrol.ErrUncertain
	case <-o.ctx.Done():
		return PaymentBinding{}, testcontrol.ErrUncertain
	}
}

// Release selects the result directive for the bound payment entry.
func (h *PaymentHold) Release(directive testcontrol.Directive) error {
	if h == nil || h.observer == nil {
		return testcontrol.ErrControlFault
	}
	if !observerValidDirective(directive) {
		return testcontrol.ErrProtocol
	}
	o := h.observer
	o.mu.Lock()
	defer o.mu.Unlock()
	if h.canceled {
		return testcontrol.ErrUncertain
	}
	if h.status != paymentHoldBound || h.event == nil {
		return testcontrol.ErrProtocol
	}
	if !time.Now().Before(h.deadline) {
		o.latchFaultLocked()
		return testcontrol.ErrUncertain
	}
	h.directive = directive
	h.status = paymentHoldReleased
	ack := observerAckCall(h.call)
	value := directive
	ack.Directive = &value
	h.event.ack = ack
	h.ack = ack
	close(h.releaseCh)
	return nil
}

func (o *Observer) waitPaymentHold(hold *PaymentHold) testcontrol.Frame {
	if hold == nil {
		return observerFault(testcontrol.FaultProtocol)
	}
	timer := time.NewTimer(time.Until(hold.deadline))
	defer timer.Stop()
	select {
	case <-hold.releaseCh:
		o.mu.Lock()
		if hold.canceled {
			o.mu.Unlock()
			return observerFault(testcontrol.FaultClosed)
		}
		ack := hold.ack
		o.mu.Unlock()
		return ack
	case <-hold.cancelCh:
		return observerFault(testcontrol.FaultClosed)
	case <-timer.C:
		o.mu.Lock()
		if hold.canceled {
			o.mu.Unlock()
			return observerFault(testcontrol.FaultClosed)
		}
		if hold.status == paymentHoldReleased || hold.status == paymentHoldConsumed {
			ack := hold.ack
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

func (h *PaymentHold) cancelLocked() {
	if h.canceled {
		return
	}
	h.canceled = true
	close(h.cancelCh)
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
	version := cfg.Version
	if version == 0 {
		version = testcontrol.Version
	}
	if version != testcontrol.Version && version != testcontrol.Version2 {
		return nil, testcontrol.ErrControlFault
	}
	cfg.Version = version
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
		version:        version,
		barrierTimeout: barrierTimeout,
		listener:       listener,
		socketPath:     cfg.Socket,
		socketInfo:     socketInfo,
		ctx:            observerCtx,
		cancel:         cancel,
		conns:          make(map[net.Conn]bool),
		connDone:       make(map[net.Conn]chan struct{}),
		generations:    make(map[uint64]*observerGeneration),
		beginIDs:       make(map[string]uint64),
		barriers:       make(map[observerBarrierKey]*Barrier),
		nextBarriers:   make(map[observerNextBarrierKey]*Barrier),
		paymentHolds:   make(map[uint64]*PaymentHold),
		queryHolds:     make(map[uint64]*QueryHold),
		expectedTerm:   make(map[net.Conn]bool),
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
		o.connDone[conn] = make(chan struct{})
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
		if done, ok := o.connDone[conn]; ok {
			delete(o.connDone, conn)
			close(done)
		}
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

	response, waitBarrier, waitHold, waitQuery := o.processFrame(frame, conn)
	if waitBarrier != nil {
		if !o.bindBarrierConn(waitBarrier, conn) {
			response = observerFault(testcontrol.FaultProtocol)
		} else {
			response = o.waitBarrierRelease(waitBarrier)
		}
	}
	if waitHold != nil {
		response = o.waitPaymentHold(waitHold)
	}
	if waitQuery != nil {
		response = o.waitQueryHold(waitQuery)
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
		o.connectionFailure(conn)
		return
	}
	if err := conn.SetWriteDeadline(time.Now().Add(observerIOTimeout)); err != nil {
		o.connectionFailure(conn)
		return
	}
	n, err := conn.Write(payload)
	if err != nil || n != len(payload) {
		o.connectionFailure(conn)
	}
}

func (o *Observer) connectionFailure(conn net.Conn) {
	o.mu.Lock()
	expected := o.expectedTerm[conn]
	if expected {
		delete(o.expectedTerm, conn)
	}
	closing := o.closing
	o.mu.Unlock()
	if expected || closing {
		return
	}
	o.latchFault()
}

func (o *Observer) processFrame(frame testcontrol.Frame, conn net.Conn) (testcontrol.Frame, *Barrier, *PaymentHold, *QueryHold) {
	o.mu.Lock()
	defer o.mu.Unlock()

	if o.closing {
		return observerFault(testcontrol.FaultClosed), nil, nil, nil
	}
	if o.uncertain {
		return observerFault(testcontrol.FaultProtocol), nil, nil, nil
	}
	if frame.Version != o.version {
		o.latchFaultLocked()
		return observerFault(testcontrol.FaultProtocol), nil, nil, nil
	}
	switch frame.Type {
	case testcontrol.TypeFault, testcontrol.TypeAck:
		o.latchFaultLocked()
		return observerFault(testcontrol.FaultProtocol), nil, nil, nil
	}
	if frame.RunID != o.cfg.RunID || frame.Epoch != o.cfg.Epoch {
		o.latchFaultLocked()
		return observerFault(testcontrol.FaultProtocol), nil, nil, nil
	}

	switch frame.Type {
	case testcontrol.TypeBegin:
		response, barrier := o.processBeginLocked(frame)
		return response, barrier, nil, nil
	case testcontrol.TypeCall:
		response, hold, queryHold := o.processCallLocked(frame, conn)
		return response, nil, hold, queryHold
	case testcontrol.TypeReach:
		response, barrier := o.processReachLocked(frame, conn)
		return response, barrier, nil, nil
	case testcontrol.TypeSeal:
		return o.processSealLocked(frame), nil, nil, nil
	default:
		o.latchFaultLocked()
		return observerFault(testcontrol.FaultProtocol), nil, nil, nil
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

func (o *Observer) processCallLocked(frame testcontrol.Frame, conn net.Conn) (testcontrol.Frame, *PaymentHold, *QueryHold) {
	state, ok := o.generations[frame.Generation]
	if !ok || state.status != observerGenerationOpen || !observerEventIDBelongsTo(frame.EventID, state.beginID) {
		o.latchFaultLocked()
		return observerFault(testcontrol.FaultProtocol), nil, nil
	}
	if event, exists := state.events[frame.Seq]; exists {
		if event.typ != testcontrol.TypeCall || event.kind != frame.Kind ||
			event.orderID != frame.OrderID || event.eventID != frame.EventID {
			o.latchFaultLocked()
			return observerFault(testcontrol.FaultProtocol), nil, nil
		}
		if event.paymentHold != nil {
			hold := event.paymentHold
			if hold.canceled {
				return observerFault(testcontrol.FaultClosed), nil, nil
			}
			switch hold.status {
			case paymentHoldBound:
				return testcontrol.Frame{}, hold, nil
			case paymentHoldReleased, paymentHoldConsumed:
				return event.ack, nil, nil
			}
		}
		if event.queryHold != nil {
			hold := event.queryHold
			if hold.canceled {
				return observerFault(testcontrol.FaultClosed), nil, nil
			}
			switch hold.status {
			case queryHoldBound:
				return testcontrol.Frame{}, nil, hold
			case queryHoldReleased, queryHoldConsumed:
				return event.ack, nil, nil
			}
		}
		return event.ack, nil, nil
	}
	if o.records >= testcontrol.MaxRecords {
		o.latchFaultLocked()
		return observerFault(testcontrol.FaultProtocol), nil, nil
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
		state.counts.Payment++
		o.counts.Payment++
	case testcontrol.KindQuery:
		state.counts.Query++
		o.counts.Query++
	}
	if frame.Kind == testcontrol.KindPayment {
		if hold, exists := o.paymentHolds[frame.Generation]; exists {
			if hold.canceled || hold.status != paymentHoldArmed {
				// Record and count the independent payment before declaring the
				// fixture uncertain; a control rejection must not stand in for
				// business duplicate prevention.
				o.latchFaultLocked()
				return observerFault(testcontrol.FaultProtocol), nil, nil
			}
			if !time.Now().Before(hold.deadline) {
				o.latchFaultLocked()
				return observerFault(testcontrol.FaultTimeout), nil, nil
			}
			hold.status = paymentHoldBound
			hold.binding = PaymentBinding{
				RunID:      o.cfg.RunID,
				Epoch:      o.cfg.Epoch,
				Generation: frame.Generation,
				CallSeq:    frame.Seq,
				EventID:    frame.EventID,
				OrderID:    frame.OrderID,
			}
			hold.event = event
			hold.call = frame
			event.paymentHold = hold
			close(hold.arrivedCh)
			return testcontrol.Frame{}, hold, nil
		}
	}
	if frame.Kind == testcontrol.KindQuery {
		if hold, exists := o.queryHolds[frame.Generation]; exists && !hold.canceled &&
			hold.status == queryHoldArmed {
			if !time.Now().Before(hold.deadline) {
				o.latchFaultLocked()
				return observerFault(testcontrol.FaultTimeout), nil, nil
			}
			hold.status = queryHoldBound
			hold.binding = QueryBinding{
				RunID:      o.cfg.RunID,
				Epoch:      o.cfg.Epoch,
				Generation: frame.Generation,
				CallSeq:    frame.Seq,
				EventID:    frame.EventID,
				OrderID:    frame.OrderID,
			}
			hold.event = event
			hold.call = frame
			event.queryHold = hold
			close(hold.arrivedCh)
			return testcontrol.Frame{}, nil, hold
		}
	}
	return event.ack, nil, nil
}

func (o *Observer) processReachLocked(frame testcontrol.Frame, conn net.Conn) (testcontrol.Frame, *Barrier) {
	state, ok := o.generations[frame.Generation]
	if !ok || state.status != observerGenerationOpen || !observerEventIDBelongsTo(frame.EventID, state.beginID) {
		o.latchFaultLocked()
		return observerFault(testcontrol.FaultProtocol), nil
	}
	if event, exists := state.events[frame.Seq]; exists {
		if event.typ != testcontrol.TypeReach || event.point != frame.Point ||
			event.orderID != frame.OrderID || event.eventID != frame.EventID ||
			!observerUintPtrEqual(event.paymentCallSeq, frame.PaymentCallSeq) ||
			!observerDirectivePtrEqual(event.appliedResult, frame.AppliedResult) ||
			!observerUintPtrEqual(event.queryCallSeq, frame.QueryCallSeq) ||
			!observerOutcomePtrEqual(event.queryOutcome, frame.QueryOutcome) {
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

	if frame.Point == testcontrol.PointMockCommitted && frame.PaymentCallSeq == nil {
		if hold, exists := o.paymentHolds[frame.Generation]; exists && !hold.canceled &&
			(hold.status == paymentHoldBound || hold.status == paymentHoldReleased) {
			o.latchFaultLocked()
			return observerFault(testcontrol.FaultProtocol), nil
		}
	}

	var proofCallSeq *uint64
	var proofResult *testcontrol.Directive
	if frame.PaymentCallSeq != nil {
		if err := o.consumePaymentProofLocked(frame); err != nil {
			o.latchFaultLocked()
			return observerFault(testcontrol.FaultProtocol), nil
		}
		callSeq := *frame.PaymentCallSeq
		result := *frame.AppliedResult
		proofCallSeq = &callSeq
		proofResult = &result
	}

	var queryCallSeq *uint64
	var queryOutcome *testcontrol.QueryOutcome
	if frame.QueryCallSeq != nil {
		if err := o.consumeQueryAppliedLocked(frame); err != nil {
			o.latchFaultLocked()
			return observerFault(testcontrol.FaultProtocol), nil
		}
		callSeq := *frame.QueryCallSeq
		outcome := *frame.QueryOutcome
		queryCallSeq = &callSeq
		queryOutcome = &outcome
	}

	event := &observerEvent{
		typ:            testcontrol.TypeReach,
		seq:            frame.Seq,
		point:          frame.Point,
		orderID:        frame.OrderID,
		eventID:        frame.EventID,
		paymentCallSeq: proofCallSeq,
		appliedResult:  proofResult,
		queryCallSeq:   queryCallSeq,
		queryOutcome:   queryOutcome,
	}
	state.events[frame.Seq] = event
	o.records++

	if frame.PaymentCallSeq == nil && frame.QueryCallSeq == nil {
		key := observerBarrierKey{generation: frame.Generation, point: frame.Point, orderID: frame.OrderID}
		nextKey := observerNextBarrierKey{generation: frame.Generation, point: frame.Point}
		exact, hasExact := o.barriers[key]
		next, hasNext := o.nextBarriers[nextKey]
		if hasExact && hasNext {
			o.latchFaultLocked()
			return observerFault(testcontrol.FaultProtocol), nil
		}
		if hasExact && !exact.arrived && !exact.canceled && !exact.released {
			exact.arrived = true
			exact.seq = frame.Seq
			exact.conn = conn
			exact.receipt = testcontrol.Receipt{
				RunID:      o.cfg.RunID,
				Epoch:      o.cfg.Epoch,
				Generation: frame.Generation,
				LastSeq:    frame.Seq,
			}
			exact.event = event
			exact.reach = frame
			event.barrier = exact
			close(exact.arrivedCh)
			return testcontrol.Frame{}, exact
		}
		if hasNext && !next.arrived && !next.canceled && !next.released {
			next.arrived = true
			next.orderID = frame.OrderID
			next.seq = frame.Seq
			next.conn = conn
			next.receipt = testcontrol.Receipt{
				RunID:      o.cfg.RunID,
				Epoch:      o.cfg.Epoch,
				Generation: frame.Generation,
				LastSeq:    frame.Seq,
			}
			next.event = event
			next.reach = frame
			event.barrier = next
			close(next.arrivedCh)
			return testcontrol.Frame{}, next
		}
	}

	event.ack = observerAckReach(frame, testcontrol.ActionContinue)
	return event.ack, nil
}

func (o *Observer) consumeQueryAppliedLocked(frame testcontrol.Frame) error {
	if frame.Point != testcontrol.PointQueryApplied ||
		frame.QueryCallSeq == nil || frame.QueryOutcome == nil ||
		*frame.QueryCallSeq < 1 || *frame.QueryCallSeq >= frame.Seq {
		return testcontrol.ErrControlFault
	}
	state, ok := o.generations[frame.Generation]
	if !ok {
		return testcontrol.ErrControlFault
	}
	call, ok := state.events[*frame.QueryCallSeq]
	if !ok || call.typ != testcontrol.TypeCall || call.kind != testcontrol.KindQuery ||
		call.orderID != frame.OrderID || call.queryApplied {
		return testcontrol.ErrControlFault
	}
	if call.queryHold != nil {
		if call.queryHold.canceled {
			return testcontrol.ErrControlFault
		}
		if call.queryHold.status != queryHoldReleased && call.queryHold.status != queryHoldConsumed {
			return testcontrol.ErrControlFault
		}
	}
	call.queryApplied = true
	call.queryAppliedSeq = frame.Seq
	call.queryAppliedEventID = frame.EventID
	call.queryAppliedOutcome = *frame.QueryOutcome
	if call.queryHold != nil {
		call.queryHold.status = queryHoldConsumed
	}
	return nil
}

func (o *Observer) consumePaymentProofLocked(frame testcontrol.Frame) error {
	if frame.Point != testcontrol.PointMockCommitted ||
		frame.PaymentCallSeq == nil || frame.AppliedResult == nil ||
		*frame.PaymentCallSeq < 1 || *frame.PaymentCallSeq >= frame.Seq {
		return testcontrol.ErrControlFault
	}
	hold, ok := o.paymentHolds[frame.Generation]
	if !ok || hold.canceled || hold.status != paymentHoldReleased || hold.event == nil {
		return testcontrol.ErrControlFault
	}
	if hold.binding.RunID != frame.RunID || hold.binding.Epoch != frame.Epoch ||
		hold.binding.Generation != frame.Generation ||
		hold.binding.CallSeq != *frame.PaymentCallSeq ||
		hold.binding.OrderID != frame.OrderID {
		return testcontrol.ErrControlFault
	}
	state, ok := o.generations[frame.Generation]
	if !ok {
		return testcontrol.ErrControlFault
	}
	event, ok := state.events[*frame.PaymentCallSeq]
	if !ok || event != hold.event || event.typ != testcontrol.TypeCall ||
		event.kind != testcontrol.KindPayment || event.orderID != frame.OrderID ||
		event.eventID != hold.binding.EventID {
		return testcontrol.ErrControlFault
	}
	if hold.directive != *frame.AppliedResult {
		return testcontrol.ErrControlFault
	}
	hold.status = paymentHoldConsumed
	return nil
}

func observerUintPtrEqual(a, b *uint64) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	return *a == *b
}

func observerOutcomePtrEqual(a, b *testcontrol.QueryOutcome) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	return *a == *b
}

func observerValidQueryScenario(value testcontrol.QueryScenario) bool {
	switch value {
	case testcontrol.QueryScenarioNormal, testcontrol.QueryScenarioFailure,
		testcontrol.QueryScenarioSubtimeout, testcontrol.QueryScenarioNotVisible,
		testcontrol.QueryScenarioMismatchReference, testcontrol.QueryScenarioMismatchAmount,
		testcontrol.QueryScenarioMismatchCurrency, testcontrol.QueryScenarioMismatchTransaction:
		return true
	default:
		return false
	}
}

func observerDirectivePtrEqual(a, b *testcontrol.Directive) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	return *a == *b
}

func observerValidDirective(value testcontrol.Directive) bool {
	return value == testcontrol.DirectiveSucceeded || value == testcontrol.DirectiveFailed
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
	for key, barrier := range o.nextBarriers {
		if key.generation == generation && !barrier.released && !barrier.canceled {
			return testcontrol.Receipt{}, testcontrol.ErrControlFault
		}
	}
	for holdGen, hold := range o.paymentHolds {
		if holdGen != generation {
			continue
		}
		if hold.canceled || hold.status != paymentHoldConsumed {
			return testcontrol.Receipt{}, testcontrol.ErrControlFault
		}
	}
	for holdGen, hold := range o.queryHolds {
		if holdGen != generation {
			continue
		}
		if hold.canceled || hold.status != queryHoldConsumed {
			return testcontrol.Receipt{}, testcontrol.ErrControlFault
		}
	}
	if o.version == testcontrol.Version2 {
		for _, event := range state.events {
			if event.typ == testcontrol.TypeCall && event.kind == testcontrol.KindQuery && !event.queryApplied {
				return testcontrol.Receipt{}, testcontrol.ErrControlFault
			}
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

func (o *Observer) bindBarrierConn(barrier *Barrier, conn net.Conn) bool {
	o.mu.Lock()
	result := false
	duplicate := false
	if barrier != nil && barrier.arrived && !barrier.canceled {
		duplicate = barrier.conn != nil && barrier.conn != conn
		switch {
		case barrier.conn == nil:
			barrier.conn = conn
			result = true
		case barrier.conn == conn:
			result = true
		case barrier.frozen:
			// After Prepare has frozen the held connection, a duplicate reach on
			// a different connection must fail closed rather than replace the target.
			o.latchFaultLocked()
			result = false
		default:
			// Before Prepare, a transport retransmission may share the barrier
			// result but never replaces the first connection.
			result = true
		}
	}
	hook := o.testBarrierBind
	o.mu.Unlock()
	if hook != nil {
		hook(barrier, conn, duplicate)
	}
	return result
}

func (o *Observer) setTestBarrierBindHook(hook func(*Barrier, net.Conn, bool)) {
	o.mu.Lock()
	o.testBarrierBind = hook
	o.mu.Unlock()
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

// ArmNextCommittedPoint arms exactly the next real Reach for one generation
// and one allowed committed point. It does not accept an order ID, event ID, or
// any caller-supplied identity; the first validated server frame binds them.
func (o *Observer) ArmNextCommittedPoint(generation uint64, point testcontrol.Point) (*Barrier, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.closing {
		return nil, testcontrol.ErrControlFault
	}
	if o.uncertain {
		return nil, testcontrol.ErrUncertain
	}
	if point != testcontrol.PointRegistered && point != testcontrol.PointMockCommitted {
		return nil, testcontrol.ErrProtocol
	}
	state, ok := o.generations[generation]
	if !ok || state.status == observerGenerationSealed || state.status == observerGenerationTruncated {
		return nil, testcontrol.ErrProtocol
	}
	nextKey := observerNextBarrierKey{generation: generation, point: point}
	if _, exists := o.nextBarriers[nextKey]; exists {
		return nil, testcontrol.ErrProtocol
	}
	for key := range o.barriers {
		if key.generation == generation && key.point == point {
			return nil, testcontrol.ErrProtocol
		}
	}
	barrier := &Barrier{
		observer:  o,
		gen:       generation,
		point:     point,
		arrivedCh: make(chan struct{}),
		releaseCh: make(chan struct{}),
		cancelCh:  make(chan struct{}),
	}
	o.nextBarriers[nextKey] = barrier
	return barrier, nil
}

// Binding returns the immutable actual binding captured for this barrier. It
// is false until a real server-validated reach has arrived and the barrier has
// not been canceled or timed out.
func (b *Barrier) Binding() (ControlledTerminationBinding, bool) {
	if b == nil || b.observer == nil {
		return ControlledTerminationBinding{}, false
	}
	o := b.observer
	o.mu.Lock()
	defer o.mu.Unlock()
	if !b.arrived || b.canceled || b.event == nil || b.orderID == "" ||
		b.seq == 0 || b.event.eventID == "" {
		return ControlledTerminationBinding{}, false
	}
	return ControlledTerminationBinding{
		RunID:       o.cfg.RunID,
		Epoch:       o.cfg.Epoch,
		Generation:  b.gen,
		Point:       b.point,
		OrderID:     b.orderID,
		Seq:         b.seq,
		EventID:     b.event.eventID,
		PrefixCount: int(b.seq),
	}, true
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
	if _, exists := o.nextBarriers[observerNextBarrierKey{generation: generation, point: point}]; exists {
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

func (o *Observer) Identity() (string, string) {
	if o == nil {
		return "", ""
	}
	return o.cfg.RunID, o.cfg.Epoch
}

// QueryApplication is one server-accepted v2 query call and its exactly-once
// QUERY_APPLIED proof. Directive is nil when the call had no scenario
// directive. Values are deep copies of validated frames, never caller input.
type QueryApplication struct {
	RunID          string
	Epoch          string
	Generation     uint64
	CallSeq        uint64
	CallEventID    string
	OrderID        string
	Directive      *testcontrol.QueryScenario
	Outcome        testcontrol.QueryOutcome
	AppliedSeq     uint64
	AppliedEventID string
}

// QueryApplicationSnapshot is the immutable measurement surface for one
// generation. Trusted is true only when that generation independently sealed
// and the observer is not uncertain.
type QueryApplicationSnapshot struct {
	Generation   uint64
	Applications []QueryApplication
	ApplyCount   int
	Trusted      bool
}

// QueryApplications returns the actual accepted query applications for one v2
// generation. It never accepts an expected value from the caller and never
// fabricates a proof for v1 or an unsealed/uncertain generation.
func (o *Observer) QueryApplications(generation uint64) (QueryApplicationSnapshot, bool) {
	if o == nil {
		return QueryApplicationSnapshot{}, false
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.version != testcontrol.Version2 {
		return QueryApplicationSnapshot{}, false
	}
	state, ok := o.generations[generation]
	if !ok {
		return QueryApplicationSnapshot{}, false
	}
	snapshot := QueryApplicationSnapshot{
		Generation: generation,
		Trusted:    state.status == observerGenerationSealed && !o.uncertain,
	}
	sequences := make([]uint64, 0, len(state.events))
	for seq := range state.events {
		sequences = append(sequences, seq)
	}
	sort.Slice(sequences, func(i, j int) bool { return sequences[i] < sequences[j] })
	for _, seq := range sequences {
		event := state.events[seq]
		if event.typ != testcontrol.TypeCall || event.kind != testcontrol.KindQuery {
			continue
		}
		application := QueryApplication{
			RunID:       o.cfg.RunID,
			Epoch:       o.cfg.Epoch,
			Generation:  generation,
			CallSeq:     event.seq,
			CallEventID: event.eventID,
			OrderID:     event.orderID,
		}
		if event.ack.QueryDirective != nil {
			directive := *event.ack.QueryDirective
			application.Directive = &directive
		}
		if event.queryApplied {
			application.Outcome = event.queryAppliedOutcome
			application.AppliedSeq = event.queryAppliedSeq
			application.AppliedEventID = event.queryAppliedEventID
			snapshot.ApplyCount++
		}
		snapshot.Applications = append(snapshot.Applications, application)
	}
	return snapshot, true
}

// SealedGeneration returns the immutable receipt and exact per-generation
// counts for one independently sealed generation. It never derives a
// generation from cumulative snapshot arithmetic.
func (o *Observer) SealedGeneration(generation uint64) (testcontrol.Receipt, Counts, bool) {
	if o == nil {
		return testcontrol.Receipt{}, Counts{}, false
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	state, ok := o.generations[generation]
	if !ok || state.status != observerGenerationSealed {
		return testcontrol.Receipt{}, Counts{}, false
	}
	if uint64(len(state.events)) != state.expectedLastSeq {
		return testcontrol.Receipt{}, Counts{}, false
	}
	for seq := uint64(1); seq <= state.expectedLastSeq; seq++ {
		if _, exists := state.events[seq]; !exists {
			return testcontrol.Receipt{}, Counts{}, false
		}
	}
	return observerReceipt(o.cfg, generation, state.expectedLastSeq), state.counts, true
}

func (o *Observer) FinalSnapshot() (ObserverFinalSnapshot, bool) {
	if o == nil {
		return ObserverFinalSnapshot{}, false
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if !o.finalReady {
		return ObserverFinalSnapshot{}, false
	}
	snapshot := o.finalSnapshot
	snapshot.Receipts = make([]testcontrol.Receipt, len(o.finalSnapshot.Receipts))
	copy(snapshot.Receipts, o.finalSnapshot.Receipts)
	return snapshot, true
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
	for _, barrier := range o.nextBarriers {
		if !barrier.released && !barrier.canceled {
			incomplete = true
			barrier.canceled = true
			close(barrier.cancelCh)
		}
	}
	for _, hold := range o.paymentHolds {
		if hold.status != paymentHoldConsumed {
			incomplete = true
		}
		hold.cancelLocked()
	}
	for generation, hold := range o.queryHolds {
		state := o.generations[generation]
		if hold.status != queryHoldConsumed && (state == nil || state.status != observerGenerationTruncated) {
			incomplete = true
		}
		hold.cancelLocked()
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
	o.mu.Lock()
	o.finalSnapshot = o.buildFinalSnapshotLocked()
	o.finalReady = true
	o.mu.Unlock()
	return nil
}

func (o *Observer) buildFinalSnapshotLocked() ObserverFinalSnapshot {
	trusted := !o.uncertain && len(o.generations) > 0
	receipts := make([]testcontrol.Receipt, 0, len(o.generations))
	for generation, state := range o.generations {
		if state.status != observerGenerationSealed {
			trusted = false
			continue
		}
		receipts = append(receipts, testcontrol.Receipt{
			RunID:      o.cfg.RunID,
			Epoch:      o.cfg.Epoch,
			Generation: generation,
			LastSeq:    state.expectedLastSeq,
		})
	}
	sort.Slice(receipts, func(i, j int) bool {
		return receipts[i].Generation < receipts[j].Generation
	})
	return ObserverFinalSnapshot{
		RunID:    o.cfg.RunID,
		Epoch:    o.cfg.Epoch,
		Counts:   o.counts,
		Receipts: receipts,
		Trusted:  trusted,
	}
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
	for _, barrier := range o.nextBarriers {
		if !barrier.released && !barrier.canceled {
			barrier.canceled = true
			close(barrier.cancelCh)
		}
	}
	for _, hold := range o.paymentHolds {
		hold.cancelLocked()
	}
	for _, hold := range o.queryHolds {
		hold.cancelLocked()
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
	frame := testcontrol.Frame{
		Version:    req.Version,
		Type:       testcontrol.TypeAck,
		RunID:      req.RunID,
		Epoch:      req.Epoch,
		Generation: req.Generation,
		EventID:    req.EventID,
		For:        testcontrol.TypeBegin,
	}
	if req.Version == testcontrol.Version2 {
		frame.Capability = testcontrol.CapabilityQueryControl
	}
	return frame
}

func observerAckCall(req testcontrol.Frame) testcontrol.Frame {
	return testcontrol.Frame{
		Version:    req.Version,
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
		Version:    req.Version,
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
		Version:    req.Version,
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
