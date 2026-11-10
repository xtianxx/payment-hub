package payment

import (
	"context"
	"errors"
	"time"
)

// The five fixed issue codes in the HTTP contract.
const (
	IssueCodeMockResultUnconfirmed = "MOCK_RESULT_UNCONFIRMED"
	IssueCodeMockResultNotFound    = "MOCK_RESULT_NOT_FOUND"
	IssueCodeMockEvidenceRejected  = "MOCK_EVIDENCE_REJECTED"
	IssueCodeResultSaveError       = "RESULT_SAVE_ERROR"
	IssueCodeTraceSaveError        = "TRACE_SAVE_ERROR"
)

const (
	issueMessageMockResultUnconfirmed = "本次付款调用未取得可用 Mock 答复"
	issueMessageMockResultNotFound    = "未取得匹配的 Mock 交易记录"
	issueMessageMockEvidenceRejected  = "Mock 依据与订单不匹配或无效"
	issueMessageResultSaveError       = "本次结果保存失败或提交情况无法确认"
	issueMessageTraceSaveError        = "本次追踪保存失败或无法确认"
)

const (
	mainProcessingTimeout = 10 * time.Second
	mockPaymentTimeout    = 3 * time.Second
	finishWindowTimeout   = 2 * time.Second
)

// Issue reports one sanitized processing problem for an otherwise returnable
// payment order.
type Issue struct {
	Code    string
	Message string
}

// SubmitResult is the business result of one POST submission. When error is
// non-nil, callers must not treat Order as a response; Created may still be
// true when a confirmed registration was followed by a cancellation.
type SubmitResult struct {
	Order   PaymentOrder
	Created bool
	Issues  []Issue
}

// Submitter orchestrates one payment submission using the existing narrow
// ports. It never retries a payment and never starts background work.
type Submitter struct {
	registrar OrderRegistrar
	lookup    OrderLookup
	recorder  ObservationRecorder
	payer     MockPayer
	lifecycle context.Context
	invalid   bool
}

// NewSubmitter builds a Submitter bound to the application lifecycle. A nil
// dependency or nil lifecycle produces a Submitter whose Submit fails safely.
func NewSubmitter(registrar OrderRegistrar, lookup OrderLookup, recorder ObservationRecorder, payer MockPayer, lifecycle context.Context) *Submitter {
	if registrar == nil || lookup == nil || recorder == nil || payer == nil || lifecycle == nil {
		return &Submitter{invalid: true}
	}
	return &Submitter{
		registrar: registrar,
		lookup:    lookup,
		recorder:  recorder,
		payer:     payer,
		lifecycle: lifecycle,
	}
}

