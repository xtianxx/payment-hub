package testcontrol

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"syscall"
	"time"
)

const clientOperationTimeout = 10 * time.Second

var (
	errClientKnown     = errors.New("testcontrol: known client response failure")
	errClientTransport = errors.New("testcontrol: client transport failure")
)

// Client is one producer generation attached to a private observer socket.
// A Client allocates all Enter/Reach sequences locally and closes a trusted
// static boundary with Seal using that local sequence count.
type Client struct {
	session Session
	beginID string
	valid   bool

	mu           sync.Mutex
	nextSeq      uint64
	inflight     int
	drainWaiters []chan struct{}
	sealing      bool
	sealed       bool
	sealedResult Receipt
	fault        bool
	closed       bool
	conns        map[net.Conn]struct{}
}

type paymentCallState struct {
	client    *Client
	seq       uint64
	eventID   string
	orderID   string
	directive *Directive
	committed bool
}

// PaymentCall is the private, single-use association between one payment entry
// call and its later MockCommitted proof. Its state is owned by the issuing
// Client; the zero value is not usable.
type PaymentCall struct {
	state *paymentCallState
}

// Directive returns the selected result directive as a value copy.
func (p PaymentCall) Directive() (Directive, bool) {
	if p.state == nil || p.state.directive == nil {
		return "", false
	}
	return *p.state.directive, true
}

// PaymentEntry records one payment call and returns a token carrying the
// observer-selected result directive, if any.
func (c *Client) PaymentEntry(ctx context.Context, orderID string) (PaymentCall, error) {
	op, err := c.startOperation(ctx, func() bool {
		return validUUIDv4(orderID)
	})
	if err != nil {
		return PaymentCall{}, err
	}
	failed := true
	defer func() {
		c.finishOperation(failed)
	}()

	opCtx, cancel, ok := newClientOperationContext(ctx)
	if !ok {
		return PaymentCall{}, ErrControlFault
	}
	defer cancel()

	frame := Frame{
		Version:    Version,
		Type:       TypeCall,
		RunID:      c.session.RunID,
		Epoch:      c.session.Epoch,
		Generation: c.session.Generation,
		Seq:        op.seq,
		EventID:    op.eventID,
		Kind:       KindPayment,
		OrderID:    orderID,
	}
	ack, err := exchangeFrame(opCtx, c.session.Socket, frame, validateClientAck, c.trackConn, c.untrackConn)
	if err != nil {
		return PaymentCall{}, ErrControlFault
	}

	var directive *Directive
	if ack.Directive != nil {
		value := *ack.Directive
		directive = &value
	}
	failed = false
	return PaymentCall{state: &paymentCallState{
		client:    c,
		seq:       op.seq,
		eventID:   op.eventID,
		orderID:   orderID,
		directive: directive,
	}}, nil
}

// MockCommitted records the mock commit proof for one PaymentEntry token.
func (c *Client) MockCommitted(ctx context.Context, token PaymentCall, actual Directive) error {
	op, err := c.startOperation(ctx, func() bool {
		if !validDirective(actual) {
			return false
		}
		state := token.state
		if state == nil || state.client != c || state.committed {
			return false
		}
		if state.seq < 1 || state.seq > MaxRecords || !validUUIDv4(state.orderID) {
			return false
		}
		state.committed = true
		return true
	})
	if err != nil {
		return err
	}
	failed := true
	defer func() {
		c.finishOperation(failed)
	}()

	opCtx, cancel, ok := newClientOperationContext(ctx)
	if !ok {
		return ErrControlFault
	}
	defer cancel()

	frame := Frame{
		Version:    Version,
		Type:       TypeReach,
		RunID:      c.session.RunID,
		Epoch:      c.session.Epoch,
		Generation: c.session.Generation,
		Seq:        op.seq,
		EventID:    op.eventID,
		Point:      PointMockCommitted,
		OrderID:    token.state.orderID,
	}
	if token.state.directive != nil {
		callSeq := token.state.seq
		applied := actual
		frame.PaymentCallSeq = &callSeq
		frame.AppliedResult = &applied
	}
	ack, err := exchangeFrame(opCtx, c.session.Socket, frame, validateClientAck, c.trackConn, c.untrackConn)
	if err != nil {
		return ErrControlFault
	}
	if ack.Action == ActionReplyHidden {
		failed = false
		return ErrReplyHidden
	}
	failed = false
	return nil
}

