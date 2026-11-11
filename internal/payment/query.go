package payment

import (
	"context"
	"errors"
	"time"
)

const (
	queryTraceSaveFailedMessage        = "本次追踪保存失败"
	queryTraceSaveIndeterminateMessage = "保存状态未确认"
)

// QueryService orchestrates one ordinary single-order query. It is built from
// the existing narrow lookup, recorder, and Mock transaction ports.
type QueryService struct {
	lookup    OrderLookup
	recorder  ObservationRecorder
	mock      MockTransactionLookup
	lifecycle context.Context
	invalid   bool
}

var _ interface {
	Query(context.Context, RequestID, QueryInput) (QueryResult, error)
} = (*QueryService)(nil)

// NewQueryService builds a QueryService bound to the application lifecycle. A
// nil dependency or nil lifecycle produces a service that fails closed.
func NewQueryService(lookup OrderLookup, recorder ObservationRecorder, mock MockTransactionLookup, lifecycle context.Context) *QueryService {
	if lookup == nil || recorder == nil || mock == nil || lifecycle == nil {
		return &QueryService{invalid: true}
	}
	return &QueryService{
		lookup:    lookup,
		recorder:  recorder,
		mock:      mock,
		lifecycle: lifecycle,
	}
}

// Query performs one ordinary single-order query. A confirmed order is
// returned directly; an unknown order is verified against the Mock's original
// transaction and the fixed observation is saved under the order lock by the
// recorder. Query never creates an order or initiates a payment.
func (s *QueryService) Query(ctx context.Context, requestID RequestID, input QueryInput) (QueryResult, error) {
	if s == nil || s.invalid || s.lifecycle == nil {
		return QueryResult{}, ErrUnavailable
	}
	if ctx == nil {
		return QueryResult{}, ErrInvalidInput
	}
	if s.lifecycle.Err() != nil {
		return QueryResult{}, ErrUnavailable
	}
	if ctx.Err() != nil {
		return QueryResult{}, ctx.Err()
	}
	if err := validateQueryInput(requestID, input); err != nil {
		return QueryResult{}, err
	}

	workCtx, cancelWork := s.newWorkContext(ctx)
	defer cancelWork()

	order, found, err := s.lookupOrder(workCtx, input)
	if err != nil {
		return QueryResult{}, ErrUnavailable
	}
	if !found {
		return QueryResult{}, ErrNotFound
	}
	if err := validateQueryOrderIdentity(order, input); err != nil {
		return QueryResult{}, err
	}
	if order.Result != ResultUnknown {
		return QueryResult{Order: clonePaymentOrder(order)}, nil
	}
	// Do not start a new Mock query after the client or lifecycle has already
	// stopped the work context.
	if s.lifecycle.Err() != nil {
		return QueryResult{}, ErrUnavailable
	}
	if workCtx.Err() != nil {
		return QueryResult{}, workCtx.Err()
	}

	transaction, mockFound, mockErr := s.mock.FindTransactionByPlatformReference(workCtx, order.ID)

	// The fixed answer or fault is already obtained. Save it in one bounded
	// lifecycle window that is independent of client cancellation.
	finish := newFinishScope(s.lifecycle)
	defer finish.cancel()

	var observation Observation
	var issues []Issue
	newConfirmation := false

	switch {
	case mockErr != nil:
		observation = queryObservation(order, requestID, Evidence{}, false,
			fixedReason(IssueCodeMockResultUnconfirmed, issueMessageForCode(IssueCodeMockResultUnconfirmed)), nil)
		issues = []Issue{newIssue(IssueCodeMockResultUnconfirmed)}
	case !mockFound:
		observation = queryObservation(order, requestID, Evidence{}, false,
			fixedReason(IssueCodeMockResultNotFound, issueMessageForCode(IssueCodeMockResultNotFound)), nil)
		issues = []Issue{newIssue(IssueCodeMockResultNotFound)}
	default:
		evidence, candidate, reason := EvaluateMockEvidence(order, transaction)
		observation = queryObservation(order, requestID, evidence, reason != nil, reason, candidate)
		if reason != nil {
			issues = []Issue{newIssue(IssueCodeMockEvidenceRejected)}
		} else {
			newConfirmation = candidate != nil
		}
	}

	return s.saveQueryObservation(finish.ctx, order, observation, issues, newConfirmation)
}

func (s *QueryService) newWorkContext(ctx context.Context) (context.Context, context.CancelFunc) {
	workCtx, cancelWork := context.WithTimeout(ctx, mainProcessingTimeout)
	stopLifecycle := context.AfterFunc(s.lifecycle, cancelWork)
	return workCtx, func() {
		stopLifecycle()
		cancelWork()
	}
}

