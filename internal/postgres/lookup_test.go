//go:build integration

package postgres_test

import (
	"context"
	"errors"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"payment-hub/internal/payment"
	"payment-hub/internal/postgres"
	"payment-hub/tests/harness"
)

var _ postgres.DB = (*lookupTestFaultDB)(nil)
var _ postgres.DB = (*lookupTestSpyDB)(nil)

type lookupTestErrorRow struct {
	err error
}

func (row lookupTestErrorRow) Scan(...any) error {
	return row.err
}

type lookupTestFaultDB struct {
	pool          *pgxpool.Pool
	failQueryRow  bool
	queryRowCalls atomic.Int32
}

func (db *lookupTestFaultDB) BeginTx(ctx context.Context, opts pgx.TxOptions) (pgx.Tx, error) {
	return db.pool.BeginTx(ctx, opts)
}

func (db *lookupTestFaultDB) QueryRow(ctx context.Context, sql string, args ...any) pgx.Row {
	db.queryRowCalls.Add(1)
	if db.failQueryRow {
		return lookupTestErrorRow{err: errors.New("storage fault")}
	}
	return db.pool.QueryRow(ctx, sql, args...)
}

type lookupTestSpyDB struct {
	pool *pgxpool.Pool

	beginCalls    atomic.Int32
	queryRowCalls atomic.Int32

	mu         sync.Mutex
	sqls       []string
	deadline   time.Time
	deadlineOK bool
}

func (db *lookupTestSpyDB) BeginTx(ctx context.Context, opts pgx.TxOptions) (pgx.Tx, error) {
	db.beginCalls.Add(1)
	return db.pool.BeginTx(ctx, opts)
}

func (db *lookupTestSpyDB) QueryRow(ctx context.Context, sql string, args ...any) pgx.Row {
	db.queryRowCalls.Add(1)
	db.mu.Lock()
	db.sqls = append(db.sqls, sql)
	if deadline, ok := ctx.Deadline(); ok {
		db.deadline = deadline
		db.deadlineOK = true
	}
	db.mu.Unlock()
	return db.pool.QueryRow(ctx, sql, args...)
}

func (db *lookupTestSpyDB) snapshot() ([]string, time.Time, bool) {
	db.mu.Lock()
	defer db.mu.Unlock()
	sqls := append([]string(nil), db.sqls...)
	return sqls, db.deadline, db.deadlineOK
}

