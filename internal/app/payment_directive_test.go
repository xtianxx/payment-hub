package app

import (
	"context"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"payment-hub/internal/mock"
	"payment-hub/internal/payment"
	"payment-hub/internal/testcontrol"
)

// TestAppPaymentDirective freezes the app test-mode adapter for per-Pay result
// directives. It uses a private UDS peer that speaks the real testcontrol
// protocol and captures only fixed proof fields, never raw frames. It never
// opens PostgreSQL, business HTTP, the launcher, or the external network.

const (
	directiveTestRunID  = "0123456789abcdef0123456789abcdef"
	directiveTestEpoch  = "fedcba9876543210fedcba9876543210"
	directiveTestOrderA = "123e4567-e89b-42d3-a456-426614174000"
	directiveTestOrderB = "223e4567-e89b-42d3-b456-426614174001"
	directiveTestBudget = 5 * time.Second
)

type directivePeerCommit struct {
	orderID  string
	hasProof bool
	callSeq  uint64
	applied  testcontrol.Directive
	action   testcontrol.Action
}

type directivePeer struct {
	runID      string
	epoch      string
	generation uint64
	socketPath string
	dir        string
	listener   *net.UnixListener

	mu             sync.Mutex
	closing        bool
	conns          map[net.Conn]struct{}
	directives     map[string]testcontrol.Directive
	hidden         map[string]bool
	paymentEntries int
	mockCommitted  int
	paymentCalls   map[string]uint64
	commits        []directivePeerCommit

	closeOnce sync.Once
	wg        sync.WaitGroup
}

func newDirectivePeer(t *testing.T, directives map[string]testcontrol.Directive, hidden map[string]bool) *directivePeer {
	t.Helper()
	dir, err := os.MkdirTemp("", "phtm-directive-")
	if err != nil {
		t.Fatal("create directive peer directory")
	}
	if err := os.Chmod(dir, 0o700); err != nil {
		_ = os.RemoveAll(dir)
		t.Fatal("secure directive peer directory")
	}
	socketPath := filepath.Join(dir, "s")
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: socketPath, Net: "unix"})
	if err != nil {
		_ = os.RemoveAll(dir)
		t.Fatal("listen directive peer socket")
	}
	if err := os.Chmod(socketPath, 0o600); err != nil {
		_ = listener.Close()
		_ = os.RemoveAll(dir)
		t.Fatal("secure directive peer socket")
	}

	peer := &directivePeer{
		runID:        directiveTestRunID,
		epoch:        directiveTestEpoch,
		generation:   1,
		socketPath:   socketPath,
		dir:          dir,
		listener:     listener,
		conns:        make(map[net.Conn]struct{}),
		directives:   directives,
		hidden:       hidden,
		paymentCalls: make(map[string]uint64),
	}
	peer.wg.Add(1)
	go peer.acceptLoop()
	t.Cleanup(func() { peer.close() })
	return peer
}

func (peer *directivePeer) close() {
	peer.closeOnce.Do(func() {
		peer.mu.Lock()
		peer.closing = true
		connections := make([]net.Conn, 0, len(peer.conns))
		for conn := range peer.conns {
			connections = append(connections, conn)
		}
		peer.mu.Unlock()

		_ = peer.listener.Close()
		for _, conn := range connections {
			_ = conn.Close()
		}
		peer.wg.Wait()
		_ = os.Remove(peer.socketPath)
		_ = os.RemoveAll(peer.dir)
	})
}

func (peer *directivePeer) acceptLoop() {
	defer peer.wg.Done()
	for {
		conn, err := peer.listener.AcceptUnix()
		if err != nil {
			return
		}
		peer.mu.Lock()
		if peer.closing {
			peer.mu.Unlock()
			_ = conn.Close()
			return
		}
		peer.conns[conn] = struct{}{}
		peer.wg.Add(1)
		peer.mu.Unlock()

		go func() {
			defer peer.wg.Done()
			defer func() {
				peer.mu.Lock()
				delete(peer.conns, conn)
				peer.mu.Unlock()
				_ = conn.Close()
			}()
			peer.handle(conn)
		}()
	}
}

