package payment

import (
	"context"
	"errors"
	"strings"
	"testing"
)

// reconcileTestQueryObservation builds a fixed QUERY observation for direct
// decision tests. QueryService builds its own observations in the integration
// style tests; this helper is only for the four-decision table.
func reconcileTestQueryObservation(order PaymentOrder, requestID RequestID) Observation {
	return Observation{
		ID:             ResultHandlingRecordID(NewUUIDv4()),
		PaymentOrderID: order.ID,
		ClientID:       order.ClientID,
		RequestID:      requestID,
		Source:         SourceQuery,
		ObservedAt:     queryTestTime(),
	}
}

func reconcileTestAssertDecision(t *testing.T, record ResultHandlingRecord, decision HandlingDecision, before, after Result) {
	t.Helper()
	if record.Decision != decision {
		t.Fatalf("decision = %q, want %q", record.Decision, decision)
	}
	if record.ResultBefore != before {
		t.Fatalf("result_before = %q, want %q", record.ResultBefore, before)
	}
	if record.ResultAfter != after {
		t.Fatalf("result_after = %q, want %q", record.ResultAfter, after)
	}
	if err := ValidateHandlingDecision(record.Decision); err != nil {
		t.Fatalf("invalid handling decision")
	}
	if err := ValidateResult(record.ResultBefore); err != nil {
		t.Fatalf("invalid result_before")
	}
	if err := ValidateResult(record.ResultAfter); err != nil {
		t.Fatalf("invalid result_after")
	}
}

func reconcileTestAssertReason(t *testing.T, record ResultHandlingRecord, wantCode string) {
	t.Helper()
	if record.ReasonCode == nil {
		t.Fatalf("expected a reason code")
	}
	if *record.ReasonCode != wantCode {
		t.Fatalf("reason code = %q, want %q", *record.ReasonCode, wantCode)
	}
	if record.ReasonMessage == nil {
		t.Fatalf("expected a reason message")
	}
	if err := ValidateReason(&Reason{Code: *record.ReasonCode, Message: record.ReasonMessage}); err != nil {
		t.Fatalf("reason is not sanitized")
	}
}

func reconcileTestAssertEvidenceAllowed(t *testing.T, evidence Evidence) {
	t.Helper()
	if evidence.PlatformReferenceID != nil {
		if ValidateUUIDv4(string(*evidence.PlatformReferenceID)) != nil {
			t.Fatalf("invalid platform reference in evidence")
		}
	}
	if evidence.MockTransactionID != nil {
		if ValidateUUIDv4(string(*evidence.MockTransactionID)) != nil {
			t.Fatalf("invalid Mock transaction ID in evidence")
		}
	}
	if evidence.Amount != nil {
		if ValidateAmount(*evidence.Amount) != nil {
			t.Fatalf("invalid amount in evidence")
		}
	}
	if evidence.Currency != nil {
		if ValidateCurrency(*evidence.Currency) != nil {
			t.Fatalf("invalid currency in evidence")
		}
	}
	if evidence.Result != nil {
		if *evidence.Result != ResultSucceeded && *evidence.Result != ResultFailed {
			t.Fatalf("invalid result in evidence")
		}
	}
	seen := make(map[EvidenceFieldName]bool, len(evidence.Differences))
	for _, difference := range evidence.Differences {
		if !validEvidenceField(difference) {
			t.Fatalf("evidence contains a non-whitelisted difference")
		}
		if seen[difference] {
			t.Fatalf("evidence contains a duplicate difference")
		}
		seen[difference] = true
	}
}

func reconcileTestEvidenceEmpty(evidence Evidence) bool {
	return evidence.PlatformReferenceID == nil &&
		evidence.MockTransactionID == nil &&
		evidence.Amount == nil &&
		evidence.Currency == nil &&
		evidence.Result == nil &&
		len(evidence.Differences) == 0
}

func reconcileTestAssertOrderFact(t *testing.T, got, want PaymentOrder) {
	t.Helper()
	if got.ID != want.ID ||
		got.ClientID != want.ClientID ||
		got.BusinessOrderNo != want.BusinessOrderNo ||
		got.Amount != want.Amount ||
		got.Currency != want.Currency ||
		got.Result != want.Result {
		t.Fatalf("returned order facts do not match the expected original fact")
	}
	if want.ConfirmedAt == nil {
		if got.ConfirmedAt != nil {
			t.Fatalf("unexpected confirmed_at")
		}
	} else if got.ConfirmedAt == nil || !got.ConfirmedAt.Equal(*want.ConfirmedAt) {
		t.Fatalf("confirmed_at does not match the expected fact")
	}
	if want.MockTransactionID == nil {
		if got.MockTransactionID != nil {
			t.Fatalf("unexpected Mock transaction ID")
		}
	} else if got.MockTransactionID == nil || *got.MockTransactionID != *want.MockTransactionID {
		t.Fatalf("Mock transaction ID does not match the expected fact")
	}
}

