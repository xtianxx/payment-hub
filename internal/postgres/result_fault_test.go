//go:build integration

package postgres_test

// T055 real-PostgreSQL fault target test for the US3 observation store.
//
// This root uses the launcher-provided isolated database only. It never starts
// PostgreSQL, creates a database, runs migrations, truncates a table, or touches
// a development database. The one temporary trigger is created for this run's
// exact record ID, then removed by t.Cleanup with a catalog-absence check.
//
// Real SQL failure is produced by PostgreSQL itself. The test-only transaction
// wrappers only observe the real SQLSTATE/constraint label or coordinate a
// synchronous race; they never fabricate a pgconn.PgError as a target red.

import (
	"context"
	"errors"
	"os"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"payment-hub/internal/mock"
	"payment-hub/internal/payment"
	"payment-hub/internal/postgres"
	"payment-hub/tests/harness"
)

var (
	_ postgres.DB = (*faultTestDB)(nil)
	_ postgres.DB = (*faultTestRaceDB)(nil)
	_ pgx.Tx      = (*faultTestTx)(nil)
	_ pgx.Tx      = (*faultTestRaceTx)(nil)
)

func TestObservationFaultIntegration(t *testing.T) {
	observer := harness.OpenCommittedPostgresPool(t)
	writer := harness.OpenCommittedPostgresPool(t)

	t.Run("record insert SQL failure rolls back the accepted order update", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		order := committedTestNewUnknownOrder(clientID, committedTestNewBusinessOrderNo(), 1100)
		committedTestInsertOrder(t, observer, order)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		observation := committedTestAcceptedObservation(order, &mockID)

		faultTestInstallRecordInsertTrigger(t, observer, observation.ID)
		db := &faultTestDB{pool: writer}
		store := postgres.NewStore(db, postgres.NewHooks(nil))

		ctx, cancel := committedTestContext(t)
		result, err := store.SaveObservation(ctx, observation)
		cancel()
		if err == nil {
			t.Fatalf("record insert SQL failure was reported as success")
		}
		if errors.Is(err, payment.ErrTraceSaveFailed) || errors.Is(err, payment.ErrTraceSaveIndeterminate) {
			t.Fatalf("an UNKNOWN order accepted by this attempt was wrongly marker-eligible")
		}
		if !errors.Is(err, payment.ErrUnavailable) {
			t.Fatalf("record insert SQL failure returned %s, want unavailable", committedTestErrorClass(err))
		}
		if got := db.recordInsertSQLStateValue(); got != "P0001" {
			t.Fatalf("record insert fault did not execute real SQL (SQLSTATE %s)", got)
		}
		if !db.sawOrderUpdate.Load() {
			t.Fatalf("record insert fault did not follow the order update")
		}
		if db.commitCalls.Load() != 0 {
			t.Fatalf("record insert fault still reached commit")
		}
		if db.rollbackCalls.Load() == 0 {
			t.Fatalf("record insert fault did not roll back the transaction")
		}
		if !reflect.DeepEqual(result, payment.SaveObservationResult{}) {
			t.Fatalf("record insert fault returned a partial save result")
		}

		row, ok := committedTestReadOrder(t, observer, order.ID)
		if !ok {
			t.Fatalf("order disappeared after the record insert fault")
		}
		if row.Result != string(payment.ResultUnknown) || row.ConfirmedAt != nil || row.MockTransactionID != nil {
			t.Fatalf("record insert fault left a half-saved order confirmation")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 0 {
			t.Fatalf("record insert fault left %d handling records, want 0", got)
		}
	})

	t.Run("UNKNOWN keep-unknown trace SQL failure never becomes a marker", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		order := committedTestNewUnknownOrder(clientID, committedTestNewBusinessOrderNo(), 1050)
		committedTestInsertOrder(t, observer, order)
		observation := committedTestKeepUnknownObservation(order)

		faultTestInstallRecordInsertTrigger(t, observer, observation.ID)
		db := &faultTestDB{pool: writer}
		store := postgres.NewStore(db, postgres.NewHooks(nil))

		ctx, cancel := committedTestContext(t)
		result, err := store.SaveObservation(ctx, observation)
		cancel()
		if err == nil {
			t.Fatalf("UNKNOWN keep-unknown trace SQL failure was reported as success")
		}
		if errors.Is(err, payment.ErrTraceSaveFailed) || errors.Is(err, payment.ErrTraceSaveIndeterminate) {
			t.Fatalf("an original UNKNOWN order was wrongly marker-eligible")
		}
		if !errors.Is(err, payment.ErrUnavailable) {
			t.Fatalf("UNKNOWN keep-unknown trace SQL failure returned %s, want unavailable", committedTestErrorClass(err))
		}
		if got := db.recordInsertSQLStateValue(); got != "P0001" {
			t.Fatalf("UNKNOWN keep-unknown trace fault did not execute real SQL (SQLSTATE %s)", got)
		}
		if db.sawOrderUpdate.Load() {
			t.Fatalf("UNKNOWN keep-unknown trace path rewrote the order")
		}
		if !reflect.DeepEqual(result, payment.SaveObservationResult{}) {
			t.Fatalf("UNKNOWN keep-unknown trace fault returned a partial save result")
		}
		row, ok := committedTestReadOrder(t, observer, order.ID)
		if !ok || row.Result != string(payment.ResultUnknown) || row.ConfirmedAt != nil || row.MockTransactionID != nil {
			t.Fatalf("UNKNOWN keep-unknown trace fault changed the order")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 0 {
			t.Fatalf("UNKNOWN keep-unknown trace fault left %d handling records, want 0", got)
		}
	})

	t.Run("original confirmation trace SQL failure returns the failed marker", func(t *testing.T) {
		for _, confirmed := range []payment.Result{payment.ResultSucceeded, payment.ResultFailed} {
			confirmed := confirmed
			t.Run(string(confirmed), func(t *testing.T) {
				clientID := committedTestNewClientID()
				committedTestCleanup(t, observer, clientID)
				mockID := payment.MockTransactionID(payment.NewUUIDv4())
				confirmedAt := time.Now().UTC().Add(-time.Minute).Truncate(time.Microsecond)
				order := committedTestNewConfirmedOrder(clientID, committedTestNewBusinessOrderNo(), 2500, confirmed, mockID, confirmedAt)
				committedTestInsertOrder(t, observer, order)
				observation := committedTestKeepUnknownObservation(order)

				faultTestInstallRecordInsertTrigger(t, observer, observation.ID)
				db := &faultTestDB{pool: writer}
				store := postgres.NewStore(db, postgres.NewHooks(nil))

				ctx, cancel := committedTestContext(t)
				result, err := store.SaveObservation(ctx, observation)
				cancel()
				if !errors.Is(err, payment.ErrTraceSaveFailed) {
					t.Fatalf("original-confirmation trace SQL failure returned %s, want the failed marker", committedTestErrorClass(err))
				}
				if errors.Is(err, payment.ErrTraceSaveIndeterminate) {
					t.Fatalf("original-confirmation trace SQL failure was reported as indeterminate")
				}
				if got := db.recordInsertSQLStateValue(); got != "P0001" {
					t.Fatalf("trace fault did not execute real SQL (SQLSTATE %s)", got)
				}
				if db.sawOrderUpdate.Load() {
					t.Fatalf("original-confirmation trace path rewrote the order")
				}
				if !reflect.DeepEqual(result.Record, payment.ResultHandlingRecord{}) {
					t.Fatalf("trace marker carried a handling record")
				}
				faultTestRequireSameOrder(t, result.Order, order, "trace marker")

				row, ok := committedTestReadOrder(t, observer, order.ID)
				if !ok {
					t.Fatalf("original confirmation order disappeared after the trace fault")
				}
				if row.Result != string(confirmed) || row.ConfirmedAt == nil || !row.ConfirmedAt.Equal(confirmedAt) {
					t.Fatalf("trace fault changed the original confirmation")
				}
				if row.MockTransactionID == nil || *row.MockTransactionID != string(mockID) {
					t.Fatalf("trace fault changed the original Mock association")
				}
				if _, ok := committedTestReadRecord(t, observer, observation.ID); ok {
					t.Fatalf("trace fault left a handling record")
				}
				if got := committedTestCountRecords(t, observer, order.ID); got != 0 {
					t.Fatalf("trace fault left %d handling records, want 0", got)
				}
			})
		}
	})

	t.Run("original confirmation rejected trace SQL failure also returns the failed marker", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		confirmedAt := time.Now().UTC().Add(-time.Minute).Truncate(time.Microsecond)
		order := committedTestNewConfirmedOrder(clientID, committedTestNewBusinessOrderNo(), 1800, payment.ResultSucceeded, mockID, confirmedAt)
		committedTestInsertOrder(t, observer, order)
		observation := committedTestRejectedObservation(order)

		faultTestInstallRecordInsertTrigger(t, observer, observation.ID)
		db := &faultTestDB{pool: writer}
		store := postgres.NewStore(db, postgres.NewHooks(nil))

		ctx, cancel := committedTestContext(t)
		result, err := store.SaveObservation(ctx, observation)
		cancel()
		if !errors.Is(err, payment.ErrTraceSaveFailed) {
			t.Fatalf("rejected trace SQL failure returned %s, want the failed marker", committedTestErrorClass(err))
		}
		if got := db.recordInsertSQLStateValue(); got != "P0001" {
			t.Fatalf("rejected trace fault did not execute real SQL (SQLSTATE %s)", got)
		}
		if !reflect.DeepEqual(result.Record, payment.ResultHandlingRecord{}) {
			t.Fatalf("rejected trace marker carried a handling record")
		}
		faultTestRequireSameOrder(t, result.Order, order, "rejected trace marker")

		row, ok := committedTestReadOrder(t, observer, order.ID)
		if !ok || row.Result != string(payment.ResultSucceeded) || row.ConfirmedAt == nil || !row.ConfirmedAt.Equal(confirmedAt) {
			t.Fatalf("rejected trace fault changed the original confirmation")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 0 {
			t.Fatalf("rejected trace fault left %d handling records, want 0", got)
		}
	})

	t.Run("control fault never becomes a trace marker", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		confirmedAt := time.Now().UTC().Add(-time.Minute).Truncate(time.Microsecond)
		order := committedTestNewConfirmedOrder(clientID, committedTestNewBusinessOrderNo(), 2100, payment.ResultSucceeded, mockID, confirmedAt)
		committedTestInsertOrder(t, observer, order)
		observation := committedTestKeepUnknownObservation(order)

		control := &committedTestControl{savingErr: errors.New("fault test control canary")}
		db := &faultTestDB{pool: writer}
		store := postgres.NewStore(db, postgres.NewHooks(control))

		ctx, cancel := committedTestContext(t)
		result, err := store.SaveObservation(ctx, observation)
		cancel()
		if !errors.Is(err, payment.ErrUnavailable) {
			t.Fatalf("control fault returned %s, want unavailable", committedTestErrorClass(err))
		}
		if errors.Is(err, payment.ErrTraceSaveFailed) || errors.Is(err, payment.ErrTraceSaveIndeterminate) {
			t.Fatalf("control fault was wrongly reported as a trace marker")
		}
		if control.savingCalls.Load() != 1 {
			t.Fatalf("control fault hook was not invoked exactly once")
		}
		if db.commitCalls.Load() != 0 || db.sawOrderUpdate.Load() {
			t.Fatalf("control fault reached a write")
		}
		if !reflect.DeepEqual(result, payment.SaveObservationResult{}) {
			t.Fatalf("control fault returned a partial save result")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 0 {
			t.Fatalf("control fault left %d handling records, want 0", got)
		}
	})

	t.Run("cross-client observation never becomes a trace marker", func(t *testing.T) {
		clientID := committedTestNewClientID()
		otherClientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		committedTestCleanup(t, observer, otherClientID)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		confirmedAt := time.Now().UTC().Add(-time.Minute).Truncate(time.Microsecond)
		order := committedTestNewConfirmedOrder(clientID, committedTestNewBusinessOrderNo(), 2200, payment.ResultSucceeded, mockID, confirmedAt)
		committedTestInsertOrder(t, observer, order)
		observation := committedTestKeepUnknownObservation(order)
		observation.ClientID = otherClientID

		db := &faultTestDB{pool: writer}
		store := postgres.NewStore(db, postgres.NewHooks(nil))

		ctx, cancel := committedTestContext(t)
		result, err := store.SaveObservation(ctx, observation)
		cancel()
		if !errors.Is(err, payment.ErrNotFound) {
			t.Fatalf("cross-client observation returned %s, want not found", committedTestErrorClass(err))
		}
		if errors.Is(err, payment.ErrTraceSaveFailed) || errors.Is(err, payment.ErrTraceSaveIndeterminate) {
			t.Fatalf("cross-client identity failure was wrongly reported as a trace marker")
		}
		if !reflect.DeepEqual(result, payment.SaveObservationResult{}) {
			t.Fatalf("cross-client observation returned a partial save result")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 0 {
			t.Fatalf("cross-client observation left %d handling records, want 0", got)
		}
	})

	t.Run("controlled postcommit lost reply reads back the committed trace", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		order := committedTestNewUnknownOrder(clientID, committedTestNewBusinessOrderNo(), 1600)
		committedTestInsertOrder(t, observer, order)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		observation := committedTestAcceptedObservation(order, &mockID)

		db := committedTestNewDB(writer)
		db.commitUnknown = true
		store := postgres.NewStore(db, postgres.NewHooks(nil))

		ctx, cancel := committedTestContext(t)
		result, err := store.SaveObservation(ctx, observation)
		cancel()
		if err != nil {
			t.Fatalf("controlled postcommit reply loss failed read-back: %s", committedTestErrorClass(err))
		}
		if db.commitCalls.Load() == 0 {
			t.Fatalf("controlled postcommit reply loss did not reach commit")
		}
		if result.Record.Decision != payment.DecisionAcceptConfirmation {
			t.Fatalf("read-back did not return the committed ACCEPT_CONFIRMATION record")
		}
		if result.Order.Result != payment.ResultSucceeded {
			t.Fatalf("read-back did not return the committed order result")
		}
		row, ok := committedTestReadOrder(t, observer, order.ID)
		if !ok || row.Result != string(payment.ResultSucceeded) {
			t.Fatalf("controlled postcommit reply loss did not preserve the order fact")
		}
		recordRow, ok := committedTestReadRecord(t, observer, observation.ID)
		if !ok {
			t.Fatalf("controlled postcommit reply loss did not preserve the handling record")
		}
		record := committedTestRecordFromRow(t, recordRow)
		committedTestRequireRecordIdentity(t, record, observation, order)
		if record.Decision != payment.DecisionAcceptConfirmation {
			t.Fatalf("controlled postcommit reply loss changed the handling decision")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 1 {
			t.Fatalf("controlled postcommit reply loss produced %d records, want 1", got)
		}
	})

	t.Run("controlled postcommit lost reply preserves a committed trace-only save", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		confirmedAt := time.Now().UTC().Add(-time.Minute).Truncate(time.Microsecond)
		order := committedTestNewConfirmedOrder(clientID, committedTestNewBusinessOrderNo(), 2600, payment.ResultSucceeded, mockID, confirmedAt)
		committedTestInsertOrder(t, observer, order)
		observation := committedTestKeepUnknownObservation(order)

		db := committedTestNewDB(writer)
		db.commitUnknown = true
		store := postgres.NewStore(db, postgres.NewHooks(nil))

		ctx, cancel := committedTestContext(t)
		result, err := store.SaveObservation(ctx, observation)
		cancel()
		if err != nil {
			t.Fatalf("trace-only commit reply loss failed read-back: %s", committedTestErrorClass(err))
		}
		if result.Record.Decision != payment.DecisionKeepConfirmed {
			t.Fatalf("read-back did not return the committed KEEP_CONFIRMED trace")
		}
		faultTestRequireSameOrder(t, result.Order, order, "trace-only read-back")
		if got := committedTestCountRecords(t, observer, order.ID); got != 1 {
			t.Fatalf("trace-only reply loss produced %d records, want 1", got)
		}
	})

	t.Run("same record id and same meaning retry reuses one confirmed trace", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		confirmedAt := time.Now().UTC().Add(-time.Minute).Truncate(time.Microsecond)
		order := committedTestNewConfirmedOrder(clientID, committedTestNewBusinessOrderNo(), 2700, payment.ResultSucceeded, mockID, confirmedAt)
		committedTestInsertOrder(t, observer, order)
		observation := committedTestKeepUnknownObservation(order)

		db := &faultTestDB{pool: writer}
		store := postgres.NewStore(db, postgres.NewHooks(nil))

		ctx, cancel := committedTestContext(t)
		first, err := store.SaveObservation(ctx, observation)
		cancel()
		if err != nil {
			t.Fatalf("first trace save failed: %s", committedTestErrorClass(err))
		}
		if first.Record.Decision != payment.DecisionKeepConfirmed {
			t.Fatalf("first trace save did not keep the confirmation")
		}

		ctx, cancel = committedTestContext(t)
		second, err := store.SaveObservation(ctx, observation)
		cancel()
		if err != nil {
			t.Fatalf("same-meaning trace retry failed: %s", committedTestErrorClass(err))
		}
		if second.Record.ID != observation.ID || second.Record.Decision != payment.DecisionKeepConfirmed {
			t.Fatalf("same-meaning trace retry did not reuse the committed record")
		}
		faultTestRequireSameOrder(t, second.Order, order, "same-meaning trace retry")
		if got := committedTestCountRecords(t, observer, order.ID); got != 1 {
			t.Fatalf("same-meaning trace retry produced %d records, want 1", got)
		}
	})

	t.Run("same record id with a different meaning is rejected", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		confirmedAt := time.Now().UTC().Add(-time.Minute).Truncate(time.Microsecond)
		order := committedTestNewConfirmedOrder(clientID, committedTestNewBusinessOrderNo(), 2800, payment.ResultSucceeded, mockID, confirmedAt)
		committedTestInsertOrder(t, observer, order)
		observation := committedTestKeepUnknownObservation(order)

		db := &faultTestDB{pool: writer}
		store := postgres.NewStore(db, postgres.NewHooks(nil))

		ctx, cancel := committedTestContext(t)
		if _, err := store.SaveObservation(ctx, observation); err != nil {
			cancel()
			t.Fatalf("first trace save failed: %s", committedTestErrorClass(err))
		}
		cancel()

		different := observation
		different.RequestID = payment.RequestID(payment.NewUUIDv4())
		ctx, cancel = committedTestContext(t)
		result, err := store.SaveObservation(ctx, different)
		cancel()
		if !errors.Is(err, payment.ErrObservationConflict) {
			t.Fatalf("different-meaning trace retry returned %s, want observation conflict", committedTestErrorClass(err))
		}
		if errors.Is(err, payment.ErrTraceSaveFailed) || errors.Is(err, payment.ErrTraceSaveIndeterminate) {
			t.Fatalf("different-meaning trace retry was wrongly reported as a trace marker")
		}
		if !reflect.DeepEqual(result, payment.SaveObservationResult{}) {
			t.Fatalf("different-meaning trace retry returned a partial save result")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 1 {
			t.Fatalf("different-meaning trace retry produced %d records, want 1", got)
		}
	})

	t.Run("historical unknown record retries against the later legitimate confirmation", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		order := committedTestNewUnknownOrder(clientID, committedTestNewBusinessOrderNo(), 1500)
		committedTestInsertOrder(t, observer, order)
		observation := committedTestKeepUnknownObservation(order)

		db := &faultTestDB{pool: writer}
		store := postgres.NewStore(db, postgres.NewHooks(nil))

		ctx, cancel := committedTestContext(t)
		first, err := store.SaveObservation(ctx, observation)
		cancel()
		if err != nil {
			t.Fatalf("historical keep-unknown save failed: %s", committedTestErrorClass(err))
		}
		if first.Record.Decision != payment.DecisionKeepUnknown {
			t.Fatalf("historical save did not record KEEP_UNKNOWN")
		}

		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		confirmedAt := time.Now().UTC().Truncate(time.Microsecond)
		committedTestConfirmOrder(t, observer, order.ID, payment.ResultSucceeded, mockID, confirmedAt)

		ctx, cancel = committedTestContext(t)
		retry, err := store.SaveObservation(ctx, observation)
		cancel()
		if err != nil {
			t.Fatalf("historical UNKNOWN retry after a later confirmation failed: %s", committedTestErrorClass(err))
		}
		if retry.Order.Result != payment.ResultSucceeded {
			t.Fatalf("historical UNKNOWN retry did not return the current confirmed result")
		}
		if retry.Record.ID != observation.ID || retry.Record.Decision != payment.DecisionKeepUnknown {
			t.Fatalf("historical UNKNOWN retry did not reuse the historical record")
		}
		if retry.Record.ResultBefore != payment.ResultUnknown || retry.Record.ResultAfter != payment.ResultUnknown {
			t.Fatalf("historical UNKNOWN retry rewrote the historical before/after state")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 1 {
			t.Fatalf("historical UNKNOWN retry produced %d records, want 1", got)
		}
	})

	t.Run("same order and record id concurrent saves append one record", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		order := committedTestNewUnknownOrder(clientID, committedTestNewBusinessOrderNo(), 1900)
		committedTestInsertOrder(t, observer, order)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		observation := committedTestAcceptedObservation(order, &mockID)

		db := &faultTestDB{pool: writer}
		store := postgres.NewStore(db, postgres.NewHooks(nil))

		calls := faultTestRunConcurrentObservations(t, store, []payment.Observation{observation, observation})
		if len(calls) != 2 {
			t.Fatalf("expected two concurrent save results, got %d", len(calls))
		}
		for _, call := range calls {
			if call.err != nil {
				t.Fatalf("same-order concurrent save returned %s", committedTestErrorClass(call.err))
			}
			if call.result.Record.ID != observation.ID {
				t.Fatalf("same-order concurrent save did not reuse the same record ID")
			}
			if call.result.Order.Result != payment.ResultSucceeded {
				t.Fatalf("same-order concurrent save did not return the confirmed result")
			}
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 1 {
			t.Fatalf("same-order concurrent saves produced %d records, want 1", got)
		}
	})

	t.Run("different orders sharing a record id get a real 23505 and no fake success", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		orderA := committedTestNewUnknownOrder(clientID, committedTestNewBusinessOrderNo(), 2000)
		orderB := committedTestNewUnknownOrder(clientID, committedTestNewBusinessOrderNo(), 2100)
		committedTestInsertOrder(t, observer, orderA)
		committedTestInsertOrder(t, observer, orderB)

		recordID := payment.ResultHandlingRecordID(payment.NewUUIDv4())
		mockA := payment.MockTransactionID(payment.NewUUIDv4())
		mockB := payment.MockTransactionID(payment.NewUUIDv4())
		observationA := committedTestAcceptedObservation(orderA, &mockA)
		observationB := committedTestAcceptedObservation(orderB, &mockB)
		observationA.ID = recordID
		observationB.ID = recordID

		db := &faultTestRaceDB{pool: writer, rendezvous: newRegistrationRendezvous()}
		store := postgres.NewStore(db, postgres.NewHooks(nil))
		calls := faultTestRunConcurrentObservations(t, store, []payment.Observation{observationA, observationB})
		if len(calls) != 2 {
			t.Fatalf("expected two concurrent save results, got %d", len(calls))
		}

		var winner, loser faultTestObservationCall
		successCount := 0
		conflictCount := 0
		for _, call := range calls {
			switch {
			case call.err == nil:
				successCount++
				winner = call
			case errors.Is(call.err, payment.ErrObservationConflict):
				conflictCount++
				loser = call
			default:
				t.Fatalf("shared-record-id race returned %s", committedTestErrorClass(call.err))
			}
		}
		if successCount != 1 || conflictCount != 1 {
			t.Fatalf("shared-record-id race returned %d successes and %d conflicts, want 1 and 1", successCount, conflictCount)
		}
		if got := db.recordInsertSQLStateValue(); got != "23505" {
			t.Fatalf("shared-record-id race did not get a real primary-key collision (SQLSTATE %s)", got)
		}
		if got := db.recordInsertConstraintValue(); got != "result_handling_records_pkey" {
			t.Fatalf("shared-record-id race collided on an unexpected constraint label")
		}
		if !reflect.DeepEqual(loser.result, payment.SaveObservationResult{}) {
			t.Fatalf("shared-record-id loser returned a fake success payload")
		}

		recordRow, ok := committedTestReadRecord(t, observer, recordID)
		if !ok {
			t.Fatalf("shared-record-id winner left no committed record")
		}
		if recordRow.PaymentOrderID != string(winner.observation.PaymentOrderID) {
			t.Fatalf("shared-record-id committed record belongs to neither the winner nor a single order")
		}
		if got := committedTestCountRecords(t, observer, orderA.ID); got+committedTestCountRecords(t, observer, orderB.ID) != 1 {
			t.Fatalf("shared-record-id race did not leave exactly one record across both orders")
		}
		winnerRow, ok := committedTestReadOrder(t, observer, winner.observation.PaymentOrderID)
		if !ok || winnerRow.Result != string(payment.ResultSucceeded) {
			t.Fatalf("shared-record-id winner order is not confirmed")
		}
		loserRow, ok := committedTestReadOrder(t, observer, loser.observation.PaymentOrderID)
		if !ok || loserRow.Result != string(payment.ResultUnknown) || loserRow.ConfirmedAt != nil || loserRow.MockTransactionID != nil {
			t.Fatalf("shared-record-id loser left a half-saved order")
		}
	})

	// Service + real-PostgreSQL composition evidence. This is not a normal
	// binary HTTP end-to-end run: the HTTP 201/Location/message contract is
	// proven separately in the httpapi real-JSON test layer.
	t.Run("submit_consumes_source_payment_trace_marker_after_concurrent_query_confirmation", func(t *testing.T) {
		clientID := committedTestNewClientID()
		committedTestCleanup(t, observer, clientID)
		input := payment.RegisterOrderInput{
			ClientID:        clientID,
			BusinessOrderNo: committedTestNewBusinessOrderNo(),
			Amount:          1300,
			Currency:        "CNY",
		}

		mockPool := harness.OpenCommittedPostgresPool(t)
		submitPool := harness.OpenCommittedPostgresPool(t)

		queryStore := postgres.NewStore(observer, postgres.NewHooks(nil))
		queryMock := mock.NewChannel(observer, mock.NewHooks(nil))
		queryService := payment.NewQueryService(queryStore, queryStore, queryMock, context.Background())

		submitDB := &faultTestDB{pool: submitPool}
		submitStore := postgres.NewStore(submitDB, postgres.NewHooks(nil))
		lookupSpy := &faultTestSubmitLookup{store: submitStore}
		recorderSpy := &faultTestSubmitRecorder{store: submitStore}

		committedCh := make(chan payment.PaymentOrderID, 1)
		callbackErrCh := make(chan error, 1)
		releaseCh := make(chan struct{})
		var releaseOnce sync.Once
		release := func() { releaseOnce.Do(func() { close(releaseCh) }) }
		defer release()

		control := &faultTestPaymentCallbackControl{}
		control.onCommitted = func(ctx context.Context, _ payment.Result) error {
			orderID := control.capturedOrderID()
			select {
			case committedCh <- orderID:
			case <-ctx.Done():
				callbackErrCh <- ctx.Err()
				return ctx.Err()
			}
			select {
			case <-releaseCh:
				return nil
			case <-ctx.Done():
				callbackErrCh <- ctx.Err()
				return ctx.Err()
			}
		}
		mockChannel := mock.NewChannel(mockPool, mock.NewHooks(control))
		payerSpy := &faultTestSubmitPayer{payer: mockChannel}
		submitter := payment.NewSubmitter(submitStore, lookupSpy, recorderSpy, payerSpy, context.Background())

		// Register this before starting Submit so an early t.Fatal still removes
		// the real Mock row. Cleanup order is trigger -> worker join/release ->
		// this exact-ID Mock delete/verify -> pool close -> order/record cleanup.
		faultTestCleanupMockForOrder(t, observer, control)

		submitDone := make(chan faultTestSubmitOutcome, 1)
		joined := false
		go func() {
			result, err := submitter.Submit(context.Background(), payment.RequestID(payment.NewUUIDv4()), input)
			submitDone <- faultTestSubmitOutcome{result: result, err: err}
		}()
		t.Cleanup(func() {
			release()
			if joined {
				return
			}
			select {
			case <-submitDone:
			case <-time.After(committedTestBudget):
				t.Errorf("Submit did not finish during cleanup")
			}
		})

		var orderID payment.PaymentOrderID
		select {
		case orderID = <-committedCh:
		case early := <-submitDone:
			joined = true
			t.Fatalf("Submit finished before the concurrent query/trigger seam (error class %s)", committedTestErrorClass(early.err))
		case err := <-callbackErrCh:
			t.Fatalf("Mock committed callback failed: %s", committedTestErrorClass(err))
		case <-time.After(committedTestBudget):
			t.Fatalf("timed out waiting for the Mock committed signal")
		}

		queryCtx, cancelQuery := context.WithTimeout(context.Background(), committedTestBudget)
		queryResult, queryErr := queryService.Query(queryCtx, payment.RequestID(payment.NewUUIDv4()), payment.QueryInput{
			ClientID:       clientID,
			PaymentOrderID: orderID,
		})
		cancelQuery()
		if queryErr != nil {
			t.Fatalf("concurrent query confirmation failed: %s", committedTestErrorClass(queryErr))
		}
		if queryResult.Order.Result != payment.ResultSucceeded ||
			queryResult.Order.ConfirmedAt == nil ||
			queryResult.Order.MockTransactionID == nil {
			t.Fatalf("concurrent query did not confirm the order")
		}
		if got := committedTestCountRecords(t, observer, orderID); got != 1 {
			t.Fatalf("concurrent query left %d handling records, want 1", got)
		}

		faultTestInstallPaymentTraceTrigger(t, observer, orderID)
		release()

		var outcome faultTestSubmitOutcome
		select {
		case outcome = <-submitDone:
			joined = true
		case <-time.After(committedTestBudget):
			t.Fatalf("Submit did not finish after the query confirmation and trigger install")
		}
		select {
		case err := <-callbackErrCh:
			t.Fatalf("Mock committed callback failed: %s", committedTestErrorClass(err))
		default:
		}
		if outcome.err != nil {
			t.Fatalf("Submit returned %s", committedTestErrorClass(outcome.err))
		}
		if !outcome.result.Created {
			t.Fatalf("Submit must retain the created fact")
		}
		if outcome.result.Order.Result != payment.ResultSucceeded {
			t.Fatalf("Submit did not return the locked confirmation")
		}
		if outcome.result.Order.ConfirmedAt == nil ||
			!outcome.result.Order.ConfirmedAt.Equal(*queryResult.Order.ConfirmedAt) {
			t.Fatalf("Submit confirmation time differs from the locked query confirmation")
		}
		if outcome.result.Order.MockTransactionID == nil ||
			*outcome.result.Order.MockTransactionID != *queryResult.Order.MockTransactionID {
			t.Fatalf("Submit Mock association differs from the locked query confirmation")
		}
		if len(outcome.result.Issues) != 1 ||
			outcome.result.Issues[0].Code != payment.IssueCodeTraceSaveError ||
			outcome.result.Issues[0].Message != "本次追踪保存失败" {
			t.Fatalf("Submit did not report the controlled trace-save failure")
		}
		if lookupSpy.byIDCalls.Load() != 0 || lookupSpy.byBusinessCalls.Load() != 0 {
			t.Fatalf("Submit marker path performed an extra lookup")
		}
		if payerSpy.calls.Load() != 1 {
			t.Fatalf("expected exactly one original Mock payment call, got %d", payerSpy.calls.Load())
		}
		if recorderSpy.calls.Load() != 1 {
			t.Fatalf("expected exactly one Submit save attempt, got %d", recorderSpy.calls.Load())
		}
		if got := submitDB.recordInsertSQLStateValue(); got != "P0001" {
			t.Fatalf("Submit PAYMENT trace insert did not execute the real trigger (SQLSTATE %s)", got)
		}
		if submitDB.sawOrderUpdate.Load() {
			t.Fatalf("Submit marker path rewrote the already confirmed order")
		}
		if submitDB.rollbackCalls.Load() == 0 {
			t.Fatalf("Submit PAYMENT trace fault did not roll back")
		}

		readCtx, cancelRead := context.WithTimeout(context.Background(), committedTestBudget)
		defer cancelRead()
		var mockID, mockCurrency, mockResult string
		var mockAmount int64
		if err := observer.QueryRow(readCtx, `SELECT id::text, amount, currency, result
			FROM mock_transactions WHERE platform_reference_id = $1`, string(orderID)).Scan(
			&mockID, &mockAmount, &mockCurrency, &mockResult,
		); err != nil {
			t.Fatalf("independent Mock row read failed (SQLSTATE %s)", committedTestSQLState(err))
		}
		if mockID != string(*queryResult.Order.MockTransactionID) ||
			mockAmount != input.Amount ||
			mockCurrency != input.Currency ||
			mockResult != string(payment.ResultSucceeded) {
			t.Fatalf("independent Mock row does not match the committed payment fact")
		}

		var paymentRecords, queryRecords int
		if err := observer.QueryRow(readCtx, `SELECT count(*) FILTER (WHERE source = 'PAYMENT'),
			count(*) FILTER (WHERE source = 'QUERY')
			FROM result_handling_records WHERE payment_order_id = $1`, string(orderID)).Scan(
			&paymentRecords, &queryRecords,
		); err != nil {
			t.Fatalf("handling-record source count failed (SQLSTATE %s)", committedTestSQLState(err))
		}
		if paymentRecords != 0 || queryRecords != 1 {
			t.Fatalf("handling records are PAYMENT=%d QUERY=%d, want 0/1", paymentRecords, queryRecords)
		}

		row, ok := committedTestReadOrder(t, observer, orderID)
		if !ok || row.Result != string(payment.ResultSucceeded) ||
			row.ConfirmedAt == nil ||
			!row.ConfirmedAt.Equal(*queryResult.Order.ConfirmedAt) {
			t.Fatalf("order is not the locked confirmed fact after Submit")
		}
	})
}

