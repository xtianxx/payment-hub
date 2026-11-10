package payment

import "errors"

// These classifications never carry raw storage or channel errors.
var (
	ErrInvalidInput        = errors.New("payment: invalid input")
	ErrConflict            = errors.New("payment: intent conflict")
	ErrUnavailable         = errors.New("payment: dependency unavailable")
	ErrIndeterminate       = errors.New("payment: commit indeterminate")
	ErrObservationConflict = errors.New("payment: observation conflict")
	ErrNotFound            = errors.New("payment: order not found")
	// ErrRegistrationRejected is a definite non-business registration failure.
	// It must not be recovered into intent reuse or grant payment eligibility.
	ErrRegistrationRejected = errors.New("payment: registration rejected")
)