// Dial creates one Client generation. The context bounds startup and Begin only;
// it is not retained as the Client lifetime. Unsafe socket paths are rejected
// before any dial.
func Dial(ctx context.Context, session Session) (*Client, error) {
	if ctx == nil || ctx.Err() != nil {
		return nil, ErrControlFault
	}
	if session.Socket == "" || !validLowerHex32(session.RunID) ||
		!validLowerHex32(session.Epoch) || session.Generation == 0 {
		return nil, ErrControlFault
	}

	beginID, err := newClientBeginID()
	if err != nil {
		return nil, ErrControlFault
	}
	begin := Frame{
		Version:    Version,
		Type:       TypeBegin,
		RunID:      session.RunID,
		Epoch:      session.Epoch,
		Generation: session.Generation,
		EventID:    beginID,
	}

	opCtx, cancel, ok := newClientOperationContext(ctx)
	if !ok {
		return nil, ErrControlFault
	}
	defer cancel()
	if _, err := exchangeFrame(opCtx, session.Socket, begin, validateClientAck, nil, nil); err != nil {
		return nil, ErrControlFault
	}

	return &Client{
		session: session,
		beginID: beginID,
		valid:   true,
		conns:   make(map[net.Conn]struct{}),
	}, nil
}

// Enter records one payment/query call after allocating a fresh local sequence.
// A confirmed postcommit reply_hidden is returned as ErrReplyHidden without
// poisoning the window; every other failure is fixed and latches ControlFault.
func (c *Client) Enter(ctx context.Context, kind Kind, orderID string) error {
	return c.call(ctx, TypeCall, kind, "", orderID)
}

// Reach records one control-window point after allocating a fresh local
// sequence. Only postcommit points may carry a confirmed reply_hidden.
func (c *Client) Reach(ctx context.Context, point Point, orderID string) error {
	return c.call(ctx, TypeReach, "", point, orderID)
}

func (c *Client) call(ctx context.Context, typ Type, kind Kind, point Point, orderID string) error {
	op, err := c.startOperation(ctx, func() bool {
		switch typ {
		case TypeCall:
			return validKind(kind) && validUUIDv4(orderID)
		case TypeReach:
			return validPoint(point) && validUUIDv4(orderID)
		default:
			return false
		}
	})
	if err != nil {
		return err
	}

	failed := true
	defer func() {
		c.finishOperation(failed)
	}()

	opCtx, cancel, ok := newClientOperationContext(ctx)
	if !ok {
		return ErrControlFault
	}
	defer cancel()

	frame := Frame{
		Version:    Version,
		Type:       typ,
		RunID:      c.session.RunID,
		Epoch:      c.session.Epoch,
		Generation: c.session.Generation,
		Seq:        op.seq,
		EventID:    op.eventID,
	}
	if typ == TypeCall {
		frame.Kind = kind
		frame.OrderID = orderID
	} else {
		frame.Point = point
		frame.OrderID = orderID
	}

	ack, err := exchangeFrame(opCtx, c.session.Socket, frame, validateClientAck, c.trackConn, c.untrackConn)
	if err != nil {
		return ErrControlFault
	}
	if typ == TypeCall && kind == KindPayment && ack.Directive != nil {
		// The legacy Enter API cannot deliver a selected result directive to
		// its caller, so accepting it would silently drop a control decision.
		return ErrControlFault
	}
	if typ == TypeReach && ack.Action == ActionReplyHidden {
		failed = false
		return ErrReplyHidden
	}
	failed = false
	return nil
}

type clientOperation struct {
	seq     uint64
	eventID string
}

