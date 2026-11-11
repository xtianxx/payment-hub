package postgres

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"payment-hub/internal/payment"
)

// observationBudget is the complete short-transaction budget for one
// SaveObservation call. Precheck, BEGIN, lock, decision, writes, COMMIT,
// rollback, hooks, and bounded read-back share this one deadline.
const observationBudget = 2 * time.Second

const (
	selectRecordByIDScopedSQL = `SELECT r.id::text, r.payment_order_id::text, r.request_id::text,
		r.source, r.observed_at, r.evidence, r.result_before, r.result_after,
		r.decision, r.reason_code, r.reason_message
		FROM result_handling_records r
		JOIN payment_orders o ON o.id = r.payment_order_id
		WHERE r.id = $1 AND o.client_id = $2`

	selectOrderByIDForUpdateSQL = `SELECT id::text, client_id, business_order_no, amount, currency, result,
		mock_transaction_id::text, registered_at, confirmed_at
		FROM payment_orders
		WHERE id = $1 AND client_id = $2
		FOR UPDATE`

	updateOrderConfirmationSQL = `UPDATE payment_orders
		SET result = $3, confirmed_at = $4, mock_transaction_id = $5
		WHERE id = $1 AND client_id = $2 AND result = $6`

	appendResultHandlingRecordSQL = `INSERT INTO result_handling_records
		(id, payment_order_id, request_id, source, observed_at, evidence,
		 result_before, result_after, decision, reason_code, reason_message)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)`
)

var errStoredEvidence = errors.New("postgres: stored evidence is not an allowed object")

var allowedEvidenceJSONFields = map[string]struct{}{
	"platform_reference_id": {},
	"mock_transaction_id":   {},
	"amount":                {},
	"currency":              {},
	"result":                {},
	"differences":           {},
}

