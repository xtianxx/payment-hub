package payment

import (
	"errors"
	"strings"
	"testing"
	"time"
)

const (
	resultTestOrderID      = PaymentOrderID("8b3e19bd-909a-467d-94c7-bf713984f03c")
	resultTestOtherOrderID = PaymentOrderID("0f1e2d3c-4b5a-4c6d-9e8f-7a6b5c4d3e2f")
	resultTestMockTxID     = MockTransactionID("1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f")
	resultTestOtherMockID  = MockTransactionID("2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f6a")
	resultTestRequestID    = RequestID("cc894a81-d54a-4d6b-a321-238ffad9920b")
	resultTestObsID        = ResultHandlingRecordID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")
)

func resultTestTime() time.Time {
	return time.Date(2026, 11, 6, 9, 0, 0, 123456789, time.UTC)
}

func resultTestOldTime() time.Time {
	return time.Date(2026, 11, 6, 8, 0, 0, 0, time.UTC)
}

func resultTestStringPtr(s string) *string     { return &s }
func resultTestInt64Ptr(v int64) *int64        { return &v }
func resultTestTimePtr(v time.Time) *time.Time { return &v }
func resultTestMockIDPtr(v MockTransactionID) *MockTransactionID {
	return &v
}

func resultTestOrder(result Result) PaymentOrder {
	order := PaymentOrder{
		ID:              resultTestOrderID,
		ClientID:        "client_1",
		BusinessOrderNo: "M100",
		Amount:          100,
		Currency:        "CNY",
		Result:          result,
		RegisteredAt:    resultTestTime(),
	}
	if result != ResultUnknown {
		order.ConfirmedAt = resultTestTimePtr(resultTestOldTime())
		order.MockTransactionID = resultTestMockIDPtr(resultTestMockTxID)
	}
	return order
}

func resultTestTransaction(order PaymentOrder, result Result) MockTransaction {
	return MockTransaction{
		ID:                  resultTestMockTxID,
		PlatformReferenceID: order.ID,
		Amount:              order.Amount,
		Currency:            order.Currency,
		Result:              result,
		ProcessedAt:         resultTestTime(),
	}
}

func resultTestEvidence(order PaymentOrder, result Result) Evidence {
	ref := order.ID
	tx := resultTestMockTxID
	if order.MockTransactionID != nil {
		tx = *order.MockTransactionID
	}
	amount := order.Amount
	currency := order.Currency
	res := result
	return Evidence{
		PlatformReferenceID: &ref,
		MockTransactionID:   &tx,
		Amount:              &amount,
		Currency:            &currency,
		Result:              &res,
	}
}

func resultTestObservation(order PaymentOrder) Observation {
	return Observation{
		ID:             resultTestObsID,
		PaymentOrderID: order.ID,
		ClientID:       order.ClientID,
		RequestID:      resultTestRequestID,
		Source:         SourcePayment,
		ObservedAt:     resultTestTime(),
	}
}

func resultTestCandidate(result Result, tx MockTransactionID) *CandidateConfirmation {
	return &CandidateConfirmation{
		Result:            result,
		MockTransactionID: resultTestMockIDPtr(tx),
	}
}

func resultTestAssertReason(t *testing.T, reason *Reason) {
	t.Helper()
	if reason == nil {
		t.Fatalf("expected a non-nil reason")
	}
	if err := ValidateReason(reason); err != nil {
		t.Fatalf("reason is not sanitized")
	}
}

func resultTestHasDiff(diffs []EvidenceFieldName, want EvidenceFieldName) bool {
	for _, diff := range diffs {
		if diff == want {
			return true
		}
	}
	return false
}

