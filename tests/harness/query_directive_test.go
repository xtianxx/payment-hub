package harness

import (
	"context"
	"errors"
	"testing"
	"time"

	"payment-hub/internal/testcontrol"
)

// TestObserverQueryDirectiveSafety freezes the v2 observer query hold,
// directive ACK, exactly-once QUERY_APPLIED proof, and per-generation seal.
// It uses the real Observer and only fixed identities.

func queryObsStart(t *testing.T, version int, barrierTimeout time.Duration, generations ...uint64) *obsTestHarness {
	t.Helper()
	dir := obsTestNewDir(t)
	socket := dir + "/s"
	cfg := obsTestConfigWithSocket(socket)
	cfg.BarrierTimeout = barrierTimeout
	cfg.Version = version
	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	obs, err := StartObserver(ctx, cfg)
	if err != nil {
		t.Fatal("start v2 observer failed")
	}
	h := &obsTestHarness{t: t, obs: obs, socket: socket, dir: dir}
	t.Cleanup(h.cleanup)
	for _, generation := range generations {
		if err := obs.Expect(generation); err != nil {
			t.Fatal("expect v2 generation failed")
		}
	}
	return h
}

func queryObsBegin(gen uint64, beginID string) testcontrol.Frame {
	return testcontrol.Frame{
		Version:    testcontrol.Version2,
		Type:       testcontrol.TypeBegin,
		RunID:      obsTestRunID,
		Epoch:      obsTestEpoch,
		Generation: gen,
		EventID:    beginID,
		Capability: testcontrol.CapabilityQueryControl,
	}
}

func queryObsCall(gen uint64, beginID string, seq uint64, kind testcontrol.Kind, orderID string) testcontrol.Frame {
	frame := obsTestCallFrame(gen, beginID, seq, kind, orderID)
	frame.Version = testcontrol.Version2
	return frame
}

func queryObsApplied(gen uint64, beginID string, seq, callSeq uint64, orderID string, outcome testcontrol.QueryOutcome) testcontrol.Frame {
	frame := obsTestReachFrame(gen, beginID, seq, testcontrol.PointQueryApplied, orderID)
	frame.Version = testcontrol.Version2
	frame.QueryCallSeq = &callSeq
	frame.QueryOutcome = &outcome
	return frame
}

func queryObsSeal(gen uint64, beginID string, expected uint64) testcontrol.Frame {
	frame := obsTestSealFrame(gen, beginID, expected)
	frame.Version = testcontrol.Version2
	return frame
}

func queryObsCheckAck(t *testing.T, req, resp testcontrol.Frame) {
	t.Helper()
	if resp.Version != testcontrol.Version2 || resp.Type != testcontrol.TypeAck {
		t.Fatal("v2 response is not a valid ack")
	}
	if resp.RunID != req.RunID || resp.Epoch != req.Epoch ||
		resp.Generation != req.Generation || resp.EventID != req.EventID ||
		resp.For != req.Type {
		t.Fatal("v2 ack identity mismatch")
	}
	switch req.Type {
	case testcontrol.TypeBegin:
		if resp.Capability != testcontrol.CapabilityQueryControl {
			t.Fatal("v2 begin ack capability mismatch")
		}
	case testcontrol.TypeCall:
		if resp.Seq != req.Seq {
			t.Fatal("v2 call ack sequence mismatch")
		}
	case testcontrol.TypeReach:
		if resp.Seq != req.Seq || resp.Action != testcontrol.ActionContinue {
			t.Fatal("v2 reach ack mismatch")
		}
	case testcontrol.TypeSeal:
		if req.ExpectedLastSeq == nil || resp.ExpectedLastSeq == nil ||
			*resp.ExpectedLastSeq != *req.ExpectedLastSeq {
			t.Fatal("v2 seal ack mismatch")
		}
	}
}

func queryObsBeginOpen(t *testing.T, h *obsTestHarness, gen uint64, beginID string) {
	t.Helper()
	begin := queryObsBegin(gen, beginID)
	ack := obsTestMustDo(t, h, begin)
	queryObsCheckAck(t, begin, ack)
}