// SaveObservation performs the T032 atomic save. It fixes a deep copy of the
// observation at entry, reuses an existing same-ID record only after checking
// its historical semantics in the current client scope, otherwise locks the
// scoped order without filtering UNKNOWN, derives the decision through
// payment.DecideObservation, conditionally updates only an accepted
// confirmation, and appends the handling record in the same transaction.
func (s *Store) SaveObservation(ctx context.Context, observation payment.Observation) (payment.SaveObservationResult, error) {
	if s == nil || isNilDB(s.db) {
		return payment.SaveObservationResult{}, payment.ErrUnavailable
	}
	if ctx == nil {
		return payment.SaveObservationResult{}, payment.ErrInvalidInput
	}

	observation = cloneObservation(observation)
	if err := validateObservationInput(observation); err != nil {
		return payment.SaveObservationResult{}, err
	}
	observation.ObservedAt = observation.ObservedAt.UTC().Truncate(time.Microsecond)

	if ctx.Err() != nil {
		return payment.SaveObservationResult{}, payment.ErrUnavailable
	}

	opCtx, cancel := context.WithTimeout(ctx, observationBudget)
	defer cancel()

	// A committed record for this exact ID is checked first. The join enforces
	// the current client scope. This is not deduplication by order or request.
	existingRecord, found, err := s.readRecordByIDScoped(opCtx, observation.ID, observation.ClientID)
	if err != nil {
		return payment.SaveObservationResult{}, err
	}
	if found {
		return s.reuseCommittedObservation(opCtx, observation, existingRecord)
	}

	tx, err := s.db.BeginTx(opCtx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if err != nil {
		return payment.SaveObservationResult{}, payment.ErrUnavailable
	}
	if tx == nil {
		return payment.SaveObservationResult{}, payment.ErrUnavailable
	}
	// Rollback is registered after cancel so it runs before cancel and uses the
	// same still-live operation deadline. ErrTxClosed after a commit is
	// expected and ignored.
	defer tx.Rollback(opCtx)

	order, err := scanPaymentOrder(tx.QueryRow(opCtx, selectOrderByIDForUpdateSQL,
		string(observation.PaymentOrderID), observation.ClientID))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return payment.SaveObservationResult{}, payment.ErrNotFound
		}
		return payment.SaveObservationResult{}, payment.ErrUnavailable
	}

	// Recheck the same record ID after the order lock. A concurrent save for
	// this order must either have committed before the lock was acquired or be
	// waiting behind it; this closes the wait-then-insert race.
	existingRecord, found, err = queryRecordByIDScoped(opCtx, tx, observation.ID, observation.ClientID)
	if err != nil {
		return payment.SaveObservationResult{}, payment.ErrUnavailable
	}
	if found {
		if err := tx.Rollback(opCtx); err != nil && !errors.Is(err, pgx.ErrTxClosed) {
			return payment.SaveObservationResult{}, payment.ErrUnavailable
		}
		match, matchErr := observationMatchesRecord(observation, existingRecord, order)
		if matchErr != nil {
			return payment.SaveObservationResult{}, matchErr
		}
		if !match {
			return payment.SaveObservationResult{}, payment.ErrObservationConflict
		}
		return payment.SaveObservationResult{Order: order, Record: existingRecord}, nil
	}

	// Fix the original committed confirmation, if any, before this attempt
	// performs any write. Only this deep-copied snapshot may accompany the two
	// trace-save markers; an UNKNOWN order or a confirmation accepted by this
	// attempt is never marker-eligible.
	originalConfirmation, err := snapshotOriginalConfirmation(order, observation)
	if err != nil {
		return payment.SaveObservationResult{}, err
	}

	confirmedAt := time.Now().UTC().Truncate(time.Microsecond)
	updated, record, err := payment.DecideObservation(order, observation, confirmedAt)
	if err != nil {
		return payment.SaveObservationResult{}, err
	}

	if err := s.hooks.SavingResult(opCtx, order.ID); err != nil {
		return payment.SaveObservationResult{}, payment.ErrUnavailable
	}

	if record.Decision == payment.DecisionAcceptConfirmation {
		if originalConfirmation != nil {
			// A locked original confirmation is never rewritten by this path.
			return payment.SaveObservationResult{}, payment.ErrUnavailable
		}
		if err := updateOrderConfirmation(opCtx, tx, order, updated); err != nil {
			return payment.SaveObservationResult{}, err
		}
	}

	if err := insertResultHandlingRecord(opCtx, tx, record); err != nil {
		rollbackErr := tx.Rollback(opCtx)
		rollbackConfirmed := rollbackErr == nil
		if isRecordPrimaryKeyConflict(err) {
			if !rollbackConfirmed && !errors.Is(rollbackErr, pgx.ErrTxClosed) {
				return traceSaveResult(originalConfirmation, payment.ErrTraceSaveIndeterminate)
			}
			result, readErr := s.readBackDuplicateObservation(opCtx, observation)
			if readErr == nil {
				return result, nil
			}
			if errors.Is(readErr, payment.ErrObservationConflict) {
				return payment.SaveObservationResult{}, readErr
			}
			if originalConfirmation != nil {
				return traceSaveResult(originalConfirmation, payment.ErrTraceSaveIndeterminate)
			}
			return payment.SaveObservationResult{}, readErr
		}
		if originalConfirmation != nil {
			if rollbackConfirmed {
				return traceSaveResult(originalConfirmation, payment.ErrTraceSaveFailed)
			}
			return traceSaveResult(originalConfirmation, payment.ErrTraceSaveIndeterminate)
		}
		if !rollbackConfirmed {
			return payment.SaveObservationResult{}, payment.ErrIndeterminate
		}
		return payment.SaveObservationResult{}, classifySaveError(err)
	}

	if err := tx.Commit(opCtx); err != nil {
		if errors.Is(err, pgx.ErrTxCommitRollback) {
			if originalConfirmation != nil {
				return traceSaveResult(originalConfirmation, payment.ErrTraceSaveFailed)
			}
			return payment.SaveObservationResult{}, payment.ErrUnavailable
		}
		// The commit outcome is unknown. End the transaction attempt and use
		// the remaining deadline to verify the same record ID and scoped order.
		_ = tx.Rollback(opCtx)
		result, readErr := s.readBackCommittedObservation(opCtx, observation)
		if readErr == nil {
			return result, nil
		}
		if errors.Is(readErr, payment.ErrObservationConflict) {
			// A definite same-ID record with a different meaning is not a
			// trace-save marker, even when the commit outcome was unknown.
			return payment.SaveObservationResult{}, readErr
		}
		if originalConfirmation != nil {
			// The original confirmation remains reliable even when the extra
			// trace read-back cannot confirm this attempt's record.
			return traceSaveResult(originalConfirmation, payment.ErrTraceSaveIndeterminate)
		}
		return payment.SaveObservationResult{}, readErr
	}

	if err := s.hooks.ResultCommitted(opCtx, order.ID); err != nil {
		if errors.Is(err, ErrReplyHidden) {
			return payment.SaveObservationResult{}, ErrReplyHidden
		}
		// The fact is committed; a postcommit observation failure must not be
		// reported as a precommit ErrUnavailable.
		return payment.SaveObservationResult{}, payment.ErrIndeterminate
	}

	return payment.SaveObservationResult{Order: updated, Record: record}, nil
}

