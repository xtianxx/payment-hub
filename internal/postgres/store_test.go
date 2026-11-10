//go:build !integration

package postgres

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgconn"

	"payment-hub/internal/payment"
)

func storeTestFixedTime(offset time.Duration) time.Time {
	return time.Date(2026, time.November, 9, 12, 0, 0, 0, time.UTC).Add(offset)
}

func storeTestOrder(result payment.Result, mockID *payment.MockTransactionID, confirmedAt *time.Time) payment.PaymentOrder {
	return payment.PaymentOrder{
		ID:                payment.PaymentOrderID(payment.NewUUIDv4()),
		ClientID:          "store-test-client",
		BusinessOrderNo:   "store-test-order",
		Amount:            1234,
		Currency:          "CNY",
		Result:            result,
		MockTransactionID: mockID,
		RegisteredAt:      storeTestFixedTime(0),
		ConfirmedAt:       confirmedAt,
	}
}

func storeTestObservation(order payment.PaymentOrder, source payment.Source) payment.Observation {
	return payment.Observation{
		ID:             payment.ResultHandlingRecordID(payment.NewUUIDv4()),
		PaymentOrderID: order.ID,
		ClientID:       order.ClientID,
		RequestID:      payment.RequestID(payment.NewUUIDv4()),
		Source:         source,
		ObservedAt:     storeTestFixedTime(time.Minute),
	}
}

func storeTestAcceptedObservation(order payment.PaymentOrder, mockID *payment.MockTransactionID) payment.Observation {
	observation := storeTestObservation(order, payment.SourcePayment)
	reference := order.ID
	amount := order.Amount
	currency := order.Currency
	result := payment.ResultSucceeded
	observation.Evidence = payment.Evidence{
		PlatformReferenceID: &reference,
		Amount:              &amount,
		Currency:            &currency,
		Result:              &result,
	}
	observation.Candidate = &payment.CandidateConfirmation{
		Result:            result,
		MockTransactionID: mockID,
	}
	if mockID != nil {
		observation.Evidence.MockTransactionID = mockID
	}
	return observation
}

func storeTestKeepUnknownObservation(order payment.PaymentOrder) payment.Observation {
	observation := storeTestObservation(order, payment.SourceQuery)
	code := "NO_MATCH"
	observation.Reason = &payment.Reason{Code: code}
	return observation
}

func storeTestRequireMatch(t *testing.T, observation payment.Observation, record payment.ResultHandlingRecord, current payment.PaymentOrder) {
	t.Helper()
	match, err := observationMatchesRecord(observation, record, current)
	if err != nil {
		t.Fatalf("idempotency comparison returned an unexpected error")
	}
	if !match {
		t.Fatalf("idempotency comparison did not recognize the same observation")
	}
}

func storeTestRequireNoMatch(t *testing.T, observation payment.Observation, record payment.ResultHandlingRecord, current payment.PaymentOrder) {
	t.Helper()
	match, err := observationMatchesRecord(observation, record, current)
	if err != nil {
		return
	}
	if match {
		t.Fatalf("idempotency comparison accepted a different observation")
	}
}

func TestObservationMatchesRecordHistoricalUnknown(t *testing.T) {
	mockID := payment.MockTransactionID(payment.NewUUIDv4())
	historical := storeTestOrder(payment.ResultUnknown, nil, nil)
	observation := storeTestAcceptedObservation(historical, &mockID)

	updated, record, err := payment.DecideObservation(historical, observation, storeTestFixedTime(2*time.Minute))
	if err != nil {
		t.Fatalf("fixture decision failed")
	}
	storeTestRequireMatch(t, observation, record, updated)

	changedRequest := observation
	changedRequest.RequestID = payment.RequestID(payment.NewUUIDv4())
	storeTestRequireNoMatch(t, changedRequest, record, updated)

	changedEvidence := observation
	changedEvidence = cloneObservation(changedEvidence)
	changedAmount := historical.Amount + 1
	changedEvidence.Evidence.Amount = &changedAmount
	storeTestRequireNoMatch(t, changedEvidence, record, updated)

	changedCandidate := observation
	changedCandidate = cloneObservation(changedCandidate)
	changedCandidate.Candidate.Result = payment.ResultFailed
	storeTestRequireNoMatch(t, changedCandidate, record, updated)

	changedTime := observation
	changedTime.ObservedAt = observation.ObservedAt.Add(time.Second)
	storeTestRequireNoMatch(t, changedTime, record, updated)
}

