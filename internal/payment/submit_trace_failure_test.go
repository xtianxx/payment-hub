package payment

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

const (
	submitTraceFailureMessageFailed        = "本次追踪保存失败"
	submitTraceFailureMessageIndeterminate = "保存状态未确认"
)

type submitTraceFailureMarker struct {
	name    string
	err     error
	message string
}

func submitTraceFailureMarkers() []submitTraceFailureMarker {
	return []submitTraceFailureMarker{
		{name: "trace_save_failed", err: ErrTraceSaveFailed, message: submitTraceFailureMessageFailed},
		{name: "trace_save_indeterminate", err: ErrTraceSaveIndeterminate, message: submitTraceFailureMessageIndeterminate},
	}
}

// submitTraceFailureCommitted builds the locked original confirmation that the
// recorder returns with a trace-save marker. It never includes a handling
// record.
func submitTraceFailureCommitted(base PaymentOrder, result Result) PaymentOrder {
	order := base
	order.Result = result
	at := submitTestOldTime()
	order.ConfirmedAt = &at
	if order.MockTransactionID != nil {
		id := *order.MockTransactionID
		order.MockTransactionID = &id
	} else {
		id := submitTestMockTxID
		order.MockTransactionID = &id
	}
	return order
}

// submitTraceFailureStore models the recorder's save lock: the returned copy is
// taken while holding the lock and never aliases the caller's order.
type submitTraceFailureStore struct {
	mu    sync.Mutex
	order PaymentOrder
}

func (s *submitTraceFailureStore) markerSave(marker error) func(context.Context, Observation) (SaveObservationResult, error) {
	return func(context.Context, Observation) (SaveObservationResult, error) {
		s.mu.Lock()
		defer s.mu.Unlock()
		return SaveObservationResult{Order: clonePaymentOrder(s.order)}, marker
	}
}

func submitTraceFailureNoLookup(t *testing.T, deps *submitTestDeps) {
	t.Helper()
	deps.lookup.byIDFn = func(context.Context, string, PaymentOrderID) (PaymentOrder, bool, error) {
		t.Fatalf("marker path must not perform a readback lookup")
		return PaymentOrder{}, false, ErrUnavailable
	}
	deps.lookup.byBusinessFn = func(context.Context, string, string) (PaymentOrder, bool, error) {
		t.Fatalf("marker path must not perform a business-order lookup")
		return PaymentOrder{}, false, ErrUnavailable
	}
}

func submitTraceFailureAssertNoLookup(t *testing.T, deps *submitTestDeps) {
	t.Helper()
	if deps.lookup.byIDCalls != 0 || deps.lookup.byBusinessCalls != 0 {
		t.Fatalf("expected no lookup, got %d by-ID and %d by-business calls",
			deps.lookup.byIDCalls, deps.lookup.byBusinessCalls)
	}
}

func submitTraceFailureAssertIssues(t *testing.T, got []Issue, wantCodes []string, wantMessage string) {
	t.Helper()
	if len(got) != len(wantCodes) {
		t.Fatalf("expected %d issues, got %d", len(wantCodes), len(got))
	}
	for i, code := range wantCodes {
		if got[i].Code != code {
			t.Fatalf("unexpected issue code at %d: %q", i, got[i].Code)
		}
		if got[i].Message == "" {
			t.Fatalf("issue message must not be empty")
		}
	}
	if wantMessage != "" && got[len(got)-1].Message != wantMessage {
		t.Fatalf("unexpected trace-save issue message: %q", got[len(got)-1].Message)
	}
}

func submitTraceFailureAssertNoExtraWork(t *testing.T, deps *submitTestDeps) {
	t.Helper()
	if deps.reg.calls != 1 {
		t.Fatalf("expected one registration call, got %d", deps.reg.calls)
	}
	if deps.payer.calls != 1 {
		t.Fatalf("expected exactly one original Mock call, got %d", deps.payer.calls)
	}
	if deps.recorder.calls != 1 {
		t.Fatalf("expected exactly one save attempt, got %d", deps.recorder.calls)
	}
}