func (c *Client) startOperation(ctx context.Context, validate func() bool) (clientOperation, error) {
	if c == nil || !c.valid {
		return clientOperation{}, ErrControlFault
	}
	if ctx == nil || ctx.Err() != nil {
		c.latchFault()
		return clientOperation{}, ErrControlFault
	}

	c.mu.Lock()
	if c.closed || c.fault {
		c.mu.Unlock()
		return clientOperation{}, ErrControlFault
	}
	if c.sealing || c.sealed {
		// New work after Seal started breaks the trusted static boundary even
		// when the caller ignores the returned error.
		c.fault = true
		c.mu.Unlock()
		return clientOperation{}, ErrControlFault
	}
	if validate == nil || !validate() {
		c.fault = true
		c.mu.Unlock()
		return clientOperation{}, ErrControlFault
	}
	if c.nextSeq >= MaxRecords {
		c.fault = true
		c.mu.Unlock()
		return clientOperation{}, ErrControlFault
	}
	c.nextSeq++
	op := clientOperation{
		seq:     c.nextSeq,
		eventID: c.beginID + ":" + strconv.FormatUint(c.nextSeq, 10),
	}
	c.inflight++
	c.mu.Unlock()
	return op, nil
}

// Seal stops new work, drains already allocated operations, and closes the
// observer window with the local sequence count. A successful Seal is
// idempotent; any post-start Enter/Reach violation latches a failure that even
// a valid in-flight acknowledgement cannot recover.
func (c *Client) Seal(ctx context.Context) (Receipt, error) {
	if c == nil || !c.valid {
		return Receipt{}, ErrControlFault
	}
	if ctx == nil || ctx.Err() != nil {
		c.latchFault()
		return Receipt{}, ErrControlFault
	}

	c.mu.Lock()
	if c.closed || c.fault {
		c.mu.Unlock()
		return Receipt{}, ErrControlFault
	}
	if c.sealing {
		c.fault = true
		c.mu.Unlock()
		return Receipt{}, ErrControlFault
	}
	if c.sealed {
		receipt := c.sealedResult
		c.mu.Unlock()
		return receipt, nil
	}

	c.sealing = true
	expected := c.nextSeq
	var drained chan struct{}
	if c.inflight > 0 {
		drained = make(chan struct{})
		c.drainWaiters = append(c.drainWaiters, drained)
	}
	c.mu.Unlock()

	opCtx, cancel, ok := newClientOperationContext(ctx)
	if !ok {
		c.failSeal()
		return Receipt{}, ErrControlFault
	}
	defer cancel()

	if drained != nil {
		select {
		case <-drained:
		case <-opCtx.Done():
			c.failSeal()
			return Receipt{}, ErrControlFault
		}
	}
	if opCtx.Err() != nil {
		c.failSeal()
		return Receipt{}, ErrControlFault
	}

	c.mu.Lock()
	if c.closed || c.fault {
		c.mu.Unlock()
		c.failSeal()
		return Receipt{}, ErrControlFault
	}
	c.mu.Unlock()

	seal := Frame{
		Version:         Version,
		Type:            TypeSeal,
		RunID:           c.session.RunID,
		Epoch:           c.session.Epoch,
		Generation:      c.session.Generation,
		EventID:         c.beginID + ":seal",
		ExpectedLastSeq: &expected,
	}
	ack, err := exchangeFrame(opCtx, c.session.Socket, seal, validateClientAck, c.trackConn, c.untrackConn)
	if err != nil || opCtx.Err() != nil {
		c.failSeal()
		return Receipt{}, ErrControlFault
	}
	if ack.ExpectedLastSeq == nil || *ack.ExpectedLastSeq != expected {
		c.failSeal()
		return Receipt{}, ErrControlFault
	}
	receipt := Receipt{
		RunID:      c.session.RunID,
		Epoch:      c.session.Epoch,
		Generation: c.session.Generation,
		LastSeq:    expected,
	}

	c.mu.Lock()
	if c.closed || c.fault {
		c.mu.Unlock()
		c.failSeal()
		return Receipt{}, ErrControlFault
	}
	c.sealing = false
	c.sealed = true
	c.sealedResult = receipt
	c.mu.Unlock()
	return receipt, nil
}

