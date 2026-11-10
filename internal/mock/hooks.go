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

// PaymentEntryOutcome is one per-Pay result directive plus the callback for the
// fact actually committed by this call. A nil Result means no directive and the
// legacy result generator remains authoritative. OnCommitted must be non-nil
// for a successful PaymentEntryResult call; it receives the actual stored
// result, never the directive value.
type PaymentEntryOutcome struct {
	Result      *payment.Result
	OnCommitted func(context.Context, payment.Result) error
}

// PaymentResultControl is the optional trusted-test-mode extension for a
// per-Pay result directive. A Control that implements it is observed only
// through PaymentEntryResult, not through the legacy PaymentEntry/Committed
// pair.
type PaymentResultControl interface {
	PaymentEntryResult(context.Context, payment.PaymentOrderID) (PaymentEntryOutcome, error)
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

// PaymentEntryResult performs the precommit payment-entry observation and
// returns the per-Pay directive/callback pair. A nil/zero Hooks returns a nil
// Result with a safe non-nil no-op callback. An optional PaymentResultControl
// is used exclusively; otherwise the legacy PaymentEntry is called exactly once
// and the returned callback wraps the legacy Committed observation.
func (h *Hooks) PaymentEntryResult(ctx context.Context, order payment.PaymentOrderID) (PaymentEntryOutcome, error) {
	if h == nil || isNilControl(h.control) {
		return PaymentEntryOutcome{OnCommitted: noopCommitted}, nil
	}

	if optional, ok := h.control.(PaymentResultControl); ok {
		outcome, err := optional.PaymentEntryResult(ctx, order)
		if err != nil {
			return PaymentEntryOutcome{}, ErrControlFault
		}
		return normalizePaymentEntryOutcome(outcome)
	}

	if err := h.control.PaymentEntry(ctx, order); err != nil {
		return PaymentEntryOutcome{}, ErrControlFault
	}
	return PaymentEntryOutcome{
		OnCommitted: func(ctx context.Context, _ payment.Result) error {
			return h.Committed(ctx, order)
		},
	}, nil
}

func normalizePaymentEntryOutcome(outcome PaymentEntryOutcome) (PaymentEntryOutcome, error) {
	if outcome.OnCommitted == nil {
		return PaymentEntryOutcome{}, ErrControlFault
	}

	if outcome.Result != nil {
		result := *outcome.Result
		if !validMockResult(result) {
			return PaymentEntryOutcome{}, ErrControlFault
		}
		outcome.Result = &result
	}

	callback := outcome.OnCommitted
	outcome.OnCommitted = func(ctx context.Context, result payment.Result) error {
		return normalizeCommittedCallbackError(callback(ctx, result))
	}
	return outcome, nil
}

func normalizeCommittedCallbackError(err error) error {
	if err == nil {
		return nil
	}
	if errors.Is(err, ErrReplyHidden) {
		return ErrReplyHidden
	}
	return ErrControlFault
}

func noopCommitted(context.Context, payment.Result) error {
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