func (s *Store) reuseCommittedObservation(ctx context.Context, observation payment.Observation, record payment.ResultHandlingRecord) (payment.SaveObservationResult, error) {
	order, found, err := s.readOrderByIDScoped(ctx, observation.ClientID, observation.PaymentOrderID)
	if err != nil {
		return payment.SaveObservationResult{}, err
	}
	if !found {
		return payment.SaveObservationResult{}, payment.ErrUnavailable
	}
	match, matchErr := observationMatchesRecord(observation, record, order)
	if matchErr != nil {
		return payment.SaveObservationResult{}, matchErr
	}
	if !match {
		return payment.SaveObservationResult{}, payment.ErrObservationConflict
	}
	return payment.SaveObservationResult{Order: order, Record: record}, nil
}

func (s *Store) readBackCommittedObservation(ctx context.Context, observation payment.Observation) (payment.SaveObservationResult, error) {
	record, found, err := s.readRecordByIDScoped(ctx, observation.ID, observation.ClientID)
	if err != nil || !found {
		return payment.SaveObservationResult{}, payment.ErrIndeterminate
	}
	order, found, err := s.readOrderByIDScoped(ctx, observation.ClientID, observation.PaymentOrderID)
	if err != nil || !found {
		return payment.SaveObservationResult{}, payment.ErrIndeterminate
	}
	match, matchErr := observationMatchesRecord(observation, record, order)
	if matchErr != nil {
		return payment.SaveObservationResult{}, payment.ErrIndeterminate
	}
	if !match {
		return payment.SaveObservationResult{}, payment.ErrObservationConflict
	}
	return payment.SaveObservationResult{Order: order, Record: record}, nil
}

func (s *Store) readBackDuplicateObservation(ctx context.Context, observation payment.Observation) (payment.SaveObservationResult, error) {
	record, found, err := s.readRecordByIDScoped(ctx, observation.ID, observation.ClientID)
	if err != nil || !found {
		return payment.SaveObservationResult{}, payment.ErrIndeterminate
	}
	order, found, err := s.readOrderByIDScoped(ctx, observation.ClientID, observation.PaymentOrderID)
	if err != nil || !found {
		return payment.SaveObservationResult{}, payment.ErrIndeterminate
	}
	match, matchErr := observationMatchesRecord(observation, record, order)
	if matchErr != nil {
		return payment.SaveObservationResult{}, payment.ErrIndeterminate
	}
	if !match {
		return payment.SaveObservationResult{}, payment.ErrObservationConflict
	}
	return payment.SaveObservationResult{Order: order, Record: record}, nil
}

// snapshotOriginalConfirmation fixes a deep copy of a locked order that was
// already SUCCEEDED or FAILED before this save attempt wrote anything. The two
// trace-save markers may carry only this snapshot. The scoped identity,
// immutable order facts, confirmation time, and any available Mock association
// are validated here; a failed validation never becomes a marker.
func snapshotOriginalConfirmation(order payment.PaymentOrder, observation payment.Observation) (*payment.PaymentOrder, error) {
	if order.Result == payment.ResultUnknown {
		return nil, nil
	}
	if order.ID != observation.PaymentOrderID || order.ClientID != observation.ClientID {
		return nil, payment.ErrUnavailable
	}
	if payment.ValidateUUIDv4(string(order.ID)) != nil ||
		payment.ValidateClientID(order.ClientID) != nil ||
		payment.ValidateBusinessOrderNo(order.BusinessOrderNo) != nil ||
		payment.ValidateAmount(order.Amount) != nil ||
		payment.ValidateCurrency(order.Currency) != nil ||
		order.RegisteredAt.IsZero() ||
		payment.ValidateUTC(order.RegisteredAt) != nil ||
		payment.ValidateResultTime(order.Result, order.ConfirmedAt) != nil {
		return nil, payment.ErrUnavailable
	}
	if order.MockTransactionID != nil && payment.ValidateUUIDv4(string(*order.MockTransactionID)) != nil {
		return nil, payment.ErrUnavailable
	}
	snapshot := clonePaymentOrder(order)
	return &snapshot, nil
}

