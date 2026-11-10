//go:build integration

package mock

// T025 integration tests. This is the target-behavior test snapshot: it must
// compile against the safe outline, but its assertions are expected to be red
// until T030/T035 are authorized after the parent proves the target red on the
// shared fresh PostgreSQL lane. It never truncates tables or touches a global
// database; every case generates its own platform UUID, never creates a
// payment_orders row, and deletes only that reference from mock_transactions in
// cleanup. A passing case therefore does not depend on platform order existence.

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"payment-hub/internal/payment"
	"payment-hub/tests/harness"
)

const (
	mockTransactionIntegrationBudget = 3 * time.Second
	mockTransactionCleanupBudget     = 3 * time.Second
	mockTransactionTestCanary        = "FAKE_T025_MOCK_TRANSACTION_CANARY"
)

var _ DB = (*pgxpool.Pool)(nil)

type mockTransactionTestControl struct {
	paymentEntries  int
	queryEntries    int
	committed       int
	paymentEntryErr error
	committedErr    error
	onCommitted     func(payment.PaymentOrderID) error
}

func (c *mockTransactionTestControl) PaymentEntry(context.Context, payment.PaymentOrderID) error {
	c.paymentEntries++
	return c.paymentEntryErr
}

func (c *mockTransactionTestControl) QueryEntry(context.Context, payment.PaymentOrderID) error {
	c.queryEntries++
	return nil
}

func (c *mockTransactionTestControl) Committed(_ context.Context, order payment.PaymentOrderID) error {
	c.committed++
	if c.onCommitted != nil {
		return c.onCommitted(order)
	}
	return c.committedErr
}

func newMockTransactionCase(t *testing.T, pool *pgxpool.Pool) payment.PaymentOrderID {
	t.Helper()
	reference := payment.PaymentOrderID(payment.NewUUIDv4())
	if err := payment.ValidateUUIDv4(string(reference)); err != nil {
		t.Fatal("mock test case setup: generated platform reference is not UUIDv4")
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), mockTransactionCleanupBudget)
		defer cancel()
		if _, err := pool.Exec(ctx, `DELETE FROM mock_transactions WHERE platform_reference_id = $1`, string(reference)); err != nil {
			t.Errorf("mock test cleanup failed (%s)", mockTransactionTestSQLState(err))
		}
	})
	return reference
}

func mockTransactionTestContext(t *testing.T) (context.Context, context.CancelFunc) {
	t.Helper()
	return context.WithTimeout(context.Background(), mockTransactionIntegrationBudget)
}

func readMockTransaction(t *testing.T, pool *pgxpool.Pool, reference payment.PaymentOrderID) (payment.MockTransaction, bool) {
	t.Helper()
	ctx, cancel := mockTransactionTestContext(t)
	defer cancel()

	var (
		id, platformReference, currency, result string
		amount                                  int64
		processedAt                             time.Time
	)
	err := pool.QueryRow(ctx, `
		SELECT id::text, platform_reference_id::text, amount, currency, result, processed_at
		FROM mock_transactions
		WHERE platform_reference_id = $1
	`, string(reference)).Scan(&id, &platformReference, &amount, &currency, &result, &processedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return payment.MockTransaction{}, false
	}
	if err != nil {
		t.Fatalf("mock transaction read failed (%s)", mockTransactionTestSQLState(err))
	}
	return payment.MockTransaction{
		ID:                  payment.MockTransactionID(id),
		PlatformReferenceID: payment.PaymentOrderID(platformReference),
		Amount:              amount,
		Currency:            currency,
		Result:              payment.Result(result),
		ProcessedAt:         processedAt.UTC(),
	}, true
}

func countMockTransactions(t *testing.T, pool *pgxpool.Pool, reference payment.PaymentOrderID) int {
	t.Helper()
	ctx, cancel := mockTransactionTestContext(t)
	defer cancel()

	var count int
	if err := pool.QueryRow(ctx, `
		SELECT count(*)
		FROM mock_transactions
		WHERE platform_reference_id = $1
	`, string(reference)).Scan(&count); err != nil {
		t.Fatalf("mock transaction count failed (%s)", mockTransactionTestSQLState(err))
	}
	return count
}

func mockTransactionTestSQLState(err error) string {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return "timeout"
	}
	if errors.Is(err, context.Canceled) {
		return "canceled"
	}
	return "none"
}

