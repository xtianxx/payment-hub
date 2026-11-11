package harness

import (
	"context"
	"io"
	"net"
	"os"
	"strconv"
	"testing"
	"time"

	"payment-hub/internal/testcontrol"
)

const controlledTerminationChildMarker = "PAYMENT_HUB_TEST_CONTROLLED_CHILD"

// TestControlledTerminationProof freezes the T058 two-stage helper: bind the
// exact held connection and real Process before the kill, accept only that
// process's real Wait proof, keep gen1 controlled-truncated, and let gen2 seal
// independently without clearing global uncertainty.

func queryControlledStart(t *testing.T, barrierTimeout time.Duration, generations ...uint64) *obsTestHarness {
	t.Helper()
	return queryObsStart(t, testcontrol.Version2, barrierTimeout, generations...)
}

// TestControlledTerminationChildHelper is the real child process used by the
// positive T058 test. The child itself dials the private observer socket and
// reaches the held point; no parent connection is substituted for it.
func TestControlledTerminationChildHelper(t *testing.T) {
	if os.Getenv(controlledTerminationChildMarker) != "1" {
		return
	}
	socket := os.Getenv("PAYMENT_HUB_TEST_CONTROL_SOCKET")
	runID := os.Getenv("PAYMENT_HUB_TEST_RUN_ID")
	epoch := os.Getenv("PAYMENT_HUB_TEST_OBSERVER_EPOCH")
	generationText := os.Getenv("PAYMENT_HUB_TEST_GENERATION")
	orderID := os.Getenv("PAYMENT_HUB_TEST_CHILD_ORDER")
	beginID := os.Getenv("PAYMENT_HUB_TEST_CHILD_BEGIN")
	generation, err := strconv.ParseUint(generationText, 10, 64)
	if err != nil || generation == 0 || socket == "" || runID == "" ||
		epoch == "" || orderID == "" || beginID == "" {
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
		Point:      testcontrol.PointRegistered,
		OrderID:    orderID,
	}
	if _, err := controlledChildExchange(socket, reach); err == nil {
		os.Exit(2)
	}
	os.Exit(2)
}

func controlledChildExchange(socket string, frame testcontrol.Frame) (testcontrol.Frame, error) {
	payload, err := testcontrol.EncodeFrame(frame)
	if err != nil {
		return testcontrol.Frame{}, err
	}
	conn, err := net.Dial("unix", socket)
	if err != nil {
		return testcontrol.Frame{}, err
	}
	defer conn.Close()
	if err := conn.SetDeadline(time.Now().Add(10 * time.Second)); err != nil {
		return testcontrol.Frame{}, err
	}
	written := 0
	for written < len(payload) {
		n, err := conn.Write(payload[written:])
		if err != nil || n <= 0 {
			return testcontrol.Frame{}, io.ErrShortWrite
		}
		written += n
	}
	unixConn, ok := conn.(*net.UnixConn)
	if !ok {
		return testcontrol.Frame{}, io.ErrUnexpectedEOF
	}
	if err := unixConn.CloseWrite(); err != nil {
		return testcontrol.Frame{}, err
	}
	raw, err := io.ReadAll(io.LimitReader(conn, testcontrol.MaxFrameBytes+1))
	if err != nil || len(raw) == 0 || len(raw) > testcontrol.MaxFrameBytes {
		return testcontrol.Frame{}, io.ErrUnexpectedEOF
	}
	return testcontrol.DecodeFrame(raw)
}

