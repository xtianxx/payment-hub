//go:build integration

package postgres_test

// These tests are the first-stage T024/T032 real-PostgreSQL target tests for
// the US1 core submission lane. They are deliberately written before the SQL
// implementation: the production files currently contain only a safe,
// panic-free ErrUnavailable outline. A fresh isolated PostgreSQL run is
// expected to fail these roots at their first behavioral assertion; that red
// run is the fixture/contract check, not evidence that the implementation is
// complete.
//
// The tests never use an outer rollback transaction. Each root opens two
// independent committed pools, creates its own client/business/order IDs, and
// removes only its own rows in result_handling_records then payment_orders
// order. No table-wide TRUNCATE/DELETE is used.

import (
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"payment-hub/internal/payment"
	"payment-hub/internal/postgres"
	"payment-hub/tests/harness"
)

const (
	committedTestBudget        = 5 * time.Second
	committedTestCleanupBudget = 3 * time.Second
)

var (
	errCommittedTestRecordInsert   = errors.New("committed test record insert fault")
	errCommittedTestCommitUnknown  = errors.New("committed test commit reply unknown")
	errCommittedTestCommitRollback = errors.New("committed test commit rollback fault")
)

var (
	_ postgres.DB                 = (*pgxpool.Pool)(nil)
	_ postgres.DB                 = (*committedTestDB)(nil)
	_ payment.OrderRegistrar      = (*postgres.Store)(nil)
	_ payment.ObservationRecorder = (*postgres.Store)(nil)
)

func TestOrderRegistrationIntegration(t *testing.T) {
	observer := harness.OpenCommittedPostgresPool(t)
	writer := harness.OpenCommittedPostgresPool(t)

	t.Run("new registration is explicit unknown and committed", func(t *testing.T) {
		clientID := committedTestNewClientID()
		businessOrderNo := committedTestNewBusinessOrderNo()
		committedTestCleanup(t, observer, clientID)

		db := committedTestNewDB(writer)
		store := postgres.NewStore(db, postgres.NewHooks(nil))
		input := payment.RegisterOrderInput{
			ClientID:        clientID,
			BusinessOrderNo: businessOrderNo,
			Amount:          1234,
			Currency:        "CNY",
		}

		ctx, cancel := committedTestContext(t)
		result, err := store.RegisterOrder(ctx, input)
		cancel()
		if err != nil {
			t.Fatalf("registration failed: %s", committedTestErrorClass(err))
		}
		if result.Outcome != payment.RegisterOutcomeCreated {
			t.Fatalf("new registration did not return the created outcome")
		}
		if db.beginCalls.Load() == 0 || db.commitCalls.Load() == 0 {
			t.Fatalf("registration did not use a committed short transaction")
		}

		if result.Order.ID == "" {
			t.Fatalf("new registration returned no payment order ID")
		}
		if err := payment.ValidateUUIDv4(string(result.Order.ID)); err != nil {
			t.Fatalf("new registration returned a non-UUIDv4 payment order ID")
		}
		if result.Order.ClientID != clientID || result.Order.BusinessOrderNo != businessOrderNo {
			t.Fatalf("new registration changed the business identity")
		}
		if result.Order.Amount != input.Amount || result.Order.Currency != input.Currency {
			t.Fatalf("new registration changed the original amount or currency")
		}
		if result.Order.Result != payment.ResultUnknown {
			t.Fatalf("new registration did not explicitly store UNKNOWN")
		}
		if result.Order.MockTransactionID != nil {
			t.Fatalf("new UNKNOWN registration invented a mock transaction ID")
		}
		if result.Order.ConfirmedAt != nil {
			t.Fatalf("new UNKNOWN registration invented a confirmation time")
		}
		if result.Order.RegisteredAt.IsZero() {
			t.Fatalf("new registration returned no registration time")
		}
		if _, offset := result.Order.RegisteredAt.Zone(); offset != 0 {
			t.Fatalf("new registration returned a non-UTC registration time")
		}

		row, ok := committedTestReadOrder(t, observer, result.Order.ID)
		if !ok {
			t.Fatalf("committed registration row is not visible through the independent pool")
		}
		committedTestRequireUnknownOrderRow(t, row, input, result.Order.RegisteredAt)
	})

	t.Run("business unique conflict reuses the original and is scoped by client", func(t *testing.T) {
		clientA := committedTestNewClientID()
		clientB := committedTestNewClientID()
		businessOrderNo := committedTestNewBusinessOrderNo()
		committedTestCleanup(t, observer, clientA)
		committedTestCleanup(t, observer, clientB)

		db := committedTestNewDB(writer)
		store := postgres.NewStore(db, postgres.NewHooks(nil))
		register := func(clientID string, amount int64) payment.RegisterOrderResult {
			t.Helper()
			ctx, cancel := committedTestContext(t)
			defer cancel()
			result, err := store.RegisterOrder(ctx, payment.RegisterOrderInput{
				ClientID:        clientID,
				BusinessOrderNo: businessOrderNo,
				Amount:          amount,
				Currency:        "CNY",
			})
			if err != nil {
				t.Fatalf("registration failed: %s", committedTestErrorClass(err))
			}
			return result
		}

		first := register(clientA, 1000)
		if first.Outcome != payment.RegisterOutcomeCreated {
			t.Fatalf("first business-key registration did not create the order")
		}
		if first.Order.Result != payment.ResultUnknown || first.Order.ConfirmedAt != nil || first.Order.MockTransactionID != nil {
			t.Fatalf("first business-key registration did not start explicitly UNKNOWN")
		}

		reused := register(clientA, 2000)
		if reused.Outcome != payment.RegisterOutcomeReused {
			t.Fatalf("same-client business-key conflict did not return the reused outcome")
		}
		if reused.Order.ID != first.Order.ID {
			t.Fatalf("same-client business-key conflict returned a different order")
		}
		if reused.Order.Amount != 1000 || reused.Order.Currency != "CNY" {
			t.Fatalf("same-client business-key conflict changed the original amount or currency")
		}
		if !reused.Order.RegisteredAt.UTC().Truncate(time.Microsecond).Equal(first.Order.RegisteredAt.UTC().Truncate(time.Microsecond)) {
			t.Fatalf("same-client business-key conflict changed the original registration time")
		}

		otherClient := register(clientB, 3000)
		if otherClient.Outcome != payment.RegisterOutcomeCreated {
			t.Fatalf("same business order number under a different client was not a new order")
		}
		if otherClient.Order.ID == first.Order.ID {
			t.Fatalf("same business order number under a different client reused the other client's order")
		}
		if otherClient.Order.ClientID != clientB || otherClient.Order.BusinessOrderNo != businessOrderNo {
			t.Fatalf("different-client registration changed the business identity")
		}

		otherReused := register(clientB, 4000)
		if otherReused.Outcome != payment.RegisterOutcomeReused || otherReused.Order.ID != otherClient.Order.ID {
			t.Fatalf("different-client business-key conflict did not reuse its own order")
		}
		if otherReused.Order.Amount != 3000 {
			t.Fatalf("different-client business-key conflict changed its original amount")
		}

		if got := committedTestCountOrdersByBusinessKey(t, observer, clientA, businessOrderNo); got != 1 {
			t.Fatalf("same-client business key produced %d orders, want 1", got)
		}
		if got := committedTestCountOrdersByBusinessKey(t, observer, clientB, businessOrderNo); got != 1 {
			t.Fatalf("different-client business key produced %d orders, want 1", got)
		}
		rowA, ok := committedTestReadOrder(t, observer, first.Order.ID)
		if !ok || rowA.Amount != 1000 || rowA.Currency != "CNY" {
			t.Fatalf("original order facts changed after a business-key conflict")
		}
		rowB, ok := committedTestReadOrder(t, observer, otherClient.Order.ID)
		if !ok || rowB.Amount != 3000 || rowB.Currency != "CNY" {
			t.Fatalf("different-client order facts changed after a business-key conflict")
		}
	})

	t.Run("non-business unique error is not treated as reuse", func(t *testing.T) {
		clientID := committedTestNewClientID()
		businessOrderNo := committedTestNewBusinessOrderNo()
		committedTestCleanup(t, observer, clientID)

		db := committedTestNewDB(writer)
		db.failOrderInsertUnique = true
		db.orderInsertUniqueErr = &pgconn.PgError{
			Code:           "23505",
			ConstraintName: "payment_orders_pkey",
			Message:        "duplicate key value violates unique constraint",
		}
		store := postgres.NewStore(db, postgres.NewHooks(nil))

		ctx, cancel := committedTestContext(t)
		result, err := store.RegisterOrder(ctx, payment.RegisterOrderInput{
			ClientID:        clientID,
			BusinessOrderNo: businessOrderNo,
			Amount:          1000,
			Currency:        "CNY",
		})
		cancel()
		committedTestRequireSafeError(t, err, "non-business unique error")
		if !errors.Is(err, payment.ErrRegistrationRejected) {
			t.Fatalf("non-business unique error was not classified as registration rejected")
		}
		if errors.Is(err, payment.ErrConflict) {
			t.Fatalf("non-business unique error was misclassified as an intent conflict")
		}
		if result.Outcome == payment.RegisterOutcomeCreated || result.Outcome == payment.RegisterOutcomeReused {
			t.Fatalf("non-business unique error was reported as a reliable registration outcome")
		}
		if !db.sawOrderInsert.Load() {
			t.Fatalf("registration did not attempt the payment order insert")
		}
		if db.beginCalls.Load() == 0 {
			t.Fatalf("registration did not begin its short transaction")
		}
		if got := committedTestCountOrdersByBusinessKey(t, observer, clientID, businessOrderNo); got != 0 {
			t.Fatalf("non-business unique error left %d order rows, want 0", got)
		}
	})

	t.Run("commit outcome unknown does not grant created eligibility", func(t *testing.T) {
		clientID := committedTestNewClientID()
		businessOrderNo := committedTestNewBusinessOrderNo()
		committedTestCleanup(t, observer, clientID)

		db := committedTestNewDB(writer)
		db.commitUnknown = true
		store := postgres.NewStore(db, postgres.NewHooks(nil))

		ctx, cancel := committedTestContext(t)
		result, err := store.RegisterOrder(ctx, payment.RegisterOrderInput{
			ClientID:        clientID,
			BusinessOrderNo: businessOrderNo,
			Amount:          1500,
			Currency:        "CNY",
		})
		cancel()
		if err == nil {
			t.Fatalf("commit outcome unknown was reported as reliable success")
		}
		if !errors.Is(err, payment.ErrIndeterminate) {
			t.Fatalf("commit outcome unknown returned %s, want the indeterminate sentinel", committedTestErrorClass(err))
		}
		if result.Outcome == payment.RegisterOutcomeCreated {
			t.Fatalf("commit outcome unknown granted created eligibility")
		}
		if result.Outcome == payment.RegisterOutcomeReused {
			t.Fatalf("commit outcome unknown was reported as reuse")
		}
		if db.beginCalls.Load() == 0 || db.commitCalls.Load() == 0 {
			t.Fatalf("registration did not reach its commit point")
		}

		row, ok := committedTestReadOrderByBusinessKey(t, observer, clientID, businessOrderNo)
		if !ok {
			t.Fatalf("committed row is not visible after a reply-unknown commit")
		}
		if row.Result != string(payment.ResultUnknown) || row.ConfirmedAt != nil || row.MockTransactionID != nil {
			t.Fatalf("reply-unknown registration did not leave an explicit UNKNOWN row")
		}
		if row.Amount != 1500 || row.Currency != "CNY" {
			t.Fatalf("reply-unknown registration changed the original amount or currency")
		}
	})
}

