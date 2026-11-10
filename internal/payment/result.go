package payment

import "time"

// Stable, sanitized classifications used by EvaluateMockEvidence and
// DecideObservation. They are not persisted as raw errors or payloads.
const (
	reasonCodeEvidenceInvalid         = "MOCK_EVIDENCE_INVALID"
	reasonCodeEvidenceMismatch        = "MOCK_EVIDENCE_MISMATCH"
	reasonCodeConfirmationConflict    = "MOCK_CONFIRMATION_CONFLICT"
	reasonCodeUnconfirmed             = "MOCK_RESULT_UNCONFIRMED"
	reasonCodeConfirmedResultKept     = "CONFIRMED_RESULT_PRESERVED"
	reasonMessageEvidenceInvalid      = "Mock 依据字段无效"
	reasonMessageEvidenceMismatch     = "Mock 依据与订单不匹配"
	reasonMessageConfirmationConflict = "Mock 依据与已有确认冲突"
	reasonMessageUnconfirmed          = "未取得可用的 Mock 确认"
	reasonMessageConfirmedResultKept  = "保留已有确认结果"
)

var evidenceFieldOrder = []EvidenceFieldName{
	EvidenceFieldPlatformReferenceID,
	EvidenceFieldMockTransactionID,
	EvidenceFieldAmount,
	EvidenceFieldCurrency,
	EvidenceFieldResult,
}

// EvaluateMockEvidence filters the fields actually obtained from a Mock
// transaction. A non-nil Reason means the evidence is invalid or does not
// match the original order, and the returned CandidateConfirmation is nil.
func EvaluateMockEvidence(order PaymentOrder, transaction MockTransaction) (Evidence, *CandidateConfirmation, *Reason) {
	var evidence Evidence
	var differences []EvidenceFieldName
	invalid := false
	mismatched := false

	orderIDValid := ValidateUUIDv4(string(order.ID)) == nil
	if !orderIDValid {
		invalid = true
		differences = append(differences, EvidenceFieldPlatformReferenceID)
	}
	orderAmountValid := ValidateAmount(order.Amount) == nil
	if !orderAmountValid {
		invalid = true
		differences = append(differences, EvidenceFieldAmount)
	}
	orderCurrencyValid := ValidateCurrency(order.Currency) == nil
	if !orderCurrencyValid {
		invalid = true
		differences = append(differences, EvidenceFieldCurrency)
	}

	if ValidateUUIDv4(string(transaction.ID)) == nil {
		id := transaction.ID
		evidence.MockTransactionID = &id
	} else {
		invalid = true
		differences = append(differences, EvidenceFieldMockTransactionID)
	}

	if ValidateUUIDv4(string(transaction.PlatformReferenceID)) == nil {
		reference := transaction.PlatformReferenceID
		evidence.PlatformReferenceID = &reference
		if !orderIDValid || reference != order.ID {
			mismatched = true
			differences = append(differences, EvidenceFieldPlatformReferenceID)
		}
	} else {
		invalid = true
		differences = append(differences, EvidenceFieldPlatformReferenceID)
	}

	if ValidateAmount(transaction.Amount) == nil {
		amount := transaction.Amount
		evidence.Amount = &amount
		if !orderAmountValid || amount != order.Amount {
			mismatched = true
			differences = append(differences, EvidenceFieldAmount)
		}
	} else {
		invalid = true
		differences = append(differences, EvidenceFieldAmount)
	}

	if ValidateCurrency(transaction.Currency) == nil {
		currency := transaction.Currency
		evidence.Currency = &currency
		if !orderCurrencyValid || currency != order.Currency {
			mismatched = true
			differences = append(differences, EvidenceFieldCurrency)
		}
	} else {
		invalid = true
		differences = append(differences, EvidenceFieldCurrency)
	}

	if transaction.Result == ResultSucceeded || transaction.Result == ResultFailed {
		result := transaction.Result
		evidence.Result = &result
	} else {
		invalid = true
		differences = append(differences, EvidenceFieldResult)
	}

	evidence.Differences = mergeDifferences(differences)
	if invalid || mismatched {
		code := reasonCodeEvidenceInvalid
		message := reasonMessageEvidenceInvalid
		if !invalid && mismatched {
			code = reasonCodeEvidenceMismatch
			message = reasonMessageEvidenceMismatch
		}
		return evidence, nil, fixedReason(code, message)
	}

	return evidence, &CandidateConfirmation{
		Result:            transaction.Result,
		MockTransactionID: mockIDPtr(transaction.ID),
	}, nil
}