// faultTestObservationCall carries one concurrent SaveObservation outcome.
type faultTestObservationCall struct {
	observation payment.Observation
	result      payment.SaveObservationResult
	err         error
}

type faultTestSubmitOutcome struct {
	result payment.SubmitResult
	err    error
}

// faultTestSubmitLookup delegates to the real store and counts actual lookups.
type faultTestSubmitLookup struct {
	store           *postgres.Store
	byIDCalls       atomic.Int32
	byBusinessCalls atomic.Int32
}

func (l *faultTestSubmitLookup) FindOrderByID(ctx context.Context, clientID string, paymentOrderID payment.PaymentOrderID) (payment.PaymentOrder, bool, error) {
	l.byIDCalls.Add(1)
	return l.store.FindOrderByID(ctx, clientID, paymentOrderID)
}

func (l *faultTestSubmitLookup) FindOrderByBusinessOrderNo(ctx context.Context, clientID, businessOrderNo string) (payment.PaymentOrder, bool, error) {
	l.byBusinessCalls.Add(1)
	return l.store.FindOrderByBusinessOrderNo(ctx, clientID, businessOrderNo)
}

// faultTestSubmitRecorder delegates to the real store and counts save attempts.
type faultTestSubmitRecorder struct {
	store *postgres.Store
	calls atomic.Int32
}