func (peer *directivePeer) handle(conn *net.UnixConn) {
	_ = conn.SetReadDeadline(time.Now().Add(directiveTestBudget))
	payload, err := readDirectivePeerFrame(conn)
	if err != nil {
		return
	}
	frame, err := testcontrol.DecodeFrame(payload)
	if err != nil {
		return
	}

	peer.mu.Lock()
	ack, ok := peer.processLocked(frame)
	peer.mu.Unlock()
	if !ok {
		return
	}
	encoded, err := testcontrol.EncodeFrame(ack)
	if err != nil {
		return
	}
	_ = conn.SetWriteDeadline(time.Now().Add(directiveTestBudget))
	_ = writeDirectivePeerFrame(conn, encoded)
}

func (peer *directivePeer) processLocked(frame testcontrol.Frame) (testcontrol.Frame, bool) {
	if frame.RunID != peer.runID || frame.Epoch != peer.epoch || frame.Generation != peer.generation {
		return directivePeerFault(), true
	}
	switch frame.Type {
	case testcontrol.TypeBegin:
		return directivePeerAck(frame, testcontrol.TypeBegin), true
	case testcontrol.TypeCall:
		if frame.Kind == testcontrol.KindPayment {
			peer.paymentEntries++
			peer.paymentCalls[frame.OrderID] = frame.Seq
		}
		ack := directivePeerAck(frame, testcontrol.TypeCall)
		if directive, ok := peer.directives[frame.OrderID]; ok {
			value := directive
			ack.Directive = &value
		}
		return ack, true
	case testcontrol.TypeReach:
		if frame.Point == testcontrol.PointMockCommitted {
			commit := directivePeerCommit{orderID: frame.OrderID}
			if frame.PaymentCallSeq != nil && frame.AppliedResult != nil {
				commit.hasProof = true
				commit.callSeq = *frame.PaymentCallSeq
				commit.applied = *frame.AppliedResult
			}
			action := testcontrol.ActionContinue
			if peer.hidden[frame.OrderID] {
				action = testcontrol.ActionReplyHidden
			}
			commit.action = action
			peer.commits = append(peer.commits, commit)
			peer.mockCommitted++
			ack := directivePeerAck(frame, testcontrol.TypeReach)
			ack.Action = action
			return ack, true
		}
		ack := directivePeerAck(frame, testcontrol.TypeReach)
		ack.Action = testcontrol.ActionContinue
		return ack, true
	case testcontrol.TypeSeal:
		ack := directivePeerAck(frame, testcontrol.TypeSeal)
		if frame.ExpectedLastSeq != nil {
			value := *frame.ExpectedLastSeq
			ack.ExpectedLastSeq = &value
		}
		return ack, true
	default:
		return directivePeerFault(), true
	}
}

func (peer *directivePeer) snapshotCommits() []directivePeerCommit {
	peer.mu.Lock()
	defer peer.mu.Unlock()
	return append([]directivePeerCommit(nil), peer.commits...)
}

func (peer *directivePeer) paymentCallsSnapshot() map[string]uint64 {
	peer.mu.Lock()
	defer peer.mu.Unlock()
	calls := make(map[string]uint64, len(peer.paymentCalls))
	for order, seq := range peer.paymentCalls {
		calls[order] = seq
	}
	return calls
}

func (peer *directivePeer) counts() (paymentEntries, mockCommitted int) {
	peer.mu.Lock()
	defer peer.mu.Unlock()
	return peer.paymentEntries, peer.mockCommitted
}

func directivePeerAck(request testcontrol.Frame, forType testcontrol.Type) testcontrol.Frame {
	return testcontrol.Frame{
		Version:    testcontrol.Version,
		Type:       testcontrol.TypeAck,
		RunID:      request.RunID,
		Epoch:      request.Epoch,
		Generation: request.Generation,
		EventID:    request.EventID,
		Seq:        request.Seq,
		For:        forType,
	}
}

func directivePeerFault() testcontrol.Frame {
	return testcontrol.Frame{
		Version: testcontrol.Version,
		Type:    testcontrol.TypeFault,
		Code:    testcontrol.FaultProtocol,
	}
}

