//go:build integration

package tests

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"

	"payment-hub/tests/harness"
)

const (
	lifecycleExitUpperBoundMS = 20000
	lifecycleExitToleranceMS  = 250
	lifecycleHostGraceMS      = 25000
	lifecycleWaiterPoll       = 10 * time.Millisecond
	lifecycleAmount           = 100
)

type lifecycleTable string

const (
	lifecycleTablePaymentOrders         lifecycleTable = "payment_orders"
	lifecycleTableResultHandlingRecords lifecycleTable = "result_handling_records"
)

type lifecycleStatement string

const (
	lifecycleStatementRegistration   lifecycleStatement = "REGISTRATION_INSERT"
	lifecycleStatementResultHandling lifecycleStatement = "RESULT_HANDLING_INSERT"
)

type lifecycleWaiterProof struct {
	HolderPID     int
	WaiterPID     int
	BackendStart  time.Time
	XactStart     *time.Time
	TableOID      uint32
	LockMode      string
	Statement     lifecycleStatement
	WaitEventType string
	WaitEvent     string
}

type lifecyclePGCounts struct {
	Order uint64
	Mock  uint64
	Trace uint64
}

type lifecycleMockRow struct {
	ID                  string
	PlatformReferenceID string
	Amount              int64
	Currency            string
	Result              string
	ProcessedAt         time.Time
}

type lifecycleOrderRow struct {
	ID                string
	ClientID          string
	BusinessOrderNo   string
	Amount            int64
	Currency          string
	Result            string
	MockTransactionID string
	RegisteredAt      time.Time
	ConfirmedAt       *time.Time
}

type lifecycleHTTPOutcome struct {
	Kind        string
	Transport   string
	HasResponse bool
	Status      int
	RequestID   string
	PaymentID   string
	Result      string
	Issues      []string
	Err         error
}

type lifecycleTableLock struct {
	mu          sync.Mutex
	pool        *pgxpool.Pool
	conn        *pgxpool.Conn
	tx          pgx.Tx
	table       lifecycleTable
	relationOID uint32
	holderPID   int
	target      lifecycleWaiterProof
	targetSet   bool
	released    bool
}

type lifecycleStopResult struct {
	exit    harness.Exit
	err     error
	elapsed time.Duration
}

func lifecycleNewScenario(name string) *us3Scenario {
	return &us3Scenario{
		Name:             name,
		Generations:      []uint64{1},
		HTTP:             []us3HTTP{},
		PG:               []us1ReportPG{},
		Calls:            []us3Call{},
		Records:          []us3Record{},
		Queries:          []us3Query{},
		Faults:           []us3Fault{},
		Facts:            us3Facts{Transition: "NONE"},
		Checks:           us3Checks{},
		GenerationProofs: []us3GenerationProof{},
		Budgets:          []us3Budget{},
		Commands:         []us1ReportCommand{},
	}
}

func lifecycleRecordFault(scenario *us3Scenario, kind string) {
	scenario.Faults = append(scenario.Faults, us3Fault{
		Kind: kind,
		Cleanup: us3Cleanup{
			Kind:     "TABLE_SHARE_LOCK",
			Removed:  true,
			Verified: true,
		},
	})
}

func lifecycleRecordCounts(t *testing.T, artifacts *harness.Artifacts, scenario *us3Scenario, counts lifecyclePGCounts, sealed harness.Counts, stableFacts bool) {
	t.Helper()
	us3RecordSQL(t, artifacts, scenario, 1, "ORDER", counts.Order)
	us3RecordSQL(t, artifacts, scenario, 1, "MOCK", counts.Mock)
	us3RecordSQL(t, artifacts, scenario, 1, "TRACE", counts.Trace)
	scenario.Checks.CountsAfter = us3Counts{
		PaymentCalls: sealed.Payment,
		QueryCalls:   sealed.Query,
		OrderRows:    counts.Order,
		MockRows:     counts.Mock,
		TraceRows:    counts.Trace,
	}
	scenario.Checks.CountsExact = true
	scenario.Checks.StableFacts = stableFacts
	scenario.Facts.StableFacts = stableFacts
}

func lifecycleSetCountsBefore(scenario *us3Scenario, counts lifecyclePGCounts) {
	scenario.Checks.CountsBefore = us3Counts{
		OrderRows: counts.Order,
		MockRows:  counts.Mock,
		TraceRows: counts.Trace,
	}
}

func lifecycleSetCancel(scenario *us3Scenario, phase string) {
	scenario.Cancel = &us3Cancel{
		Phase:                  phase,
		PostCancelPaymentCalls: 0,
		PostCancelQueryCalls:   0,
		FinishCheckedMock:      false,
		RollbackClaimed:        false,
	}
}

func lifecycleSetShutdown(scenario *us3Scenario, exit harness.Exit, sealed harness.Counts, elapsed time.Duration) {
	// NoUnmanagedGoroutine is a composite inference from the real terminal
	// GroupGone/OutputComplete fields plus app-unit ordering; it is not a
	// goroutine count. D requires the closed source below.
	scenario.Shutdown = &us3Shutdown{
		PostShutdownPaymentCalls:   sealed.Payment,
		PostShutdownQueryCalls:     sealed.Query,
		RollbackClaimed:            false,
		NoUnmanagedGoroutine:       exit.GroupGone && exit.OutputComplete,
		NoUnmanagedGoroutineSource: "APP_UNIT_PLUS_GROUP_GONE",
		PoolClosedAfterUsers:       true,
		PoolClosedAfterUsersSource: "APP_UNIT_EVIDENCE",
		ExitElapsedMS:              uint64(elapsed / time.Millisecond),
		ExitUpperBoundMS:           lifecycleExitUpperBoundMS,
		ObservationToleranceMS:     lifecycleExitToleranceMS,
		HostGraceMS:                lifecycleHostGraceMS,
		HostGracePassBudget:        false,
	}
}

func lifecycleRecordExitBudgets(t *testing.T, artifacts *harness.Artifacts, scenario *us3Scenario, elapsed time.Duration) {
	t.Helper()
	us3RecordBudget(t, artifacts, scenario, 1, "NORMAL", "EXIT", "APP", true, 20*time.Second, elapsed)
	us3RecordBudget(t, artifacts, scenario, 1, "NORMAL", "PROCESS_GRACE", "HARNESS", false, 25*time.Second, elapsed)
}

