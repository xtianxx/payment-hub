//go:build integration

package postgres_test

// T043 real-PostgreSQL concurrency target test for US2 storage idempotency.
//
// The root uses two independent committed pools and a synchronous start
// barrier. The test-only transaction wrapper records statement order and can
// substitute the candidate payment-order ID on the real INSERT so that a
// primary-key collision is produced by PostgreSQL itself, not by a fabricated
// pgconn.PgError. It never adds a production UUID-injection seam, changes the
// schema, truncates a table, or touches a development database.

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"payment-hub/internal/payment"
	"payment-hub/internal/postgres"
	"payment-hub/tests/harness"
)

var (
	_ postgres.DB = (*registrationTestDB)(nil)
	_ pgx.Tx      = (*registrationTestTx)(nil)
	_ pgx.Row     = (*registrationTestRow)(nil)
)

// registrationEventLog records fixed event kinds and the whitelisted SQLSTATE
// and constraint label observed from a real PostgreSQL rejection. It never
// records raw SQL, arguments, or tool output.
type registrationEventLog struct {
	mu sync.Mutex

	events         []string
	insertAttempts int
	selectCalls    int
	sqlState       string
	constraint     string
}

func (log *registrationEventLog) record(kind string) {
	log.mu.Lock()
	log.events = append(log.events, kind)
	log.mu.Unlock()
}

func (log *registrationEventLog) recordInsert() {
	log.mu.Lock()
	log.insertAttempts++
	log.events = append(log.events, "insert")
	log.mu.Unlock()
}

func (log *registrationEventLog) recordSelect() {
	log.mu.Lock()
	log.selectCalls++
	log.events = append(log.events, "select")
	log.mu.Unlock()
}

func (log *registrationEventLog) captureSQLState(sqlState, constraint string) {
	log.mu.Lock()
	log.sqlState = sqlState
	log.constraint = constraint
	log.mu.Unlock()
}

func (log *registrationEventLog) snapshot() (events []string, inserts, selects int, sqlState, constraint string) {
	log.mu.Lock()
	defer log.mu.Unlock()
	return append([]string(nil), log.events...), log.insertAttempts, log.selectCalls, log.sqlState, log.constraint
}

// registrationRendezvous makes the competing INSERT overlap deterministic
// without sleeping. Whichever transaction wins the unique-key race waits for
// the other transaction to enter its INSERT before it commits, so the losing
// INSERT is already in flight when the winner commits.
type registrationRendezvous struct {
	mu           sync.Mutex
	entries      int
	secondInsert chan struct{}
	closeOnce    sync.Once
}

func newRegistrationRendezvous() *registrationRendezvous {
	return &registrationRendezvous{secondInsert: make(chan struct{})}
}

func (rendezvous *registrationRendezvous) enterInsert() {
	rendezvous.mu.Lock()
	rendezvous.entries++
	reached := rendezvous.entries >= 2
	rendezvous.mu.Unlock()
	if reached {
		rendezvous.closeOnce.Do(func() { close(rendezvous.secondInsert) })
	}
}

func (rendezvous *registrationRendezvous) waitForPeerInsert(ctx context.Context) {
	select {
	case <-rendezvous.secondInsert:
	case <-ctx.Done():
	}
}

type registrationTestDB struct {
	pool *pgxpool.Pool
	log  *registrationEventLog

	// collideCandidateID is test-only. When set, the wrapper substitutes it for
	// the INSERT's candidate order ID so PostgreSQL raises a real primary-key
	// collision. Production code still generates the candidate ID itself.
	collideCandidateID string
	rendezvous         *registrationRendezvous
}

func newRegistrationTestDB(pool *pgxpool.Pool) *registrationTestDB {
	return &registrationTestDB{pool: pool, log: &registrationEventLog{}}
}

func (db *registrationTestDB) BeginTx(ctx context.Context, opts pgx.TxOptions) (pgx.Tx, error) {
	db.log.record("begin")
	tx, err := db.pool.BeginTx(ctx, opts)
	if err != nil {
		return nil, err
	}
	return &registrationTestTx{Tx: tx, db: db}, nil
}

func (db *registrationTestDB) QueryRow(ctx context.Context, sql string, args ...any) pgx.Row {
	db.log.recordSelect()
	return db.pool.QueryRow(ctx, sql, args...)
}

type registrationTestTx struct {
	pgx.Tx
	db *registrationTestDB
}