// Submit validates one intent, registers it, calls Mock at most once for a
// newly created order, and saves the fixed observation synchronously.
func (s *Submitter) Submit(ctx context.Context, requestID RequestID, input RegisterOrderInput) (SubmitResult, error) {
	if s == nil || s.invalid || s.lifecycle == nil {
		return SubmitResult{}, ErrUnavailable
	}
	if ctx == nil {
		return SubmitResult{}, ErrInvalidInput
	}
	if s.lifecycle.Err() != nil {
		return SubmitResult{}, ErrUnavailable
	}
	if ctx.Err() != nil {
		return SubmitResult{}, ctx.Err()
	}
	if err := validateSubmitRequestInput(requestID, input); err != nil {
		return SubmitResult{}, err
	}
	if err := validateSubmitBusinessInput(input); err != nil {
		return s.resolveInvalidBusinessValue(ctx, input)
	}

	workCtx, cancelWork := s.newWorkContext(ctx)
	defer cancelWork()

	registered, registerErr := s.registrar.RegisterOrder(workCtx, input)
	if registerErr != nil {
		if errors.Is(registerErr, ErrRegistrationRejected) {
			return SubmitResult{}, ErrRegistrationRejected
		}
		return s.recoverRegistration(input, registerErr)
	}
	if err := validateRegisterResult(registered, input); err != nil {
		return SubmitResult{}, err
	}
	order := clonePaymentOrder(registered.Order)

	if registered.Outcome == RegisterOutcomeReused {
		if order.Amount != input.Amount || order.Currency != input.Currency {
			return SubmitResult{Order: order, Created: false}, ErrConflict
		}
		return SubmitResult{Order: order, Created: false}, nil
	}

	// Only a confirmed created registration grants this request one payment
	// attempt. Re-check cancellation before starting the Mock call. Lifecycle
	// shutdown is classified deterministically before a client-only cancel so
	// the asynchronous cancellation linkage cannot change the error identity.
	if s.lifecycle.Err() != nil {
		return SubmitResult{Order: order, Created: true}, ErrUnavailable
	}
	if workCtx.Err() != nil {
		return SubmitResult{Order: order, Created: true}, workCtx.Err()
	}

	payCtx, cancelPay := context.WithTimeout(workCtx, mockPaymentTimeout)
	transaction, payErr := s.payer.Pay(payCtx, MockPaymentRequest{
		PlatformReferenceID: order.ID,
		Amount:              input.Amount,
		Currency:            input.Currency,
	})
	cancelPay()

	finish := newFinishScope(s.lifecycle)
	defer finish.cancel()

	if payErr != nil {
		observation := Observation{
			ID:             ResultHandlingRecordID(NewUUIDv4()),
			PaymentOrderID: order.ID,
			ClientID:       order.ClientID,
			RequestID:      requestID,
			Source:         SourcePayment,
			ObservedAt:     time.Now().UTC(),
			Reason:         fixedReason(reasonCodeUnconfirmed, issueMessageMockResultUnconfirmed),
		}
		return s.saveAndReport(
			finish.ctx,
			order,
			observation,
			false,
			[]Issue{newIssue(IssueCodeMockResultUnconfirmed)},
		)
	}

	evidence, candidate, reason := EvaluateMockEvidence(order, transaction)
	observation := Observation{
		ID:               ResultHandlingRecordID(NewUUIDv4()),
		PaymentOrderID:   order.ID,
		ClientID:         order.ClientID,
		RequestID:        requestID,
		Source:           SourcePayment,
		ObservedAt:       time.Now().UTC(),
		Evidence:         evidence,
		EvidenceRejected: reason != nil,
		Reason:           reason,
		Candidate:        candidate,
	}
	var initialIssues []Issue
	if reason != nil {
		initialIssues = []Issue{newIssue(IssueCodeMockEvidenceRejected)}
	}
	return s.saveAndReport(
		finish.ctx,
		order,
		observation,
		candidate != nil && reason == nil,
		initialIssues,
	)
}

func validateRegisterResult(result RegisterOrderResult, input RegisterOrderInput) error {
	if result.Outcome != RegisterOutcomeCreated && result.Outcome != RegisterOutcomeReused {
		return ErrUnavailable
	}
	order := result.Order
	if ValidateUUIDv4(string(order.ID)) != nil {
		return ErrUnavailable
	}
	if order.ClientID != input.ClientID || order.BusinessOrderNo != input.BusinessOrderNo {
		return ErrUnavailable
	}
	if ValidateResultTime(order.Result, order.ConfirmedAt) != nil {
		return ErrUnavailable
	}
	if order.MockTransactionID != nil && ValidateUUIDv4(string(*order.MockTransactionID)) != nil {
		return ErrUnavailable
	}
	if result.Outcome == RegisterOutcomeCreated {
		if order.Amount != input.Amount || order.Currency != input.Currency {
			return ErrUnavailable
		}
		if order.Result != ResultUnknown || order.ConfirmedAt != nil || order.MockTransactionID != nil {
			return ErrUnavailable
		}
	}
	return nil
}

func (s *Submitter) recoverRegistration(input RegisterOrderInput, registerErr error) (SubmitResult, error) {
	// A definite non-business registration failure is not an unknown commit
	// and must not be read back into intent reuse or payment eligibility.
	if errors.Is(registerErr, ErrRegistrationRejected) {
		return SubmitResult{}, ErrRegistrationRejected
	}

	finish := newFinishScope(s.lifecycle)
	defer finish.cancel()

	// A stopped lifecycle cannot produce new valid readback work.
	if s.lifecycle.Err() != nil || finish.ctx.Err() != nil {
		return SubmitResult{}, ErrUnavailable
	}
	order, found, lookupErr := s.lookup.FindOrderByBusinessOrderNo(finish.ctx, input.ClientID, input.BusinessOrderNo)
	if s.lifecycle.Err() != nil || finish.ctx.Err() != nil {
		return SubmitResult{}, ErrUnavailable
	}
	if lookupErr != nil {
		if errors.Is(registerErr, ErrUnavailable) || errors.Is(lookupErr, ErrUnavailable) {
			return SubmitResult{}, ErrUnavailable
		}
		return SubmitResult{}, ErrIndeterminate
	}
	if !found {
		if errors.Is(registerErr, ErrUnavailable) {
			return SubmitResult{}, ErrUnavailable
		}
		return SubmitResult{}, ErrIndeterminate
	}
	if err := validateRecoveredOrderIdentity(order, input); err != nil {
		return SubmitResult{}, err
	}
	if s.lifecycle.Err() != nil || finish.ctx.Err() != nil {
		return SubmitResult{}, ErrUnavailable
	}
	if order.Amount != input.Amount || order.Currency != input.Currency {
		return SubmitResult{Order: clonePaymentOrder(order), Created: false}, ErrConflict
	}
	return SubmitResult{
		Order:   clonePaymentOrder(order),
		Created: false,
		Issues:  []Issue{newIssue(IssueCodeResultSaveError)},
	}, nil
}