func (r *faultTestSubmitRecorder) SaveObservation(ctx context.Context, observation payment.Observation) (payment.SaveObservationResult, error) {
	r.calls.Add(1)
	return r.store.SaveObservation(ctx, observation)
}

// faultTestSubmitPayer delegates to the real Mock channel and counts Pay calls.
type faultTestSubmitPayer struct {
	payer payment.MockPayer
	calls atomic.Int32
}

func (p *faultTestSubmitPayer) Pay(ctx context.Context, request payment.MockPaymentRequest) (payment.MockTransaction, error) {
	p.calls.Add(1)
	return p.payer.Pay(ctx, request)
}

// faultTestPaymentCallbackControl coordinates the real Mock postcommit callback
// with the main test goroutine. The callback never calls t.Fatal itself.
type faultTestPaymentCallbackControl struct {
	mu          sync.Mutex
	orderID     payment.PaymentOrderID
	onCommitted func(context.Context, payment.Result) error
}

func (c *faultTestPaymentCallbackControl) PaymentEntry(context.Context, payment.PaymentOrderID) error {
	return nil
}

func (c *faultTestPaymentCallbackControl) QueryEntry(context.Context, payment.PaymentOrderID) error {
	return nil
}

func (c *faultTestPaymentCallbackControl) Committed(context.Context, payment.PaymentOrderID) error {
	return nil
}