func TestObservationAtomicSaveIntegration(t *testing.T) {
	observer := harness.OpenCommittedPostgresPool(t)
	writer := harness.OpenCommittedPostgresPool(t)

	t.Run("accepts matching confirmation and commits order with minimal evidence", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		order := committedTestNewUnknownOrder(clientID, committedTestNewBusinessOrderNo(), 1234)
		committedTestInsertOrder(t, observer, order)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		observation := committedTestAcceptedObservation(order, &mockID)

		db := committedTestNewDB(writer)
		store := postgres.NewStore(db, postgres.NewHooks(nil))
		ctx, cancel := committedTestContext(t)
		result, err := store.SaveObservation(ctx, observation)
		cancel()
		if err != nil {
			t.Fatalf("observation save failed: %s", committedTestErrorClass(err))
		}
		if db.beginCalls.Load() == 0 || db.commitCalls.Load() == 0 {
			t.Fatalf("observation save did not use a committed short transaction")
		}
		if !db.sawOrderUpdate.Load() {
			t.Fatalf("accepted save did not update the order")
		}
		if !db.sawRecordInsert.Load() {
			t.Fatalf("accepted save did not append a handling record")
		}

		committedTestRequireSavedAccepted(t, result, observation, order, mockID, true)

		row, ok := committedTestReadOrder(t, observer, order.ID)
		if !ok {
			t.Fatalf("saved order is not visible through the independent pool")
		}
		if row.Result != string(payment.ResultSucceeded) {
			t.Fatalf("committed order result is not SUCCEEDED")
		}
		if row.ConfirmedAt == nil {
			t.Fatalf("committed confirmed order has no confirmation time")
		}
		if row.MockTransactionID == nil || *row.MockTransactionID != string(mockID) {
			t.Fatalf("committed order did not store the verified mock transaction ID")
		}
		if row.Amount != order.Amount || row.Currency != order.Currency {
			t.Fatalf("committed save changed the original amount or currency")
		}
		if !row.RegisteredAt.Equal(order.RegisteredAt) {
			t.Fatalf("committed save changed the original registration time")
		}
		if result.Order.ConfirmedAt == nil || !row.ConfirmedAt.Equal(result.Order.ConfirmedAt.UTC().Truncate(time.Microsecond)) {
			t.Fatalf("returned confirmation time does not match the committed row")
		}

		recordRow, ok := committedTestReadRecord(t, observer, observation.ID)
		if !ok {
			t.Fatalf("handling record is not visible through the independent pool")
		}
		evidenceObject := committedTestEvidenceObject(t, recordRow.Evidence)
		for _, key := range []string{"platform_reference_id", "mock_transaction_id", "amount", "currency", "result"} {
			if _, ok := evidenceObject[key]; !ok {
				t.Fatalf("accepted evidence is missing the %q field", key)
			}
		}
		record := committedTestRecordFromRow(t, recordRow)
		committedTestRequireRecordIdentity(t, record, observation, order)
		if record.Decision != payment.DecisionAcceptConfirmation {
			t.Fatalf("committed handling decision is not ACCEPT_CONFIRMATION")
		}
		if record.ResultBefore != payment.ResultUnknown || record.ResultAfter != payment.ResultSucceeded {
			t.Fatalf("committed handling record did not derive UNKNOWN -> SUCCEEDED")
		}
		if !reflect.DeepEqual(record.Evidence, observation.Evidence) {
			t.Fatalf("committed handling evidence differs from the fixed observation evidence")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 1 {
			t.Fatalf("accepted save produced %d handling records, want 1", got)
		}
	})

	t.Run("accepts minimal evidence without inventing a mock transaction id", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		order := committedTestNewUnknownOrder(clientID, committedTestNewBusinessOrderNo(), 1400)
		committedTestInsertOrder(t, observer, order)
		observation := committedTestAcceptedObservation(order, nil)

		db := committedTestNewDB(writer)
		store := postgres.NewStore(db, postgres.NewHooks(nil))
		ctx, cancel := committedTestContext(t)
		result, err := store.SaveObservation(ctx, observation)
		cancel()
		if err != nil {
			t.Fatalf("minimal-evidence observation save failed: %s", committedTestErrorClass(err))
		}
		if result.Order.Result != payment.ResultSucceeded {
			t.Fatalf("minimal-evidence confirmation did not store SUCCEEDED")
		}
		if result.Order.MockTransactionID != nil {
			t.Fatalf("minimal-evidence confirmation invented a mock transaction ID")
		}
		if result.Order.ConfirmedAt == nil {
			t.Fatalf("minimal-evidence confirmation did not store a confirmation time")
		}
		if result.Record.Decision != payment.DecisionAcceptConfirmation {
			t.Fatalf("minimal-evidence confirmation did not record ACCEPT_CONFIRMATION")
		}
		if result.Record.Evidence.MockTransactionID != nil {
			t.Fatalf("minimal-evidence record invented a mock transaction ID")
		}

		recordRow, ok := committedTestReadRecord(t, observer, observation.ID)
		if !ok {
			t.Fatalf("minimal-evidence handling record is not visible")
		}
		evidenceObject := committedTestEvidenceObject(t, recordRow.Evidence)
		if _, ok := evidenceObject["mock_transaction_id"]; ok {
			t.Fatalf("minimal-evidence record persisted an invented mock transaction ID")
		}
		for _, key := range []string{"platform_reference_id", "amount", "currency", "result"} {
			if _, ok := evidenceObject[key]; !ok {
				t.Fatalf("minimal-evidence record is missing the %q field", key)
			}
		}
		row, ok := committedTestReadOrder(t, observer, order.ID)
		if !ok || row.Result != string(payment.ResultSucceeded) || row.ConfirmedAt == nil || row.MockTransactionID != nil {
			t.Fatalf("minimal-evidence confirmation did not commit the expected order state")
		}
		record := committedTestRecordFromRow(t, recordRow)
		committedTestRequireRecordIdentity(t, record, observation, order)
		if got := committedTestCountRecords(t, observer, order.ID); got != 1 {
			t.Fatalf("minimal-evidence save produced %d handling records, want 1", got)
		}
	})

	t.Run("keeps unknown and appends a handling record", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		order := committedTestNewUnknownOrder(clientID, committedTestNewBusinessOrderNo(), 1000)
		committedTestInsertOrder(t, observer, order)
		observation := committedTestKeepUnknownObservation(order)

		db := committedTestNewDB(writer)
		store := postgres.NewStore(db, postgres.NewHooks(nil))
		ctx, cancel := committedTestContext(t)
		result, err := store.SaveObservation(ctx, observation)
		cancel()
		if err != nil {
			t.Fatalf("keep-unknown save failed: %s", committedTestErrorClass(err))
		}
		if !db.sawRecordInsert.Load() {
			t.Fatalf("keep-unknown save did not append a handling record")
		}
		if result.Order.Result != payment.ResultUnknown || result.Order.ConfirmedAt != nil || result.Order.MockTransactionID != nil {
			t.Fatalf("keep-unknown save changed the order result or confirmation facts")
		}
		if result.Record.Decision != payment.DecisionKeepUnknown {
			t.Fatalf("keep-unknown save did not record KEEP_UNKNOWN")
		}
		if result.Record.ResultBefore != payment.ResultUnknown || result.Record.ResultAfter != payment.ResultUnknown {
			t.Fatalf("keep-unknown record did not preserve UNKNOWN before and after")
		}
		if result.Record.ReasonCode == nil || *result.Record.ReasonCode != "NO_MATCH" {
			t.Fatalf("keep-unknown record did not preserve the controlled reason code")
		}

		row, ok := committedTestReadOrder(t, observer, order.ID)
		if !ok {
			t.Fatalf("keep-unknown order is not visible")
		}
		if row.Result != string(payment.ResultUnknown) || row.ConfirmedAt != nil || row.MockTransactionID != nil {
			t.Fatalf("keep-unknown save changed the committed order")
		}
		recordRow, ok := committedTestReadRecord(t, observer, observation.ID)
		if !ok {
			t.Fatalf("keep-unknown handling record is not visible")
		}
		record := committedTestRecordFromRow(t, recordRow)
		committedTestRequireRecordIdentity(t, record, observation, order)
		if record.Decision != payment.DecisionKeepUnknown || record.ResultBefore != payment.ResultUnknown || record.ResultAfter != payment.ResultUnknown {
			t.Fatalf("committed keep-unknown record has the wrong decision or before/after state")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 1 {
			t.Fatalf("keep-unknown save produced %d handling records, want 1", got)
		}
	})

	t.Run("keeps existing confirmation without overwriting it", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		confirmedAt := time.Now().UTC().Add(-time.Minute).Truncate(time.Microsecond)
		order := committedTestNewConfirmedOrder(clientID, committedTestNewBusinessOrderNo(), 2500, payment.ResultSucceeded, mockID, confirmedAt)
		committedTestInsertOrder(t, observer, order)
		observation := committedTestAcceptedObservation(order, &mockID)

		db := committedTestNewDB(writer)
		store := postgres.NewStore(db, postgres.NewHooks(nil))
		ctx, cancel := committedTestContext(t)
		result, err := store.SaveObservation(ctx, observation)
		cancel()
		if err != nil {
			t.Fatalf("keep-confirmed save failed: %s", committedTestErrorClass(err))
		}
		if result.Order.Result != payment.ResultSucceeded {
			t.Fatalf("keep-confirmed save changed the confirmed result")
		}
		if result.Order.ConfirmedAt == nil || !result.Order.ConfirmedAt.Equal(confirmedAt) {
			t.Fatalf("keep-confirmed save overwrote the original confirmation time")
		}
		if result.Order.MockTransactionID == nil || *result.Order.MockTransactionID != mockID {
			t.Fatalf("keep-confirmed save changed the associated mock transaction ID")
		}
		if result.Record.Decision != payment.DecisionKeepConfirmed {
			t.Fatalf("keep-confirmed save did not record KEEP_CONFIRMED")
		}
		if result.Record.ResultBefore != payment.ResultSucceeded || result.Record.ResultAfter != payment.ResultSucceeded {
			t.Fatalf("keep-confirmed record did not preserve SUCCEEDED before and after")
		}

		row, ok := committedTestReadOrder(t, observer, order.ID)
		if !ok {
			t.Fatalf("keep-confirmed order is not visible")
		}
		if row.Result != string(payment.ResultSucceeded) || row.ConfirmedAt == nil || !row.ConfirmedAt.Equal(confirmedAt) {
			t.Fatalf("keep-confirmed save changed the committed confirmation")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 1 {
			t.Fatalf("keep-confirmed save produced %d handling records, want 1", got)
		}
	})

	t.Run("rejects conflicting evidence without overwriting a confirmation", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		confirmedAt := time.Now().UTC().Add(-time.Minute).Truncate(time.Microsecond)
		order := committedTestNewConfirmedOrder(clientID, committedTestNewBusinessOrderNo(), 1800, payment.ResultSucceeded, mockID, confirmedAt)
		committedTestInsertOrder(t, observer, order)
		observation := committedTestRejectedObservation(order)

		db := committedTestNewDB(writer)
		store := postgres.NewStore(db, postgres.NewHooks(nil))
		ctx, cancel := committedTestContext(t)
		result, err := store.SaveObservation(ctx, observation)
		cancel()
		if err != nil {
			t.Fatalf("rejected-evidence save failed to persist its record: %s", committedTestErrorClass(err))
		}
		if result.Order.Result != payment.ResultSucceeded {
			t.Fatalf("rejected-evidence save overwrote the confirmed result")
		}
		if result.Order.ConfirmedAt == nil || !result.Order.ConfirmedAt.Equal(confirmedAt) {
			t.Fatalf("rejected-evidence save overwrote the confirmation time")
		}
		if result.Record.Decision != payment.DecisionRejectEvidence {
			t.Fatalf("rejected-evidence save did not record REJECT_EVIDENCE")
		}
		if result.Record.ResultBefore != payment.ResultSucceeded || result.Record.ResultAfter != payment.ResultSucceeded {
			t.Fatalf("rejected-evidence record did not preserve SUCCEEDED before and after")
		}
		if result.Record.ReasonCode == nil || *result.Record.ReasonCode != "EVIDENCE_MISMATCH" {
			t.Fatalf("rejected-evidence record did not preserve the controlled reason code")
		}
		if !reflect.DeepEqual(result.Record.Evidence.Differences, []payment.EvidenceFieldName{payment.EvidenceFieldAmount}) {
			t.Fatalf("rejected-evidence record did not preserve the controlled differences")
		}

		row, ok := committedTestReadOrder(t, observer, order.ID)
		if !ok || row.Result != string(payment.ResultSucceeded) || row.ConfirmedAt == nil || !row.ConfirmedAt.Equal(confirmedAt) {
			t.Fatalf("rejected-evidence save changed the committed confirmation")
		}
		recordRow, ok := committedTestReadRecord(t, observer, observation.ID)
		if !ok {
			t.Fatalf("rejected-evidence handling record is not visible")
		}
		committedTestEvidenceObject(t, recordRow.Evidence)
		record := committedTestRecordFromRow(t, recordRow)
		committedTestRequireRecordIdentity(t, record, observation, order)
		if record.Decision != payment.DecisionRejectEvidence {
			t.Fatalf("committed rejected-evidence record has the wrong decision")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 1 {
			t.Fatalf("rejected-evidence save produced %d handling records, want 1", got)
		}
	})

	t.Run("derives the decision from the locked current state", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		order := committedTestNewUnknownOrder(clientID, committedTestNewBusinessOrderNo(), 1000)
		committedTestInsertOrder(t, observer, order)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		observation := committedTestAcceptedObservation(order, &mockID)

		// A different committed save confirms the order after the observation
		// was fixed but before this save acquires the row lock.
		externalConfirmedAt := time.Now().UTC().Truncate(time.Microsecond)
		committedTestConfirmOrder(t, observer, order.ID, payment.ResultSucceeded, mockID, externalConfirmedAt)

		db := committedTestNewDB(writer)
		store := postgres.NewStore(db, postgres.NewHooks(nil))
		ctx, cancel := committedTestContext(t)
		result, err := store.SaveObservation(ctx, observation)
		cancel()
		if err != nil {
			t.Fatalf("locked-current-state save failed: %s", committedTestErrorClass(err))
		}
		if result.Order.Result != payment.ResultSucceeded {
			t.Fatalf("locked-current-state save did not return the current confirmed result")
		}
		if result.Order.ConfirmedAt == nil || !result.Order.ConfirmedAt.Equal(externalConfirmedAt) {
			t.Fatalf("locked-current-state save changed the already committed confirmation time")
		}
		if result.Record.Decision != payment.DecisionKeepConfirmed {
			t.Fatalf("locked-current-state save did not derive KEEP_CONFIRMED")
		}
		if result.Record.ResultBefore != payment.ResultSucceeded || result.Record.ResultAfter != payment.ResultSucceeded {
			t.Fatalf("locked-current-state record did not use the locked current state")
		}

		recordRow, ok := committedTestReadRecord(t, observer, observation.ID)
		if !ok {
			t.Fatalf("locked-current-state handling record is not visible")
		}
		committedTestEvidenceObject(t, recordRow.Evidence)
		record := committedTestRecordFromRow(t, recordRow)
		committedTestRequireRecordIdentity(t, record, observation, order)
		if record.ResultBefore != payment.ResultSucceeded || record.ResultAfter != payment.ResultSucceeded {
			t.Fatalf("committed record copied a stale pre-lock UNKNOWN snapshot")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 1 {
			t.Fatalf("locked-current-state save produced %d handling records, want 1", got)
		}
	})

	t.Run("same record id retry reuses the decision and rejects a different meaning", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		order := committedTestNewUnknownOrder(clientID, committedTestNewBusinessOrderNo(), 1500)
		committedTestInsertOrder(t, observer, order)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		observation := committedTestAcceptedObservation(order, &mockID)

		db := committedTestNewDB(writer)
		store := postgres.NewStore(db, postgres.NewHooks(nil))
		ctx, cancel := committedTestContext(t)
		first, err := store.SaveObservation(ctx, observation)
		cancel()
		if err != nil {
			t.Fatalf("first observation save failed: %s", committedTestErrorClass(err))
		}
		if first.Record.Decision != payment.DecisionAcceptConfirmation {
			t.Fatalf("first observation save did not accept the confirmation")
		}

		ctx, cancel = committedTestContext(t)
		second, err := store.SaveObservation(ctx, observation)
		cancel()
		if err != nil {
			t.Fatalf("identical retry failed: %s", committedTestErrorClass(err))
		}
		if second.Record.ID != observation.ID {
			t.Fatalf("identical retry did not reuse the existing record ID")
		}
		if second.Record.Decision != payment.DecisionAcceptConfirmation {
			t.Fatalf("identical retry did not reuse the existing handling decision")
		}
		if second.Record.ResultBefore != payment.ResultUnknown || second.Record.ResultAfter != payment.ResultSucceeded {
			t.Fatalf("identical retry rewrote the historical before/after state")
		}
		if second.Order.Result != payment.ResultSucceeded {
			t.Fatalf("identical retry did not return the current order result")
		}
		if first.Order.ConfirmedAt == nil || second.Order.ConfirmedAt == nil ||
			!second.Order.ConfirmedAt.UTC().Truncate(time.Microsecond).Equal(first.Order.ConfirmedAt.UTC().Truncate(time.Microsecond)) {
			t.Fatalf("identical retry changed the committed confirmation time")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 1 {
			t.Fatalf("identical retry produced %d handling records, want 1", got)
		}

		different := observation
		different.RequestID = payment.RequestID(payment.NewUUIDv4())
		ctx, cancel = committedTestContext(t)
		_, err = store.SaveObservation(ctx, different)
		cancel()
		if !errors.Is(err, payment.ErrObservationConflict) {
			t.Fatalf("same record ID with a different meaning returned %s, want the observation-conflict sentinel", committedTestErrorClass(err))
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 1 {
			t.Fatalf("different-meaning retry produced %d handling records, want 1", got)
		}
		row, ok := committedTestReadOrder(t, observer, order.ID)
		if !ok || row.Result != string(payment.ResultSucceeded) {
			t.Fatalf("different-meaning retry changed the committed order")
		}
	})

	t.Run("record insert fault leaves no half-saved confirmation", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		order := committedTestNewUnknownOrder(clientID, committedTestNewBusinessOrderNo(), 1100)
		committedTestInsertOrder(t, observer, order)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		observation := committedTestAcceptedObservation(order, &mockID)

		db := committedTestNewDB(writer)
		db.failRecordInsert = true
		store := postgres.NewStore(db, postgres.NewHooks(nil))
		ctx, cancel := committedTestContext(t)
		_, err := store.SaveObservation(ctx, observation)
		cancel()
		committedTestRequireSafeError(t, err, "record insert fault")
		if !db.sawRecordInsert.Load() {
			t.Fatalf("record insert fault was not reached")
		}
		if db.beginCalls.Load() == 0 {
			t.Fatalf("record insert fault did not run inside a short transaction")
		}

		row, ok := committedTestReadOrder(t, observer, order.ID)
		if !ok {
			t.Fatalf("order disappeared after a record insert fault")
		}
		if row.Result != string(payment.ResultUnknown) || row.ConfirmedAt != nil || row.MockTransactionID != nil {
			t.Fatalf("record insert fault left a half-saved order confirmation")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 0 {
			t.Fatalf("record insert fault left %d handling records, want 0", got)
		}
	})

	t.Run("commit fault rolls back the order update and the handling record", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		order := committedTestNewUnknownOrder(clientID, committedTestNewBusinessOrderNo(), 1200)
		committedTestInsertOrder(t, observer, order)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		observation := committedTestAcceptedObservation(order, &mockID)

		db := committedTestNewDB(writer)
		db.commitRollback = true
		store := postgres.NewStore(db, postgres.NewHooks(nil))
		ctx, cancel := committedTestContext(t)
		_, err := store.SaveObservation(ctx, observation)
		cancel()
		committedTestRequireSafeError(t, err, "commit fault")
		if !db.sawOrderUpdate.Load() || !db.sawRecordInsert.Load() {
			t.Fatalf("commit fault did not follow both writes in the same transaction")
		}
		if db.commitCalls.Load() == 0 {
			t.Fatalf("commit fault did not reach the commit point")
		}

		row, ok := committedTestReadOrder(t, observer, order.ID)
		if !ok {
			t.Fatalf("order disappeared after a commit fault")
		}
		if row.Result != string(payment.ResultUnknown) || row.ConfirmedAt != nil || row.MockTransactionID != nil {
			t.Fatalf("commit fault left a half-saved order confirmation")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 0 {
			t.Fatalf("commit fault left %d handling records, want 0", got)
		}
	})

	t.Run("cannot save an observation outside the order client scope", func(t *testing.T) {
		clientA := committedTestNewClientID()
		clientB := committedTestNewClientID()
		committedTestCleanup(t, observer, clientA)
		committedTestCleanup(t, observer, clientB)
		order := committedTestNewUnknownOrder(clientA, committedTestNewBusinessOrderNo(), 1300)
		committedTestInsertOrder(t, observer, order)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		observation := committedTestAcceptedObservation(order, &mockID)
		observation.ClientID = clientB

		db := committedTestNewDB(writer)
		store := postgres.NewStore(db, postgres.NewHooks(nil))
		ctx, cancel := committedTestContext(t)
		_, err := store.SaveObservation(ctx, observation)
		cancel()
		if !errors.Is(err, payment.ErrNotFound) {
			t.Fatalf("cross-client save returned %s, want the not-found sentinel", committedTestErrorClass(err))
		}
		if db.beginCalls.Load() == 0 && db.queryRowCalls.Load() == 0 {
			t.Fatalf("cross-client save did not consult the database")
		}

		row, ok := committedTestReadOrder(t, observer, order.ID)
		if !ok {
			t.Fatalf("cross-client save removed the original order")
		}
		if row.Result != string(payment.ResultUnknown) || row.ConfirmedAt != nil || row.MockTransactionID != nil {
			t.Fatalf("cross-client save changed the other client's order")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 0 {
			t.Fatalf("cross-client save appended %d handling records, want 0", got)
		}
	})

	t.Run("postcommit reply hiding preserves the fact and retry reuses the record", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		order := committedTestNewUnknownOrder(clientID, committedTestNewBusinessOrderNo(), 1600)
		committedTestInsertOrder(t, observer, order)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		observation := committedTestAcceptedObservation(order, &mockID)

		control := &committedTestControl{resultCommittedErr: postgres.ErrReplyHidden}
		db := committedTestNewDB(writer)
		store := postgres.NewStore(db, postgres.NewHooks(control))
		ctx, cancel := committedTestContext(t)
		_, err := store.SaveObservation(ctx, observation)
		cancel()
		if err == nil {
			t.Fatalf("postcommit reply hiding was reported as ordinary success")
		}
		if errors.Is(err, payment.ErrUnavailable) {
			t.Fatalf("postcommit reply hiding was misreported as a precommit dependency failure")
		}
		if !errors.Is(err, postgres.ErrReplyHidden) && !errors.Is(err, payment.ErrIndeterminate) {
			t.Fatalf("postcommit reply hiding returned %s, want reply-hidden or indeterminate", committedTestErrorClass(err))
		}
		if control.resultCommittedCalls.Load() != 1 {
			t.Fatalf("postcommit reply hiding did not invoke the committed hook exactly once")
		}
		if db.commitCalls.Load() == 0 {
			t.Fatalf("postcommit reply hiding did not reach the commit point")
		}

		row, ok := committedTestReadOrder(t, observer, order.ID)
		if !ok {
			t.Fatalf("postcommit reply hiding removed the committed order")
		}
		if row.Result != string(payment.ResultSucceeded) || row.ConfirmedAt == nil || row.MockTransactionID == nil || *row.MockTransactionID != string(mockID) {
			t.Fatalf("postcommit reply hiding did not preserve the committed confirmation")
		}
		recordRow, ok := committedTestReadRecord(t, observer, observation.ID)
		if !ok {
			t.Fatalf("postcommit reply hiding removed the committed handling record")
		}
		record := committedTestRecordFromRow(t, recordRow)
		committedTestRequireRecordIdentity(t, record, observation, order)
		if record.Decision != payment.DecisionAcceptConfirmation {
			t.Fatalf("postcommit reply hiding changed the committed handling decision")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 1 {
			t.Fatalf("postcommit reply hiding produced %d handling records, want 1", got)
		}

		retryStore := postgres.NewStore(db, postgres.NewHooks(nil))
		ctx, cancel = committedTestContext(t)
		retry, err := retryStore.SaveObservation(ctx, observation)
		cancel()
		if err != nil {
			t.Fatalf("retry after reply hiding failed: %s", committedTestErrorClass(err))
		}
		if retry.Record.ID != observation.ID || retry.Record.Decision != payment.DecisionAcceptConfirmation {
			t.Fatalf("retry after reply hiding did not reuse the committed record")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 1 {
			t.Fatalf("retry after reply hiding produced %d handling records, want 1", got)
		}
	})

	t.Run("precommit saving hook fault leaves the order unknown", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		order := committedTestNewUnknownOrder(clientID, committedTestNewBusinessOrderNo(), 1700)
		committedTestInsertOrder(t, observer, order)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		observation := committedTestAcceptedObservation(order, &mockID)

		control := &committedTestControl{savingErr: errors.New("committed test saving canary")}
		db := committedTestNewDB(writer)
		store := postgres.NewStore(db, postgres.NewHooks(control))
		ctx, cancel := committedTestContext(t)
		_, err := store.SaveObservation(ctx, observation)
		cancel()
		committedTestRequireSafeError(t, err, "precommit saving hook fault")
		if control.savingCalls.Load() != 1 {
			t.Fatalf("precommit saving hook was not invoked exactly once")
		}
		if db.commitCalls.Load() != 0 {
			t.Fatalf("precommit saving hook fault still committed the transaction")
		}

		row, ok := committedTestReadOrder(t, observer, order.ID)
		if !ok {
			t.Fatalf("precommit saving hook fault removed the order")
		}
		if row.Result != string(payment.ResultUnknown) || row.ConfirmedAt != nil || row.MockTransactionID != nil {
			t.Fatalf("precommit saving hook fault left a half-saved confirmation")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 0 {
			t.Fatalf("precommit saving hook fault left %d handling records, want 0", got)
		}
	})
}

// committedTestDB is a narrow wrapper around the real test pool. It counts
// short transactions and lets a test inject a precise SQL or commit fault
// without changing migrations or installing database triggers.
type committedTestDB struct {
	pool *pgxpool.Pool

	beginCalls    atomic.Int32
	commitCalls   atomic.Int32
	rollbackCalls atomic.Int32
	queryRowCalls atomic.Int32

	sawOrderInsert  atomic.Bool
	sawOrderUpdate  atomic.Bool
	sawRecordInsert atomic.Bool

	failOrderInsertUnique bool
	orderInsertUniqueErr  error
	failRecordInsert      bool
	commitUnknown         bool
	commitRollback        bool
}

func committedTestNewDB(pool *pgxpool.Pool) *committedTestDB {
	return &committedTestDB{pool: pool}
}

func (db *committedTestDB) BeginTx(ctx context.Context, opts pgx.TxOptions) (pgx.Tx, error) {
	db.beginCalls.Add(1)
	tx, err := db.pool.BeginTx(ctx, opts)
	if err != nil {
		return nil, err
	}
	return &committedTestTx{Tx: tx, db: db}, nil
}

func (db *committedTestDB) QueryRow(ctx context.Context, sql string, args ...any) pgx.Row {
	db.queryRowCalls.Add(1)
	return db.pool.QueryRow(ctx, sql, args...)
}

type committedTestTx struct {
	pgx.Tx
	db *committedTestDB
}

func (tx *committedTestTx) Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	if err := tx.observeSQL(sql); err != nil {
		return pgconn.CommandTag{}, err
	}
	return tx.Tx.Exec(ctx, sql, args...)
}

func (tx *committedTestTx) QueryRow(ctx context.Context, sql string, args ...any) pgx.Row {
	if err := tx.observeSQL(sql); err != nil {
		return committedTestErrorRow{err: err}
	}
	return tx.Tx.QueryRow(ctx, sql, args...)
}

func (tx *committedTestTx) Commit(ctx context.Context) error {
	tx.db.commitCalls.Add(1)
	switch {
	case tx.db.commitUnknown:
		if err := tx.Tx.Commit(ctx); err != nil {
			return err
		}
		return errCommittedTestCommitUnknown
	case tx.db.commitRollback:
		if err := tx.Tx.Rollback(ctx); err != nil && !errors.Is(err, pgx.ErrTxClosed) {
			return err
		}
		return errCommittedTestCommitRollback
	default:
		return tx.Tx.Commit(ctx)
	}
}

func (tx *committedTestTx) Rollback(ctx context.Context) error {
	tx.db.rollbackCalls.Add(1)
	return tx.Tx.Rollback(ctx)
}

func (tx *committedTestTx) observeSQL(sql string) error {
	normalized := strings.ToLower(sql)
	if strings.Contains(normalized, "insert into payment_orders") {
		tx.db.sawOrderInsert.Store(true)
		if tx.db.failOrderInsertUnique {
			if tx.db.orderInsertUniqueErr != nil {
				return tx.db.orderInsertUniqueErr
			}
			return &pgconn.PgError{
				Code:           "23505",
				ConstraintName: "payment_orders_pkey",
				Message:        "duplicate key value violates unique constraint",
			}
		}
	}
	if strings.Contains(normalized, "update payment_orders") {
		tx.db.sawOrderUpdate.Store(true)
	}
	if strings.Contains(normalized, "insert into result_handling_records") {
		tx.db.sawRecordInsert.Store(true)
		if tx.db.failRecordInsert {
			return errCommittedTestRecordInsert
		}
	}
	return nil
}

type committedTestErrorRow struct {
	err error
}

func (row committedTestErrorRow) Scan(...any) error {
	return row.err
}

type committedTestControl struct {
	registeredErr      error
	savingErr          error
	resultCommittedErr error

	registeredCalls      atomic.Int32
	savingCalls          atomic.Int32
	resultCommittedCalls atomic.Int32
}

func (control *committedTestControl) Registered(context.Context, payment.PaymentOrderID) error {
	control.registeredCalls.Add(1)
	return control.registeredErr
}

func (control *committedTestControl) SavingResult(context.Context, payment.PaymentOrderID) error {
	control.savingCalls.Add(1)
	return control.savingErr
}

func (control *committedTestControl) ResultCommitted(context.Context, payment.PaymentOrderID) error {
	control.resultCommittedCalls.Add(1)
	return control.resultCommittedErr
}

func committedTestNewClientID() string {
	return "pg-" + payment.NewUUIDv4()
}

func committedTestNewBusinessOrderNo() string {
	return "bo-" + payment.NewUUIDv4()
}

func committedTestContext(t *testing.T) (context.Context, context.CancelFunc) {
	t.Helper()
	return context.WithTimeout(context.Background(), committedTestBudget)
}

func committedTestErrorClass(err error) string {
	switch {
	case err == nil:
		return "none"
	case errors.Is(err, payment.ErrInvalidInput):
		return "invalid input"
	case errors.Is(err, payment.ErrConflict):
		return "conflict"
	case errors.Is(err, payment.ErrRegistrationRejected):
		return "registration rejected"
	case errors.Is(err, payment.ErrUnavailable):
		return "unavailable"
	case errors.Is(err, payment.ErrIndeterminate):
		return "indeterminate"
	case errors.Is(err, payment.ErrObservationConflict):
		return "observation conflict"
	case errors.Is(err, payment.ErrNotFound):
		return "not found"
	case errors.Is(err, postgres.ErrReplyHidden):
		return "reply hidden"
	case errors.Is(err, context.DeadlineExceeded):
		return "timeout"
	case errors.Is(err, context.Canceled):
		return "canceled"
	default:
		return "unclassified"
	}
}

func committedTestRequireSafeError(t *testing.T, err error, label string) {
	t.Helper()
	if err == nil {
		t.Fatalf("%s: expected an error", label)
	}
	if class := committedTestErrorClass(err); class == "unclassified" {
		t.Fatalf("%s: error was not classified by a safe sentinel", label)
	}
}

func committedTestSQLState(err error) string {
	if err == nil {
		return "none"
	}
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code
	}
	return "none"
}

