// Package payment defines the business contracts shared by the HTTP,
// persistence, and mock-payment layers. It intentionally depends only on the
// standard library: it must not import Gin, pgx, concrete PostgreSQL or mock
// implementations, or test-harness code. These declarations contain no
// persistence or state transitions; value validation lives in separate files.
package payment

import "time"

// Result is the platform-visible payment result. Storage uses the same three
// values as TEXT with a CHECK constraint. The zero value is not a valid
// result.
type Result string

const (
	// ResultUnknown means the platform cannot yet confirm the payment. It is
	// not a failure and does not imply that a background retry exists.
	ResultUnknown Result = "UNKNOWN"
	// ResultSucceeded means the platform has confirmed a successful payment.
	ResultSucceeded Result = "SUCCEEDED"
	// ResultFailed means the platform has confirmed a failed payment. It is a
	// normal payment fact, not an infrastructure error.
	ResultFailed Result = "FAILED"
)

// Source identifies what produced a result-handling observation. The zero
// value is not a valid source.
type Source string

const (
	// SourcePayment is the answer or failure of the first payment call.
	SourcePayment Source = "PAYMENT"
	// SourceQuery is a later verification of the original Mock transaction.
	SourceQuery Source = "QUERY"
)

// HandlingDecision records why a result-handling record was appended. The
// zero value is not a valid decision.
type HandlingDecision string

const (
	// DecisionAcceptConfirmation accepts matching evidence for a previously
	// unknown order.
	DecisionAcceptConfirmation HandlingDecision = "ACCEPT_CONFIRMATION"
	// DecisionKeepUnknown keeps the order unknown because no usable matching
	// confirmation was obtained.
	DecisionKeepUnknown HandlingDecision = "KEEP_UNKNOWN"
	// DecisionKeepConfirmed preserves an already confirmed result.
	DecisionKeepConfirmed HandlingDecision = "KEEP_CONFIRMED"
	// DecisionRejectEvidence rejects mismatched, conflicting, or invalid
	// evidence without changing the order result.
	DecisionRejectEvidence HandlingDecision = "REJECT_EVIDENCE"
)

// PaymentOrderID is the platform payment order identifier. It is a UUIDv4
// string in storage and is never a Mock transaction number or request ID.
type PaymentOrderID string

// MockTransactionID is the Mock channel's own transaction identifier.
type MockTransactionID string

// ResultHandlingRecordID is the application-generated ID of one result
// handling record. Retries of the same observation reuse it.
type ResultHandlingRecordID string

// RequestID is the entry request identifier. It is not an idempotency key and
// does not grant payment eligibility.
type RequestID string

// PaymentOrder is the platform's payment intent. It has exactly the nine
// fields of payment_orders.
type PaymentOrder struct {
	ID PaymentOrderID
	// ClientID is the verified business identity, not an API key, and does not
	// change when the API key changes.
	ClientID string
	// BusinessOrderNo is the client's business order number. Together with
	// ClientID it identifies the payment intent.
	BusinessOrderNo string
	Amount          int64 // integer minor units (cents); never a float
	// Currency is the original order currency. The first phase accepts only
	// CNY.
	Currency string
	// Result is the platform's current reliable result.
	Result Result
	// MockTransactionID is nil until a verified Mock transaction ID has been
	// stored. Nil does not prove that no payment happened and never grants a
	// new payment attempt.
	MockTransactionID *MockTransactionID
	// RegisteredAt is when the platform created the order record. It does not
	// by itself prove that the registration transaction committed.
	RegisteredAt time.Time
	// ConfirmedAt is nil exactly while Result is ResultUnknown. It is set for
	// both confirmed success and confirmed failure and is a local platform
	// confirmation time, not the channel processing time.
	ConfirmedAt *time.Time
}

// MockTransaction is the Mock channel's independently stored fact. It has
// exactly the six fields of mock_transactions.
type MockTransaction struct {
	ID MockTransactionID
	// PlatformReferenceID directly uses the platform payment order ID; Mock
	// does not create a separate payment-initiation number.
	PlatformReferenceID PaymentOrderID
	Amount              int64 // actual amount processed by Mock; never a float
	Currency            string
	// Result is only ResultSucceeded or ResultFailed. Mock transactions never
	// have an UNKNOWN state.
	Result Result
	// ProcessedAt is when Mock formed the transaction result. It is not the
	// platform's local confirmation time.
	ProcessedAt time.Time
}