// traceSaveResult returns the original confirmation snapshot only for the two
// trace-save markers. A nil snapshot can never produce either marker: it is an
// UNKNOWN order or a confirmation owned by this attempt, so the ordinary
// indeterminate error is returned with an empty result. The record is always
// empty: neither marker proves that this attempt's trace was saved.
func traceSaveResult(snapshot *payment.PaymentOrder, sentinel error) (payment.SaveObservationResult, error) {
	if snapshot == nil {
		return payment.SaveObservationResult{}, payment.ErrIndeterminate
	}
	return payment.SaveObservationResult{Order: clonePaymentOrder(*snapshot)}, sentinel
}

func clonePaymentOrder(in payment.PaymentOrder) payment.PaymentOrder {
	out := in
	out.ConfirmedAt = cloneTimePointer(in.ConfirmedAt)
	out.MockTransactionID = cloneMockIDPointer(in.MockTransactionID)
	return out
}

func validateObservationInput(observation payment.Observation) error {
	if payment.ValidateUUIDv4(string(observation.ID)) != nil {
		return payment.ErrInvalidInput
	}
	if payment.ValidateUUIDv4(string(observation.PaymentOrderID)) != nil {
		return payment.ErrInvalidInput
	}
	if payment.ValidateClientID(observation.ClientID) != nil {
		return payment.ErrInvalidInput
	}
	if payment.ValidateUUIDv4(string(observation.RequestID)) != nil {
		return payment.ErrInvalidInput
	}
	if payment.ValidateSource(observation.Source) != nil {
		return payment.ErrInvalidInput
	}
	if payment.ValidateUTC(observation.ObservedAt) != nil {
		return payment.ErrInvalidInput
	}
	if payment.ValidateReason(observation.Reason) != nil {
		return payment.ErrInvalidInput
	}
	return nil
}

func (s *Store) readRecordByIDScoped(ctx context.Context, recordID payment.ResultHandlingRecordID, clientID string) (payment.ResultHandlingRecord, bool, error) {
	if s == nil || isNilDB(s.db) {
		return payment.ResultHandlingRecord{}, false, payment.ErrUnavailable
	}
	return queryRecordByIDScoped(ctx, s.db, recordID, clientID)
}

func queryRecordByIDScoped(ctx context.Context, q queryRower, recordID payment.ResultHandlingRecordID, clientID string) (payment.ResultHandlingRecord, bool, error) {
	record, err := scanResultHandlingRecord(q.QueryRow(ctx, selectRecordByIDScopedSQL, string(recordID), clientID))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return payment.ResultHandlingRecord{}, false, nil
		}
		return payment.ResultHandlingRecord{}, false, payment.ErrUnavailable
	}
	return record, true, nil
}

func scanResultHandlingRecord(row pgx.Row) (payment.ResultHandlingRecord, error) {
	var (
		idText        string
		orderIDText   string
		requestIDText string
		sourceText    string
		observedAt    time.Time
		evidenceRaw   []byte
		beforeText    string
		afterText     string
		decisionText  string
		reasonCode    *string
		reasonMessage *string
	)
	if err := row.Scan(
		&idText,
		&orderIDText,
		&requestIDText,
		&sourceText,
		&observedAt,
		&evidenceRaw,
		&beforeText,
		&afterText,
		&decisionText,
		&reasonCode,
		&reasonMessage,
	); err != nil {
		return payment.ResultHandlingRecord{}, err
	}

	evidence, err := decodeEvidence(evidenceRaw)
	if err != nil {
		return payment.ResultHandlingRecord{}, err
	}

	return payment.ResultHandlingRecord{
		ID:             payment.ResultHandlingRecordID(idText),
		PaymentOrderID: payment.PaymentOrderID(orderIDText),
		RequestID:      payment.RequestID(requestIDText),
		Source:         payment.Source(sourceText),
		ObservedAt:     observedAt.UTC(),
		Evidence:       evidence,
		ResultBefore:   payment.Result(beforeText),
		ResultAfter:    payment.Result(afterText),
		Decision:       payment.HandlingDecision(decisionText),
		ReasonCode:     reasonCode,
		ReasonMessage:  reasonMessage,
	}, nil
}

func decodeEvidence(raw []byte) (payment.Evidence, error) {
	var object map[string]json.RawMessage
	if err := json.Unmarshal(raw, &object); err != nil || object == nil {
		return payment.Evidence{}, errStoredEvidence
	}
	for key := range object {
		if _, ok := allowedEvidenceJSONFields[key]; !ok {
			return payment.Evidence{}, errStoredEvidence
		}
	}
	var evidence payment.Evidence
	if err := json.Unmarshal(raw, &evidence); err != nil {
		return payment.Evidence{}, errStoredEvidence
	}
	return evidence, nil
}

