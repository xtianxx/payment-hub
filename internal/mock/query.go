package mock

import (
	"context"

	"payment-hub/internal/payment"
)

var _ payment.MockTransactionLookup = (*Channel)(nil)

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

	if err := c.hooks.QueryEntry(operationCtx, platformReferenceID); err != nil {
		return payment.MockTransaction{}, false, err
	}
	if operationCtx.Err() != nil {
		return payment.MockTransaction{}, false, payment.ErrUnavailable
	}

	databaseCtx, cancelDatabase := context.WithTimeout(operationCtx, mockDatabaseBudget)
	defer cancelDatabase()

	transaction, found, err := findMockTransaction(databaseCtx, c.db, platformReferenceID)
	if err != nil {
		return payment.MockTransaction{}, false, payment.ErrUnavailable
	}
	if !found {
		return payment.MockTransaction{}, false, nil
	}
	return transaction, true, nil
}