func (c *faultTestPaymentCallbackControl) PaymentEntryResult(_ context.Context, orderID payment.PaymentOrderID) (mock.PaymentEntryOutcome, error) {
	c.mu.Lock()
	c.orderID = orderID
	c.mu.Unlock()
	return mock.PaymentEntryOutcome{OnCommitted: c.onCommitted}, nil
}

func (c *faultTestPaymentCallbackControl) capturedOrderID() payment.PaymentOrderID {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.orderID
}

// faultTestCleanupMockForOrder removes only the real Mock row for the actual
// order captured by this run. An empty capture means no known Mock fact was
// formed and is skipped; a non-empty invalid capture is a fixture failure. The
// post-delete independent count must be zero.
func faultTestCleanupMockForOrder(t *testing.T, pool *pgxpool.Pool, control *faultTestPaymentCallbackControl) {
	t.Helper()
	t.Cleanup(func() {
		orderID := control.capturedOrderID()
		if orderID == "" {
			return
		}
		if payment.ValidateUUIDv4(string(orderID)) != nil {
			t.Errorf("fixture Mock cleanup: captured order ID is not a UUIDv4")
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), committedTestCleanupBudget)
		defer cancel()
		if _, err := pool.Exec(ctx, `DELETE FROM mock_transactions WHERE platform_reference_id = $1`, string(orderID)); err != nil {
			t.Errorf("fixture Mock cleanup delete failed (SQLSTATE %s)", committedTestSQLState(err))
			return
		}
		var count int
		if err := pool.QueryRow(ctx, `SELECT count(*) FROM mock_transactions WHERE platform_reference_id = $1`, string(orderID)).Scan(&count); err != nil {
			t.Errorf("fixture Mock cleanup verification failed (SQLSTATE %s)", committedTestSQLState(err))
			return
		}
		if count != 0 {
			t.Errorf("fixture Mock cleanup left %d rows for this order", count)
		}
	})
}

