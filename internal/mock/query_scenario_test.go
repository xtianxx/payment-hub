package mock

import (
	"context"
	"errors"
	"testing"
	"time"

	"payment-hub/internal/payment"
)

// TestMockQueryScenarios freezes the trusted query scenario execution and the
// applied-proof callback. It uses only the existing no-PG fake DB from
// transaction_unit_test.go and never opens PostgreSQL, the launcher, Docker, or
// the network.

type queryScenarioTestControl struct {
	scenario   QueryScenario
	entryErr   error
	appliedErr error

	entryCalls   int
	appliedCalls int
	outcomes     []QueryOutcome
	appliedCtxOK []bool
}

func (c *queryScenarioTestControl) PaymentEntry(context.Context, payment.PaymentOrderID) error {
	return nil
}

func (c *queryScenarioTestControl) QueryEntry(context.Context, payment.PaymentOrderID) error {
	c.entryCalls++
	return nil
}

func (c *queryScenarioTestControl) Committed(context.Context, payment.PaymentOrderID) error {
	return nil
}

func (c *queryScenarioTestControl) QueryEntryResult(_ context.Context, _ payment.PaymentOrderID) (QueryEntryOutcome, error) {
	if c.entryErr != nil {
		return QueryEntryOutcome{}, c.entryErr
	}
	return QueryEntryOutcome{
		Scenario: c.scenario,
		OnApplied: func(appliedCtx context.Context, outcome QueryOutcome) error {
			c.appliedCalls++
			c.outcomes = append(c.outcomes, outcome)
			c.appliedCtxOK = append(c.appliedCtxOK, appliedCtx.Err() == nil)
			return c.appliedErr
		},
	}, nil
}

func queryScenarioExisting(reference payment.PaymentOrderID) payment.MockTransaction {
	return payment.MockTransaction{
		ID:                  payment.MockTransactionID(payment.NewUUIDv4()),
		PlatformReferenceID: reference,
		Amount:              1234,
		Currency:            "CNY",
		Result:              payment.ResultSucceeded,
		ProcessedAt:         time.Now().UTC().Truncate(time.Microsecond),
	}
}

func queryScenarioRun(t *testing.T, scenario QueryScenario, reference payment.PaymentOrderID, existing *payment.MockTransaction) (payment.MockTransaction, bool, error, *queryScenarioTestControl, *mockUnitDB) {
	t.Helper()
	db := newMockUnitDB()
	if existing != nil {
		db.existing[reference] = *existing
	}
	control := &queryScenarioTestControl{scenario: scenario}
	channel := NewChannel(db, NewHooks(control))
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	transaction, found, err := channel.FindTransactionByPlatformReference(ctx, reference)
	return transaction, found, err, control, db
}

func queryScenarioRequireOneOutcome(t *testing.T, control *queryScenarioTestControl, want QueryOutcome) {
	t.Helper()
	if control.appliedCalls != 1 {
		t.Fatal("query scenario applied callback was not called exactly once")
	}
	if len(control.outcomes) != 1 || control.outcomes[0] != want {
		t.Fatal("query scenario reported the wrong actual outcome")
	}
	if len(control.appliedCtxOK) != 1 || !control.appliedCtxOK[0] {
		t.Fatal("query scenario callback did not use the still-valid operation context")
	}
}

func queryScenarioSameExcept(t *testing.T, original, changed payment.MockTransaction, field string) {
	t.Helper()
	if changed.ProcessedAt != original.ProcessedAt {
		t.Fatal("query scenario changed processed_at")
	}
	switch field {
	case "reference":
		if changed.PlatformReferenceID == original.PlatformReferenceID {
			t.Fatal("reference mismatch scenario did not change the reference")
		}
		changed.PlatformReferenceID = original.PlatformReferenceID
	case "amount":
		if changed.Amount == original.Amount {
			t.Fatal("amount mismatch scenario did not change the amount")
		}
		changed.Amount = original.Amount
	case "currency":
		if changed.Currency == original.Currency {
			t.Fatal("currency mismatch scenario did not change the currency")
		}
		changed.Currency = original.Currency
	case "transaction":
		if changed.ID == original.ID {
			t.Fatal("transaction mismatch scenario did not change the transaction ID")
		}
		changed.ID = original.ID
	default:
		t.Fatal("unknown scenario comparison field")
	}
	if changed != original {
		t.Fatal("query scenario changed more than the selected copy field")
	}
}