func (tx *registrationTestTx) QueryRow(ctx context.Context, sql string, args ...any) pgx.Row {
	if !registrationIsOrderInsert(sql) {
		tx.db.log.record("tx_query")
		return tx.Tx.QueryRow(ctx, sql, args...)
	}

	tx.db.log.recordInsert()
	argsCopy := append([]any(nil), args...)
	if tx.db.collideCandidateID != "" {
		argsCopy[0] = tx.db.collideCandidateID
	}
	if tx.db.rendezvous != nil {
		tx.db.rendezvous.enterInsert()
	}
	row := tx.Tx.QueryRow(ctx, sql, argsCopy...)
	return &registrationTestRow{Row: row, log: tx.db.log}
}

func (tx *registrationTestTx) Commit(ctx context.Context) error {
	if tx.db.rendezvous != nil {
		tx.db.rendezvous.waitForPeerInsert(ctx)
	}
	err := tx.Tx.Commit(ctx)
	if err != nil {
		tx.db.log.record("commit_error")
		return err
	}
	tx.db.log.record("commit")
	return nil
}

func (tx *registrationTestTx) Rollback(ctx context.Context) error {
	tx.db.log.record("rollback")
	return tx.Tx.Rollback(ctx)
}

type registrationTestRow struct {
	pgx.Row
	log *registrationEventLog
}

func (row *registrationTestRow) Scan(dest ...any) error {
	err := row.Row.Scan(dest...)
	if err != nil {
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) {
			row.log.captureSQLState(pgErr.Code, pgErr.ConstraintName)
		}
	}
	return err
}

func registrationIsOrderInsert(sql string) bool {
	return strings.Contains(strings.ToLower(sql), "insert into payment_orders")
}

type registrationCall struct {
	requestedAmount int64
	result          payment.RegisterOrderResult
	err             error
}

func registrationRunConcurrent(t *testing.T, store *postgres.Store, clientID, businessOrderNo string, amounts []int64) []registrationCall {
	t.Helper()

	start := make(chan struct{})
	results := make(chan registrationCall, len(amounts))
	var wait sync.WaitGroup
	for _, amount := range amounts {
		amount := amount
		wait.Add(1)
		go func() {
			defer wait.Done()
			<-start
			ctx, cancel := context.WithTimeout(context.Background(), committedTestBudget)
			defer cancel()
			result, err := store.RegisterOrder(ctx, payment.RegisterOrderInput{
				ClientID:        clientID,
				BusinessOrderNo: businessOrderNo,
				Amount:          amount,
				Currency:        "CNY",
			})
			results <- registrationCall{requestedAmount: amount, result: result, err: err}
		}()
	}
	close(start)
	wait.Wait()
	close(results)

	calls := make([]registrationCall, 0, len(amounts))
	for call := range results {
		calls = append(calls, call)
	}
	return calls
}

func registrationRequireConcurrentOutcomes(t *testing.T, calls []registrationCall) (created, reused registrationCall) {
	t.Helper()
	if len(calls) != 2 {
		t.Fatalf("expected two concurrent registration results, got %d", len(calls))
	}

	createdCount := 0
	for _, call := range calls {
		if call.err != nil {
			t.Fatalf("concurrent registration returned %s", committedTestErrorClass(call.err))
		}
		switch call.result.Outcome {
		case payment.RegisterOutcomeCreated:
			createdCount++
			created = call
		case payment.RegisterOutcomeReused:
			reused = call
		default:
			t.Fatalf("concurrent registration returned an unreliable outcome")
		}
	}
	if createdCount != 1 {
		t.Fatalf("concurrent registration returned %d created outcomes, want exactly 1", createdCount)
	}
	if reused.result.Outcome != payment.RegisterOutcomeReused {
		t.Fatalf("concurrent registration returned no reused outcome")
	}
	if created.result.Order.ID == "" || reused.result.Order.ID != created.result.Order.ID {
		t.Fatalf("concurrent registration did not converge on one payment order")
	}
	return created, reused
}

func registrationRequireUnknownOrder(t *testing.T, order payment.PaymentOrder, label string) {
	t.Helper()
	if order.Result != payment.ResultUnknown {
		t.Fatalf("%s did not keep the explicit UNKNOWN result", label)
	}
	if order.ConfirmedAt != nil {
		t.Fatalf("%s invented a confirmation time", label)
	}
	if order.MockTransactionID != nil {
		t.Fatalf("%s invented a mock transaction ID", label)
	}
}

func registrationRequireEventPrefix(t *testing.T, events []string, want ...string) {
	t.Helper()
	if len(events) < len(want) {
		t.Fatalf("registration event sequence was too short: %v", events)
	}
	for index := range want {
		if events[index] != want[index] {
			t.Fatalf("registration event sequence did not end the insert transaction before the independent read: %v", events)
		}
	}
}