func committedTestNewUnknownOrder(clientID, businessOrderNo string, amount int64) payment.PaymentOrder {
	return payment.PaymentOrder{
		ID:              payment.PaymentOrderID(payment.NewUUIDv4()),
		ClientID:        clientID,
		BusinessOrderNo: businessOrderNo,
		Amount:          amount,
		Currency:        "CNY",
		Result:          payment.ResultUnknown,
		RegisteredAt:    time.Now().UTC().Truncate(time.Microsecond),
	}
}

func committedTestNewConfirmedOrder(clientID, businessOrderNo string, amount int64, result payment.Result, mockID payment.MockTransactionID, confirmedAt time.Time) payment.PaymentOrder {
	confirmed := confirmedAt.UTC().Truncate(time.Microsecond)
	return payment.PaymentOrder{
		ID:                payment.PaymentOrderID(payment.NewUUIDv4()),
		ClientID:          clientID,
		BusinessOrderNo:   businessOrderNo,
		Amount:            amount,
		Currency:          "CNY",
		Result:            result,
		MockTransactionID: &mockID,
		RegisteredAt:      time.Now().UTC().Add(-2 * time.Minute).Truncate(time.Microsecond),
		ConfirmedAt:       &confirmed,
	}
}

func committedTestInsertOrder(t *testing.T, pool *pgxpool.Pool, order payment.PaymentOrder) {
	t.Helper()
	ctx, cancel := committedTestContext(t)
	defer cancel()
	var mockID any
	if order.MockTransactionID != nil {
		mockID = string(*order.MockTransactionID)
	}
	var confirmedAt any
	if order.ConfirmedAt != nil {
		confirmedAt = order.ConfirmedAt.UTC()
	}
	_, err := pool.Exec(ctx, `INSERT INTO payment_orders
		(id, client_id, business_order_no, amount, currency, result, mock_transaction_id, registered_at, confirmed_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`,
		string(order.ID), order.ClientID, order.BusinessOrderNo, order.Amount, order.Currency,
		string(order.Result), mockID, order.RegisteredAt.UTC(), confirmedAt,
	)
	if err != nil {
		t.Fatalf("fixture order insert failed (SQLSTATE %s)", committedTestSQLState(err))
	}
}