// faultTestRunConcurrentObservations starts every save behind one synchronous
// barrier. It never sleeps; each call owns its bounded context.
func faultTestRunConcurrentObservations(t *testing.T, store *postgres.Store, observations []payment.Observation) []faultTestObservationCall {
	t.Helper()
	start := make(chan struct{})
	results := make(chan faultTestObservationCall, len(observations))
	var wait sync.WaitGroup
	for _, observation := range observations {
		observation := observation
		wait.Add(1)
		go func() {
			defer wait.Done()
			<-start
			ctx, cancel := context.WithTimeout(context.Background(), committedTestBudget)
			defer cancel()
			result, err := store.SaveObservation(ctx, observation)
			results <- faultTestObservationCall{observation: observation, result: result, err: err}
		}()
	}
	close(start)
	wait.Wait()
	close(results)

	calls := make([]faultTestObservationCall, 0, len(observations))
	for call := range results {
		calls = append(calls, call)
	}
	return calls
}

// faultTestDB wraps the real pool. It injects nothing; it only observes the
// real SQLSTATE of the record INSERT and counts transaction endpoints.
type faultTestDB struct {
	pool *pgxpool.Pool

	sawOrderUpdate  atomic.Bool
	commitCalls     atomic.Int32
	rollbackCalls   atomic.Int32
	recordInsertSQL atomic.Value
}