// Close releases owned connections without sealing. Nil, zero-value, and
// repeated Close calls are safe.
func (c *Client) Close() error {
	if c == nil {
		return nil
	}
	c.mu.Lock()
	if c.closed {
		c.mu.Unlock()
		return nil
	}
	c.closed = true
	for _, waiter := range c.drainWaiters {
		close(waiter)
	}
	c.drainWaiters = nil
	connections := make([]net.Conn, 0, len(c.conns))
	for conn := range c.conns {
		connections = append(connections, conn)
	}
	c.conns = nil
	c.mu.Unlock()

	for _, conn := range connections {
		_ = conn.Close()
	}
	return nil
}

func (c *Client) latchFault() {
	c.mu.Lock()
	c.fault = true
	c.mu.Unlock()
}

func (c *Client) failSeal() {
	c.mu.Lock()
	c.fault = true
	c.sealing = false
	c.mu.Unlock()
}

func (c *Client) finishOperation(failed bool) {
	c.mu.Lock()
	if failed {
		c.fault = true
	}
	if c.inflight > 0 {
		c.inflight--
	}
	if c.inflight == 0 {
		for _, waiter := range c.drainWaiters {
			close(waiter)
		}
		c.drainWaiters = nil
	}
	c.mu.Unlock()
}

func (c *Client) trackConn(conn net.Conn) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.valid || c.closed {
		return false
	}
	if c.conns == nil {
		c.conns = make(map[net.Conn]struct{})
	}
	c.conns[conn] = struct{}{}
	return true
}

func (c *Client) untrackConn(conn net.Conn) {
	c.mu.Lock()
	delete(c.conns, conn)
	c.mu.Unlock()
}

func newClientBeginID() (string, error) {
	var random [16]byte
	if _, err := rand.Read(random[:]); err != nil {
		return "", err
	}
	return hex.EncodeToString(random[:]), nil
}

func newClientOperationContext(ctx context.Context) (context.Context, context.CancelFunc, bool) {
	if ctx == nil || ctx.Err() != nil {
		return nil, nil, false
	}
	deadline := time.Now().Add(clientOperationTimeout)
	if callerDeadline, ok := ctx.Deadline(); ok && callerDeadline.Before(deadline) {
		deadline = callerDeadline
	}
	opCtx, cancel := context.WithDeadline(ctx, deadline)
	return opCtx, cancel, true
}

func exchangeFrame(
	ctx context.Context,
	socket string,
	frame Frame,
	validate func(Frame, Frame) error,
	track func(net.Conn) bool,
	untrack func(net.Conn),
) (Frame, error) {
	payload, err := EncodeFrame(frame)
	if err != nil {
		return Frame{}, errClientKnown
	}
	deadline, ok := ctx.Deadline()
	if !ok {
		deadline = time.Now().Add(clientOperationTimeout)
	}

	for attempt := 0; attempt < 2; attempt++ {
		if ctx.Err() != nil {
			return Frame{}, errClientTransport
		}
		if err := validateClientSocket(socket); err != nil {
			return Frame{}, errClientKnown
		}

		var dialer net.Dialer
		conn, err := dialer.DialContext(ctx, "unix", socket)
		if err != nil {
			if ctx.Err() != nil || !time.Now().Before(deadline) {
				return Frame{}, errClientTransport
			}
			continue
		}
		if track != nil && !track(conn) {
			_ = conn.Close()
			return Frame{}, errClientTransport
		}

		ack, err := func() (Frame, error) {
			if untrack != nil {
				defer untrack(conn)
			}
			defer func() {
				_ = conn.Close()
			}()
			callbackDone := make(chan struct{})
			stop := context.AfterFunc(ctx, func() {
				defer close(callbackDone)
				_ = conn.Close()
			})
			defer func() {
				// AfterFunc's stop does not wait for an already-started callback.
				if !stop() {
					<-callbackDone
				}
			}()

			if err := conn.SetDeadline(deadline); err != nil {
				return Frame{}, errClientTransport
			}
			if err := writeClientFrame(conn, payload); err != nil {
				return Frame{}, errClientTransport
			}
			if err := closeClientWrite(conn); err != nil {
				return Frame{}, errClientTransport
			}
			raw, err := readClientResponse(conn)
			if err != nil {
				if len(raw) > 0 {
					return Frame{}, errClientKnown
				}
				return Frame{}, errClientTransport
			}
			if len(raw) == 0 {
				return Frame{}, errClientTransport
			}
			if len(raw) > MaxFrameBytes {
				return Frame{}, errClientKnown
			}
			response, err := DecodeFrame(raw)
			if err != nil {
				return Frame{}, errClientKnown
			}
			if err := validate(frame, response); err != nil {
				return Frame{}, errClientKnown
			}
			return response, nil
		}()
		if err == nil {
			return ack, nil
		}
		if err == errClientKnown {
			return Frame{}, errClientKnown
		}
		if ctx.Err() != nil || !time.Now().Before(deadline) {
			return Frame{}, errClientTransport
		}
	}
	return Frame{}, errClientTransport
}

