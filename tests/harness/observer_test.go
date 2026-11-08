package harness

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"payment-hub/internal/testcontrol"
)

const (
	obsTestRunID        = "0123456789abcdef0123456789abcdef"
	obsTestEpoch        = "fedcba9876543210fedcba9876543210"
	obsTestBegin1       = "00112233445566778899aabbccddeeff"
	obsTestBegin2       = "ffeeddccbbaa99887766554433221100"
	obsTestOrderA       = "123e4567-e89b-42d3-a456-426614174000"
	obsTestOrderB       = "223e4567-e89b-42d3-b456-426614174001"
	obsTestCanary       = "FAKE_T018_OBSERVER_CANARY"
	obsTestBudget       = 5 * time.Second
	obsTestBarrier      = 3 * time.Second
	obsTestBarrierShort = 500 * time.Millisecond
	obsTestShort        = 250 * time.Millisecond

	obsTestWrongRunID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	obsTestWrongEpoch = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
)

type obsTestResult struct {
	frame testcontrol.Frame
	err   error
}

type obsTestHarness struct {
	t      *testing.T
	obs    *Observer
	socket string
	dir    string

	mu       sync.Mutex
	closed   bool
	closeErr error
}

func obsTestNewDir(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "phobs-")
	if err != nil {
		t.Fatal("create observer temp directory")
	}
	if err := os.Chmod(dir, 0o700); err != nil {
		t.Fatal("secure observer temp directory")
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(dir); err != nil {
			t.Errorf("remove observer temp directory")
		}
	})
	return dir
}

func obsTestConfigWithSocket(socket string) ObserverConfig {
	return ObserverConfig{
		Socket:         socket,
		RunID:          obsTestRunID,
		Epoch:          obsTestEpoch,
		BarrierTimeout: obsTestBarrier,
	}
}

func obsTestStart(t *testing.T, barrierTimeout time.Duration) *obsTestHarness {
	t.Helper()
	dir := obsTestNewDir(t)
	socket := filepath.Join(dir, "s")
	cfg := obsTestConfigWithSocket(socket)
	cfg.BarrierTimeout = barrierTimeout
	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	// StartObserver's context bounds startup only. Canceling it after success is
	// intentional and must not terminate the observer; Close owns the lifecycle.
	defer cancel()
	obs, err := StartObserver(ctx, cfg)
	if err != nil {
		t.Fatal("start observer failed")
	}
	if obs == nil {
		t.Fatal("start observer returned a nil observer")
	}
	h := &obsTestHarness{t: t, obs: obs, socket: socket, dir: dir}
	t.Cleanup(h.cleanup)
	return h
}

func obsTestStartExpectError(t *testing.T, cfg ObserverConfig) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	obs, err := StartObserver(ctx, cfg)
	if err == nil {
		closeCtx, closeCancel := context.WithTimeout(context.Background(), obsTestBudget)
		defer closeCancel()
		if obs != nil {
			_ = obs.Close(closeCtx)
		}
		t.Fatal("start observer unexpectedly succeeded")
	}
	if errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("start observer exceeded the test budget")
	}
}

func (h *obsTestHarness) close() error {
	h.mu.Lock()
	if h.closed {
		err := h.closeErr
		h.mu.Unlock()
		return err
	}
	h.closed = true
	h.mu.Unlock()

	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	err := h.obs.Close(ctx)

	h.mu.Lock()
	h.closeErr = err
	h.mu.Unlock()
	return err
}

func (h *obsTestHarness) cleanup() {
	h.mu.Lock()
	closed := h.closed
	h.mu.Unlock()
	if closed {
		return
	}
	if err := h.close(); err != nil {
		h.t.Errorf("close observer")
	}
}

type obsTestExchangeOutcome struct {
	raw    []byte
	dialed bool
	wrote  bool
	err    error
}

func obsTestExchangeRaw(ctx context.Context, socket string, payload []byte) obsTestExchangeOutcome {
	var dialer net.Dialer
	conn, err := dialer.DialContext(ctx, "unix", socket)
	if err != nil {
		return obsTestExchangeOutcome{err: err}
	}
	defer conn.Close()

	if deadline, ok := ctx.Deadline(); ok {
		if err := conn.SetDeadline(deadline); err != nil {
			return obsTestExchangeOutcome{dialed: true, err: err}
		}
	}

	n, err := conn.Write(payload)
	if err != nil {
		return obsTestExchangeOutcome{dialed: true, err: err}
	}
	if n != len(payload) {
		return obsTestExchangeOutcome{dialed: true, err: errors.New("short observer request write")}
	}

	unixConn, ok := conn.(*net.UnixConn)
	if !ok {
		return obsTestExchangeOutcome{dialed: true, err: errors.New("observer connection is not a unix socket")}
	}
	if err := unixConn.CloseWrite(); err != nil {
		return obsTestExchangeOutcome{dialed: true, err: err}
	}

	raw, err := obsTestReadResponse(conn)
	return obsTestExchangeOutcome{raw: raw, dialed: true, wrote: true, err: err}
}

// obsTestReadResponse reads the complete response until EOF, bounded by one
// byte above MaxFrameBytes so the codec remains the authority that rejects an
// oversized response. It never treats a timeout or partial read as success.
func obsTestReadResponse(conn net.Conn) ([]byte, error) {
	limit := int64(testcontrol.MaxFrameBytes) + 1
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
			return nil, err
		}
		if int64(len(buf)) >= limit {
			return buf, nil
		}
	}
}

func obsTestIsTimeout(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, os.ErrDeadlineExceeded) {
		return true
	}
	var netErr net.Error
	return errors.As(err, &netErr) && netErr.Timeout()
}

func obsTestExplicitPeerFailure(err error) bool {
	if err == nil {
		return false
	}
	if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
		return true
	}
	return errors.Is(err, syscall.ECONNRESET) || errors.Is(err, syscall.EPIPE)
}

func obsTestSend(ctx context.Context, socket string, frame testcontrol.Frame) (testcontrol.Frame, error) {
	payload, err := testcontrol.EncodeFrame(frame)
	if err != nil {
		return testcontrol.Frame{}, err
	}
	outcome := obsTestExchangeRaw(ctx, socket, payload)
	if outcome.err != nil {
		return testcontrol.Frame{}, outcome.err
	}
	// Keep an explicit peer EOF distinguishable from a malformed nonempty
	// response; Close is allowed to interrupt a pending exchange this way.
	if len(outcome.raw) == 0 {
		return testcontrol.Frame{}, io.EOF
	}
	return testcontrol.DecodeFrame(outcome.raw)
}

func obsTestMustEncode(t *testing.T, frame testcontrol.Frame) []byte {
	t.Helper()
	payload, err := testcontrol.EncodeFrame(frame)
	if err != nil {
		t.Fatal("test frame failed protocol encoding")
	}
	return payload
}

func obsTestMustDo(t *testing.T, h *obsTestHarness, frame testcontrol.Frame) testcontrol.Frame {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	resp, err := obsTestSend(ctx, h.socket, frame)
	if err != nil {
		t.Fatal("observer exchange failed")
	}
	if resp.Type != testcontrol.TypeAck {
		t.Fatal("expected an ack response")
	}
	return resp
}

func obsTestAwait(t *testing.T, ctx context.Context, ch <-chan obsTestResult) obsTestResult {
	t.Helper()
	select {
	case res := <-ch:
		return res
	case <-ctx.Done():
		t.Fatal("observer exchange did not complete within budget")
	}
	return obsTestResult{}
}

func obsTestCheckAck(t *testing.T, req, resp testcontrol.Frame) {
	t.Helper()
	if resp.Version != testcontrol.Version || resp.Type != testcontrol.TypeAck {
		t.Fatal("response is not a valid ack")
	}
	if resp.RunID != req.RunID || resp.Epoch != req.Epoch ||
		resp.Generation != req.Generation || resp.EventID != req.EventID {
		t.Fatal("ack identity mismatch")
	}
	if resp.For != req.Type {
		t.Fatal("ack target mismatch")
	}
	if resp.Kind != "" || resp.Point != "" || resp.OrderID != "" {
		t.Fatal("ack carries forbidden fields")
	}

	switch req.Type {
	case testcontrol.TypeBegin:
		if resp.Seq != 0 || resp.Action != "" || resp.ExpectedLastSeq != nil {
			t.Fatal("begin ack carries forbidden fields")
		}
	case testcontrol.TypeCall:
		if resp.Seq != req.Seq || resp.Action != "" || resp.ExpectedLastSeq != nil {
			t.Fatal("call ack fields mismatch")
		}
	case testcontrol.TypeReach:
		if resp.Seq != req.Seq || resp.ExpectedLastSeq != nil {
			t.Fatal("reach ack fields mismatch")
		}
		if resp.Action != testcontrol.ActionContinue && resp.Action != testcontrol.ActionReplyHidden {
			t.Fatal("reach ack action is invalid")
		}
	case testcontrol.TypeSeal:
		if resp.Seq != 0 || resp.Action != "" {
			t.Fatal("seal ack carries forbidden fields")
		}
		if req.ExpectedLastSeq == nil || resp.ExpectedLastSeq == nil ||
			*resp.ExpectedLastSeq != *req.ExpectedLastSeq {
			t.Fatal("seal ack expected_last_seq mismatch")
		}
	default:
		t.Fatal("ack target type is invalid")
	}
}