func lifecycleRequireExitElapsed(t *testing.T, elapsed time.Duration) {
	t.Helper()
	if elapsed > 20*time.Second+250*time.Millisecond {
		t.Fatalf("lifecycle exit exceeded the frozen bound")
	}
}

func lifecycleSealedCounts(t *testing.T, observer *harness.Observer, wantPayment, wantQuery uint64) harness.Counts {
	t.Helper()
	_, counts, ok := observer.SealedGeneration(1)
	if !ok {
		t.Fatalf("lifecycle generation is not independently sealed")
	}
	if counts.Payment != wantPayment || counts.Query != wantQuery {
		t.Fatalf("lifecycle sealed call counts mismatch")
	}
	return counts
}

func lifecycleRequireCallsMatchChecks(t *testing.T, scenario *us3Scenario, wantPayment, wantQuery uint64) {
	t.Helper()
	if len(scenario.Calls) != 1 {
		t.Fatalf("lifecycle finalizer calls are missing")
	}
	if scenario.Calls[0].PaymentCalls != wantPayment ||
		scenario.Calls[0].QueryCalls != wantQuery {
		t.Fatalf("lifecycle finalizer calls do not match sealed checks")
	}
}

func lifecycleRequireGracefulExit(t *testing.T, exit harness.Exit, err error) {
	t.Helper()
	if !errors.Is(err, harness.ErrProcess) {
		t.Fatalf("lifecycle process stop did not return controlled ErrProcess")
	}
	if exit.Code != 0 || !exit.Canceled || exit.TimedOut || exit.Forced ||
		!exit.Reaped || !exit.OutputComplete || !exit.GroupGone {
		t.Fatalf("lifecycle process terminal proof mismatch")
	}
}

func lifecycleStopAndElapsed(t *testing.T, process *harness.Process) (harness.Exit, time.Duration) {
	t.Helper()
	start := time.Now()
	exit := us1StopProcess(t, process)
	elapsed := time.Since(start)
	if exit.TimedOut {
		t.Fatalf("lifecycle process stop timed out")
	}
	return exit, elapsed
}

func lifecycleBeginStop(process *harness.Process) <-chan lifecycleStopResult {
	ch := make(chan lifecycleStopResult, 1)
	go func() {
		start := time.Now()
		ctx, cancel := context.WithTimeout(context.Background(), us1StopBudget)
		defer cancel()
		exit, err := process.Stop(ctx)
		ch <- lifecycleStopResult{exit: exit, err: err, elapsed: time.Since(start)}
	}()
	return ch
}

func lifecycleAwaitStop(t *testing.T, ch <-chan lifecycleStopResult) (harness.Exit, time.Duration) {
	t.Helper()
	timer := time.NewTimer(us1StopBudget + us1CleanupBudget)
	defer timer.Stop()
	select {
	case got := <-ch:
		lifecycleRequireGracefulExit(t, got.exit, got.err)
		return got.exit, got.elapsed
	case <-timer.C:
		t.Fatalf("lifecycle process stop did not complete")
		return harness.Exit{}, 0
	}
}

func lifecycleAcquireTableShareLock(t *testing.T, pool *pgxpool.Pool, table lifecycleTable) *lifecycleTableLock {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), us1BarrierBudget)
	defer cancel()
	conn, err := pool.Acquire(ctx)
	if err != nil {
		t.Fatalf("lifecycle table lock connection failed")
	}
	tx, err := conn.Begin(ctx)
	if err != nil {
		conn.Release()
		t.Fatalf("lifecycle table lock transaction failed")
	}
	lock := &lifecycleTableLock{pool: pool, conn: conn, tx: tx, table: table}
	if err := tx.QueryRow(ctx, "SELECT pg_backend_pid()").Scan(&lock.holderPID); err != nil {
		_ = tx.Rollback(ctx)
		conn.Release()
		t.Fatalf("lifecycle table lock holder identity failed")
	}
	if err := tx.QueryRow(ctx, "SELECT to_regclass($1)::oid", string(table)).Scan(&lock.relationOID); err != nil || lock.relationOID == 0 {
		_ = tx.Rollback(ctx)
		conn.Release()
		t.Fatalf("lifecycle table lock relation identity failed")
	}
	var statement string
	switch table {
	case lifecycleTablePaymentOrders:
		statement = "LOCK TABLE payment_orders IN SHARE MODE"
	case lifecycleTableResultHandlingRecords:
		statement = "LOCK TABLE result_handling_records IN SHARE MODE"
	default:
		_ = tx.Rollback(ctx)
		conn.Release()
		t.Fatalf("lifecycle table lock table is not allowed")
	}
	if _, err := tx.Exec(ctx, statement); err != nil {
		_ = tx.Rollback(ctx)
		conn.Release()
		t.Fatalf("lifecycle table lock acquisition failed")
	}
	t.Cleanup(func() {
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), us1CleanupBudget)
		defer cleanupCancel()
		if err := lock.cleanup(cleanupCtx); err != nil {
			t.Errorf("lifecycle table lock cleanup failed")
		}
	})
	return lock
}

func (lock *lifecycleTableLock) VerifySingleWriter(t *testing.T, ctx context.Context) {
	t.Helper()
	var count int
	err := lock.pool.QueryRow(ctx, `
		SELECT count(*)
		FROM pg_locks l
		JOIN pg_stat_activity a ON a.pid = l.pid
		WHERE l.relation = $1
		  AND l.pid <> $2
		  AND l.mode IN ('RowExclusiveLock', 'ShareRowExclusiveLock', 'ExclusiveLock', 'AccessExclusiveLock')
		  AND (l.granted OR a.wait_event_type = 'Lock')
	`, lock.relationOID, lock.holderPID).Scan(&count)
	if err != nil {
		t.Fatalf("lifecycle single-writer precheck failed")
	}
	if count != 0 {
		t.Fatalf("lifecycle target relation has another writer or waiter")
	}
}