func committedTestConfirmOrder(t *testing.T, pool *pgxpool.Pool, orderID payment.PaymentOrderID, result payment.Result, mockID payment.MockTransactionID, confirmedAt time.Time) {
	t.Helper()
	ctx, cancel := committedTestContext(t)
	defer cancel()
	_, err := pool.Exec(ctx, `UPDATE payment_orders
		SET result = $2, mock_transaction_id = $3, confirmed_at = $4
		WHERE id = $1`,
		string(orderID), string(result), string(mockID), confirmedAt.UTC(),
	)
	if err != nil {
		t.Fatalf("fixture order confirmation failed (SQLSTATE %s)", committedTestSQLState(err))
	}
}

func committedTestNewObservation(order payment.PaymentOrder, source payment.Source) payment.Observation {
	return payment.Observation{
		ID:             payment.ResultHandlingRecordID(payment.NewUUIDv4()),
		PaymentOrderID: order.ID,
		ClientID:       order.ClientID,
		RequestID:      payment.RequestID(payment.NewUUIDv4()),
		Source:         source,
		ObservedAt:     time.Now().UTC().Truncate(time.Microsecond),
	}
}

func committedTestAcceptedObservation(order payment.PaymentOrder, mockID *payment.MockTransactionID) payment.Observation {
	observation := committedTestNewObservation(order, payment.SourcePayment)
	platformReferenceID := order.ID
	amount := order.Amount
	currency := order.Currency
	result := payment.ResultSucceeded
	observation.Evidence = payment.Evidence{
		PlatformReferenceID: &platformReferenceID,
		Amount:              &amount,
		Currency:            &currency,
		Result:              &result,
	}
	observation.Candidate = &payment.CandidateConfirmation{
		Result:            result,
		MockTransactionID: mockID,
	}
	if mockID != nil {
		observation.Evidence.MockTransactionID = mockID
	}
	return observation
}

