//go:build !integration

package postgres

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"payment-hub/internal/payment"
)

// These are the frozen T055/US3 same-record retry semantics. The root is a
// default (non-integration) unit test because observationMatchesRecord is the
// pure decision behind same-ID reuse. It does not open a database or call Mock.

func retryTestTime(offset time.Duration) time.Time {
	return time.Date(2026, time.November, 10, 12, 0, 0, 0, time.UTC).Add(offset)
}

func retryTestOrder(result payment.Result, mockID *payment.MockTransactionID, confirmedAt *time.Time) payment.PaymentOrder {
	return payment.PaymentOrder{
		ID:                payment.PaymentOrderID(payment.NewUUIDv4()),
		ClientID:          "retry-test-client",
		BusinessOrderNo:   "retry-test-order",
		Amount:            1234,
		Currency:          "CNY",
		Result:            result,
		MockTransactionID: mockID,
		RegisteredAt:      retryTestTime(0),
		ConfirmedAt:       confirmedAt,
	}
}

func retryTestObservation(order payment.PaymentOrder, source payment.Source) payment.Observation {
	return payment.Observation{
		ID:             payment.ResultHandlingRecordID(payment.NewUUIDv4()),
		PaymentOrderID: order.ID,
		ClientID:       order.ClientID,
		RequestID:      payment.RequestID(payment.NewUUIDv4()),
		Source:         source,
		ObservedAt:     retryTestTime(time.Minute),
	}
}

