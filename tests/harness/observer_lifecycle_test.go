package harness

import (
	"context"
	"errors"
	"net"
	"os"
	"testing"
	"time"

	"payment-hub/internal/testcontrol"
)

func TestObserverBarrierWaitUsesConfiguredBudget(t *testing.T) {
	h := obsTestStart(t, 250*time.Millisecond)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)

	barrier, err := h.obs.Arm(1, testcontrol.PointRegistered, obsTestOrderA)
	if err != nil {
		t.Fatal("arm barrier failed")
	}

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_, err = barrier.Wait(ctx)
	if err == nil {
		t.Fatal("barrier wait without a reach succeeded")
	}
	if ctx.Err() != nil {
		t.Fatal("barrier wait exceeded its configured budget")
	}
	if !errors.Is(err, testcontrol.ErrControlFault) && !errors.Is(err, testcontrol.ErrUncertain) {
		t.Fatal("barrier wait error is not a fixed integrity error")
	}

	obsTestAssertUncertain(t, h)
	if _, err := h.obs.Seal(1, 0); err == nil {
		t.Fatal("polluted run closed after barrier wait timeout")
	}
}

func TestObserverBarrierWaitCanceledInputAfterArrival(t *testing.T) {
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

	select {
	case <-barrier.arrivedCh:
	case <-ctx.Done():
		t.Fatal("barrier did not arrive within budget")
	}

	canceled, cancelWait := context.WithCancel(context.Background())
	cancelWait()
	_, err = barrier.Wait(canceled)
	if err == nil {
		t.Fatal("canceled barrier wait returned success")
	}
	if !errors.Is(err, testcontrol.ErrControlFault) && !errors.Is(err, testcontrol.ErrUncertain) {
		t.Fatal("canceled barrier wait error is not a fixed integrity error")
	}

	obsTestAssertUncertain(t, h)
	if _, err := h.obs.Seal(1, 1); err == nil {
		t.Fatal("polluted run closed after canceled barrier wait")
	}
	res := obsTestAwait(t, ctx, ch)
	obsTestCheckPendingEnded(t, res)
}

func TestObserverCloseLatchesIncompleteAcceptedConnection(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)
	obsTestSealZero(t, h, 1)
	obsTestAssertCounts(t, h, Counts{})

	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	var dialer net.Dialer
	partial, err := dialer.DialContext(ctx, "unix", h.socket)
	if err != nil {
		t.Fatal("dial partial observer connection failed")
	}
	defer partial.Close()
	if _, err := partial.Write([]byte(`{"v":1,"type":"begin"`)); err != nil {
		t.Fatal("write partial request failed")
	}

	seal := obsTestSealFrame(1, obsTestBegin1, 0)
	resp := obsTestMustDo(t, h, seal)
	obsTestCheckAck(t, seal, resp)

	if err := h.close(); err != nil {
		t.Fatal("close observer failed")
	}
	obsTestAssertUncertain(t, h)

	if err := partial.SetReadDeadline(time.Now().Add(obsTestShort)); err != nil {
		t.Fatal("set partial read deadline failed")
	}
	raw, readErr := obsTestReadResponse(partial)
	if readErr != nil {
		if obsTestIsTimeout(readErr) || errors.Is(readErr, context.Canceled) {
			t.Fatal("close did not interrupt the partial connection")
		}
		if !obsTestExplicitPeerFailure(readErr) {
			t.Fatal("partial connection ended with an unexpected error")
		}
	} else if len(raw) > 0 {
		frame, decodeErr := testcontrol.DecodeFrame(raw)
		if decodeErr != nil {
			t.Fatal("partial rejection was not a protocol frame")
		}
		obsTestCheckFault(t, frame)
	}
}

func TestObserverClosePreservesTrustedSealedCounts(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)
	call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
	resp := obsTestMustDo(t, h, call)
	obsTestCheckAck(t, call, resp)
	if _, err := h.obs.Seal(1, 1); err != nil {
		t.Fatal("seal trusted generation failed")
	}
	obsTestAssertCounts(t, h, Counts{Payment: 1})

	if err := h.close(); err != nil {
		t.Fatal("close observer failed")
	}
	obsTestAssertCounts(t, h, Counts{Payment: 1})
}

func TestObserverCloseRejectsReplacedSocketIdentity(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)

	moved := h.socket + ".moved"
	if err := os.Rename(h.socket, moved); err != nil {
		t.Fatal("move observer socket fixture failed")
	}
	if err := os.WriteFile(h.socket, []byte("foreign"), 0o600); err != nil {
		t.Fatal("create replacement socket path fixture")
	}

	err := h.close()
	if err == nil {
		t.Fatal("close accepted a replaced socket identity")
	}
	if !errors.Is(err, testcontrol.ErrControlFault) {
		t.Fatal("replaced socket close error is not a fixed control fault")
	}

	data, readErr := os.ReadFile(h.socket)
	if readErr != nil {
		t.Fatal("close removed a replaced socket path")
	}
	if string(data) != "foreign" {
		t.Fatal("close changed a replaced socket path")
	}
	if _, statErr := os.Lstat(moved); statErr != nil {
		t.Fatal("close removed the moved socket target")
	}
}

func TestObserverAcceptFailureLatchesAndStops(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)
	obsTestSealZero(t, h, 1)
	obsTestAssertCounts(t, h, Counts{})

	if err := h.obs.listener.Close(); err != nil {
		t.Fatal("close injected listener failed")
	}

	done := make(chan struct{})
	go func() {
		h.obs.wg.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("accept loop did not stop after listener failure")
	}

	obsTestAssertUncertain(t, h)
	if _, err := h.obs.Seal(1, 0); err == nil {
		t.Fatal("polluted run closed after accept failure")
	}
}