func readDirectivePeerFrame(conn net.Conn) ([]byte, error) {
	limit := int64(testcontrol.MaxFrameBytes) + 1
	reader := &io.LimitedReader{R: conn, N: limit}
	data, err := io.ReadAll(reader)
	if err != nil {
		return nil, err
	}
	if len(data) == 0 || len(data) > testcontrol.MaxFrameBytes {
		return nil, errors.New("directive peer: invalid frame size")
	}
	return data, nil
}

func writeDirectivePeerFrame(conn net.Conn, payload []byte) error {
	for len(payload) > 0 {
		written, err := conn.Write(payload)
		if err != nil || written <= 0 {
			return errors.New("directive peer: write failed")
		}
		payload = payload[written:]
	}
	return nil
}

func openDirectiveTestMode(t *testing.T, peer *directivePeer) *TestMode {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), directiveTestBudget)
	defer cancel()
	tm, err := OpenTestMode(ctx, TestModeConfig{Enabled: true, Session: testcontrol.Session{
		Socket:     peer.socketPath,
		RunID:      peer.runID,
		Epoch:      peer.epoch,
		Generation: peer.generation,
	}})
	if err != nil {
		t.Fatal("open directive test mode failed")
	}
	if tm == nil {
		t.Fatal("open directive test mode returned nil")
	}
	t.Cleanup(func() { directiveTestCloseOK(t, tm) })
	return tm
}

func directiveTestCloseOK(t *testing.T, tm *TestMode) {
	t.Helper()
	if tm == nil {
		return
	}
	if err := tm.Close(); err != nil {
		t.Error("close directive test mode failed")
	}
}

func directiveTestContext(t *testing.T) (context.Context, context.CancelFunc) {
	t.Helper()
	return context.WithTimeout(context.Background(), directiveTestBudget)
}