func (s *QueryService) lookupOrder(ctx context.Context, input QueryInput) (PaymentOrder, bool, error) {
	if input.PaymentOrderID != "" {
		return s.lookup.FindOrderByID(ctx, input.ClientID, input.PaymentOrderID)
	}
	return s.lookup.FindOrderByBusinessOrderNo(ctx, input.ClientID, input.BusinessOrderNo)
}

func (s *QueryService) saveQueryObservation(
	finishCtx context.Context,
	order PaymentOrder,
	observation Observation,
	issues []Issue,
	newConfirmation bool,
) (QueryResult, error) {
	saved, saveErr := s.recorder.SaveObservation(finishCtx, observation)
	if saveErr == nil {
		if err := validateSavedResult(saved, order, observation); err != nil {
			return QueryResult{}, err
		}
		issues = issuesForSavedRecord(issues, saved.Record, newConfirmation)
		return QueryResult{Order: clonePaymentOrder(saved.Order), Issues: cloneIssues(issues)}, nil
	}

	if message, ok := queryTraceSaveFailureMessage(saveErr); ok {
		if err := validateTraceFailureResult(order, saved); err != nil {
			return QueryResult{}, err
		}
		// The recorder's marker carries an original committed confirmation
		// read under its lock before this attempt wrote anything. Return that
		// fact directly; another lookup is not a precondition.
		issues = appendIssue(issues, Issue{Code: IssueCodeTraceSaveError, Message: message})
		return QueryResult{Order: clonePaymentOrder(saved.Order), Issues: cloneIssues(issues)}, nil
	}

	if s.lifecycle.Err() != nil {
		return QueryResult{}, ErrUnavailable
	}

	// An ordinary save error never authorizes using the returned Order or
	// Record as a committed fact. Read the current scoped order with the same
	// finish deadline; do not open a new window.
	current, found, lookupErr := s.lookup.FindOrderByID(finishCtx, order.ClientID, order.ID)
	if s.lifecycle.Err() != nil {
		return QueryResult{}, ErrUnavailable
	}
	if lookupErr != nil {
		if errors.Is(saveErr, ErrUnavailable) || errors.Is(lookupErr, ErrUnavailable) {
			return QueryResult{}, ErrUnavailable
		}
		return QueryResult{}, ErrIndeterminate
	}
	if !found {
		if errors.Is(saveErr, ErrUnavailable) {
			return QueryResult{}, ErrUnavailable
		}
		return QueryResult{}, ErrIndeterminate
	}
	if err := validateQueryReadbackOrder(current, order); err != nil {
		return QueryResult{}, err
	}

	saveIssue := IssueCodeTraceSaveError
	if newConfirmation {
		saveIssue = IssueCodeResultSaveError
	}
	issues = appendIssue(issues, newIssue(saveIssue))
	return QueryResult{Order: clonePaymentOrder(current), Issues: cloneIssues(issues)}, nil
}

func queryObservation(
	order PaymentOrder,
	requestID RequestID,
	evidence Evidence,
	evidenceRejected bool,
	reason *Reason,
	candidate *CandidateConfirmation,
) Observation {
	return Observation{
		ID:               ResultHandlingRecordID(NewUUIDv4()),
		PaymentOrderID:   order.ID,
		ClientID:         order.ClientID,
		RequestID:        requestID,
		Source:           SourceQuery,
		ObservedAt:       time.Now().UTC(),
		Evidence:         evidence,
		EvidenceRejected: evidenceRejected,
		Reason:           reason,
		Candidate:        candidate,
	}
}

func validateQueryInput(requestID RequestID, input QueryInput) error {
	if ValidateClientID(input.ClientID) != nil {
		return ErrInvalidInput
	}
	if ValidateUUIDv4(string(requestID)) != nil {
		return ErrInvalidInput
	}
	hasID := input.PaymentOrderID != ""
	hasBusiness := input.BusinessOrderNo != ""
	if hasID == hasBusiness {
		return ErrInvalidInput
	}
	if hasID {
		if ValidateUUIDv4(string(input.PaymentOrderID)) != nil {
			return ErrInvalidInput
		}
	} else if ValidateBusinessOrderNo(input.BusinessOrderNo) != nil {
		return ErrInvalidInput
	}
	return nil
}

func validateQueryOrderIdentity(order PaymentOrder, input QueryInput) error {
	if ValidateUUIDv4(string(order.ID)) != nil {
		return ErrUnavailable
	}
	if ValidateClientID(order.ClientID) != nil || order.ClientID != input.ClientID {
		return ErrUnavailable
	}
	if ValidateBusinessOrderNo(order.BusinessOrderNo) != nil {
		return ErrUnavailable
	}
	if input.PaymentOrderID != "" && order.ID != input.PaymentOrderID {
		return ErrUnavailable
	}
	if input.BusinessOrderNo != "" && order.BusinessOrderNo != input.BusinessOrderNo {
		return ErrUnavailable
	}
	if ValidateAmount(order.Amount) != nil {
		return ErrUnavailable
	}
	if ValidateCurrency(order.Currency) != nil {
		return ErrUnavailable
	}
	if ValidateResultTime(order.Result, order.ConfirmedAt) != nil {
		return ErrUnavailable
	}
	if order.MockTransactionID != nil && ValidateUUIDv4(string(*order.MockTransactionID)) != nil {
		return ErrUnavailable
	}
	return nil
}