func TestObservationMatchesRecordHistoricalConfirmed(t *testing.T) {
	mockID := payment.MockTransactionID(payment.NewUUIDv4())
	confirmedAt := storeTestFixedTime(-time.Minute)
	current := storeTestOrder(payment.ResultSucceeded, &mockID, &confirmedAt)
	observation := storeTestAcceptedObservation(current, &mockID)

	_, record, err := payment.DecideObservation(current, observation, storeTestFixedTime(2*time.Minute))
	if err != nil {
		t.Fatalf("fixture decision failed")
	}
	storeTestRequireMatch(t, observation, record, current)

	conflicting := observation
	conflicting = cloneObservation(conflicting)
	conflicting.Candidate.Result = payment.ResultFailed
	storeTestRequireNoMatch(t, conflicting, record, current)
}

func TestObservationMatchesRecordDoesNotForceCurrentAfterState(t *testing.T) {
	historical := storeTestOrder(payment.ResultUnknown, nil, nil)
	observation := storeTestKeepUnknownObservation(historical)

	_, record, err := payment.DecideObservation(historical, observation, storeTestFixedTime(2*time.Minute))
	if err != nil {
		t.Fatalf("fixture decision failed")
	}

	// A later committed save confirmed the order. The historical record still
	// describes the earlier UNKNOWN observation, so idempotency must compare
	// against the reconstructed historical state, not the current after-state.
	mockID := payment.MockTransactionID(payment.NewUUIDv4())
	confirmedAt := storeTestFixedTime(time.Minute)
	current := historical
	current.Result = payment.ResultSucceeded
	current.MockTransactionID = &mockID
	current.ConfirmedAt = &confirmedAt

	storeTestRequireMatch(t, observation, record, current)

	changedReason := observation
	changedReason = cloneObservation(changedReason)
	changedReason.Reason = &payment.Reason{Code: "OTHER_REASON"}
	storeTestRequireNoMatch(t, changedReason, record, current)
}

func TestCloneObservationDeepCopiesPointersAndSlices(t *testing.T) {
	mockID := payment.MockTransactionID(payment.NewUUIDv4())
	reference := payment.PaymentOrderID(payment.NewUUIDv4())
	amount := int64(1234)
	currency := "CNY"
	result := payment.ResultSucceeded
	message := "message"
	reasonCode := "REASON"
	differences := []payment.EvidenceFieldName{payment.EvidenceFieldAmount}
	candidateMockID := payment.MockTransactionID(payment.NewUUIDv4())

	original := payment.Observation{
		ID:             payment.ResultHandlingRecordID(payment.NewUUIDv4()),
		PaymentOrderID: reference,
		ClientID:       "store-test-client",
		RequestID:      payment.RequestID(payment.NewUUIDv4()),
		Source:         payment.SourcePayment,
		ObservedAt:     storeTestFixedTime(0),
		Evidence: payment.Evidence{
			PlatformReferenceID: &reference,
			MockTransactionID:   &mockID,
			Amount:              &amount,
			Currency:            &currency,
			Result:              &result,
			Differences:         differences,
		},
		Reason:    &payment.Reason{Code: reasonCode, Message: &message},
		Candidate: &payment.CandidateConfirmation{Result: result, MockTransactionID: &candidateMockID},
	}
	cloned := cloneObservation(original)
	wantReference := reference
	wantMockID := mockID
	wantCandidateMockID := candidateMockID

	*original.Evidence.PlatformReferenceID = payment.PaymentOrderID(payment.NewUUIDv4())
	*original.Evidence.MockTransactionID = payment.MockTransactionID(payment.NewUUIDv4())
	*original.Evidence.Amount = 9999
	*original.Evidence.Currency = "USD"
	*original.Evidence.Result = payment.ResultFailed
	original.Evidence.Differences[0] = payment.EvidenceFieldCurrency
	original.Reason.Code = "CHANGED"
	*original.Reason.Message = "changed"
	*original.Candidate.MockTransactionID = payment.MockTransactionID(payment.NewUUIDv4())

	if cloned.Evidence.PlatformReferenceID == nil || *cloned.Evidence.PlatformReferenceID != wantReference {
		t.Fatalf("cloned platform reference alias changed")
	}
	if cloned.Evidence.MockTransactionID == nil || *cloned.Evidence.MockTransactionID != wantMockID {
		t.Fatalf("cloned mock transaction alias changed")
	}
	if cloned.Evidence.Amount == nil || *cloned.Evidence.Amount != 1234 {
		t.Fatalf("cloned amount alias changed")
	}
	if cloned.Evidence.Currency == nil || *cloned.Evidence.Currency != "CNY" {
		t.Fatalf("cloned currency alias changed")
	}
	if cloned.Evidence.Result == nil || *cloned.Evidence.Result != payment.ResultSucceeded {
		t.Fatalf("cloned result alias changed")
	}
	if len(cloned.Evidence.Differences) != 1 || cloned.Evidence.Differences[0] != payment.EvidenceFieldAmount {
		t.Fatalf("cloned differences alias changed")
	}
	if cloned.Reason == nil || cloned.Reason.Code != "REASON" || cloned.Reason.Message == nil || *cloned.Reason.Message != "message" {
		t.Fatalf("cloned reason alias changed")
	}
	if cloned.Candidate == nil || cloned.Candidate.MockTransactionID == nil || *cloned.Candidate.MockTransactionID != wantCandidateMockID {
		t.Fatalf("cloned candidate alias changed")
	}
}