func obsTestCheckFault(t *testing.T, resp testcontrol.Frame) {
	t.Helper()
	if resp.Version != testcontrol.Version || resp.Type != testcontrol.TypeFault {
		t.Fatal("response is not a valid fault")
	}
	switch resp.Code {
	case testcontrol.FaultProtocol, testcontrol.FaultIncomplete,
		testcontrol.FaultTimeout, testcontrol.FaultClosed:
	default:
		t.Fatal("fault code is invalid")
	}
	if resp.RunID != "" || resp.Epoch != "" || resp.Generation != 0 ||
		resp.Seq != 0 || resp.EventID != "" || resp.Kind != "" ||
		resp.Point != "" || resp.OrderID != "" || resp.ExpectedLastSeq != nil ||
		resp.For != "" || resp.Action != "" {
		t.Fatal("fault frame carries forbidden fields")
	}
}

func obsTestRejectRawSemantic(t *testing.T, h *obsTestHarness, payload []byte) []byte {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	outcome := obsTestExchangeRaw(ctx, h.socket, payload)
	if !outcome.dialed {
		t.Fatal("observer dial or setup failed")
	}
	if !outcome.wrote {
		t.Fatal("semantic request was not fully sent")
	}
	if outcome.err != nil {
		if obsTestIsTimeout(outcome.err) || errors.Is(outcome.err, context.Canceled) {
			t.Fatal("observer did not reject the semantic frame within budget")
		}
		t.Fatal("semantic rejection did not return a fault")
	}
	resp, err := testcontrol.DecodeFrame(outcome.raw)
	if err != nil {
		t.Fatal("observer rejection was not a protocol frame")
	}
	obsTestCheckFault(t, resp)
	return outcome.raw
}

func obsTestRejectFrame(t *testing.T, h *obsTestHarness, frame testcontrol.Frame) {
	t.Helper()
	obsTestRejectRawSemantic(t, h, obsTestMustEncode(t, frame))
}

func obsTestRejectMalformedRaw(t *testing.T, h *obsTestHarness, payload []byte) []byte {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	outcome := obsTestExchangeRaw(ctx, h.socket, payload)
	if !outcome.dialed {
		t.Fatal("observer dial or setup failed")
	}
	if !outcome.wrote {
		if obsTestExplicitPeerFailure(outcome.err) {
			return nil
		}
		if obsTestIsTimeout(outcome.err) || errors.Is(outcome.err, context.Canceled) {
			t.Fatal("observer did not reject the malformed frame within budget")
		}
		t.Fatal("malformed request ended with an unexpected setup error")
	}
	if outcome.err != nil {
		if obsTestIsTimeout(outcome.err) || errors.Is(outcome.err, context.Canceled) {
			t.Fatal("observer did not reject the malformed frame within budget")
		}
		if obsTestExplicitPeerFailure(outcome.err) {
			return nil
		}
		t.Fatal("malformed rejection ended with an unexpected error")
	}
	if len(outcome.raw) == 0 {
		return nil
	}
	resp, err := testcontrol.DecodeFrame(outcome.raw)
	if err != nil {
		t.Fatal("observer rejection was not a protocol frame")
	}
	obsTestCheckFault(t, resp)
	return outcome.raw
}

func obsTestCheckPendingEnded(t *testing.T, res obsTestResult) {
	t.Helper()
	if res.err == nil {
		if res.frame.Type != testcontrol.TypeFault {
			t.Fatal("pending exchange ended without a fault")
		}
		obsTestCheckFault(t, res.frame)
		return
	}
	if obsTestIsTimeout(res.err) || errors.Is(res.err, context.Canceled) {
		t.Fatal("pending exchange did not end within budget")
	}
	if !obsTestExplicitPeerFailure(res.err) {
		t.Fatal("pending exchange ended with an unexpected error")
	}
}

func obsTestReadPipe(t *testing.T, payload []byte, closeWrite bool, deadline time.Duration) ([]byte, error) {
	t.Helper()
	reader, writer := net.Pipe()
	defer reader.Close()
	defer writer.Close()
	go func() {
		_, _ = writer.Write(payload)
		if closeWrite {
			_ = writer.Close()
		}
	}()
	if deadline > 0 {
		if err := reader.SetReadDeadline(time.Now().Add(deadline)); err != nil {
			t.Fatal("set pipe read deadline failed")
		}
	}
	return obsTestReadResponse(reader)
}

func obsTestAssertUncertain(t *testing.T, h *obsTestHarness) {
	t.Helper()
	counts, err := h.obs.Snapshot()
	if !errors.Is(err, testcontrol.ErrUncertain) {
		t.Fatal("snapshot is not uncertain")
	}
	if counts != (Counts{}) {
		t.Fatal("uncertain snapshot returned nonzero counts")
	}
}

func obsTestAssertCounts(t *testing.T, h *obsTestHarness, want Counts) {
	t.Helper()
	counts, err := h.obs.Snapshot()
	if err != nil {
		t.Fatal("trusted snapshot failed")
	}
	if counts != want {
		t.Fatal("snapshot counts mismatch")
	}
}

func obsTestEventID(beginID string, seq uint64) string {
	return beginID + ":" + strconv.FormatUint(seq, 10)
}

func obsTestBeginFrame(gen uint64, beginID string) testcontrol.Frame {
	return testcontrol.Frame{
		Version:    testcontrol.Version,
		Type:       testcontrol.TypeBegin,
		RunID:      obsTestRunID,
		Epoch:      obsTestEpoch,
		Generation: gen,
		EventID:    beginID,
	}
}

func obsTestCallFrame(gen uint64, beginID string, seq uint64, kind testcontrol.Kind, orderID string) testcontrol.Frame {
	return testcontrol.Frame{
		Version:    testcontrol.Version,
		Type:       testcontrol.TypeCall,
		RunID:      obsTestRunID,
		Epoch:      obsTestEpoch,
		Generation: gen,
		Seq:        seq,
		EventID:    obsTestEventID(beginID, seq),
		Kind:       kind,
		OrderID:    orderID,
	}
}

func obsTestReachFrame(gen uint64, beginID string, seq uint64, point testcontrol.Point, orderID string) testcontrol.Frame {
	return testcontrol.Frame{
		Version:    testcontrol.Version,
		Type:       testcontrol.TypeReach,
		RunID:      obsTestRunID,
		Epoch:      obsTestEpoch,
		Generation: gen,
		Seq:        seq,
		EventID:    obsTestEventID(beginID, seq),
		Point:      point,
		OrderID:    orderID,
	}
}

func obsTestExpectOpen(t *testing.T, h *obsTestHarness, gen uint64, beginID string) {
	t.Helper()
	if err := h.obs.Expect(gen); err != nil {
		t.Fatal("expect generation failed")
	}
	begin := obsTestBeginFrame(gen, beginID)
	resp := obsTestMustDo(t, h, begin)
	obsTestCheckAck(t, begin, resp)
}

func obsTestSealZero(t *testing.T, h *obsTestHarness, gen uint64) testcontrol.Receipt {
	t.Helper()
	receipt, err := h.obs.Seal(gen, 0)
	if err != nil {
		t.Fatal("seal trusted zero generation failed")
	}
	if receipt.RunID != obsTestRunID || receipt.Epoch != obsTestEpoch ||
		receipt.Generation != gen || receipt.LastSeq != 0 {
		t.Fatal("seal receipt mismatch")
	}
	return receipt
}

func obsTestRequireProtocolError(t *testing.T, err error) {
	t.Helper()
	if err == nil {
		t.Fatal("expected a protocol error")
	}
	if !errors.Is(err, testcontrol.ErrProtocol) {
		t.Fatal("error is not ErrProtocol")
	}
}

func obsTestRequireIntegrityError(t *testing.T, err error) {
	t.Helper()
	if err == nil {
		t.Fatal("expected an integrity error")
	}
	if !errors.Is(err, testcontrol.ErrControlFault) && !errors.Is(err, testcontrol.ErrUncertain) {
		t.Fatal("error is not ErrControlFault or ErrUncertain")
	}
}

func obsTestSealFrame(gen uint64, beginID string, expected uint64) testcontrol.Frame {
	expectedCopy := expected
	return testcontrol.Frame{
		Version:         testcontrol.Version,
		Type:            testcontrol.TypeSeal,
		RunID:           obsTestRunID,
		Epoch:           obsTestEpoch,
		Generation:      gen,
		EventID:         beginID + ":seal",
		ExpectedLastSeq: &expectedCopy,
	}
}

func obsTestRawSeal(gen uint64, beginID string, expected uint64) string {
	return `{"v":1,"type":"seal","run_id":"` + obsTestRunID +
		`","observer_epoch":"` + obsTestEpoch +
		`","generation":` + strconv.FormatUint(gen, 10) +
		`,"event_id":"` + beginID + `:seal","expected_last_seq":` +
		strconv.FormatUint(expected, 10) + `}`
}

