package postgres

import (
	"context"
	"errors"
	"reflect"

	"payment-hub/internal/payment"
)

var (
	// ErrControlFault is the fixed consumer-side control failure.
	ErrControlFault = errors.New("postgres: control fault")
	// ErrReplyHidden is the confirmed postcommit reply-hiding result.
	ErrReplyHidden = errors.New("postgres: reply hidden")
)

// Control is the narrow consumer-side seam used by the postgres package. It
// does not expose protocol frames, SQL, or transactions.
type Control interface {
	Registered(context.Context, payment.PaymentOrderID) error
	SavingResult(context.Context, payment.PaymentOrderID) error
	ResultCommitted(context.Context, payment.PaymentOrderID) error
}

// Hooks forwards postgres observation points to an optional Control. The zero
// value and nil Control are safe no-ops.
type Hooks struct {
	control Control
}

// NewHooks returns a non-nil Hooks. Nil and typed-nil controls are treated as
// disabled.
func NewHooks(control Control) *Hooks {
	if isNilControl(control) {
		return &Hooks{}
	}
	return &Hooks{control: control}
}

// Registered forwards the postcommit registration observation and may return
// ErrReplyHidden.
func (h *Hooks) Registered(ctx context.Context, order payment.PaymentOrderID) error {
	if h == nil || h.control == nil {
		return nil
	}
	if err := h.control.Registered(ctx, order); err != nil {
		if err == ErrReplyHidden {
			return ErrReplyHidden
		}
		return ErrControlFault
	}
	return nil
}

// SavingResult forwards the precommit result-saving observation. Reply hiding
// is not allowed at this point.
func (h *Hooks) SavingResult(ctx context.Context, order payment.PaymentOrderID) error {
	if h == nil || h.control == nil {
		return nil
	}
	if err := h.control.SavingResult(ctx, order); err != nil {
		return ErrControlFault
	}
	return nil
}

// ResultCommitted forwards the postcommit result observation and may return
// ErrReplyHidden.
func (h *Hooks) ResultCommitted(ctx context.Context, order payment.PaymentOrderID) error {
	if h == nil || h.control == nil {
		return nil
	}
	if err := h.control.ResultCommitted(ctx, order); err != nil {
		if err == ErrReplyHidden {
			return ErrReplyHidden
		}
		return ErrControlFault
	}
	return nil
}

func isNilControl(control Control) bool {
	if control == nil {
		return true
	}
	value := reflect.ValueOf(control)
	switch value.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Ptr, reflect.Slice:
		return value.IsNil()
	default:
		return false
	}
}
