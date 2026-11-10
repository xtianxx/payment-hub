package payment

// QueryInput selects one order in the authenticated client's scope.
// Exactly one of PaymentOrderID and BusinessOrderNo must be provided.
type QueryInput struct {
	ClientID        string
	PaymentOrderID  PaymentOrderID
	BusinessOrderNo string
}

// QueryResult contains reliable order state and non-fatal handling issues.
type QueryResult struct {
	Order  PaymentOrder
	Issues []Issue
}