func obsTestBarrierRoundTrip(t *testing.T, h *obsTestHarness, gen, seq uint64, beginID string,
	point testcontrol.Point, orderID string, action testcontrol.Action) testcontrol.Receipt {
	t.Helper()
	barrier, err := h.obs.Arm(gen, point, orderID)
	if err != nil {
		t.Fatal("arm barrier failed")
	}
	reach := obsTestReachFrame(gen, beginID, seq, point, orderID)
	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	ch := make(chan obsTestResult, 1)
	go func() {
		resp, err := obsTestSend(ctx, h.socket, reach)
		ch <- obsTestResult{frame: resp, err: err}
	}()

	receipt, err := barrier.Wait(ctx)
	if err != nil {
		t.Fatal("barrier wait failed")
	}
	if err := barrier.Release(action); err != nil {
		t.Fatal("barrier release failed")
	}
	res := obsTestAwait(t, ctx, ch)
	if res.err != nil {
		t.Fatal("barrier reach exchange failed")
	}
	obsTestCheckAck(t, reach, res.frame)
	if res.frame.Action != action {
		t.Fatal("barrier action was not acknowledged")
	}
	return receipt
}

func obsTestRawBegin(beginID string) string {
	return `{"v":1,"type":"begin","run_id":"` + obsTestRunID +
		`","observer_epoch":"` + obsTestEpoch +
		`","generation":1,"event_id":"` + beginID + `"}`
}

func obsTestRawCall(eventID string, seq uint64, kind, orderID string) string {
	return `{"v":1,"type":"call","run_id":"` + obsTestRunID +
		`","observer_epoch":"` + obsTestEpoch +
		`","generation":1,"seq":` + strconv.FormatUint(seq, 10) +
		`,"event_id":"` + eventID + `","kind":"` + kind +
		`","order_id":"` + orderID + `"}`
}

func TestObserverResponseReaderHelper(t *testing.T) {
	frame := testcontrol.Frame{Version: testcontrol.Version, Type: testcontrol.TypeFault, Code: testcontrol.FaultProtocol}
	encoded := obsTestMustEncode(t, frame)

	t.Run("single_frame", func(t *testing.T) {
		raw, err := obsTestReadPipe(t, encoded, true, obsTestBudget)
		if err != nil {
			t.Fatal("single frame read failed")
		}
		if !bytes.Equal(raw, encoded) {
			t.Fatal("single frame bytes mismatch")
		}
		decoded, err := testcontrol.DecodeFrame(raw)
		if err != nil {
			t.Fatal("single frame did not decode")
		}
		if decoded.Type != testcontrol.TypeFault || decoded.Code != testcontrol.FaultProtocol {
			t.Fatal("single frame decoded incorrectly")
		}
	})

	t.Run("frame_plus_extra_byte", func(t *testing.T) {
		payload := append(append([]byte{}, encoded...), 'x')
		raw, err := obsTestReadPipe(t, payload, true, obsTestBudget)
		if err != nil {
			t.Fatal("extra byte read failed")
		}
		if _, err := testcontrol.DecodeFrame(raw); err == nil {
			t.Fatal("frame plus extra byte was accepted")
		}
	})

	t.Run("two_frames", func(t *testing.T) {
		payload := append(append([]byte{}, encoded...), encoded...)
		raw, err := obsTestReadPipe(t, payload, true, obsTestBudget)
		if err != nil {
			t.Fatal("two frame read failed")
		}
		if _, err := testcontrol.DecodeFrame(raw); err == nil {
			t.Fatal("two frames were accepted as one")
		}
	})

	t.Run("missing_newline", func(t *testing.T) {
		payload := encoded[:len(encoded)-1]
		raw, err := obsTestReadPipe(t, payload, true, obsTestBudget)
		if err != nil {
			t.Fatal("missing newline read failed")
		}
		if _, err := testcontrol.DecodeFrame(raw); err == nil {
			t.Fatal("frame without newline was accepted")
		}
	})

	t.Run("oversized", func(t *testing.T) {
		payload := bytes.Repeat([]byte{'x'}, testcontrol.MaxFrameBytes+1)
		raw, err := obsTestReadPipe(t, payload, true, obsTestBudget)
		if err != nil {
			t.Fatal("oversized response read failed")
		}
		if len(raw) != testcontrol.MaxFrameBytes+1 {
			t.Fatal("oversized response length mismatch")
		}
		if _, err := testcontrol.DecodeFrame(raw); err == nil {
			t.Fatal("oversized response was accepted")
		}
	})

	t.Run("no_close_timeout", func(t *testing.T) {
		raw, err := obsTestReadPipe(t, encoded, false, obsTestShort)
		if err == nil {
			t.Fatal("open response was accepted")
		}
		if !obsTestIsTimeout(err) {
			t.Fatal("open response error is not a timeout")
		}
		if len(raw) != 0 {
			t.Fatal("timed out response returned bytes")
		}
	})
}

func TestObserverUncertainStates(t *testing.T) {
	t.Run("initial_empty_expected", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestAssertUncertain(t, h)
	})

	t.Run("expected_without_begin", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		if err := h.obs.Expect(1); err != nil {
			t.Fatal("expect generation failed")
		}
		obsTestAssertUncertain(t, h)
	})

	t.Run("begun_without_seal", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)
		obsTestAssertUncertain(t, h)
	})

	t.Run("seal_before_begin", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		if _, err := h.obs.Seal(1, 0); err == nil {
			t.Fatal("seal before begin was accepted")
		}
		obsTestAssertUncertain(t, h)
	})

	t.Run("polling_unsealed_does_not_latch", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)
		obsTestAssertUncertain(t, h)

		call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
		resp := obsTestMustDo(t, h, call)
		obsTestCheckAck(t, call, resp)
		if _, err := h.obs.Seal(1, 1); err != nil {
			t.Fatal("snapshot polling poisoned the run")
		}
		obsTestAssertCounts(t, h, Counts{Payment: 1})
	})
}

func TestObserverTrustedZero(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	if err := h.obs.Expect(1); err != nil {
		t.Fatal("expect generation failed")
	}

	begin := obsTestBeginFrame(1, obsTestBegin1)
	resp := obsTestMustDo(t, h, begin)
	obsTestCheckAck(t, begin, resp)
	resp = obsTestMustDo(t, h, begin)
	obsTestCheckAck(t, begin, resp)

	receipt := obsTestSealZero(t, h, 1)
	obsTestAssertCounts(t, h, Counts{})

	retry, err := h.obs.Seal(1, 0)
	if err != nil {
		t.Fatal("idempotent seal retry failed")
	}
	if retry != receipt {
		t.Fatal("seal receipt changed on retry")
	}
	obsTestAssertCounts(t, h, Counts{})
}

func TestObserverWireSealAckAndRetry(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)

	call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
	resp := obsTestMustDo(t, h, call)
	obsTestCheckAck(t, call, resp)

	seal := obsTestSealFrame(1, obsTestBegin1, 1)
	first := obsTestMustDo(t, h, seal)
	obsTestCheckAck(t, seal, first)
	if first.EventID != obsTestBegin1+":seal" {
		t.Fatal("wire seal ack event_id mismatch")
	}

	second := obsTestMustDo(t, h, seal)
	obsTestCheckAck(t, seal, second)
	if first.EventID != second.EventID || first.For != second.For ||
		first.ExpectedLastSeq == nil || second.ExpectedLastSeq == nil ||
		*first.ExpectedLastSeq != *second.ExpectedLastSeq {
		t.Fatal("wire seal retry ack mismatch")
	}

	bad := obsTestSealFrame(1, obsTestBegin1, 2)
	obsTestRejectFrame(t, h, bad)
	obsTestAssertUncertain(t, h)
	if _, err := h.obs.Seal(1, 1); err == nil {
		t.Fatal("polluted run closed after a wrong wire seal")
	}
}

func TestObserverWireSealOverLimit(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)

	payload := obsTestRawSeal(1, obsTestBegin1, testcontrol.MaxRecords+1)
	obsTestRejectMalformedRaw(t, h, []byte(payload+"\n"))
	obsTestAssertUncertain(t, h)
	if _, err := h.obs.Seal(1, 0); err == nil {
		t.Fatal("polluted run closed after an over-limit wire seal")
	}
}

func TestObserverIndependentCalls(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)

	calls := []testcontrol.Frame{
		obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA),
		obsTestCallFrame(1, obsTestBegin1, 2, testcontrol.KindPayment, obsTestOrderA),
		obsTestCallFrame(1, obsTestBegin1, 3, testcontrol.KindQuery, obsTestOrderA),
		obsTestCallFrame(1, obsTestBegin1, 4, testcontrol.KindQuery, obsTestOrderB),
	}
	for _, call := range calls {
		resp := obsTestMustDo(t, h, call)
		obsTestCheckAck(t, call, resp)
	}

	receipt, err := h.obs.Seal(1, 4)
	if err != nil {
		t.Fatal("seal complete generation failed")
	}
	if receipt.LastSeq != 4 {
		t.Fatal("seal receipt last sequence mismatch")
	}
	obsTestAssertCounts(t, h, Counts{Payment: 2, Query: 2})
}