// TestPaymentReconciliation is the frozen US3 payment-level reconciliation
// root. It uses the existing in-memory payment fakes only: no PostgreSQL, no
// Mock server, no payer, no registrar, and no sleeping/random waiting.
func TestPaymentReconciliation(t *testing.T) {
	t.Run("confirmed_query_returns_current_fact_without_mock", func(t *testing.T) {
		for _, result := range []Result{ResultSucceeded, ResultFailed} {
			for _, locator := range []struct {
				name  string
				input QueryInput
				byID  bool
			}{
				{name: "by_id", input: queryTestInputByID(), byID: true},
				{name: "by_business_order_no", input: queryTestInputByBusiness(), byID: false},
			} {
				result, locator := result, locator
				t.Run(string(result)+"/"+locator.name, func(t *testing.T) {
					order := queryTestConfirmedOrder(result)
					deps := queryTestValidDeps(order)
					opposite := ResultSucceeded
					if result == ResultSucceeded {
						opposite = ResultFailed
					}
					deps.mock.transaction = queryTestTransaction(order, opposite)
					deps.mock.found = true

					got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, locator.input)
					if err != nil {
						t.Fatalf("confirmed query returned an error")
					}
					reconcileTestAssertOrderFact(t, got.Order, order)
					queryTestAssertNoIssues(t, got.Issues)
					if deps.mock.calls != 0 || deps.recorder.calls != 0 {
						t.Fatalf("ordinary confirmed query must not call Mock or save")
					}
					if locator.byID {
						if deps.lookup.byIDCalls != 1 || deps.lookup.byBusinessCalls != 0 {
							t.Fatalf("expected exactly one lookup by ID")
						}
					} else {
						if deps.lookup.byBusinessCalls != 1 || deps.lookup.byIDCalls != 0 {
							t.Fatalf("expected exactly one lookup by business order number")
						}
					}
				})
			}
		}
	})

	t.Run("unknown_matching_evidence_accepts_and_saves_minimum_evidence", func(t *testing.T) {
		for _, result := range []Result{ResultSucceeded, ResultFailed} {
			result := result
			t.Run(string(result), func(t *testing.T) {
				base := queryTestUnknownOrder()
				deps := queryTestValidDeps(base)
				deps.mock.transaction = queryTestTransaction(base, result)
				deps.mock.found = true
				deps.recorder.fn = queryTestDecideRecorder(base)

				got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
				if err != nil {
					t.Fatalf("matching reconciliation returned an error")
				}
				if got.Order.Result != result || got.Order.ConfirmedAt == nil {
					t.Fatalf("matching evidence must confirm the order")
				}
				if got.Order.MockTransactionID == nil || *got.Order.MockTransactionID != queryTestMockTxID {
					t.Fatalf("matching evidence must save the Mock transaction ID")
				}
				queryTestAssertNoIssues(t, got.Issues)
				if deps.lookup.byIDCalls != 1 || deps.mock.calls != 1 || deps.recorder.calls != 1 {
					t.Fatalf("expected one lookup, one Mock query, and one save")
				}
				if deps.mock.reference != base.ID {
					t.Fatalf("Mock query did not use the platform order ID")
				}

				observation := deps.recorder.observation
				if observation.Source != SourceQuery ||
					observation.PaymentOrderID != base.ID ||
					observation.ClientID != base.ClientID ||
					observation.RequestID != queryTestRequestID {
					t.Fatalf("query observation identity mismatch")
				}
				if observation.Candidate == nil || observation.Candidate.Result != result {
					t.Fatalf("expected a matching candidate")
				}
				if observation.Candidate.MockTransactionID == nil || *observation.Candidate.MockTransactionID != queryTestMockTxID {
					t.Fatalf("candidate Mock transaction ID mismatch")
				}
				if observation.EvidenceRejected || observation.Reason != nil {
					t.Fatalf("matching evidence must not be marked rejected")
				}
				reconcileTestAssertEvidenceAllowed(t, observation.Evidence)
				if observation.Evidence.PlatformReferenceID == nil || *observation.Evidence.PlatformReferenceID != base.ID {
					t.Fatalf("evidence platform reference mismatch")
				}
				if observation.Evidence.MockTransactionID == nil || *observation.Evidence.MockTransactionID != queryTestMockTxID {
					t.Fatalf("evidence Mock transaction ID mismatch")
				}
				if observation.Evidence.Amount == nil || *observation.Evidence.Amount != base.Amount {
					t.Fatalf("evidence amount mismatch")
				}
				if observation.Evidence.Currency == nil || *observation.Evidence.Currency != base.Currency {
					t.Fatalf("evidence currency mismatch")
				}
				if observation.Evidence.Result == nil || *observation.Evidence.Result != result {
					t.Fatalf("evidence result mismatch")
				}
				if len(observation.Evidence.Differences) != 0 {
					t.Fatalf("accepted evidence must not contain differences")
				}

				record := deps.recorder.saved.Record
				reconcileTestAssertDecision(t, record, DecisionAcceptConfirmation, ResultUnknown, result)
				if record.ReasonCode != nil || record.ReasonMessage != nil {
					t.Fatalf("accepted confirmation must not carry a reason")
				}
				reconcileTestAssertEvidenceAllowed(t, record.Evidence)
				if record.Evidence.PlatformReferenceID == nil || *record.Evidence.PlatformReferenceID != base.ID {
					t.Fatalf("saved evidence platform reference mismatch")
				}
				if record.Evidence.Amount == nil || *record.Evidence.Amount != base.Amount {
					t.Fatalf("saved evidence amount mismatch")
				}
				if record.Evidence.Currency == nil || *record.Evidence.Currency != base.Currency {
					t.Fatalf("saved evidence currency mismatch")
				}
				if record.Evidence.Result == nil || *record.Evidence.Result != result {
					t.Fatalf("saved evidence result mismatch")
				}
			})
		}
	})

	t.Run("unknown_business_mismatch_rejects_without_confirmation", func(t *testing.T) {
		cases := []struct {
			name            string
			mutate          func(*MockTransaction)
			wantDiff        EvidenceFieldName
			wantReason      string
			wantMockNil     bool
			wantAmountNil   bool
			wantCurrencyNil bool
			wantResultNil   bool
		}{
			{
				name:       "platform_reference_mismatch",
				mutate:     func(tx *MockTransaction) { tx.PlatformReferenceID = queryTestOtherOrderID },
				wantDiff:   EvidenceFieldPlatformReferenceID,
				wantReason: reasonCodeEvidenceMismatch,
			},
			{
				name:       "amount_mismatch",
				mutate:     func(tx *MockTransaction) { tx.Amount = 101 },
				wantDiff:   EvidenceFieldAmount,
				wantReason: reasonCodeEvidenceMismatch,
			},
			{
				name:            "currency_invalid",
				mutate:          func(tx *MockTransaction) { tx.Currency = "USD" },
				wantDiff:        EvidenceFieldCurrency,
				wantReason:      reasonCodeEvidenceInvalid,
				wantCurrencyNil: true,
			},
			{
				name:        "mock_transaction_id_invalid",
				mutate:      func(tx *MockTransaction) { tx.ID = "not-a-uuid" },
				wantDiff:    EvidenceFieldMockTransactionID,
				wantReason:  reasonCodeEvidenceInvalid,
				wantMockNil: true,
			},
			{
				name:          "result_unknown",
				mutate:        func(tx *MockTransaction) { tx.Result = ResultUnknown },
				wantDiff:      EvidenceFieldResult,
				wantReason:    reasonCodeEvidenceInvalid,
				wantResultNil: true,
			},
		}

		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				base := queryTestUnknownOrder()
				transaction := queryTestTransaction(base, ResultSucceeded)
				tc.mutate(&transaction)
				deps := queryTestValidDeps(base)
				deps.mock.transaction = transaction
				deps.mock.found = true
				deps.recorder.fn = queryTestDecideRecorder(base)

				got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
				if err != nil {
					t.Fatalf("mismatch reconciliation returned an error")
				}
				if got.Order.Result != ResultUnknown {
					t.Fatalf("mismatch must not confirm the order")
				}
				queryTestAssertIssues(t, got.Issues, IssueCodeMockEvidenceRejected)
				if deps.lookup.byIDCalls != 1 || deps.mock.calls != 1 || deps.recorder.calls != 1 {
					t.Fatalf("expected one lookup, one Mock query, and one save")
				}

				observation := deps.recorder.observation
				if observation.Candidate != nil || !observation.EvidenceRejected {
					t.Fatalf("mismatch must be rejected without a candidate")
				}
				if observation.Reason == nil || observation.Reason.Code != tc.wantReason {
					t.Fatalf("unexpected observation reason")
				}
				reconcileTestAssertEvidenceAllowed(t, observation.Evidence)
				if len(observation.Evidence.Differences) != 1 || observation.Evidence.Differences[0] != tc.wantDiff {
					t.Fatalf("expected exactly the %q difference", tc.wantDiff)
				}
				if tc.wantMockNil && observation.Evidence.MockTransactionID != nil {
					t.Fatalf("invalid Mock transaction ID must be omitted")
				}
				if tc.wantAmountNil && observation.Evidence.Amount != nil {
					t.Fatalf("invalid amount must be omitted")
				}
				if tc.wantCurrencyNil && observation.Evidence.Currency != nil {
					t.Fatalf("invalid currency must be omitted")
				}
				if tc.wantResultNil && observation.Evidence.Result != nil {
					t.Fatalf("invalid result must be omitted")
				}

				record := deps.recorder.saved.Record
				reconcileTestAssertDecision(t, record, DecisionRejectEvidence, ResultUnknown, ResultUnknown)
				reconcileTestAssertReason(t, record, tc.wantReason)
				reconcileTestAssertEvidenceAllowed(t, record.Evidence)
				if len(record.Evidence.Differences) != 1 || record.Evidence.Differences[0] != tc.wantDiff {
					t.Fatalf("saved evidence must retain exactly the %q difference", tc.wantDiff)
				}
			})
		}
	})

	t.Run("not_found_unreadable_and_mock_no_match_are_distinct", func(t *testing.T) {
		base := queryTestUnknownOrder()

		t.Run("platform_not_found", func(t *testing.T) {
			deps := queryTestValidDeps(base)
			deps.lookup.byIDFound = false

			got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
			if !errors.Is(err, ErrNotFound) {
				t.Fatalf("expected platform not found")
			}
			if errors.Is(err, ErrUnavailable) {
				t.Fatalf("platform not found must not be unreadable")
			}
			if got.Order.ID != "" || len(got.Issues) != 0 {
				t.Fatalf("platform not found must not produce a result")
			}
			if deps.mock.calls != 0 || deps.recorder.calls != 0 {
				t.Fatalf("platform not found must not call Mock or save")
			}
		})

		t.Run("platform_unreadable", func(t *testing.T) {
			deps := queryTestValidDeps(base)
			deps.lookup.byIDErr = ErrUnavailable

			got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
			if !errors.Is(err, ErrUnavailable) {
				t.Fatalf("expected platform unreadable")
			}
			if errors.Is(err, ErrNotFound) {
				t.Fatalf("platform unreadable must not be reported as not found")
			}
			if got.Order.ID != "" || len(got.Issues) != 0 {
				t.Fatalf("platform unreadable must not produce a result")
			}
			if deps.mock.calls != 0 || deps.recorder.calls != 0 {
				t.Fatalf("platform unreadable must not call Mock or save")
			}
		})

		t.Run("mock_not_found", func(t *testing.T) {
			deps := queryTestValidDeps(base)
			deps.mock.found = false
			deps.recorder.fn = queryTestDecideRecorder(base)

			got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
			if err != nil {
				t.Fatalf("Mock no-match must be reported as an issue, not an error")
			}
			if errors.Is(err, ErrNotFound) {
				t.Fatalf("Mock no-match must not be platform not found")
			}
			if got.Order.ID != base.ID || got.Order.Result != ResultUnknown {
				t.Fatalf("Mock no-match must keep the original UNKNOWN order")
			}
			queryTestAssertIssues(t, got.Issues, IssueCodeMockResultNotFound)
			if deps.lookup.byIDCalls != 1 || deps.mock.calls != 1 || deps.recorder.calls != 1 {
				t.Fatalf("expected one lookup, one Mock query, and one save")
			}
			observation := deps.recorder.observation
			if observation.Candidate != nil || observation.EvidenceRejected {
				t.Fatalf("Mock no-match must not produce a candidate or rejection")
			}
			if observation.Reason == nil || observation.Reason.Code != IssueCodeMockResultNotFound {
				t.Fatalf("expected the Mock no-match reason")
			}
			if !reconcileTestEvidenceEmpty(observation.Evidence) {
				t.Fatalf("Mock no-match must not fabricate evidence")
			}
			record := deps.recorder.saved.Record
			reconcileTestAssertDecision(t, record, DecisionKeepUnknown, ResultUnknown, ResultUnknown)
			reconcileTestAssertReason(t, record, IssueCodeMockResultNotFound)
			if !reconcileTestEvidenceEmpty(record.Evidence) {
				t.Fatalf("saved no-match evidence must remain empty")
			}
		})

		t.Run("mock_fault", func(t *testing.T) {
			deps := queryTestValidDeps(base)
			deps.mock.err = ErrUnavailable
			deps.recorder.fn = queryTestDecideRecorder(base)

			got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
			if err != nil {
				t.Fatalf("Mock fault must be reported as an issue, not an error")
			}
			if got.Order.ID != base.ID || got.Order.Result != ResultUnknown {
				t.Fatalf("Mock fault must keep the original UNKNOWN order")
			}
			queryTestAssertIssues(t, got.Issues, IssueCodeMockResultUnconfirmed)
			if deps.lookup.byIDCalls != 1 || deps.mock.calls != 1 || deps.recorder.calls != 1 {
				t.Fatalf("expected one lookup, one Mock query, and one save")
			}
			observation := deps.recorder.observation
			if observation.Candidate != nil || observation.EvidenceRejected {
				t.Fatalf("Mock fault must not produce a candidate or rejection")
			}
			if observation.Reason == nil || observation.Reason.Code != IssueCodeMockResultUnconfirmed {
				t.Fatalf("expected the Mock fault reason")
			}
			if !reconcileTestEvidenceEmpty(observation.Evidence) {
				t.Fatalf("Mock fault must not fabricate evidence")
			}
			record := deps.recorder.saved.Record
			reconcileTestAssertDecision(t, record, DecisionKeepUnknown, ResultUnknown, ResultUnknown)
			reconcileTestAssertReason(t, record, IssueCodeMockResultUnconfirmed)
			if !reconcileTestEvidenceEmpty(record.Evidence) {
				t.Fatalf("saved fault evidence must remain empty")
			}
		})
	})

	t.Run("in_flight_unknown_fault_or_mismatch_cannot_downgrade_later_confirmation", func(t *testing.T) {
		cases := []struct {
			name                 string
			firstKind            string
			wantIssue            string
			wantDecision         HandlingDecision
			wantReason           string
			wantDiff             EvidenceFieldName
			wantEvidenceRejected bool
			wantCandidate        bool
		}{
			{
				name:         "fault",
				firstKind:    "fault",
				wantIssue:    IssueCodeMockResultUnconfirmed,
				wantDecision: DecisionKeepConfirmed,
				wantReason:   reasonCodeUnconfirmed,
			},
			{
				name:                 "amount_mismatch",
				firstKind:            "amount_mismatch",
				wantIssue:            IssueCodeMockEvidenceRejected,
				wantDecision:         DecisionRejectEvidence,
				wantReason:           reasonCodeEvidenceMismatch,
				wantDiff:             EvidenceFieldAmount,
				wantEvidenceRejected: true,
			},
			{
				name:          "transaction_id_conflict",
				firstKind:     "transaction_id_conflict",
				wantIssue:     IssueCodeMockEvidenceRejected,
				wantDecision:  DecisionRejectEvidence,
				wantReason:    reasonCodeConfirmationConflict,
				wantDiff:      EvidenceFieldMockTransactionID,
				wantCandidate: true,
			},
		}

		for _, currentResult := range []Result{ResultSucceeded, ResultFailed} {
			for _, tc := range cases {
				currentResult, tc := currentResult, tc
				t.Run(string(currentResult)+"/"+tc.name, func(t *testing.T) {
					base := queryTestUnknownOrder()
					deps := queryTestValidDeps(base)

					switch tc.firstKind {
					case "fault":
						deps.mock.found = true
						deps.mock.err = ErrUnavailable
					case "amount_mismatch":
						transaction := queryTestTransaction(base, currentResult)
						transaction.Amount = base.Amount + 1
						deps.mock.transaction = transaction
						deps.mock.found = true
					case "transaction_id_conflict":
						transaction := queryTestTransaction(base, currentResult)
						transaction.ID = queryTestOtherMockID
						deps.mock.transaction = transaction
						deps.mock.found = true
					default:
						t.Fatalf("unknown first request kind")
					}

					// Another request has already confirmed the same order. Its
					// observation was accepted from the same original UNKNOWN fact.
					confirmationObservation := reconcileTestQueryObservation(base, RequestID(NewUUIDv4()))
					confirmationEvidence, confirmationCandidate, confirmationReason := EvaluateMockEvidence(
						base,
						queryTestTransaction(base, currentResult),
					)
					if confirmationReason != nil || confirmationCandidate == nil {
						t.Fatalf("expected a matching second-request confirmation")
					}
					confirmationObservation.Evidence = confirmationEvidence
					confirmationObservation.Candidate = confirmationCandidate
					confirmedOrder, confirmationRecord, err := DecideObservation(base, confirmationObservation, queryTestTime())
					if err != nil {
						t.Fatalf("second-request confirmation failed")
					}
					if confirmationRecord.Decision != DecisionAcceptConfirmation ||
						confirmedOrder.Result != currentResult ||
						confirmedOrder.ConfirmedAt == nil ||
						confirmedOrder.MockTransactionID == nil {
						t.Fatalf("second request did not establish the expected confirmation")
					}

					saveReached := make(chan struct{})
					confirmedCh := make(chan PaymentOrder, 1)
					deps.recorder.fn = func(_ context.Context, observation Observation) (SaveObservationResult, error) {
						close(saveReached)
						// Barrier: the second request's confirmation is visible
						// before this first request saves its own observation.
						applied := <-confirmedCh
						updated, record, err := DecideObservation(applied, observation, queryTestTime())
						if err != nil {
							return SaveObservationResult{}, err
						}
						return SaveObservationResult{Order: updated, Record: record}, nil
					}
					go func() {
						<-saveReached
						confirmedCh <- confirmedOrder
					}()

					got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
					if err != nil {
						t.Fatalf("in-flight reconciliation returned an error")
					}
					reconcileTestAssertOrderFact(t, got.Order, confirmedOrder)
					queryTestAssertIssues(t, got.Issues, tc.wantIssue)
					if deps.lookup.byIDCalls != 1 || deps.mock.calls != 1 || deps.recorder.calls != 1 {
						t.Fatalf("expected one lookup, one Mock query, and one save")
					}

					firstObservation := deps.recorder.observation
					if firstObservation.EvidenceRejected != tc.wantEvidenceRejected {
						t.Fatalf("unexpected first-request evidence rejection state")
					}
					if tc.wantCandidate {
						if firstObservation.Candidate == nil || firstObservation.Candidate.MockTransactionID == nil ||
							*firstObservation.Candidate.MockTransactionID != queryTestOtherMockID {
							t.Fatalf("expected the first request to carry the conflicting transaction ID")
						}
					} else if firstObservation.Candidate != nil {
						t.Fatalf("first request must not carry a candidate")
					}

					saved := deps.recorder.saved
					reconcileTestAssertDecision(t, saved.Record, tc.wantDecision, currentResult, currentResult)
					reconcileTestAssertReason(t, saved.Record, tc.wantReason)
					reconcileTestAssertEvidenceAllowed(t, saved.Record.Evidence)
					if tc.wantDiff == "" {
						if len(saved.Record.Evidence.Differences) != 0 {
							t.Fatalf("fault must not record an evidence difference")
						}
					} else if len(saved.Record.Evidence.Differences) != 1 || saved.Record.Evidence.Differences[0] != tc.wantDiff {
						t.Fatalf("expected exactly the %q difference", tc.wantDiff)
					}
					if saved.Order.ConfirmedAt == nil || !saved.Order.ConfirmedAt.Equal(*confirmedOrder.ConfirmedAt) {
						t.Fatalf("lock-derived save must preserve the later confirmed_at")
					}
					if saved.Order.MockTransactionID == nil || *saved.Order.MockTransactionID != *confirmedOrder.MockTransactionID {
						t.Fatalf("lock-derived save must preserve the later Mock transaction ID")
					}
				})
			}
		}
	})

	t.Run("save_failure_readback_returns_confirmed_original_fact", func(t *testing.T) {
		for _, result := range []Result{ResultSucceeded, ResultFailed} {
			result := result
			t.Run(string(result), func(t *testing.T) {
				base := queryTestUnknownOrder()
				confirmed := queryTestConfirmedOrder(result)
				opposite := ResultSucceeded
				if result == ResultSucceeded {
					opposite = ResultFailed
				}
				fabricated := queryTestConfirmedOrder(opposite)
				fabricated.ID = queryTestOtherOrderID

				deps := queryTestValidDeps(base)
				deps.mock.transaction = queryTestTransaction(base, result)
				deps.mock.found = true
				deps.recorder.err = ErrIndeterminate
				deps.recorder.result = SaveObservationResult{
					Order: fabricated,
					Record: ResultHandlingRecord{
						ID:             ResultHandlingRecordID(queryTestMockTxID),
						PaymentOrderID: fabricated.ID,
						RequestID:      queryTestRequestID,
						Source:         SourceQuery,
						ObservedAt:     queryTestTime(),
						ResultBefore:   ResultUnknown,
						ResultAfter:    opposite,
						Decision:       DecisionAcceptConfirmation,
					},
				}

				lookupCalls := 0
				deps.lookup.byIDFn = func(context.Context, string, PaymentOrderID) (PaymentOrder, bool, error) {
					lookupCalls++
					if lookupCalls == 1 {
						return base, true, nil
					}
					return confirmed, true, nil
				}

				got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
				if err != nil {
					t.Fatalf("save failure with a reliable readback must return the order")
				}
				reconcileTestAssertOrderFact(t, got.Order, confirmed)
				if got.Order.ID != base.ID {
					t.Fatalf("readback must return the original order, not the error-returned order")
				}
				if got.Order.Result == fabricated.Result {
					t.Fatalf("readback must not use the error-returned order result")
				}
				queryTestAssertIssues(t, got.Issues, IssueCodeResultSaveError)
				if deps.mock.calls != 1 || deps.recorder.calls != 1 || deps.lookup.byIDCalls != 2 {
					t.Fatalf("expected one Mock query, one save attempt, and one readback")
				}
				if deps.lookup.byBusinessCalls != 0 {
					t.Fatalf("readback must use the original platform order ID")
				}
			})
		}
	})

	t.Run("four_decisions_have_strict_transitions_reasons_and_evidence", func(t *testing.T) {
		t.Run("accept_confirmation", func(t *testing.T) {
			order := resultTestOrder(ResultUnknown)
			observation := resultTestObservation(order)
			observation.Source = SourceQuery
			observation.Evidence = resultTestEvidence(order, ResultSucceeded)
			observation.Candidate = resultTestCandidate(ResultSucceeded, resultTestMockTxID)

			updated, record, err := DecideObservation(order, observation, resultTestTime())
			if err != nil {
				t.Fatalf("accept decision returned an error")
			}
			if updated.Result != ResultSucceeded {
				t.Fatalf("accept decision must update the result")
			}
			if updated.ConfirmedAt == nil || !updated.ConfirmedAt.Equal(resultTestTime()) {
				t.Fatalf("accept decision must set confirmed_at")
			}
			if updated.MockTransactionID == nil || *updated.MockTransactionID != resultTestMockTxID {
				t.Fatalf("accept decision must set the Mock transaction ID")
			}
			reconcileTestAssertDecision(t, record, DecisionAcceptConfirmation, ResultUnknown, ResultSucceeded)
			if record.ReasonCode != nil || record.ReasonMessage != nil {
				t.Fatalf("accept decision must not carry a reason")
			}
			reconcileTestAssertEvidenceAllowed(t, record.Evidence)
			if record.Evidence.PlatformReferenceID == nil || *record.Evidence.PlatformReferenceID != order.ID {
				t.Fatalf("accept evidence platform reference mismatch")
			}
			if record.Evidence.Amount == nil || *record.Evidence.Amount != order.Amount {
				t.Fatalf("accept evidence amount mismatch")
			}
			if record.Evidence.Currency == nil || *record.Evidence.Currency != order.Currency {
				t.Fatalf("accept evidence currency mismatch")
			}
			if record.Evidence.Result == nil || *record.Evidence.Result != ResultSucceeded {
				t.Fatalf("accept evidence result mismatch")
			}
			if len(record.Evidence.Differences) != 0 {
				t.Fatalf("accept evidence must not contain differences")
			}
		})

		t.Run("keep_unknown", func(t *testing.T) {
			order := resultTestOrder(ResultUnknown)
			observation := resultTestObservation(order)
			observation.Source = SourceQuery
			observation.Reason = fixedReason(reasonCodeUnconfirmed, reasonMessageUnconfirmed)

			updated, record, err := DecideObservation(order, observation, resultTestTime())
			if err != nil {
				t.Fatalf("keep-unknown decision returned an error")
			}
			if updated.Result != ResultUnknown || updated.ConfirmedAt != nil || updated.MockTransactionID != nil {
				t.Fatalf("keep-unknown must not fabricate a confirmation")
			}
			reconcileTestAssertDecision(t, record, DecisionKeepUnknown, ResultUnknown, ResultUnknown)
			reconcileTestAssertReason(t, record, reasonCodeUnconfirmed)
			if !reconcileTestEvidenceEmpty(record.Evidence) {
				t.Fatalf("keep-unknown must not fabricate evidence")
			}
		})

		t.Run("keep_confirmed", func(t *testing.T) {
			order := resultTestOrder(ResultSucceeded)
			oldConfirmed := *order.ConfirmedAt
			oldMockID := *order.MockTransactionID
			observation := resultTestObservation(order)
			observation.Source = SourceQuery
			observation.Reason = fixedReason(reasonCodeUnconfirmed, reasonMessageUnconfirmed)

			updated, record, err := DecideObservation(order, observation, resultTestTime())
			if err != nil {
				t.Fatalf("keep-confirmed decision returned an error")
			}
			if updated.Result != ResultSucceeded {
				t.Fatalf("keep-confirmed must preserve the confirmed result")
			}
			if updated.ConfirmedAt == nil || !updated.ConfirmedAt.Equal(oldConfirmed) {
				t.Fatalf("keep-confirmed must not replace confirmed_at")
			}
			if updated.MockTransactionID == nil || *updated.MockTransactionID != oldMockID {
				t.Fatalf("keep-confirmed must not replace the Mock transaction ID")
			}
			reconcileTestAssertDecision(t, record, DecisionKeepConfirmed, ResultSucceeded, ResultSucceeded)
			reconcileTestAssertReason(t, record, reasonCodeUnconfirmed)
			if !reconcileTestEvidenceEmpty(record.Evidence) {
				t.Fatalf("keep-confirmed must not fabricate evidence")
			}
		})

		t.Run("reject_evidence", func(t *testing.T) {
			order := resultTestOrder(ResultUnknown)
			observation := resultTestObservation(order)
			observation.Source = SourceQuery
			observation.Evidence = resultTestEvidence(order, ResultSucceeded)
			observation.Evidence.Amount = resultTestInt64Ptr(101)
			observation.EvidenceRejected = true
			observation.Reason = fixedReason(reasonCodeEvidenceMismatch, reasonMessageEvidenceMismatch)

			updated, record, err := DecideObservation(order, observation, resultTestTime())
			if err != nil {
				t.Fatalf("reject decision returned an error")
			}
			if updated.Result != ResultUnknown || updated.ConfirmedAt != nil {
				t.Fatalf("reject decision must not confirm the order")
			}
			reconcileTestAssertDecision(t, record, DecisionRejectEvidence, ResultUnknown, ResultUnknown)
			reconcileTestAssertReason(t, record, reasonCodeEvidenceMismatch)
			reconcileTestAssertEvidenceAllowed(t, record.Evidence)
			if len(record.Evidence.Differences) != 1 || record.Evidence.Differences[0] != EvidenceFieldAmount {
				t.Fatalf("reject decision must retain exactly the amount difference")
			}
		})
	})

	t.Run("evidence_whitelist_and_reason_limits_are_enforced", func(t *testing.T) {
		order := resultTestOrder(ResultUnknown)
		observation := resultTestObservation(order)
		observation.Source = SourceQuery
		long := strings.Repeat("x", 500)
		badPlatform := PaymentOrderID(long)
		badMock := MockTransactionID(strings.Repeat("y", 500))
		zeroAmount := int64(0)
		badCurrency := "USD"
		unknownResult := ResultUnknown
		observation.Evidence = Evidence{
			PlatformReferenceID: &badPlatform,
			MockTransactionID:   &badMock,
			Amount:              &zeroAmount,
			Currency:            &badCurrency,
			Result:              &unknownResult,
			Differences: []EvidenceFieldName{
				EvidenceFieldName("raw_field"),
				EvidenceFieldAmount,
				EvidenceFieldAmount,
			},
		}
		observation.Reason = &Reason{
			Code:    strings.Repeat("z", 100),
			Message: resultTestStringPtr(strings.Repeat("w", 600)),
		}

		updated, record, err := DecideObservation(order, observation, resultTestTime())
		if err != nil {
			t.Fatalf("malicious evidence decision returned an error")
		}
		if updated.Result != ResultUnknown {
			t.Fatalf("malicious evidence must not change the result")
		}
		reconcileTestAssertDecision(t, record, DecisionRejectEvidence, ResultUnknown, ResultUnknown)
		if record.Evidence.PlatformReferenceID != nil ||
			record.Evidence.MockTransactionID != nil ||
			record.Evidence.Amount != nil ||
			record.Evidence.Currency != nil ||
			record.Evidence.Result != nil {
			t.Fatalf("invalid evidence fields must be omitted")
		}
		wantDifferences := []EvidenceFieldName{
			EvidenceFieldPlatformReferenceID,
			EvidenceFieldMockTransactionID,
			EvidenceFieldAmount,
			EvidenceFieldCurrency,
			EvidenceFieldResult,
		}
		if len(record.Evidence.Differences) != len(wantDifferences) {
			t.Fatalf("unexpected whitelisted difference count")
		}
		for i, want := range wantDifferences {
			if record.Evidence.Differences[i] != want {
				t.Fatalf("unexpected whitelisted difference at %d", i)
			}
		}
		if record.ReasonCode == nil || *record.ReasonCode != reasonCodeEvidenceInvalid {
			t.Fatalf("expected a controlled invalid-evidence reason")
		}
		if record.ReasonMessage == nil ||
			len([]rune(*record.ReasonMessage)) > maxReasonMessageRunes ||
			strings.Contains(*record.ReasonMessage, "wwww") {
			t.Fatalf("raw or overlong reason message must not be retained")
		}
		if err := ValidateReason(&Reason{Code: *record.ReasonCode, Message: record.ReasonMessage}); err != nil {
			t.Fatalf("saved reason is not sanitized")
		}
	})

	t.Run("candidate_without_minimum_evidence_is_rejected", func(t *testing.T) {
		cases := []struct {
			name     string
			mutate   func(*Observation)
			wantDiff EvidenceFieldName
		}{
			{
				name:     "missing_platform_reference",
				mutate:   func(o *Observation) { o.Evidence.PlatformReferenceID = nil },
				wantDiff: EvidenceFieldPlatformReferenceID,
			},
			{
				name:     "missing_amount",
				mutate:   func(o *Observation) { o.Evidence.Amount = nil },
				wantDiff: EvidenceFieldAmount,
			},
			{
				name:     "missing_currency",
				mutate:   func(o *Observation) { o.Evidence.Currency = nil },
				wantDiff: EvidenceFieldCurrency,
			},
			{
				name:     "missing_result",
				mutate:   func(o *Observation) { o.Evidence.Result = nil },
				wantDiff: EvidenceFieldResult,
			},
			{
				name:     "missing_mock_transaction_id",
				mutate:   func(o *Observation) { o.Evidence.MockTransactionID = nil },
				wantDiff: EvidenceFieldMockTransactionID,
			},
			{
				name: "invalid_mock_transaction_id",
				mutate: func(o *Observation) {
					bad := MockTransactionID("not-a-uuid")
					o.Evidence.MockTransactionID = &bad
				},
				wantDiff: EvidenceFieldMockTransactionID,
			},
			{
				name:     "candidate_result_unknown",
				mutate:   func(o *Observation) { o.Candidate.Result = ResultUnknown },
				wantDiff: EvidenceFieldResult,
			},
		}

		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				order := resultTestOrder(ResultUnknown)
				observation := resultTestObservation(order)
				observation.Source = SourceQuery
				observation.Evidence = resultTestEvidence(order, ResultSucceeded)
				observation.Candidate = resultTestCandidate(ResultSucceeded, resultTestMockTxID)
				tc.mutate(&observation)

				updated, record, err := DecideObservation(order, observation, resultTestTime())
				if err != nil {
					t.Fatalf("incomplete candidate decision returned an error")
				}
				if updated.Result != ResultUnknown {
					t.Fatalf("incomplete candidate must not confirm the order")
				}
				reconcileTestAssertDecision(t, record, DecisionRejectEvidence, ResultUnknown, ResultUnknown)
				reconcileTestAssertReason(t, record, reasonCodeEvidenceInvalid)
				reconcileTestAssertEvidenceAllowed(t, record.Evidence)
				if !resultTestHasDiff(record.Evidence.Differences, tc.wantDiff) {
					t.Fatalf("expected the %q difference", tc.wantDiff)
				}
			})
		}
	})
}