func mockTransactionTestErrorClass(err error) string {
	switch {
	case err == nil:
		return "none"
	case errors.Is(err, payment.ErrUnavailable):
		return "unavailable"
	case errors.Is(err, payment.ErrInvalidInput):
		return "invalid input"
	case errors.Is(err, payment.ErrConflict):
		return "conflict"
	case errors.Is(err, payment.ErrIndeterminate):
		return "indeterminate"
	case errors.Is(err, payment.ErrObservationConflict):
		return "observation conflict"
	case errors.Is(err, payment.ErrNotFound):
		return "not found"
	case errors.Is(err, ErrReplyHidden):
		return "reply hidden"
	case errors.Is(err, ErrControlFault):
		return "control fault"
	default:
		return "unexpected"
	}
}

func requireMockTransactionEqual(t *testing.T, got, want payment.MockTransaction) {
	t.Helper()
	if got.ID != want.ID {
		t.Fatalf("stored mock transaction ID differs")
	}
	if got.PlatformReferenceID != want.PlatformReferenceID {
		t.Fatalf("stored mock platform reference differs")
	}
	if got.Amount != want.Amount {
		t.Fatalf("stored mock amount differs")
	}
	if got.Currency != want.Currency {
		t.Fatalf("stored mock currency differs")
	}
	if got.Result != want.Result {
		t.Fatalf("stored mock result differs")
	}
	if delta := got.ProcessedAt.Sub(want.ProcessedAt); delta < -time.Microsecond || delta > time.Microsecond {
		t.Fatalf("stored mock processed time differs")
	}
}

func requireMockTimesClose(t *testing.T, first, second time.Time) {
	t.Helper()
	if delta := first.Sub(second); delta < -time.Microsecond || delta > time.Microsecond {
		t.Fatalf("mock processed time changed")
	}
}