func committedTestKeepUnknownObservation(order payment.PaymentOrder) payment.Observation {
	observation := committedTestNewObservation(order, payment.SourceQuery)
	reasonCode := "NO_MATCH"
	observation.Reason = &payment.Reason{Code: reasonCode}
	return observation
}

func committedTestRejectedObservation(order payment.PaymentOrder) payment.Observation {
	observation := committedTestNewObservation(order, payment.SourceQuery)
	platformReferenceID := order.ID
	amount := order.Amount + 1
	currency := order.Currency
	observation.Evidence = payment.Evidence{
		PlatformReferenceID: &platformReferenceID,
		Amount:              &amount,
		Currency:            &currency,
		Differences:         []payment.EvidenceFieldName{payment.EvidenceFieldAmount},
	}
	observation.EvidenceRejected = true
	reasonCode := "EVIDENCE_MISMATCH"
	observation.Reason = &payment.Reason{Code: reasonCode}
	return observation
}

type committedTestOrderRow struct {
	ID                string
	ClientID          string
	BusinessOrderNo   string
	Amount            int64
	Currency          string
	Result            string
	MockTransactionID *string
	RegisteredAt      time.Time
	ConfirmedAt       *time.Time
}

func committedTestReadOrder(t *testing.T, pool *pgxpool.Pool, orderID payment.PaymentOrderID) (committedTestOrderRow, bool) {
	t.Helper()
	ctx, cancel := committedTestContext(t)
	defer cancel()
	var row committedTestOrderRow
	err := pool.QueryRow(ctx, `SELECT id::text, client_id, business_order_no, amount, currency, result,
		mock_transaction_id::text, registered_at, confirmed_at
		FROM payment_orders WHERE id = $1`, string(orderID)).Scan(
		&row.ID, &row.ClientID, &row.BusinessOrderNo, &row.Amount, &row.Currency, &row.Result,
		&row.MockTransactionID, &row.RegisteredAt, &row.ConfirmedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return committedTestOrderRow{}, false
	}
	if err != nil {
		t.Fatalf("fixture order read failed (SQLSTATE %s)", committedTestSQLState(err))
	}
	return row, true
}