func TestSubmitTraceFailureProtection(t *testing.T) {
	t.Run("marker_returns_locked_confirmation_without_readback", func(t *testing.T) {
		scenarios := []struct {
			name          string
			setup         func(*submitTestDeps, PaymentOrder, Result)
			wantIssues    []string
			wantCandidate bool
		}{
			{
				name: "accepted_confirmation",
				setup: func(deps *submitTestDeps, base PaymentOrder, result Result) {
					deps.payer.transaction = submitTestTransaction(base, result)
				},
				wantIssues:    []string{IssueCodeTraceSaveError},
				wantCandidate: true,
			},
			{
				name: "mock_fault",
				setup: func(deps *submitTestDeps, _ PaymentOrder, _ Result) {
					deps.payer.err = errors.New("mock fault secret")
				},
				wantIssues: []string{IssueCodeMockResultUnconfirmed, IssueCodeTraceSaveError},
			},
			{
				name: "evidence_mismatch",
				setup: func(deps *submitTestDeps, base PaymentOrder, result Result) {
					tx := submitTestTransaction(base, result)
					tx.Amount = 101
					deps.payer.transaction = tx
				},
				wantIssues: []string{IssueCodeMockEvidenceRejected, IssueCodeTraceSaveError},
			},
		}

		for _, marker := range submitTraceFailureMarkers() {
			marker := marker
			for _, scenario := range scenarios {
				scenario := scenario
				for _, result := range []Result{ResultSucceeded, ResultFailed} {
					result := result
					t.Run(marker.name+"/"+scenario.name+"/"+string(result), func(t *testing.T) {
						base := submitTestOrder(ResultUnknown, 100)
						committed := submitTraceFailureCommitted(base, result)
						deps := submitTestValidDeps(base)
						scenario.setup(deps, base, result)
						deps.recorder.save = (&submitTraceFailureStore{order: committed}).markerSave(marker.err)
						submitTraceFailureNoLookup(t, deps)

						got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
						if err != nil {
							t.Fatalf("expected the locked confirmation to be returned")
						}
						if !got.Created {
							t.Fatalf("marker path must retain the created fact")
						}
						if got.Order.Result != result {
							t.Fatalf("expected the locked original result %q, got %q", result, got.Order.Result)
						}
						if got.Order.ID != base.ID || got.Order.ClientID != base.ClientID ||
							got.Order.BusinessOrderNo != base.BusinessOrderNo ||
							got.Order.Amount != base.Amount || got.Order.Currency != base.Currency ||
							!got.Order.RegisteredAt.Equal(base.RegisteredAt) {
							t.Fatalf("returned order identity does not match the original")
						}
						if got.Order.ConfirmedAt == nil || committed.ConfirmedAt == nil ||
							!got.Order.ConfirmedAt.Equal(*committed.ConfirmedAt) {
							t.Fatalf("returned confirmation time does not match the locked copy")
						}
						if got.Order.MockTransactionID == nil || committed.MockTransactionID == nil ||
							*got.Order.MockTransactionID != *committed.MockTransactionID {
							t.Fatalf("returned Mock transaction ID does not match the locked copy")
						}
						submitTraceFailureAssertIssues(t, got.Issues, scenario.wantIssues, marker.message)
						submitTraceFailureAssertNoExtraWork(t, deps)
						submitTraceFailureAssertNoLookup(t, deps)
						if scenario.wantCandidate {
							if deps.recorder.observation.Candidate == nil {
								t.Fatalf("expected this attempt to have a new confirmation candidate")
							}
						} else if deps.recorder.observation.Candidate != nil {
							t.Fatalf("expected no new confirmation candidate")
						}
					})
				}
			}
		}
	})

	t.Run("marker_keeps_locked_result_over_candidate", func(t *testing.T) {
		pairs := []struct {
			name            string
			paidResult      Result
			committedResult Result
		}{
			{name: "candidate_succeeded_locked_failed", paidResult: ResultSucceeded, committedResult: ResultFailed},
			{name: "candidate_failed_locked_succeeded", paidResult: ResultFailed, committedResult: ResultSucceeded},
		}
		for _, pair := range pairs {
			pair := pair
			for _, marker := range submitTraceFailureMarkers() {
				marker := marker
				t.Run(pair.name+"/"+marker.name, func(t *testing.T) {
					base := submitTestOrder(ResultUnknown, 100)
					committed := submitTraceFailureCommitted(base, pair.committedResult)
					deps := submitTestValidDeps(base)
					deps.payer.transaction = submitTestTransaction(base, pair.paidResult)
					deps.recorder.save = (&submitTraceFailureStore{order: committed}).markerSave(marker.err)
					submitTraceFailureNoLookup(t, deps)

					got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
					if err != nil {
						t.Fatalf("expected the locked confirmation to be returned")
					}
					if !got.Created {
						t.Fatalf("marker path must retain the created fact")
					}
					if got.Order.Result != pair.committedResult {
						t.Fatalf("expected the locked result %q, got %q", pair.committedResult, got.Order.Result)
					}
					submitTraceFailureAssertIssues(t, got.Issues, []string{IssueCodeTraceSaveError}, marker.message)
					submitTraceFailureAssertNoExtraWork(t, deps)
					submitTraceFailureAssertNoLookup(t, deps)
				})
			}
		}
	})

	t.Run("marker_returns_committed_fact_after_cancellation", func(t *testing.T) {
		for _, marker := range submitTraceFailureMarkers() {
			marker := marker
			t.Run(marker.name, func(t *testing.T) {
				base := submitTestOrder(ResultUnknown, 100)
				committed := submitTraceFailureCommitted(base, ResultSucceeded)
				deps := submitTestValidDeps(base)
				deps.payer.transaction = submitTestTransaction(base, ResultSucceeded)
				lifecycle, cancelLifecycle := context.WithCancel(context.Background())
				defer cancelLifecycle()
				clientCtx, cancelClient := context.WithCancel(context.Background())
				defer cancelClient()
				var saveDeadline time.Time
				deps.recorder.save = func(ctx context.Context, _ Observation) (SaveObservationResult, error) {
					saveDeadline, _ = ctx.Deadline()
					cancelClient()
					cancelLifecycle()
					return SaveObservationResult{Order: clonePaymentOrder(committed)}, marker.err
				}
				submitTraceFailureNoLookup(t, deps)

				got, err := deps.submitter(lifecycle).Submit(clientCtx, submitTestRequestID, submitTestInput())
				if err != nil {
					t.Fatalf("expected the committed fact to survive cancellation")
				}
				if !got.Created || got.Order.Result != ResultSucceeded {
					t.Fatalf("expected the created committed fact")
				}
				submitTraceFailureAssertIssues(t, got.Issues, []string{IssueCodeTraceSaveError}, marker.message)
				if saveDeadline.IsZero() {
					t.Fatalf("expected the existing finish window to bound the save")
				}
				submitTraceFailureAssertNoExtraWork(t, deps)
				submitTraceFailureAssertNoLookup(t, deps)
			})
		}
	})

	t.Run("marker_does_not_use_readback", func(t *testing.T) {
		for _, marker := range submitTraceFailureMarkers() {
			marker := marker
			t.Run(marker.name, func(t *testing.T) {
				base := submitTestOrder(ResultUnknown, 100)
				committed := submitTraceFailureCommitted(base, ResultFailed)
				deps := submitTestValidDeps(base)
				deps.payer.transaction = submitTestTransaction(base, ResultFailed)
				deps.recorder.save = (&submitTraceFailureStore{order: committed}).markerSave(marker.err)
				readbackCalls := 0
				deps.lookup.byIDFn = func(context.Context, string, PaymentOrderID) (PaymentOrder, bool, error) {
					readbackCalls++
					return PaymentOrder{}, false, ErrUnavailable
				}

				got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
				if err != nil {
					t.Fatalf("expected the marker fact without a readback")
				}
				if !got.Created || got.Order.Result != ResultFailed {
					t.Fatalf("expected the created locked result")
				}
				submitTraceFailureAssertIssues(t, got.Issues, []string{IssueCodeTraceSaveError}, marker.message)
				if readbackCalls != 0 {
					t.Fatalf("marker path performed %d readback lookups", readbackCalls)
				}
				submitTraceFailureAssertNoExtraWork(t, deps)
			})
		}
	})

	t.Run("ordinary_error_result_is_not_trusted", func(t *testing.T) {
		cases := []struct {
			name       string
			payResult  Result
			payErr     error
			wantIssues []string
		}{
			{name: "new_confirmation", payResult: ResultSucceeded, wantIssues: []string{IssueCodeResultSaveError}},
			{name: "mock_fault", payErr: errors.New("mock fault secret"), wantIssues: []string{IssueCodeMockResultUnconfirmed, IssueCodeTraceSaveError}},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				base := submitTestOrder(ResultUnknown, 100)
				committed := submitTraceFailureCommitted(base, ResultSucceeded)
				deps := submitTestValidDeps(base)
				if tc.payErr != nil {
					deps.payer.err = tc.payErr
				} else {
					deps.payer.transaction = submitTestTransaction(base, tc.payResult)
				}
				deps.recorder.save = func(context.Context, Observation) (SaveObservationResult, error) {
					return SaveObservationResult{Order: committed}, errors.New("ordinary save fault")
				}
				deps.lookup.byIDOrder = base
				deps.lookup.byIDFound = true

				got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
				if err != nil {
					t.Fatalf("expected the ordinary readback path to complete")
				}
				if !got.Created {
					t.Fatalf("expected the created fact to be retained")
				}
				if got.Order.Result != ResultUnknown {
					t.Fatalf("ordinary error result must not be trusted")
				}
				submitTraceFailureAssertIssues(t, got.Issues, tc.wantIssues, "")
				if deps.lookup.byIDCalls != 1 || deps.lookup.byBusinessCalls != 0 {
					t.Fatalf("expected exactly one ordinary readback")
				}
			})
		}
	})

	t.Run("illegal_marker_snapshot_is_rejected", func(t *testing.T) {
		cases := []struct {
			name   string
			mutate func(*PaymentOrder, *ResultHandlingRecord)
		}{
			{name: "record_id", mutate: func(_ *PaymentOrder, r *ResultHandlingRecord) {
				r.ID = ResultHandlingRecordID(submitTestObservationID)
			}},
			{name: "record_evidence", mutate: func(_ *PaymentOrder, r *ResultHandlingRecord) {
				amount := int64(100)
				r.Evidence.Amount = &amount
			}},
			{name: "record_reason", mutate: func(_ *PaymentOrder, r *ResultHandlingRecord) {
				code := reasonCodeEvidenceMismatch
				r.ReasonCode = &code
			}},
			{name: "record_decision", mutate: func(_ *PaymentOrder, r *ResultHandlingRecord) {
				r.Decision = DecisionAcceptConfirmation
			}},
			{name: "invalid_order_id", mutate: func(o *PaymentOrder, _ *ResultHandlingRecord) { o.ID = "" }},
			{name: "other_order_id", mutate: func(o *PaymentOrder, _ *ResultHandlingRecord) { o.ID = submitTestOtherOrderID }},
			{name: "client_mismatch", mutate: func(o *PaymentOrder, _ *ResultHandlingRecord) { o.ClientID = "other_client" }},
			{name: "business_no_mismatch", mutate: func(o *PaymentOrder, _ *ResultHandlingRecord) { o.BusinessOrderNo = "M-OTHER" }},
			{name: "amount_mismatch", mutate: func(o *PaymentOrder, _ *ResultHandlingRecord) { o.Amount++ }},
			{name: "currency_mismatch", mutate: func(o *PaymentOrder, _ *ResultHandlingRecord) { o.Currency = "USD" }},
			{name: "registered_at_mismatch", mutate: func(o *PaymentOrder, _ *ResultHandlingRecord) {
				o.RegisteredAt = o.RegisteredAt.Add(time.Second)
			}},
			{name: "registered_at_non_utc", mutate: func(o *PaymentOrder, _ *ResultHandlingRecord) {
				o.RegisteredAt = o.RegisteredAt.In(time.FixedZone("TRACE", 3600))
			}},
			{name: "result_unknown", mutate: func(o *PaymentOrder, _ *ResultHandlingRecord) {
				o.Result = ResultUnknown
				o.ConfirmedAt = nil
			}},
			{name: "result_empty", mutate: func(o *PaymentOrder, _ *ResultHandlingRecord) {
				o.Result = ""
				o.ConfirmedAt = nil
			}},
			{name: "confirmed_at_missing", mutate: func(o *PaymentOrder, _ *ResultHandlingRecord) { o.ConfirmedAt = nil }},
			{name: "confirmed_at_zero", mutate: func(o *PaymentOrder, _ *ResultHandlingRecord) {
				at := time.Time{}
				o.ConfirmedAt = &at
			}},
			{name: "confirmed_at_non_utc", mutate: func(o *PaymentOrder, _ *ResultHandlingRecord) {
				at := o.ConfirmedAt.In(time.FixedZone("TRACE", 3600))
				o.ConfirmedAt = &at
			}},
			{name: "invalid_mock_id", mutate: func(o *PaymentOrder, _ *ResultHandlingRecord) {
				id := MockTransactionID("not-a-uuid")
				o.MockTransactionID = &id
			}},
		}

		for _, tc := range cases {
			tc := tc
			for _, marker := range submitTraceFailureMarkers() {
				marker := marker
				t.Run(tc.name+"/"+marker.name, func(t *testing.T) {
					base := submitTestOrder(ResultUnknown, 100)
					committed := submitTraceFailureCommitted(base, ResultSucceeded)
					var record ResultHandlingRecord
					tc.mutate(&committed, &record)
					deps := submitTestValidDeps(base)
					deps.payer.transaction = submitTestTransaction(base, ResultSucceeded)
					deps.recorder.save = func(context.Context, Observation) (SaveObservationResult, error) {
						return SaveObservationResult{Order: committed, Record: record}, marker.err
					}
					submitTraceFailureNoLookup(t, deps)

					got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
					if !errors.Is(err, ErrUnavailable) {
						t.Fatalf("expected an illegal marker to fail safely, got %v", err)
					}
					if got.Order.ID != "" || got.Created || len(got.Issues) != 0 {
						t.Fatalf("illegal marker must not return a candidate or created result")
					}
					submitTraceFailureAssertNoExtraWork(t, deps)
					submitTraceFailureAssertNoLookup(t, deps)
				})
			}
		}
	})

	t.Run("ordinary_baselines_remain_unchanged", func(t *testing.T) {
		t.Run("reused_order_stays_not_created", func(t *testing.T) {
			order := submitTestOrder(ResultSucceeded, 100)
			deps := submitTestValidDeps(order)
			deps.reg.result = RegisterOrderResult{Order: order, Outcome: RegisterOutcomeReused}
			got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
			if err != nil {
				t.Fatalf("expected nil error")
			}
			if got.Created {
				t.Fatalf("reused order must not be reported created")
			}
			submitTraceFailureAssertNoExtraWorkOnlyRegistration(t, deps)
			submitTraceFailureAssertNoLookup(t, deps)
		})

		t.Run("registration_recovery_stays_not_created", func(t *testing.T) {
			base := submitTestOrder(ResultUnknown, 100)
			deps := submitTestValidDeps(base)
			deps.reg.result = RegisterOrderResult{}
			deps.reg.err = errors.New("registration commit secret")
			deps.lookup.byBusinessOrder = base
			deps.lookup.byBusinessFound = true
			got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
			if err != nil {
				t.Fatalf("expected nil error")
			}
			if got.Created {
				t.Fatalf("registration readback must not be reported created")
			}
			if got.Order.Result != ResultUnknown {
				t.Fatalf("expected the readback order")
			}
			submitTraceFailureAssertIssues(t, got.Issues, []string{IssueCodeResultSaveError}, "")
			if deps.reg.calls != 1 || deps.payer.calls != 0 || deps.recorder.calls != 0 {
				t.Fatalf("registration recovery must not pay or save")
			}
			if deps.lookup.byBusinessCalls != 1 || deps.lookup.byIDCalls != 0 {
				t.Fatalf("expected one business-order readback")
			}
		})
	})
}

func submitTraceFailureAssertNoExtraWorkOnlyRegistration(t *testing.T, deps *submitTestDeps) {
	t.Helper()
	if deps.reg.calls != 1 {
		t.Fatalf("expected one registration call, got %d", deps.reg.calls)
	}
	if deps.payer.calls != 0 || deps.recorder.calls != 0 {
		t.Fatalf("reused order must not pay or save")
	}
}