func TestAppPaymentDirective(t *testing.T) {
	t.Run("failed directive is copied and actual is sent", func(t *testing.T) {
		peer := newDirectivePeer(t, map[string]testcontrol.Directive{
			directiveTestOrderA: testcontrol.DirectiveFailed,
		}, nil)
		tm := openDirectiveTestMode(t, peer)
		hooks := tm.MockHooks()
		if hooks == nil {
			t.Fatal("enabled test mode returned nil mock hooks")
		}
		ctx, cancel := directiveTestContext(t)
		defer cancel()

		outcome, err := hooks.PaymentEntryResult(ctx, payment.PaymentOrderID(directiveTestOrderA))
		if err != nil {
			t.Fatalf("directive entry failed (%s)", mockErrorClass(err))
		}
		if outcome.Result == nil {
			t.Fatal("expected failed got nil")
		}
		if *outcome.Result != payment.ResultFailed {
			t.Fatalf("expected failed got %s", *outcome.Result)
		}
		if outcome.OnCommitted == nil {
			t.Fatal("directive callback is nil")
		}
		if err := outcome.OnCommitted(ctx, payment.ResultSucceeded); err != nil {
			t.Fatalf("directive callback failed (%s)", mockErrorClass(err))
		}

		commits := peer.snapshotCommits()
		if len(commits) != 1 {
			t.Fatalf("expected one committed proof, got %d", len(commits))
		}
		commit := commits[0]
		if commit.orderID != directiveTestOrderA {
			t.Fatal("committed proof used the wrong order")
		}
		if !commit.hasProof {
			t.Fatal("directive commit lacked proof")
		}
		if commit.applied != testcontrol.DirectiveSucceeded {
			t.Fatalf("expected actual succeeded got %s", commit.applied)
		}
		calls := peer.paymentCallsSnapshot()
		if commit.callSeq != calls[directiveTestOrderA] {
			t.Fatal("committed proof used a different call sequence")
		}
		entries, committed := peer.counts()
		if entries != 1 || committed != 1 {
			t.Fatalf("unexpected peer counts: entry=%d committed=%d", entries, committed)
		}
	})

	t.Run("concurrent outcomes and tokens do not cross", func(t *testing.T) {
		peer := newDirectivePeer(t, map[string]testcontrol.Directive{
			directiveTestOrderA: testcontrol.DirectiveFailed,
			directiveTestOrderB: testcontrol.DirectiveSucceeded,
		}, nil)
		tm := openDirectiveTestMode(t, peer)
		hooks := tm.MockHooks()
		if hooks == nil {
			t.Fatal("enabled test mode returned nil mock hooks")
		}
		ctx, cancel := directiveTestContext(t)
		defer cancel()

		type entryResult struct {
			order   string
			outcome mock.PaymentEntryOutcome
			err     error
		}
		entryResults := make(chan entryResult, 2)
		var entryWG sync.WaitGroup
		for _, order := range []string{directiveTestOrderA, directiveTestOrderB} {
			entryWG.Add(1)
			go func(order string) {
				defer entryWG.Done()
				outcome, err := hooks.PaymentEntryResult(ctx, payment.PaymentOrderID(order))
				entryResults <- entryResult{order: order, outcome: outcome, err: err}
			}(order)
		}
		entryWG.Wait()
		close(entryResults)

		outcomes := make(map[string]mock.PaymentEntryOutcome, 2)
		expected := map[string]payment.Result{
			directiveTestOrderA: payment.ResultFailed,
			directiveTestOrderB: payment.ResultSucceeded,
		}
		for result := range entryResults {
			if result.err != nil {
				t.Fatalf("concurrent entry failed (%s)", mockErrorClass(result.err))
			}
			if result.outcome.Result == nil || *result.outcome.Result != expected[result.order] {
				t.Fatalf("concurrent directive crossed for %s", result.order)
			}
			outcomes[result.order] = result.outcome
		}

		commitErrs := make(chan error, 2)
		var commitWG sync.WaitGroup
		for order, outcome := range outcomes {
			commitWG.Add(1)
			go func(order string, outcome mock.PaymentEntryOutcome) {
				defer commitWG.Done()
				commitErrs <- outcome.OnCommitted(ctx, expected[order])
			}(order, outcome)
		}
		commitWG.Wait()
		close(commitErrs)
		for err := range commitErrs {
			if err != nil {
				t.Fatalf("concurrent committed callback failed (%s)", mockErrorClass(err))
			}
		}

		commits := peer.snapshotCommits()
		if len(commits) != 2 {
			t.Fatalf("expected two committed proofs, got %d", len(commits))
		}
		calls := peer.paymentCallsSnapshot()
		applied := map[string]testcontrol.Directive{
			directiveTestOrderA: testcontrol.DirectiveFailed,
			directiveTestOrderB: testcontrol.DirectiveSucceeded,
		}
		for _, commit := range commits {
			if !commit.hasProof {
				t.Fatalf("concurrent proof lacked proof for %s", commit.orderID)
			}
			if commit.callSeq != calls[commit.orderID] {
				t.Fatalf("concurrent proof crossed call sequence for %s", commit.orderID)
			}
			if commit.applied != applied[commit.orderID] {
				t.Fatalf("concurrent proof crossed actual result for %s", commit.orderID)
			}
		}
		entries, committed := peer.counts()
		if entries != 2 || committed != 2 {
			t.Fatalf("unexpected concurrent peer counts: entry=%d committed=%d", entries, committed)
		}
	})

	t.Run("legacy no directive sends bare commit", func(t *testing.T) {
		peer := newDirectivePeer(t, nil, nil)
		tm := openDirectiveTestMode(t, peer)
		hooks := tm.MockHooks()
		if hooks == nil {
			t.Fatal("enabled test mode returned nil mock hooks")
		}
		ctx, cancel := directiveTestContext(t)
		defer cancel()

		outcome, err := hooks.PaymentEntryResult(ctx, payment.PaymentOrderID(directiveTestOrderA))
		if err != nil {
			t.Fatalf("legacy entry failed (%s)", mockErrorClass(err))
		}
		if outcome.Result != nil {
			t.Fatal("legacy entry unexpectedly returned a directive")
		}
		if outcome.OnCommitted == nil {
			t.Fatal("legacy callback is nil")
		}
		if err := outcome.OnCommitted(ctx, payment.ResultFailed); err != nil {
			t.Fatalf("legacy callback failed (%s)", mockErrorClass(err))
		}

		commits := peer.snapshotCommits()
		if len(commits) != 1 {
			t.Fatalf("expected one legacy commit, got %d", len(commits))
		}
		commit := commits[0]
		if commit.hasProof {
			t.Fatal("legacy commit unexpectedly carried proof")
		}
		if commit.callSeq != 0 || commit.applied != "" {
			t.Fatal("legacy commit carried a fixed proof value")
		}
		if commit.action != testcontrol.ActionContinue {
			t.Fatal("legacy commit returned an unexpected action")
		}
		entries, committed := peer.counts()
		if entries != 1 || committed != 1 {
			t.Fatalf("unexpected legacy peer counts: entry=%d committed=%d", entries, committed)
		}
	})

	t.Run("one entry and callback once", func(t *testing.T) {
		peer := newDirectivePeer(t, nil, nil)
		tm := openDirectiveTestMode(t, peer)
		hooks := tm.MockHooks()
		if hooks == nil {
			t.Fatal("enabled test mode returned nil mock hooks")
		}
		ctx, cancel := directiveTestContext(t)
		defer cancel()

		outcome, err := hooks.PaymentEntryResult(ctx, payment.PaymentOrderID(directiveTestOrderA))
		if err != nil {
			t.Fatalf("single-use entry failed (%s)", mockErrorClass(err))
		}
		if err := outcome.OnCommitted(ctx, payment.ResultSucceeded); err != nil {
			t.Fatalf("first callback failed (%s)", mockErrorClass(err))
		}
		if err := outcome.OnCommitted(ctx, payment.ResultSucceeded); !errors.Is(err, mock.ErrControlFault) {
			t.Fatalf("second callback returned %s", mockErrorClass(err))
		}
		entries, committed := peer.counts()
		if entries != 1 || committed != 1 {
			t.Fatalf("unexpected single-use peer counts: entry=%d committed=%d", entries, committed)
		}
		if commits := peer.snapshotCommits(); len(commits) != 1 {
			t.Fatalf("expected one committed proof, got %d", len(commits))
		}
	})

	t.Run("missing client fails closed", func(t *testing.T) {
		control := &testModeMockControl{}
		_, err := control.PaymentEntryResult(context.Background(), payment.PaymentOrderID(directiveTestOrderA))
		if !errors.Is(err, mock.ErrControlFault) {
			t.Fatalf("missing client returned %s", mockErrorClass(err))
		}
		var nilControl *testModeMockControl
		_, err = nilControl.PaymentEntryResult(context.Background(), payment.PaymentOrderID(directiveTestOrderA))
		if !errors.Is(err, mock.ErrControlFault) {
			t.Fatalf("nil control returned %s", mockErrorClass(err))
		}
	})

	t.Run("entry error fails closed", func(t *testing.T) {
		peer := newDirectivePeer(t, nil, nil)
		tm := openDirectiveTestMode(t, peer)
		hooks := tm.MockHooks()
		if hooks == nil {
			t.Fatal("enabled test mode returned nil mock hooks")
		}
		if err := tm.Close(); err != nil {
			t.Fatal("close test mode before entry error failed")
		}
		ctx, cancel := directiveTestContext(t)
		defer cancel()
		_, err := hooks.PaymentEntryResult(ctx, payment.PaymentOrderID(directiveTestOrderA))
		if !errors.Is(err, mock.ErrControlFault) {
			t.Fatalf("entry error returned %s", mockErrorClass(err))
		}
	})

	t.Run("invalid actual and enum fail closed", func(t *testing.T) {
		t.Run("invalid actual", func(t *testing.T) {
			peer := newDirectivePeer(t, map[string]testcontrol.Directive{
				directiveTestOrderA: testcontrol.DirectiveFailed,
			}, nil)
			tm := openDirectiveTestMode(t, peer)
			hooks := tm.MockHooks()
			if hooks == nil {
				t.Fatal("enabled test mode returned nil mock hooks")
			}
			ctx, cancel := directiveTestContext(t)
			defer cancel()
			outcome, err := hooks.PaymentEntryResult(ctx, payment.PaymentOrderID(directiveTestOrderA))
			if err != nil {
				t.Fatalf("invalid actual entry failed (%s)", mockErrorClass(err))
			}
			if err := outcome.OnCommitted(ctx, payment.ResultUnknown); !errors.Is(err, mock.ErrControlFault) {
				t.Fatalf("invalid actual callback returned %s", mockErrorClass(err))
			}
			if commits := peer.snapshotCommits(); len(commits) != 0 {
				t.Fatal("invalid actual sent a committed proof")
			}
		})

		t.Run("invalid enum mapping", func(t *testing.T) {
			if _, ok := testModePaymentResult(testcontrol.Directive("BOGUS")); ok {
				t.Fatal("invalid directive mapped to a payment result")
			}
			if _, ok := testModeAppliedDirective(payment.ResultUnknown); ok {
				t.Fatal("invalid actual mapped to a directive")
			}
			if _, ok := testModeAppliedDirective(payment.Result("BOGUS")); ok {
				t.Fatal("invalid actual string mapped to a directive")
			}
		})
	})

	t.Run("reply hidden is preserved", func(t *testing.T) {
		peer := newDirectivePeer(t, map[string]testcontrol.Directive{
			directiveTestOrderA: testcontrol.DirectiveFailed,
		}, map[string]bool{
			directiveTestOrderA: true,
		})
		tm := openDirectiveTestMode(t, peer)
		hooks := tm.MockHooks()
		if hooks == nil {
			t.Fatal("enabled test mode returned nil mock hooks")
		}
		ctx, cancel := directiveTestContext(t)
		defer cancel()

		outcome, err := hooks.PaymentEntryResult(ctx, payment.PaymentOrderID(directiveTestOrderA))
		if err != nil {
			t.Fatalf("reply hidden entry failed (%s)", mockErrorClass(err))
		}
		if outcome.Result == nil || *outcome.Result != payment.ResultFailed {
			t.Fatal("expected failed directive for reply hidden case")
		}
		if err := outcome.OnCommitted(ctx, payment.ResultFailed); !errors.Is(err, mock.ErrReplyHidden) {
			t.Fatalf("reply hidden callback returned %s", mockErrorClass(err))
		}
		commits := peer.snapshotCommits()
		if len(commits) != 1 {
			t.Fatalf("expected one reply-hidden proof, got %d", len(commits))
		}
		if commits[0].applied != testcontrol.DirectiveFailed {
			t.Fatal("reply hidden proof carried the wrong actual result")
		}
		if commits[0].action != testcontrol.ActionReplyHidden {
			t.Fatal("reply hidden proof did not record the hidden action")
		}
	})

	t.Run("default off does not dial", func(t *testing.T) {
		ctx, cancel := directiveTestContext(t)
		defer cancel()
		tm, err := OpenTestMode(ctx, TestModeConfig{Session: testcontrol.Session{
			Socket:     filepath.Join(t.TempDir(), "missing.sock"),
			RunID:      directiveTestRunID,
			Epoch:      directiveTestEpoch,
			Generation: 1,
		}})
		if err != nil {
			t.Fatalf("disabled test mode open failed (%s)", mockErrorClass(err))
		}
		if tm != nil {
			t.Fatal("disabled test mode returned non-nil")
		}
		if tm.MockHooks() != nil {
			t.Fatal("disabled test mode returned mock hooks")
		}
	})
}

func mockErrorClass(err error) string {
	switch {
	case err == nil:
		return "none"
	case errors.Is(err, mock.ErrControlFault):
		return "control fault"
	case errors.Is(err, mock.ErrReplyHidden):
		return "reply hidden"
	case errors.Is(err, ErrTestModeConfig):
		return "config"
	case errors.Is(err, ErrTestModeDisabled):
		return "disabled"
	default:
		return "unexpected"
	}
}