func TestObserverRetransmissionIdempotent(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)

	begin := obsTestBeginFrame(1, obsTestBegin1)
	resp := obsTestMustDo(t, h, begin)
	obsTestCheckAck(t, begin, resp)

	call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
	resp = obsTestMustDo(t, h, call)
	obsTestCheckAck(t, call, resp)
	resp = obsTestMustDo(t, h, call)
	obsTestCheckAck(t, call, resp)

	reach := obsTestReachFrame(1, obsTestBegin1, 2, testcontrol.PointRegistered, obsTestOrderA)
	resp = obsTestMustDo(t, h, reach)
	obsTestCheckAck(t, reach, resp)
	if resp.Action != testcontrol.ActionContinue {
		t.Fatal("unarmed reach action mismatch")
	}
	resp = obsTestMustDo(t, h, reach)
	obsTestCheckAck(t, reach, resp)
	if resp.Action != testcontrol.ActionContinue {
		t.Fatal("retransmitted reach action mismatch")
	}

	if _, err := h.obs.Seal(1, 2); err != nil {
		t.Fatal("seal after retransmissions failed")
	}
	obsTestAssertCounts(t, h, Counts{Payment: 1})
}

func TestObserverOutOfOrderComplete(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)

	frames := []testcontrol.Frame{
		obsTestCallFrame(1, obsTestBegin1, 3, testcontrol.KindPayment, obsTestOrderA),
		obsTestReachFrame(1, obsTestBegin1, 2, testcontrol.PointRegistered, obsTestOrderA),
		obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA),
	}
	for _, frame := range frames {
		resp := obsTestMustDo(t, h, frame)
		obsTestCheckAck(t, frame, resp)
	}

	receipt, err := h.obs.Seal(1, 3)
	if err != nil {
		t.Fatal("seal complete out-of-order sequence failed")
	}
	if receipt.LastSeq != 3 {
		t.Fatal("seal receipt last sequence mismatch")
	}
	obsTestAssertCounts(t, h, Counts{Payment: 2})
}

func TestObserverReachWithoutArm(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)

	reach := obsTestReachFrame(1, obsTestBegin1, 1, testcontrol.PointRegistered, obsTestOrderA)
	resp := obsTestMustDo(t, h, reach)
	obsTestCheckAck(t, reach, resp)
	if resp.Action != testcontrol.ActionContinue {
		t.Fatal("unarmed reach action mismatch")
	}
	resp = obsTestMustDo(t, h, reach)
	obsTestCheckAck(t, reach, resp)
	if resp.Action != testcontrol.ActionContinue {
		t.Fatal("retransmitted unarmed reach action mismatch")
	}

	if _, err := h.obs.Seal(1, 1); err != nil {
		t.Fatal("seal reach-only generation failed")
	}
	obsTestAssertCounts(t, h, Counts{})
}

func TestObserverCrossGeneration(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)

	obsTestExpectOpen(t, h, 1, obsTestBegin1)
	call1 := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
	resp := obsTestMustDo(t, h, call1)
	obsTestCheckAck(t, call1, resp)
	receipt1, err := h.obs.Seal(1, 1)
	if err != nil {
		t.Fatal("seal first generation failed")
	}
	if receipt1.Generation != 1 {
		t.Fatal("first generation receipt mismatch")
	}

	obsTestExpectOpen(t, h, 2, obsTestBegin2)
	call2 := obsTestCallFrame(2, obsTestBegin2, 1, testcontrol.KindPayment, obsTestOrderB)
	resp = obsTestMustDo(t, h, call2)
	obsTestCheckAck(t, call2, resp)
	query2 := obsTestCallFrame(2, obsTestBegin2, 2, testcontrol.KindQuery, obsTestOrderB)
	resp = obsTestMustDo(t, h, query2)
	obsTestCheckAck(t, query2, resp)
	receipt2, err := h.obs.Seal(2, 2)
	if err != nil {
		t.Fatal("seal second generation failed")
	}
	if receipt2.Generation != 2 || receipt2.LastSeq != 2 {
		t.Fatal("second generation receipt mismatch")
	}

	obsTestAssertCounts(t, h, Counts{Payment: 2, Query: 1})
}

func TestObserverRunRecordCapacityAcrossGenerations(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)
	obsTestExpectOpen(t, h, 2, obsTestBegin2)

	const perGeneration = uint64(testcontrol.MaxRecords / 2)
	for seq := uint64(1); seq <= perGeneration; seq++ {
		call := obsTestCallFrame(1, obsTestBegin1, seq, testcontrol.KindPayment, obsTestOrderA)
		resp := obsTestMustDo(t, h, call)
		obsTestCheckAck(t, call, resp)
	}
	for seq := uint64(1); seq <= perGeneration; seq++ {
		call := obsTestCallFrame(2, obsTestBegin2, seq, testcontrol.KindQuery, obsTestOrderB)
		resp := obsTestMustDo(t, h, call)
		obsTestCheckAck(t, call, resp)
	}

	overflow := obsTestCallFrame(1, obsTestBegin1, perGeneration+1, testcontrol.KindPayment, obsTestOrderA)
	obsTestRejectFrame(t, h, overflow)
	obsTestAssertUncertain(t, h)
}

func TestObserverExpectRules(t *testing.T) {
	t.Run("zero_generation", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestRequireProtocolError(t, h.obs.Expect(0))
		obsTestAssertUncertain(t, h)
	})

	t.Run("duplicate_after_open", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)
		obsTestRequireProtocolError(t, h.obs.Expect(1))
		call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
		resp := obsTestMustDo(t, h, call)
		obsTestCheckAck(t, call, resp)
		if _, err := h.obs.Seal(1, 1); err != nil {
			t.Fatal("duplicate expectation reset the open generation")
		}
		obsTestAssertCounts(t, h, Counts{Payment: 1})
	})

	t.Run("duplicate_after_seal", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)
		receipt := obsTestSealZero(t, h, 1)
		obsTestRequireProtocolError(t, h.obs.Expect(1))
		retry, err := h.obs.Seal(1, 0)
		if err != nil {
			t.Fatal("duplicate expectation reset the sealed generation")
		}
		if retry != receipt {
			t.Fatal("duplicate expectation changed the sealed receipt")
		}
		obsTestAssertCounts(t, h, Counts{})
	})

	t.Run("max_generations", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		maxGeneration := ^uint64(0)
		if err := h.obs.Expect(maxGeneration); err != nil {
			t.Fatal("maximum generation value was rejected")
		}
		for gen := uint64(1); gen <= 63; gen++ {
			if err := h.obs.Expect(gen); err != nil {
				t.Fatal("generation registration below the limit failed")
			}
		}
		obsTestRequireProtocolError(t, h.obs.Expect(64))

		begin := obsTestBeginFrame(maxGeneration, obsTestBegin1)
		resp := obsTestMustDo(t, h, begin)
		obsTestCheckAck(t, begin, resp)
		obsTestAssertUncertain(t, h)
	})
}