func TestMockTransactionIntegration(t *testing.T) {
	pool := harness.OpenCommittedPostgresPool(t)
	if pool == nil {
		t.Fatal("mock integration fixture returned a nil pool")
	}

	t.Run("success persists actual request values with independent identity", func(t *testing.T) {
		reference := newMockTransactionCase(t, pool)
		control := &mockTransactionTestControl{}
		committedSawRow := false
		control.onCommitted = func(order payment.PaymentOrderID) error {
			if order != reference {
				return errors.New("committed hook received a different order")
			}
			if _, ok := readMockTransaction(t, pool, order); !ok {
				return errors.New("committed hook ran before the fact was committed")
			}
			committedSawRow = true
			return nil
		}

		channel := NewChannel(pool, NewHooks(control))
		if channel == nil {
			t.Fatal("NewChannel returned nil")
		}
		request := payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              12345,
			Currency:            "USD",
		}

		before := time.Now().UTC()
		ctx, cancel := mockTransactionTestContext(t)
		defer cancel()
		transaction, err := channel.Pay(ctx, request)
		after := time.Now().UTC()
		if err != nil {
			t.Fatalf("pay failed (%s)", mockTransactionTestErrorClass(err))
		}

		if transaction.Result != payment.ResultSucceeded {
			t.Fatalf("default constructor did not explicitly succeed")
		}
		if err := payment.ValidateUUIDv4(string(transaction.ID)); err != nil {
			t.Fatalf("mock transaction ID is not UUIDv4")
		}
		if transaction.ID == payment.MockTransactionID(reference) {
			t.Fatalf("mock transaction ID reused the platform reference")
		}
		if transaction.PlatformReferenceID != reference {
			t.Fatalf("mock platform reference differs from the actual request")
		}
		if transaction.Amount != request.Amount {
			t.Fatalf("mock amount differs from the actual request")
		}
		if transaction.Currency != request.Currency {
			t.Fatalf("mock currency differs from the actual request")
		}
		if transaction.ProcessedAt.Location() != time.UTC {
			t.Fatalf("mock processed time is not UTC")
		}
		// PostgreSQL stores timestamptz at microsecond precision, so allow one
		// microsecond of truncation at each edge of the observed call window.
		if transaction.ProcessedAt.Before(before.Add(-time.Microsecond)) || transaction.ProcessedAt.After(after.Add(time.Microsecond)) {
			t.Fatalf("mock processed time is outside the payment call window")
		}
		if !committedSawRow {
			t.Fatalf("committed hook did not observe the committed fact")
		}
		if control.paymentEntries != 1 || control.committed != 1 {
			t.Fatalf("unexpected hook counts: entry=%d committed=%d", control.paymentEntries, control.committed)
		}
		if control.queryEntries != 0 {
			t.Fatalf("payment unexpectedly invoked query entry")
		}

		stored, ok := readMockTransaction(t, pool, reference)
		if !ok {
			t.Fatalf("successful mock transaction was not persisted")
		}
		requireMockTransactionEqual(t, stored, transaction)
		if got := countMockTransactions(t, pool, reference); got != 1 {
			t.Fatalf("expected exactly one mock transaction effect, got %d", got)
		}
	})

	t.Run("injected failure persists actual request values without business-field rules", func(t *testing.T) {
		reference := newMockTransactionCase(t, pool)
		control := &mockTransactionTestControl{}
		var generatedRequest payment.MockPaymentRequest
		generatorCalls := 0
		channel := newChannel(pool, NewHooks(control), func(request payment.MockPaymentRequest) payment.Result {
			generatorCalls++
			generatedRequest = request
			return payment.ResultFailed
		})
		if channel == nil {
			t.Fatal("newChannel returned nil")
		}
		request := payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              12345,
			Currency:            "USD",
		}

		ctx, cancel := mockTransactionTestContext(t)
		defer cancel()
		transaction, err := channel.Pay(ctx, request)
		if err != nil {
			t.Fatalf("injected failure pay returned an infrastructure error (%s)", mockTransactionTestErrorClass(err))
		}
		if generatorCalls != 1 {
			t.Fatalf("expected one generator call, got %d", generatorCalls)
		}
		if generatedRequest != request {
			t.Fatalf("generator did not receive the actual request values")
		}
		if transaction.Result != payment.ResultFailed {
			t.Fatalf("injected failure did not persist a FAILED fact")
		}
		if err := payment.ValidateUUIDv4(string(transaction.ID)); err != nil {
			t.Fatalf("mock transaction ID is not UUIDv4")
		}
		if transaction.ID == payment.MockTransactionID(reference) {
			t.Fatalf("mock transaction ID reused the platform reference")
		}
		if transaction.PlatformReferenceID != reference {
			t.Fatalf("mock platform reference differs from the actual request")
		}
		if transaction.Amount != request.Amount {
			t.Fatalf("mock amount differs from the actual request")
		}
		if transaction.Currency != request.Currency {
			t.Fatalf("mock currency differs from the actual request")
		}
		if transaction.ProcessedAt.Location() != time.UTC {
			t.Fatalf("mock processed time is not UTC")
		}

		stored, ok := readMockTransaction(t, pool, reference)
		if !ok {
			t.Fatalf("failed mock transaction was not persisted")
		}
		requireMockTransactionEqual(t, stored, transaction)
		if stored.Result != payment.ResultFailed {
			t.Fatalf("stored mock result is not FAILED")
		}
		if got := countMockTransactions(t, pool, reference); got != 1 {
			t.Fatalf("expected exactly one mock transaction effect, got %d", got)
		}
	})

	t.Run("duplicate pay observes every entry but keeps one effect", func(t *testing.T) {
		reference := newMockTransactionCase(t, pool)
		control := &mockTransactionTestControl{}
		channel := NewChannel(pool, NewHooks(control))
		request := payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              2222,
			Currency:            "CNY",
		}

		ctx, cancel := mockTransactionTestContext(t)
		defer cancel()
		first, err := channel.Pay(ctx, request)
		if err != nil {
			t.Fatalf("first pay failed (%s)", mockTransactionTestErrorClass(err))
		}
		firstStored, ok := readMockTransaction(t, pool, reference)
		if !ok {
			t.Fatalf("first pay did not persist a mock transaction")
		}
		requireMockTransactionEqual(t, firstStored, first)

		second, secondErr := channel.Pay(ctx, request)
		if control.paymentEntries != 2 {
			t.Fatalf("expected payment entry observation for both calls, got %d", control.paymentEntries)
		}
		if got := countMockTransactions(t, pool, reference); got != 1 {
			t.Fatalf("duplicate pay created %d mock transaction effects, want at most 1", got)
		}
		secondStored, ok := readMockTransaction(t, pool, reference)
		if !ok {
			t.Fatalf("duplicate pay removed the original mock transaction")
		}
		requireMockTransactionEqual(t, secondStored, firstStored)

		// US1 only requires that a duplicate call cannot create a second effect.
		// The full identical-reuse versus conflicting-reuse split is T049.
		if secondErr == nil {
			if second.ID != first.ID {
				t.Fatalf("duplicate pay returned a different mock transaction ID")
			}
			if second.Result != first.Result {
				t.Fatalf("duplicate pay changed the mock result")
			}
			requireMockTimesClose(t, second.ProcessedAt, first.ProcessedAt)
		}
	})

	t.Run("missing query neither pays nor creates a transaction", func(t *testing.T) {
		reference := newMockTransactionCase(t, pool)
		control := &mockTransactionTestControl{}
		channel := NewChannel(pool, NewHooks(control))

		ctx, cancel := mockTransactionTestContext(t)
		defer cancel()
		for attempt := 1; attempt <= 2; attempt++ {
			transaction, found, err := channel.FindTransactionByPlatformReference(ctx, reference)
			if err != nil {
				t.Fatalf("missing query failed (%s)", mockTransactionTestErrorClass(err))
			}
			if found {
				t.Fatalf("missing query unexpectedly found a transaction")
			}
			if transaction.ID != "" {
				t.Fatalf("missing query returned a transaction ID")
			}
		}
		if control.paymentEntries != 0 {
			t.Fatalf("query invoked the payment entry observation")
		}
		if got := countMockTransactions(t, pool, reference); got != 0 {
			t.Fatalf("query created %d mock transaction effects", got)
		}
	})

	t.Run("normal query returns saved facts without payment or mutation", func(t *testing.T) {
		reference := newMockTransactionCase(t, pool)
		control := &mockTransactionTestControl{}
		channel := NewChannel(pool, NewHooks(control))
		request := payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              6543,
			Currency:            "JPY",
		}

		ctx, cancel := mockTransactionTestContext(t)
		defer cancel()
		paid, err := channel.Pay(ctx, request)
		if err != nil {
			t.Fatalf("setup pay failed (%s)", mockTransactionTestErrorClass(err))
		}
		storedBefore, ok := readMockTransaction(t, pool, reference)
		if !ok {
			t.Fatalf("setup pay did not persist a mock transaction")
		}
		requireMockTransactionEqual(t, storedBefore, paid)

		queried, found, err := channel.FindTransactionByPlatformReference(ctx, reference)
		if err != nil {
			t.Fatalf("normal query failed (%s)", mockTransactionTestErrorClass(err))
		}
		if !found {
			t.Fatalf("normal query did not find the saved transaction")
		}
		requireMockTransactionEqual(t, queried, storedBefore)
		if control.paymentEntries != 1 {
			t.Fatalf("normal query changed the payment entry count")
		}

		storedAfter, ok := readMockTransaction(t, pool, reference)
		if !ok {
			t.Fatalf("normal query removed the saved transaction")
		}
		requireMockTransactionEqual(t, storedAfter, storedBefore)
		if got := countMockTransactions(t, pool, reference); got != 1 {
			t.Fatalf("normal query changed the mock transaction effect count to %d", got)
		}
	})

	t.Run("invalid generator result is an explicit fault without a row", func(t *testing.T) {
		reference := newMockTransactionCase(t, pool)
		control := &mockTransactionTestControl{}
		channel := newChannel(pool, NewHooks(control), func(payment.MockPaymentRequest) payment.Result {
			return payment.ResultUnknown
		})

		ctx, cancel := mockTransactionTestContext(t)
		defer cancel()
		transaction, err := channel.Pay(ctx, payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              100,
			Currency:            "CNY",
		})
		if err == nil {
			t.Fatalf("invalid generator result did not return an explicit fault")
		}
		if !errors.Is(err, payment.ErrUnavailable) {
			t.Fatalf("invalid generator result returned an unsafe fault (%s)", mockTransactionTestErrorClass(err))
		}
		if transaction.Result == payment.ResultUnknown {
			t.Fatalf("invalid generator result returned UNKNOWN as a fact")
		}
		if control.committed != 0 {
			t.Fatalf("invalid generator result reached the committed hook")
		}
		if got := countMockTransactions(t, pool, reference); got != 0 {
			t.Fatalf("invalid generator result wrote %d mock transaction rows", got)
		}
	})

	t.Run("committed hook control fault preserves committed fact", func(t *testing.T) {
		reference := newMockTransactionCase(t, pool)
		control := &mockTransactionTestControl{committedErr: errors.New(mockTransactionTestCanary)}
		channel := NewChannel(pool, NewHooks(control))
		request := payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              3456,
			Currency:            "USD",
		}

		ctx, cancel := mockTransactionTestContext(t)
		defer cancel()
		_, err := channel.Pay(ctx, request)
		if !errors.Is(err, ErrControlFault) {
			t.Fatalf("committed control fault returned an unsafe error (%s)", mockTransactionTestErrorClass(err))
		}
		if strings.Contains(err.Error(), mockTransactionTestCanary) {
			t.Fatalf("committed control fault echoed the raw control error")
		}
		if control.committed != 1 {
			t.Fatalf("expected one committed hook call, got %d", control.committed)
		}
		stored, ok := readMockTransaction(t, pool, reference)
		if !ok {
			t.Fatalf("committed control fault removed the committed fact")
		}
		if stored.Result != payment.ResultSucceeded {
			t.Fatalf("committed control fault changed the committed result")
		}
		if stored.Amount != request.Amount || stored.Currency != request.Currency {
			t.Fatalf("committed control fault changed the committed request values")
		}
		if got := countMockTransactions(t, pool, reference); got != 1 {
			t.Fatalf("committed control fault changed the effect count to %d", got)
		}
	})

	t.Run("reply hidden preserves committed fact", func(t *testing.T) {
		reference := newMockTransactionCase(t, pool)
		control := &mockTransactionTestControl{committedErr: ErrReplyHidden}
		channel := NewChannel(pool, NewHooks(control))
		request := payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              4567,
			Currency:            "USD",
		}

		ctx, cancel := mockTransactionTestContext(t)
		defer cancel()
		_, err := channel.Pay(ctx, request)
		if !errors.Is(err, ErrReplyHidden) {
			t.Fatalf("reply hiding returned an unsafe error (%s)", mockTransactionTestErrorClass(err))
		}
		if control.committed != 1 {
			t.Fatalf("expected one committed hook call, got %d", control.committed)
		}
		stored, ok := readMockTransaction(t, pool, reference)
		if !ok {
			t.Fatalf("reply hiding removed the committed fact")
		}
		if stored.Result != payment.ResultSucceeded {
			t.Fatalf("reply hiding changed the committed result")
		}
		if stored.Amount != request.Amount || stored.Currency != request.Currency {
			t.Fatalf("reply hiding changed the committed request values")
		}
		if got := countMockTransactions(t, pool, reference); got != 1 {
			t.Fatalf("reply hiding changed the effect count to %d", got)
		}
	})

	t.Run("payment entry hook fault prevents a new fact", func(t *testing.T) {
		reference := newMockTransactionCase(t, pool)
		control := &mockTransactionTestControl{paymentEntryErr: errors.New(mockTransactionTestCanary)}
		channel := NewChannel(pool, NewHooks(control))

		ctx, cancel := mockTransactionTestContext(t)
		defer cancel()
		_, err := channel.Pay(ctx, payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              5678,
			Currency:            "USD",
		})
		if !errors.Is(err, ErrControlFault) {
			t.Fatalf("payment entry fault returned an unsafe error (%s)", mockTransactionTestErrorClass(err))
		}
		if control.paymentEntries != 1 {
			t.Fatalf("expected one payment entry observation, got %d", control.paymentEntries)
		}
		if control.committed != 0 {
			t.Fatalf("payment entry fault reached the committed hook")
		}
		if got := countMockTransactions(t, pool, reference); got != 0 {
			t.Fatalf("payment entry fault wrote %d mock transaction rows", got)
		}
	})

	t.Run("canceled external context does not create a fact", func(t *testing.T) {
		reference := newMockTransactionCase(t, pool)
		control := &mockTransactionTestControl{}
		channel := NewChannel(pool, NewHooks(control))

		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		_, err := channel.Pay(ctx, payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              6789,
			Currency:            "USD",
		})
		if !errors.Is(err, payment.ErrUnavailable) {
			t.Fatalf("canceled context returned an unsafe error (%s)", mockTransactionTestErrorClass(err))
		}
		if got := countMockTransactions(t, pool, reference); got != 0 {
			t.Fatalf("canceled context wrote %d mock transaction rows", got)
		}
	})
}