func TestEvidenceDecision(t *testing.T) {
	t.Run("evaluate_accepts_matching_succeeded", func(t *testing.T) {
		order := resultTestOrder(ResultUnknown)
		tx := resultTestTransaction(order, ResultSucceeded)
		evidence, candidate, reason := EvaluateMockEvidence(order, tx)
		if reason != nil {
			t.Fatalf("expected nil reason")
		}
		if candidate == nil {
			t.Fatalf("expected candidate")
		}
		if candidate.Result != ResultSucceeded {
			t.Fatalf("unexpected candidate result")
		}
		if candidate.MockTransactionID == nil || *candidate.MockTransactionID != tx.ID {
			t.Fatalf("candidate mock transaction ID mismatch")
		}
		if evidence.PlatformReferenceID == nil || *evidence.PlatformReferenceID != order.ID {
			t.Fatalf("evidence platform reference mismatch")
		}
		if evidence.MockTransactionID == nil || *evidence.MockTransactionID != tx.ID {
			t.Fatalf("evidence mock transaction ID mismatch")
		}
		if evidence.Amount == nil || *evidence.Amount != order.Amount {
			t.Fatalf("evidence amount mismatch")
		}
		if evidence.Currency == nil || *evidence.Currency != order.Currency {
			t.Fatalf("evidence currency mismatch")
		}
		if evidence.Result == nil || *evidence.Result != ResultSucceeded {
			t.Fatalf("evidence result mismatch")
		}
		if len(evidence.Differences) != 0 {
			t.Fatalf("expected no differences")
		}
	})

	t.Run("evaluate_accepts_matching_failed", func(t *testing.T) {
		order := resultTestOrder(ResultUnknown)
		tx := resultTestTransaction(order, ResultFailed)
		evidence, candidate, reason := EvaluateMockEvidence(order, tx)
		if reason != nil || candidate == nil {
			t.Fatalf("expected accepted failed evidence")
		}
		if candidate.Result != ResultFailed {
			t.Fatalf("unexpected candidate result")
		}
		if evidence.Result == nil || *evidence.Result != ResultFailed {
			t.Fatalf("evidence result mismatch")
		}
	})

	t.Run("evaluate_accepts_amount_boundaries", func(t *testing.T) {
		for _, amount := range []int64{1, 1_000_000} {
			order := resultTestOrder(ResultUnknown)
			order.Amount = amount
			tx := resultTestTransaction(order, ResultSucceeded)
			_, candidate, reason := EvaluateMockEvidence(order, tx)
			if reason != nil || candidate == nil {
				t.Fatalf("expected amount %d to be accepted", amount)
			}
		}
	})

	t.Run("evaluate_rejects_invalid_and_mismatched_fields", func(t *testing.T) {
		cases := []struct {
			name            string
			mutateOrder     func(*PaymentOrder)
			mutateTx        func(*MockTransaction)
			wantCode        string
			wantDiff        EvidenceFieldName
			wantAmountNil   bool
			wantCurrencyNil bool
			wantResultNil   bool
			wantMockNil     bool
			wantPlatformNil bool
		}{
			{
				name:          "transaction_amount_zero",
				mutateTx:      func(tx *MockTransaction) { tx.Amount = 0 },
				wantCode:      reasonCodeEvidenceInvalid,
				wantDiff:      EvidenceFieldAmount,
				wantAmountNil: true,
			},
			{
				name:          "transaction_amount_negative",
				mutateTx:      func(tx *MockTransaction) { tx.Amount = -1 },
				wantCode:      reasonCodeEvidenceInvalid,
				wantDiff:      EvidenceFieldAmount,
				wantAmountNil: true,
			},
			{
				name:          "transaction_amount_above_maximum",
				mutateTx:      func(tx *MockTransaction) { tx.Amount = 1_000_001 },
				wantCode:      reasonCodeEvidenceInvalid,
				wantDiff:      EvidenceFieldAmount,
				wantAmountNil: true,
			},
			{
				name:     "transaction_amount_mismatch",
				mutateTx: func(tx *MockTransaction) { tx.Amount = 101 },
				wantCode: reasonCodeEvidenceMismatch,
				wantDiff: EvidenceFieldAmount,
			},
			{
				name:            "transaction_currency_invalid",
				mutateTx:        func(tx *MockTransaction) { tx.Currency = "USD" },
				wantCode:        reasonCodeEvidenceInvalid,
				wantDiff:        EvidenceFieldCurrency,
				wantCurrencyNil: true,
			},
			{
				name:            "transaction_currency_lowercase",
				mutateTx:        func(tx *MockTransaction) { tx.Currency = "cny" },
				wantCode:        reasonCodeEvidenceInvalid,
				wantDiff:        EvidenceFieldCurrency,
				wantCurrencyNil: true,
			},
			{
				name:          "transaction_result_unknown",
				mutateTx:      func(tx *MockTransaction) { tx.Result = ResultUnknown },
				wantCode:      reasonCodeEvidenceInvalid,
				wantDiff:      EvidenceFieldResult,
				wantResultNil: true,
			},
			{
				name:          "transaction_result_invalid",
				mutateTx:      func(tx *MockTransaction) { tx.Result = Result("SUCCESS") },
				wantCode:      reasonCodeEvidenceInvalid,
				wantDiff:      EvidenceFieldResult,
				wantResultNil: true,
			},
			{
				name:        "transaction_mock_id_invalid",
				mutateTx:    func(tx *MockTransaction) { tx.ID = "not-a-uuid" },
				wantCode:    reasonCodeEvidenceInvalid,
				wantDiff:    EvidenceFieldMockTransactionID,
				wantMockNil: true,
			},
			{
				name:        "transaction_mock_id_empty",
				mutateTx:    func(tx *MockTransaction) { tx.ID = "" },
				wantCode:    reasonCodeEvidenceInvalid,
				wantDiff:    EvidenceFieldMockTransactionID,
				wantMockNil: true,
			},
			{
				name:        "transaction_mock_id_unbounded",
				mutateTx:    func(tx *MockTransaction) { tx.ID = MockTransactionID(strings.Repeat("a", 1000)) },
				wantCode:    reasonCodeEvidenceInvalid,
				wantDiff:    EvidenceFieldMockTransactionID,
				wantMockNil: true,
			},
			{
				name:     "transaction_platform_reference_mismatch",
				mutateTx: func(tx *MockTransaction) { tx.PlatformReferenceID = resultTestOtherOrderID },
				wantCode: reasonCodeEvidenceMismatch,
				wantDiff: EvidenceFieldPlatformReferenceID,
			},
			{
				name:            "transaction_platform_reference_invalid",
				mutateTx:        func(tx *MockTransaction) { tx.PlatformReferenceID = "not-a-uuid" },
				wantCode:        reasonCodeEvidenceInvalid,
				wantDiff:        EvidenceFieldPlatformReferenceID,
				wantPlatformNil: true,
			},
			{
				name:        "order_amount_invalid",
				mutateOrder: func(order *PaymentOrder) { order.Amount = 0 },
				mutateTx:    func(tx *MockTransaction) { tx.Amount = 100 },
				wantCode:    reasonCodeEvidenceInvalid,
				wantDiff:    EvidenceFieldAmount,
			},
			{
				name:        "order_currency_invalid",
				mutateOrder: func(order *PaymentOrder) { order.Currency = "USD" },
				mutateTx:    func(tx *MockTransaction) { tx.Currency = "CNY" },
				wantCode:    reasonCodeEvidenceInvalid,
				wantDiff:    EvidenceFieldCurrency,
			},
			{
				name:            "order_id_invalid",
				mutateOrder:     func(order *PaymentOrder) { order.ID = "not-a-uuid" },
				mutateTx:        func(tx *MockTransaction) { tx.PlatformReferenceID = "not-a-uuid" },
				wantCode:        reasonCodeEvidenceInvalid,
				wantDiff:        EvidenceFieldPlatformReferenceID,
				wantPlatformNil: true,
			},
		}

		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				order := resultTestOrder(ResultUnknown)
				tx := resultTestTransaction(order, ResultSucceeded)
				if tc.mutateOrder != nil {
					tc.mutateOrder(&order)
				}
				if tc.mutateTx != nil {
					tc.mutateTx(&tx)
				}
				evidence, candidate, reason := EvaluateMockEvidence(order, tx)
				if candidate != nil {
					t.Fatalf("expected nil candidate")
				}
				resultTestAssertReason(t, reason)
				if reason.Code != tc.wantCode {
					t.Fatalf("unexpected reason code")
				}
				if !resultTestHasDiff(evidence.Differences, tc.wantDiff) {
					t.Fatalf("expected difference %q", tc.wantDiff)
				}
				if tc.wantAmountNil && evidence.Amount != nil {
					t.Fatalf("expected invalid amount to be omitted")
				}
				if tc.wantCurrencyNil && evidence.Currency != nil {
					t.Fatalf("expected invalid currency to be omitted")
				}
				if tc.wantResultNil && evidence.Result != nil {
					t.Fatalf("expected invalid result to be omitted")
				}
				if tc.wantMockNil && evidence.MockTransactionID != nil {
					t.Fatalf("expected invalid mock transaction ID to be omitted")
				}
				if tc.wantPlatformNil && evidence.PlatformReferenceID != nil {
					t.Fatalf("expected invalid platform reference to be omitted")
				}
			})
		}
	})

	t.Run("evaluate_does_not_mutate_or_share_inputs", func(t *testing.T) {
		order := resultTestOrder(ResultSucceeded)
		tx := resultTestTransaction(order, ResultSucceeded)
		oldConfirmed := *order.ConfirmedAt
		oldMockID := *order.MockTransactionID
		evidence, candidate, reason := EvaluateMockEvidence(order, tx)
		if reason != nil || candidate == nil {
			t.Fatalf("expected accepted evidence")
		}
		if order.ConfirmedAt == nil || !order.ConfirmedAt.Equal(oldConfirmed) {
			t.Fatalf("order confirmed_at was mutated")
		}
		if order.MockTransactionID == nil || *order.MockTransactionID != oldMockID {
			t.Fatalf("order mock transaction ID was mutated")
		}
		*evidence.Amount = 999
		*evidence.MockTransactionID = resultTestOtherMockID
		*evidence.Result = ResultFailed
		*candidate.MockTransactionID = resultTestOtherMockID
		if tx.Amount != order.Amount || tx.ID != oldMockID || tx.Result != ResultSucceeded {
			t.Fatalf("transaction value was mutated through shared pointers")
		}
		if *order.MockTransactionID != oldMockID {
			t.Fatalf("order mock transaction ID changed through shared pointers")
		}
	})

	t.Run("decide_accepts_succeeded_for_unknown", func(t *testing.T) {
		order := resultTestOrder(ResultUnknown)
		observation := resultTestObservation(order)
		observation.Evidence = resultTestEvidence(order, ResultSucceeded)
		observation.Candidate = resultTestCandidate(ResultSucceeded, resultTestMockTxID)
		confirmedAt := resultTestTime()
		updated, record, err := DecideObservation(order, observation, confirmedAt)
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if updated.Result != ResultSucceeded {
			t.Fatalf("expected succeeded result")
		}
		if updated.ConfirmedAt == nil || !updated.ConfirmedAt.Equal(confirmedAt) {
			t.Fatalf("expected confirmed_at to be set")
		}
		if updated.MockTransactionID == nil || *updated.MockTransactionID != resultTestMockTxID {
			t.Fatalf("expected mock transaction ID to be set")
		}
		if record.ID != observation.ID || record.PaymentOrderID != order.ID {
			t.Fatalf("record identity mismatch")
		}
		if record.RequestID != observation.RequestID || record.Source != observation.Source {
			t.Fatalf("record request/source mismatch")
		}
		if !record.ObservedAt.Equal(observation.ObservedAt) {
			t.Fatalf("record observed_at mismatch")
		}
		if record.ResultBefore != ResultUnknown || record.ResultAfter != ResultSucceeded {
			t.Fatalf("record result transition mismatch")
		}
		if record.Decision != DecisionAcceptConfirmation {
			t.Fatalf("expected accept decision")
		}
		if record.ReasonCode != nil || record.ReasonMessage != nil {
			t.Fatalf("expected no reason for accepted confirmation")
		}
		if record.Evidence.Result == nil || *record.Evidence.Result != ResultSucceeded {
			t.Fatalf("record evidence result mismatch")
		}

		oldObsAmount := *observation.Evidence.Amount
		oldObsMock := *observation.Evidence.MockTransactionID
		*updated.ConfirmedAt = resultTestOldTime()
		*updated.MockTransactionID = resultTestOtherMockID
		*record.Evidence.Amount = 999
		if *observation.Evidence.Amount != oldObsAmount || *observation.Evidence.MockTransactionID != oldObsMock {
			t.Fatalf("observation evidence was mutated through shared pointers")
		}
	})

	t.Run("decide_accepts_failed_for_unknown", func(t *testing.T) {
		order := resultTestOrder(ResultUnknown)
		observation := resultTestObservation(order)
		observation.Evidence = resultTestEvidence(order, ResultFailed)
		observation.Candidate = resultTestCandidate(ResultFailed, resultTestMockTxID)
		updated, record, err := DecideObservation(order, observation, resultTestTime())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if updated.Result != ResultFailed || record.Decision != DecisionAcceptConfirmation {
			t.Fatalf("expected failed confirmation to be accepted")
		}
		if record.ResultAfter != ResultFailed {
			t.Fatalf("record result_after mismatch")
		}
	})

	t.Run("decide_keeps_unknown_without_candidate", func(t *testing.T) {
		order := resultTestOrder(ResultUnknown)
		observation := resultTestObservation(order)
		observation.Reason = &Reason{Code: reasonCodeUnconfirmed, Message: resultTestStringPtr("timeout")}
		updated, record, err := DecideObservation(order, observation, resultTestTime())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if updated.Result != ResultUnknown || updated.ConfirmedAt != nil || updated.MockTransactionID != nil {
			t.Fatalf("expected order to remain unknown")
		}
		if record.Decision != DecisionKeepUnknown {
			t.Fatalf("expected keep unknown decision")
		}
		if record.ResultBefore != ResultUnknown || record.ResultAfter != ResultUnknown {
			t.Fatalf("record result transition mismatch")
		}
		if record.ReasonCode == nil || *record.ReasonCode != reasonCodeUnconfirmed {
			t.Fatalf("expected unconfirmed reason code")
		}
		if record.ReasonMessage == nil || *record.ReasonMessage != "timeout" {
			t.Fatalf("expected sanitized reason message to be preserved")
		}
	})

	t.Run("decide_synthesizes_reason_when_missing_or_invalid", func(t *testing.T) {
		order := resultTestOrder(ResultUnknown)
		observation := resultTestObservation(order)
		updated, record, err := DecideObservation(order, observation, resultTestTime())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if updated.Result != ResultUnknown || record.Decision != DecisionKeepUnknown {
			t.Fatalf("expected unknown result to be kept")
		}
		if record.ReasonCode == nil || *record.ReasonCode != reasonCodeUnconfirmed {
			t.Fatalf("expected synthesized unconfirmed reason")
		}

		observation.Reason = &Reason{
			Code:    strings.Repeat("x", 100),
			Message: resultTestStringPtr(strings.Repeat("y", 600)),
		}
		_, record, err = DecideObservation(order, observation, resultTestTime())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if record.ReasonCode == nil || *record.ReasonCode != reasonCodeUnconfirmed {
			t.Fatalf("expected invalid reason to be replaced")
		}
		if record.ReasonMessage == nil || strings.Contains(*record.ReasonMessage, "yyy") {
			t.Fatalf("expected invalid reason message to be replaced")
		}
	})

	t.Run("decide_keeps_existing_confirmed_result", func(t *testing.T) {
		for _, result := range []Result{ResultSucceeded, ResultFailed} {
			result := result
			t.Run(string(result), func(t *testing.T) {
				order := resultTestOrder(result)
				observation := resultTestObservation(order)
				observation.Evidence = resultTestEvidence(order, result)
				observation.Candidate = resultTestCandidate(result, resultTestMockTxID)
				oldConfirmed := *order.ConfirmedAt
				oldMockID := *order.MockTransactionID
				updated, record, err := DecideObservation(order, observation, resultTestTime())
				if err != nil {
					t.Fatalf("expected nil error")
				}
				if updated.Result != result {
					t.Fatalf("expected confirmed result to be preserved")
				}
				if updated.ConfirmedAt == nil || !updated.ConfirmedAt.Equal(oldConfirmed) {
					t.Fatalf("confirmed_at must not be replaced")
				}
				if updated.MockTransactionID == nil || *updated.MockTransactionID != oldMockID {
					t.Fatalf("mock transaction ID must not be replaced")
				}
				if record.Decision != DecisionKeepConfirmed {
					t.Fatalf("expected keep confirmed decision")
				}
				if record.ResultBefore != result || record.ResultAfter != result {
					t.Fatalf("record result transition mismatch")
				}
				if record.ReasonCode != nil {
					t.Fatalf("expected no reason for matching confirmed result")
				}
			})
		}
	})

	t.Run("decide_keeps_confirmed_without_new_confirmation", func(t *testing.T) {
		order := resultTestOrder(ResultSucceeded)
		observation := resultTestObservation(order)
		observation.Reason = &Reason{Code: reasonCodeUnconfirmed, Message: resultTestStringPtr("query failed")}
		updated, record, err := DecideObservation(order, observation, resultTestTime())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if updated.Result != ResultSucceeded {
			t.Fatalf("expected confirmed result to be preserved")
		}
		if record.Decision != DecisionKeepConfirmed {
			t.Fatalf("expected keep confirmed decision")
		}
		if record.ReasonCode == nil || *record.ReasonCode != reasonCodeUnconfirmed {
			t.Fatalf("expected reason to be preserved")
		}
	})

	t.Run("decide_rejects_conflicting_candidate_without_downgrade", func(t *testing.T) {
		order := resultTestOrder(ResultSucceeded)
		observation := resultTestObservation(order)
		observation.Evidence = resultTestEvidence(order, ResultFailed)
		observation.Candidate = resultTestCandidate(ResultFailed, resultTestMockTxID)
		oldConfirmed := *order.ConfirmedAt
		oldMockID := *order.MockTransactionID
		updated, record, err := DecideObservation(order, observation, resultTestTime())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if updated.Result != ResultSucceeded {
			t.Fatalf("confirmed result must not be downgraded")
		}
		if updated.ConfirmedAt == nil || !updated.ConfirmedAt.Equal(oldConfirmed) {
			t.Fatalf("confirmed_at must not change")
		}
		if updated.MockTransactionID == nil || *updated.MockTransactionID != oldMockID {
			t.Fatalf("mock transaction ID must not change")
		}
		if record.Decision != DecisionRejectEvidence {
			t.Fatalf("expected reject decision")
		}
		if record.ResultBefore != ResultSucceeded || record.ResultAfter != ResultSucceeded {
			t.Fatalf("record result transition mismatch")
		}
		if record.ReasonCode == nil || *record.ReasonCode != reasonCodeConfirmationConflict {
			t.Fatalf("expected conflict reason")
		}
		if !resultTestHasDiff(record.Evidence.Differences, EvidenceFieldResult) {
			t.Fatalf("expected result difference")
		}
	})

	t.Run("decide_rejects_explicitly_rejected_evidence", func(t *testing.T) {
		order := resultTestOrder(ResultUnknown)
		observation := resultTestObservation(order)
		observation.EvidenceRejected = true
		observation.Reason = &Reason{Code: "MOCK_EVIDENCE_REJECTED", Message: resultTestStringPtr("rejected")}
		updated, record, err := DecideObservation(order, observation, resultTestTime())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if updated.Result != ResultUnknown {
			t.Fatalf("result must not change")
		}
		if record.Decision != DecisionRejectEvidence {
			t.Fatalf("expected reject decision")
		}
		if record.ReasonCode == nil || *record.ReasonCode != "MOCK_EVIDENCE_REJECTED" {
			t.Fatalf("expected sanitized reason to be preserved")
		}

		observation.Candidate = resultTestCandidate(ResultSucceeded, resultTestMockTxID)
		observation.Evidence = resultTestEvidence(order, ResultSucceeded)
		updated, record, err = DecideObservation(order, observation, resultTestTime())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if updated.Result != ResultUnknown || record.Decision != DecisionRejectEvidence {
			t.Fatalf("rejected observation must not accept a candidate")
		}
	})

	t.Run("decide_rejects_inconsistent_candidate_and_evidence", func(t *testing.T) {
		cases := []struct {
			name     string
			mutate   func(*Observation)
			wantDiff EvidenceFieldName
		}{
			{
				name: "candidate_result_differs_from_evidence",
				mutate: func(o *Observation) {
					o.Candidate = resultTestCandidate(ResultSucceeded, resultTestMockTxID)
					o.Evidence = resultTestEvidence(resultTestOrder(ResultUnknown), ResultFailed)
				},
				wantDiff: EvidenceFieldResult,
			},
			{
				name: "candidate_transaction_differs",
				mutate: func(o *Observation) {
					o.Candidate = resultTestCandidate(ResultSucceeded, resultTestOtherMockID)
				},
				wantDiff: EvidenceFieldMockTransactionID,
			},
			{
				name: "candidate_transaction_missing",
				mutate: func(o *Observation) {
					o.Candidate.MockTransactionID = nil
				},
				wantDiff: EvidenceFieldMockTransactionID,
			},
			{
				name: "evidence_transaction_missing",
				mutate: func(o *Observation) {
					o.Evidence.MockTransactionID = nil
				},
				wantDiff: EvidenceFieldMockTransactionID,
			},
			{
				name: "evidence_amount_mismatch",
				mutate: func(o *Observation) {
					o.Evidence.Amount = resultTestInt64Ptr(101)
				},
				wantDiff: EvidenceFieldAmount,
			},
			{
				name: "evidence_amount_missing",
				mutate: func(o *Observation) {
					o.Evidence.Amount = nil
				},
				wantDiff: EvidenceFieldAmount,
			},
			{
				name: "evidence_currency_missing",
				mutate: func(o *Observation) {
					o.Evidence.Currency = nil
				},
				wantDiff: EvidenceFieldCurrency,
			},
			{
				name: "evidence_platform_reference_mismatch",
				mutate: func(o *Observation) {
					o.Evidence.PlatformReferenceID = func() *PaymentOrderID {
						id := resultTestOtherOrderID
						return &id
					}()
				},
				wantDiff: EvidenceFieldPlatformReferenceID,
			},
			{
				name: "evidence_platform_reference_missing",
				mutate: func(o *Observation) {
					o.Evidence.PlatformReferenceID = nil
				},
				wantDiff: EvidenceFieldPlatformReferenceID,
			},
			{
				name: "evidence_result_missing",
				mutate: func(o *Observation) {
					o.Evidence.Result = nil
				},
				wantDiff: EvidenceFieldResult,
			},
			{
				name: "evidence_result_unknown",
				mutate: func(o *Observation) {
					o.Evidence.Result = func() *Result {
						result := ResultUnknown
						return &result
					}()
				},
				wantDiff: EvidenceFieldResult,
			},
			{
				name: "evidence_declares_differences",
				mutate: func(o *Observation) {
					o.Evidence.Differences = []EvidenceFieldName{EvidenceFieldAmount}
				},
				wantDiff: EvidenceFieldAmount,
			},
			{
				name: "candidate_result_unknown",
				mutate: func(o *Observation) {
					o.Candidate.Result = ResultUnknown
				},
				wantDiff: EvidenceFieldResult,
			},
			{
				name: "candidate_transaction_invalid",
				mutate: func(o *Observation) {
					o.Candidate.MockTransactionID = func() *MockTransactionID {
						id := MockTransactionID("not-a-uuid")
						return &id
					}()
				},
				wantDiff: EvidenceFieldMockTransactionID,
			},
		}

		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				order := resultTestOrder(ResultUnknown)
				observation := resultTestObservation(order)
				observation.Evidence = resultTestEvidence(order, ResultSucceeded)
				observation.Candidate = resultTestCandidate(ResultSucceeded, resultTestMockTxID)
				tc.mutate(&observation)
				updated, record, err := DecideObservation(order, observation, resultTestTime())
				if err != nil {
					t.Fatalf("expected nil error")
				}
				if updated.Result != ResultUnknown {
					t.Fatalf("result must not change")
				}
				if record.Decision != DecisionRejectEvidence {
					t.Fatalf("expected reject decision")
				}
				resultTestAssertReason(t, &Reason{Code: valueOrEmpty(record.ReasonCode), Message: record.ReasonMessage})
				if !resultTestHasDiff(record.Evidence.Differences, tc.wantDiff) {
					t.Fatalf("expected difference %q", tc.wantDiff)
				}
				if record.Evidence.Result != nil && *record.Evidence.Result == ResultUnknown {
					t.Fatalf("record evidence must not retain UNKNOWN result")
				}
			})
		}
	})

	t.Run("decide_filters_malicious_evidence_and_reason", func(t *testing.T) {
		order := resultTestOrder(ResultUnknown)
		observation := resultTestObservation(order)
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
		_, record, err := DecideObservation(order, observation, resultTestTime())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if record.Decision != DecisionRejectEvidence {
			t.Fatalf("expected reject decision")
		}
		if record.Evidence.PlatformReferenceID != nil ||
			record.Evidence.MockTransactionID != nil ||
			record.Evidence.Amount != nil ||
			record.Evidence.Currency != nil ||
			record.Evidence.Result != nil {
			t.Fatalf("invalid evidence fields must be omitted")
		}
		wantDiffs := []EvidenceFieldName{
			EvidenceFieldPlatformReferenceID,
			EvidenceFieldMockTransactionID,
			EvidenceFieldAmount,
			EvidenceFieldCurrency,
			EvidenceFieldResult,
		}
		if len(record.Evidence.Differences) != len(wantDiffs) {
			t.Fatalf("unexpected difference count")
		}
		for i, want := range wantDiffs {
			if record.Evidence.Differences[i] != want {
				t.Fatalf("unexpected difference at %d", i)
			}
		}
		if record.ReasonCode == nil || *record.ReasonCode != reasonCodeEvidenceInvalid {
			t.Fatalf("expected controlled invalid reason")
		}
		if record.ReasonMessage == nil || strings.Contains(*record.ReasonMessage, "www") {
			t.Fatalf("raw reason message must not be retained")
		}
		oldDiff := observation.Evidence.Differences[0]
		record.Evidence.Differences[0] = EvidenceFieldResult
		if observation.Evidence.Differences[0] != oldDiff {
			t.Fatalf("differences slice must not be shared")
		}
	})

	t.Run("decide_rejects_invalid_structure", func(t *testing.T) {
		cases := []struct {
			name        string
			mutateOrder func(*PaymentOrder)
			mutateObs   func(*Observation)
			confirmedAt func() time.Time
			want        error
		}{
			{
				name:        "invalid_order_id",
				mutateOrder: func(o *PaymentOrder) { o.ID = "not-a-uuid" },
				want:        ErrInvalidInput,
			},
			{
				name:        "invalid_order_amount",
				mutateOrder: func(o *PaymentOrder) { o.Amount = 0 },
				want:        ErrInvalidInput,
			},
			{
				name:        "invalid_order_currency",
				mutateOrder: func(o *PaymentOrder) { o.Currency = "USD" },
				want:        ErrInvalidInput,
			},
			{
				name: "confirmed_without_confirmed_at",
				mutateOrder: func(o *PaymentOrder) {
					o.Result = ResultSucceeded
					o.ConfirmedAt = nil
				},
				want: ErrInvalidInput,
			},
			{
				name: "unknown_with_confirmed_at",
				mutateOrder: func(o *PaymentOrder) {
					o.ConfirmedAt = resultTestTimePtr(resultTestOldTime())
				},
				want: ErrInvalidInput,
			},
			{
				name:      "invalid_observation_id",
				mutateObs: func(o *Observation) { o.ID = "not-a-uuid" },
				want:      ErrInvalidInput,
			},
			{
				name:      "payment_order_mismatch",
				mutateObs: func(o *Observation) { o.PaymentOrderID = resultTestOtherOrderID },
				want:      ErrObservationConflict,
			},
			{
				name:      "client_mismatch",
				mutateObs: func(o *Observation) { o.ClientID = "other_client" },
				want:      ErrObservationConflict,
			},
			{
				name:      "invalid_request_id",
				mutateObs: func(o *Observation) { o.RequestID = "not-a-uuid" },
				want:      ErrInvalidInput,
			},
			{
				name:      "invalid_source",
				mutateObs: func(o *Observation) { o.Source = Source("CALLBACK") },
				want:      ErrInvalidInput,
			},
			{
				name: "observed_at_not_utc",
				mutateObs: func(o *Observation) {
					o.ObservedAt = time.Date(2026, 11, 6, 9, 0, 0, 0, time.FixedZone("CST", 8*60*60))
				},
				want: ErrInvalidInput,
			},
			{
				name: "accept_confirmed_at_not_utc",
				mutateObs: func(o *Observation) {
					o.Evidence = resultTestEvidence(resultTestOrder(ResultUnknown), ResultSucceeded)
					o.Candidate = resultTestCandidate(ResultSucceeded, resultTestMockTxID)
				},
				confirmedAt: func() time.Time {
					return time.Date(2026, 11, 6, 9, 0, 0, 0, time.FixedZone("CST", 8*60*60))
				},
				want: ErrInvalidInput,
			},
		}

		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				order := resultTestOrder(ResultUnknown)
				observation := resultTestObservation(order)
				observation.Evidence = resultTestEvidence(order, ResultSucceeded)
				observation.Candidate = resultTestCandidate(ResultSucceeded, resultTestMockTxID)
				if tc.mutateOrder != nil {
					tc.mutateOrder(&order)
				}
				if tc.mutateObs != nil {
					tc.mutateObs(&observation)
				}
				at := resultTestTime()
				if tc.confirmedAt != nil {
					at = tc.confirmedAt()
				}
				updated, record, err := DecideObservation(order, observation, at)
				if !errors.Is(err, tc.want) {
					t.Fatalf("expected error %v", tc.want)
				}
				if updated.ID != "" || record.ID != "" {
					t.Fatalf("invalid structure must not produce a record")
				}
			})
		}
	})
}

func valueOrEmpty(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}