func TestDecodeEvidenceAcceptsOnlyTypedAllowedFields(t *testing.T) {
	empty, err := decodeEvidence([]byte(`{}`))
	if err != nil {
		t.Fatalf("empty evidence object was rejected")
	}
	if empty.PlatformReferenceID != nil || empty.MockTransactionID != nil || empty.Amount != nil ||
		empty.Currency != nil || empty.Result != nil || empty.Differences != nil {
		t.Fatalf("empty evidence object produced unexpected fields")
	}

	raw := []byte(`{"platform_reference_id":"11111111-1111-4111-8111-111111111111","amount":1234,"currency":"CNY","result":"SUCCEEDED","differences":["amount"]}`)
	evidence, err := decodeEvidence(raw)
	if err != nil {
		t.Fatalf("allowed evidence object was rejected")
	}
	if evidence.PlatformReferenceID == nil || *evidence.PlatformReferenceID != payment.PaymentOrderID("11111111-1111-4111-8111-111111111111") {
		t.Fatalf("allowed platform reference was not decoded")
	}
	if evidence.Amount == nil || *evidence.Amount != 1234 || evidence.Currency == nil || *evidence.Currency != "CNY" {
		t.Fatalf("allowed amount or currency was not decoded")
	}
	if evidence.Result == nil || *evidence.Result != payment.ResultSucceeded {
		t.Fatalf("allowed result was not decoded")
	}
	if len(evidence.Differences) != 1 || evidence.Differences[0] != payment.EvidenceFieldAmount {
		t.Fatalf("allowed differences were not decoded")
	}

	for _, invalid := range [][]byte{
		[]byte(`null`),
		[]byte(`[]`),
		[]byte(`{"unexpected":"value"}`),
		[]byte(`{"amount":"not-an-integer"}`),
	} {
		if _, err := decodeEvidence(invalid); !errors.Is(err, errStoredEvidence) {
			t.Fatalf("invalid stored evidence was not rejected by the fixed error")
		}
	}
}

func TestClassifyRegistrationErrorNonBusinessUnique(t *testing.T) {
	// A unique collision other than the targeted business-key conflict is a
	// definite non-business rejection, never intent reuse. This unit assertion
	// is the fast red/green companion to the real-PostgreSQL collision test.
	for _, constraint := range []string{"payment_orders_pkey", "payment_orders_mock_transaction_id_key"} {
		err := &pgconn.PgError{Code: "23505", ConstraintName: constraint}
		classified := classifyRegistrationError(err)
		if !errors.Is(classified, payment.ErrRegistrationRejected) {
			t.Fatalf("non-business unique error did not map to registration rejected")
		}
		if errors.Is(classified, payment.ErrConflict) {
			t.Fatalf("non-business unique error was misclassified as an intent conflict")
		}
	}
	if got := classifyRegistrationError(errors.New("storage fault")); !errors.Is(got, payment.ErrUnavailable) {
		t.Fatalf("non-unique storage error changed its unavailable classification")
	}
	if got := classifyRegistrationError(nil); got != nil {
		t.Fatalf("nil error changed classification")
	}
}

func TestStoreNilSafety(t *testing.T) {
	var nilStore *Store
	if _, err := nilStore.RegisterOrder(context.Background(), payment.RegisterOrderInput{}); !errors.Is(err, payment.ErrUnavailable) {
		t.Fatalf("nil store registration did not return unavailable")
	}
	if _, err := nilStore.SaveObservation(context.Background(), payment.Observation{}); !errors.Is(err, payment.ErrUnavailable) {
		t.Fatalf("nil store observation did not return unavailable")
	}

	emptyStore := NewStore(nil, nil)
	if _, err := emptyStore.RegisterOrder(context.Background(), payment.RegisterOrderInput{}); !errors.Is(err, payment.ErrUnavailable) {
		t.Fatalf("nil database registration did not return unavailable")
	}
	if _, err := emptyStore.SaveObservation(context.Background(), payment.Observation{}); !errors.Is(err, payment.ErrUnavailable) {
		t.Fatalf("nil database observation did not return unavailable")
	}
}