func queryControlledChildProcess(t *testing.T, socket string, generation uint64, orderID string) *Process {
	t.Helper()
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifactsAt(t, t020TempDir(t), source, obsTestRunID)
	env := []string{
		controlledTerminationChildMarker + "=1",
		"PAYMENT_HUB_TEST_CONTROL_SOCKET=" + socket,
		"PAYMENT_HUB_TEST_RUN_ID=" + obsTestRunID,
		"PAYMENT_HUB_TEST_OBSERVER_EPOCH=" + obsTestEpoch,
		"PAYMENT_HUB_TEST_GENERATION=" + queryControlledUint(generation),
		"PAYMENT_HUB_TEST_CHILD_ORDER=" + orderID,
		"PAYMENT_HUB_TEST_CHILD_BEGIN=" + obsTestBegin1,
		"TMPDIR=/tmp/opencode",
		"TZ=UTC",
		"GORACE=atexit_sleep_ms=0",
	}
	lifetime, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	process, err := StartProcess(lifetime, ProcessConfig{
		Path:       t020HelperPath(t),
		Dir:        t020TempDir(t),
		Args:       []string{"-test.run=^TestControlledTerminationChildHelper$"},
		Env:        env,
		Generation: generation,
		Grace:      time.Second,
		Artifacts:  artifacts,
	})
	if err != nil {
		cancel()
		t.Fatal("start controlled termination child failed")
	}
	t.Cleanup(func() {
		cancel()
		stopCtx, stopCancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer stopCancel()
		_, _ = process.Stop(stopCtx)
	})
	return process
}

func queryControlledProcess(t *testing.T, generation uint64, mode string) (*Process, *t020Held) {
	t.Helper()
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifactsAt(t, t020TempDir(t), source, obsTestRunID)
	held := t020HeldSocket(t)
	env := []string{
		"PAYMENT_HUB_TEST_RUN_ID=" + obsTestRunID,
		"PAYMENT_HUB_TEST_OBSERVER_EPOCH=" + obsTestEpoch,
		"PAYMENT_HUB_TEST_GENERATION=" + queryControlledUint(generation),
	}
	process, cancel := t020StartLifecycleProcess(t, artifacts, generation, mode, time.Second,
		append(env, t020HelperReadySocket+"="+held.path)...)
	t.Cleanup(cancel)
	held.awaitReady(t)
	t.Cleanup(held.releaseNow)
	return process, held
}

func queryControlledPrepareChild(t *testing.T, h *obsTestHarness, orderID string) (*ControlledTermination, *Process) {
	t.Helper()
	barrier, err := h.obs.Arm(1, testcontrol.PointRegistered, orderID)
	if err != nil {
		t.Fatal("arm controlled termination barrier failed")
	}
	process := queryControlledChildProcess(t, h.socket, 1, orderID)
	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	if _, err := barrier.Wait(ctx); err != nil {
		t.Fatal("controlled termination barrier wait failed")
	}
	intent, err := h.obs.PrepareControlledTermination(barrier, process)
	if err != nil {
		t.Fatal("prepare controlled termination failed")
	}
	return intent, process
}

func queryControlledUint(value uint64) string {
	if value == 0 {
		return "0"
	}
	digits := "0123456789"
	var buf [20]byte
	index := len(buf)
	for value > 0 {
		index--
		buf[index] = digits[value%10]
		value /= 10
	}
	return string(buf[index:])
}

func queryControlledHold(t *testing.T, h *obsTestHarness, beginID string, point testcontrol.Point, orderID string) (*Barrier, <-chan obsTestResult) {
	t.Helper()
	barrier, err := h.obs.Arm(1, point, orderID)
	if err != nil {
		t.Fatal("arm controlled termination barrier failed")
	}
	call := queryObsCall(1, beginID, 1, testcontrol.KindQuery, orderID)
	callAck := obsTestMustDo(t, h, call)
	queryObsCheckAck(t, call, callAck)
	reach := obsTestReachFrame(1, beginID, 2, point, orderID)
	reach.Version = testcontrol.Version2
	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	t.Cleanup(cancel)
	done := queryObsSendAsync(ctx, h.socket, reach)
	if _, err := barrier.Wait(ctx); err != nil {
		t.Fatal("controlled termination barrier wait failed")
	}
	return barrier, done
}