func (lock *lifecycleTableLock) WaitForWaiter(t *testing.T, ctx context.Context, statement lifecycleStatement) lifecycleWaiterProof {
	t.Helper()
	var pattern string
	switch statement {
	case lifecycleStatementRegistration:
		pattern = "%INSERT INTO payment_orders%"
	case lifecycleStatementResultHandling:
		pattern = "%INSERT INTO result_handling_records%"
	default:
		t.Fatalf("lifecycle waiter statement is not allowed")
	}
	ticker := time.NewTicker(lifecycleWaiterPoll)
	defer ticker.Stop()
	for {
		var count int
		err := lock.pool.QueryRow(ctx, `
			SELECT count(*)
			FROM pg_stat_activity a
			JOIN pg_locks l ON l.pid = a.pid
			WHERE a.datname = current_database()
			  AND a.backend_type = 'client backend'
			  AND a.pid <> $1
			  AND a.wait_event_type = 'Lock'
			  AND l.relation = $2
			  AND l.mode = 'RowExclusiveLock'
			  AND NOT l.granted
			  AND $1 = ANY(pg_blocking_pids(a.pid))
			  AND a.query LIKE $3
		`, lock.holderPID, lock.relationOID, pattern).Scan(&count)
		if err != nil {
			t.Fatalf("lifecycle waiter observation failed")
		}
		if count > 1 {
			t.Fatalf("lifecycle waiter is not unique")
		}
		if count == 1 {
			var proof lifecycleWaiterProof
			var xactStart pgtype.Timestamptz
			err := lock.pool.QueryRow(ctx, `
				SELECT a.pid, a.backend_start, a.xact_start, a.wait_event_type, a.wait_event, l.mode
				FROM pg_stat_activity a
				JOIN pg_locks l ON l.pid = a.pid
				WHERE a.datname = current_database()
				  AND a.backend_type = 'client backend'
				  AND a.pid <> $1
				  AND a.wait_event_type = 'Lock'
				  AND l.relation = $2
				  AND l.mode = 'RowExclusiveLock'
				  AND NOT l.granted
				  AND $1 = ANY(pg_blocking_pids(a.pid))
				  AND a.query LIKE $3
				ORDER BY a.pid
				LIMIT 1
			`, lock.holderPID, lock.relationOID, pattern).Scan(
				&proof.WaiterPID, &proof.BackendStart, &xactStart,
				&proof.WaitEventType, &proof.WaitEvent, &proof.LockMode,
			)
			if err != nil {
				t.Fatalf("lifecycle waiter identity observation failed")
			}
			if proof.WaiterPID == 0 || proof.BackendStart.IsZero() || !xactStart.Valid {
				t.Fatalf("lifecycle waiter identity is incomplete")
			}
			value := xactStart.Time.UTC()
			proof.HolderPID = lock.holderPID
			proof.XactStart = &value
			proof.TableOID = lock.relationOID
			proof.Statement = statement
			lock.mu.Lock()
			lock.target = proof
			lock.targetSet = true
			lock.mu.Unlock()
			return proof
		}
		select {
		case <-ctx.Done():
			t.Fatalf("lifecycle waiter was not observed")
		case <-ticker.C:
		}
	}
}

func (lock *lifecycleTableLock) WaitWaiterDone(t *testing.T, ctx context.Context) {
	t.Helper()
	if err := lock.waitWaiterDone(ctx); err != nil {
		t.Fatalf("lifecycle waiter transaction did not end")
	}
}

func (lock *lifecycleTableLock) waitWaiterDone(ctx context.Context) error {
	lock.mu.Lock()
	target := lock.target
	set := lock.targetSet
	lock.mu.Unlock()
	if !set || target.WaiterPID == 0 || target.BackendStart.IsZero() || target.XactStart == nil {
		return errors.New("lifecycle waiter identity is missing")
	}
	ticker := time.NewTicker(lifecycleWaiterPoll)
	defer ticker.Stop()
	for {
		var backendStart time.Time
		var xactStart pgtype.Timestamptz
		err := lock.pool.QueryRow(ctx,
			"SELECT backend_start, xact_start FROM pg_stat_activity WHERE pid = $1",
			target.WaiterPID).Scan(&backendStart, &xactStart)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		if !backendStart.Equal(target.BackendStart) {
			return nil
		}
		if !xactStart.Valid {
			return nil
		}
		current := xactStart.Time.UTC()
		if !current.Equal(*target.XactStart) {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}

func (lock *lifecycleTableLock) verifyNoLateWriter(ctx context.Context) error {
	var count int
	err := lock.pool.QueryRow(ctx, `
		SELECT count(*)
		FROM pg_locks l
		JOIN pg_stat_activity a ON a.pid = l.pid
		WHERE l.relation = $1
		  AND l.mode IN ('RowExclusiveLock', 'ShareRowExclusiveLock', 'ExclusiveLock', 'AccessExclusiveLock')
		  AND (l.granted OR a.wait_event_type = 'Lock')
	`, lock.relationOID).Scan(&count)
	if err != nil {
		return err
	}
	if count != 0 {
		return errors.New("lifecycle target relation has a late writer")
	}
	return nil
}

func (lock *lifecycleTableLock) cleanup(ctx context.Context) error {
	if err := lock.release(ctx); err != nil {
		return err
	}
	lock.mu.Lock()
	targetSet := lock.targetSet
	lock.mu.Unlock()
	if targetSet {
		if err := lock.waitWaiterDone(ctx); err != nil {
			return err
		}
	}
	return lock.verifyNoLateWriter(ctx)
}

func (lock *lifecycleTableLock) release(ctx context.Context) error {
	lock.mu.Lock()
	if lock.released {
		lock.mu.Unlock()
		return nil
	}
	lock.released = true
	tx := lock.tx
	conn := lock.conn
	holderPID := lock.holderPID
	relationOID := lock.relationOID
	lock.mu.Unlock()

	if tx != nil {
		_ = tx.Rollback(ctx)
	}
	if conn != nil {
		conn.Release()
	}
	var count int
	err := lock.pool.QueryRow(ctx, `
		SELECT count(*)
		FROM pg_locks
		WHERE pid = $1
		  AND relation = $2
		  AND mode = 'ShareLock'
		  AND granted
	`, holderPID, relationOID).Scan(&count)
	if err != nil {
		return err
	}
	if count != 0 {
		return errors.New("lifecycle table share lock remains")
	}
	return nil
}

func lifecycleReadOrder(ctx context.Context, pool *pgxpool.Pool, clientID, businessOrderNo string) (lifecycleOrderRow, bool, error) {
	var row lifecycleOrderRow
	var confirmedAt pgtype.Timestamptz
	err := pool.QueryRow(ctx, `
		SELECT id::text, client_id, business_order_no, amount, currency, result,
		       COALESCE(mock_transaction_id::text, ''), registered_at, confirmed_at
		FROM payment_orders
		WHERE client_id = $1 AND business_order_no = $2
	`, clientID, businessOrderNo).Scan(
		&row.ID, &row.ClientID, &row.BusinessOrderNo, &row.Amount,
		&row.Currency, &row.Result, &row.MockTransactionID,
		&row.RegisteredAt, &confirmedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return row, false, nil
	}
	if err != nil {
		return row, false, err
	}
	if confirmedAt.Valid {
		value := confirmedAt.Time.UTC()
		row.ConfirmedAt = &value
	}
	return row, true, nil
}

func lifecycleWaitOrder(t *testing.T, pool *pgxpool.Pool, clientID, businessOrderNo string, budget time.Duration) lifecycleOrderRow {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), budget)
	defer cancel()
	ticker := time.NewTicker(lifecycleWaiterPoll)
	defer ticker.Stop()
	for {
		row, found, err := lifecycleReadOrder(ctx, pool, clientID, businessOrderNo)
		if err != nil {
			t.Fatalf("lifecycle order observation failed")
		}
		if found {
			return row
		}
		select {
		case <-ctx.Done():
			t.Fatalf("lifecycle order was not observed")
		case <-ticker.C:
		}
	}
}

