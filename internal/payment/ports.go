package payment

import "context"

// RegisterOrderInput carries the intent parameters for one registration
// attempt. The data access implementation generates the candidate payment
// order ID (UUIDv4) inside RegisterOrder; callers must not supply or reuse an
// ID here.
type RegisterOrderInput struct {
	ClientID        string
	BusinessOrderNo string
	Amount          int64 // integer minor units (cents); never a float
	Currency        string
}

// RegisterOutcome tells the caller exactly what the registration call did.
type RegisterOutcome string

const (
	// RegisterOutcomeCreated means this call inserted the order and the
	// transaction commit was confirmed. It is the only outcome that allows a
	// future business caller to judge first-initiation eligibility.
	RegisterOutcomeCreated RegisterOutcome = "CREATED"
	// RegisterOutcomeReused means an existing order for the same
	// client_id + business_order_no was read. Reuse never grants a new payment
	// attempt, even when the existing result is UNKNOWN or its
	// mock_transaction_id is nil.
	RegisterOutcomeReused RegisterOutcome = "REUSED"
)

// RegisterOrderResult is returned only when the registration outcome is
// reliable. An unreadable existing row or an unconfirmed commit is an error,
// not RegisterOutcomeReused and not a false "not found". A business-key
// conflict that is read back reliably returns RegisterOutcomeReused; the
// caller compares amount and currency.
type RegisterOrderResult struct {
	Order   PaymentOrder
	Outcome RegisterOutcome
}

// OrderRegistrar registers a payment intent. Implementations must use the
// business unique key (client_id, business_order_no), distinguish a real
// insert from a conflict, and return RegisterOutcomeCreated only after the
// commit is confirmed. A business-key conflict is read back and returned as
// RegisterOutcomeReused; other unique errors must not be treated as reuse. A
// commit whose outcome is unknown is an error; it must not be reported as
// "not found" or as a reused order. It must not call Mock or initiate a
// payment.
type OrderRegistrar interface {
	RegisterOrder(ctx context.Context, input RegisterOrderInput) (RegisterOrderResult, error)
}

// OrderLookup is an ordinary, scoped single-order read. It never calls Mock,
// does not verify an UNKNOWN order, does not create handling records, and does
// not grant payment eligibility. The found result is meaningful only when
// error is nil; false with a nil error means the order does not exist in the
// current client scope. An error must never be treated as "not found".
type OrderLookup interface {
	FindOrderByID(ctx context.Context, clientID string, paymentOrderID PaymentOrderID) (PaymentOrder, bool, error)
	FindOrderByBusinessOrderNo(ctx context.Context, clientID string, businessOrderNo string) (PaymentOrder, bool, error)
}

// ObservationRecorder atomically saves an accepted confirmation (if any) and
// its handling record in one platform database transaction. Implementations
// must lock the order within the supplied client scope, derive result_before,
// result_after, and decision from the current locked order, and return them in
// the saved record. Callers cannot supply those values or confirmed_at. An
// already confirmed result must not be overwritten; the lock-derived decision
// is KEEP_CONFIRMED or REJECT_EVIDENCE instead.
//
// Retrying the same observation must reuse Observation.ID; a genuinely new
// observation must use a new ID. The recorder must not call Mock or initiate a
// payment. If the commit outcome is unknown, the complete save implementation
// must either perform a bounded read-back and verification keyed by the same
// Observation.ID, or return an explicit indeterminate error. In the latter
// case the caller may retry this same SaveObservation (reusing the ID) or use
// the provided ordinary OrderLookup within the established finish window; it
// must not be required to call a record-ID CRUD API that this port does not
// provide. When error is non-nil, any returned Order or Record must not be
// treated as a newly committed success. Retrying the same observation must
// never initiate another payment.
type ObservationRecorder interface {
	SaveObservation(ctx context.Context, observation Observation) (SaveObservationResult, error)
}

// SaveObservationResult contains only facts from the committed save. Order is
// the current order state after the save; Record is the appended handling
// record, including the lock-derived before/after result and decision. When
// SaveObservation returns an error, any returned Order or Record must not be
// treated as a newly committed success.
type SaveObservationResult struct {
	Order  PaymentOrder
	Record ResultHandlingRecord
}

// MockPaymentRequest is the actual payment call sent to the Mock channel.
// PlatformReferenceID is the platform order ID; the Mock creates its own
// transaction ID and processed_at. Amount and Currency are the values actually
// sent, not values read back from the platform order during a query.
type MockPaymentRequest struct {
	PlatformReferenceID PaymentOrderID
	Amount              int64 // integer minor units (cents); never a float
	Currency            string
}

// MockPayer performs one Mock payment call. Callers must invoke it only after
// a registration has returned RegisterOutcomeCreated with a confirmed commit.
// It uses the request values and must not read the platform order or its
// result. A returned MockTransaction has ResultSucceeded or ResultFailed; an
// error means no usable answer was obtained and must not be interpreted as a
// failed payment. Mock never stores UNKNOWN.
type MockPayer interface {
	Pay(ctx context.Context, request MockPaymentRequest) (MockTransaction, error)
}

// MockTransactionLookup reads the Mock's own original transaction. It never
// triggers a payment, never reads the platform order or its result, and never
// creates a transaction or handling record. false with a nil error means no
// matching Mock transaction exists; that does not prove the payment did not
// happen and never grants a new payment attempt. An error must not be treated
// as "not found".
type MockTransactionLookup interface {
	FindTransactionByPlatformReference(ctx context.Context, platformReferenceID PaymentOrderID) (MockTransaction, bool, error)
}