func TestObserverProtocolViolationsPolluteRun(t *testing.T) {
	cases := []struct {
		name string
		run  func(t *testing.T, h *obsTestHarness)
	}{
		{
			name: "same_seq_different_kind",
			run: func(t *testing.T, h *obsTestHarness) {
				obsTestExpectOpen(t, h, 1, obsTestBegin1)
				first := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
				obsTestMustDo(t, h, first)
				conflict := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindQuery, obsTestOrderA)
				obsTestRejectFrame(t, h, conflict)
			},
		},
		{
			name: "same_seq_different_order",
			run: func(t *testing.T, h *obsTestHarness) {
				obsTestExpectOpen(t, h, 1, obsTestBegin1)
				first := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
				obsTestMustDo(t, h, first)
				conflict := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderB)
				obsTestRejectFrame(t, h, conflict)
			},
		},
		{
			name: "same_seq_different_type",
			run: func(t *testing.T, h *obsTestHarness) {
				obsTestExpectOpen(t, h, 1, obsTestBegin1)
				first := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
				obsTestMustDo(t, h, first)
				conflict := obsTestReachFrame(1, obsTestBegin1, 1, testcontrol.PointRegistered, obsTestOrderA)
				obsTestRejectFrame(t, h, conflict)
			},
		},
		{
			name: "event_id_other_begin_same_seq",
			run: func(t *testing.T, h *obsTestHarness) {
				obsTestExpectOpen(t, h, 1, obsTestBegin1)
				conflict := obsTestCallFrame(1, obsTestBegin2, 1, testcontrol.KindPayment, obsTestOrderA)
				obsTestRejectFrame(t, h, conflict)
			},
		},
		{
			name: "wrong_epoch",
			run: func(t *testing.T, h *obsTestHarness) {
				if err := h.obs.Expect(1); err != nil {
					t.Fatal("expect generation failed")
				}
				bad := obsTestBeginFrame(1, obsTestBegin1)
				bad.Epoch = obsTestWrongEpoch
				obsTestRejectFrame(t, h, bad)
				obsTestRejectFrame(t, h, obsTestBeginFrame(1, obsTestBegin1))
			},
		},
		{
			name: "wrong_run",
			run: func(t *testing.T, h *obsTestHarness) {
				if err := h.obs.Expect(1); err != nil {
					t.Fatal("expect generation failed")
				}
				bad := obsTestBeginFrame(1, obsTestBegin1)
				bad.RunID = obsTestWrongRunID
				obsTestRejectFrame(t, h, bad)
				obsTestRejectFrame(t, h, obsTestBeginFrame(1, obsTestBegin1))
			},
		},
		{
			name: "unknown_generation",
			run: func(t *testing.T, h *obsTestHarness) {
				if err := h.obs.Expect(1); err != nil {
					t.Fatal("expect generation failed")
				}
				obsTestRejectFrame(t, h, obsTestBeginFrame(2, obsTestBegin1))
				obsTestRejectFrame(t, h, obsTestBeginFrame(1, obsTestBegin1))
			},
		},
		{
			name: "second_begin_client",
			run: func(t *testing.T, h *obsTestHarness) {
				obsTestExpectOpen(t, h, 1, obsTestBegin1)
				obsTestRejectFrame(t, h, obsTestBeginFrame(1, obsTestBegin2))
				obsTestRejectFrame(t, h, obsTestBeginFrame(1, obsTestBegin1))
			},
		},
		{
			name: "begin_id_reuse_across_generations",
			run: func(t *testing.T, h *obsTestHarness) {
				obsTestExpectOpen(t, h, 1, obsTestBegin1)
				call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
				obsTestMustDo(t, h, call)
				if _, err := h.obs.Seal(1, 1); err != nil {
					t.Fatal("seal first generation failed")
				}
				if err := h.obs.Expect(2); err != nil {
					t.Fatal("expect second generation failed")
				}
				obsTestRejectFrame(t, h, obsTestBeginFrame(2, obsTestBegin1))
				obsTestRejectFrame(t, h, obsTestBeginFrame(2, obsTestBegin2))
			},
		},
		{
			name: "missing_tail_seal",
			run: func(t *testing.T, h *obsTestHarness) {
				obsTestExpectOpen(t, h, 1, obsTestBegin1)
				call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
				obsTestMustDo(t, h, call)
				if _, err := h.obs.Seal(1, 2); err == nil {
					t.Fatal("seal with a missing tail was accepted")
				}
				if _, err := h.obs.Seal(1, 1); err == nil {
					t.Fatal("polluted seal recovered after a missing tail")
				}
			},
		},
		{
			name: "intermediate_gap_seal",
			run: func(t *testing.T, h *obsTestHarness) {
				obsTestExpectOpen(t, h, 1, obsTestBegin1)
				first := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
				obsTestMustDo(t, h, first)
				third := obsTestCallFrame(1, obsTestBegin1, 3, testcontrol.KindQuery, obsTestOrderB)
				obsTestMustDo(t, h, third)
				if _, err := h.obs.Seal(1, 3); err == nil {
					t.Fatal("seal with an intermediate gap was accepted")
				}
				if _, err := h.obs.Seal(1, 3); err == nil {
					t.Fatal("polluted seal recovered after an intermediate gap")
				}
			},
		},
		{
			name: "seal_zero_with_tail",
			run: func(t *testing.T, h *obsTestHarness) {
				obsTestExpectOpen(t, h, 1, obsTestBegin1)
				call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
				obsTestMustDo(t, h, call)
				if _, err := h.obs.Seal(1, 0); err == nil {
					t.Fatal("seal with a zero count and an existing tail was accepted")
				}
				if _, err := h.obs.Seal(1, 1); err == nil {
					t.Fatal("polluted seal recovered after a zero count")
				}
			},
		},
		{
			name: "seal_above_max_records",
			run: func(t *testing.T, h *obsTestHarness) {
				obsTestExpectOpen(t, h, 1, obsTestBegin1)
				if _, err := h.obs.Seal(1, testcontrol.MaxRecords+1); err == nil {
					t.Fatal("seal above the record limit was accepted")
				}
				if _, err := h.obs.Seal(1, 0); err == nil {
					t.Fatal("polluted seal recovered after an out-of-range count")
				}
			},
		},
		{
			name: "wrong_seal_retry_after_success",
			run: func(t *testing.T, h *obsTestHarness) {
				obsTestExpectOpen(t, h, 1, obsTestBegin1)
				call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
				obsTestMustDo(t, h, call)
				if _, err := h.obs.Seal(1, 1); err != nil {
					t.Fatal("initial seal failed")
				}
				obsTestAssertCounts(t, h, Counts{Payment: 1})
				if _, err := h.obs.Seal(1, 2); err == nil {
					t.Fatal("different seal count was accepted")
				}
			},
		},
		{
			name: "call_after_seal",
			run: func(t *testing.T, h *obsTestHarness) {
				obsTestExpectOpen(t, h, 1, obsTestBegin1)
				obsTestSealZero(t, h, 1)
				obsTestAssertCounts(t, h, Counts{})
				call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
				obsTestRejectFrame(t, h, call)
			},
		},
		{
			name: "reach_after_seal",
			run: func(t *testing.T, h *obsTestHarness) {
				obsTestExpectOpen(t, h, 1, obsTestBegin1)
				obsTestSealZero(t, h, 1)
				obsTestAssertCounts(t, h, Counts{})
				reach := obsTestReachFrame(1, obsTestBegin1, 1, testcontrol.PointRegistered, obsTestOrderA)
				obsTestRejectFrame(t, h, reach)
			},
		},
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			h := obsTestStart(t, obsTestBarrier)
			tc.run(t, h)
			obsTestAssertUncertain(t, h)
		})
	}
}

func TestObserverMalformedFramesPolluteRun(t *testing.T) {
	rawBegin := obsTestRawBegin(obsTestBegin1)
	invalidUTF8 := strings.Replace(rawBegin, obsTestRunID,
		obsTestRunID[:31]+string([]byte{0xff}), 1)
	oversized := "{" + strings.Repeat(" ", testcontrol.MaxFrameBytes) +
		strings.TrimPrefix(rawBegin, "{")
	duplicateKey := `{"v":1,` + strings.TrimPrefix(rawBegin, "{")
	missingEventID := strings.Replace(rawBegin, `,"event_id":"`+obsTestBegin1+`"`, "", 1)
	generationZero := strings.Replace(rawBegin, `"generation":1`, `"generation":0`, 1)
	zeroSeqBegin := strings.TrimSuffix(rawBegin, "}") + `,"seq":0}`

	cases := []struct {
		name    string
		payload string
	}{
		{name: "invalid_json", payload: `{"v":1,"type":"begin"`},
		{name: "duplicate_key", payload: duplicateKey},
		{name: "invalid_utf8", payload: invalidUTF8},
		{name: "oversized", payload: oversized},
		{name: "missing_event_id", payload: missingEventID},
		{name: "generation_zero", payload: generationZero},
		{name: "explicit_zero_seq_begin", payload: zeroSeqBegin},
		{name: "noncanonical_call_seq", payload: obsTestRawCall(obsTestBegin1+":01", 1, "PAYMENT", obsTestOrderA)},
		{name: "event_seq_mismatch", payload: obsTestRawCall(obsTestBegin1+":2", 1, "PAYMENT", obsTestOrderA)},
		{name: "invalid_order_id", payload: obsTestRawCall(obsTestBegin1+":1", 1, "PAYMENT", "not-a-uuid")},
		{name: "ack_for_fault", payload: `{"v":1,"type":"ack","run_id":"` + obsTestRunID +
			`","observer_epoch":"` + obsTestEpoch +
			`","generation":1,"event_id":"` + obsTestBegin1 + `","for":"fault"}`},
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			h := obsTestStart(t, obsTestBarrier)
			obsTestExpectOpen(t, h, 1, obsTestBegin1)
			obsTestSealZero(t, h, 1)
			obsTestAssertCounts(t, h, Counts{})

			raw := obsTestRejectMalformedRaw(t, h, []byte(tc.payload+"\n"))
			if raw != nil && bytes.Contains(raw, []byte(obsTestCanary)) {
				t.Fatal("observer echoed malformed input")
			}
			obsTestAssertUncertain(t, h)
		})
	}
}

func TestObserverMalformedFrameDoesNotEchoInput(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)
	obsTestSealZero(t, h, 1)

	payload := strings.TrimSuffix(obsTestRawBegin(obsTestBegin1), "}") +
		`,"note":"` + obsTestCanary + `"}` + "\n"
	raw := obsTestRejectMalformedRaw(t, h, []byte(payload))
	if raw != nil && bytes.Contains(raw, []byte(obsTestCanary)) {
		t.Fatal("observer echoed malformed input")
	}
	obsTestAssertUncertain(t, h)
}

