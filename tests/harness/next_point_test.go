package harness

import (
	"context"
	"errors"
	"net"
	"os"
	"strconv"
	"testing"
	"time"

	"payment-hub/internal/testcontrol"
)

// TestNextCommittedPointBarrier freezes the parent-only next committed point
// seam. It never supplies an order ID or event at arm time; the first real
// server-validated reach binds the actual identity.

const nextPointChildMarker = "PAYMENT_HUB_TEST_NEXT_POINT_CHILD"

// TestNextCommittedPointChildHelper is the real child process for the
// REGISTERED positive case. It dials the private UDS itself and blocks at the
// held reach; the parent kills this exact process.
func TestNextCommittedPointChildHelper(t *testing.T) {
	if os.Getenv(nextPointChildMarker) != "1" {
		return
	}
	socket := os.Getenv("PAYMENT_HUB_TEST_CONTROL_SOCKET")
	runID := os.Getenv("PAYMENT_HUB_TEST_RUN_ID")
	epoch := os.Getenv("PAYMENT_HUB_TEST_OBSERVER_EPOCH")
	generationText := os.Getenv("PAYMENT_HUB_TEST_GENERATION")
	orderID := os.Getenv("PAYMENT_HUB_TEST_CHILD_ORDER")
	beginID := os.Getenv("PAYMENT_HUB_TEST_CHILD_BEGIN")
	pointText := os.Getenv("PAYMENT_HUB_TEST_CHILD_POINT")
	generation, err := strconv.ParseUint(generationText, 10, 64)
	if err != nil || generation == 0 || socket == "" || runID == "" ||
		epoch == "" || orderID == "" || beginID == "" {
		os.Exit(2)
	}
	point := testcontrol.Point(pointText)
	if point != testcontrol.PointRegistered && point != testcontrol.PointMockCommitted {
		os.Exit(2)
	}
	begin := testcontrol.Frame{
		Version:    testcontrol.Version2,
		Type:       testcontrol.TypeBegin,
		RunID:      runID,
		Epoch:      epoch,
		Generation: generation,
		EventID:    beginID,
		Capability: testcontrol.CapabilityQueryControl,
	}
	if _, err := controlledChildExchange(socket, begin); err != nil {
		os.Exit(2)
	}
	call := testcontrol.Frame{
		Version:    testcontrol.Version2,
		Type:       testcontrol.TypeCall,
		RunID:      runID,
		Epoch:      epoch,
		Generation: generation,
		Seq:        1,
		EventID:    beginID + ":1",
		Kind:       testcontrol.KindQuery,
		OrderID:    orderID,
	}
	if _, err := controlledChildExchange(socket, call); err != nil {
		os.Exit(2)
	}
	reach := testcontrol.Frame{
		Version:    testcontrol.Version2,
		Type:       testcontrol.TypeReach,
		RunID:      runID,
		Epoch:      epoch,
		Generation: generation,
		Seq:        2,
		EventID:    beginID + ":2",
		Point:      point,
		OrderID:    orderID,
	}
	if _, err := controlledChildExchange(socket, reach); err == nil {
		os.Exit(2)
	}
	os.Exit(2)
}

func nextPointChildProcess(t *testing.T, socket string, generation uint64, orderID, beginID string, point testcontrol.Point) *Process {
	t.Helper()
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifactsAt(t, t020TempDir(t), source, obsTestRunID)
	env := []string{
		nextPointChildMarker + "=1",
		"PAYMENT_HUB_TEST_CONTROL_SOCKET=" + socket,
		"PAYMENT_HUB_TEST_RUN_ID=" + obsTestRunID,
		"PAYMENT_HUB_TEST_OBSERVER_EPOCH=" + obsTestEpoch,
		"PAYMENT_HUB_TEST_GENERATION=" + queryControlledUint(generation),
		"PAYMENT_HUB_TEST_CHILD_ORDER=" + orderID,
		"PAYMENT_HUB_TEST_CHILD_BEGIN=" + beginID,
		"PAYMENT_HUB_TEST_CHILD_POINT=" + string(point),
		"TMPDIR=/tmp/opencode",
		"TZ=UTC",
		"GORACE=atexit_sleep_ms=0",
	}
	lifetime, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	process, err := StartProcess(lifetime, ProcessConfig{
		Path:       t020HelperPath(t),
		Dir:        t020TempDir(t),
		Args:       []string{"-test.run=^TestNextCommittedPointChildHelper$"},
		Env:        env,
		Generation: generation,
		Grace:      time.Second,
		Artifacts:  artifacts,
	})
	if err != nil {
		cancel()
		t.Fatal("start next committed point child failed")
	}
	t.Cleanup(func() {
		cancel()
		stopCtx, stopCancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer stopCancel()
		_, _ = process.Stop(stopCtx)
	})
	return process
}