func (db *faultTestDB) BeginTx(ctx context.Context, opts pgx.TxOptions) (pgx.Tx, error) {
	tx, err := db.pool.BeginTx(ctx, opts)
	if err != nil {
		return nil, err
	}
	return &faultTestTx{Tx: tx, db: db}, nil
}

func (db *faultTestDB) QueryRow(ctx context.Context, sql string, args ...any) pgx.Row {
	return db.pool.QueryRow(ctx, sql, args...)
}

func (db *faultTestDB) recordInsertSQLStateValue() string {
	value := db.recordInsertSQL.Load()
	if value == nil {
		return "none"
	}
	return value.(string)
}

type faultTestTx struct {
	pgx.Tx
	db *faultTestDB
}

func (tx *faultTestTx) Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	tag, err := tx.Tx.Exec(ctx, sql, args...)
	normalized := strings.ToLower(sql)
	if strings.Contains(normalized, "update payment_orders") {
		tx.db.sawOrderUpdate.Store(true)
	}
	if faultTestRecordInsertSQL(normalized) {
		tx.db.recordInsertSQL.Store(committedTestSQLState(err))
	}
	return tag, err
}

func (tx *faultTestTx) Commit(ctx context.Context) error {
	tx.db.commitCalls.Add(1)
	return tx.Tx.Commit(ctx)
}

