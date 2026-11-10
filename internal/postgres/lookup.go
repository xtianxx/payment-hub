package postgres

import (
	"context"
	"time"

	"payment-hub/internal/payment"
)

const lookupBudget = 2 * time.Second

var _ payment.OrderLookup = (*Store)(nil)

// FindOrderByID performs one ordinary committed read scoped to the current
// client. It does not begin a transaction, lock a row, write records, or grant
// payment eligibility.
func (s *Store) FindOrderByID(ctx context.Context, clientID string, paymentOrderID payment.PaymentOrderID) (payment.PaymentOrder, bool, error) {
	if s == nil || isNilDB(s.db) || ctx == nil {
		return payment.PaymentOrder{}, false, payment.ErrUnavailable
	}
	if ctx.Err() != nil {
		return payment.PaymentOrder{}, false, payment.ErrUnavailable
	}
	opCtx, cancel := context.WithTimeout(ctx, lookupBudget)
	defer cancel()
	return s.readOrderByIDScoped(opCtx, clientID, paymentOrderID)
}

// FindOrderByBusinessOrderNo performs one ordinary committed read scoped to
// the current client. It does not begin a transaction, lock a row, write
// records, or grant payment eligibility.
func (s *Store) FindOrderByBusinessOrderNo(ctx context.Context, clientID string, businessOrderNo string) (payment.PaymentOrder, bool, error) {
	if s == nil || isNilDB(s.db) || ctx == nil {
		return payment.PaymentOrder{}, false, payment.ErrUnavailable
	}
	if ctx.Err() != nil {
		return payment.PaymentOrder{}, false, payment.ErrUnavailable
	}
	opCtx, cancel := context.WithTimeout(ctx, lookupBudget)
	defer cancel()
	return s.readOrderByBusinessKeyScoped(opCtx, clientID, businessOrderNo)
}