func committedTestReadOrderByBusinessKey(t *testing.T, pool *pgxpool.Pool, clientID, businessOrderNo string) (committedTestOrderRow, bool) {
	t.Helper()
	ctx, cancel := committedTestContext(t)
	defer cancel()
	var row committedTestOrderRow
	err := pool.QueryRow(ctx, `SELECT id::text, client_id, business_order_no, amount, currency, result,
		mock_transaction_id::text, registered_at, confirmed_at
		FROM payment_orders WHERE client_id = $1 AND business_order_no = $2`, clientID, businessOrderNo).Scan(
		&row.ID, &row.ClientID, &row.BusinessOrderNo, &row.Amount, &row.Currency, &row.Result,
		&row.MockTransactionID, &row.RegisteredAt, &row.ConfirmedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return committedTestOrderRow{}, false
	}
	if err != nil {
		t.Fatalf("fixture business-key order read failed (SQLSTATE %s)", committedTestSQLState(err))
	}
	return row, true
}

func committedTestCountOrdersByBusinessKey(t *testing.T, pool *pgxpool.Pool, clientID, businessOrderNo string) int {
	t.Helper()
	ctx, cancel := committedTestContext(t)
	defer cancel()
	var count int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM payment_orders WHERE client_id = $1 AND business_order_no = $2`, clientID, businessOrderNo).Scan(&count); err != nil {
		t.Fatalf("fixture business-key order count failed (SQLSTATE %s)", committedTestSQLState(err))
	}
	return count
}

type committedTestRecordRow struct {
	ID             string
	PaymentOrderID string
	RequestID      string
	Source         string
	ObservedAt     time.Time
	Evidence       []byte
	ResultBefore   string
	ResultAfter    string
	Decision       string
	ReasonCode     *string
	ReasonMessage  *string
}

func committedTestReadRecord(t *testing.T, pool *pgxpool.Pool, recordID payment.ResultHandlingRecordID) (committedTestRecordRow, bool) {
	t.Helper()
	ctx, cancel := committedTestContext(t)
	defer cancel()
	var row committedTestRecordRow
	err := pool.QueryRow(ctx, `SELECT id::text, payment_order_id::text, request_id::text, source, observed_at,
		evidence, result_before, result_after, decision, reason_code, reason_message
		FROM result_handling_records WHERE id = $1`, string(recordID)).Scan(
		&row.ID, &row.PaymentOrderID, &row.RequestID, &row.Source, &row.ObservedAt,
		&row.Evidence, &row.ResultBefore, &row.ResultAfter, &row.Decision, &row.ReasonCode, &row.ReasonMessage,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return committedTestRecordRow{}, false
	}
	if err != nil {
		t.Fatalf("fixture handling record read failed (SQLSTATE %s)", committedTestSQLState(err))
	}
	return row, true
}

func committedTestCountRecords(t *testing.T, pool *pgxpool.Pool, orderID payment.PaymentOrderID) int {
	t.Helper()
	ctx, cancel := committedTestContext(t)
	defer cancel()
	var count int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM result_handling_records WHERE payment_order_id = $1`, string(orderID)).Scan(&count); err != nil {
		t.Fatalf("fixture handling record count failed (SQLSTATE %s)", committedTestSQLState(err))
	}
	return count
}

