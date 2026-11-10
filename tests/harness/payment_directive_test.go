package harness

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"payment-hub/internal/testcontrol"
)

func obsTestSendAsync(ctx context.Context, socket string, frame testcontrol.Frame) <-chan obsTestResult {
	ch := make(chan obsTestResult, 1)
	go func() {
		resp, err := obsTestSend(ctx, socket, frame)
		ch <- obsTestResult{frame: resp, err: err}
	}()
	return ch
}

func obsTestAwaitHoldBinding(t *testing.T, hold *PaymentHold, ctx context.Context) PaymentBinding {
	t.Helper()
	binding, err := hold.Wait(ctx)
	if err != nil {
		t.Fatal("payment hold wait failed")
	}
	return binding
}

func obsTestReleaseHold(t *testing.T, hold *PaymentHold, directive testcontrol.Directive) {
	t.Helper()
	if err := hold.Release(directive); err != nil {
		t.Fatal("payment hold release failed")
	}
}

func obsTestAwaitPaymentAck(t *testing.T, ctx context.Context, ch <-chan obsTestResult, request testcontrol.Frame) testcontrol.Frame {
	t.Helper()
	res := obsTestAwait(t, ctx, ch)
	if res.err != nil {
		t.Fatal("payment call exchange failed")
	}
	obsTestCheckAck(t, request, res.frame)
	return res.frame
}

func obsTestRequireAckDirective(t *testing.T, frame testcontrol.Frame, want testcontrol.Directive) {
	t.Helper()
	if frame.Directive == nil {
		t.Fatal("payment call ack lacks a directive")
	}
	if *frame.Directive != want {
		t.Fatal("payment call ack directive mismatch")
	}
}

func obsTestProofReachFrame(gen uint64, beginID string, seq uint64, orderID string,
	callSeq uint64, directive testcontrol.Directive) testcontrol.Frame {
	frame := obsTestReachFrame(gen, beginID, seq, testcontrol.PointMockCommitted, orderID)
	frame.PaymentCallSeq = &callSeq
	frame.AppliedResult = &directive
	return frame
}

func obsTestSetupReleasedPayment(t *testing.T, h *obsTestHarness, gen uint64, beginID string,
	callSeq uint64, orderID string, directive testcontrol.Directive) (testcontrol.Frame, *PaymentHold) {
	t.Helper()
	hold, err := h.obs.HoldPaymentEntry(gen)
	if err != nil {
		t.Fatal("hold payment entry failed")
	}
	call := obsTestCallFrame(gen, beginID, callSeq, testcontrol.KindPayment, orderID)
	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	ch := obsTestSendAsync(ctx, h.socket, call)
	binding := obsTestAwaitHoldBinding(t, hold, ctx)
	if binding.RunID != obsTestRunID || binding.Epoch != obsTestEpoch ||
		binding.Generation != gen || binding.CallSeq != callSeq || binding.OrderID != orderID ||
		binding.EventID != obsTestEventID(beginID, callSeq) {
		t.Fatal("payment binding mismatch")
	}
	obsTestReleaseHold(t, hold, directive)
	ack := obsTestAwaitPaymentAck(t, ctx, ch, call)
	obsTestRequireAckDirective(t, ack, directive)
	return call, hold
}