// DecideObservation derives the post-save order and handling record from the
// locked current order and one fixed observation. It is pure: inputs are not
// mutated and pointer/slice values in the outputs do not alias the inputs.
func DecideObservation(order PaymentOrder, observation Observation, confirmedAt time.Time) (PaymentOrder, ResultHandlingRecord, error) {
	if err := validateDecisionOrder(order); err != nil {
		return PaymentOrder{}, ResultHandlingRecord{}, err
	}
	if err := validateDecisionObservation(order, observation); err != nil {
		return PaymentOrder{}, ResultHandlingRecord{}, err
	}

	evidence, differences, invalid, mismatched := screenEvidence(observation.Evidence, order)
	candidate := observation.Candidate
	resultAfter := order.Result
	accepted := false
	var decision HandlingDecision
	var reason *Reason

	switch {
	case observation.EvidenceRejected:
		if candidate != nil {
			invalid = true
			differences = mergeDifferences(differences, []EvidenceFieldName{EvidenceFieldResult})
		}
		evidence.Differences = differences
		reason = sanitizeReason(observation.Reason, reasonCodeEvidenceInvalid, reasonMessageEvidenceInvalid)
		decision = DecisionRejectEvidence
	case candidate != nil:
		check := verifyCandidate(order, evidence, candidate, differences, invalid, mismatched)
		evidence.Differences = check.differences
		if check.reason != nil {
			reason = check.reason
			decision = DecisionRejectEvidence
			resultAfter = order.Result
		} else if order.Result == ResultUnknown {
			if ValidateUTC(confirmedAt) != nil {
				return PaymentOrder{}, ResultHandlingRecord{}, ErrInvalidInput
			}
			accepted = true
			decision = DecisionAcceptConfirmation
			resultAfter = candidate.Result
		} else {
			decision = DecisionKeepConfirmed
			resultAfter = order.Result
		}
	default:
		evidence.Differences = differences
		switch {
		case invalid:
			reason = fixedReason(reasonCodeEvidenceInvalid, reasonMessageEvidenceInvalid)
			decision = DecisionRejectEvidence
			resultAfter = order.Result
		case mismatched || len(differences) > 0:
			reason = fixedReason(reasonCodeEvidenceMismatch, reasonMessageEvidenceMismatch)
			decision = DecisionRejectEvidence
			resultAfter = order.Result
		case order.Result == ResultUnknown:
			reason = sanitizeReason(observation.Reason, reasonCodeUnconfirmed, reasonMessageUnconfirmed)
			decision = DecisionKeepUnknown
			resultAfter = ResultUnknown
		default:
			reason = sanitizeReason(observation.Reason, reasonCodeConfirmedResultKept, reasonMessageConfirmedResultKept)
			decision = DecisionKeepConfirmed
			resultAfter = order.Result
		}
	}

	updated := clonePaymentOrder(order)
	if accepted {
		updated.Result = resultAfter
		at := confirmedAt
		updated.ConfirmedAt = &at
		if evidence.MockTransactionID != nil {
			updated.MockTransactionID = mockIDPtr(*evidence.MockTransactionID)
		}
	}

	record := ResultHandlingRecord{
		ID:             observation.ID,
		PaymentOrderID: order.ID,
		RequestID:      observation.RequestID,
		Source:         observation.Source,
		ObservedAt:     observation.ObservedAt,
		Evidence:       evidence,
		ResultBefore:   order.Result,
		ResultAfter:    resultAfter,
		Decision:       decision,
	}
	if reason != nil {
		code := reason.Code
		record.ReasonCode = &code
		if reason.Message != nil {
			message := *reason.Message
			record.ReasonMessage = &message
		}
	}

	return updated, record, nil
}

func validateDecisionOrder(order PaymentOrder) error {
	if ValidateUUIDv4(string(order.ID)) != nil {
		return ErrInvalidInput
	}
	if ValidateClientID(order.ClientID) != nil {
		return ErrInvalidInput
	}
	if ValidateBusinessOrderNo(order.BusinessOrderNo) != nil {
		return ErrInvalidInput
	}
	if ValidateAmount(order.Amount) != nil {
		return ErrInvalidInput
	}
	if ValidateCurrency(order.Currency) != nil {
		return ErrInvalidInput
	}
	if ValidateResultTime(order.Result, order.ConfirmedAt) != nil {
		return ErrInvalidInput
	}
	if order.MockTransactionID != nil && ValidateUUIDv4(string(*order.MockTransactionID)) != nil {
		return ErrInvalidInput
	}
	return nil
}