func committedTestRecordFromRow(t *testing.T, row committedTestRecordRow) payment.ResultHandlingRecord {
	t.Helper()
	var evidence payment.Evidence
	if err := json.Unmarshal(row.Evidence, &evidence); err != nil {
		t.Fatalf("fixture handling record evidence is not decodable")
	}
	return payment.ResultHandlingRecord{
		ID:             payment.ResultHandlingRecordID(row.ID),
		PaymentOrderID: payment.PaymentOrderID(row.PaymentOrderID),
		RequestID:      payment.RequestID(row.RequestID),
		Source:         payment.Source(row.Source),
		ObservedAt:     row.ObservedAt,
		Evidence:       evidence,
		ResultBefore:   payment.Result(row.ResultBefore),
		ResultAfter:    payment.Result(row.ResultAfter),
		Decision:       payment.HandlingDecision(row.Decision),
		ReasonCode:     row.ReasonCode,
		ReasonMessage:  row.ReasonMessage,
	}
}

func committedTestEvidenceObject(t *testing.T, raw []byte) map[string]json.RawMessage {
	t.Helper()
	var object map[string]json.RawMessage
	if err := json.Unmarshal(raw, &object); err != nil || object == nil {
		t.Fatalf("fixture handling record evidence is not a JSON object")
	}
	allowed := map[string]bool{
		"platform_reference_id": true,
		"mock_transaction_id":   true,
		"amount":                true,
		"currency":              true,
		"result":                true,
		"differences":           true,
	}
	for key := range object {
		if !allowed[key] {
			t.Fatalf("handling record evidence contains disallowed field %q", key)
		}
	}
	return object
}