func nextPointSendReach(ctx context.Context, h *obsTestHarness, beginID string, seq uint64, point testcontrol.Point, orderID string) <-chan obsTestResult {
	frame := obsTestReachFrame(1, beginID, seq, point, orderID)
	frame.Version = testcontrol.Version2
	return queryObsSendAsync(ctx, h.socket, frame)
}

func TestNextCommittedPointBarrier(t *testing.T) {
	t.Run("registered_real_child_held_then_termination_and_gen2", func(t *testing.T) {
		h := queryObsStart(t, testcontrol.Version2, obsTestBarrier, 1, 2)
		barrier, err := h.obs.ArmNextCommittedPoint(1, testcontrol.PointRegistered)
		if err != nil {
			t.Fatal("arm next registered point failed")
		}
		if _, ok := barrier.Binding(); ok {
			t.Fatal("next barrier reported a binding before a real reach")
		}
		process := nextPointChildProcess(t, h.socket, 1, obsTestOrderA, obsTestBegin1, testcontrol.PointRegistered)
		ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
		defer cancel()
		if _, err := barrier.Wait(ctx); err != nil {
			t.Fatal("next registered barrier wait failed")
		}
		binding, ok := barrier.Binding()
		if !ok || binding.Generation != 1 || binding.Point != testcontrol.PointRegistered ||
			binding.OrderID != obsTestOrderA || binding.Seq != 2 ||
			binding.EventID != obsTestEventID(obsTestBegin1, 2) {
			t.Fatal("next registered binding mismatch")
		}
		select {
		case <-process.terminal:
			t.Fatal("child returned before the held registered reach was terminated")
		default:
		}
		intent, err := h.obs.PrepareControlledTermination(barrier, process)
		if err != nil {
			t.Fatal("prepare next registered termination failed")
		}
		if intent.peerPID != process.cmd.Process.Pid {
			t.Fatal("next registered termination did not bind the real child peer")
		}
		stopCtx, stopCancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer stopCancel()
		_, _ = process.Stop(stopCtx)
		proof, err := process.ControlledTerminationProof(stopCtx)
		if err != nil {
			t.Fatal("next registered terminal proof failed")
		}
		result, err := intent.Terminate(proof)
		if err != nil {
			t.Fatal("next registered termination failed")
		}
		if !result.ControlledTruncated || !result.NoReceipt || result.ExactCounts ||
			result.ObservedPrefix != (Counts{Query: 1}) {
			t.Fatal("next registered truncation result mismatch")
		}
		if _, _, ok := h.obs.SealedGeneration(1); ok {
			t.Fatal("truncated next registered generation reported a receipt")
		}

		queryObsBeginOpen(t, h, 2, obsTestBegin2)
		call := queryObsCall(2, obsTestBegin2, 1, testcontrol.KindQuery, obsTestOrderB)
		obsTestMustDo(t, h, call)
		obsTestMustDo(t, h, queryObsApplied(2, obsTestBegin2, 2, 1, obsTestOrderB, testcontrol.QueryOutcomeFound))
		if _, err := h.obs.Seal(2, 2); err != nil {
			t.Fatal("gen2 seal after next point termination failed")
		}
		if _, counts, ok := h.obs.SealedGeneration(2); !ok || counts != (Counts{Query: 1}) {
			t.Fatal("gen2 counts after next point termination mismatch")
		}
	})

	t.Run("mock_committed_normal_without_directive", func(t *testing.T) {
		h := queryObsStart(t, testcontrol.Version2, obsTestBarrier, 1)
		barrier, err := h.obs.ArmNextCommittedPoint(1, testcontrol.PointMockCommitted)
		if err != nil {
			t.Fatal("arm next mock committed point failed")
		}
		queryObsBeginOpen(t, h, 1, obsTestBegin1)
		call := queryObsCall(1, obsTestBegin1, 1, testcontrol.KindQuery, obsTestOrderA)
		obsTestMustDo(t, h, call)
		ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
		defer cancel()
		reach := nextPointSendReach(ctx, h, obsTestBegin1, 2, testcontrol.PointMockCommitted, obsTestOrderA)
		if _, err := barrier.Wait(ctx); err != nil {
			t.Fatal("next mock committed barrier wait failed")
		}
		binding, ok := barrier.Binding()
		if !ok || binding.Point != testcontrol.PointMockCommitted ||
			binding.OrderID != obsTestOrderA || binding.Seq != 2 {
			t.Fatal("next mock committed binding mismatch")
		}
		if err := barrier.Release(testcontrol.ActionContinue); err != nil {
			t.Fatal("next mock committed release failed")
		}
		result := obsTestAwait(t, ctx, reach)
		if result.err != nil {
			t.Fatal("next mock committed reach failed")
		}
		queryObsCheckAck(t, obsTestReachFrame(1, obsTestBegin1, 2, testcontrol.PointMockCommitted, obsTestOrderA), result.frame)
		if result.frame.QueryDirective != nil {
			t.Fatal("normal mock committed reach gained a query directive")
		}
	})

	t.Run("transport_retry_keeps_first_connection_and_binding", func(t *testing.T) {
		h := queryObsStart(t, testcontrol.Version2, obsTestBarrier, 1)
		barrier, err := h.obs.ArmNextCommittedPoint(1, testcontrol.PointRegistered)
		if err != nil {
			t.Fatal("arm next registered point failed")
		}
		retryArrived := make(chan struct{}, 1)
		h.obs.setTestBarrierBindHook(func(_ *Barrier, _ net.Conn, duplicate bool) {
			if !duplicate {
				return
			}
			select {
			case retryArrived <- struct{}{}:
			default:
			}
		})
		queryObsBeginOpen(t, h, 1, obsTestBegin1)
		call := queryObsCall(1, obsTestBegin1, 1, testcontrol.KindQuery, obsTestOrderA)
		obsTestMustDo(t, h, call)
		ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
		defer cancel()
		first := nextPointSendReach(ctx, h, obsTestBegin1, 2, testcontrol.PointRegistered, obsTestOrderA)
		if _, err := barrier.Wait(ctx); err != nil {
			t.Fatal("next registered barrier wait failed")
		}
		firstConn := barrier.conn
		firstBinding, ok := barrier.Binding()
		if !ok || firstConn == nil {
			t.Fatal("next registered first binding missing")
		}
		second := nextPointSendReach(ctx, h, obsTestBegin1, 2, testcontrol.PointRegistered, obsTestOrderA)
		select {
		case <-retryArrived:
		case <-ctx.Done():
			t.Fatal("second retry handler did not reach the barrier bind")
		}
		if barrier.conn != firstConn {
			t.Fatal("transport retry replaced the held connection")
		}
		secondBinding, ok := barrier.Binding()
		if !ok || secondBinding != firstBinding {
			t.Fatal("transport retry changed the actual binding")
		}
		if err := barrier.Release(testcontrol.ActionContinue); err != nil {
			t.Fatal("next registered release failed")
		}
		for _, ch := range []<-chan obsTestResult{first, second} {
			result := obsTestAwait(t, ctx, ch)
			if result.err != nil {
				t.Fatal("next registered retransmission exchange failed")
			}
			queryObsCheckAck(t, obsTestReachFrame(1, obsTestBegin1, 2, testcontrol.PointRegistered, obsTestOrderA), result.frame)
		}
	})

	t.Run("exact_and_next_arm_conflicts_fail_closed", func(t *testing.T) {
		h := queryObsStart(t, testcontrol.Version2, obsTestBarrier, 1)
		if _, err := h.obs.ArmNextCommittedPoint(1, testcontrol.PointRegistered); err != nil {
			t.Fatal("arm next registered point failed")
		}
		if _, err := h.obs.Arm(1, testcontrol.PointRegistered, obsTestOrderA); !errors.Is(err, testcontrol.ErrProtocol) {
			t.Fatal("exact arm accepted a conflicting next barrier")
		}

		h = queryObsStart(t, testcontrol.Version2, obsTestBarrier, 1)
		if _, err := h.obs.Arm(1, testcontrol.PointRegistered, obsTestOrderA); err != nil {
			t.Fatal("exact arm failed")
		}
		if _, err := h.obs.ArmNextCommittedPoint(1, testcontrol.PointRegistered); !errors.Is(err, testcontrol.ErrProtocol) {
			t.Fatal("next arm accepted a conflicting exact barrier")
		}
	})

	t.Run("duplicate_next_arm_rejected", func(t *testing.T) {
		h := queryObsStart(t, testcontrol.Version2, obsTestBarrier, 1)
		if _, err := h.obs.ArmNextCommittedPoint(1, testcontrol.PointRegistered); err != nil {
			t.Fatal("first next arm failed")
		}
		if _, err := h.obs.ArmNextCommittedPoint(1, testcontrol.PointRegistered); !errors.Is(err, testcontrol.ErrProtocol) {
			t.Fatal("duplicate next arm was accepted")
		}
	})

	t.Run("invalid_points_and_generations_rejected", func(t *testing.T) {
		h := queryObsStart(t, testcontrol.Version2, obsTestBarrier, 1)
		for _, point := range []testcontrol.Point{
			testcontrol.PointSavingResult,
			testcontrol.PointResultCommitted,
			testcontrol.PointQueryApplied,
			testcontrol.Point("other"),
		} {
			if _, err := h.obs.ArmNextCommittedPoint(1, point); !errors.Is(err, testcontrol.ErrProtocol) {
				t.Fatal("invalid next committed point was accepted")
			}
		}
		if _, err := h.obs.ArmNextCommittedPoint(2, testcontrol.PointRegistered); !errors.Is(err, testcontrol.ErrProtocol) {
			t.Fatal("cross-generation next arm was accepted")
		}
	})

	t.Run("sealed_timeout_closed_uncertain_rejected", func(t *testing.T) {
		h := queryObsStart(t, testcontrol.Version2, obsTestBarrier, 1)
		queryObsBeginOpen(t, h, 1, obsTestBegin1)
		if _, err := h.obs.Seal(1, 0); err != nil {
			t.Fatal("seal empty generation failed")
		}
		if _, err := h.obs.ArmNextCommittedPoint(1, testcontrol.PointRegistered); !errors.Is(err, testcontrol.ErrProtocol) {
			t.Fatal("sealed generation accepted a next barrier")
		}

		h = queryObsStart(t, testcontrol.Version2, obsTestBarrierShort, 1)
		barrier, err := h.obs.ArmNextCommittedPoint(1, testcontrol.PointRegistered)
		if err != nil {
			t.Fatal("arm next registered point failed")
		}
		ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
		if _, err := barrier.Wait(ctx); err == nil {
			cancel()
			t.Fatal("unreached next barrier did not time out")
		}
		cancel()
		if _, err := h.obs.ArmNextCommittedPoint(1, testcontrol.PointRegistered); !errors.Is(err, testcontrol.ErrUncertain) {
			t.Fatal("timed-out observer accepted another next barrier")
		}

		h = queryObsStart(t, testcontrol.Version2, obsTestBarrier, 1)
		closeCtx, closeCancel := context.WithTimeout(context.Background(), obsTestBudget)
		if err := h.obs.Close(closeCtx); err != nil {
			closeCancel()
			t.Fatal("close observer failed")
		}
		closeCancel()
		if _, err := h.obs.ArmNextCommittedPoint(1, testcontrol.PointRegistered); !errors.Is(err, testcontrol.ErrControlFault) {
			t.Fatal("closed observer accepted a next barrier")
		}

		h = queryObsStart(t, testcontrol.Version2, obsTestBarrier, 1)
		bad := queryObsCall(1, obsTestBegin1, 1, testcontrol.KindQuery, obsTestOrderA)
		bad.RunID = obsTestWrongRunID
		if _, err := obsTestSend(context.Background(), h.socket, bad); err != nil {
			t.Fatal("uncertainty trigger exchange failed")
		}
		if _, err := h.obs.ArmNextCommittedPoint(1, testcontrol.PointRegistered); !errors.Is(err, testcontrol.ErrUncertain) {
			t.Fatal("uncertain observer accepted a next barrier")
		}
	})
}
