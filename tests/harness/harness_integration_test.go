//go:build integration

package harness

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"payment-hub/internal/payment"
)

// This experiment proves only the T020 harness mechanism and parent-state
// retention: a real committed UNKNOWN row, one parent observer/socket/run, two
// real re-exec helper processes with new generations, and safe artifacts. It
// does not claim a real application restart or business repository behavior.
func TestHarnessProcessPreservesParentStateIntegration(t *testing.T) {
	writer, runID := commitHookTestOpen(t)
	reader, readerRunID := commitHookTestOpen(t)
	if writer == reader {
		t.Fatal("t020 integration did not obtain two independent database connections")
	}
	if runID != readerRunID {
		t.Fatal("t020 integration connections did not agree on the run ID")
	}
	epoch := commitHookTestNewEpoch(t)

	orderID := payment.PaymentOrderID(payment.NewUUIDv4())
	clientID := "t020-" + payment.NewUUIDv4()
	businessOrderNo := "t020-order-" + payment.NewUUIDv4()
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
			t.Errorf("t020 integration row cleanup failed (SQLSTATE %s)", commitHookTestSQLState(err))
			return
		}
		if committed && tag.RowsAffected() != 1 {
			t.Errorf("t020 integration row cleanup did not remove exactly one row")
		}
		var residual bool
		if err := reader.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM payment_orders WHERE id = $1)`, string(orderID)).Scan(&residual); err != nil {
			t.Errorf("t020 integration row cleanup verification failed (SQLSTATE %s)", commitHookTestSQLState(err))
			return
		}
		if residual {
			t.Errorf("t020 integration row cleanup left a residual row")
		}
	})

	commitHookTestExec(t, writer, "t020 integration begin", "BEGIN")
	txOpen := true
	defer func() {
		if !txOpen {
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), commitHookTestCleanupBudget)
		defer cancel()
		if _, err := writer.Exec(ctx, "ROLLBACK"); err != nil {
			t.Errorf("t020 integration rollback failed (SQLSTATE %s)", commitHookTestSQLState(err))
		}
	}()
	insertCtx, cancelInsert := context.WithTimeout(context.Background(), commitHookTestSQLBudget)
	_, err := writer.Exec(insertCtx, commitHookTestInsertSQL,
		string(orderID), clientID, businessOrderNo, commitHookTestAmount, "CNY",
		string(payment.ResultUnknown), nil, registeredAt, nil,
	)
	cancelInsert()
	if err != nil {
		t.Fatalf("t020 integration insert failed (SQLSTATE %s)", commitHookTestSQLState(err))
	}
	inserted = true
	commitHookTestExec(t, writer, "t020 integration commit", "COMMIT")
	txOpen = false
	committed = true
	if writer.PgConn().TxStatus() != 'I' {
		t.Fatal("t020 integration writer transaction did not commit")
	}
	if reader.PgConn().TxStatus() != 'I' {
		t.Fatal("t020 integration reader unexpectedly has an open transaction")
	}

	repoRoot, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal("t020 integration repo root resolution failed")
	}
	if _, err := os.Stat(filepath.Join(repoRoot, "go.mod")); err != nil {
		t.Fatal("t020 integration repo root is not the project root")
	}
	root := t020TempDir(t)
	artifacts, reportPath := t020BeginArtifactsAt(t, root, repoRoot, runID)

	observerDir := t020TempDir(t)
	socket := filepath.Join(observerDir, "s")
	observerCtx, cancelObserver := context.WithTimeout(context.Background(), 5*time.Second)
	observer, err := StartObserver(observerCtx, ObserverConfig{
		Socket:         socket,
		RunID:          runID,
		Epoch:          epoch,
		BarrierTimeout: 5 * time.Second,
	})
	cancelObserver()
	if err != nil {
		t.Fatal("t020 integration observer start failed")
	}
	t.Cleanup(func() {
		closeCtx, cancelClose := context.WithTimeout(context.Background(), commitHookTestCleanupBudget)
		defer cancelClose()
		if err := observer.Close(closeCtx); err != nil {
			t.Errorf("t020 integration observer close failed")
		}
	})
	before := commitHookTestRead(t, reader, orderID)
	commitHookTestVerifyRow(t, before, string(orderID), clientID, businessOrderNo, registeredAt)

	for _, generation := range []uint64{201, 202} {
		if err := observer.Expect(generation); err != nil {
			t.Fatal("t020 integration observer expect generation failed")
		}
		exit, result := t020RunObserverHelper(t, artifacts, socket, runID, epoch, generation, string(orderID))
		t020RequireExit(t, exit, generation, 0, false, false, false)
		if result.Generation != generation || result.LastSeq != 2 ||
			result.RunID != runID || result.Epoch != epoch {
			t.Fatal("t020 integration helper observer receipt mismatch")
		}

		counts, err := observer.Snapshot()
		if err != nil {
			t.Fatal("t020 integration observer snapshot failed")
		}
		want := generation - 200
		if counts.Payment != want || counts.Query != want {
			t.Fatal("t020 integration observer counts mismatch")
		}

		after := commitHookTestRead(t, reader, orderID)
		commitHookTestVerifyRow(t, after, string(orderID), clientID, businessOrderNo, registeredAt)
		if !commitHookTestRowsEqual(before, after) {
			t.Fatal("t020 integration helper changed the committed row")
		}
	}

	if err := artifacts.RecordObserver(observer); err != nil {
		t.Fatal("t020 integration record observer failed")
	}
	path, err := artifacts.Finish()
	if err != nil {
		t.Fatal("t020 integration finish artifacts failed")
	}
	if path != reportPath {
		t.Fatal("t020 integration report path mismatch")
	}
	report, data := t020ReadReport(t, path)
	if !report.CollectionComplete || len(report.Faults) != 0 || len(report.Processes) != 2 || len(report.Logs) != 2 {
		t.Fatal("t020 integration report is incomplete")
	}
	if report.Observer == nil || report.Observer.Counts.Payment != 2 ||
		report.Observer.Counts.Query != 2 || len(report.Observer.Receipts) != 2 {
		t.Fatal("t020 integration observer report mismatch")
	}
	for _, receipt := range report.Observer.Receipts {
		if receipt.RunID != runID || receipt.Epoch != epoch || receipt.LastSeq != 2 {
			t.Fatal("t020 integration observer receipt mismatch")
		}
	}
	generations := map[uint64]bool{}
	for _, process := range report.Processes {
		generations[process.Generation] = true
		if !process.Waited || !process.Reaped || !process.OutputComplete || !process.GroupGone {
			t.Fatal("t020 integration process resources are not confirmed")
		}
	}
	if !generations[201] || !generations[202] {
		t.Fatal("t020 integration process generations mismatch")
	}
	if !report.Source.Unchanged || report.Source.ManifestDigest == "" || len(report.Source.Manifest) == 0 {
		t.Fatal("t020 integration source manifest is incomplete")
	}
	if bytes.Contains(data, []byte(socket)) ||
		bytes.Contains(data, []byte(clientID)) ||
		bytes.Contains(data, []byte(businessOrderNo)) ||
		bytes.Contains(data, []byte(string(orderID))) {
		t.Fatal("t020 integration report leaked runtime or database identifiers")
	}

	final := commitHookTestRead(t, reader, orderID)
	commitHookTestVerifyRow(t, final, string(orderID), clientID, businessOrderNo, registeredAt)
	if !commitHookTestRowsEqual(before, final) {
		t.Fatal("t020 integration final committed row facts changed")
	}
}
