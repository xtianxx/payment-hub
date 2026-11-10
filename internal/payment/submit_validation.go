package payment

import (
	"context"
	"errors"
	"time"
)

// conflictLookupTimeout bounds the one ordinary business-key read used when a
// request is structurally safe but its amount or currency is not a valid
// business value. It is deliberately separate from the finish window: this
// rejection path performs one lookup and never retries or opens a new budget.
const conflictLookupTimeout = 2 * time.Second

// validateSubmitRequestInput checks the request identity, request ID, and
// business-order number format before any business-value decision. These
// failures never read storage, register an order, or call the payer.
func validateSubmitRequestInput(requestID RequestID, input RegisterOrderInput) error {
	if ValidateClientID(input.ClientID) != nil {
		return ErrInvalidInput
	}
	if ValidateBusinessOrderNo(input.BusinessOrderNo) != nil {
		return ErrInvalidInput
	}
	if ValidateUUIDv4(string(requestID)) != nil {
		return ErrInvalidInput
	}
	return nil
}

// validateSubmitBusinessInput checks only the business values. Callers must
// run validateSubmitRequestInput first so a safe-input failure keeps priority
// and never causes a lookup.
func validateSubmitBusinessInput(input RegisterOrderInput) error {
	if ValidateAmount(input.Amount) != nil {
		return ErrInvalidInput
	}
	if ValidateCurrency(input.Currency) != nil {
		return ErrInvalidInput
	}
	return nil
}

// resolveInvalidBusinessValue applies the contract's limited conflict-priority
// read. The input already passed request identity and format validation, so
// its integer amount and string currency are safe to compare even when the
// amount is outside the business range or the currency is not CNY.
//
// Exactly one ordinary client-scoped business-key read is attempted with a
// bounded context. It never looks up by platform ID, calls the query business
// flow, registers an order, calls Mock, saves a record, or grants payment
// eligibility. A reliable existing fact with different amount or currency is a
// conflict; a reliable miss is invalid input; a dependency or timeout failure
// is unavailable; any other failure is indeterminate. An order returned
// alongside an error is never treated as a fact.
func (s *Submitter) resolveInvalidBusinessValue(ctx context.Context, input RegisterOrderInput) (SubmitResult, error) {
	if s.lifecycle.Err() != nil {
		return SubmitResult{}, ErrUnavailable
	}
	if ctx.Err() != nil {
		return SubmitResult{}, ctx.Err()
	}

	lookupCtx, cancelLookup := context.WithTimeout(ctx, conflictLookupTimeout)
	stopLifecycle := context.AfterFunc(s.lifecycle, cancelLookup)
	defer func() {
		stopLifecycle()
		cancelLookup()
	}()

	order, found, lookupErr := s.lookup.FindOrderByBusinessOrderNo(lookupCtx, input.ClientID, input.BusinessOrderNo)
	if s.lifecycle.Err() != nil {
		return SubmitResult{}, ErrUnavailable
	}
	if lookupCtx.Err() != nil {
		if ctx.Err() != nil {
			return SubmitResult{}, ctx.Err()
		}
		return SubmitResult{}, ErrUnavailable
	}
	if lookupErr != nil {
		return SubmitResult{}, classifyConflictLookupError(ctx, lookupErr)
	}
	if !found {
		return SubmitResult{}, ErrInvalidInput
	}
	if err := validateConflictLookupOrder(order, input); err != nil {
		return SubmitResult{}, err
	}
	if order.Amount != input.Amount || order.Currency != input.Currency {
		return SubmitResult{Order: clonePaymentOrder(order)}, ErrConflict
	}
	// A valid existing order cannot legitimately have the same invalid
	// business values. Without a different reliable fact there is no conflict,
	// so the request remains invalid input.
	return SubmitResult{}, ErrInvalidInput
}

func validateConflictLookupOrder(order PaymentOrder, input RegisterOrderInput) error {
	if err := validateRecoveredOrderIdentity(order, input); err != nil {
		return err
	}
	// A row whose stored business values violate the platform invariants is
	// not a reliable fact to compare against.
	if ValidateAmount(order.Amount) != nil || ValidateCurrency(order.Currency) != nil {
		return ErrUnavailable
	}
	return nil
}

func classifyConflictLookupError(ctx context.Context, lookupErr error) error {
	if ctx != nil && ctx.Err() != nil {
		return ctx.Err()
	}
	if errors.Is(lookupErr, ErrUnavailable) ||
		errors.Is(lookupErr, context.Canceled) ||
		errors.Is(lookupErr, context.DeadlineExceeded) {
		return ErrUnavailable
	}
	return ErrIndeterminate
}