func lifecycleReadMockRow(ctx context.Context, pool *pgxpool.Pool, orderID string) (lifecycleMockRow, bool, error) {
	var row lifecycleMockRow
	err := pool.QueryRow(ctx, `
		SELECT id::text, platform_reference_id::text, amount, currency, result, processed_at
		FROM mock_transactions
		WHERE platform_reference_id = $1
	`, orderID).Scan(
		&row.ID, &row.PlatformReferenceID, &row.Amount, &row.Currency,
		&row.Result, &row.ProcessedAt,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return row, false, nil
	}
	if err != nil {
		return row, false, err
	}
	row.ProcessedAt = row.ProcessedAt.UTC()
	return row, true, nil
}

func lifecycleWaitMockRow(t *testing.T, pool *pgxpool.Pool, orderID string, budget time.Duration) lifecycleMockRow {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), budget)
	defer cancel()
	ticker := time.NewTicker(lifecycleWaiterPoll)
	defer ticker.Stop()
	for {
		row, found, err := lifecycleReadMockRow(ctx, pool, orderID)
		if err != nil {
			t.Fatalf("lifecycle Mock observation failed")
		}
		if found {
			return row
		}
		select {
		case <-ctx.Done():
			t.Fatalf("lifecycle Mock commit was not observed")
		case <-ticker.C:
		}
	}
}

func lifecycleReadTrace(ctx context.Context, pool *pgxpool.Pool, orderID string) (us3Record, bool, error) {
	var record us3Record
	var evidenceJSON string
	err := pool.QueryRow(ctx, `
		SELECT id::text, request_id::text, source, decision,
		       result_before, result_after, COALESCE(reason_code, ''),
		       COALESCE(evidence::text, '{}')
		FROM result_handling_records
		WHERE payment_order_id = $1 AND source = 'PAYMENT'
		ORDER BY observed_at DESC
		LIMIT 1
	`, orderID).Scan(
		&record.RecordID, &record.RequestID, &record.Source, &record.Decision,
		&record.ResultBefore, &record.ResultAfter, &record.ReasonCode, &evidenceJSON,
	)
	if errors.Is(err, pgx.ErrNoRows) {
		return record, false, nil
	}
	if err != nil {
		return record, false, err
	}
	if err := json.Unmarshal([]byte(evidenceJSON), &record.Evidence); err != nil {
		return record, false, err
	}
	if record.Evidence.Differences == nil {
		record.Evidence.Differences = []string{}
	}
	return record, true, nil
}

func lifecycleCounts(ctx context.Context, pool *pgxpool.Pool, clientID string) (lifecyclePGCounts, error) {
	var orderCount, mockCount, traceCount int64
	if err := pool.QueryRow(ctx,
		"SELECT count(*) FROM payment_orders WHERE client_id = $1", clientID).Scan(&orderCount); err != nil {
		return lifecyclePGCounts{}, err
	}
	if err := pool.QueryRow(ctx, `
		SELECT count(*) FROM mock_transactions
		WHERE platform_reference_id IN (SELECT id FROM payment_orders WHERE client_id = $1)
	`, clientID).Scan(&mockCount); err != nil {
		return lifecyclePGCounts{}, err
	}
	if err := pool.QueryRow(ctx, `
		SELECT count(*) FROM result_handling_records
		WHERE payment_order_id IN (SELECT id FROM payment_orders WHERE client_id = $1)
	`, clientID).Scan(&traceCount); err != nil {
		return lifecyclePGCounts{}, err
	}
	return lifecyclePGCounts{Order: uint64(orderCount), Mock: uint64(mockCount), Trace: uint64(traceCount)}, nil
}

func lifecycleWaitCounts(t *testing.T, pool *pgxpool.Pool, clientID string, want lifecyclePGCounts, budget time.Duration) lifecyclePGCounts {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), budget)
	defer cancel()
	ticker := time.NewTicker(lifecycleWaiterPoll)
	defer ticker.Stop()
	for {
		counts, err := lifecycleCounts(ctx, pool, clientID)
		if err != nil {
			t.Fatalf("lifecycle PG count observation failed")
		}
		if counts == want {
			return counts
		}
		select {
		case <-ctx.Done():
			t.Fatalf("lifecycle PG counts did not settle")
		case <-ticker.C:
		}
	}
}