func validateDecisionObservation(order PaymentOrder, observation Observation) error {
	if ValidateUUIDv4(string(observation.ID)) != nil {
		return ErrInvalidInput
	}
	if observation.PaymentOrderID != order.ID {
		return ErrObservationConflict
	}
	if observation.ClientID != order.ClientID {
		return ErrObservationConflict
	}
	if ValidateUUIDv4(string(observation.RequestID)) != nil {
		return ErrInvalidInput
	}
	if ValidateSource(observation.Source) != nil {
		return ErrInvalidInput
	}
	if ValidateUTC(observation.ObservedAt) != nil {
		return ErrInvalidInput
	}
	return nil
}

func screenEvidence(raw Evidence, order PaymentOrder) (Evidence, []EvidenceFieldName, bool, bool) {
	var evidence Evidence
	var differences []EvidenceFieldName
	invalid := false
	mismatched := false

	for _, difference := range raw.Differences {
		if validEvidenceField(difference) {
			differences = append(differences, difference)
		} else {
			invalid = true
		}
	}

	if raw.PlatformReferenceID != nil {
		if ValidateUUIDv4(string(*raw.PlatformReferenceID)) == nil {
			reference := *raw.PlatformReferenceID
			evidence.PlatformReferenceID = &reference
			if reference != order.ID {
				mismatched = true
				differences = append(differences, EvidenceFieldPlatformReferenceID)
			}
		} else {
			invalid = true
			differences = append(differences, EvidenceFieldPlatformReferenceID)
		}
	}

	if raw.MockTransactionID != nil {
		if ValidateUUIDv4(string(*raw.MockTransactionID)) == nil {
			id := *raw.MockTransactionID
			evidence.MockTransactionID = &id
			if order.MockTransactionID != nil && id != *order.MockTransactionID {
				mismatched = true
				differences = append(differences, EvidenceFieldMockTransactionID)
			}
		} else {
			invalid = true
			differences = append(differences, EvidenceFieldMockTransactionID)
		}
	}

	if raw.Amount != nil {
		if ValidateAmount(*raw.Amount) == nil {
			amount := *raw.Amount
			evidence.Amount = &amount
			if amount != order.Amount {
				mismatched = true
				differences = append(differences, EvidenceFieldAmount)
			}
		} else {
			invalid = true
			differences = append(differences, EvidenceFieldAmount)
		}
	}

	if raw.Currency != nil {
		if ValidateCurrency(*raw.Currency) == nil {
			currency := *raw.Currency
			evidence.Currency = &currency
			if currency != order.Currency {
				mismatched = true
				differences = append(differences, EvidenceFieldCurrency)
			}
		} else {
			invalid = true
			differences = append(differences, EvidenceFieldCurrency)
		}
	}

	if raw.Result != nil {
		if *raw.Result == ResultSucceeded || *raw.Result == ResultFailed {
			result := *raw.Result
			evidence.Result = &result
			if order.Result != ResultUnknown && result != order.Result {
				mismatched = true
				differences = append(differences, EvidenceFieldResult)
			}
		} else {
			invalid = true
			differences = append(differences, EvidenceFieldResult)
		}
	}

	evidence.Differences = mergeDifferences(differences)
	return evidence, evidence.Differences, invalid, mismatched
}

type candidateCheck struct {
	differences []EvidenceFieldName
	reason      *Reason
}

