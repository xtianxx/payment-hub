//go:build integration

package harness

// This experiment proves only the T019 test seam: a real committed UNKNOWN row,
// a real app test-mode postgres hook, a real private UDS barrier, and controlled
// reply_hidden. It does not implement or claim T029/T030/T032 business wiring,
// real network isolation, or production result/reason atomicity.

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"payment-hub/internal/app"
	"payment-hub/internal/payment"
	"payment-hub/internal/postgres"
	"payment-hub/internal/testcontrol"
)

const (
	commitHookTestRunIDVariable = "PAYMENT_HUB_TEST_RUN_ID"
	commitHookTestGen           = uint64(7)
	commitHookTestAmount        = int64(1234)
	commitHookTestBudget        = 5 * time.Second
	commitHookTestSQLBudget     = 5 * time.Second
	commitHookTestConnectBudget = 5 * time.Second
	commitHookTestVerifyBudget  = 5 * time.Second
	commitHookTestCleanupBudget = 3 * time.Second
)

const commitHookTestInsertSQL = `INSERT INTO payment_orders
	(id, client_id, business_order_no, amount, currency, result, mock_transaction_id, registered_at, confirmed_at)
	VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`

type commitHookTestRow struct {
	id                string
	clientID          string
	businessOrderNo   string
	amount            int64
	currency          string
	result            string
	mockTransactionID *string
	registeredAt      time.Time
	confirmedAt       *time.Time
}

func commitHookTestSQLState(err error) string {
	if err == nil {
		return "none"
	}
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code
	}
	return "none"
}

func commitHookTestErrorCategory(err error) string {
	if err == nil {
		return "none"
	}
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return "SQLSTATE " + pgErr.Code
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return "timeout"
	}
	if errors.Is(err, context.Canceled) {
		return "canceled"
	}
	return "connection error"
}

func commitHookTestOpen(t *testing.T) (*pgx.Conn, string) {
	t.Helper()
	config := openPostgresConfig(t)
	runID := os.Getenv(commitHookTestRunIDVariable)

	connectCtx, cancelConnect := context.WithTimeout(context.Background(), commitHookTestConnectBudget)
	conn, err := pgx.ConnectConfig(connectCtx, config)
	cancelConnect()
	if err != nil {
		t.Fatalf("commit hook test connection failed (%s)", commitHookTestErrorCategory(err))
	}
	// Register bounded close before UTC/verify so a Fatal in either step still
	// closes the successfully opened connection.
	t.Cleanup(func() {
		closeCtx, cancelClose := context.WithTimeout(context.Background(), commitHookTestCleanupBudget)
		defer cancelClose()
		if err := conn.Close(closeCtx); err != nil {
			t.Errorf("commit hook test connection close failed (%s)", commitHookTestErrorCategory(err))
		}
	})

	configureUTCTimestamps(t, conn)

	verifyCtx, cancelVerify := context.WithTimeout(context.Background(), commitHookTestVerifyBudget)
	check, err := verifyPostgres(conn, verifyCtx, config.Database)
	cancelVerify()
	if err != nil {
		t.Fatalf("commit hook test verification failed: %s (%s)", check, commitHookTestErrorCategory(err))
	}
	if check != "" {
		t.Fatalf("commit hook test verification failed: %s", check)
	}
	if conn.PgConn().TxStatus() != 'I' {
		t.Fatalf("commit hook test connection unexpectedly has an open transaction")
	}
	return conn, runID
}

func commitHookTestNewEpoch(t *testing.T) string {
	t.Helper()
	var random [16]byte
	if _, err := rand.Read(random[:]); err != nil {
		t.Fatalf("commit hook test epoch generation failed")
	}
	return hex.EncodeToString(random[:])
}

func commitHookTestExec(t *testing.T, conn *pgx.Conn, label, query string, args ...any) pgconn.CommandTag {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), commitHookTestSQLBudget)
	defer cancel()
	tag, err := conn.Exec(ctx, query, args...)
	if err != nil {
		t.Fatalf("%s failed (SQLSTATE %s)", label, commitHookTestSQLState(err))
	}
	return tag
}