func committedTestRequireUnknownOrderRow(t *testing.T, row committedTestOrderRow, input payment.RegisterOrderInput, registeredAt time.Time) {
	t.Helper()
	if row.ClientID != input.ClientID || row.BusinessOrderNo != input.BusinessOrderNo {
		t.Fatalf("committed registration row changed the business identity")
	}
	if row.Amount != input.Amount || row.Currency != input.Currency {
		t.Fatalf("committed registration row changed the original amount or currency")
	}
	if row.Result != string(payment.ResultUnknown) {
		t.Fatalf("committed registration row is not UNKNOWN")
	}
	if row.MockTransactionID != nil {
		t.Fatalf("committed UNKNOWN registration row has a mock transaction ID")
	}
	if row.ConfirmedAt != nil {
		t.Fatalf("committed UNKNOWN registration row has a confirmation time")
	}
	if !row.RegisteredAt.Equal(registeredAt.UTC().Truncate(time.Microsecond)) {
		t.Fatalf("committed registration row has a different registration time")
	}
	if _, offset := row.RegisteredAt.Zone(); offset != 0 {
		t.Fatalf("committed registration row has a non-UTC registration time")
	}
}

func committedTestRequireRecordIdentity(t *testing.T, record payment.ResultHandlingRecord, observation payment.Observation, order payment.PaymentOrder) {
	t.Helper()
	if record.PaymentOrderID != order.ID {
		t.Fatalf("handling record changed the order identity")
	}
	if record.RequestID != observation.RequestID {
		t.Fatalf("handling record changed the request ID")
	}
	if record.Source != observation.Source {
		t.Fatalf("handling record changed the source")
	}
	if !record.ObservedAt.Equal(observation.ObservedAt) {
		t.Fatalf("handling record changed the observation time")
	}
	if !reflect.DeepEqual(record.Evidence, observation.Evidence) {
		t.Fatalf("handling record changed the fixed evidence")
	}
}

func committedTestRequireSavedAccepted(t *testing.T, result payment.SaveObservationResult, observation payment.Observation, order payment.PaymentOrder, mockID payment.MockTransactionID, wantMock bool) {
	t.Helper()
	if result.Order.ID != order.ID {
		t.Fatalf("saved order ID changed")
	}
	if result.Order.ClientID != order.ClientID || result.Order.BusinessOrderNo != order.BusinessOrderNo {
		t.Fatalf("saved order business identity changed")
	}
	if result.Order.Amount != order.Amount || result.Order.Currency != order.Currency {
		t.Fatalf("saved order amount or currency changed")
	}
	if result.Order.Result != payment.ResultSucceeded {
		t.Fatalf("saved order result is not SUCCEEDED")
	}
	if result.Order.ConfirmedAt == nil {
		t.Fatalf("saved order has no confirmation time")
	}
	if !result.Order.RegisteredAt.Equal(order.RegisteredAt) {
		t.Fatalf("saved order registration time changed")
	}
	if wantMock {
		if result.Order.MockTransactionID == nil || *result.Order.MockTransactionID != mockID {
			t.Fatalf("saved order did not store the verified mock transaction ID")
		}
	} else if result.Order.MockTransactionID != nil {
		t.Fatalf("saved order invented a mock transaction ID")
	}
	if result.Record.ID != observation.ID {
		t.Fatalf("saved handling record ID changed")
	}
	if result.Record.PaymentOrderID != order.ID {
		t.Fatalf("saved handling record changed the order identity")
	}
	if result.Record.RequestID != observation.RequestID || result.Record.Source != observation.Source {
		t.Fatalf("saved handling record changed the request or source")
	}
	if !result.Record.ObservedAt.Equal(observation.ObservedAt) {
		t.Fatalf("saved handling record changed the observation time")
	}
	if result.Record.ResultBefore != payment.ResultUnknown || result.Record.ResultAfter != payment.ResultSucceeded {
		t.Fatalf("saved handling record did not derive UNKNOWN -> SUCCEEDED")
	}
	if result.Record.Decision != payment.DecisionAcceptConfirmation {
		t.Fatalf("saved handling record did not derive ACCEPT_CONFIRMATION")
	}
	if result.Record.ReasonCode != nil || result.Record.ReasonMessage != nil {
		t.Fatalf("saved accepted handling record unexpectedly has a reason")
	}
	if !reflect.DeepEqual(result.Record.Evidence, observation.Evidence) {
		t.Fatalf("saved handling evidence differs from the fixed observation evidence")
	}
}

func committedTestCleanup(t *testing.T, pool *pgxpool.Pool, clientID string) {
	t.Helper()
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), committedTestCleanupBudget)
		defer cancel()
		if _, err := pool.Exec(ctx, `DELETE FROM result_handling_records
			WHERE payment_order_id IN (SELECT id FROM payment_orders WHERE client_id = $1)`, clientID); err != nil {
			t.Errorf("committed test record cleanup failed (SQLSTATE %s)", committedTestSQLState(err))
			return
		}
		if _, err := pool.Exec(ctx, `DELETE FROM payment_orders WHERE client_id = $1`, clientID); err != nil {
			t.Errorf("committed test order cleanup failed (SQLSTATE %s)", committedTestSQLState(err))
			return
		}
		var residual int
		if err := pool.QueryRow(ctx, `SELECT count(*) FROM payment_orders WHERE client_id = $1`, clientID).Scan(&residual); err != nil {
			t.Errorf("committed test cleanup verification failed (SQLSTATE %s)", committedTestSQLState(err))
			return
		}
		if residual != 0 {
			t.Errorf("committed test cleanup left %d order rows", residual)
		}
	})
}
