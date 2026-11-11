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

// QueryScenario is the closed set of trusted query-control scenarios.
type QueryScenario string

const (
	QueryScenarioNormal              QueryScenario = "normal"
	QueryScenarioFailure             QueryScenario = "failure"
	QueryScenarioSubtimeout          QueryScenario = "subtimeout"
	QueryScenarioNotVisible          QueryScenario = "notvisible"
	QueryScenarioMismatchReference   QueryScenario = "mismatch_reference"
	QueryScenarioMismatchAmount      QueryScenario = "mismatch_amount"
	QueryScenarioMismatchCurrency    QueryScenario = "mismatch_currency"
	QueryScenarioMismatchTransaction QueryScenario = "mismatch_transaction"
)

// QueryOutcome is the closed set of outcomes actually reached by one query.
type QueryOutcome string

const (
	QueryOutcomeFound           QueryOutcome = "found"
	QueryOutcomeNotFound        QueryOutcome = "not_found"
	QueryOutcomeUnavailable     QueryOutcome = "unavailable"
	QueryOutcomeSubtimeout      QueryOutcome = "subtimeout"
	QueryOutcomeMismatchApplied QueryOutcome = "mismatch_applied"
)

// QueryEntryOutcome is one per-query scenario plus the callback proving the
// outcome actually reached. OnApplied must be non-nil and is called exactly
// once with the branch result, never with an expected scenario value.
type QueryEntryOutcome struct {
	Scenario  QueryScenario
	OnApplied func(context.Context, QueryOutcome) error
}

// QueryResultControl is the optional trusted-test-mode extension for one query
// scenario. A Control that implements it is observed only through
// QueryEntryResult, not through the legacy QueryEntry method.
type QueryResultControl interface {
	QueryEntryResult(context.Context, payment.PaymentOrderID) (QueryEntryOutcome, error)
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

// QueryEntryResult performs the precommit query-entry observation and returns
// the selected scenario plus the applied-proof callback. A nil/zero Hooks
// returns the normal scenario with a non-nil no-op callback. A legacy Control
// is called exactly once through QueryEntry; an optional QueryResultControl is
// used exclusively.
func (h *Hooks) QueryEntryResult(ctx context.Context, order payment.PaymentOrderID) (QueryEntryOutcome, error) {
	if h == nil || isNilControl(h.control) {
		return QueryEntryOutcome{Scenario: QueryScenarioNormal, OnApplied: noopQueryApplied}, nil
	}

	if optional, ok := h.control.(QueryResultControl); ok {
		outcome, err := optional.QueryEntryResult(ctx, order)
		if err != nil {
			return QueryEntryOutcome{}, ErrControlFault
		}
		return normalizeQueryEntryOutcome(outcome)
	}

	if err := h.control.QueryEntry(ctx, order); err != nil {
		return QueryEntryOutcome{}, ErrControlFault
	}
	return QueryEntryOutcome{Scenario: QueryScenarioNormal, OnApplied: noopQueryApplied}, nil
}

func normalizeQueryEntryOutcome(outcome QueryEntryOutcome) (QueryEntryOutcome, error) {
	if !validQueryScenario(outcome.Scenario) || outcome.OnApplied == nil {
		return QueryEntryOutcome{}, ErrControlFault
	}
	callback := outcome.OnApplied
	outcome.OnApplied = func(ctx context.Context, actual QueryOutcome) error {
		if !validQueryOutcome(actual) {
			return ErrControlFault
		}
		if err := callback(ctx, actual); err != nil {
			return ErrControlFault
		}
		return nil
	}
	return outcome, nil
}

func validQueryScenario(value QueryScenario) bool {
	switch value {
	case QueryScenarioNormal, QueryScenarioFailure, QueryScenarioSubtimeout,
		QueryScenarioNotVisible, QueryScenarioMismatchReference,
		QueryScenarioMismatchAmount, QueryScenarioMismatchCurrency,
		QueryScenarioMismatchTransaction:
		return true
	default:
		return false
	}
}

func validQueryOutcome(value QueryOutcome) bool {
	switch value {
	case QueryOutcomeFound, QueryOutcomeNotFound, QueryOutcomeUnavailable,
		QueryOutcomeSubtimeout, QueryOutcomeMismatchApplied:
		return true
	default:
		return false
	}
}

func noopQueryApplied(context.Context, QueryOutcome) error {
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