func TestOrderLookupIntegration(t *testing.T) {
	observer := harness.OpenCommittedPostgresPool(t)
	writer := harness.OpenCommittedPostgresPool(t)

	t.Run("finds by id and business key with exact committed values", func(t *testing.T) {
		clientID := committedTestNewClientID()
		unknownBusinessNo := committedTestNewBusinessOrderNo()
		confirmedBusinessNo := committedTestNewBusinessOrderNo()
		committedTestCleanup(t, observer, clientID)

		unknown := committedTestNewUnknownOrder(clientID, unknownBusinessNo, 1100)
		committedTestInsertOrder(t, observer, unknown)
		confirmedAt := time.Now().UTC().Add(-time.Minute).Truncate(time.Microsecond)
		confirmedMockID := payment.MockTransactionID(payment.NewUUIDv4())
		confirmed := committedTestNewConfirmedOrder(clientID, confirmedBusinessNo, 2200, payment.ResultSucceeded, confirmedMockID, confirmedAt)
		committedTestInsertOrder(t, observer, confirmed)

		store := postgres.NewStore(writer, postgres.NewHooks(nil))

		for _, tc := range []struct {
			name  string
			order payment.PaymentOrder
			byID  bool
		}{
			{name: "unknown_by_id", order: unknown, byID: true},
			{name: "unknown_by_business", order: unknown, byID: false},
			{name: "confirmed_by_id", order: confirmed, byID: true},
			{name: "confirmed_by_business", order: confirmed, byID: false},
		} {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				ctx, cancel := committedTestContext(t)
				defer cancel()
				var got payment.PaymentOrder
				var found bool
				var err error
				if tc.byID {
					got, found, err = store.FindOrderByID(ctx, clientID, tc.order.ID)
				} else {
					got, found, err = store.FindOrderByBusinessOrderNo(ctx, clientID, tc.order.BusinessOrderNo)
				}
				if err != nil {
					t.Fatalf("lookup failed: %s", committedTestErrorClass(err))
				}
				if !found {
					t.Fatalf("lookup did not find the committed order")
				}
				lookupTestRequireOrder(t, got, tc.order)
			})
		}
	})

	t.Run("cross client and missing rows are not found without side effects", func(t *testing.T) {
		clientA := committedTestNewClientID()
		clientB := committedTestNewClientID()
		businessNo := committedTestNewBusinessOrderNo()
		committedTestCleanup(t, observer, clientA)
		committedTestCleanup(t, observer, clientB)

		order := committedTestNewUnknownOrder(clientA, businessNo, 1300)
		committedTestInsertOrder(t, observer, order)
		store := postgres.NewStore(writer, postgres.NewHooks(nil))

		for _, tc := range []struct {
			name     string
			clientID string
			orderID  payment.PaymentOrderID
			business string
			byID     bool
		}{
			{name: "cross_client_by_id", clientID: clientB, orderID: order.ID, byID: true},
			{name: "cross_client_by_business", clientID: clientB, business: businessNo, byID: false},
			{name: "missing_id", clientID: clientA, orderID: payment.PaymentOrderID(payment.NewUUIDv4()), byID: true},
			{name: "missing_business", clientID: clientA, business: "missing-" + payment.NewUUIDv4(), byID: false},
		} {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				ctx, cancel := committedTestContext(t)
				defer cancel()
				var got payment.PaymentOrder
				var found bool
				var err error
				if tc.byID {
					got, found, err = store.FindOrderByID(ctx, tc.clientID, tc.orderID)
				} else {
					got, found, err = store.FindOrderByBusinessOrderNo(ctx, tc.clientID, tc.business)
				}
				if err != nil {
					t.Fatalf("missing lookup failed: %s", committedTestErrorClass(err))
				}
				if found {
					t.Fatalf("missing/scoped lookup unexpectedly found an order")
				}
				if got.ID != "" {
					t.Fatalf("missing lookup returned an order value")
				}
			})
		}

		if got := committedTestCountOrdersByBusinessKey(t, observer, clientA, businessNo); got != 1 {
			t.Fatalf("lookups changed the committed order count")
		}
		if got := committedTestCountRecords(t, observer, order.ID); got != 0 {
			t.Fatalf("lookups created %d handling records, want 0", got)
		}
	})

	t.Run("storage fault is not reported as missing", func(t *testing.T) {
		db := &lookupTestFaultDB{pool: writer, failQueryRow: true}
		store := postgres.NewStore(db, postgres.NewHooks(nil))
		ctx, cancel := committedTestContext(t)
		defer cancel()

		if _, found, err := store.FindOrderByID(ctx, "client_query", payment.PaymentOrderID(payment.NewUUIDv4())); found || err == nil {
			t.Fatalf("storage fault was reported as a reliable lookup result")
		} else if errors.Is(err, payment.ErrNotFound) {
			t.Fatalf("storage fault was reported as not found")
		}
		if _, found, err := store.FindOrderByBusinessOrderNo(ctx, "client_query", "missing-"+payment.NewUUIDv4()); found || err == nil {
			t.Fatalf("storage fault was reported as a reliable lookup result")
		} else if errors.Is(err, payment.ErrNotFound) {
			t.Fatalf("storage fault was reported as not found")
		}
		if db.queryRowCalls.Load() == 0 {
			t.Fatalf("lookup did not attempt a database read")
		}
	})

	t.Run("confirmed lookup does not begin tx, lock, or write records", func(t *testing.T) {
		clientID := committedTestNewClientID()
		businessNo := committedTestNewBusinessOrderNo()
		committedTestCleanup(t, observer, clientID)

		confirmedAt := time.Now().UTC().Add(-time.Minute).Truncate(time.Microsecond)
		mockID := payment.MockTransactionID(payment.NewUUIDv4())
		order := committedTestNewConfirmedOrder(clientID, businessNo, 2400, payment.ResultFailed, mockID, confirmedAt)
		committedTestInsertOrder(t, observer, order)
		recordID := payment.ResultHandlingRecordID(payment.NewUUIDv4())
		lookupTestInsertConfirmedRecord(t, observer, order, recordID)

		beforeOrder, ok := committedTestReadOrder(t, observer, order.ID)
		if !ok {
			t.Fatalf("fixture order is missing")
		}
		beforeRecord, ok := committedTestReadRecord(t, observer, recordID)
		if !ok {
			t.Fatalf("fixture handling record is missing")
		}
		beforeRecordCount := committedTestCountRecords(t, observer, order.ID)

		spy := &lookupTestSpyDB{pool: writer}
		store := postgres.NewStore(spy, postgres.NewHooks(nil))
		ctx, cancel := committedTestContext(t)
		defer cancel()

		got, found, err := store.FindOrderByID(ctx, clientID, order.ID)
		if err != nil || !found {
			t.Fatalf("confirmed lookup by ID failed: %s", committedTestErrorClass(err))
		}
		lookupTestRequireOrder(t, got, order)
		got, found, err = store.FindOrderByBusinessOrderNo(ctx, clientID, businessNo)
		if err != nil || !found {
			t.Fatalf("confirmed lookup by business failed: %s", committedTestErrorClass(err))
		}
		lookupTestRequireOrder(t, got, order)

		if spy.beginCalls.Load() != 0 {
			t.Fatalf("ordinary lookup began %d transactions, want 0", spy.beginCalls.Load())
		}
		if spy.queryRowCalls.Load() < 2 {
			t.Fatalf("ordinary lookup did not use committed reads")
		}
		sqls, _, _ := spy.snapshot()
		sawIDShape := false
		sawBusinessShape := false
		for _, sql := range sqls {
			normalized := lookupTestNormalizedSQL(sql)
			if !strings.Contains(normalized, "from payment_orders") {
				t.Fatalf("lookup SQL did not read payment_orders")
			}
			if strings.Contains(normalized, "for update") ||
				strings.Contains(normalized, "insert ") ||
				strings.Contains(normalized, "update ") ||
				strings.Contains(normalized, "result_handling_records") {
				t.Fatalf("ordinary lookup SQL used a write, lock, or handling-record path")
			}
			if strings.Contains(normalized, "where id = $1 and client_id = $2") {
				sawIDShape = true
			}
			if strings.Contains(normalized, "where client_id = $1 and business_order_no = $2") {
				sawBusinessShape = true
			}
		}
		if !sawIDShape || !sawBusinessShape {
			t.Fatalf("ordinary lookup SQL did not use both scoped read shapes")
		}

		afterOrder, ok := committedTestReadOrder(t, observer, order.ID)
		if !ok {
			t.Fatalf("order disappeared after ordinary lookup")
		}
		lookupTestRequireOrderRow(t, afterOrder, beforeOrder)
		afterRecord, ok := committedTestReadRecord(t, observer, recordID)
		if !ok {
			t.Fatalf("handling record disappeared after ordinary lookup")
		}
		lookupTestRequireRecordRow(t, afterRecord, beforeRecord)
		if got := committedTestCountRecords(t, observer, order.ID); got != beforeRecordCount {
			t.Fatalf("ordinary lookup changed the handling-record count")
		}
	})

	t.Run("lookup has a bounded two second deadline", func(t *testing.T) {
		clientID := committedTestNewClientID()
		businessNo := committedTestNewBusinessOrderNo()
		committedTestCleanup(t, observer, clientID)
		order := committedTestNewUnknownOrder(clientID, businessNo, 2500)
		committedTestInsertOrder(t, observer, order)

		spy := &lookupTestSpyDB{pool: writer}
		store := postgres.NewStore(spy, postgres.NewHooks(nil))
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()

		got, found, err := store.FindOrderByID(ctx, clientID, order.ID)
		if err != nil || !found {
			t.Fatalf("bounded lookup failed: %s", committedTestErrorClass(err))
		}
		lookupTestRequireOrder(t, got, order)
		_, deadline, ok := spy.snapshot()
		if !ok {
			t.Fatalf("lookup did not establish an operation deadline")
		}
		remaining := time.Until(deadline)
		if remaining <= 0 || remaining > 2*time.Second {
			t.Fatalf("lookup deadline was not bounded to two seconds")
		}
	})
}