func queryObsCheckFault(t *testing.T, resp testcontrol.Frame) {
	t.Helper()
	if (resp.Version != testcontrol.Version && resp.Version != testcontrol.Version2) ||
		resp.Type != testcontrol.TypeFault {
		t.Fatal("response is not a valid fault")
	}
	switch resp.Code {
	case testcontrol.FaultProtocol, testcontrol.FaultIncomplete,
		testcontrol.FaultTimeout, testcontrol.FaultClosed:
	default:
		t.Fatal("fault code is invalid")
	}
}

func queryObsRejectFrame(t *testing.T, h *obsTestHarness, frame testcontrol.Frame) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	outcome := obsTestExchangeRaw(ctx, h.socket, obsTestMustEncode(t, frame))
	if !outcome.dialed || !outcome.wrote || outcome.err != nil {
		t.Fatal("v2 observer did not reject the semantic frame with a fault")
	}
	resp, err := testcontrol.DecodeFrame(outcome.raw)
	if err != nil {
		t.Fatal("v2 observer rejection was not a protocol frame")
	}
	queryObsCheckFault(t, resp)
}

func queryObsSendAsync(ctx context.Context, socket string, frame testcontrol.Frame) <-chan obsTestResult {
	ch := make(chan obsTestResult, 1)
	go func() {
		resp, err := obsTestSend(ctx, socket, frame)
		ch <- obsTestResult{frame: resp, err: err}
	}()
	return ch
}