// observationMatchesRecord verifies the same-ID idempotency semantics without
// requiring current state to equal the historical record's after-state. It
// rebuilds the order state that existed before the historical save from
// record.ResultBefore, clears confirmation facts for an UNKNOWN history, uses
// the immutable confirmed facts for a confirmed history, and re-runs the pure
// decision function against the fixed observation.
func observationMatchesRecord(observation payment.Observation, record payment.ResultHandlingRecord, currentOrder payment.PaymentOrder) (bool, error) {
	if record.ID != observation.ID ||
		record.PaymentOrderID != observation.PaymentOrderID ||
		record.RequestID != observation.RequestID ||
		record.Source != observation.Source {
		return false, nil
	}
	if !record.ObservedAt.UTC().Truncate(time.Microsecond).Equal(observation.ObservedAt.UTC().Truncate(time.Microsecond)) {
		return false, nil
	}

	historical := currentOrder
	historical.Result = record.ResultBefore
	if record.ResultBefore == payment.ResultUnknown {
		historical.ConfirmedAt = nil
		historical.MockTransactionID = nil
		// A historical KEEP_UNKNOWN or UNKNOWN-after REJECT_EVIDENCE record may
		// later be followed by a legitimate confirmation; that same-ID retry
		// still reads the current reliable facts. A historical acceptance, or
		// any record whose after-state is already confirmed, must instead agree
		// with the current confirmation and its known transaction basis.
		if record.ResultAfter != payment.ResultUnknown || record.Decision == payment.DecisionAcceptConfirmation {
			if !confirmedHistoryMatchesCurrent(record, currentOrder) {
				return false, nil
			}
		}
	} else {
		if currentOrder.Result != record.ResultBefore {
			return false, nil
		}
		historical.ConfirmedAt = cloneTimePointer(currentOrder.ConfirmedAt)
		historical.MockTransactionID = cloneMockIDPointer(currentOrder.MockTransactionID)
	}

	_, expectedRecord, err := payment.DecideObservation(historical, observation, record.ObservedAt.UTC())
	if err != nil {
		return false, err
	}

	if expectedRecord.Decision != record.Decision ||
		expectedRecord.ResultBefore != record.ResultBefore ||
		expectedRecord.ResultAfter != record.ResultAfter {
		return false, nil
	}
	if !evidenceEqual(expectedRecord.Evidence, record.Evidence) {
		return false, nil
	}
	if !stringPointerEqual(expectedRecord.ReasonCode, record.ReasonCode) ||
		!stringPointerEqual(expectedRecord.ReasonMessage, record.ReasonMessage) {
		return false, nil
	}
	return true, nil
}

// confirmedHistoryMatchesCurrent verifies that a historical record whose
// after-state was already confirmed still describes the current order
// confirmation. It rejects an opposite current result, an unusable
// confirmation time, and a missing or conflicting known Mock association,
// while allowing historical evidence fields that were not obtained to remain
// absent.
func confirmedHistoryMatchesCurrent(record payment.ResultHandlingRecord, current payment.PaymentOrder) bool {
	if current.Result != record.ResultAfter {
		return false
	}
	if payment.ValidateResultTime(current.Result, current.ConfirmedAt) != nil {
		return false
	}
	if current.MockTransactionID != nil && payment.ValidateUUIDv4(string(*current.MockTransactionID)) != nil {
		return false
	}
	if record.Evidence.PlatformReferenceID != nil && *record.Evidence.PlatformReferenceID != current.ID {
		return false
	}
	if record.Evidence.Amount != nil && *record.Evidence.Amount != current.Amount {
		return false
	}
	if record.Evidence.Currency != nil && *record.Evidence.Currency != current.Currency {
		return false
	}
	if record.Evidence.Result != nil && *record.Evidence.Result != current.Result {
		return false
	}
	if record.Evidence.MockTransactionID != nil {
		if current.MockTransactionID == nil ||
			*record.Evidence.MockTransactionID != *current.MockTransactionID {
			return false
		}
	}
	return true
}