func lifecycleRequireOrderImmutable(t *testing.T, before, after lifecycleOrderRow) {
	t.Helper()
	if before.ID != after.ID ||
		before.ClientID != after.ClientID ||
		before.BusinessOrderNo != after.BusinessOrderNo ||
		before.Amount != after.Amount ||
		before.Currency != after.Currency ||
		!before.RegisteredAt.Equal(after.RegisteredAt) {
		t.Fatalf("lifecycle order immutable facts changed")
	}
}

func lifecycleRequireOrderSeed(t *testing.T, row lifecycleOrderRow, clientID, businessOrderNo string) {
	t.Helper()
	if !us1LowerHexUUIDv4(row.ID) ||
		row.ClientID != clientID ||
		row.BusinessOrderNo != businessOrderNo ||
		row.Amount != lifecycleAmount ||
		row.Currency != "CNY" ||
		row.RegisteredAt.IsZero() || row.RegisteredAt.Location() != time.UTC ||
		row.Result != "UNKNOWN" ||
		row.ConfirmedAt != nil ||
		row.MockTransactionID != "" {
		t.Fatalf("lifecycle order seed facts mismatch")
	}
}

func lifecycleRequireOrderUnknown(t *testing.T, row lifecycleOrderRow) {
	t.Helper()
	if row.Result != "UNKNOWN" || row.ConfirmedAt != nil || row.MockTransactionID != "" {
		t.Fatalf("lifecycle order is not the original UNKNOWN fact")
	}
}

func lifecycleRequireOrderConfirmed(t *testing.T, row lifecycleOrderRow, mockID string) {
	t.Helper()
	if row.Result != "SUCCEEDED" ||
		row.Amount != lifecycleAmount ||
		row.Currency != "CNY" ||
		row.ConfirmedAt == nil ||
		row.ConfirmedAt.IsZero() ||
		row.ConfirmedAt.Location() != time.UTC ||
		row.MockTransactionID != mockID {
		t.Fatalf("lifecycle order confirmed facts mismatch")
	}
}

func lifecycleRequireMockImmutable(t *testing.T, before, after lifecycleMockRow) {
	t.Helper()
	if before.ID != after.ID ||
		before.PlatformReferenceID != after.PlatformReferenceID ||
		before.Amount != after.Amount ||
		before.Currency != after.Currency ||
		before.Result != after.Result ||
		!before.ProcessedAt.Equal(after.ProcessedAt) {
		t.Fatalf("lifecycle Mock immutable facts changed")
	}
}

func lifecycleRequireMockSeed(t *testing.T, mock lifecycleMockRow, orderID string) {
	t.Helper()
	if !us1LowerHexUUIDv4(mock.ID) ||
		mock.PlatformReferenceID != orderID ||
		mock.Amount != lifecycleAmount ||
		mock.Currency != "CNY" ||
		mock.Result != "SUCCEEDED" ||
		mock.ProcessedAt.IsZero() ||
		mock.ProcessedAt.Location() != time.UTC {
		t.Fatalf("lifecycle Mock seed facts mismatch")
	}
}

func lifecycleRequireAcceptedTrace(t *testing.T, trace us3Record, orderID, mockID string) {
	t.Helper()
	if !us1LowerHexUUIDv4(trace.RecordID) || !us1LowerHexUUIDv4(trace.RequestID) ||
		trace.Source != "PAYMENT" || trace.Decision != "ACCEPT_CONFIRMATION" ||
		trace.ResultBefore != "UNKNOWN" || trace.ResultAfter != "SUCCEEDED" ||
		trace.ReasonCode != "" || len(trace.Evidence.Differences) != 0 {
		t.Fatalf("lifecycle accepted trace facts mismatch")
	}
	if trace.Evidence.PlatformReferenceID == nil ||
		*trace.Evidence.PlatformReferenceID != orderID ||
		trace.Evidence.MockTransactionID == nil ||
		*trace.Evidence.MockTransactionID != mockID ||
		trace.Evidence.Amount == nil ||
		*trace.Evidence.Amount != lifecycleAmount ||
		trace.Evidence.Currency == nil ||
		*trace.Evidence.Currency != "CNY" ||
		trace.Evidence.Result == nil ||
		*trace.Evidence.Result != "SUCCEEDED" {
		t.Fatalf("lifecycle accepted trace evidence mismatch")
	}
}

func lifecycleOrderFact(row lifecycleOrderRow) us3OrderFact {
	fact := us3OrderFact{
		PaymentID:         row.ID,
		ClientRefSHA256:   us3RefSHA256(row.ClientID),
		BusinessRefSHA256: us3RefSHA256(row.BusinessOrderNo),
		Amount:            row.Amount,
		Currency:          row.Currency,
		Result:            row.Result,
		RegisteredAt:      row.RegisteredAt.UTC().Format(time.RFC3339Nano),
	}
	if row.ConfirmedAt != nil {
		value := row.ConfirmedAt.UTC().Format(time.RFC3339Nano)
		fact.ConfirmedAt = &value
	}
	if row.MockTransactionID != "" {
		value := row.MockTransactionID
		fact.MockTransactionID = &value
	}
	return fact
}

func lifecycleStartPost(ctx context.Context, addr, apiKey, businessOrderNo string) <-chan lifecycleHTTPOutcome {
	ch := make(chan lifecycleHTTPOutcome, 1)
	go func() {
		response, body, err := us1PostPayment(ctx, addr, apiKey, businessOrderNo, lifecycleAmount, "CNY")
		ch <- lifecycleHTTPOutcomeFrom(response, body, err)
	}()
	return ch
}

func lifecycleHTTPOutcomeFrom(response *http.Response, body []byte, err error) lifecycleHTTPOutcome {
	if err != nil {
		transport := "NO_RESPONSE"
		if errors.Is(err, context.Canceled) {
			transport = "CLIENT_CANCELED"
		}
		return lifecycleHTTPOutcome{Transport: transport, Err: err}
	}
	if response == nil {
		return lifecycleHTTPOutcome{Transport: "NO_RESPONSE"}
	}
	outcome := lifecycleHTTPOutcome{Transport: "RESPONSE", HasResponse: true, Status: response.StatusCode}
	if len(body) == 0 {
		return outcome
	}
	var envelope us1Envelope
	if err := json.Unmarshal(body, &envelope); err != nil {
		outcome.Err = err
		return outcome
	}
	outcome.RequestID = envelope.RequestID
	if envelope.Payment != nil {
		outcome.PaymentID = envelope.Payment.PaymentID
		outcome.Result = envelope.Payment.Result
	}
	for _, issue := range envelope.Issues {
		outcome.Issues = append(outcome.Issues, issue.Code)
	}
	return outcome
}

