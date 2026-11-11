package payment

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"
)

// These literals are the frozen, redacted descriptions for the two trace-save
// markers. The test intentionally spells them out so the production path must
// use the same fixed text instead of leaking an error or claiming persistence.
const (
	traceFailureMessageFailed        = "本次追踪保存失败"
	traceFailureMessageIndeterminate = "保存状态未确认"
)

type traceFailureMarker struct {
	name    string
	err     error
	message string
}

func traceFailureMarkers() []traceFailureMarker {
	return []traceFailureMarker{
		{name: "trace_save_failed", err: ErrTraceSaveFailed, message: traceFailureMessageFailed},
		{name: "trace_save_indeterminate", err: ErrTraceSaveIndeterminate, message: traceFailureMessageIndeterminate},
	}
}

// traceFailureCommittedOrder builds the original committed confirmation that a
// recorder reads under its order lock before any write of the failed attempt.
func traceFailureCommittedOrder(base PaymentOrder, result Result) PaymentOrder {
	order := base
	order.Result = result
	at := queryTestOldTime()
	order.ConfirmedAt = &at
	if order.MockTransactionID != nil {
		id := *order.MockTransactionID
		order.MockTransactionID = &id
	} else {
		id := queryTestMockTxID
		order.MockTransactionID = &id
	}
	return order
}

// traceFailureStore models the recorder's save lock: the returned copy is
// taken while holding the lock and never aliases the caller's order.
type traceFailureStore struct {
	mu    sync.Mutex
	order PaymentOrder
}

func (s *traceFailureStore) markerSave(marker error) func(context.Context, Observation) (SaveObservationResult, error) {
	return func(context.Context, Observation) (SaveObservationResult, error) {
		s.mu.Lock()
		defer s.mu.Unlock()
		return SaveObservationResult{Order: clonePaymentOrder(s.order)}, marker
	}
}