func lookupTestNormalizedSQL(sql string) string {
	return strings.Join(strings.Fields(strings.ToLower(sql)), " ")
}

func lookupTestRequireOrder(t *testing.T, got, want payment.PaymentOrder) {
	t.Helper()
	if got.ID != want.ID ||
		got.ClientID != want.ClientID ||
		got.BusinessOrderNo != want.BusinessOrderNo ||
		got.Amount != want.Amount ||
		got.Currency != want.Currency ||
		got.Result != want.Result {
		t.Fatalf("lookup changed the order identity or payment facts")
	}
	if !got.RegisteredAt.UTC().Truncate(time.Microsecond).Equal(want.RegisteredAt.UTC().Truncate(time.Microsecond)) {
		t.Fatalf("lookup changed registered_at")
	}
	if want.ConfirmedAt == nil {
		if got.ConfirmedAt != nil {
			t.Fatalf("lookup invented confirmed_at")
		}
	} else if got.ConfirmedAt == nil ||
		!got.ConfirmedAt.UTC().Truncate(time.Microsecond).Equal(want.ConfirmedAt.UTC().Truncate(time.Microsecond)) {
		t.Fatalf("lookup changed confirmed_at")
	}
	if want.MockTransactionID == nil {
		if got.MockTransactionID != nil {
			t.Fatalf("lookup invented mock_transaction_id")
		}
	} else if got.MockTransactionID == nil || *got.MockTransactionID != *want.MockTransactionID {
		t.Fatalf("lookup changed mock_transaction_id")
	}
}