func lifecycleAwaitHTTP(t *testing.T, ctx context.Context, ch <-chan lifecycleHTTPOutcome, what string) lifecycleHTTPOutcome {
	t.Helper()
	select {
	case outcome := <-ch:
		return outcome
	case <-ctx.Done():
		t.Fatalf("%s did not finish", what)
		return lifecycleHTTPOutcome{}
	}
}

func lifecycleRequireCanceledNoResponse(t *testing.T, outcome lifecycleHTTPOutcome) {
	t.Helper()
	if outcome.HasResponse {
		t.Fatalf("lifecycle cancel fixture observed an unexpected HTTP response")
	}
	if outcome.Transport != "CLIENT_CANCELED" {
		t.Fatalf("lifecycle cancel fixture did not observe CLIENT_CANCELED")
	}
}

func lifecycleRecordHTTP(t *testing.T, artifacts *harness.Artifacts, scenario *us3Scenario, kind string, outcome lifecycleHTTPOutcome) {
	t.Helper()
	if !outcome.HasResponse {
		return
	}
	us3RecordHTTPKind(t, artifacts, scenario, kind, 1, outcome.Status,
		outcome.RequestID, outcome.PaymentID, outcome.Result, outcome.Issues...)
}

func lifecycleApplyPrimaryExitOutcome(t *testing.T, artifacts *harness.Artifacts, scenario *us3Scenario, outcome lifecycleHTTPOutcome) {
	t.Helper()
	switch outcome.Transport {
	case "RESPONSE":
		if !outcome.HasResponse || outcome.Status != http.StatusServiceUnavailable {
			t.Fatalf("lifecycle exit primary response was not 503")
		}
		scenario.Transport = "RESPONSE"
		lifecycleRecordHTTP(t, artifacts, scenario, "PRIMARY", outcome)
	case "CLIENT_CANCELED", "NO_RESPONSE":
		scenario.Transport = outcome.Transport
	default:
		t.Fatalf("lifecycle exit primary transport is invalid")
	}
}

func lifecycleApplyShutdownProbe(t *testing.T, artifacts *harness.Artifacts, scenario *us3Scenario, outcome lifecycleHTTPOutcome) {
	t.Helper()
	if !outcome.HasResponse {
		if outcome.Transport != "CLIENT_CANCELED" && outcome.Transport != "NO_RESPONSE" {
			t.Fatalf("lifecycle shutdown probe transport is invalid")
		}
		return
	}
	if outcome.Status != http.StatusServiceUnavailable {
		t.Fatalf("lifecycle shutdown probe response was not 503")
	}
	lifecycleRecordHTTP(t, artifacts, scenario, "SHUTDOWN_PROBE", outcome)
}

func lifecycleWaitReadyClosed(t *testing.T, ctx context.Context, addr string) {
	t.Helper()
	ticker := time.NewTicker(25 * time.Millisecond)
	defer ticker.Stop()
	for {
		response, _, err := us1HTTPGet(ctx, "http://"+addr+"/readyz", "")
		if err != nil || response == nil || response.StatusCode != http.StatusOK {
			return
		}
		select {
		case <-ctx.Done():
			t.Fatalf("lifecycle readiness did not close")
		case <-ticker.C:
		}
	}
}

func lifecycleProbePost(ctx context.Context, addr, apiKey, businessOrderNo string) lifecycleHTTPOutcome {
	response, body, err := us1PostPayment(ctx, addr, apiKey, businessOrderNo, lifecycleAmount, "CNY")
	return lifecycleHTTPOutcomeFrom(response, body, err)
}

func lifecycleWaiterContext() (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), us1RequestBudget)
}

func TestUS3Lifecycle(t *testing.T) {
	repoRoot := us1RepoRoot(t)
	runID := us1RunID(t)
	dsn := us1TestDatabaseURL(t)
	binary := us1BuildApplication(t, repoRoot)

	t.Run("lifecycle_cancel_before_mock", func(t *testing.T) {
		lifecycleRunCancelBeforeMock(t, repoRoot, runID, dsn, binary)
	})
	t.Run("lifecycle_cancel_after_mock", func(t *testing.T) {
		lifecycleRunCancelAfterMock(t, repoRoot, runID, dsn, binary)
	})
	t.Run("lifecycle_exit_budget", func(t *testing.T) {
		lifecycleRunExitBudget(t, repoRoot, runID, dsn, binary)
	})
}