func TestControlledTerminationProof(t *testing.T) {
	t.Run("expected_truncation_keeps_gen2_independent", func(t *testing.T) {
		h := queryControlledStart(t, obsTestBarrier, 1, 2)
		barrier, err := h.obs.Arm(1, testcontrol.PointRegistered, obsTestOrderA)
		if err != nil {
			t.Fatal("arm controlled termination barrier failed")
		}
		process := queryControlledChildProcess(t, h.socket, 1, obsTestOrderA)
		ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
		defer cancel()
		if _, err := barrier.Wait(ctx); err != nil {
			t.Fatal("controlled termination barrier wait failed")
		}

		intent, err := h.obs.PrepareControlledTermination(barrier, process)
		if err != nil {
			t.Fatal("prepare controlled termination failed")
		}
		if intent.conn == nil || intent.peerPID != process.cmd.Process.Pid {
			t.Fatal("prepare did not bind the real child peer connection")
		}
		stopCtx, stopCancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer stopCancel()
		_, _ = process.Stop(stopCtx)
		proof, err := process.ControlledTerminationProof(stopCtx)
		if err != nil {
			t.Fatal("real process terminal proof failed")
		}
		result, err := intent.Terminate(proof)
		if err != nil {
			t.Fatal("terminate exact held connection failed")
		}
		if !result.ControlledTruncated || !result.NoReceipt || result.ExactCounts {
			t.Fatal("controlled truncation result claimed a receipt or exact counts")
		}
		if result.Binding.Generation != 1 || result.Binding.Point != testcontrol.PointRegistered ||
			result.Binding.OrderID != obsTestOrderA || result.Binding.Seq != 2 {
			t.Fatal("controlled truncation binding mismatch")
		}
		if result.ObservedPrefix != (Counts{Query: 1}) {
			t.Fatal("controlled truncation observed prefix mismatch")
		}
		if !result.Exit.Reaped || !result.Exit.GroupGone || !result.Exit.OutputComplete {
			t.Fatal("controlled truncation did not carry the real process terminal proof")
		}
		if _, _, ok := h.obs.SealedGeneration(1); ok {
			t.Fatal("truncated gen1 reported a sealed receipt")
		}

		queryObsBeginOpen(t, h, 2, obsTestBegin2)
		call := queryObsCall(2, obsTestBegin2, 1, testcontrol.KindQuery, obsTestOrderB)
		obsTestMustDo(t, h, call)
		applied := queryObsApplied(2, obsTestBegin2, 2, 1, obsTestOrderB, testcontrol.QueryOutcomeFound)
		obsTestMustDo(t, h, applied)
		if _, err := h.obs.Seal(2, 2); err != nil {
			t.Fatal("gen2 independent seal failed")
		}
		if _, counts, ok := h.obs.SealedGeneration(2); !ok || counts != (Counts{Query: 1}) {
			t.Fatal("gen2 independent counts mismatch")
		}
		closeCtx, closeCancel := context.WithTimeout(context.Background(), obsTestBudget)
		if err := h.obs.Close(closeCtx); err != nil {
			closeCancel()
			t.Fatal("close observer after controlled truncation failed")
		}
		closeCancel()
		final, ok := h.obs.FinalSnapshot()
		if !ok || final.Trusted {
			t.Fatal("global final snapshot was trusted after controlled truncation")
		}
		for _, receipt := range final.Receipts {
			if receipt.Generation == 1 {
				t.Fatal("truncated gen1 appeared in final receipts")
			}
		}
	})

	t.Run("child_connection_with_wrong_process_rejected", func(t *testing.T) {
		h := queryControlledStart(t, obsTestBarrier, 1)
		barrier, err := h.obs.Arm(1, testcontrol.PointRegistered, obsTestOrderA)
		if err != nil {
			t.Fatal("arm controlled termination barrier failed")
		}
		process := queryControlledChildProcess(t, h.socket, 1, obsTestOrderA)
		ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
		defer cancel()
		if _, err := barrier.Wait(ctx); err != nil {
			t.Fatal("controlled termination barrier wait failed")
		}
		other, _ := queryControlledProcess(t, 1, t020LifecycleHeldOK)
		if _, err := h.obs.PrepareControlledTermination(barrier, other); err == nil {
			t.Fatal("prepare accepted a different process for the child-owned connection")
		}
		intent, err := h.obs.PrepareControlledTermination(barrier, process)
		if err != nil {
			t.Fatal("prepare rejected the real child-owned connection")
		}
		stopCtx, stopCancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer stopCancel()
		_, _ = process.Stop(stopCtx)
		proof, err := process.ControlledTerminationProof(stopCtx)
		if err != nil {
			t.Fatal("real process terminal proof failed")
		}
		if _, err := intent.Terminate(proof); err != nil {
			t.Fatal("valid child proof was rejected after wrong-process attempt")
		}
	})

	t.Run("parent_connection_with_unrelated_process_rejected", func(t *testing.T) {
		h := queryControlledStart(t, obsTestBarrier, 1)
		queryObsBeginOpen(t, h, 1, obsTestBegin1)
		barrier, done := queryControlledHold(t, h, obsTestBegin1, testcontrol.PointRegistered, obsTestOrderA)
		process, _ := queryControlledProcess(t, 1, t020LifecycleHeldOK)
		if _, err := h.obs.PrepareControlledTermination(barrier, process); err == nil {
			t.Fatal("prepare accepted a caller connection owned by another process")
		}
		_ = barrier.Release(testcontrol.ActionContinue)
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), obsTestBudget)
		defer cleanupCancel()
		_ = obsTestAwait(t, cleanupCtx, done)
	})

	t.Run("duplicate_reach_on_new_connection_rejected", func(t *testing.T) {
		h := queryControlledStart(t, obsTestBarrier, 1)
		intent, process := queryControlledPrepareChild(t, h, obsTestOrderA)
		duplicate := obsTestReachFrame(1, obsTestBegin1, 2, testcontrol.PointRegistered, obsTestOrderA)
		duplicate.Version = testcontrol.Version2
		shortCtx, shortCancel := context.WithTimeout(context.Background(), 500*time.Millisecond)
		defer shortCancel()
		response, err := obsTestSend(shortCtx, h.socket, duplicate)
		if err != nil {
			t.Fatal("duplicate reach on a new connection did not fail closed promptly")
		}
		queryObsCheckFault(t, response)
		stopCtx, stopCancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer stopCancel()
		_, _ = process.Stop(stopCtx)
		proof, err := process.ControlledTerminationProof(stopCtx)
		if err != nil {
			t.Fatal("real process terminal proof failed")
		}
		if _, err := intent.Terminate(proof); err == nil {
			t.Fatal("duplicate reach replacement still allowed controlled termination")
		}
		if _, _, ok := h.obs.SealedGeneration(1); ok {
			t.Fatal("duplicate reach connection replacement produced a receipt")
		}
	})

	t.Run("zero_proof_rejected", func(t *testing.T) {
		h := queryControlledStart(t, obsTestBarrier, 1)
		intent, _ := queryControlledPrepareChild(t, h, obsTestOrderA)
		if _, err := intent.Terminate(ProcessTerminalProof{}); err == nil {
			t.Fatal("zero terminal proof was accepted")
		}
	})

	t.Run("other_process_and_wrong_identity_rejected", func(t *testing.T) {
		h := queryControlledStart(t, obsTestBarrier, 1)
		intent, process := queryControlledPrepareChild(t, h, obsTestOrderA)
		stopCtx, stopCancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer stopCancel()
		_, _ = process.Stop(stopCtx)
		proof, err := process.ControlledTerminationProof(stopCtx)
		if err != nil {
			t.Fatal("real process terminal proof failed")
		}
		other := proof
		other.process = &Process{}
		if _, err := intent.Terminate(other); err == nil {
			t.Fatal("proof from another process object was accepted")
		}
		wrongPID := proof
		wrongPID.pid++
		if _, err := intent.Terminate(wrongPID); err == nil {
			t.Fatal("proof with a different pid was accepted")
		}
		wrongGen := proof
		wrongGen.generation++
		if _, err := intent.Terminate(wrongGen); err == nil {
			t.Fatal("proof with a different generation was accepted")
		}
		wrongRun := proof
		wrongRun.runID = "ffffffffffffffffffffffffffffffff"
		if _, err := intent.Terminate(wrongRun); err == nil {
			t.Fatal("proof from a stale run was accepted")
		}
		wrongEpoch := proof
		wrongEpoch.epoch = "ffffffffffffffffffffffffffffffff"
		if _, err := intent.Terminate(wrongEpoch); err == nil {
			t.Fatal("proof from a different epoch was accepted")
		}
		if _, err := intent.Terminate(proof); err != nil {
			t.Fatal("valid proof was rejected after negative attempts")
		}
		if _, err := intent.Terminate(proof); err == nil {
			t.Fatal("duplicate termination was accepted")
		}
	})

	t.Run("prepare_requires_arrived_unreleased_barrier", func(t *testing.T) {
		h := queryControlledStart(t, obsTestBarrier, 1)
		queryObsBeginOpen(t, h, 1, obsTestBegin1)
		barrier, err := h.obs.Arm(1, testcontrol.PointRegistered, obsTestOrderA)
		if err != nil {
			t.Fatal("arm controlled termination barrier failed")
		}
		process, _ := queryControlledProcess(t, 1, t020LifecycleHeldOK)
		if _, err := h.obs.PrepareControlledTermination(barrier, process); err == nil {
			t.Fatal("prepare accepted an unreached barrier")
		}

		call := queryObsCall(1, obsTestBegin1, 1, testcontrol.KindQuery, obsTestOrderA)
		obsTestMustDo(t, h, call)
		reach := obsTestReachFrame(1, obsTestBegin1, 2, testcontrol.PointRegistered, obsTestOrderA)
		reach.Version = testcontrol.Version2
		ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
		defer cancel()
		done := queryObsSendAsync(ctx, h.socket, reach)
		if _, err := barrier.Wait(ctx); err != nil {
			t.Fatal("controlled termination barrier wait failed")
		}
		if err := barrier.Release(testcontrol.ActionContinue); err != nil {
			t.Fatal("controlled termination barrier release failed")
		}
		_ = obsTestAwait(t, ctx, done)
		if _, err := h.obs.PrepareControlledTermination(barrier, process); err == nil {
			t.Fatal("prepare accepted an already released barrier")
		}
	})

	t.Run("prepare_after_uncertain_or_timeout_rejected", func(t *testing.T) {
		h := queryControlledStart(t, obsTestBarrierShort, 1)
		queryObsBeginOpen(t, h, 1, obsTestBegin1)
		barrier, _ := queryControlledHold(t, h, obsTestBegin1, testcontrol.PointRegistered, obsTestOrderA)
		process, _ := queryControlledProcess(t, 1, t020LifecycleHeldOK)
		time.Sleep(obsTestBarrierShort + 100*time.Millisecond)
		if _, err := h.obs.PrepareControlledTermination(barrier, process); err == nil {
			t.Fatal("prepare accepted a timed-out held barrier")
		}
	})

	t.Run("existing_uncertain_is_never_reset", func(t *testing.T) {
		h := queryControlledStart(t, obsTestBarrier, 1)
		intent, process := queryControlledPrepareChild(t, h, obsTestOrderA)
		stopCtx, stopCancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer stopCancel()
		_, _ = process.Stop(stopCtx)
		proof, err := process.ControlledTerminationProof(stopCtx)
		if err != nil {
			t.Fatal("real process terminal proof failed")
		}
		bad := queryObsCall(1, obsTestBegin1, 3, testcontrol.KindQuery, obsTestOrderB)
		bad.RunID = obsTestWrongRunID
		if _, err := obsTestSend(context.Background(), h.socket, bad); err != nil {
			t.Fatal("uncertainty trigger exchange failed")
		}
		if _, err := intent.Terminate(proof); err == nil {
			t.Fatal("controlled termination reset an existing uncertainty")
		}
		if _, _, ok := h.obs.SealedGeneration(1); ok {
			t.Fatal("uncertain generation reported a receipt")
		}
	})

	t.Run("output_collection_failure_proof_rejected", func(t *testing.T) {
		source := t020SourceFixture(t)
		artifacts, _ := t020BeginArtifactsAt(t, t020TempDir(t), source, obsTestRunID)
		held := t020HeldSocket(t)
		env := []string{
			"PAYMENT_HUB_TEST_RUN_ID=" + obsTestRunID,
			"PAYMENT_HUB_TEST_OBSERVER_EPOCH=" + obsTestEpoch,
			"PAYMENT_HUB_TEST_GENERATION=1",
			t020HelperReadySocket + "=" + held.path,
		}
		process, cancel := t020StartLifecycleProcess(t, artifacts, 1, t020LifecycleHeldBad, time.Second, env...)
		defer cancel()
		held.awaitReady(t)
		held.releaseNow()
		ctx, waitCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer waitCancel()
		_, _ = process.Wait(ctx)
		if _, err := process.ControlledTerminationProof(ctx); err == nil {
			t.Fatal("output collection failure was accepted as a terminal proof")
		}
	})
}