func TestObserverStartContextIsStartupOnly(t *testing.T) {
	t.Run("canceled_input", func(t *testing.T) {
		dir := obsTestNewDir(t)
		cfg := obsTestConfigWithSocket(filepath.Join(dir, "s"))
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		obs, err := StartObserver(ctx, cfg)
		if err == nil {
			closeCtx, closeCancel := context.WithTimeout(context.Background(), obsTestBudget)
			defer closeCancel()
			if obs != nil {
				_ = obs.Close(closeCtx)
			}
			t.Fatal("start accepted a canceled context")
		}
	})

	t.Run("expired_input", func(t *testing.T) {
		dir := obsTestNewDir(t)
		cfg := obsTestConfigWithSocket(filepath.Join(dir, "s"))
		ctx, cancel := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
		defer cancel()
		obs, err := StartObserver(ctx, cfg)
		if err == nil {
			closeCtx, closeCancel := context.WithTimeout(context.Background(), obsTestBudget)
			defer closeCancel()
			if obs != nil {
				_ = obs.Close(closeCtx)
			}
			t.Fatal("start accepted an expired context")
		}
	})

	t.Run("cancel_after_success", func(t *testing.T) {
		dir := obsTestNewDir(t)
		socket := filepath.Join(dir, "s")
		cfg := obsTestConfigWithSocket(socket)
		ctx, cancel := context.WithCancel(context.Background())
		obs, err := StartObserver(ctx, cfg)
		if err != nil {
			cancel()
			t.Fatal("start observer failed")
		}
		if obs == nil {
			cancel()
			t.Fatal("start observer returned a nil observer")
		}
		cancel()

		h := &obsTestHarness{t: t, obs: obs, socket: socket, dir: dir}
		t.Cleanup(h.cleanup)

		obsTestExpectOpen(t, h, 1, obsTestBegin1)
		obsTestSealZero(t, h, 1)
		obsTestAssertCounts(t, h, Counts{})
	})
}

func TestObserverStartValidation(t *testing.T) {
	t.Run("empty_socket", func(t *testing.T) {
		cfg := obsTestConfigWithSocket("")
		obsTestStartExpectError(t, cfg)
	})

	t.Run("relative_socket", func(t *testing.T) {
		cfg := obsTestConfigWithSocket("relative.sock")
		obsTestStartExpectError(t, cfg)
	})

	t.Run("missing_parent", func(t *testing.T) {
		dir := obsTestNewDir(t)
		cfg := obsTestConfigWithSocket(filepath.Join(dir, "missing", "s"))
		obsTestStartExpectError(t, cfg)
	})

	t.Run("wrong_directory_mode", func(t *testing.T) {
		dir := obsTestNewDir(t)
		if err := os.Chmod(dir, 0o755); err != nil {
			t.Fatal("set unsafe directory mode fixture")
		}
		cfg := obsTestConfigWithSocket(filepath.Join(dir, "s"))
		obsTestStartExpectError(t, cfg)

		info, err := os.Stat(dir)
		if err != nil {
			t.Fatal("stat caller directory failed")
		}
		if info.Mode().Perm() != 0o755 {
			t.Fatal("start observer changed the caller directory mode")
		}
	})

	t.Run("symlink_parent", func(t *testing.T) {
		parent := obsTestNewDir(t)
		realDir := filepath.Join(parent, "real")
		if err := os.Mkdir(realDir, 0o700); err != nil {
			t.Fatal("create real socket directory fixture")
		}
		link := filepath.Join(parent, "link")
		if err := os.Symlink(realDir, link); err != nil {
			t.Fatal("create symlink fixture")
		}
		cfg := obsTestConfigWithSocket(filepath.Join(link, "s"))
		obsTestStartExpectError(t, cfg)
	})

	t.Run("existing_file", func(t *testing.T) {
		dir := obsTestNewDir(t)
		socket := filepath.Join(dir, "s")
		if err := os.WriteFile(socket, []byte("foreign"), 0o600); err != nil {
			t.Fatal("create existing target fixture")
		}
		cfg := obsTestConfigWithSocket(socket)
		obsTestStartExpectError(t, cfg)

		data, err := os.ReadFile(socket)
		if err != nil {
			t.Fatal("existing target was removed")
		}
		if string(data) != "foreign" {
			t.Fatal("existing target was overwritten")
		}
	})

	t.Run("existing_socket", func(t *testing.T) {
		dir := obsTestNewDir(t)
		socket := filepath.Join(dir, "s")
		listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: socket, Net: "unix"})
		if err != nil {
			t.Fatal("create existing socket fixture")
		}
		listener.SetUnlinkOnClose(false)
		if err := listener.Close(); err != nil {
			t.Fatal("close existing socket fixture")
		}
		info, err := os.Lstat(socket)
		if err != nil {
			t.Fatal("stat existing socket fixture failed")
		}
		if info.Mode()&os.ModeSocket == 0 {
			t.Fatal("existing socket fixture is not a socket")
		}
		cfg := obsTestConfigWithSocket(socket)
		obsTestStartExpectError(t, cfg)
	})

	t.Run("socket_is_directory", func(t *testing.T) {
		dir := obsTestNewDir(t)
		socket := filepath.Join(dir, "s")
		if err := os.Mkdir(socket, 0o700); err != nil {
			t.Fatal("create socket directory fixture")
		}
		cfg := obsTestConfigWithSocket(socket)
		obsTestStartExpectError(t, cfg)
	})

	t.Run("negative_barrier_timeout", func(t *testing.T) {
		dir := obsTestNewDir(t)
		cfg := obsTestConfigWithSocket(filepath.Join(dir, "s"))
		cfg.BarrierTimeout = -time.Second
		obsTestStartExpectError(t, cfg)
	})

	t.Run("zero_barrier_timeout", func(t *testing.T) {
		h := obsTestStart(t, 0)
		if err := h.close(); err != nil {
			t.Fatal("close observer failed")
		}
		if _, err := os.Lstat(h.socket); !errors.Is(err, os.ErrNotExist) {
			t.Fatal("observer socket was not removed")
		}
	})
}

func TestObserverSocketPermissionsAndCleanup(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)

	info, err := os.Lstat(h.socket)
	if err != nil {
		t.Fatal("stat observer socket failed")
	}
	if info.Mode()&os.ModeSocket == 0 {
		t.Fatal("observer target is not a socket")
	}
	if info.Mode().Perm() != 0o600 {
		t.Fatal("observer socket mode is not 0600")
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		t.Fatal("observer socket stat type is unavailable")
	}
	if stat.Uid != uint32(os.Getuid()) {
		t.Fatal("observer socket owner mismatch")
	}

	dirInfo, err := os.Lstat(h.dir)
	if err != nil {
		t.Fatal("stat observer directory failed")
	}
	if dirInfo.Mode()&os.ModeSymlink != 0 {
		t.Fatal("observer directory is a symlink")
	}
	if dirInfo.Mode().Perm() != 0o700 {
		t.Fatal("observer directory mode changed")
	}

	foreign := filepath.Join(h.dir, "foreign")
	if err := os.WriteFile(foreign, []byte("keep"), 0o600); err != nil {
		t.Fatal("create foreign file fixture")
	}

	if err := h.close(); err != nil {
		t.Fatal("close observer failed")
	}
	if _, err := os.Lstat(h.socket); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("observer socket was not removed")
	}
	data, err := os.ReadFile(foreign)
	if err != nil {
		t.Fatal("observer removed a foreign file")
	}
	if string(data) != "keep" {
		t.Fatal("observer changed a foreign file")
	}
	if _, err := os.Lstat(h.dir); err != nil {
		t.Fatal("observer removed the caller directory")
	}
}

func TestObserverClosePreservesReplacedSocket(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)

	moved := h.socket + ".moved"
	if err := os.Rename(h.socket, moved); err != nil {
		t.Fatal("move observer socket fixture failed")
	}
	if err := os.WriteFile(h.socket, []byte("foreign"), 0o600); err != nil {
		t.Fatal("create replacement socket path fixture")
	}

	_ = h.close()

	data, err := os.ReadFile(h.socket)
	if err != nil {
		t.Fatal("observer removed a replaced socket path")
	}
	if string(data) != "foreign" {
		t.Fatal("observer changed a replaced socket path")
	}
}

func TestObserverCloseDoesNotSeal(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)
	call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
	resp := obsTestMustDo(t, h, call)
	obsTestCheckAck(t, call, resp)

	if err := h.close(); err != nil {
		t.Fatal("close observer failed")
	}
	obsTestAssertUncertain(t, h)
}