func traceFailureAssertIssues(t *testing.T, got []Issue, wantCodes []string, wantMessage string) {
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

func traceFailureNoReadback(t *testing.T, deps *queryTestDeps, base PaymentOrder) {
	t.Helper()
	calls := 0
	deps.lookup.byIDFn = func(context.Context, string, PaymentOrderID) (PaymentOrder, bool, error) {
		calls++
		if calls != 1 {
			t.Fatalf("marker path must not perform a readback lookup")
		}
		return base, true, nil
	}
}

func traceFailureAssertNoExtraWork(t *testing.T, deps *queryTestDeps, base PaymentOrder) {
	t.Helper()
	if deps.lookup.byIDCalls != 1 || deps.lookup.byBusinessCalls != 0 {
		t.Fatalf("expected exactly the initial lookup, got %d by-ID and %d by-business calls",
			deps.lookup.byIDCalls, deps.lookup.byBusinessCalls)
	}
	if deps.mock.calls != 1 {
		t.Fatalf("expected exactly one Mock lookup, got %d", deps.mock.calls)
	}
	if deps.recorder.calls != 1 {
		t.Fatalf("expected exactly one save attempt, got %d", deps.recorder.calls)
	}
}

func TestQueryTraceFailureProtection(t *testing.T) {
	t.Run("marker_returns_original_confirmation_without_readback", func(t *testing.T) {
		scenarios := []struct {
			name       string
			mockFound  bool
			mockErr    error
			mockTx     func(PaymentOrder) MockTransaction
			wantIssues []string
		}{
			{
				name:      "accepted_confirmation",
				mockFound: true,
				mockTx: func(order PaymentOrder) MockTransaction {
					return queryTestTransaction(order, ResultSucceeded)
				},
				wantIssues: []string{IssueCodeTraceSaveError},
			},
			{
				name:       "mock_fault",
				mockErr:    ErrUnavailable,
				wantIssues: []string{IssueCodeMockResultUnconfirmed, IssueCodeTraceSaveError},
			},
			{
				name:       "mock_not_found",
				mockFound:  false,
				wantIssues: []string{IssueCodeMockResultNotFound, IssueCodeTraceSaveError},
			},
			{
				name:      "evidence_mismatch",
				mockFound: true,
				mockTx: func(order PaymentOrder) MockTransaction {
					tx := queryTestTransaction(order, ResultSucceeded)
					tx.Amount = 101
					return tx
				},
				wantIssues: []string{IssueCodeMockEvidenceRejected, IssueCodeTraceSaveError},
			},
		}

		for _, marker := range traceFailureMarkers() {
			marker := marker
			for _, scenario := range scenarios {
				scenario := scenario
				for _, result := range []Result{ResultSucceeded, ResultFailed} {
					result := result
					t.Run(marker.name+"/"+scenario.name+"/"+string(result), func(t *testing.T) {
						base := queryTestUnknownOrder()
						committed := traceFailureCommittedOrder(base, result)
						deps := queryTestValidDeps(base)
						deps.mock.found = scenario.mockFound
						deps.mock.err = scenario.mockErr
						if scenario.mockTx != nil {
							deps.mock.transaction = scenario.mockTx(base)
						}
						deps.recorder.fn = (&traceFailureStore{order: committed}).markerSave(marker.err)
						traceFailureNoReadback(t, deps, base)

						got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
						if err != nil {
							t.Fatalf("expected the committed original fact to be returned")
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
						if got.Order.ConfirmedAt == nil {
							t.Fatalf("confirmed original must carry confirmed_at")
						}
						if got.Order.MockTransactionID == nil || committed.MockTransactionID == nil ||
							*got.Order.MockTransactionID != *committed.MockTransactionID {
							t.Fatalf("returned Mock transaction ID does not match the committed copy")
						}
						traceFailureAssertIssues(t, got.Issues, scenario.wantIssues, marker.message)
						traceFailureAssertNoExtraWork(t, deps, base)
						if scenario.name == "accepted_confirmation" {
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

	t.Run("marker_keeps_locked_original_result_over_candidate", func(t *testing.T) {
		pairs := []struct {
			name            string
			mockResult      Result
			committedResult Result
		}{
			{name: "candidate_succeeded_locked_failed", mockResult: ResultSucceeded, committedResult: ResultFailed},
			{name: "candidate_failed_locked_succeeded", mockResult: ResultFailed, committedResult: ResultSucceeded},
		}
		for _, pair := range pairs {
			pair := pair
			for _, marker := range traceFailureMarkers() {
				marker := marker
				t.Run(pair.name+"/"+marker.name, func(t *testing.T) {
					base := queryTestUnknownOrder()
					committed := traceFailureCommittedOrder(base, pair.committedResult)
					deps := queryTestValidDeps(base)
					deps.mock.found = true
					deps.mock.transaction = queryTestTransaction(base, pair.mockResult)
					deps.recorder.fn = (&traceFailureStore{order: committed}).markerSave(marker.err)
					traceFailureNoReadback(t, deps, base)

					got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
					if err != nil {
						t.Fatalf("expected the committed original fact to be returned")
					}
					if got.Order.Result != pair.committedResult {
						t.Fatalf("expected the locked original result %q, got %q", pair.committedResult, got.Order.Result)
					}
					traceFailureAssertIssues(t, got.Issues, []string{IssueCodeTraceSaveError}, marker.message)
					traceFailureAssertNoExtraWork(t, deps, base)
				})
			}
		}
	})

	t.Run("marker_mock_id_contract", func(t *testing.T) {
		t.Run("absent_when_unexpected", func(t *testing.T) {
			for _, marker := range traceFailureMarkers() {
				marker := marker
				t.Run(marker.name, func(t *testing.T) {
					base := queryTestUnknownOrder()
					committed := traceFailureCommittedOrder(base, ResultSucceeded)
					committed.MockTransactionID = nil
					deps := queryTestValidDeps(base)
					deps.mock.found = true
					deps.mock.transaction = queryTestTransaction(base, ResultSucceeded)
					deps.recorder.fn = (&traceFailureStore{order: committed}).markerSave(marker.err)
					traceFailureNoReadback(t, deps, base)

					got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
					if err != nil {
						t.Fatalf("expected a committed confirmation without a Mock ID")
					}
					if got.Order.Result != ResultSucceeded {
						t.Fatalf("expected the locked original result")
					}
					if got.Order.MockTransactionID != nil {
						t.Fatalf("must not fabricate a Mock transaction ID")
					}
					traceFailureAssertIssues(t, got.Issues, []string{IssueCodeTraceSaveError}, marker.message)
					traceFailureAssertNoExtraWork(t, deps, base)
				})
			}
		})

		t.Run("matching_initial_id", func(t *testing.T) {
			for _, marker := range traceFailureMarkers() {
				marker := marker
				t.Run(marker.name, func(t *testing.T) {
					base := queryTestUnknownOrder()
					id := queryTestMockTxID
					base.MockTransactionID = &id
					committed := traceFailureCommittedOrder(base, ResultFailed)
					deps := queryTestValidDeps(base)
					deps.mock.found = true
					deps.mock.transaction = queryTestTransaction(base, ResultFailed)
					deps.recorder.fn = (&traceFailureStore{order: committed}).markerSave(marker.err)
					traceFailureNoReadback(t, deps, base)

					got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
					if err != nil {
						t.Fatalf("expected a matching initial Mock ID to be accepted")
					}
					if got.Order.Result != ResultFailed {
						t.Fatalf("expected the locked original result")
					}
					if got.Order.MockTransactionID == nil || *got.Order.MockTransactionID != queryTestMockTxID {
						t.Fatalf("expected the matching initial Mock transaction ID")
					}
					traceFailureAssertIssues(t, got.Issues, []string{IssueCodeTraceSaveError}, marker.message)
					traceFailureAssertNoExtraWork(t, deps, base)
				})
			}
		})
	})

	t.Run("marker_returns_committed_fact_after_cancellation", func(t *testing.T) {
		for _, marker := range traceFailureMarkers() {
			marker := marker
			t.Run(marker.name, func(t *testing.T) {
				base := queryTestUnknownOrder()
				committed := traceFailureCommittedOrder(base, ResultSucceeded)
				deps := queryTestValidDeps(base)
				deps.mock.found = true
				deps.mock.transaction = queryTestTransaction(base, ResultSucceeded)
				lifecycle, cancelLifecycle := context.WithCancel(context.Background())
				defer cancelLifecycle()
				clientCtx, cancelClient := context.WithCancel(context.Background())
				defer cancelClient()
				var saveDeadline time.Time
				deps.recorder.fn = func(ctx context.Context, _ Observation) (SaveObservationResult, error) {
					saveDeadline, _ = ctx.Deadline()
					cancelClient()
					cancelLifecycle()
					return SaveObservationResult{Order: clonePaymentOrder(committed)}, marker.err
				}
				traceFailureNoReadback(t, deps, base)

				got, err := deps.service(lifecycle).Query(clientCtx, queryTestRequestID, queryTestInputByID())
				if err != nil {
					t.Fatalf("expected the committed original fact to survive cancellation")
				}
				if got.Order.Result != ResultSucceeded {
					t.Fatalf("expected the locked original result")
				}
				traceFailureAssertIssues(t, got.Issues, []string{IssueCodeTraceSaveError}, marker.message)
				if saveDeadline.IsZero() {
					t.Fatalf("expected the existing finish window to bound the save")
				}
				traceFailureAssertNoExtraWork(t, deps, base)
			})
		}
	})

	t.Run("illegal_marker_order_is_rejected", func(t *testing.T) {
		cases := []struct {
			name    string
			prepare func(*PaymentOrder)
			mutate  func(*PaymentOrder)
		}{
			{name: "invalid_order_id", mutate: func(o *PaymentOrder) { o.ID = "" }},
			{name: "other_order_id", mutate: func(o *PaymentOrder) { o.ID = queryTestOtherOrderID }},
			{name: "client_mismatch", mutate: func(o *PaymentOrder) { o.ClientID = "client_other" }},
			{name: "business_no_mismatch", mutate: func(o *PaymentOrder) { o.BusinessOrderNo = "M-QRY-OTHER" }},
			{name: "amount_mismatch", mutate: func(o *PaymentOrder) { o.Amount++ }},
			{name: "currency_mismatch", mutate: func(o *PaymentOrder) { o.Currency = "USD" }},
			{name: "registered_at_mismatch", mutate: func(o *PaymentOrder) { o.RegisteredAt = o.RegisteredAt.Add(time.Second) }},
			{name: "registered_at_non_utc", mutate: func(o *PaymentOrder) {
				o.RegisteredAt = o.RegisteredAt.In(time.FixedZone("TRACE", 3600))
			}},
			{name: "result_unknown", mutate: func(o *PaymentOrder) {
				o.Result = ResultUnknown
				o.ConfirmedAt = nil
			}},
			{name: "result_empty", mutate: func(o *PaymentOrder) {
				o.Result = ""
				o.ConfirmedAt = nil
			}},
			{name: "confirmed_at_missing", mutate: func(o *PaymentOrder) { o.ConfirmedAt = nil }},
			{name: "confirmed_at_zero", mutate: func(o *PaymentOrder) {
				at := time.Time{}
				o.ConfirmedAt = &at
			}},
			{name: "confirmed_at_non_utc", mutate: func(o *PaymentOrder) {
				at := o.ConfirmedAt.In(time.FixedZone("TRACE", 3600))
				o.ConfirmedAt = &at
			}},
			{
				name: "mock_id_conflict_with_initial",
				prepare: func(o *PaymentOrder) {
					id := queryTestMockTxID
					o.MockTransactionID = &id
				},
				mutate: func(o *PaymentOrder) {
					id := queryTestOtherMockID
					o.MockTransactionID = &id
				},
			},
			{
				name: "mock_id_missing_when_initial_expected",
				prepare: func(o *PaymentOrder) {
					id := queryTestMockTxID
					o.MockTransactionID = &id
				},
				mutate: func(o *PaymentOrder) { o.MockTransactionID = nil },
			},
			{name: "mock_id_invalid_when_unexpected", mutate: func(o *PaymentOrder) {
				id := MockTransactionID("not-a-uuid")
				o.MockTransactionID = &id
			}},
		}

		for _, tc := range cases {
			tc := tc
			for _, marker := range traceFailureMarkers() {
				marker := marker
				t.Run(tc.name+"/"+marker.name, func(t *testing.T) {
					base := queryTestUnknownOrder()
					if tc.prepare != nil {
						tc.prepare(&base)
					}
					committed := traceFailureCommittedOrder(base, ResultSucceeded)
					tc.mutate(&committed)
					deps := queryTestValidDeps(base)
					deps.mock.found = true
					deps.mock.transaction = queryTestTransaction(base, ResultSucceeded)
					deps.recorder.fn = (&traceFailureStore{order: committed}).markerSave(marker.err)
					traceFailureNoReadback(t, deps, base)

					got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
					if !errors.Is(err, ErrUnavailable) {
						t.Fatalf("expected an illegal marker to fail safely, got %v", err)
					}
					if got.Order.ID != "" || len(got.Issues) != 0 {
						t.Fatalf("illegal marker must not return the candidate or an error ID")
					}
					traceFailureAssertNoExtraWork(t, deps, base)
				})
			}
		}
	})

	t.Run("illegal_marker_record_is_rejected", func(t *testing.T) {
		cases := []struct {
			name   string
			mutate func(*ResultHandlingRecord)
		}{
			{name: "record_id", mutate: func(r *ResultHandlingRecord) { r.ID = ResultHandlingRecordID(queryTestMockTxID) }},
			{name: "payment_order_id", mutate: func(r *ResultHandlingRecord) { r.PaymentOrderID = queryTestOrderID }},
			{name: "request_id", mutate: func(r *ResultHandlingRecord) { r.RequestID = queryTestRequestID }},
			{name: "source", mutate: func(r *ResultHandlingRecord) { r.Source = SourceQuery }},
			{name: "observed_at", mutate: func(r *ResultHandlingRecord) { r.ObservedAt = queryTestTime() }},
			{name: "evidence_platform_reference", mutate: func(r *ResultHandlingRecord) {
				reference := queryTestOrderID
				r.Evidence.PlatformReferenceID = &reference
			}},
			{name: "evidence_mock_transaction_id", mutate: func(r *ResultHandlingRecord) {
				id := queryTestMockTxID
				r.Evidence.MockTransactionID = &id
			}},
			{name: "evidence_amount", mutate: func(r *ResultHandlingRecord) {
				amount := int64(100)
				r.Evidence.Amount = &amount
			}},
			{name: "evidence_currency", mutate: func(r *ResultHandlingRecord) {
				currency := "CNY"
				r.Evidence.Currency = &currency
			}},
			{name: "evidence_result", mutate: func(r *ResultHandlingRecord) {
				result := ResultSucceeded
				r.Evidence.Result = &result
			}},
			{name: "evidence_differences", mutate: func(r *ResultHandlingRecord) {
				r.Evidence.Differences = []EvidenceFieldName{EvidenceFieldAmount}
			}},
			{name: "result_before", mutate: func(r *ResultHandlingRecord) { r.ResultBefore = ResultUnknown }},
			{name: "result_after", mutate: func(r *ResultHandlingRecord) { r.ResultAfter = ResultSucceeded }},
			{name: "decision", mutate: func(r *ResultHandlingRecord) { r.Decision = DecisionAcceptConfirmation }},
			{name: "reason_code", mutate: func(r *ResultHandlingRecord) {
				code := reasonCodeEvidenceMismatch
				r.ReasonCode = &code
			}},
			{name: "reason_message", mutate: func(r *ResultHandlingRecord) {
				message := reasonMessageEvidenceMismatch
				r.ReasonMessage = &message
			}},
		}

		for _, tc := range cases {
			tc := tc
			for _, marker := range traceFailureMarkers() {
				marker := marker
				t.Run(tc.name+"/"+marker.name, func(t *testing.T) {
					base := queryTestUnknownOrder()
					committed := traceFailureCommittedOrder(base, ResultSucceeded)
					var record ResultHandlingRecord
					tc.mutate(&record)
					deps := queryTestValidDeps(base)
					deps.mock.found = true
					deps.mock.transaction = queryTestTransaction(base, ResultSucceeded)
					deps.recorder.fn = func(context.Context, Observation) (SaveObservationResult, error) {
						return SaveObservationResult{Order: committed, Record: record}, marker.err
					}
					traceFailureNoReadback(t, deps, base)

					got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
					if !errors.Is(err, ErrUnavailable) {
						t.Fatalf("expected a non-empty Record to fail safely, got %v", err)
					}
					if got.Order.ID != "" || len(got.Issues) != 0 {
						t.Fatalf("non-empty Record must not be returned")
					}
					traceFailureAssertNoExtraWork(t, deps, base)
				})
			}
		}
	})

	t.Run("marker_preserves_known_confirmed_fact", func(t *testing.T) {
		changedAt := queryTestTime()
		nonUTCAt := queryTestOldTime().In(time.FixedZone("TRACE", 3600))
		cases := []struct {
			name     string
			original PaymentOrder
			current  PaymentOrder
			wantErr  error
		}{
			{
				name:     "confirmed_succeeded_same_result_and_time",
				original: queryTestConfirmedOrder(ResultSucceeded),
				current:  queryTestConfirmedOrder(ResultSucceeded),
			},
			{
				name:     "confirmed_failed_same_result_and_time",
				original: queryTestConfirmedOrder(ResultFailed),
				current:  queryTestConfirmedOrder(ResultFailed),
			},
			{
				name:     "confirmed_original_opposite_result_rejected",
				original: queryTestConfirmedOrder(ResultSucceeded),
				current:  queryTestConfirmedOrder(ResultFailed),
				wantErr:  ErrUnavailable,
			},
			{
				name:     "confirmed_original_changed_time_rejected",
				original: queryTestConfirmedOrder(ResultSucceeded),
				current: func() PaymentOrder {
					order := queryTestConfirmedOrder(ResultSucceeded)
					order.ConfirmedAt = &changedAt
					return order
				}(),
				wantErr: ErrUnavailable,
			},
			{
				name: "confirmed_original_missing_time_rejected",
				original: func() PaymentOrder {
					order := queryTestConfirmedOrder(ResultSucceeded)
					order.ConfirmedAt = nil
					return order
				}(),
				current: queryTestConfirmedOrder(ResultSucceeded),
				wantErr: ErrUnavailable,
			},
			{
				name: "confirmed_original_non_utc_time_rejected",
				original: func() PaymentOrder {
					order := queryTestConfirmedOrder(ResultSucceeded)
					order.ConfirmedAt = &nonUTCAt
					return order
				}(),
				current: queryTestConfirmedOrder(ResultSucceeded),
				wantErr: ErrUnavailable,
			},
			{
				name:     "unknown_original_new_confirmation_allowed",
				original: queryTestUnknownOrder(),
				current:  queryTestConfirmedOrder(ResultSucceeded),
			},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				err := validateTraceFailureResult(tc.original, SaveObservationResult{Order: tc.current})
				if !errors.Is(err, tc.wantErr) {
					t.Fatalf("unexpected trace-failure validation result: %v", err)
				}
			})
		}
	})

	t.Run("wrapped_marker_is_recognized", func(t *testing.T) {
		for _, marker := range traceFailureMarkers() {
			marker := marker
			t.Run(marker.name, func(t *testing.T) {
				base := queryTestUnknownOrder()
				committed := traceFailureCommittedOrder(base, ResultFailed)
				deps := queryTestValidDeps(base)
				deps.mock.found = true
				deps.mock.transaction = queryTestTransaction(base, ResultFailed)
				deps.recorder.fn = func(context.Context, Observation) (SaveObservationResult, error) {
					return SaveObservationResult{Order: committed}, fmt.Errorf("wrapped marker: %w", marker.err)
				}
				traceFailureNoReadback(t, deps, base)

				got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
				if err != nil {
					t.Fatalf("expected a wrapped marker to be recognized")
				}
				if got.Order.Result != ResultFailed {
					t.Fatalf("expected the locked original result")
				}
				traceFailureAssertIssues(t, got.Issues, []string{IssueCodeTraceSaveError}, marker.message)
				traceFailureAssertNoExtraWork(t, deps, base)
			})
		}
	})

	t.Run("arbitrary_error_result_is_not_trusted", func(t *testing.T) {
		base := queryTestUnknownOrder()
		committed := traceFailureCommittedOrder(base, ResultSucceeded)
		deps := queryTestValidDeps(base)
		deps.mock.found = true
		deps.mock.transaction = queryTestTransaction(base, ResultSucceeded)
		deps.recorder.fn = func(context.Context, Observation) (SaveObservationResult, error) {
			return SaveObservationResult{Order: committed}, errors.New("ordinary storage fault")
		}
		readbackCalls := 0
		deps.lookup.byIDFn = func(context.Context, string, PaymentOrderID) (PaymentOrder, bool, error) {
			readbackCalls++
			if readbackCalls == 1 {
				return base, true, nil
			}
			return base, true, nil
		}

		got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
		if err != nil {
			t.Fatalf("expected the ordinary readback path to complete")
		}
		if got.Order.Result != ResultUnknown {
			t.Fatalf("an ordinary error's attached Order must not be trusted")
		}
		traceFailureAssertIssues(t, got.Issues, []string{IssueCodeResultSaveError}, "")
		if readbackCalls != 2 {
			t.Fatalf("expected the ordinary readback lookup, got %d calls", readbackCalls)
		}
	})

	t.Run("ordinary_baselines_remain_unchanged", func(t *testing.T) {
		t.Run("confirmed_direct", func(t *testing.T) {
			order := queryTestConfirmedOrder(ResultSucceeded)
			deps := queryTestValidDeps(order)
			got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
			if err != nil {
				t.Fatalf("expected nil error")
			}
			if got.Order.Result != ResultSucceeded {
				t.Fatalf("expected the confirmed order directly")
			}
			queryTestAssertNoIssues(t, got.Issues)
			if deps.lookup.byIDCalls != 1 || deps.mock.calls != 0 || deps.recorder.calls != 0 {
				t.Fatalf("confirmed query must not call Mock or save")
			}
		})

		t.Run("no_mock_confirmation", func(t *testing.T) {
			base := queryTestUnknownOrder()
			deps := queryTestValidDeps(base)
			deps.mock.found = false
			deps.recorder.fn = queryTestDecideRecorder(base)
			got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
			if err != nil {
				t.Fatalf("expected nil error")
			}
			if got.Order.Result != ResultUnknown {
				t.Fatalf("expected the saved UNKNOWN result")
			}
			queryTestAssertIssues(t, got.Issues, IssueCodeMockResultNotFound)
			if deps.mock.calls != 1 || deps.recorder.calls != 1 {
				t.Fatalf("expected one Mock query and one save")
			}
		})

		t.Run("non_marker_readback_unknown", func(t *testing.T) {
			base := queryTestUnknownOrder()
			deps := queryTestValidDeps(base)
			deps.mock.err = ErrUnavailable
			deps.recorder.err = ErrIndeterminate
			readbackCalls := 0
			deps.lookup.byIDFn = func(context.Context, string, PaymentOrderID) (PaymentOrder, bool, error) {
				readbackCalls++
				if readbackCalls == 1 {
					return base, true, nil
				}
				return base, true, nil
			}
			got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
			if err != nil {
				t.Fatalf("expected nil error")
			}
			if got.Order.Result != ResultUnknown {
				t.Fatalf("expected the read-back UNKNOWN result")
			}
			traceFailureAssertIssues(t, got.Issues,
				[]string{IssueCodeMockResultUnconfirmed, IssueCodeTraceSaveError}, "")
			if readbackCalls != 2 {
				t.Fatalf("expected one ordinary readback, got %d calls", readbackCalls)
			}
		})

		t.Run("non_marker_readback_confirmation", func(t *testing.T) {
			base := queryTestUnknownOrder()
			confirmed := queryTestConfirmedOrder(ResultSucceeded)
			deps := queryTestValidDeps(base)
			deps.mock.found = true
			deps.mock.transaction = queryTestTransaction(base, ResultSucceeded)
			deps.recorder.err = ErrIndeterminate
			readbackCalls := 0
			deps.lookup.byIDFn = func(context.Context, string, PaymentOrderID) (PaymentOrder, bool, error) {
				readbackCalls++
				if readbackCalls == 1 {
					return base, true, nil
				}
				return confirmed, true, nil
			}
			got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
			if err != nil {
				t.Fatalf("expected nil error")
			}
			if got.Order.Result != ResultSucceeded {
				t.Fatalf("expected the ordinary read-back confirmation")
			}
			traceFailureAssertIssues(t, got.Issues, []string{IssueCodeResultSaveError}, "")
			if readbackCalls != 2 {
				t.Fatalf("expected one ordinary readback, got %d calls", readbackCalls)
			}
		})
	})
}
