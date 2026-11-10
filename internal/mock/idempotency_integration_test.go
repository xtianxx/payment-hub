//go:build integration

package mock

// T049 real-PostgreSQL target test for the Mock channel's independent
// duplicate defense. It verifies that identical duplicates reuse the original
// transaction without changing its ID, result, or processed time, while a
// conflicting amount or currency is rejected without rewriting the original
// fact. Every Pay call must be observed independently at the entry hook; the
// stored row count is a separate effect assertion and is never used to infer
// how many calls happened.

import (
	"errors"
	"testing"

	"payment-hub/internal/payment"
	"payment-hub/tests/harness"
)

func TestMockIdempotencyIntegration(t *testing.T) {
	pool := harness.OpenCommittedPostgresPool(t)

	t.Run("success_duplicate_reuses_original", func(t *testing.T) {
		reference := newMockTransactionCase(t, pool)
		control := &mockTransactionTestControl{}
		channel := NewChannel(pool, NewHooks(control))
		request := payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              1234,
			Currency:            "CNY",
		}

		ctx, cancel := mockTransactionTestContext(t)
		defer cancel()

		first, err := channel.Pay(ctx, request)
		if err != nil {
			t.Fatalf("first success pay failed (%s)", mockTransactionTestErrorClass(err))
		}
		if first.Result != payment.ResultSucceeded {
			t.Fatalf("default channel did not persist a SUCCEEDED fact")
		}
		storedBefore, found := readMockTransaction(t, pool, reference)
		if !found {
			t.Fatalf("first success pay did not persist a mock transaction")
		}
		requireMockTransactionEqual(t, storedBefore, first)

		second, err := channel.Pay(ctx, request)
		if err != nil {
			t.Fatalf("identical success duplicate failed (%s)", mockTransactionTestErrorClass(err))
		}
		requireMockTransactionEqual(t, second, first)

		storedAfter, found := readMockTransaction(t, pool, reference)
		if !found {
			t.Fatalf("identical success duplicate removed the original mock transaction")
		}
		requireMockTransactionEqual(t, storedAfter, storedBefore)

		if control.paymentEntries != 2 {
			t.Fatalf("entry observation counted %d calls, want 2 independent calls", control.paymentEntries)
		}
		if control.committed != 1 {
			t.Fatalf("committed observation fired %d times, want 1 effect", control.committed)
		}
		if got := countMockTransactions(t, pool, reference); got != 1 {
			t.Fatalf("identical success duplicate produced %d effects, want 1", got)
		}
	})

	t.Run("failed_duplicate_reuses_original", func(t *testing.T) {
		reference := newMockTransactionCase(t, pool)
		control := &mockTransactionTestControl{}
		generatorCalls := 0
		channel := newChannel(pool, NewHooks(control), func(payment.MockPaymentRequest) payment.Result {
			generatorCalls++
			return payment.ResultFailed
		})
		request := payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              4321,
			Currency:            "CNY",
		}

		ctx, cancel := mockTransactionTestContext(t)
		defer cancel()

		first, err := channel.Pay(ctx, request)
		if err != nil {
			t.Fatalf("first failed pay returned an infrastructure error (%s)", mockTransactionTestErrorClass(err))
		}
		if first.Result != payment.ResultFailed {
			t.Fatalf("failed channel did not persist a FAILED fact")
		}
		storedBefore, found := readMockTransaction(t, pool, reference)
		if !found {
			t.Fatalf("first failed pay did not persist a mock transaction")
		}
		requireMockTransactionEqual(t, storedBefore, first)

		second, err := channel.Pay(ctx, request)
		if err != nil {
			t.Fatalf("identical failed duplicate failed (%s)", mockTransactionTestErrorClass(err))
		}
		requireMockTransactionEqual(t, second, first)

		storedAfter, found := readMockTransaction(t, pool, reference)
		if !found {
			t.Fatalf("identical failed duplicate removed the original mock transaction")
		}
		requireMockTransactionEqual(t, storedAfter, storedBefore)
		if storedAfter.Result != payment.ResultFailed {
			t.Fatalf("identical failed duplicate changed the original FAILED result")
		}

		if generatorCalls != 1 {
			t.Fatalf("result generator ran %d times, want only the first effect", generatorCalls)
		}
		if control.paymentEntries != 2 {
			t.Fatalf("entry observation counted %d calls, want 2 independent calls", control.paymentEntries)
		}
		if control.committed != 1 {
			t.Fatalf("committed observation fired %d times, want 1 effect", control.committed)
		}
		if got := countMockTransactions(t, pool, reference); got != 1 {
			t.Fatalf("identical failed duplicate produced %d effects, want 1", got)
		}
	})

	t.Run("conflicting_duplicate_does_not_rewrite_original", func(t *testing.T) {
		reference := newMockTransactionCase(t, pool)
		control := &mockTransactionTestControl{}
		channel := NewChannel(pool, NewHooks(control))
		original := payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              1000,
			Currency:            "CNY",
		}

		ctx, cancel := mockTransactionTestContext(t)
		defer cancel()

		first, err := channel.Pay(ctx, original)
		if err != nil {
			t.Fatalf("original success pay failed (%s)", mockTransactionTestErrorClass(err))
		}
		storedBefore, found := readMockTransaction(t, pool, reference)
		if !found {
			t.Fatalf("original success pay did not persist a mock transaction")
		}
		requireMockTransactionEqual(t, storedBefore, first)

		conflictingAmount := payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              2000,
			Currency:            "CNY",
		}
		if _, err := channel.Pay(ctx, conflictingAmount); !errors.Is(err, payment.ErrConflict) {
			t.Fatalf("conflicting amount returned %s", mockTransactionTestErrorClass(err))
		}
		afterAmount, found := readMockTransaction(t, pool, reference)
		if !found {
			t.Fatalf("conflicting amount removed the original mock transaction")
		}
		requireMockTransactionEqual(t, afterAmount, storedBefore)
		if afterAmount.Result != payment.ResultSucceeded {
			t.Fatalf("conflicting amount rewrote the original result to %s", afterAmount.Result)
		}

		conflictingCurrency := payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              1000,
			Currency:            "USD",
		}
		if _, err := channel.Pay(ctx, conflictingCurrency); !errors.Is(err, payment.ErrConflict) {
			t.Fatalf("conflicting currency returned %s", mockTransactionTestErrorClass(err))
		}
		afterCurrency, found := readMockTransaction(t, pool, reference)
		if !found {
			t.Fatalf("conflicting currency removed the original mock transaction")
		}
		requireMockTransactionEqual(t, afterCurrency, storedBefore)
		if afterCurrency.Result != payment.ResultSucceeded {
			t.Fatalf("conflicting currency rewrote the original result to %s", afterCurrency.Result)
		}

		// Three independent Pay calls were observed, but only the first
		// committed an effect. The row count must not be used to infer calls.
		if control.paymentEntries != 3 {
			t.Fatalf("entry observation counted %d calls, want 3 independent calls", control.paymentEntries)
		}
		if control.committed != 1 {
			t.Fatalf("committed observation fired %d times, want 1 effect", control.committed)
		}
		if got := countMockTransactions(t, pool, reference); got != 1 {
			t.Fatalf("conflicting duplicates produced %d effects, want 1", got)
		}
	})
}