func commitHookTestRead(t *testing.T, conn *pgx.Conn, orderID payment.PaymentOrderID) commitHookTestRow {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), commitHookTestSQLBudget)
	defer cancel()
	var row commitHookTestRow
	err := conn.QueryRow(ctx, `
		SELECT id::text, client_id, business_order_no, amount, currency, result,
		       mock_transaction_id::text, registered_at, confirmed_at
		FROM payment_orders
		WHERE id = $1
	`, string(orderID)).Scan(
		&row.id, &row.clientID, &row.businessOrderNo, &row.amount, &row.currency,
		&row.result, &row.mockTransactionID, &row.registeredAt, &row.confirmedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		t.Fatalf("commit hook test committed order row is not visible")
	}
	if err != nil {
		t.Fatalf("commit hook test committed order read failed (SQLSTATE %s)", commitHookTestSQLState(err))
	}
	return row
}

func commitHookTestVerifyRow(t *testing.T, row commitHookTestRow, orderID, clientID, businessOrderNo string, registeredAt time.Time) {
	t.Helper()
	if row.id != orderID || row.clientID != clientID || row.businessOrderNo != businessOrderNo {
		t.Fatalf("commit hook test committed order identity does not match the inserted fact")
	}
	if row.amount != commitHookTestAmount {
		t.Fatalf("commit hook test committed order amount does not match the inserted fact")
	}
	if row.currency != "CNY" {
		t.Fatalf("commit hook test committed order currency is not CNY")
	}
	if row.result != string(payment.ResultUnknown) {
		t.Fatalf("commit hook test committed order result is not UNKNOWN")
	}
	if row.mockTransactionID != nil {
		t.Fatalf("commit hook test committed UNKNOWN order unexpectedly has a mock transaction")
	}
	if row.confirmedAt != nil {
		t.Fatalf("commit hook test committed UNKNOWN order unexpectedly has a confirmation time")
	}
	if _, offset := row.registeredAt.Zone(); offset != 0 {
		t.Fatalf("commit hook test committed order registered_at is not UTC")
	}
	if diff := row.registeredAt.Sub(registeredAt); diff < -time.Microsecond || diff > time.Microsecond {
		t.Fatalf("commit hook test committed order registered_at does not match the inserted fact")
	}
}

func commitHookTestStringPtrEqual(a, b *string) bool {
	if a == nil || b == nil {
		return a == b
	}
	return *a == *b
}

func commitHookTestTimePtrEqual(a, b *time.Time) bool {
	if a == nil || b == nil {
		return a == b
	}
	return a.Equal(*b)
}

func commitHookTestRowsEqual(a, b commitHookTestRow) bool {
	if a.id != b.id || a.clientID != b.clientID || a.businessOrderNo != b.businessOrderNo ||
		a.amount != b.amount || a.currency != b.currency || a.result != b.result {
		return false
	}
	if !commitHookTestStringPtrEqual(a.mockTransactionID, b.mockTransactionID) {
		return false
	}
	if !commitHookTestTimePtrEqual(a.confirmedAt, b.confirmedAt) {
		return false
	}
	return a.registeredAt.Equal(b.registeredAt)
}