func TestObserverCloseInterruptsOwnedConnections(t *testing.T) {
	t.Run("partial_request", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)

		ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
		defer cancel()
		var dialer net.Dialer
		conn, err := dialer.DialContext(ctx, "unix", h.socket)
		if err != nil {
			t.Fatal("dial observer failed")
		}
		defer conn.Close()

		partial := []byte(`{"v":1,"type":"begin"`)
		if _, err := conn.Write(partial); err != nil {
			t.Fatal("write partial request failed")
		}

		if err := h.close(); err != nil {
			t.Fatal("close observer failed")
		}

		if err := conn.SetReadDeadline(time.Now().Add(obsTestShort)); err != nil {
			t.Fatal("set partial read deadline failed")
		}
		raw, readErr := obsTestReadResponse(conn)
		if readErr != nil {
			if obsTestIsTimeout(readErr) || errors.Is(readErr, context.Canceled) {
				t.Fatal("close did not interrupt the partial request")
			}
			if !obsTestExplicitPeerFailure(readErr) {
				t.Fatal("partial request ended with an unexpected error")
			}
		} else if len(raw) > 0 {
			resp, decodeErr := testcontrol.DecodeFrame(raw)
			if decodeErr != nil {
				t.Fatal("partial request rejection was not a protocol frame")
			}
			obsTestCheckFault(t, resp)
		}
		obsTestAssertUncertain(t, h)
	})

	t.Run("pending_barrier", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)

		barrier, err := h.obs.Arm(1, testcontrol.PointRegistered, obsTestOrderA)
		if err != nil {
			t.Fatal("arm barrier failed")
		}
		reach := obsTestReachFrame(1, obsTestBegin1, 1, testcontrol.PointRegistered, obsTestOrderA)
		ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
		defer cancel()
		ch := make(chan obsTestResult, 1)
		go func() {
			resp, err := obsTestSend(ctx, h.socket, reach)
			ch <- obsTestResult{frame: resp, err: err}
		}()

		if _, err := barrier.Wait(ctx); err != nil {
			t.Fatal("barrier wait failed")
		}
		if err := h.close(); err != nil {
			t.Fatal("close observer failed")
		}
		res := obsTestAwait(t, ctx, ch)
		obsTestCheckPendingEnded(t, res)
		obsTestAssertUncertain(t, h)
		if _, err := h.obs.Seal(1, 1); err == nil {
			t.Fatal("closed run accepted a seal")
		}
	})
}

func TestObserverBarrierFlow(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)
	obsTestExpectOpen(t, h, 2, obsTestBegin2)

	barrier, err := h.obs.Arm(1, testcontrol.PointRegistered, obsTestOrderA)
	if err != nil {
		t.Fatal("arm barrier failed")
	}

	reach := obsTestReachFrame(1, obsTestBegin1, 1, testcontrol.PointRegistered, obsTestOrderA)
	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	ch := make(chan obsTestResult, 1)
	go func() {
		resp, err := obsTestSend(ctx, h.socket, reach)
		ch <- obsTestResult{frame: resp, err: err}
	}()

	receipt, err := barrier.Wait(ctx)
	if err != nil {
		t.Fatal("barrier wait failed")
	}
	if receipt.RunID != obsTestRunID || receipt.Epoch != obsTestEpoch ||
		receipt.Generation != 1 || receipt.LastSeq != 1 {
		t.Fatal("barrier receipt mismatch")
	}

	other := obsTestCallFrame(2, obsTestBegin2, 1, testcontrol.KindPayment, obsTestOrderB)
	resp := obsTestMustDo(t, h, other)
	obsTestCheckAck(t, other, resp)

	sameGeneration := obsTestCallFrame(1, obsTestBegin1, 2, testcontrol.KindPayment, obsTestOrderA)
	resp = obsTestMustDo(t, h, sameGeneration)
	obsTestCheckAck(t, sameGeneration, resp)

	if err := barrier.Release(testcontrol.ActionContinue); err != nil {
		t.Fatal("barrier release failed")
	}
	res := obsTestAwait(t, ctx, ch)
	if res.err != nil {
		t.Fatal("pending reach exchange failed")
	}
	obsTestCheckAck(t, reach, res.frame)
	if res.frame.Action != testcontrol.ActionContinue {
		t.Fatal("barrier action was not acknowledged")
	}
	obsTestRequireProtocolError(t, barrier.Release(testcontrol.ActionContinue))

	second := obsTestReachFrame(1, obsTestBegin1, 3, testcontrol.PointRegistered, obsTestOrderA)
	resp = obsTestMustDo(t, h, second)
	obsTestCheckAck(t, second, resp)
	if resp.Action != testcontrol.ActionContinue {
		t.Fatal("consumed barrier blocked a second independent reach")
	}

	if _, err := h.obs.Seal(1, 3); err != nil {
		t.Fatal("seal first generation after barrier failed")
	}
	if _, err := h.obs.Seal(2, 1); err != nil {
		t.Fatal("seal second generation after barrier failed")
	}
	obsTestAssertCounts(t, h, Counts{Payment: 2})
}

func TestObserverBarrierExactMatch(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)

	barrier, err := h.obs.Arm(1, testcontrol.PointRegistered, obsTestOrderA)
	if err != nil {
		t.Fatal("arm barrier failed")
	}

	differentPoint := obsTestReachFrame(1, obsTestBegin1, 1, testcontrol.PointSavingResult, obsTestOrderA)
	resp := obsTestMustDo(t, h, differentPoint)
	obsTestCheckAck(t, differentPoint, resp)
	if resp.Action != testcontrol.ActionContinue {
		t.Fatal("reach with a non-matching point was held")
	}

	differentOrder := obsTestReachFrame(1, obsTestBegin1, 2, testcontrol.PointRegistered, obsTestOrderB)
	resp = obsTestMustDo(t, h, differentOrder)
	obsTestCheckAck(t, differentOrder, resp)
	if resp.Action != testcontrol.ActionContinue {
		t.Fatal("reach with a non-matching order was held")
	}

	reach := obsTestReachFrame(1, obsTestBegin1, 3, testcontrol.PointRegistered, obsTestOrderA)
	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	ch := make(chan obsTestResult, 1)
	go func() {
		resp, err := obsTestSend(ctx, h.socket, reach)
		ch <- obsTestResult{frame: resp, err: err}
	}()

	receipt, err := barrier.Wait(ctx)
	if err != nil {
		t.Fatal("barrier wait failed")
	}
	if receipt.LastSeq != 3 {
		t.Fatal("barrier receipt last sequence mismatch")
	}
	if err := barrier.Release(testcontrol.ActionContinue); err != nil {
		t.Fatal("barrier release failed")
	}
	res := obsTestAwait(t, ctx, ch)
	if res.err != nil {
		t.Fatal("matching reach exchange failed")
	}
	obsTestCheckAck(t, reach, res.frame)
	if res.frame.Action != testcontrol.ActionContinue {
		t.Fatal("matching barrier action mismatch")
	}

	if _, err := h.obs.Seal(1, 3); err != nil {
		t.Fatal("seal after exact barrier match failed")
	}
	obsTestAssertCounts(t, h, Counts{})
}

func TestObserverBarrierSealRequiresNoPending(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)

	barrier, err := h.obs.Arm(1, testcontrol.PointRegistered, obsTestOrderA)
	if err != nil {
		t.Fatal("arm barrier failed")
	}
	reach := obsTestReachFrame(1, obsTestBegin1, 1, testcontrol.PointRegistered, obsTestOrderA)
	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	ch := make(chan obsTestResult, 1)
	go func() {
		resp, err := obsTestSend(ctx, h.socket, reach)
		ch <- obsTestResult{frame: resp, err: err}
	}()

	if _, err := barrier.Wait(ctx); err != nil {
		t.Fatal("barrier wait failed")
	}
	if _, err := h.obs.Seal(1, 1); err == nil {
		t.Fatal("seal with a pending barrier was accepted")
	}

	res := obsTestAwait(t, ctx, ch)
	obsTestCheckPendingEnded(t, res)
	obsTestAssertUncertain(t, h)

	if _, err := h.obs.Seal(1, 1); err == nil {
		t.Fatal("polluted run closed after a failed seal")
	}
	if err := barrier.Release(testcontrol.ActionContinue); err == nil {
		t.Fatal("failed seal left the barrier releasable")
	}
}

func TestObserverBarrierReleaseRules(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)

	barrier, err := h.obs.Arm(1, testcontrol.PointRegistered, obsTestOrderA)
	if err != nil {
		t.Fatal("arm barrier failed")
	}
	obsTestRequireProtocolError(t, barrier.Release(testcontrol.ActionContinue))

	reach := obsTestReachFrame(1, obsTestBegin1, 1, testcontrol.PointRegistered, obsTestOrderA)
	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	ch := make(chan obsTestResult, 1)
	go func() {
		resp, err := obsTestSend(ctx, h.socket, reach)
		ch <- obsTestResult{frame: resp, err: err}
	}()

	if _, err := barrier.Wait(ctx); err != nil {
		t.Fatal("barrier wait failed")
	}
	obsTestRequireProtocolError(t, barrier.Release(testcontrol.Action("bogus")))
	if err := barrier.Release(testcontrol.ActionContinue); err != nil {
		t.Fatal("barrier release failed")
	}
	res := obsTestAwait(t, ctx, ch)
	if res.err != nil {
		t.Fatal("pending reach exchange failed")
	}
	obsTestCheckAck(t, reach, res.frame)
	if res.frame.Action != testcontrol.ActionContinue {
		t.Fatal("barrier action was not acknowledged")
	}
	obsTestRequireProtocolError(t, barrier.Release(testcontrol.ActionContinue))

	if _, err := h.obs.Seal(1, 1); err != nil {
		t.Fatal("seal after barrier release failed")
	}
	obsTestAssertCounts(t, h, Counts{})
}