func verifyCandidate(order PaymentOrder, evidence Evidence, candidate *CandidateConfirmation, differences []EvidenceFieldName, invalid, mismatched bool) candidateCheck {
	invalidCandidate := invalid
	mismatch := mismatched
	addDifference := func(field EvidenceFieldName) {
		differences = append(differences, field)
	}

	if candidate.Result != ResultSucceeded && candidate.Result != ResultFailed {
		invalidCandidate = true
		addDifference(EvidenceFieldResult)
	}

	if candidate.MockTransactionID != nil {
		if ValidateUUIDv4(string(*candidate.MockTransactionID)) != nil {
			invalidCandidate = true
			addDifference(EvidenceFieldMockTransactionID)
		} else if evidence.MockTransactionID == nil {
			invalidCandidate = true
			addDifference(EvidenceFieldMockTransactionID)
		} else if *evidence.MockTransactionID != *candidate.MockTransactionID {
			mismatch = true
			addDifference(EvidenceFieldMockTransactionID)
		}
	} else if evidence.MockTransactionID != nil {
		invalidCandidate = true
		addDifference(EvidenceFieldMockTransactionID)
	}

	if evidence.PlatformReferenceID == nil {
		invalidCandidate = true
		addDifference(EvidenceFieldPlatformReferenceID)
	} else if *evidence.PlatformReferenceID != order.ID {
		mismatch = true
		addDifference(EvidenceFieldPlatformReferenceID)
	}

	if evidence.Amount == nil {
		invalidCandidate = true
		addDifference(EvidenceFieldAmount)
	} else if *evidence.Amount != order.Amount {
		mismatch = true
		addDifference(EvidenceFieldAmount)
	}

	if evidence.Currency == nil {
		invalidCandidate = true
		addDifference(EvidenceFieldCurrency)
	} else if *evidence.Currency != order.Currency {
		mismatch = true
		addDifference(EvidenceFieldCurrency)
	}

	if evidence.Result == nil {
		invalidCandidate = true
		addDifference(EvidenceFieldResult)
	} else if candidate.Result == ResultSucceeded || candidate.Result == ResultFailed {
		if *evidence.Result != candidate.Result {
			mismatch = true
			addDifference(EvidenceFieldResult)
		}
	}

	if order.Result != ResultUnknown {
		if candidate.Result == ResultSucceeded || candidate.Result == ResultFailed {
			if candidate.Result != order.Result {
				mismatch = true
				addDifference(EvidenceFieldResult)
			}
		}
		if order.MockTransactionID != nil && candidate.MockTransactionID != nil &&
			*candidate.MockTransactionID != *order.MockTransactionID {
			mismatch = true
			addDifference(EvidenceFieldMockTransactionID)
		}
	}

	differences = mergeDifferences(differences)
	if invalidCandidate || mismatch || len(differences) > 0 {
		code := reasonCodeEvidenceInvalid
		message := reasonMessageEvidenceInvalid
		switch {
		case !invalidCandidate && mismatch && order.Result != ResultUnknown:
			code = reasonCodeConfirmationConflict
			message = reasonMessageConfirmationConflict
		case !invalidCandidate && (mismatch || len(differences) > 0):
			code = reasonCodeEvidenceMismatch
			message = reasonMessageEvidenceMismatch
		}
		return candidateCheck{differences: differences, reason: fixedReason(code, message)}
	}
	return candidateCheck{differences: differences}
}

func sanitizeReason(reason *Reason, fallbackCode, fallbackMessage string) *Reason {
	if reason == nil || ValidateReason(reason) != nil {
		return fixedReason(fallbackCode, fallbackMessage)
	}
	out := &Reason{Code: reason.Code}
	if reason.Message != nil {
		message := *reason.Message
		out.Message = &message
	}
	return out
}

func fixedReason(code, message string) *Reason {
	text := message
	return &Reason{Code: code, Message: &text}
}

func validEvidenceField(field EvidenceFieldName) bool {
	switch field {
	case EvidenceFieldPlatformReferenceID,
		EvidenceFieldMockTransactionID,
		EvidenceFieldAmount,
		EvidenceFieldCurrency,
		EvidenceFieldResult:
		return true
	default:
		return false
	}
}

func mergeDifferences(lists ...[]EvidenceFieldName) []EvidenceFieldName {
	seen := make(map[EvidenceFieldName]bool)
	for _, list := range lists {
		for _, field := range list {
			if validEvidenceField(field) {
				seen[field] = true
			}
		}
	}
	var merged []EvidenceFieldName
	for _, field := range evidenceFieldOrder {
		if seen[field] {
			merged = append(merged, field)
		}
	}
	return merged
}

func clonePaymentOrder(order PaymentOrder) PaymentOrder {
	cloned := order
	if order.MockTransactionID != nil {
		id := *order.MockTransactionID
		cloned.MockTransactionID = &id
	}
	if order.ConfirmedAt != nil {
		at := *order.ConfirmedAt
		cloned.ConfirmedAt = &at
	}
	return cloned
}

func mockIDPtr(id MockTransactionID) *MockTransactionID {
	return &id
}