func TestMockQueryScenarios(t *testing.T) {
	t.Run("normal_reads_real_mock_and_applies_found", func(t *testing.T) {
		reference := mockUnitReference(t)
		existing := queryScenarioExisting(reference)
		got, found, err, control, db := queryScenarioRun(t, QueryScenarioNormal, reference, &existing)
		if err != nil || !found {
			t.Fatal("normal scenario did not return the real Mock fact")
		}
		if got != existing {
			t.Fatal("normal scenario changed the real Mock fact")
		}
		if db.queryCalls != 1 {
			t.Fatal("normal scenario did not read the Mock exactly once")
		}
		queryScenarioRequireOneOutcome(t, control, QueryOutcomeFound)
	})

	t.Run("failure_is_fixed_unavailable_without_database", func(t *testing.T) {
		reference := mockUnitReference(t)
		got, found, err, control, db := queryScenarioRun(t, QueryScenarioFailure, reference, nil)
		if found || got != (payment.MockTransaction{}) {
			t.Fatal("failure scenario returned a fabricated fact")
		}
		if !errors.Is(err, payment.ErrUnavailable) {
			t.Fatal("failure scenario did not return the fixed unavailable sentinel")
		}
		if db.queryCalls != 0 {
			t.Fatal("failure scenario touched the database")
		}
		queryScenarioRequireOneOutcome(t, control, QueryOutcomeUnavailable)
	})

	t.Run("notvisible_returns_no_match_without_mutation", func(t *testing.T) {
		reference := mockUnitReference(t)
		existing := queryScenarioExisting(reference)
		got, found, err, control, db := queryScenarioRun(t, QueryScenarioNotVisible, reference, &existing)
		if found || got != (payment.MockTransaction{}) || err != nil {
			t.Fatal("notvisible scenario did not return a clean no-match")
		}
		if db.queryCalls != 0 {
			t.Fatal("notvisible scenario read or wrote the database")
		}
		if db.existing[reference] != existing {
			t.Fatal("notvisible scenario mutated the stored fact")
		}
		queryScenarioRequireOneOutcome(t, control, QueryOutcomeNotFound)
	})

	t.Run("subtimeout_is_real_250ms_child_expiry_in_valid_operation_context", func(t *testing.T) {
		reference := mockUnitReference(t)
		start := time.Now()
		_, found, err, control, db := queryScenarioRun(t, QueryScenarioSubtimeout, reference, nil)
		elapsed := time.Since(start)
		if found || !errors.Is(err, payment.ErrUnavailable) {
			t.Fatal("subtimeout scenario did not return the fixed unavailable sentinel")
		}
		if elapsed < 200*time.Millisecond || elapsed > 2*time.Second {
			t.Fatal("subtimeout scenario did not use a real bounded child deadline")
		}
		if db.queryCalls != 0 {
			t.Fatal("subtimeout scenario touched the database")
		}
		queryScenarioRequireOneOutcome(t, control, QueryOutcomeSubtimeout)
	})

	mismatchCases := []struct {
		name      string
		scenario  QueryScenario
		field     string
		validCopy func(*testing.T, payment.MockTransaction)
	}{
		{
			name: "mismatch_reference", scenario: QueryScenarioMismatchReference, field: "reference",
			validCopy: func(t *testing.T, transaction payment.MockTransaction) {
				if err := payment.ValidateUUIDv4(string(transaction.PlatformReferenceID)); err != nil {
					t.Fatal("reference mismatch copy is not a valid UUIDv4")
				}
			},
		},
		{
			name: "mismatch_amount", scenario: QueryScenarioMismatchAmount, field: "amount",
			validCopy: func(t *testing.T, transaction payment.MockTransaction) {
				if err := payment.ValidateAmount(transaction.Amount); err != nil {
					t.Fatal("amount mismatch copy is not a valid integer amount")
				}
			},
		},
		{
			name: "mismatch_currency", scenario: QueryScenarioMismatchCurrency, field: "currency",
			validCopy: func(t *testing.T, transaction payment.MockTransaction) {
				if transaction.Currency == "" || transaction.Currency == "CNY" {
					t.Fatal("currency mismatch copy is not a non-CNY currency")
				}
			},
		},
		{
			name: "mismatch_transaction", scenario: QueryScenarioMismatchTransaction, field: "transaction",
			validCopy: func(t *testing.T, transaction payment.MockTransaction) {
				if err := payment.ValidateUUIDv4(string(transaction.ID)); err != nil {
					t.Fatal("transaction mismatch copy is not a valid UUIDv4")
				}
			},
		},
	}
	for _, tc := range mismatchCases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			reference := mockUnitReference(t)
			existing := queryScenarioExisting(reference)
			got, found, err, control, db := queryScenarioRun(t, tc.scenario, reference, &existing)
			if err != nil || !found {
				t.Fatal("mismatch scenario did not return the real Mock fact copy")
			}
			queryScenarioSameExcept(t, existing, got, tc.field)
			tc.validCopy(t, got)
			if db.existing[reference] != existing {
				t.Fatal("mismatch scenario rewrote the stored fact")
			}
			if db.queryCalls != 1 {
				t.Fatal("mismatch scenario did not read the real Mock fact once")
			}
			queryScenarioRequireOneOutcome(t, control, QueryOutcomeMismatchApplied)
		})
	}

	t.Run("mismatch_without_real_fact_never_fabricates", func(t *testing.T) {
		reference := mockUnitReference(t)
		_, found, err, control, db := queryScenarioRun(t, QueryScenarioMismatchAmount, reference, nil)
		if found || err != nil {
			t.Fatal("mismatch without a real fact did not return a clean no-match")
		}
		if db.queryCalls != 1 {
			t.Fatal("mismatch without a real fact did not attempt the real read")
		}
		queryScenarioRequireOneOutcome(t, control, QueryOutcomeNotFound)
	})

	t.Run("legacy_control_uses_one_query_entry_and_noop_applied", func(t *testing.T) {
		reference := mockUnitReference(t)
		db := newMockUnitDB()
		control := &mockUnitControl{}
		hooks := NewHooks(control)
		outcome, err := hooks.QueryEntryResult(context.Background(), reference)
		if err != nil {
			t.Fatal("legacy query entry result failed")
		}
		if outcome.Scenario != QueryScenarioNormal || outcome.OnApplied == nil {
			t.Fatal("legacy query entry result did not use the normal no-op path")
		}
		if err := outcome.OnApplied(context.Background(), QueryOutcomeUnavailable); err != nil {
			t.Fatal("legacy no-op applied callback failed")
		}
		if control.queryEntries != 1 {
			t.Fatal("legacy control was not observed exactly once")
		}
		channel := NewChannel(db, hooks)
		if _, found, err := channel.FindTransactionByPlatformReference(context.Background(), reference); err != nil || found {
			t.Fatal("legacy query path did not read the empty database")
		}
		if control.queryEntries != 2 {
			t.Fatal("legacy query path did not call QueryEntry exactly once per lookup")
		}
		if db.queryCalls != 1 {
			t.Fatal("legacy query path did not perform the real read")
		}
	})

	t.Run("zero_hooks_is_normal_noop", func(t *testing.T) {
		outcome, err := NewHooks(nil).QueryEntryResult(context.Background(), payment.PaymentOrderID(mockUnitReference(t)))
		if err != nil || outcome.Scenario != QueryScenarioNormal || outcome.OnApplied == nil {
			t.Fatal("zero hooks query entry result is not the normal no-op path")
		}
		if err := outcome.OnApplied(context.Background(), QueryOutcomeFound); err != nil {
			t.Fatal("zero hooks no-op applied failed")
		}
	})

	t.Run("control_and_applied_errors_fail_closed", func(t *testing.T) {
		reference := mockUnitReference(t)
		db := newMockUnitDB()
		control := &queryScenarioTestControl{scenario: QueryScenarioNormal, entryErr: errors.New("fake entry")}
		channel := NewChannel(db, NewHooks(control))
		if _, _, err := channel.FindTransactionByPlatformReference(context.Background(), reference); !errors.Is(err, ErrControlFault) {
			t.Fatal("query entry control error did not fail closed")
		}
		if db.queryCalls != 0 {
			t.Fatal("query entry control error touched the database")
		}

		existing := queryScenarioExisting(reference)
		db.existing[reference] = existing
		control = &queryScenarioTestControl{scenario: QueryScenarioNormal, appliedErr: errors.New("fake applied")}
		channel = NewChannel(db, NewHooks(control))
		if _, _, err := channel.FindTransactionByPlatformReference(context.Background(), reference); !errors.Is(err, ErrControlFault) {
			t.Fatal("query applied error did not fail closed")
		}
		if control.appliedCalls != 1 {
			t.Fatal("query applied error did not report exactly one attempted proof")
		}
	})
}