func retryTestAcceptedObservation(order payment.PaymentOrder, mockID *payment.MockTransactionID) payment.Observation {
	observation := retryTestObservation(order, payment.SourcePayment)
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

func retryTestKeepUnknownObservation(order payment.PaymentOrder) payment.Observation {
	observation := retryTestObservation(order, payment.SourceQuery)
	code := "NO_MATCH"
	observation.Reason = &payment.Reason{Code: code}
	return observation
}

func retryTestRejectedObservation(order payment.PaymentOrder) payment.Observation {
	observation := retryTestObservation(order, payment.SourceQuery)
	reference := order.ID
	amount := order.Amount + 1
	currency := order.Currency
	observation.Evidence = payment.Evidence{
		PlatformReferenceID: &reference,
		Amount:              &amount,
		Currency:            &currency,
		Differences:         []payment.EvidenceFieldName{payment.EvidenceFieldAmount},
	}
	observation.EvidenceRejected = true
	code := "EVIDENCE_MISMATCH"
	observation.Reason = &payment.Reason{Code: code}
	return observation
}

func retryTestRecord(t *testing.T, historical payment.PaymentOrder, observation payment.Observation) payment.ResultHandlingRecord {
	t.Helper()
	_, record, err := payment.DecideObservation(historical, observation, retryTestTime(2*time.Minute))
	if err != nil {
		t.Fatalf("fixture decision failed")
	}
	return record
}

func retryTestMatch(t *testing.T, observation payment.Observation, record payment.ResultHandlingRecord, current payment.PaymentOrder) bool {
	t.Helper()
	match, err := observationMatchesRecord(observation, record, current)
	if err != nil {
		t.Fatalf("same-record comparison returned an unexpected error")
	}
	return match
}

// TestObservationRecordRetrySemantics is the frozen default-unit root for
// T055/T063 same-record reuse. Historical UNKNOWN records must keep matching
// after a later legitimate confirmation, while a historical ACCEPT_CONFIRMATION
// must not be replayed against an opposite current result or a conflicting
// known Mock association.
func TestObservationRecordRetrySemantics(t *testing.T) {
	t.Run("historical keep-unknown stays retryable after a later legitimate confirmation", func(t *testing.T) {
		historical := retryTestOrder(payment.ResultUnknown, nil, nil)
		observation := retryTestKeepUnknownObservation(historical)
		record := retryTestRecord(t, historical, observation)
		if record.Decision != payment.DecisionKeepUnknown || record.ResultBefore != payment.ResultUnknown || record.ResultAfter != payment.ResultUnknown {
			t.Fatalf("fixture did not produce a historical KEEP_UNKNOWN record")
		}

		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		confirmedAt := retryTestTime(time.Minute)
		current := historical
		current.Result = payment.ResultSucceeded
		current.MockTransactionID = &mockID
		current.ConfirmedAt = &confirmedAt

		if !retryTestMatch(t, observation, record, current) {
			t.Fatalf("historical KEEP_UNKNOWN record stopped matching after a later legitimate confirmation")
		}
	})

	t.Run("historical reject-unknown stays retryable after a later legitimate confirmation", func(t *testing.T) {
		historical := retryTestOrder(payment.ResultUnknown, nil, nil)
		observation := retryTestRejectedObservation(historical)
		record := retryTestRecord(t, historical, observation)
		if record.Decision != payment.DecisionRejectEvidence || record.ResultAfter != payment.ResultUnknown {
			t.Fatalf("fixture did not produce a historical UNKNOWN-after REJECT_EVIDENCE record")
		}

		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		confirmedAt := retryTestTime(time.Minute)
		current := historical
		current.Result = payment.ResultFailed
		current.MockTransactionID = &mockID
		current.ConfirmedAt = &confirmedAt

		if !retryTestMatch(t, observation, record, current) {
			t.Fatalf("historical UNKNOWN-after REJECT_EVIDENCE record stopped matching after a later legitimate confirmation")
		}
	})

	t.Run("historical accept-confirmation rejects an opposite current result", func(t *testing.T) {
		historical := retryTestOrder(payment.ResultUnknown, nil, nil)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		observation := retryTestAcceptedObservation(historical, &mockID)
		record := retryTestRecord(t, historical, observation)
		if record.Decision != payment.DecisionAcceptConfirmation || record.ResultAfter != payment.ResultSucceeded {
			t.Fatalf("fixture did not produce a historical ACCEPT_CONFIRMATION record")
		}

		confirmedAt := retryTestTime(time.Minute)
		current := historical
		current.Result = payment.ResultFailed
		current.MockTransactionID = &mockID
		current.ConfirmedAt = &confirmedAt

		if retryTestMatch(t, observation, record, current) {
			t.Fatalf("historical ACCEPT_CONFIRMATION was replayed against an opposite current result")
		}
	})

	t.Run("historical accept-confirmation rejects a conflicting known Mock association", func(t *testing.T) {
		historical := retryTestOrder(payment.ResultUnknown, nil, nil)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		observation := retryTestAcceptedObservation(historical, &mockID)
		record := retryTestRecord(t, historical, observation)

		otherMockID := payment.MockTransactionID(payment.NewUUIDv4())
		confirmedAt := retryTestTime(time.Minute)
		current := historical
		current.Result = payment.ResultSucceeded
		current.MockTransactionID = &otherMockID
		current.ConfirmedAt = &confirmedAt

		if retryTestMatch(t, observation, record, current) {
			t.Fatalf("historical ACCEPT_CONFIRMATION was replayed against a conflicting known Mock association")
		}
	})

	t.Run("historical accept-confirmation rejects a missing known Mock association", func(t *testing.T) {
		historical := retryTestOrder(payment.ResultUnknown, nil, nil)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		observation := retryTestAcceptedObservation(historical, &mockID)
		record := retryTestRecord(t, historical, observation)

		confirmedAt := retryTestTime(time.Minute)
		current := historical
		current.Result = payment.ResultSucceeded
		current.MockTransactionID = nil
		current.ConfirmedAt = &confirmedAt

		if retryTestMatch(t, observation, record, current) {
			t.Fatalf("historical ACCEPT_CONFIRMATION was replayed without its known Mock association")
		}
	})

	t.Run("historical accept-confirmation rejects a missing current confirmation time", func(t *testing.T) {
		historical := retryTestOrder(payment.ResultUnknown, nil, nil)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		observation := retryTestAcceptedObservation(historical, &mockID)
		record := retryTestRecord(t, historical, observation)

		current := historical
		current.Result = payment.ResultSucceeded
		current.MockTransactionID = &mockID
		current.ConfirmedAt = nil

		if retryTestMatch(t, observation, record, current) {
			t.Fatalf("historical ACCEPT_CONFIRMATION was replayed without a current confirmation time")
		}
	})

	t.Run("historical accept-confirmation matches the same current confirmation", func(t *testing.T) {
		historical := retryTestOrder(payment.ResultUnknown, nil, nil)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		observation := retryTestAcceptedObservation(historical, &mockID)
		record := retryTestRecord(t, historical, observation)

		confirmedAt := retryTestTime(time.Minute)
		current := historical
		current.Result = payment.ResultSucceeded
		current.MockTransactionID = &mockID
		current.ConfirmedAt = &confirmedAt

		if !retryTestMatch(t, observation, record, current) {
			t.Fatalf("historical ACCEPT_CONFIRMATION stopped matching the same current confirmation")
		}
	})

	t.Run("nil snapshot never returns a trace-save marker", func(t *testing.T) {
		for _, sentinel := range []error{payment.ErrTraceSaveFailed, payment.ErrTraceSaveIndeterminate} {
			result, err := traceSaveResult(nil, sentinel)
			if errors.Is(err, payment.ErrTraceSaveFailed) || errors.Is(err, payment.ErrTraceSaveIndeterminate) {
				t.Fatalf("nil snapshot returned a trace-save marker")
			}
			if !errors.Is(err, payment.ErrIndeterminate) {
				t.Fatalf("nil snapshot did not return the ordinary indeterminate sentinel")
			}
			if !reflect.DeepEqual(result, payment.SaveObservationResult{}) {
				t.Fatalf("nil snapshot returned a partial save result")
			}
		}
	})

	t.Run("original UNKNOWN duplicate rollback uncertainty never returns a trace-save marker", func(t *testing.T) {
		order := retryTestOrder(payment.ResultUnknown, nil, nil)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		observation := retryTestAcceptedObservation(order, &mockID)

		tx := &retryTestTx{
			order:       order,
			insertErr:   &pgconn.PgError{Code: "23505", ConstraintName: "result_handling_records_pkey"},
			rollbackErr: errors.New("rollback outcome unknown"),
		}
		store := NewStore(&retryTestDB{tx: tx}, NewHooks(nil))
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()

		result, err := store.SaveObservation(ctx, observation)
		if errors.Is(err, payment.ErrTraceSaveFailed) || errors.Is(err, payment.ErrTraceSaveIndeterminate) {
			t.Fatalf("original UNKNOWN duplicate rollback uncertainty returned a trace-save marker")
		}
		if !errors.Is(err, payment.ErrIndeterminate) {
			t.Fatalf("original UNKNOWN duplicate rollback uncertainty did not return the ordinary indeterminate sentinel")
		}
		if !reflect.DeepEqual(result, payment.SaveObservationResult{}) {
			t.Fatalf("original UNKNOWN duplicate rollback uncertainty returned a partial save result")
		}
	})

	t.Run("original confirmation snapshot requires non-zero UTC registration time", func(t *testing.T) {
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		confirmedAt := retryTestTime(time.Minute)
		order := retryTestOrder(payment.ResultSucceeded, &mockID, &confirmedAt)
		observation := retryTestKeepUnknownObservation(order)

		if _, err := snapshotOriginalConfirmation(order, observation); err != nil {
			t.Fatalf("valid original confirmation was rejected")
		}

		zero := order
		zero.RegisteredAt = time.Time{}
		if _, err := snapshotOriginalConfirmation(zero, observation); !errors.Is(err, payment.ErrUnavailable) {
			t.Fatalf("zero registration time was accepted for a marker snapshot")
		}

		nonUTC := order
		nonUTC.RegisteredAt = order.RegisteredAt.In(time.FixedZone("offset", 3600))
		if _, err := snapshotOriginalConfirmation(nonUTC, observation); !errors.Is(err, payment.ErrUnavailable) {
			t.Fatalf("non-UTC registration time was accepted for a marker snapshot")
		}
	})
}

// retryTestDB and retryTestTx are the minimal default-unit database surface for
// the duplicate rollback-uncertain branch. They never claim real PostgreSQL:
// the synthetic 23505 only routes the unit branch, while the real collision is
// covered by the integration fixture.
type retryTestDB struct {
	tx pgx.Tx
}

func (db *retryTestDB) BeginTx(context.Context, pgx.TxOptions) (pgx.Tx, error) {
	return db.tx, nil
}

func (db *retryTestDB) QueryRow(context.Context, string, ...any) pgx.Row {
	return retryTestRow{err: pgx.ErrNoRows}
}

type retryTestTx struct {
	pgx.Tx
	order       payment.PaymentOrder
	queryCalls  int
	insertErr   error
	rollbackErr error
}

func (tx *retryTestTx) QueryRow(context.Context, string, ...any) pgx.Row {
	tx.queryCalls++
	if tx.queryCalls == 1 {
		order := tx.order
		return retryTestRow{order: &order}
	}
	return retryTestRow{err: pgx.ErrNoRows}
}

func (tx *retryTestTx) Exec(_ context.Context, sql string, _ ...any) (pgconn.CommandTag, error) {
	if strings.Contains(strings.ToLower(sql), "insert into result_handling_records") {
		return pgconn.CommandTag{}, tx.insertErr
	}
	return pgconn.NewCommandTag("UPDATE 1"), nil
}

func (tx *retryTestTx) Rollback(context.Context) error {
	return tx.rollbackErr
}

func (tx *retryTestTx) Commit(context.Context) error {
	return errors.New("unexpected commit")
}

type retryTestRow struct {
	order *payment.PaymentOrder
	err   error
}

func (row retryTestRow) Scan(dest ...any) error {
	if row.err != nil {
		return row.err
	}
	if row.order == nil || len(dest) != 9 {
		return errors.New("unexpected scan shape")
	}
	order := row.order
	*dest[0].(*string) = string(order.ID)
	*dest[1].(*string) = order.ClientID
	*dest[2].(*string) = order.BusinessOrderNo
	*dest[3].(*int64) = order.Amount
	*dest[4].(*string) = order.Currency
	*dest[5].(*string) = string(order.Result)
	if order.MockTransactionID != nil {
		id := string(*order.MockTransactionID)
		*dest[6].(**string) = &id
	} else {
		*dest[6].(**string) = nil
	}
	*dest[7].(*time.Time) = order.RegisteredAt
	if order.ConfirmedAt != nil {
		at := *order.ConfirmedAt
		*dest[8].(**time.Time) = &at
	} else {
		*dest[8].(**time.Time) = nil
	}
	return nil
}