func updateOrderConfirmation(ctx context.Context, tx pgx.Tx, locked, updated payment.PaymentOrder) error {
	var confirmedAt *time.Time
	if updated.ConfirmedAt != nil {
		at := updated.ConfirmedAt.UTC()
		confirmedAt = &at
	}
	var mockID *string
	if updated.MockTransactionID != nil {
		id := string(*updated.MockTransactionID)
		mockID = &id
	}
	tag, err := tx.Exec(ctx, updateOrderConfirmationSQL,
		string(locked.ID),
		locked.ClientID,
		string(updated.Result),
		confirmedAt,
		mockID,
		string(locked.Result),
	)
	if err != nil {
		return classifySaveError(err)
	}
	if tag.RowsAffected() != 1 {
		return payment.ErrUnavailable
	}
	return nil
}

func insertResultHandlingRecord(ctx context.Context, tx pgx.Tx, record payment.ResultHandlingRecord) error {
	evidence, err := json.Marshal(record.Evidence)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, appendResultHandlingRecordSQL,
		string(record.ID),
		string(record.PaymentOrderID),
		string(record.RequestID),
		string(record.Source),
		record.ObservedAt.UTC(),
		string(evidence),
		string(record.ResultBefore),
		string(record.ResultAfter),
		string(record.Decision),
		record.ReasonCode,
		record.ReasonMessage,
	)
	return err
}

func isRecordPrimaryKeyConflict(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) &&
		pgErr.Code == "23505" &&
		pgErr.ConstraintName == "result_handling_records_pkey"
}

func classifySaveError(err error) error {
	if err == nil {
		return nil
	}
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		if pgErr.Code == "23505" {
			return payment.ErrConflict
		}
		return payment.ErrUnavailable
	}
	return payment.ErrUnavailable
}

func cloneObservation(in payment.Observation) payment.Observation {
	out := in
	out.Evidence = cloneEvidence(in.Evidence)
	if in.Reason != nil {
		reason := *in.Reason
		if in.Reason.Message != nil {
			message := *in.Reason.Message
			reason.Message = &message
		}
		out.Reason = &reason
	}
	if in.Candidate != nil {
		candidate := *in.Candidate
		candidate.MockTransactionID = cloneMockIDPointer(in.Candidate.MockTransactionID)
		out.Candidate = &candidate
	}
	return out
}

func cloneEvidence(in payment.Evidence) payment.Evidence {
	out := in
	if in.PlatformReferenceID != nil {
		value := *in.PlatformReferenceID
		out.PlatformReferenceID = &value
	}
	if in.MockTransactionID != nil {
		value := *in.MockTransactionID
		out.MockTransactionID = &value
	}
	if in.Amount != nil {
		value := *in.Amount
		out.Amount = &value
	}
	if in.Currency != nil {
		value := *in.Currency
		out.Currency = &value
	}
	if in.Result != nil {
		value := *in.Result
		out.Result = &value
	}
	if in.Differences != nil {
		out.Differences = append([]payment.EvidenceFieldName(nil), in.Differences...)
	}
	return out
}

func cloneTimePointer(in *time.Time) *time.Time {
	if in == nil {
		return nil
	}
	value := *in
	return &value
}

func cloneMockIDPointer(in *payment.MockTransactionID) *payment.MockTransactionID {
	if in == nil {
		return nil
	}
	value := *in
	return &value
}

func stringPointerEqual(a, b *string) bool {
	if a == nil || b == nil {
		return a == b
	}
	return *a == *b
}

func evidenceEqual(a, b payment.Evidence) bool {
	if !paymentOrderIDPointerEqual(a.PlatformReferenceID, b.PlatformReferenceID) ||
		!mockTransactionIDPointerEqual(a.MockTransactionID, b.MockTransactionID) ||
		!int64PointerEqual(a.Amount, b.Amount) ||
		!stringPointerEqual(a.Currency, b.Currency) ||
		!resultPointerEqual(a.Result, b.Result) {
		return false
	}
	if len(a.Differences) != len(b.Differences) {
		return false
	}
	for i := range a.Differences {
		if a.Differences[i] != b.Differences[i] {
			return false
		}
	}
	return true
}

func paymentOrderIDPointerEqual(a, b *payment.PaymentOrderID) bool {
	if a == nil || b == nil {
		return a == b
	}
	return *a == *b
}

func mockTransactionIDPointerEqual(a, b *payment.MockTransactionID) bool {
	if a == nil || b == nil {
		return a == b
	}
	return *a == *b
}

func int64PointerEqual(a, b *int64) bool {
	if a == nil || b == nil {
		return a == b
	}
	return *a == *b
}

func resultPointerEqual(a, b *payment.Result) bool {
	if a == nil || b == nil {
		return a == b
	}
	return *a == *b
}
