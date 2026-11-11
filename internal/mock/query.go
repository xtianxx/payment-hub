package mock

import (
	"context"
	"math"
	"time"

	"payment-hub/internal/payment"
)

var _ payment.MockTransactionLookup = (*Channel)(nil)

// querySubtimeoutBudget is the fixed real child deadline for the subtimeout
// scenario. It is not the default operation or database budget.
const querySubtimeoutBudget = 250 * time.Millisecond

// FindTransactionByPlatformReference reads the Mock's own original transaction.
// It never calls Pay, never creates or mutates a transaction, and never reads
// platform order facts. A missing reference is a reliable false with a nil
// error; storage and context failures return a safe sentinel instead.
func (c *Channel) FindTransactionByPlatformReference(ctx context.Context, platformReferenceID payment.PaymentOrderID) (payment.MockTransaction, bool, error) {
	if c == nil || c.db == nil || c.hooks == nil {
		return payment.MockTransaction{}, false, payment.ErrUnavailable
	}
	if ctx == nil || ctx.Err() != nil {
		return payment.MockTransaction{}, false, payment.ErrUnavailable
	}
	if err := payment.ValidateUUIDv4(string(platformReferenceID)); err != nil {
		return payment.MockTransaction{}, false, payment.ErrInvalidInput
	}

	operationCtx, cancelOperation := context.WithTimeout(ctx, mockOperationBudget)
	defer cancelOperation()

	outcome, err := c.hooks.QueryEntryResult(operationCtx, platformReferenceID)
	if err != nil {
		return payment.MockTransaction{}, false, err
	}
	if outcome.OnApplied == nil {
		return payment.MockTransaction{}, false, ErrControlFault
	}
	if operationCtx.Err() != nil {
		return payment.MockTransaction{}, false, payment.ErrUnavailable
	}

	transaction, found, queryErr, actual := c.runQueryScenario(operationCtx, platformReferenceID, outcome.Scenario)
	if operationCtx.Err() != nil {
		return payment.MockTransaction{}, false, payment.ErrUnavailable
	}
	if err := outcome.OnApplied(operationCtx, actual); err != nil {
		return payment.MockTransaction{}, false, ErrControlFault
	}
	return transaction, found, queryErr
}

func (c *Channel) runQueryScenario(ctx context.Context, platformReferenceID payment.PaymentOrderID, scenario QueryScenario) (payment.MockTransaction, bool, error, QueryOutcome) {
	switch scenario {
	case QueryScenarioNormal:
		return c.readQueryTransaction(ctx, platformReferenceID)
	case QueryScenarioFailure:
		return payment.MockTransaction{}, false, payment.ErrUnavailable, QueryOutcomeUnavailable
	case QueryScenarioNotVisible:
		return payment.MockTransaction{}, false, nil, QueryOutcomeNotFound
	case QueryScenarioSubtimeout:
		subCtx, cancel := context.WithTimeout(ctx, querySubtimeoutBudget)
		defer cancel()
		<-subCtx.Done()
		return payment.MockTransaction{}, false, payment.ErrUnavailable, QueryOutcomeSubtimeout
	case QueryScenarioMismatchReference, QueryScenarioMismatchAmount,
		QueryScenarioMismatchCurrency, QueryScenarioMismatchTransaction:
		transaction, found, err, actual := c.readQueryTransaction(ctx, platformReferenceID)
		if err != nil || !found {
			return transaction, found, err, actual
		}
		return applyQueryMismatch(transaction, scenario), true, nil, QueryOutcomeMismatchApplied
	default:
		return payment.MockTransaction{}, false, ErrControlFault, QueryOutcomeUnavailable
	}
}

func (c *Channel) readQueryTransaction(ctx context.Context, platformReferenceID payment.PaymentOrderID) (payment.MockTransaction, bool, error, QueryOutcome) {
	databaseCtx, cancelDatabase := context.WithTimeout(ctx, mockDatabaseBudget)
	defer cancelDatabase()
	transaction, found, err := findMockTransaction(databaseCtx, c.db, platformReferenceID)
	if err != nil {
		return payment.MockTransaction{}, false, payment.ErrUnavailable, QueryOutcomeUnavailable
	}
	if !found {
		return payment.MockTransaction{}, false, nil, QueryOutcomeNotFound
	}
	return transaction, true, nil, QueryOutcomeFound
}

// applyQueryMismatch changes only the returned copy of a real Mock fact. The
// stored row, including processed_at, is never rewritten.
func applyQueryMismatch(transaction payment.MockTransaction, scenario QueryScenario) payment.MockTransaction {
	switch scenario {
	case QueryScenarioMismatchReference:
		transaction.PlatformReferenceID = payment.PaymentOrderID(differentMockUUID(string(transaction.PlatformReferenceID)))
	case QueryScenarioMismatchAmount:
		if transaction.Amount == math.MaxInt64 {
			transaction.Amount--
		} else {
			transaction.Amount++
		}
	case QueryScenarioMismatchCurrency:
		transaction.Currency = "USD"
	case QueryScenarioMismatchTransaction:
		transaction.ID = payment.MockTransactionID(differentMockUUID(string(transaction.ID)))
	}
	return transaction
}

func differentMockUUID(existing string) string {
	for {
		value := payment.NewUUIDv4()
		if value != existing {
			return value
		}
	}
}