func validateClientAck(request, ack Frame) error {
	if ack.Type != TypeAck ||
		ack.RunID != request.RunID ||
		ack.Epoch != request.Epoch ||
		ack.Generation != request.Generation ||
		ack.EventID != request.EventID ||
		ack.For != request.Type {
		return errClientKnown
	}
	switch request.Type {
	case TypeBegin:
	case TypeCall:
		if ack.Seq != request.Seq {
			return errClientKnown
		}
		if ack.Directive != nil {
			if request.Kind != KindPayment || !validDirective(*ack.Directive) {
				return errClientKnown
			}
		}
	case TypeReach:
		if ack.Seq != request.Seq || !validAction(ack.Action) {
			return errClientKnown
		}
		if ack.Action == ActionReplyHidden && !validPostcommitPoint(request.Point) {
			return errClientKnown
		}
	case TypeSeal:
		if request.ExpectedLastSeq == nil || ack.ExpectedLastSeq == nil ||
			*ack.ExpectedLastSeq != *request.ExpectedLastSeq {
			return errClientKnown
		}
	default:
		return errClientKnown
	}
	return nil
}

func validPostcommitPoint(point Point) bool {
	switch point {
	case PointRegistered, PointMockCommitted, PointResultCommitted:
		return true
	default:
		return false
	}
}

func validateClientSocket(socket string) error {
	if socket == "" || !filepath.IsAbs(socket) {
		return ErrControlFault
	}
	parent := filepath.Dir(socket)
	parentInfo, err := os.Lstat(parent)
	if err != nil || parentInfo.Mode()&os.ModeSymlink != 0 || !parentInfo.IsDir() ||
		parentInfo.Mode().Perm() != 0o700 || !ownedByCurrentUser(parentInfo) {
		return ErrControlFault
	}
	socketInfo, err := os.Lstat(socket)
	if err != nil || socketInfo.Mode()&os.ModeSymlink != 0 ||
		socketInfo.Mode()&os.ModeSocket == 0 || socketInfo.Mode().Perm() != 0o600 ||
		!ownedByCurrentUser(socketInfo) {
		return ErrControlFault
	}
	return nil
}

func ownedByCurrentUser(info os.FileInfo) bool {
	stat, ok := info.Sys().(*syscall.Stat_t)
	return ok && stat.Uid == uint32(os.Getuid())
}

func writeClientFrame(conn net.Conn, payload []byte) error {
	for len(payload) > 0 {
		written, err := conn.Write(payload)
		if err != nil || written <= 0 {
			return errClientTransport
		}
		payload = payload[written:]
	}
	return nil
}

func closeClientWrite(conn net.Conn) error {
	unixConn, ok := conn.(*net.UnixConn)
	if !ok {
		return errClientTransport
	}
	if err := unixConn.CloseWrite(); err != nil {
		return errClientTransport
	}
	return nil
}

func readClientResponse(conn net.Conn) ([]byte, error) {
	limit := int64(MaxFrameBytes) + 1
	reader := &io.LimitedReader{R: conn, N: limit}
	buf := make([]byte, 0, 256)
	tmp := make([]byte, 512)
	for {
		n, err := reader.Read(tmp)
		if n > 0 {
			buf = append(buf, tmp[:n]...)
		}
		if err != nil {
			if errors.Is(err, io.EOF) {
				return buf, nil
			}
			return buf, err
		}
		if int64(len(buf)) >= limit {
			return buf, nil
		}
	}
}