func TestOrderRegistrationConcurrencyIntegration(t *testing.T) {
	observer := harness.OpenCommittedPostgresPool(t)
	writer := harness.OpenCommittedPostgresPool(t)

	t.Run("simultaneous_identical_requests", func(t *testing.T) {
		clientID := committedTestNewClientID()
		businessOrderNo := committedTestNewBusinessOrderNo()
		committedTestCleanup(t, observer, clientID)

		db := newRegistrationTestDB(writer)
		db.rendezvous = newRegistrationRendezvous()
		control := &committedTestControl{}
		store := postgres.NewStore(db, postgres.NewHooks(control))

		calls := registrationRunConcurrent(t, store, clientID, businessOrderNo, []int64{1500, 1500})
		created, reused := registrationRequireConcurrentOutcomes(t, calls)

		registrationRequireUnknownOrder(t, created.result.Order, "created order")
		registrationRequireUnknownOrder(t, reused.result.Order, "reused order")
		if created.result.Order.Amount != 1500 || reused.result.Order.Amount != 1500 {
			t.Fatalf("simultaneous identical requests did not preserve the requested amount")
		}
		if got := control.registeredCalls.Load(); got != 1 {
			t.Fatalf("postcommit registration eligibility fired %d times, want only the created request", got)
		}

		row, found := committedTestReadOrderByBusinessKey(t, observer, clientID, businessOrderNo)
		if !found {
			t.Fatalf("simultaneous identical requests did not persist the order")
		}
		if row.ID != string(created.result.Order.ID) || row.Amount != 1500 || row.Currency != "CNY" {
			t.Fatalf("simultaneous identical requests did not converge on the original facts")
		}
		if row.Result != string(payment.ResultUnknown) || row.ConfirmedAt != nil || row.MockTransactionID != nil {
			t.Fatalf("simultaneous identical requests changed the original UNKNOWN facts")
		}
		if got := committedTestCountOrdersByBusinessKey(t, observer, clientID, businessOrderNo); got != 1 {
			t.Fatalf("simultaneous identical requests created %d orders, want 1", got)
		}
	})

	t.Run("simultaneous_conflicting_amounts", func(t *testing.T) {
		clientID := committedTestNewClientID()
		businessOrderNo := committedTestNewBusinessOrderNo()
		committedTestCleanup(t, observer, clientID)

		db := newRegistrationTestDB(writer)
		db.rendezvous = newRegistrationRendezvous()
		control := &committedTestControl{}
		store := postgres.NewStore(db, postgres.NewHooks(control))

		calls := registrationRunConcurrent(t, store, clientID, businessOrderNo, []int64{1000, 2000})
		created, reused := registrationRequireConcurrentOutcomes(t, calls)

		if created.result.Order.Amount != created.requestedAmount {
			t.Fatalf("created request did not preserve its own amount")
		}
		if reused.result.Order.Amount != created.result.Order.Amount {
			t.Fatalf("reused request did not return the creator's original amount")
		}
		if reused.result.Order.Amount == reused.requestedAmount {
			t.Fatalf("conflicting request overwrote the original amount")
		}
		registrationRequireUnknownOrder(t, reused.result.Order, "reused order")
		if got := control.registeredCalls.Load(); got != 1 {
			t.Fatalf("postcommit registration eligibility fired %d times, want only the created request", got)
		}

		row, found := committedTestReadOrderByBusinessKey(t, observer, clientID, businessOrderNo)
		if !found {
			t.Fatalf("simultaneous conflicting requests did not persist the order")
		}
		if row.ID != string(created.result.Order.ID) || row.Amount != created.result.Order.Amount || row.Currency != "CNY" {
			t.Fatalf("simultaneous conflicting requests did not preserve the creator's facts")
		}
		if row.Result != string(payment.ResultUnknown) || row.ConfirmedAt != nil || row.MockTransactionID != nil {
			t.Fatalf("simultaneous conflicting requests changed the original UNKNOWN facts")
		}
		if got := committedTestCountOrdersByBusinessKey(t, observer, clientID, businessOrderNo); got != 1 {
			t.Fatalf("simultaneous conflicting requests created %d orders, want 1", got)
		}
	})

	t.Run("business_conflict_reads_after_transaction_end", func(t *testing.T) {
		clientID := committedTestNewClientID()
		businessOrderNo := committedTestNewBusinessOrderNo()
		committedTestCleanup(t, observer, clientID)

		existing := committedTestNewUnknownOrder(clientID, businessOrderNo, 1000)
		committedTestInsertOrder(t, observer, existing)

		db := newRegistrationTestDB(writer)
		control := &committedTestControl{}
		store := postgres.NewStore(db, postgres.NewHooks(control))

		ctx, cancel := committedTestContext(t)
		result, err := store.RegisterOrder(ctx, payment.RegisterOrderInput{
			ClientID:        clientID,
			BusinessOrderNo: businessOrderNo,
			Amount:          2000,
			Currency:        "CNY",
		})
		cancel()
		if err != nil {
			t.Fatalf("business conflict returned %s", committedTestErrorClass(err))
		}
		if result.Outcome != payment.RegisterOutcomeReused {
			t.Fatalf("business conflict did not return a reused outcome")
		}
		if result.Order.ID != existing.ID || result.Order.Amount != 1000 || result.Order.Currency != "CNY" {
			t.Fatalf("business conflict did not return the original facts")
		}
		registrationRequireUnknownOrder(t, result.Order, "reused order")
		if got := control.registeredCalls.Load(); got != 0 {
			t.Fatalf("reused UNKNOWN order was granted postcommit registration eligibility")
		}

		events, inserts, selects, _, _ := db.log.snapshot()
		registrationRequireEventPrefix(t, events, "begin", "insert", "rollback", "select")
		if inserts != 1 || selects != 1 {
			t.Fatalf("business conflict performed %d inserts and %d independent reads, want 1 and 1", inserts, selects)
		}

		row, found := committedTestReadOrderByBusinessKey(t, observer, clientID, businessOrderNo)
		if !found || row.ID != string(existing.ID) || row.Amount != 1000 || row.Currency != "CNY" {
			t.Fatalf("business conflict changed the original row")
		}
		if got := committedTestCountOrdersByBusinessKey(t, observer, clientID, businessOrderNo); got != 1 {
			t.Fatalf("business conflict produced %d orders, want 1", got)
		}
	})

	t.Run("non_business_primary_key_collision", func(t *testing.T) {
		clientID := committedTestNewClientID()
		existingBusinessOrderNo := committedTestNewBusinessOrderNo()
		newBusinessOrderNo := committedTestNewBusinessOrderNo()
		committedTestCleanup(t, observer, clientID)

		existing := committedTestNewUnknownOrder(clientID, existingBusinessOrderNo, 1000)
		committedTestInsertOrder(t, observer, existing)

		db := newRegistrationTestDB(writer)
		db.collideCandidateID = string(existing.ID)
		control := &committedTestControl{}
		store := postgres.NewStore(db, postgres.NewHooks(control))

		ctx, cancel := committedTestContext(t)
		result, err := store.RegisterOrder(ctx, payment.RegisterOrderInput{
			ClientID:        clientID,
			BusinessOrderNo: newBusinessOrderNo,
			Amount:          2000,
			Currency:        "CNY",
		})
		cancel()

		if err == nil {
			t.Fatalf("real primary-key collision was not rejected")
		}

		events, inserts, selects, sqlState, constraint := db.log.snapshot()
		if inserts != 1 {
			t.Fatalf("real primary-key collision attempted %d inserts, want 1", inserts)
		}
		if selects != 0 {
			t.Fatalf("real primary-key collision performed %d independent reuse reads, want 0", selects)
		}
		if sqlState != "23505" {
			t.Fatalf("real primary-key collision returned SQLSTATE %s, want 23505", sqlState)
		}
		if constraint != "payment_orders_pkey" {
			t.Fatalf("real primary-key collision hit unexpected constraint %s", constraint)
		}
		t.Logf("real primary-key collision SQLSTATE=%s constraint=%s", sqlState, constraint)

		if !errors.Is(err, payment.ErrRegistrationRejected) {
			t.Fatalf("real non-business unique error was not classified as registration rejected")
		}
		if errors.Is(err, payment.ErrConflict) {
			t.Fatalf("real non-business unique error was misclassified as an intent conflict")
		}
		if errors.Is(err, payment.ErrUnavailable) {
			t.Fatalf("real non-business unique error lost its definite rejection classification")
		}
		if result.Outcome == payment.RegisterOutcomeCreated || result.Outcome == payment.RegisterOutcomeReused {
			t.Fatalf("real non-business unique error was reported as a reliable registration outcome")
		}
		if result.Order.ID != "" {
			t.Fatalf("real non-business unique error returned a candidate order fact")
		}
		if got := control.registeredCalls.Load(); got != 0 {
			t.Fatalf("real non-business unique error was granted registration eligibility")
		}

		registrationRequireEventPrefix(t, events, "begin", "insert")
		if got := committedTestCountOrdersByBusinessKey(t, observer, clientID, newBusinessOrderNo); got != 0 {
			t.Fatalf("real primary-key collision left %d new order rows, want 0", got)
		}
		row, found := committedTestReadOrder(t, observer, existing.ID)
		if !found || row.Amount != 1000 || row.Result != string(payment.ResultUnknown) {
			t.Fatalf("real primary-key collision changed the existing order")
		}
	})
}
