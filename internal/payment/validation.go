package payment

import (
	"errors"
	"time"
	"unicode/utf8"
)

var (
	errInvalidAmount           = errors.New("invalid amount")
	errInvalidCurrency         = errors.New("invalid currency")
	errInvalidReason           = errors.New("invalid reason")
	errInvalidResult           = errors.New("invalid result")
	errInvalidSource           = errors.New("invalid source")
	errInvalidHandlingDecision = errors.New("invalid handling_decision")
	errInvalidUTC              = errors.New("invalid utc time")
	errInvalidResultTime       = errors.New("invalid result time")
)

const (
	minAmount             int64 = 1
	maxAmount             int64 = 1_000_000
	maxReasonCodeRunes          = 64
	maxReasonMessageRunes       = 512
)

// ValidateAmount checks the platform amount in integer minor units.
func ValidateAmount(amount int64) error {
	if amount < minAmount || amount > maxAmount {
		return errInvalidAmount
	}
	return nil
}

// ValidateCurrency checks the exact platform currency.
func ValidateCurrency(currency string) error {
	if currency != "CNY" {
		return errInvalidCurrency
	}
	return nil
}

// ValidateReason checks the optional, already-sanitized handling reason.
func ValidateReason(reason *Reason) error {
	if reason == nil {
		return nil
	}
	if !utf8.ValidString(reason.Code) {
		return errInvalidReason
	}
	if n := utf8.RuneCountInString(reason.Code); n < 1 || n > maxReasonCodeRunes {
		return errInvalidReason
	}
	if reason.Message == nil {
		return nil
	}
	if !utf8.ValidString(*reason.Message) {
		return errInvalidReason
	}
	if utf8.RuneCountInString(*reason.Message) > maxReasonMessageRunes {
		return errInvalidReason
	}
	return nil
}

// ValidateResult checks the approved platform results.
func ValidateResult(result Result) error {
	switch result {
	case ResultUnknown, ResultSucceeded, ResultFailed:
		return nil
	default:
		return errInvalidResult
	}
}

// ValidateSource checks the approved observation sources.
func ValidateSource(source Source) error {
	switch source {
	case SourcePayment, SourceQuery:
		return nil
	default:
		return errInvalidSource
	}
}

// ValidateHandlingDecision checks the approved handling decisions.
func ValidateHandlingDecision(decision HandlingDecision) error {
	switch decision {
	case DecisionAcceptConfirmation, DecisionKeepUnknown, DecisionKeepConfirmed, DecisionRejectEvidence:
		return nil
	default:
		return errInvalidHandlingDecision
	}
}

// ValidateUTC checks UTC zero-offset semantics. It intentionally does not
// compare Location identity, impose a year range, or order timestamps.
func ValidateUTC(at time.Time) error {
	_, offset := at.Zone()
	if offset != 0 {
		return errInvalidUTC
	}
	return nil
}

// ValidateResultTime checks the approved result/time combination without
// changing the supplied values.
func ValidateResultTime(result Result, confirmedAt *time.Time) error {
	if err := ValidateResult(result); err != nil {
		return errInvalidResultTime
	}
	if result == ResultUnknown {
		if confirmedAt != nil {
			return errInvalidResultTime
		}
		return nil
	}
	if confirmedAt == nil {
		return errInvalidResultTime
	}
	if err := ValidateUTC(*confirmedAt); err != nil {
		return errInvalidResultTime
	}
	return nil
}