func (tx *faultTestTx) Rollback(ctx context.Context) error {
	tx.db.rollbackCalls.Add(1)
	return tx.Tx.Rollback(ctx)
}

// faultTestRaceDB coordinates the shared-record-id race. It observes the real
// collision state and constraint label; it never substitutes a fake error.
type faultTestRaceDB struct {
	pool       *pgxpool.Pool
	rendezvous *registrationRendezvous

	recordInsertSQL        atomic.Value
	recordInsertConstraint atomic.Value
}

func (db *faultTestRaceDB) BeginTx(ctx context.Context, opts pgx.TxOptions) (pgx.Tx, error) {
	tx, err := db.pool.BeginTx(ctx, opts)
	if err != nil {
		return nil, err
	}
	return &faultTestRaceTx{Tx: tx, db: db}, nil
}

func (db *faultTestRaceDB) QueryRow(ctx context.Context, sql string, args ...any) pgx.Row {
	return db.pool.QueryRow(ctx, sql, args...)
}

func (db *faultTestRaceDB) recordInsertSQLStateValue() string {
	value := db.recordInsertSQL.Load()
	if value == nil {
		return "none"
	}
	return value.(string)
}

func (db *faultTestRaceDB) recordInsertConstraintValue() string {
	value := db.recordInsertConstraint.Load()
	if value == nil {
		return "none"
	}
	return value.(string)
}

type faultTestRaceTx struct {
	pgx.Tx
	db *faultTestRaceDB
}

func (tx *faultTestRaceTx) Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	if !faultTestRecordInsertSQL(strings.ToLower(sql)) {
		return tx.Tx.Exec(ctx, sql, args...)
	}
	// Announce this INSERT before executing it. The peer's commit waits for
	// both INSERTs to be in flight, so the loser gets a real 23505.
	tx.db.rendezvous.enterInsert()
	tag, err := tx.Tx.Exec(ctx, sql, args...)
	if err != nil {
		tx.db.recordInsertSQL.Store(committedTestSQLState(err))
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) {
			tx.db.recordInsertConstraint.Store(pgErr.ConstraintName)
		}
	}
	return tag, err
}

func (tx *faultTestRaceTx) Commit(ctx context.Context) error {
	tx.db.rendezvous.waitForPeerInsert(ctx)
	return tx.Tx.Commit(ctx)
}

func faultTestRecordInsertSQL(normalized string) bool {
	return strings.Contains(normalized, "insert into result_handling_records")
}

func faultTestRequireSameOrder(t *testing.T, got, want payment.PaymentOrder, label string) {
	t.Helper()
	if got.ID != want.ID ||
		got.ClientID != want.ClientID ||
		got.BusinessOrderNo != want.BusinessOrderNo ||
		got.Amount != want.Amount ||
		got.Currency != want.Currency ||
		got.Result != want.Result ||
		!got.RegisteredAt.Equal(want.RegisteredAt) {
		t.Fatalf("%s did not return the original order facts", label)
	}
	if want.ConfirmedAt == nil || got.ConfirmedAt == nil || !got.ConfirmedAt.Equal(*want.ConfirmedAt) {
		t.Fatalf("%s did not return the original confirmation time", label)
	}
	if want.MockTransactionID == nil || got.MockTransactionID == nil || *got.MockTransactionID != *want.MockTransactionID {
		t.Fatalf("%s did not return the original Mock association", label)
	}
}

// faultTestTrigger is one temporary BEFORE INSERT trigger bound to the exact
// record ID of this run. Both names contain a sanitized run token and a fresh
// UUID and are quoted with pgx.Identifier, never by free SQL.
type faultTestTrigger struct {
	pool         *pgxpool.Pool
	functionName string
	triggerName  string
}

