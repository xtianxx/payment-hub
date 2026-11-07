package mock

import (
	"context"
	"errors"
	"reflect"

	"payment-hub/internal/payment"
)

var (
	// ErrControlFault is the fixed consumer-side control failure.
	ErrControlFault = errors.New("mock: control fault")
	// ErrReplyHidden is the confirmed postcommit reply-hiding result.
	ErrReplyHidden = errors.New("mock: reply hidden")
)

// Control is the narrow consumer-side seam used by the mock package. It does
// not expose protocol frames, SQL, or transactions.
type Control interface {
	PaymentEntry(context.Context, payment.PaymentOrderID) error
	QueryEntry(context.Context, payment.PaymentOrderID) error
	Committed(context.Context, payment.PaymentOrderID) error
}

// Hooks forwards mock observation points to an optional Control. The zero value
// and nil Control are safe no-ops.
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

// PaymentEntry forwards the precommit payment-entry observation.
func (h *Hooks) PaymentEntry(ctx context.Context, order payment.PaymentOrderID) error {
	if h == nil || h.control == nil {
		return nil
	}
	if err := h.control.PaymentEntry(ctx, order); err != nil {
		return ErrControlFault
	}
	return nil
}

// QueryEntry forwards the precommit query-entry observation.
func (h *Hooks) QueryEntry(ctx context.Context, order payment.PaymentOrderID) error {
	if h == nil || h.control == nil {
		return nil
	}
	if err := h.control.QueryEntry(ctx, order); err != nil {
		return ErrControlFault
	}
	return nil
}

// Committed forwards the postcommit observation and may return ErrReplyHidden.
func (h *Hooks) Committed(ctx context.Context, order payment.PaymentOrderID) error {
	if h == nil || h.control == nil {
		return nil
	}
	if err := h.control.Committed(ctx, order); err != nil {
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