func TestCommitHookIntegrationHidesReplyAfterRealCommit(t *testing.T) {
	writer, runID := commitHookTestOpen(t)
	reader, readerRunID := commitHookTestOpen(t)
	if writer == reader {
		t.Fatalf("commit hook test did not obtain two independent database connections")
	}
	if runID != readerRunID {
		t.Fatalf("commit hook test connections did not agree on the run ID")
	}
	epoch := commitHookTestNewEpoch(t)

	orderID := payment.PaymentOrderID(payment.NewUUIDv4())
	clientID := "ch-" + payment.NewUUIDv4()
	businessOrderNo := "ch-order-" + payment.NewUUIDv4()
	registeredAt := time.Now().UTC().Truncate(time.Microsecond)
	inserted := false
	committed := false

	// Register exact cleanup for this run's only row before any insert.
	t.Cleanup(func() {
		if !inserted {
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), commitHookTestCleanupBudget)
		defer cancel()
		tag, err := reader.Exec(ctx, `DELETE FROM payment_orders WHERE id = $1`, string(orderID))
		if err != nil {
			t.Errorf("commit hook test row cleanup failed (SQLSTATE %s)", commitHookTestSQLState(err))
			return
		}
		if committed && tag.RowsAffected() != 1 {
			t.Errorf("commit hook test row cleanup did not remove exactly one row")
		}
		var residual bool
		if err := reader.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM payment_orders WHERE id = $1)`, string(orderID)).Scan(&residual); err != nil {
			t.Errorf("commit hook test row cleanup verification failed (SQLSTATE %s)", commitHookTestSQLState(err))
			return
		}
		if residual {
			t.Errorf("commit hook test row cleanup left a residual row")
		}
	})

	dir, err := os.MkdirTemp("", "phch-")
	if err != nil {
		t.Fatalf("commit hook test observer directory creation failed")
	}
	if err := os.Chmod(dir, 0o700); err != nil {
		t.Fatalf("commit hook test observer directory securing failed")
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(dir); err != nil {
			t.Errorf("commit hook test observer directory removal failed")
		}
	})
	socket := filepath.Join(dir, "s")

	var obs *Observer
	var tm *app.TestMode
	var hookDone chan error
	hookFinished := false
	t.Cleanup(func() {
		if hookDone != nil && !hookFinished {
			select {
			case <-hookDone:
			case <-time.After(commitHookTestBudget):
				t.Errorf("commit hook test hook goroutine did not stop")
			}
		}
	})
	t.Cleanup(func() {
		if tm != nil {
			if err := tm.Close(); err != nil {
				t.Errorf("commit hook test mode close failed")
			}
		}
		if obs != nil {
			closeCtx, cancelClose := context.WithTimeout(context.Background(), commitHookTestCleanupBudget)
			defer cancelClose()
			if err := obs.Close(closeCtx); err != nil {
				t.Errorf("commit hook test observer close failed")
			}
		}
	})

	observerCtx, cancelObserver := context.WithTimeout(context.Background(), commitHookTestBudget)
	obs, err = StartObserver(observerCtx, ObserverConfig{
		Socket:         socket,
		RunID:          runID,
		Epoch:          epoch,
		BarrierTimeout: commitHookTestBudget,
	})
	cancelObserver()
	if err != nil {
		t.Fatalf("commit hook test observer start failed")
	}
	if err := obs.Expect(commitHookTestGen); err != nil {
		t.Fatalf("commit hook test observer expect failed")
	}

	// The SQL transaction is real and explicit. No postcommit hook is called
	// unless BEGIN, INSERT, and COMMIT have all succeeded.
	commitHookTestExec(t, writer, "commit hook test begin", "BEGIN")
	txOpen := true
	defer func() {
		if !txOpen {
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), commitHookTestCleanupBudget)
		defer cancel()
		if _, err := writer.Exec(ctx, "ROLLBACK"); err != nil {
			t.Errorf("commit hook test rollback failed (SQLSTATE %s)", commitHookTestSQLState(err))
		}
	}()
	insertCtx, cancelInsert := context.WithTimeout(context.Background(), commitHookTestSQLBudget)
	_, err = writer.Exec(insertCtx, commitHookTestInsertSQL,
		string(orderID), clientID, businessOrderNo, commitHookTestAmount, "CNY",
		string(payment.ResultUnknown), nil, registeredAt, nil,
	)
	cancelInsert()
	if err != nil {
		t.Fatalf("commit hook test insert failed (SQLSTATE %s)", commitHookTestSQLState(err))
	}
	inserted = true
	commitHookTestExec(t, writer, "commit hook test commit", "COMMIT")
	txOpen = false
	committed = true
	if writer.PgConn().TxStatus() != 'I' {
		t.Fatalf("commit hook test writer transaction did not commit")
	}
	if reader.PgConn().TxStatus() != 'I' {
		t.Fatalf("commit hook test reader unexpectedly has an open transaction")
	}

	openCtx, cancelOpen := context.WithTimeout(context.Background(), commitHookTestBudget)
	tm, err = app.OpenTestMode(openCtx, app.TestModeConfig{Enabled: true, Session: testcontrol.Session{
		Socket:     socket,
		RunID:      runID,
		Epoch:      epoch,
		Generation: commitHookTestGen,
	}})
	cancelOpen()
	if err != nil {
		t.Fatalf("commit hook test mode open failed")
	}
	if tm == nil {
		t.Fatalf("commit hook test mode is nil")
	}
	hooks := tm.PostgresHooks()
	if hooks == nil {
		t.Fatalf("commit hook test postgres hooks are nil")
	}

	barrier, err := obs.Arm(commitHookTestGen, testcontrol.PointRegistered, string(orderID))
	if err != nil {
		t.Fatalf("commit hook test barrier arm failed")
	}
	hookCtx, cancelHook := context.WithTimeout(context.Background(), commitHookTestBudget)
	defer cancelHook()
	hookDone = make(chan error, 1)
	go func() {
		hookDone <- hooks.Registered(hookCtx, orderID)
	}()
	receipt, err := barrier.Wait(hookCtx)
	if err != nil {
		t.Fatalf("commit hook test barrier wait failed")
	}
	if receipt.LastSeq == 0 {
		t.Fatalf("commit hook test barrier receipt has no reach sequence")
	}
	if receipt.RunID != runID || receipt.Epoch != epoch || receipt.Generation != commitHookTestGen {
		t.Fatalf("commit hook test barrier receipt identity mismatch")
	}

	// The row must already be visible through the independent reader while the
	// postcommit hook is still blocked at the UDS barrier.
	before := commitHookTestRead(t, reader, orderID)
	commitHookTestVerifyRow(t, before, string(orderID), clientID, businessOrderNo, registeredAt)

	if err := barrier.Release(testcontrol.ActionReplyHidden); err != nil {
		t.Fatalf("commit hook test reply_hidden release failed")
	}
	select {
	case err := <-hookDone:
		hookFinished = true
		if !errors.Is(err, postgres.ErrReplyHidden) {
			t.Fatalf("commit hook test postgres hooks did not report controlled reply hiding")
		}
	case <-time.After(commitHookTestBudget):
		t.Fatalf("commit hook test postgres hook did not return after release")
	}

	after := commitHookTestRead(t, reader, orderID)
	commitHookTestVerifyRow(t, after, string(orderID), clientID, businessOrderNo, registeredAt)
	if !commitHookTestRowsEqual(before, after) {
		t.Fatalf("commit hook test reply hiding changed committed row facts")
	}

	sealCtx, cancelSeal := context.WithTimeout(context.Background(), commitHookTestBudget)
	sealReceipt, err := tm.Seal(sealCtx)
	cancelSeal()
	if err != nil {
		t.Fatalf("commit hook test seal failed")
	}
	if sealReceipt.LastSeq != 1 {
		t.Fatalf("commit hook test seal did not close the local reach sequence")
	}
	if sealReceipt.RunID != runID || sealReceipt.Epoch != epoch ||
		sealReceipt.Generation != commitHookTestGen {
		t.Fatalf("commit hook test seal receipt identity mismatch")
	}
	counts, err := obs.Snapshot()
	if err != nil {
		t.Fatalf("commit hook test observer snapshot failed")
	}
	if counts.Payment != 0 || counts.Query != 0 {
		t.Fatalf("commit hook test observer counts are not zero")
	}

	if err := tm.Close(); err != nil {
		t.Fatalf("commit hook test mode close failed")
	}
	closeCtx, cancelClose := context.WithTimeout(context.Background(), commitHookTestCleanupBudget)
	if err := obs.Close(closeCtx); err != nil {
		cancelClose()
		t.Fatalf("commit hook test observer close failed")
	}
	cancelClose()
}