func faultTestInstallRecordInsertTrigger(t *testing.T, pool *pgxpool.Pool, recordID payment.ResultHandlingRecordID) *faultTestTrigger {
	t.Helper()
	if payment.ValidateUUIDv4(string(recordID)) != nil {
		t.Fatalf("fault trigger record ID is not a UUIDv4")
	}
	suffix := strings.ReplaceAll(payment.NewUUIDv4(), "-", "")
	functionName := "us3_" + faultTestRunToken() + "_trace_fn_" + suffix
	triggerName := "us3_" + faultTestRunToken() + "_trace_tr_" + suffix
	functionIdent := pgx.Identifier{functionName}.Sanitize()
	triggerIdent := pgx.Identifier{triggerName}.Sanitize()
	tableIdent := pgx.Identifier{"result_handling_records"}.Sanitize()

	createFunction := `CREATE FUNCTION ` + functionIdent + `() RETURNS trigger LANGUAGE plpgsql AS $us3$
BEGIN
    RAISE EXCEPTION 'us3 trace insert fault' USING ERRCODE = 'P0001';
END
$us3$`
	// recordID is validated UUIDv4 text; it is the only literal in this DDL.
	createTrigger := `CREATE TRIGGER ` + triggerIdent + ` BEFORE INSERT ON ` + tableIdent +
		` FOR EACH ROW WHEN (NEW.id = '` + string(recordID) + `'::uuid) EXECUTE FUNCTION ` + functionIdent + `()`

	ctx, cancel := context.WithTimeout(context.Background(), committedTestBudget)
	defer cancel()
	if _, err := pool.Exec(ctx, createFunction); err != nil {
		t.Fatalf("fault trigger function creation failed (SQLSTATE %s)", committedTestSQLState(err))
	}
	// Register the exact cleanup immediately after the function exists. If
	// trigger creation fails below, t.Fatalf still runs this cleanup, which
	// attempts DROP IF EXISTS for both objects and checks catalog absence.
	trigger := &faultTestTrigger{pool: pool, functionName: functionName, triggerName: triggerName}
	t.Cleanup(func() { faultTestRemoveRecordInsertTrigger(t, trigger) })
	if _, err := pool.Exec(ctx, createTrigger); err != nil {
		t.Fatalf("fault trigger creation failed (SQLSTATE %s)", committedTestSQLState(err))
	}
	return trigger
}

// faultTestInstallPaymentTraceTrigger rejects only this run's PAYMENT trace
// insert for the exact order. The QUERY record was already committed before
// installation. Cleanup reuses the exact catalog-absence helper.
func faultTestInstallPaymentTraceTrigger(t *testing.T, pool *pgxpool.Pool, orderID payment.PaymentOrderID) *faultTestTrigger {
	t.Helper()
	if payment.ValidateUUIDv4(string(orderID)) != nil {
		t.Fatalf("payment trace trigger order ID is not a UUIDv4")
	}
	suffix := strings.ReplaceAll(payment.NewUUIDv4(), "-", "")
	functionName := "us3_" + faultTestRunToken() + "_st_fn_" + suffix
	triggerName := "us3_" + faultTestRunToken() + "_st_tr_" + suffix
	functionIdent := pgx.Identifier{functionName}.Sanitize()
	triggerIdent := pgx.Identifier{triggerName}.Sanitize()
	tableIdent := pgx.Identifier{"result_handling_records"}.Sanitize()

	createFunction := `CREATE FUNCTION ` + functionIdent + `() RETURNS trigger LANGUAGE plpgsql AS $us3$
BEGIN
    RAISE EXCEPTION 'us3 submit trace insert fault' USING ERRCODE = 'P0001';
END
$us3$`
	// orderID is validated UUIDv4 text; it is the only literal in this DDL.
	createTrigger := `CREATE TRIGGER ` + triggerIdent + ` BEFORE INSERT ON ` + tableIdent +
		` FOR EACH ROW WHEN (NEW.payment_order_id = '` + string(orderID) +
		`'::uuid AND NEW.source = 'PAYMENT') EXECUTE FUNCTION ` + functionIdent + `()`

	ctx, cancel := context.WithTimeout(context.Background(), committedTestBudget)
	defer cancel()
	if _, err := pool.Exec(ctx, createFunction); err != nil {
		t.Fatalf("payment trace trigger function creation failed (SQLSTATE %s)", committedTestSQLState(err))
	}
	trigger := &faultTestTrigger{pool: pool, functionName: functionName, triggerName: triggerName}
	t.Cleanup(func() { faultTestRemoveRecordInsertTrigger(t, trigger) })
	if _, err := pool.Exec(ctx, createTrigger); err != nil {
		t.Fatalf("payment trace trigger creation failed (SQLSTATE %s)", committedTestSQLState(err))
	}
	return trigger
}

func faultTestRemoveRecordInsertTrigger(t *testing.T, trigger *faultTestTrigger) {
	t.Helper()
	if trigger == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), committedTestCleanupBudget)
	defer cancel()

	triggerIdent := pgx.Identifier{trigger.triggerName}.Sanitize()
	tableIdent := pgx.Identifier{"result_handling_records"}.Sanitize()
	functionIdent := pgx.Identifier{trigger.functionName}.Sanitize()

	if _, err := trigger.pool.Exec(ctx, `DROP TRIGGER IF EXISTS `+triggerIdent+` ON `+tableIdent); err != nil {
		t.Errorf("fault trigger cleanup failed (SQLSTATE %s)", committedTestSQLState(err))
		return
	}
	if _, err := trigger.pool.Exec(ctx, `DROP FUNCTION IF EXISTS `+functionIdent+`()`); err != nil {
		t.Errorf("fault trigger function cleanup failed (SQLSTATE %s)", committedTestSQLState(err))
		return
	}

	var triggerCount, functionCount int
	if err := trigger.pool.QueryRow(ctx, `SELECT count(*) FROM pg_trigger WHERE tgname = $1`, trigger.triggerName).Scan(&triggerCount); err != nil {
		t.Errorf("fault trigger catalog check failed (SQLSTATE %s)", committedTestSQLState(err))
		return
	}
	if err := trigger.pool.QueryRow(ctx, `SELECT count(*) FROM pg_proc WHERE proname = $1`, trigger.functionName).Scan(&functionCount); err != nil {
		t.Errorf("fault trigger function catalog check failed (SQLSTATE %s)", committedTestSQLState(err))
		return
	}
	if triggerCount != 0 || functionCount != 0 {
		t.Errorf("fault trigger cleanup left catalog objects")
	}
}

func faultTestRunToken() string {
	raw := os.Getenv("PAYMENT_HUB_TEST_RUN_ID")
	var token strings.Builder
	for _, r := range raw {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
			token.WriteRune(r)
		}
		if token.Len() == 12 {
			break
		}
	}
	if token.Len() == 0 {
		return "local"
	}
	return token.String()
}