func (s *Submitter) saveAndReport(
	finishCtx context.Context,
	order PaymentOrder,
	observation Observation,
	newConfirmation bool,
	issues []Issue,
) (SubmitResult, error) {
	saved, saveErr := s.recorder.SaveObservation(finishCtx, observation)
	if saveErr == nil {
		if err := validateSavedResult(saved, order, observation); err != nil {
			return SubmitResult{}, err
		}
		issues = issuesForSavedRecord(issues, saved.Record, newConfirmation)
		return SubmitResult{
			Order:   clonePaymentOrder(saved.Order),
			Created: true,
			Issues:  cloneIssues(issues),
		}, nil
	}

	// A save error never authorizes using the returned Order or Record as a
	// committed fact. Read the current order with the same finish deadline.
	current, found, lookupErr := s.lookup.FindOrderByID(finishCtx, order.ClientID, order.ID)
	if s.lifecycle.Err() != nil {
		return SubmitResult{}, ErrUnavailable
	}
	if lookupErr != nil {
		if errors.Is(saveErr, ErrUnavailable) || errors.Is(lookupErr, ErrUnavailable) {
			return SubmitResult{}, ErrUnavailable
		}
		return SubmitResult{}, ErrIndeterminate
	}
	if !found {
		if errors.Is(saveErr, ErrUnavailable) {
			return SubmitResult{}, ErrUnavailable
		}
		return SubmitResult{}, ErrIndeterminate
	}
	identityInput := RegisterOrderInput{
		ClientID:        order.ClientID,
		BusinessOrderNo: order.BusinessOrderNo,
		Amount:          order.Amount,
		Currency:        order.Currency,
	}
	if err := validateRecoveredOrderIdentity(current, identityInput); err != nil {
		return SubmitResult{}, err
	}
	if current.Amount != order.Amount || current.Currency != order.Currency {
		return SubmitResult{}, ErrUnavailable
	}
	if s.lifecycle.Err() != nil {
		return SubmitResult{}, ErrUnavailable
	}

	saveIssue := IssueCodeTraceSaveError
	if newConfirmation {
		saveIssue = IssueCodeResultSaveError
	}
	issues = appendIssue(issues, newIssue(saveIssue))
	return SubmitResult{
		Order:   clonePaymentOrder(current),
		Created: true,
		Issues:  cloneIssues(issues),
	}, nil
}