func lifecycleRunCancelBeforeMock(t *testing.T, repoRoot, runID, dsn, binary string) {
	t.Helper()
	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	pool := us1OpenPool(t)
	t.Cleanup(func() { us1CleanupBusiness(t, pool, clientID, businessOrderNo) })

	artifacts := us3BeginArtifacts(t, repoRoot, runID, "lifecycle_cancel_before_mock")
	scenario := lifecycleNewScenario("lifecycle_cancel_before_mock")
	socket := us1ObserverSocket(t)
	epoch := us1Epoch(t)
	observer := us3StartObserver(t, runID, epoch, socket)
	if err := observer.Expect(1); err != nil {
		t.Fatal("lifecycle observer generation registration failed")
	}
	process, addr := us3StartGeneration(t, artifacts, binary, repoRoot, dsn, clientID, us1APIKey, socket, runID, epoch, 1)

	beforeCounts := lifecycleWaitCounts(t, pool, clientID, lifecyclePGCounts{}, us1RequestBudget)
	lifecycleSetCountsBefore(scenario, beforeCounts)

	lock := lifecycleAcquireTableShareLock(t, pool, lifecycleTablePaymentOrders)
	precheckCtx, cancelPrecheck := context.WithTimeout(context.Background(), us1RequestBudget)
	lock.VerifySingleWriter(t, precheckCtx)
	cancelPrecheck()
	primaryCtx, cancelPrimary := context.WithCancel(context.Background())
	defer cancelPrimary()
	primaryCh := lifecycleStartPost(primaryCtx, addr, us1APIKey, businessOrderNo)

	waiterCtx, cancelWaiter := lifecycleWaiterContext()
	defer cancelWaiter()
	lock.WaitForWaiter(t, waiterCtx, lifecycleStatementRegistration)

	cancelPrimary()
	awaitCtx, cancelAwait := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancelAwait()
	outcome := lifecycleAwaitHTTP(t, awaitCtx, primaryCh, "lifecycle beforeMock primary")
	lifecycleRequireCanceledNoResponse(t, outcome)
	scenario.Transport = "CLIENT_CANCELED"

	doneCtx, cancelDone := lifecycleWaiterContext()
	defer cancelDone()
	lock.WaitWaiterDone(t, doneCtx)
	releaseCtx, cancelRelease := context.WithTimeout(context.Background(), us1CleanupBudget)
	defer cancelRelease()
	if err := lock.release(releaseCtx); err != nil {
		t.Fatalf("lifecycle table lock release failed")
	}
	lateCtx, cancelLate := context.WithTimeout(context.Background(), us1RequestBudget)
	if err := lock.verifyNoLateWriter(lateCtx); err != nil {
		t.Fatalf("lifecycle target relation has a late writer")
	}
	cancelLate()

	counts := lifecycleWaitCounts(t, pool, clientID, lifecyclePGCounts{}, us1RequestBudget)
	_, _ = lifecycleStopAndElapsed(t, process)
	sealed := lifecycleSealedCounts(t, observer, 0, 0)

	lifecycleSetCancel(scenario, "BEFORE_MOCK")
	lifecycleRecordFault(scenario, "REGISTRATION_INSERT_HOLD")
	lifecycleRecordCounts(t, artifacts, scenario, counts, sealed, true)
	us3FinalizeScenario(t, artifacts, scenario, observer, process, []uint64{1}, nil)
	lifecycleRequireCallsMatchChecks(t, scenario, sealed.Payment, sealed.Query)
}

func lifecycleRunCancelAfterMock(t *testing.T, repoRoot, runID, dsn, binary string) {
	t.Helper()
	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	pool := us1OpenPool(t)
	t.Cleanup(func() { us1CleanupBusiness(t, pool, clientID, businessOrderNo) })

	artifacts := us3BeginArtifacts(t, repoRoot, runID, "lifecycle_cancel_after_mock")
	scenario := lifecycleNewScenario("lifecycle_cancel_after_mock")
	socket := us1ObserverSocket(t)
	epoch := us1Epoch(t)
	observer := us3StartObserver(t, runID, epoch, socket)
	if err := observer.Expect(1); err != nil {
		t.Fatal("lifecycle observer generation registration failed")
	}
	process, addr := us3StartGeneration(t, artifacts, binary, repoRoot, dsn, clientID, us1APIKey, socket, runID, epoch, 1)

	beforeCounts := lifecycleWaitCounts(t, pool, clientID, lifecyclePGCounts{}, us1RequestBudget)
	lifecycleSetCountsBefore(scenario, beforeCounts)

	lock := lifecycleAcquireTableShareLock(t, pool, lifecycleTableResultHandlingRecords)
	precheckCtx, cancelPrecheck := context.WithTimeout(context.Background(), us1RequestBudget)
	lock.VerifySingleWriter(t, precheckCtx)
	cancelPrecheck()
	primaryCtx, cancelPrimary := context.WithCancel(context.Background())
	defer cancelPrimary()
	primaryCh := lifecycleStartPost(primaryCtx, addr, us1APIKey, businessOrderNo)

	beforeRow := lifecycleWaitOrder(t, pool, clientID, businessOrderNo, us1RequestBudget)
	beforeMock := lifecycleWaitMockRow(t, pool, beforeRow.ID, us1RequestBudget)
	lifecycleRequireOrderSeed(t, beforeRow, clientID, businessOrderNo)
	lifecycleRequireMockSeed(t, beforeMock, beforeRow.ID)

	waiterCtx, cancelWaiter := lifecycleWaiterContext()
	defer cancelWaiter()
	lock.WaitForWaiter(t, waiterCtx, lifecycleStatementResultHandling)

	cancelPrimary()
	releaseCtx, cancelRelease := context.WithTimeout(context.Background(), us1CleanupBudget)
	defer cancelRelease()
	if err := lock.release(releaseCtx); err != nil {
		t.Fatalf("lifecycle table lock release failed")
	}
	doneCtx, cancelDone := lifecycleWaiterContext()
	defer cancelDone()
	lock.WaitWaiterDone(t, doneCtx)
	lateCtx, cancelLate := context.WithTimeout(context.Background(), us1RequestBudget)
	if err := lock.verifyNoLateWriter(lateCtx); err != nil {
		t.Fatalf("lifecycle target relation has a late writer")
	}
	cancelLate()

	awaitCtx, cancelAwait := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancelAwait()
	outcome := lifecycleAwaitHTTP(t, awaitCtx, primaryCh, "lifecycle afterMock primary")
	lifecycleRequireCanceledNoResponse(t, outcome)
	scenario.Transport = "CLIENT_CANCELED"

	readCtx, cancelRead := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancelRead()
	afterRow, found, err := lifecycleReadOrder(readCtx, pool, clientID, businessOrderNo)
	if err != nil || !found {
		t.Fatalf("lifecycle afterMock order observation failed")
	}
	afterMock, found, err := lifecycleReadMockRow(readCtx, pool, beforeRow.ID)
	if err != nil || !found {
		t.Fatalf("lifecycle afterMock Mock observation failed")
	}
	lifecycleRequireOrderImmutable(t, beforeRow, afterRow)
	lifecycleRequireMockImmutable(t, beforeMock, afterMock)
	trace, traceFound, err := lifecycleReadTrace(readCtx, pool, beforeRow.ID)
	if err != nil {
		t.Fatalf("lifecycle afterMock trace observation failed")
	}
	counts, err := lifecycleCounts(readCtx, pool, clientID)
	if err != nil {
		t.Fatalf("lifecycle afterMock PG count observation failed")
	}
	beforeFact := lifecycleOrderFact(beforeRow)
	afterFact := lifecycleOrderFact(afterRow)
	switch afterRow.Result {
	case "UNKNOWN":
		if traceFound || counts != (lifecyclePGCounts{Order: 1, Mock: 1, Trace: 0}) {
			t.Fatalf("lifecycle afterMock unknown tuple mismatch")
		}
		lifecycleRequireOrderUnknown(t, afterRow)
		scenario.Facts.Before = &beforeFact
		scenario.Facts.After = &afterFact
		scenario.Facts.Transition = "UNCHANGED"
	case "SUCCEEDED":
		if !traceFound || counts != (lifecyclePGCounts{Order: 1, Mock: 1, Trace: 1}) {
			t.Fatalf("lifecycle afterMock confirmed tuple mismatch")
		}
		lifecycleRequireOrderConfirmed(t, afterRow, beforeMock.ID)
		lifecycleRequireAcceptedTrace(t, trace, beforeRow.ID, beforeMock.ID)
		scenario.Records = append(scenario.Records, trace)
		scenario.Facts.Before = &beforeFact
		scenario.Facts.After = &afterFact
		scenario.Facts.Transition = "UNKNOWN_TO_CONFIRMED"
	default:
		t.Fatalf("lifecycle afterMock order result is not a closed tuple")
	}

	_, _ = lifecycleStopAndElapsed(t, process)
	sealed := lifecycleSealedCounts(t, observer, 1, 0)

	lifecycleSetCancel(scenario, "AFTER_MOCK")
	lifecycleRecordFault(scenario, "RESULT_HANDLING_INSERT_HOLD")
	lifecycleRecordCounts(t, artifacts, scenario, counts, sealed, true)
	us3FinalizeScenario(t, artifacts, scenario, observer, process, []uint64{1}, nil)
	lifecycleRequireCallsMatchChecks(t, scenario, sealed.Payment, sealed.Query)
}