func TestObserverBarrierWaitBound(t *testing.T) {
	t.Run("wait_timeout", func(t *testing.T) {
		h := obsTestStart(t, time.Second)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)

		barrier, err := h.obs.Arm(1, testcontrol.PointRegistered, obsTestOrderA)
		if err != nil {
			t.Fatal("arm barrier failed")
		}

		ctx, cancel := context.WithTimeout(context.Background(), obsTestShort)
		defer cancel()
		_, err = barrier.Wait(ctx)
		obsTestRequireIntegrityError(t, err)
		obsTestAssertUncertain(t, h)
		if _, err := h.obs.Seal(1, 0); err == nil {
			t.Fatal("polluted run closed after a barrier wait timeout")
		}
		if err := barrier.Release(testcontrol.ActionContinue); err == nil {
			t.Fatal("timed out barrier remained releasable")
		}
	})

	t.Run("wait_canceled", func(t *testing.T) {
		h := obsTestStart(t, time.Second)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)

		barrier, err := h.obs.Arm(1, testcontrol.PointRegistered, obsTestOrderA)
		if err != nil {
			t.Fatal("arm barrier failed")
		}

		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		_, err = barrier.Wait(ctx)
		obsTestRequireIntegrityError(t, err)
		obsTestAssertUncertain(t, h)
		if _, err := h.obs.Seal(1, 0); err == nil {
			t.Fatal("polluted run closed after a canceled barrier wait")
		}
	})
}

func TestObserverBarrierReleaseTimeoutPoisonsRun(t *testing.T) {
	h := obsTestStart(t, obsTestBarrierShort)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)

	barrier, err := h.obs.Arm(1, testcontrol.PointRegistered, obsTestOrderA)
	if err != nil {
		t.Fatal("arm barrier failed")
	}
	reach := obsTestReachFrame(1, obsTestBegin1, 1, testcontrol.PointRegistered, obsTestOrderA)
	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	ch := make(chan obsTestResult, 1)
	go func() {
		resp, err := obsTestSend(ctx, h.socket, reach)
		ch <- obsTestResult{frame: resp, err: err}
	}()

	if _, err := barrier.Wait(ctx); err != nil {
		t.Fatal("barrier wait failed")
	}

	res := obsTestAwait(t, ctx, ch)
	obsTestCheckPendingEnded(t, res)
	obsTestAssertUncertain(t, h)
	if _, err := h.obs.Seal(1, 1); err == nil {
		t.Fatal("polluted run closed after a release timeout")
	}
	if err := barrier.Release(testcontrol.ActionContinue); err == nil {
		t.Fatal("timed out barrier remained releasable")
	}
}

func TestObserverReplyHiddenAllowedPoints(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)

	points := []testcontrol.Point{
		testcontrol.PointRegistered,
		testcontrol.PointMockCommitted,
		testcontrol.PointResultCommitted,
	}
	for i, point := range points {
		seq := uint64(i + 1)
		receipt := obsTestBarrierRoundTrip(t, h, 1, seq, obsTestBegin1, point, obsTestOrderA, testcontrol.ActionReplyHidden)
		if receipt.LastSeq != seq {
			t.Fatal("reply_hidden barrier receipt mismatch")
		}
	}

	if _, err := h.obs.Seal(1, 3); err != nil {
		t.Fatal("seal after reply_hidden barriers failed")
	}
	obsTestAssertCounts(t, h, Counts{})
}

func TestObserverReplyHiddenRejectsSavingResult(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)

	barrier, err := h.obs.Arm(1, testcontrol.PointSavingResult, obsTestOrderA)
	if err != nil {
		t.Fatal("arm saving_result barrier failed")
	}
	reach := obsTestReachFrame(1, obsTestBegin1, 1, testcontrol.PointSavingResult, obsTestOrderA)
	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	ch := make(chan obsTestResult, 1)
	go func() {
		resp, err := obsTestSend(ctx, h.socket, reach)
		ch <- obsTestResult{frame: resp, err: err}
	}()

	if _, err := barrier.Wait(ctx); err != nil {
		t.Fatal("barrier wait failed")
	}
	err = barrier.Release(testcontrol.ActionReplyHidden)
	obsTestRequireProtocolError(t, err)
	if errors.Is(err, testcontrol.ErrReplyHidden) {
		t.Fatal("saving_result rejection was ErrReplyHidden")
	}

	if err := barrier.Release(testcontrol.ActionContinue); err != nil {
		t.Fatal("continue release after invalid reply_hidden failed")
	}
	res := obsTestAwait(t, ctx, ch)
	if res.err != nil {
		t.Fatal("pending reach exchange failed")
	}
	obsTestCheckAck(t, reach, res.frame)
	if res.frame.Action != testcontrol.ActionContinue {
		t.Fatal("continued reach action mismatch")
	}

	if _, err := h.obs.Seal(1, 1); err != nil {
		t.Fatal("seal after rejected reply_hidden failed")
	}
	obsTestAssertCounts(t, h, Counts{})
}

func TestObserverArmValidation(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)

	_, err := h.obs.Arm(1, testcontrol.PointRegistered, obsTestOrderA)
	obsTestRequireProtocolError(t, err)

	if err := h.obs.Expect(1); err != nil {
		t.Fatal("expect generation failed")
	}
	begin := obsTestBeginFrame(1, obsTestBegin1)
	resp := obsTestMustDo(t, h, begin)
	obsTestCheckAck(t, begin, resp)

	_, err = h.obs.Arm(1, testcontrol.Point("BOGUS"), obsTestOrderA)
	obsTestRequireProtocolError(t, err)
	_, err = h.obs.Arm(1, testcontrol.PointRegistered, "not-a-uuid")
	obsTestRequireProtocolError(t, err)

	barrier, err := h.obs.Arm(1, testcontrol.PointRegistered, obsTestOrderA)
	if err != nil {
		t.Fatal("arm barrier failed")
	}
	_, err = h.obs.Arm(1, testcontrol.PointRegistered, obsTestOrderA)
	obsTestRequireProtocolError(t, err)

	reach := obsTestReachFrame(1, obsTestBegin1, 1, testcontrol.PointRegistered, obsTestOrderA)
	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	ch := make(chan obsTestResult, 1)
	go func() {
		resp, err := obsTestSend(ctx, h.socket, reach)
		ch <- obsTestResult{frame: resp, err: err}
	}()
	if _, err := barrier.Wait(ctx); err != nil {
		t.Fatal("barrier wait failed")
	}
	if err := barrier.Release(testcontrol.ActionContinue); err != nil {
		t.Fatal("barrier release failed")
	}
	res := obsTestAwait(t, ctx, ch)
	if res.err != nil {
		t.Fatal("barrier reach exchange failed")
	}
	obsTestCheckAck(t, reach, res.frame)
	if res.frame.Action != testcontrol.ActionContinue {
		t.Fatal("barrier action mismatch")
	}

	if _, err := h.obs.Seal(1, 1); err != nil {
		t.Fatal("seal after arm validation failed")
	}
	obsTestAssertCounts(t, h, Counts{})

	_, err = h.obs.Arm(1, testcontrol.PointRegistered, obsTestOrderA)
	obsTestRequireProtocolError(t, err)
}

func TestObserverBarrierRetransmissionSharesResult(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)

	barrier, err := h.obs.Arm(1, testcontrol.PointRegistered, obsTestOrderA)
	if err != nil {
		t.Fatal("arm barrier failed")
	}
	reach := obsTestReachFrame(1, obsTestBegin1, 1, testcontrol.PointRegistered, obsTestOrderA)
	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()

	first := make(chan obsTestResult, 1)
	second := make(chan obsTestResult, 1)
	go func() {
		resp, err := obsTestSend(ctx, h.socket, reach)
		first <- obsTestResult{frame: resp, err: err}
	}()
	go func() {
		resp, err := obsTestSend(ctx, h.socket, reach)
		second <- obsTestResult{frame: resp, err: err}
	}()

	if _, err := barrier.Wait(ctx); err != nil {
		t.Fatal("barrier wait failed")
	}
	if err := barrier.Release(testcontrol.ActionReplyHidden); err != nil {
		t.Fatal("barrier release failed")
	}

	for _, ch := range []<-chan obsTestResult{first, second} {
		res := obsTestAwait(t, ctx, ch)
		if res.err != nil {
			t.Fatal("retransmitted barrier reach exchange failed")
		}
		obsTestCheckAck(t, reach, res.frame)
		if res.frame.Action != testcontrol.ActionReplyHidden {
			t.Fatal("retransmitted barrier action was not shared")
		}
	}

	if _, err := h.obs.Seal(1, 1); err != nil {
		t.Fatal("seal after retransmitted barrier failed")
	}
	obsTestAssertCounts(t, h, Counts{})
}