func lookupTestRequireOrderRow(t *testing.T, got, want committedTestOrderRow) {
	t.Helper()
	if got.ID != want.ID ||
		got.ClientID != want.ClientID ||
		got.BusinessOrderNo != want.BusinessOrderNo ||
		got.Amount != want.Amount ||
		got.Currency != want.Currency ||
		got.Result != want.Result {
		t.Fatalf("ordinary lookup changed the committed order row")
	}
	if !got.RegisteredAt.Equal(want.RegisteredAt) {
		t.Fatalf("ordinary lookup changed the committed registration time")
	}
	lookupTestRequireTimePtr(t, got.ConfirmedAt, want.ConfirmedAt)
	lookupTestRequireStringPtr(t, got.MockTransactionID, want.MockTransactionID)
}

func lookupTestRequireRecordRow(t *testing.T, got, want committedTestRecordRow) {
	t.Helper()
	if got.ID != want.ID ||
		got.PaymentOrderID != want.PaymentOrderID ||
		got.RequestID != want.RequestID ||
		got.Source != want.Source ||
		!got.ObservedAt.Equal(want.ObservedAt) ||
		string(got.Evidence) != string(want.Evidence) ||
		got.ResultBefore != want.ResultBefore ||
		got.ResultAfter != want.ResultAfter ||
		got.Decision != want.Decision {
		t.Fatalf("ordinary lookup changed the committed handling record")
	}
	lookupTestRequireStringPtr(t, got.ReasonCode, want.ReasonCode)
	lookupTestRequireStringPtr(t, got.ReasonMessage, want.ReasonMessage)
}

func lookupTestRequireTimePtr(t *testing.T, got, want *time.Time) {
	t.Helper()
	if want == nil {
		if got != nil {
			t.Fatalf("unexpected time value")
		}
		return
	}
	if got == nil || !got.UTC().Truncate(time.Microsecond).Equal(want.UTC().Truncate(time.Microsecond)) {
		t.Fatalf("time value changed")
	}
}

func lookupTestRequireStringPtr(t *testing.T, got, want *string) {
	t.Helper()
	if want == nil {
		if got != nil {
			t.Fatalf("unexpected string value")
		}
		return
	}
	if got == nil || *got != *want {
		t.Fatalf("string value changed")
	}
}

func lookupTestInsertConfirmedRecord(t *testing.T, pool *pgxpool.Pool, order payment.PaymentOrder, recordID payment.ResultHandlingRecordID) {
	t.Helper()
	ctx, cancel := committedTestContext(t)
	defer cancel()
	_, err := pool.Exec(ctx, `INSERT INTO result_handling_records
		(id, payment_order_id, request_id, source, observed_at, evidence,
		 result_before, result_after, decision, reason_code, reason_message)
		VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11)`,
		string(recordID),
		string(order.ID),
		string(payment.RequestID(payment.NewUUIDv4())),
		string(payment.SourceQuery),
		time.Now().UTC().Truncate(time.Microsecond),
		"{}",
		string(order.Result),
		string(order.Result),
		string(payment.DecisionKeepConfirmed),
		nil,
		nil,
	)
	if err != nil {
		t.Fatalf("fixture handling record insert failed (SQLSTATE %s)", committedTestSQLState(err))
	}
}