func validateRecoveredOrderIdentity(order PaymentOrder, input RegisterOrderInput) error {
	if ValidateUUIDv4(string(order.ID)) != nil {
		return ErrUnavailable
	}
	if order.ClientID != input.ClientID || order.BusinessOrderNo != input.BusinessOrderNo {
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

func validateSavedResult(saved SaveObservationResult, order PaymentOrder, observation Observation) error {
	if saved.Order.ID != order.ID ||
		saved.Order.ClientID != order.ClientID ||
		saved.Order.BusinessOrderNo != order.BusinessOrderNo ||
		saved.Order.Amount != order.Amount ||
		saved.Order.Currency != order.Currency {
		return ErrUnavailable
	}
	if ValidateResultTime(saved.Order.Result, saved.Order.ConfirmedAt) != nil {
		return ErrUnavailable
	}
	if saved.Order.MockTransactionID != nil && ValidateUUIDv4(string(*saved.Order.MockTransactionID)) != nil {
		return ErrUnavailable
	}
	if saved.Record.ID != observation.ID || saved.Record.PaymentOrderID != order.ID {
		return ErrUnavailable
	}
	if saved.Record.RequestID != observation.RequestID || saved.Record.Source != observation.Source {
		return ErrUnavailable
	}
	if ValidateHandlingDecision(saved.Record.Decision) != nil {
		return ErrUnavailable
	}
	if ValidateResult(saved.Record.ResultBefore) != nil || ValidateResult(saved.Record.ResultAfter) != nil {
		return ErrUnavailable
	}
	if !validSavedRecordTransition(saved.Record) {
		return ErrUnavailable
	}
	if saved.Order.Result == saved.Record.ResultAfter {
		return nil
	}
	// A same-ID replay returns the historical record with the current order.
	// The historical record may have kept UNKNOWN while a later observation
	// legitimately confirmed the order. Accept only that forward evolution:
	// the historical record must be an UNKNOWN-to-UNKNOWN keep/reject, and the
	// current order must be confirmed. A confirmed historical after-state can
	// never be reverted or flipped.
	if saved.Record.ResultBefore == ResultUnknown &&
		saved.Record.ResultAfter == ResultUnknown &&
		(saved.Record.Decision == DecisionKeepUnknown || saved.Record.Decision == DecisionRejectEvidence) &&
		(saved.Order.Result == ResultSucceeded || saved.Order.Result == ResultFailed) {
		return nil
	}
	return ErrUnavailable
}

func validSavedRecordTransition(record ResultHandlingRecord) bool {
	switch record.Decision {
	case DecisionAcceptConfirmation:
		return record.ResultBefore == ResultUnknown &&
			(record.ResultAfter == ResultSucceeded || record.ResultAfter == ResultFailed)
	case DecisionKeepUnknown:
		return record.ResultBefore == ResultUnknown && record.ResultAfter == ResultUnknown
	case DecisionKeepConfirmed:
		return (record.ResultBefore == ResultSucceeded || record.ResultBefore == ResultFailed) &&
			record.ResultBefore == record.ResultAfter
	case DecisionRejectEvidence:
		return record.ResultBefore == record.ResultAfter
	default:
		return false
	}
}

func issuesForSavedRecord(issues []Issue, record ResultHandlingRecord, newConfirmation bool) []Issue {
	switch record.Decision {
	case DecisionRejectEvidence:
		return appendIssue(issues, newIssue(IssueCodeMockEvidenceRejected))
	case DecisionKeepUnknown:
		if newConfirmation {
			return appendIssue(issues, newIssue(IssueCodeMockResultUnconfirmed))
		}
	case DecisionAcceptConfirmation, DecisionKeepConfirmed:
	}
	return issues
}

func newIssue(code string) Issue {
	return Issue{Code: code, Message: issueMessageForCode(code)}
}

func issueMessageForCode(code string) string {
	switch code {
	case IssueCodeMockResultUnconfirmed:
		return issueMessageMockResultUnconfirmed
	case IssueCodeMockResultNotFound:
		return issueMessageMockResultNotFound
	case IssueCodeMockEvidenceRejected:
		return issueMessageMockEvidenceRejected
	case IssueCodeResultSaveError:
		return issueMessageResultSaveError
	case IssueCodeTraceSaveError:
		return issueMessageTraceSaveError
	default:
		return issueMessageTraceSaveError
	}
}

func appendIssue(issues []Issue, issue Issue) []Issue {
	for _, existing := range issues {
		if existing.Code == issue.Code {
			return issues
		}
	}
	cloned := cloneIssues(issues)
	return append(cloned, issue)
}

func cloneIssues(issues []Issue) []Issue {
	if len(issues) == 0 {
		return nil
	}
	cloned := make([]Issue, len(issues))
	copy(cloned, issues)
	return cloned
}

type finishScope struct {
	ctx    context.Context
	cancel context.CancelFunc
}

func newFinishScope(lifecycle context.Context) finishScope {
	ctx, cancel := context.WithTimeout(lifecycle, finishWindowTimeout)
	return finishScope{ctx: ctx, cancel: cancel}
}

// newWorkContext bounds main processing at 10 seconds and links the work
// context to both the client request and the application lifecycle. The
// lifecycle callback only cancels the existing context; it never starts
// payment or other background work. The returned cancel function stops and
// releases the lifecycle callback before canceling the work context.
func (s *Submitter) newWorkContext(ctx context.Context) (context.Context, context.CancelFunc) {
	workCtx, cancelWork := context.WithTimeout(ctx, mainProcessingTimeout)
	stopLifecycle := context.AfterFunc(s.lifecycle, cancelWork)
	return workCtx, func() {
		stopLifecycle()
		cancelWork()
	}
}