func TestObserverPaymentDirective(t *testing.T) {
	t.Run("happy_path_consume_and_seal", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)
		_, _ = obsTestSetupReleasedPayment(t, h, 1, obsTestBegin1, 1, obsTestOrderA, testcontrol.DirectiveSucceeded)

		proof := obsTestProofReachFrame(1, obsTestBegin1, 2, obsTestOrderA, 1, testcontrol.DirectiveSucceeded)
		ack := obsTestMustDo(t, h, proof)
		obsTestCheckAck(t, proof, ack)
		if ack.Action != testcontrol.ActionContinue {
			t.Fatal("payment proof ack action mismatch")
		}
		if _, err := h.obs.Seal(1, 2); err != nil {
			t.Fatal("seal after payment proof consumption failed")
		}
		obsTestAssertCounts(t, h, Counts{Payment: 1})
	})

	t.Run("ack_is_not_written_before_release", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)
		hold, err := h.obs.HoldPaymentEntry(1)
		if err != nil {
			t.Fatal("hold payment entry failed")
		}
		call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
		ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
		defer cancel()
		ch := obsTestSendAsync(ctx, h.socket, call)
		_ = obsTestAwaitHoldBinding(t, hold, ctx)
		select {
		case res := <-ch:
			if res.err == nil {
				t.Fatal("payment ack was written before release")
			}
		case <-time.After(obsTestShort):
		}
		obsTestReleaseHold(t, hold, testcontrol.DirectiveSucceeded)
		ack := obsTestAwaitPaymentAck(t, ctx, ch, call)
		obsTestRequireAckDirective(t, ack, testcontrol.DirectiveSucceeded)

		proof := obsTestProofReachFrame(1, obsTestBegin1, 2, obsTestOrderA, 1, testcontrol.DirectiveSucceeded)
		obsTestMustDo(t, h, proof)
		if _, err := h.obs.Seal(1, 2); err != nil {
			t.Fatal("seal after delayed release failed")
		}
	})

	t.Run("retransmitted_call_waits_same_hold", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)
		hold, err := h.obs.HoldPaymentEntry(1)
		if err != nil {
			t.Fatal("hold payment entry failed")
		}
		call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
		ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
		defer cancel()
		first := obsTestSendAsync(ctx, h.socket, call)
		second := obsTestSendAsync(ctx, h.socket, call)
		_ = obsTestAwaitHoldBinding(t, hold, ctx)
		for _, ch := range []<-chan obsTestResult{first, second} {
			select {
			case res := <-ch:
				if res.err == nil {
					t.Fatal("retransmitted payment call returned before release")
				}
			case <-time.After(obsTestShort):
			}
		}
		obsTestReleaseHold(t, hold, testcontrol.DirectiveSucceeded)
		for _, ch := range []<-chan obsTestResult{first, second} {
			ack := obsTestAwaitPaymentAck(t, ctx, ch, call)
			obsTestRequireAckDirective(t, ack, testcontrol.DirectiveSucceeded)
		}

		h.obs.mu.Lock()
		payments := h.obs.counts.Payment
		records := h.obs.records
		h.obs.mu.Unlock()
		if payments != 1 || records != 1 {
			t.Fatal("retransmitted payment call was counted twice")
		}

		proof := obsTestProofReachFrame(1, obsTestBegin1, 2, obsTestOrderA, 1, testcontrol.DirectiveSucceeded)
		obsTestMustDo(t, h, proof)
		if _, err := h.obs.Seal(1, 2); err != nil {
			t.Fatal("seal after retransmitted payment call failed")
		}
	})

	t.Run("unreached_hold_blocks_seal", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)
		hold, err := h.obs.HoldPaymentEntry(1)
		if err != nil {
			t.Fatal("hold payment entry failed")
		}
		if _, err := h.obs.Seal(1, 0); err == nil {
			t.Fatal("seal with an unreached payment hold was accepted")
		}
		obsTestAssertUncertain(t, h)
		if err := hold.Release(testcontrol.DirectiveSucceeded); err == nil {
			t.Fatal("unreached hold remained releasable after a failed seal")
		}
	})

	t.Run("unreleased_hold_blocks_seal", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)
		hold, err := h.obs.HoldPaymentEntry(1)
		if err != nil {
			t.Fatal("hold payment entry failed")
		}
		call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
		ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
		defer cancel()
		ch := obsTestSendAsync(ctx, h.socket, call)
		_ = obsTestAwaitHoldBinding(t, hold, ctx)
		if _, err := h.obs.Seal(1, 1); err == nil {
			t.Fatal("seal with an unreleased payment hold was accepted")
		}
		obsTestCheckPendingEnded(t, obsTestAwait(t, ctx, ch))
		obsTestAssertUncertain(t, h)
		if err := hold.Release(testcontrol.DirectiveSucceeded); err == nil {
			t.Fatal("unreleased hold remained releasable after a failed seal")
		}
	})

	t.Run("unconsumed_hold_blocks_seal", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)
		_, hold := obsTestSetupReleasedPayment(t, h, 1, obsTestBegin1, 1, obsTestOrderA, testcontrol.DirectiveSucceeded)
		if _, err := h.obs.Seal(1, 1); err == nil {
			t.Fatal("seal with a released but unconsumed payment hold was accepted")
		}
		obsTestAssertUncertain(t, h)
		if err := hold.Release(testcontrol.DirectiveSucceeded); err == nil {
			t.Fatal("released hold remained releasable after a failed seal")
		}
	})

	t.Run("hold_validation", func(t *testing.T) {
		t.Run("before_expect", func(t *testing.T) {
			h := obsTestStart(t, obsTestBarrier)
			if _, err := h.obs.HoldPaymentEntry(1); !errors.Is(err, testcontrol.ErrProtocol) {
				t.Fatal("hold before generation expectation was not rejected")
			}
		})
		t.Run("duplicate", func(t *testing.T) {
			h := obsTestStart(t, obsTestBarrier)
			obsTestExpectOpen(t, h, 1, obsTestBegin1)
			if _, err := h.obs.HoldPaymentEntry(1); err != nil {
				t.Fatal("first payment hold failed")
			}
			if _, err := h.obs.HoldPaymentEntry(1); !errors.Is(err, testcontrol.ErrProtocol) {
				t.Fatal("second payment hold in one generation was not rejected")
			}
		})
		t.Run("sealed_generation", func(t *testing.T) {
			h := obsTestStart(t, obsTestBarrier)
			obsTestExpectOpen(t, h, 1, obsTestBegin1)
			obsTestSealZero(t, h, 1)
			if _, err := h.obs.HoldPaymentEntry(1); !errors.Is(err, testcontrol.ErrProtocol) {
				t.Fatal("hold on a sealed generation was not rejected")
			}
		})
		t.Run("payment_already_occurred", func(t *testing.T) {
			h := obsTestStart(t, obsTestBarrier)
			obsTestExpectOpen(t, h, 1, obsTestBegin1)
			call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
			obsTestMustDo(t, h, call)
			if _, err := h.obs.HoldPaymentEntry(1); !errors.Is(err, testcontrol.ErrProtocol) {
				t.Fatal("hold after a payment entry was not rejected")
			}
		})
		t.Run("release_before_arrival", func(t *testing.T) {
			h := obsTestStart(t, obsTestBarrier)
			obsTestExpectOpen(t, h, 1, obsTestBegin1)
			hold, err := h.obs.HoldPaymentEntry(1)
			if err != nil {
				t.Fatal("hold payment entry failed")
			}
			if err := hold.Release(testcontrol.DirectiveSucceeded); !errors.Is(err, testcontrol.ErrProtocol) {
				t.Fatal("release before payment arrival was not rejected")
			}
		})
		t.Run("invalid_directive", func(t *testing.T) {
			h := obsTestStart(t, obsTestBarrier)
			obsTestExpectOpen(t, h, 1, obsTestBegin1)
			hold, err := h.obs.HoldPaymentEntry(1)
			if err != nil {
				t.Fatal("hold payment entry failed")
			}
			call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
			ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
			defer cancel()
			ch := obsTestSendAsync(ctx, h.socket, call)
			_ = obsTestAwaitHoldBinding(t, hold, ctx)
			if err := hold.Release(testcontrol.Directive(obsTestCanary)); !errors.Is(err, testcontrol.ErrProtocol) {
				t.Fatal("invalid result directive was not rejected")
			}
			obsTestReleaseHold(t, hold, testcontrol.DirectiveSucceeded)
			obsTestAwaitPaymentAck(t, ctx, ch, call)
		})
		t.Run("nil_hold", func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
			defer cancel()
			var hold *PaymentHold
			if _, err := hold.Wait(ctx); !errors.Is(err, testcontrol.ErrControlFault) {
				t.Fatal("nil payment hold wait was not a control fault")
			}
			if err := hold.Release(testcontrol.DirectiveSucceeded); !errors.Is(err, testcontrol.ErrControlFault) {
				t.Fatal("nil payment hold release was not a control fault")
			}
		})
	})

	t.Run("proof_negative_cases", func(t *testing.T) {
		t.Run("no_pending_pair", func(t *testing.T) {
			h := obsTestStart(t, obsTestBarrier)
			obsTestExpectOpen(t, h, 1, obsTestBegin1)
			proof := obsTestProofReachFrame(1, obsTestBegin1, 2, obsTestOrderA, 1, testcontrol.DirectiveSucceeded)
			obsTestRejectFrame(t, h, proof)
			obsTestAssertUncertain(t, h)
		})

		t.Run("armed_hold_pair", func(t *testing.T) {
			h := obsTestStart(t, obsTestBarrier)
			obsTestExpectOpen(t, h, 1, obsTestBegin1)
			if _, err := h.obs.HoldPaymentEntry(1); err != nil {
				t.Fatal("hold payment entry failed")
			}
			proof := obsTestProofReachFrame(1, obsTestBegin1, 2, obsTestOrderA, 1, testcontrol.DirectiveSucceeded)
			obsTestRejectFrame(t, h, proof)
			obsTestAssertUncertain(t, h)
		})

		t.Run("bound_hold_pair", func(t *testing.T) {
			h := obsTestStart(t, obsTestBarrier)
			obsTestExpectOpen(t, h, 1, obsTestBegin1)
			hold, err := h.obs.HoldPaymentEntry(1)
			if err != nil {
				t.Fatal("hold payment entry failed")
			}
			call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
			ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
			defer cancel()
			ch := obsTestSendAsync(ctx, h.socket, call)
			_ = obsTestAwaitHoldBinding(t, hold, ctx)
			proof := obsTestProofReachFrame(1, obsTestBegin1, 2, obsTestOrderA, 1, testcontrol.DirectiveSucceeded)
			obsTestRejectFrame(t, h, proof)
			obsTestCheckPendingEnded(t, obsTestAwait(t, ctx, ch))
			obsTestAssertUncertain(t, h)
			if err := hold.Release(testcontrol.DirectiveSucceeded); err == nil {
				t.Fatal("bound hold remained releasable after a proof fault")
			}
		})

		t.Run("released_hold_bare_committed", func(t *testing.T) {
			h := obsTestStart(t, obsTestBarrier)
			obsTestExpectOpen(t, h, 1, obsTestBegin1)
			obsTestSetupReleasedPayment(t, h, 1, obsTestBegin1, 1, obsTestOrderA, testcontrol.DirectiveSucceeded)
			bare := obsTestReachFrame(1, obsTestBegin1, 2, testcontrol.PointMockCommitted, obsTestOrderA)
			obsTestRejectFrame(t, h, bare)
			obsTestAssertUncertain(t, h)
		})

		t.Run("released_hold_wrong_actual", func(t *testing.T) {
			h := obsTestStart(t, obsTestBarrier)
			obsTestExpectOpen(t, h, 1, obsTestBegin1)
			obsTestSetupReleasedPayment(t, h, 1, obsTestBegin1, 1, obsTestOrderA, testcontrol.DirectiveSucceeded)
			proof := obsTestProofReachFrame(1, obsTestBegin1, 2, obsTestOrderA, 1, testcontrol.DirectiveFailed)
			obsTestRejectFrame(t, h, proof)
			obsTestAssertUncertain(t, h)
		})

		t.Run("released_hold_wrong_order", func(t *testing.T) {
			h := obsTestStart(t, obsTestBarrier)
			obsTestExpectOpen(t, h, 1, obsTestBegin1)
			obsTestSetupReleasedPayment(t, h, 1, obsTestBegin1, 1, obsTestOrderA, testcontrol.DirectiveSucceeded)
			proof := obsTestProofReachFrame(1, obsTestBegin1, 2, obsTestOrderB, 1, testcontrol.DirectiveSucceeded)
			obsTestRejectFrame(t, h, proof)
			obsTestAssertUncertain(t, h)
		})

		t.Run("released_hold_wrong_generation", func(t *testing.T) {
			h := obsTestStart(t, obsTestBarrier)
			obsTestExpectOpen(t, h, 1, obsTestBegin1)
			obsTestExpectOpen(t, h, 2, obsTestBegin2)
			obsTestSetupReleasedPayment(t, h, 1, obsTestBegin1, 1, obsTestOrderA, testcontrol.DirectiveSucceeded)
			proof := obsTestProofReachFrame(2, obsTestBegin2, 2, obsTestOrderA, 1, testcontrol.DirectiveSucceeded)
			obsTestRejectFrame(t, h, proof)
			obsTestAssertUncertain(t, h)
		})

		t.Run("released_hold_reused_old_transaction", func(t *testing.T) {
			h := obsTestStart(t, obsTestBarrier)
			obsTestExpectOpen(t, h, 1, obsTestBegin1)
			query := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindQuery, obsTestOrderA)
			obsTestMustDo(t, h, query)
			hold, err := h.obs.HoldPaymentEntry(1)
			if err != nil {
				t.Fatal("hold payment entry failed")
			}
			call := obsTestCallFrame(1, obsTestBegin1, 2, testcontrol.KindPayment, obsTestOrderA)
			ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
			defer cancel()
			ch := obsTestSendAsync(ctx, h.socket, call)
			_ = obsTestAwaitHoldBinding(t, hold, ctx)
			obsTestReleaseHold(t, hold, testcontrol.DirectiveSucceeded)
			obsTestAwaitPaymentAck(t, ctx, ch, call)
			proof := obsTestProofReachFrame(1, obsTestBegin1, 3, obsTestOrderA, 1, testcontrol.DirectiveSucceeded)
			obsTestRejectFrame(t, h, proof)
			obsTestAssertUncertain(t, h)
		})

		t.Run("consume_twice_different_seq", func(t *testing.T) {
			h := obsTestStart(t, obsTestBarrier)
			obsTestExpectOpen(t, h, 1, obsTestBegin1)
			obsTestSetupReleasedPayment(t, h, 1, obsTestBegin1, 1, obsTestOrderA, testcontrol.DirectiveSucceeded)
			first := obsTestProofReachFrame(1, obsTestBegin1, 2, obsTestOrderA, 1, testcontrol.DirectiveSucceeded)
			obsTestMustDo(t, h, first)
			second := obsTestProofReachFrame(1, obsTestBegin1, 3, obsTestOrderA, 1, testcontrol.DirectiveSucceeded)
			obsTestRejectFrame(t, h, second)
			obsTestAssertUncertain(t, h)
		})

		t.Run("duplicate_pair_content_mismatch", func(t *testing.T) {
			h := obsTestStart(t, obsTestBarrier)
			obsTestExpectOpen(t, h, 1, obsTestBegin1)
			obsTestSetupReleasedPayment(t, h, 1, obsTestBegin1, 1, obsTestOrderA, testcontrol.DirectiveSucceeded)
			first := obsTestProofReachFrame(1, obsTestBegin1, 2, obsTestOrderA, 1, testcontrol.DirectiveSucceeded)
			obsTestMustDo(t, h, first)
			conflict := obsTestProofReachFrame(1, obsTestBegin1, 2, obsTestOrderA, 1, testcontrol.DirectiveFailed)
			obsTestRejectFrame(t, h, conflict)
			obsTestAssertUncertain(t, h)
		})

		t.Run("replay_same_proof_does_not_reconsume", func(t *testing.T) {
			h := obsTestStart(t, obsTestBarrier)
			obsTestExpectOpen(t, h, 1, obsTestBegin1)
			obsTestSetupReleasedPayment(t, h, 1, obsTestBegin1, 1, obsTestOrderA, testcontrol.DirectiveSucceeded)
			proof := obsTestProofReachFrame(1, obsTestBegin1, 2, obsTestOrderA, 1, testcontrol.DirectiveSucceeded)
			first := obsTestMustDo(t, h, proof)
			second := obsTestMustDo(t, h, proof)
			if first.Action != testcontrol.ActionContinue || second.Action != testcontrol.ActionContinue {
				t.Fatal("replayed payment proof action mismatch")
			}
			if _, err := h.obs.Seal(1, 2); err != nil {
				t.Fatal("seal after replayed payment proof failed")
			}
			obsTestAssertCounts(t, h, Counts{Payment: 1})
		})
	})

	t.Run("second_payment_counts_then_uncertain", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)
		obsTestSetupReleasedPayment(t, h, 1, obsTestBegin1, 1, obsTestOrderA, testcontrol.DirectiveSucceeded)
		proof := obsTestProofReachFrame(1, obsTestBegin1, 2, obsTestOrderA, 1, testcontrol.DirectiveSucceeded)
		obsTestMustDo(t, h, proof)

		second := obsTestCallFrame(1, obsTestBegin1, 3, testcontrol.KindPayment, obsTestOrderB)
		obsTestRejectFrame(t, h, second)
		h.obs.mu.Lock()
		payments := h.obs.counts.Payment
		records := h.obs.records
		h.obs.mu.Unlock()
		if payments != 2 {
			t.Fatal("second independent payment was not counted before uncertainty")
		}
		if records != 3 {
			t.Fatal("second independent payment record count mismatch")
		}
		obsTestAssertUncertain(t, h)
		if _, err := h.obs.Seal(1, 3); err == nil {
			t.Fatal("polluted run closed after a second independent payment")
		}
	})

	t.Run("second_payment_while_pending_counts_then_uncertain", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)
		hold, err := h.obs.HoldPaymentEntry(1)
		if err != nil {
			t.Fatal("hold payment entry failed")
		}
		first := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
		ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
		defer cancel()
		ch := obsTestSendAsync(ctx, h.socket, first)
		_ = obsTestAwaitHoldBinding(t, hold, ctx)

		second := obsTestCallFrame(1, obsTestBegin1, 2, testcontrol.KindPayment, obsTestOrderB)
		obsTestRejectFrame(t, h, second)
		obsTestCheckPendingEnded(t, obsTestAwait(t, ctx, ch))
		h.obs.mu.Lock()
		payments := h.obs.counts.Payment
		h.obs.mu.Unlock()
		if payments != 2 {
			t.Fatal("pending second payment was not counted before uncertainty")
		}
		obsTestAssertUncertain(t, h)
		if err := hold.Release(testcontrol.DirectiveSucceeded); err == nil {
			t.Fatal("pending hold remained releasable after a second payment")
		}
	})

	t.Run("close_cancels_released_unconsumed_hold", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)
		_, hold := obsTestSetupReleasedPayment(t, h, 1, obsTestBegin1, 1, obsTestOrderA, testcontrol.DirectiveSucceeded)
		if err := h.close(); err != nil {
			t.Fatal("close with a released unconsumed payment hold failed")
		}
		if err := hold.Release(testcontrol.DirectiveSucceeded); err == nil {
			t.Fatal("close did not cancel a released unconsumed payment hold")
		}
		if _, err := hold.Wait(context.Background()); err == nil {
			t.Fatal("closed payment hold remained waitable")
		}
		obsTestAssertUncertain(t, h)
	})

	t.Run("wait_timeout_is_uncertain", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)
		hold, err := h.obs.HoldPaymentEntry(1)
		if err != nil {
			t.Fatal("hold payment entry failed")
		}
		ctx, cancel := context.WithTimeout(context.Background(), observerPaymentHoldTimeout+time.Second)
		defer cancel()
		if _, err := hold.Wait(ctx); !errors.Is(err, testcontrol.ErrUncertain) {
			t.Fatal("payment hold timeout was not fixture uncertainty")
		}
		if ctx.Err() != nil {
			t.Fatal("payment hold timeout exceeded the fixture deadline")
		}
		obsTestAssertUncertain(t, h)
		if err := hold.Release(testcontrol.DirectiveSucceeded); err == nil {
			t.Fatal("timed out payment hold remained releasable")
		}
	})

	t.Run("wait_canceled", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)
		hold, err := h.obs.HoldPaymentEntry(1)
		if err != nil {
			t.Fatal("hold payment entry failed")
		}
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		if _, err := hold.Wait(ctx); !errors.Is(err, testcontrol.ErrControlFault) && !errors.Is(err, testcontrol.ErrUncertain) {
			t.Fatal("canceled payment hold wait was not a fixed integrity error")
		}
		obsTestAssertUncertain(t, h)
		if err := hold.Release(testcontrol.DirectiveSucceeded); err == nil {
			t.Fatal("canceled payment hold remained releasable")
		}
	})

	t.Run("release_close_race", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)
		hold, err := h.obs.HoldPaymentEntry(1)
		if err != nil {
			t.Fatal("hold payment entry failed")
		}
		call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
		ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
		defer cancel()
		ch := obsTestSendAsync(ctx, h.socket, call)
		_ = obsTestAwaitHoldBinding(t, hold, ctx)

		var releaseErr error
		var wg sync.WaitGroup
		wg.Add(1)
		go func() {
			defer wg.Done()
			releaseErr = hold.Release(testcontrol.DirectiveSucceeded)
		}()
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := h.close(); err != nil {
				t.Errorf("close during release race failed")
			}
		}()
		wg.Wait()

		select {
		case res := <-ch:
			if res.err == nil {
				if res.frame.Type == testcontrol.TypeAck {
					if res.frame.Directive == nil {
						t.Fatal("release/close race ack lacked the released directive")
					}
				} else if res.frame.Type != testcontrol.TypeFault {
					t.Fatal("release/close race returned an unexpected frame")
				}
			} else if !obsTestExplicitPeerFailure(res.err) {
				t.Fatal("release/close race ended with an unexpected transport error")
			}
		case <-time.After(obsTestBudget):
			t.Fatal("release/close race left the payment exchange pending")
		}
		if releaseErr != nil && !errors.Is(releaseErr, testcontrol.ErrUncertain) {
			t.Fatal("release/close race returned an unexpected release error")
		}
		if err := hold.Release(testcontrol.DirectiveSucceeded); err == nil {
			t.Fatal("release/close race left the hold releasable")
		}
		obsTestAssertUncertain(t, h)
	})

	t.Run("legacy_bare_committed_still_works", func(t *testing.T) {
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)
		call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
		ack := obsTestMustDo(t, h, call)
		obsTestCheckAck(t, call, ack)
		if ack.Directive != nil {
			t.Fatal("legacy payment call ack unexpectedly carried a directive")
		}
		reach := obsTestReachFrame(1, obsTestBegin1, 2, testcontrol.PointMockCommitted, obsTestOrderA)
		obsTestMustDo(t, h, reach)
		if _, err := h.obs.Seal(1, 2); err != nil {
			t.Fatal("legacy bare committed flow failed to seal")
		}
		obsTestAssertCounts(t, h, Counts{Payment: 1})
	})
}