// EvidenceFieldName identifies a field whose value differed during
// verification. It is deliberately a closed set: differences are field names,
// not arbitrary nested payloads.
type EvidenceFieldName string

const (
	EvidenceFieldPlatformReferenceID EvidenceFieldName = "platform_reference_id"
	EvidenceFieldMockTransactionID   EvidenceFieldName = "mock_transaction_id"
	EvidenceFieldAmount              EvidenceFieldName = "amount"
	EvidenceFieldCurrency            EvidenceFieldName = "currency"
	EvidenceFieldResult              EvidenceFieldName = "result"
)

// Evidence is the filtered, typed evidence allowed in a result-handling
// record. A nil pointer means the value was not obtained; it must not be
// filled in later from the platform order or from a fabricated response.
type Evidence struct {
	PlatformReferenceID *PaymentOrderID    `json:"platform_reference_id,omitempty"`
	MockTransactionID   *MockTransactionID `json:"mock_transaction_id,omitempty"`
	Amount              *int64             `json:"amount,omitempty"`
	Currency            *string            `json:"currency,omitempty"`
	// Result is the result actually obtained. Mock evidence uses
	// ResultSucceeded or ResultFailed, never ResultUnknown.
	Result *Result `json:"result,omitempty"`
	// Differences lists only mismatched field names. It is not a raw payload.
	Differences []EvidenceFieldName `json:"differences,omitempty"`
}

// Reason is an optional, sanitized explanation for a handling decision. A nil
// Reason means no exceptional condition. When Reason is present, Code must be
// a stable classification and Message, when present, is a sanitized summary;
// raw errors and payloads are not allowed.
type Reason struct {
	Code    string
	Message *string
}

// CandidateConfirmation is a verified proposal to confirm an order, not a
// persisted fact. It is non-nil only after the business layer has verified
// outside the save lock that the evidence matches the original order, and it
// must be accompanied by that matching evidence. Result must be
// ResultSucceeded or ResultFailed, never ResultUnknown.
//
// MockTransactionID is optional because the minimum confirmation evidence does
// not require inventing a transaction ID that was not obtained. When the value
// is present, the business association must still be verified; being optional
// never excuses skipping verification of a value that was obtained.
type CandidateConfirmation struct {
	Result            Result
	MockTransactionID *MockTransactionID
}

// Observation is the fixed input to one result-handling save. Its ID
// identifies this observation: retries of the same save reuse it, while a
// genuinely new verification uses a new ID. Observation deliberately has no
// result_before, result_after, or confirmed_at; the recorder derives those
// under the order lock from the current order and returns them in the saved
// record.
type Observation struct {
	ID             ResultHandlingRecordID
	PaymentOrderID PaymentOrderID
	ClientID       string
	RequestID      RequestID
	Source         Source
	ObservedAt     time.Time
	Evidence       Evidence
	// EvidenceRejected is the fixed observation's screened evaluation, not the
	// final HandlingDecision. True means the business layer identified
	// mismatched or invalid evidence; it requires a non-nil controlled Reason
	// and is mutually exclusive with Candidate. The recorder still derives the
	// final decision under the order lock, and this flag is not persisted.
	EvidenceRejected bool
	// Reason is set for failures, rejections, and any exceptional handling
	// outcome.
	Reason *Reason
	// Candidate is non-nil only for a fixed candidate that the business layer
	// verified outside the save lock. It does not grant payment eligibility and
	// does not determine the final handling: a concurrent save may confirm
	// first, so the lock-derived decision may keep the confirmation or reject a
	// conflict.
	Candidate *CandidateConfirmation
}

// ResultHandlingRecord is one appended, saved handling record. It has exactly
// the eleven fields of result_handling_records. ResultBefore, ResultAfter, and
// Decision describe the state under the order lock at save time; they are not
// caller input.
type ResultHandlingRecord struct {
	ID             ResultHandlingRecordID
	PaymentOrderID PaymentOrderID
	RequestID      RequestID
	Source         Source
	ObservedAt     time.Time
	Evidence       Evidence
	// ResultBefore and ResultAfter are the locked order states at save time.
	ResultBefore Result
	ResultAfter  Result
	// Decision is derived from the locked order state and the observation.
	Decision HandlingDecision
	// ReasonCode is a stable classification, present for exceptional outcomes.
	ReasonCode *string
	// ReasonMessage is a sanitized summary of at most 512 characters.
	ReasonMessage *string
}