func queryTraceSaveFailureMessage(err error) (string, bool) {
	switch {
	case errors.Is(err, ErrTraceSaveFailed):
		return queryTraceSaveFailedMessage, true
	case errors.Is(err, ErrTraceSaveIndeterminate):
		return queryTraceSaveIndeterminateMessage, true
	default:
		return "", false
	}
}

// validateTraceFailureResult independently re-validates the only fact a trace
// save marker may carry: a copy of the original committed confirmation read
// under the recorder's order lock before this attempt wrote anything. It never
// treats the returned Record as saved.
func validateTraceFailureResult(original PaymentOrder, saved SaveObservationResult) error {
	if !queryTraceFailureRecordEmpty(saved.Record) {
		return ErrUnavailable
	}
	current := saved.Order
	if ValidateUUIDv4(string(current.ID)) != nil || current.ID != original.ID {
		return ErrUnavailable
	}
	if ValidateClientID(current.ClientID) != nil || current.ClientID != original.ClientID {
		return ErrUnavailable
	}
	if ValidateBusinessOrderNo(current.BusinessOrderNo) != nil ||
		current.BusinessOrderNo != original.BusinessOrderNo {
		return ErrUnavailable
	}
	if ValidateAmount(current.Amount) != nil || current.Amount != original.Amount {
		return ErrUnavailable
	}
	if ValidateCurrency(current.Currency) != nil || current.Currency != original.Currency {
		return ErrUnavailable
	}
	if current.RegisteredAt.IsZero() || ValidateUTC(current.RegisteredAt) != nil ||
		!current.RegisteredAt.Equal(original.RegisteredAt) {
		return ErrUnavailable
	}
	if current.Result != ResultSucceeded && current.Result != ResultFailed {
		return ErrUnavailable
	}
	if current.ConfirmedAt == nil || current.ConfirmedAt.IsZero() ||
		ValidateResultTime(current.Result, current.ConfirmedAt) != nil {
		return ErrUnavailable
	}
	if original.Result == ResultSucceeded || original.Result == ResultFailed {
		// A known confirmed fact is immutable: a marker may not change its
		// result or confirmation time. An UNKNOWN original may still become
		// confirmed under the lock.
		if current.Result != original.Result {
			return ErrUnavailable
		}
		if original.ConfirmedAt == nil || original.ConfirmedAt.IsZero() ||
			ValidateResultTime(original.Result, original.ConfirmedAt) != nil ||
			!current.ConfirmedAt.Equal(*original.ConfirmedAt) {
			return ErrUnavailable
		}
	}
	if original.MockTransactionID != nil {
		if current.MockTransactionID == nil ||
			*current.MockTransactionID != *original.MockTransactionID {
			return ErrUnavailable
		}
	}
	if current.MockTransactionID != nil &&
		ValidateUUIDv4(string(*current.MockTransactionID)) != nil {
		return ErrUnavailable
	}
	return nil
}

func queryTraceFailureRecordEmpty(record ResultHandlingRecord) bool {
	if record.ID != "" || record.PaymentOrderID != "" || record.RequestID != "" ||
		record.Source != "" || !record.ObservedAt.IsZero() ||
		record.ResultBefore != "" || record.ResultAfter != "" || record.Decision != "" ||
		record.ReasonCode != nil || record.ReasonMessage != nil {
		return false
	}
	evidence := record.Evidence
	return evidence.PlatformReferenceID == nil &&
		evidence.MockTransactionID == nil &&
		evidence.Amount == nil &&
		evidence.Currency == nil &&
		evidence.Result == nil &&
		len(evidence.Differences) == 0
}

func validateQueryReadbackOrder(current, original PaymentOrder) error {
	if current.ID != original.ID ||
		current.ClientID != original.ClientID ||
		current.BusinessOrderNo != original.BusinessOrderNo ||
		current.Amount != original.Amount ||
		current.Currency != original.Currency {
		return ErrUnavailable
	}
	if ValidateResultTime(current.Result, current.ConfirmedAt) != nil {
		return ErrUnavailable
	}
	if current.MockTransactionID != nil && ValidateUUIDv4(string(*current.MockTransactionID)) != nil {
		return ErrUnavailable
	}
	return nil
}