func TestObserverQueryDirectiveSafety(t *testing.T) {
	t.Run("hold_first_query_second_independent_apply_and_seal", func(t *testing.T) {
		h := queryObsStart(t, testcontrol.Version2, obsTestBarrier, 1)
		queryObsBeginOpen(t, h, 1, obsTestBegin1)
		hold, err := h.obs.HoldQueryEntry(1)
		if err != nil {
			t.Fatal("hold query entry failed")
		}
		ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
		defer cancel()
		firstCall := queryObsCall(1, obsTestBegin1, 1, testcontrol.KindQuery, obsTestOrderA)
		first := queryObsSendAsync(ctx, h.socket, firstCall)
		binding, err := hold.Wait(ctx)
		if err != nil {
			t.Fatal("query hold wait failed")
		}
		if binding.Generation != 1 || binding.CallSeq != 1 || binding.OrderID != obsTestOrderA {
			t.Fatal("query binding mismatch")
		}
		secondCall := queryObsCall(1, obsTestBegin1, 2, testcontrol.KindQuery, obsTestOrderB)
		secondAck := obsTestMustDo(t, h, secondCall)
		queryObsCheckAck(t, secondCall, secondAck)
		if secondAck.QueryDirective != nil {
			t.Fatal("independent second query was held or given a directive")
		}
		select {
		case result := <-first:
			if result.err == nil {
				t.Fatal("held first query ack arrived before release")
			}
		case <-time.After(obsTestShort):
		}
		if err := hold.Release(testcontrol.QueryScenarioFailure); err != nil {
			t.Fatal("query hold release failed")
		}
		firstAck := obsTestAwait(t, ctx, first)
		if firstAck.err != nil {
			t.Fatal("held query ack failed after release")
		}
		queryObsCheckAck(t, firstCall, firstAck.frame)
		if firstAck.frame.QueryDirective == nil ||
			*firstAck.frame.QueryDirective != testcontrol.QueryScenarioFailure {
			t.Fatal("held query directive mismatch")
		}

		firstApplied := queryObsApplied(1, obsTestBegin1, 3, 1, obsTestOrderA, testcontrol.QueryOutcomeUnavailable)
		firstAppliedAck := obsTestMustDo(t, h, firstApplied)
		queryObsCheckAck(t, firstApplied, firstAppliedAck)
		secondApplied := queryObsApplied(1, obsTestBegin1, 4, 2, obsTestOrderB, testcontrol.QueryOutcomeFound)
		secondAppliedAck := obsTestMustDo(t, h, secondApplied)
		queryObsCheckAck(t, secondApplied, secondAppliedAck)

		receipt, err := h.obs.Seal(1, 4)
		if err != nil {
			t.Fatal("seal after query proofs failed")
		}
		sealedReceipt, counts, ok := h.obs.SealedGeneration(1)
		if !ok || sealedReceipt != receipt || counts != (Counts{Query: 2}) {
			t.Fatal("sealed generation proof mismatch")
		}
	})

	t.Run("directive_ack_is_not_consumption", func(t *testing.T) {
		h := queryObsStart(t, testcontrol.Version2, obsTestBarrier, 1)
		queryObsBeginOpen(t, h, 1, obsTestBegin1)
		hold, err := h.obs.HoldQueryEntry(1)
		if err != nil {
			t.Fatal("hold query entry failed")
		}
		ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
		defer cancel()
		call := queryObsCall(1, obsTestBegin1, 1, testcontrol.KindQuery, obsTestOrderA)
		done := queryObsSendAsync(ctx, h.socket, call)
		if _, err := hold.Wait(ctx); err != nil {
			t.Fatal("query hold wait failed")
		}
		if err := hold.Release(testcontrol.QueryScenarioMismatchAmount); err != nil {
			t.Fatal("query hold release failed")
		}
		result := obsTestAwait(t, ctx, done)
		if result.err != nil || result.frame.QueryDirective == nil {
			t.Fatal("query directive ack failed")
		}
		if _, err := h.obs.Seal(1, 1); err == nil {
			t.Fatal("seal accepted a query with only a directive ack")
		}
	})

	t.Run("normal_query_without_directive_still_needs_apply", func(t *testing.T) {
		h := queryObsStart(t, testcontrol.Version2, obsTestBarrier, 1)
		queryObsBeginOpen(t, h, 1, obsTestBegin1)
		call := queryObsCall(1, obsTestBegin1, 1, testcontrol.KindQuery, obsTestOrderA)
		ack := obsTestMustDo(t, h, call)
		queryObsCheckAck(t, call, ack)
		if ack.QueryDirective != nil {
			t.Fatal("normal query gained a directive")
		}
		if _, err := h.obs.Seal(1, 1); err == nil {
			t.Fatal("seal accepted an unapplied normal query")
		}
	})

	t.Run("duplicate_or_cross_order_apply_fails", func(t *testing.T) {
		h := queryObsStart(t, testcontrol.Version2, obsTestBarrier, 1)
		queryObsBeginOpen(t, h, 1, obsTestBegin1)
		call := queryObsCall(1, obsTestBegin1, 1, testcontrol.KindQuery, obsTestOrderA)
		obsTestMustDo(t, h, call)
		applied := queryObsApplied(1, obsTestBegin1, 2, 1, obsTestOrderA, testcontrol.QueryOutcomeFound)
		obsTestMustDo(t, h, applied)
		crossOrder := queryObsApplied(1, obsTestBegin1, 3, 1, obsTestOrderB, testcontrol.QueryOutcomeFound)
		queryObsRejectFrame(t, h, crossOrder)
		if _, err := h.obs.Seal(1, 3); err == nil {
			t.Fatal("seal recovered after cross-order query apply")
		}
	})

	t.Run("duplicate_apply_fails_and_latches", func(t *testing.T) {
		h := queryObsStart(t, testcontrol.Version2, obsTestBarrier, 1)
		queryObsBeginOpen(t, h, 1, obsTestBegin1)
		call := queryObsCall(1, obsTestBegin1, 1, testcontrol.KindQuery, obsTestOrderA)
		obsTestMustDo(t, h, call)
		applied := queryObsApplied(1, obsTestBegin1, 2, 1, obsTestOrderA, testcontrol.QueryOutcomeFound)
		obsTestMustDo(t, h, applied)
		duplicate := queryObsApplied(1, obsTestBegin1, 3, 1, obsTestOrderA, testcontrol.QueryOutcomeFound)
		queryObsRejectFrame(t, h, duplicate)
		if _, err := h.obs.Seal(1, 3); err == nil {
			t.Fatal("seal recovered after duplicate query apply")
		}
	})

	t.Run("unreached_hold_blocks_seal", func(t *testing.T) {
		h := queryObsStart(t, testcontrol.Version2, obsTestBarrier, 1)
		queryObsBeginOpen(t, h, 1, obsTestBegin1)
		if _, err := h.obs.HoldQueryEntry(1); err != nil {
			t.Fatal("hold query entry failed")
		}
		if _, err := h.obs.Seal(1, 0); err == nil {
			t.Fatal("seal accepted an unreached query hold")
		}
	})

	t.Run("v1_observer_rejects_v2_negotiation", func(t *testing.T) {
		h := queryObsStart(t, testcontrol.Version, obsTestBarrier, 1)
		begin := queryObsBegin(1, obsTestBegin1)
		resp, err := obsTestSend(context.Background(), h.socket, begin)
		if err != nil {
			t.Fatal("v1 observer exchange failed")
		}
		queryObsCheckFault(t, resp)
		if _, _, ok := h.obs.SealedGeneration(1); ok {
			t.Fatal("v1 observer sealed after rejecting v2 negotiation")
		}
	})

	t.Run("v2_observer_rejects_v1_negotiation", func(t *testing.T) {
		h := queryObsStart(t, testcontrol.Version2, obsTestBarrier, 1)
		begin := obsTestBeginFrame(1, obsTestBegin1)
		resp, err := obsTestSend(context.Background(), h.socket, begin)
		if err != nil {
			t.Fatal("v2 observer exchange failed")
		}
		queryObsCheckFault(t, resp)
	})

	t.Run("invalid_release_scenario_rejected", func(t *testing.T) {
		h := queryObsStart(t, testcontrol.Version2, obsTestBarrier, 1)
		queryObsBeginOpen(t, h, 1, obsTestBegin1)
		hold, err := h.obs.HoldQueryEntry(1)
		if err != nil {
			t.Fatal("hold query entry failed")
		}
		ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
		defer cancel()
		call := queryObsCall(1, obsTestBegin1, 1, testcontrol.KindQuery, obsTestOrderA)
		done := queryObsSendAsync(ctx, h.socket, call)
		if _, err := hold.Wait(ctx); err != nil {
			t.Fatal("query hold wait failed")
		}
		if err := hold.Release(testcontrol.QueryScenario("other")); !errors.Is(err, testcontrol.ErrProtocol) {
			t.Fatal("invalid query scenario release was accepted")
		}
		if err := hold.Release(testcontrol.QueryScenarioFailure); err != nil {
			t.Fatal("valid query scenario release failed")
		}
		result := obsTestAwait(t, ctx, done)
		if result.err != nil {
			t.Fatal("query release exchange failed")
		}
	})

	t.Run("query_applications_are_actual_server_measurements", func(t *testing.T) {
		h := queryObsStart(t, testcontrol.Version2, obsTestBarrier, 1)
		queryObsBeginOpen(t, h, 1, obsTestBegin1)
		hold, err := h.obs.HoldQueryEntry(1)
		if err != nil {
			t.Fatal("hold query entry failed")
		}
		ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
		defer cancel()
		firstCall := queryObsCall(1, obsTestBegin1, 1, testcontrol.KindQuery, obsTestOrderA)
		first := queryObsSendAsync(ctx, h.socket, firstCall)
		if _, err := hold.Wait(ctx); err != nil {
			t.Fatal("query hold wait failed")
		}
		if err := hold.Release(testcontrol.QueryScenarioFailure); err != nil {
			t.Fatal("query hold release failed")
		}
		if result := obsTestAwait(t, ctx, first); result.err != nil {
			t.Fatal("held query ack failed")
		}
		secondCall := queryObsCall(1, obsTestBegin1, 2, testcontrol.KindQuery, obsTestOrderB)
		obsTestMustDo(t, h, secondCall)

		before, ok := h.obs.QueryApplications(1)
		if !ok || before.Trusted || before.ApplyCount != 0 || len(before.Applications) != 2 {
			t.Fatal("unapplied query applications were reported as trusted")
		}
		if before.Applications[0].Directive == nil ||
			*before.Applications[0].Directive != testcontrol.QueryScenarioFailure ||
			before.Applications[0].Outcome != "" ||
			before.Applications[0].AppliedSeq != 0 {
			t.Fatal("query application did not expose the actual directive-only call")
		}
		if before.Applications[1].Directive != nil {
			t.Fatal("normal query application gained a fabricated directive")
		}

		firstApplied := queryObsApplied(1, obsTestBegin1, 3, 1, obsTestOrderA, testcontrol.QueryOutcomeUnavailable)
		obsTestMustDo(t, h, firstApplied)
		secondApplied := queryObsApplied(1, obsTestBegin1, 4, 2, obsTestOrderB, testcontrol.QueryOutcomeFound)
		obsTestMustDo(t, h, secondApplied)

		partial, ok := h.obs.QueryApplications(1)
		if !ok || partial.Trusted || partial.ApplyCount != 2 {
			t.Fatal("partial query applications were reported as trusted")
		}
		if partial.Applications[0].Outcome != testcontrol.QueryOutcomeUnavailable ||
			partial.Applications[0].AppliedSeq != 3 ||
			partial.Applications[0].AppliedEventID != obsTestEventID(obsTestBegin1, 3) {
			t.Fatal("first actual query application proof mismatch")
		}
		if partial.Applications[1].Outcome != testcontrol.QueryOutcomeFound ||
			partial.Applications[1].AppliedSeq != 4 ||
			partial.Applications[1].AppliedEventID != obsTestEventID(obsTestBegin1, 4) {
			t.Fatal("second actual query application proof mismatch")
		}

		if _, err := h.obs.Seal(1, 4); err != nil {
			t.Fatal("query applications seal failed")
		}
		trusted, ok := h.obs.QueryApplications(1)
		if !ok || !trusted.Trusted || trusted.ApplyCount != 2 {
			t.Fatal("sealed query applications were not trusted")
		}
	})

	t.Run("v1_query_applications_never_emit_v2_proof", func(t *testing.T) {
		h := queryObsStart(t, testcontrol.Version, obsTestBarrier, 1)
		begin := obsTestBeginFrame(1, obsTestBegin1)
		obsTestCheckAck(t, begin, obsTestMustDo(t, h, begin))
		call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindQuery, obsTestOrderA)
		obsTestMustDo(t, h, call)
		if snapshot, ok := h.obs.QueryApplications(1); ok || snapshot.Trusted || snapshot.ApplyCount != 0 {
			t.Fatal("v1 observer emitted a v2 query application proof")
		}
	})

	t.Run("sealed_generation_is_per_generation_not_cumulative", func(t *testing.T) {
		h := queryObsStart(t, testcontrol.Version2, obsTestBarrier, 1, 2)
		queryObsBeginOpen(t, h, 1, obsTestBegin1)
		call1 := queryObsCall(1, obsTestBegin1, 1, testcontrol.KindQuery, obsTestOrderA)
		obsTestMustDo(t, h, call1)
		obsTestMustDo(t, h, queryObsApplied(1, obsTestBegin1, 2, 1, obsTestOrderA, testcontrol.QueryOutcomeFound))
		if _, err := h.obs.Seal(1, 2); err != nil {
			t.Fatal("first generation seal failed")
		}
		queryObsBeginOpen(t, h, 2, obsTestBegin2)
		call2 := queryObsCall(2, obsTestBegin2, 1, testcontrol.KindQuery, obsTestOrderB)
		obsTestMustDo(t, h, call2)
		obsTestMustDo(t, h, queryObsApplied(2, obsTestBegin2, 2, 1, obsTestOrderB, testcontrol.QueryOutcomeNotFound))
		if _, err := h.obs.Seal(2, 2); err != nil {
			t.Fatal("second generation seal failed")
		}
		if _, counts, ok := h.obs.SealedGeneration(1); !ok || counts != (Counts{Query: 1}) {
			t.Fatal("first generation counts were cumulative")
		}
		if _, counts, ok := h.obs.SealedGeneration(2); !ok || counts != (Counts{Query: 1}) {
			t.Fatal("second generation counts were cumulative")
		}
	})
}