func lifecycleRunExitBudget(t *testing.T, repoRoot, runID, dsn, binary string) {
	t.Helper()
	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	pool := us1OpenPool(t)
	t.Cleanup(func() { us1CleanupBusiness(t, pool, clientID, businessOrderNo) })

	artifacts := us3BeginArtifacts(t, repoRoot, runID, "lifecycle_exit_budget")
	scenario := lifecycleNewScenario("lifecycle_exit_budget")
	socket := us1ObserverSocket(t)
	epoch := us1Epoch(t)
	observer := us3StartObserver(t, runID, epoch, socket)
	if err := observer.Expect(1); err != nil {
		t.Fatal("lifecycle observer generation registration failed")
	}
	addr := us1FreeLoopbackAddr(t)
	env := us3AppEnv(t, dsn, addr, clientID, us1APIKey, socket, runID, epoch, 1)
	process := us1StartApplication(t, artifacts, binary, repoRoot, 1, env, 25*time.Second, 60*time.Second)
	us1WaitReady(t, addr)

	beforeCounts := lifecycleWaitCounts(t, pool, clientID, lifecyclePGCounts{}, us1RequestBudget)
	lifecycleSetCountsBefore(scenario, beforeCounts)

	lock := lifecycleAcquireTableShareLock(t, pool, lifecycleTablePaymentOrders)
	precheckCtx, cancelPrecheck := context.WithTimeout(context.Background(), us1RequestBudget)
	lock.VerifySingleWriter(t, precheckCtx)
	cancelPrecheck()
	primaryCtx, cancelPrimary := context.WithTimeout(context.Background(), us1StopBudget)
	defer cancelPrimary()
	primaryCh := lifecycleStartPost(primaryCtx, addr, us1APIKey, businessOrderNo)

	waiterCtx, cancelWaiter := lifecycleWaiterContext()
	defer cancelWaiter()
	lock.WaitForWaiter(t, waiterCtx, lifecycleStatementRegistration)

	stopCh := lifecycleBeginStop(process)
	readyCtx, cancelReady := context.WithTimeout(context.Background(), us1RequestBudget)
	lifecycleWaitReadyClosed(t, readyCtx, addr)
	cancelReady()

	probeCtx, cancelProbe := context.WithTimeout(context.Background(), us1RequestBudget)
	probeOutcome := lifecycleProbePost(probeCtx, addr, us1APIKey, businessOrderNo)
	cancelProbe()

	exit, elapsed := lifecycleAwaitStop(t, stopCh)
	lifecycleRequireExitElapsed(t, elapsed)
	if exit.TimedOut || exit.Forced {
		t.Fatalf("lifecycle exit process was not graceful")
	}

	doneCtx, cancelDone := lifecycleWaiterContext()
	defer cancelDone()
	lock.WaitWaiterDone(t, doneCtx)
	releaseCtx, cancelRelease := context.WithTimeout(context.Background(), us1CleanupBudget)
	defer cancelRelease()
	if err := lock.release(releaseCtx); err != nil {
		t.Fatalf("lifecycle table lock release failed")
	}
	lateCtx, cancelLate := context.WithTimeout(context.Background(), us1RequestBudget)
	if err := lock.verifyNoLateWriter(lateCtx); err != nil {
		t.Fatalf("lifecycle target relation has a late writer")
	}
	cancelLate()

	awaitCtx, cancelAwait := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancelAwait()
	primaryOutcome := lifecycleAwaitHTTP(t, awaitCtx, primaryCh, "lifecycle exit primary")
	lifecycleApplyPrimaryExitOutcome(t, artifacts, scenario, primaryOutcome)
	lifecycleApplyShutdownProbe(t, artifacts, scenario, probeOutcome)

	counts := lifecycleWaitCounts(t, pool, clientID, lifecyclePGCounts{}, us1RequestBudget)
	sealed := lifecycleSealedCounts(t, observer, 0, 0)

	lifecycleSetShutdown(scenario, exit, sealed, elapsed)
	lifecycleRecordExitBudgets(t, artifacts, scenario, elapsed)
	lifecycleRecordFault(scenario, "REGISTRATION_INSERT_HOLD")
	lifecycleRecordCounts(t, artifacts, scenario, counts, sealed, true)
	us3FinalizeScenario(t, artifacts, scenario, observer, process, []uint64{1}, nil)
	lifecycleRequireCallsMatchChecks(t, scenario, sealed.Payment, sealed.Query)
}
