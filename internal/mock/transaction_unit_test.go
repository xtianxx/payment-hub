package mock

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"payment-hub/internal/payment"
)

// These are focused, no-PostgreSQL unit tests for the Mock channel's local
// transaction policy. They use a tiny in-memory DB/Tx fake and never open a
// connection, launcher, Docker resource, or network socket.

const mockUnitCanary = "FAKE_T030_MOCK_UNIT_CANARY"

type mockUnitControl struct {
	paymentEntries  int
	queryEntries    int
	committed       int
	paymentEntryErr error
	queryEntryErr   error
	committedErr    error
}

func (c *mockUnitControl) PaymentEntry(context.Context, payment.PaymentOrderID) error {
	c.paymentEntries++
	return c.paymentEntryErr
}

func (c *mockUnitControl) QueryEntry(context.Context, payment.PaymentOrderID) error {
	c.queryEntries++
	return c.queryEntryErr
}

func (c *mockUnitControl) Committed(context.Context, payment.PaymentOrderID) error {
	c.committed++
	return c.committedErr
}

type mockUnitRow struct {
	transaction payment.MockTransaction
	found       bool
	err         error
}

func (r mockUnitRow) Scan(dest ...any) error {
	if r.err != nil {
		return r.err
	}
	if !r.found {
		return pgx.ErrNoRows
	}
	if len(dest) != 6 {
		return errors.New("mock unit row: unexpected scan arity")
	}
	*dest[0].(*string) = string(r.transaction.ID)
	*dest[1].(*string) = string(r.transaction.PlatformReferenceID)
	*dest[2].(*int64) = r.transaction.Amount
	*dest[3].(*string) = r.transaction.Currency
	*dest[4].(*string) = string(r.transaction.Result)
	*dest[5].(*time.Time) = r.transaction.ProcessedAt
	return nil
}

type mockUnitDB struct {
	existing      map[payment.PaymentOrderID]payment.MockTransaction
	queryErr      error
	beginErr      error
	insertErr     error
	commitErr     error
	discardInsert bool
	beginCalls    int
	queryCalls    int
}

func newMockUnitDB() *mockUnitDB {
	return &mockUnitDB{existing: make(map[payment.PaymentOrderID]payment.MockTransaction)}
}

func (db *mockUnitDB) BeginTx(context.Context, pgx.TxOptions) (pgx.Tx, error) {
	db.beginCalls++
	if db.beginErr != nil {
		return nil, db.beginErr
	}
	return &mockUnitTx{db: db}, nil
}

func (db *mockUnitDB) QueryRow(_ context.Context, _ string, args ...any) pgx.Row {
	db.queryCalls++
	if db.queryErr != nil {
		return mockUnitRow{err: db.queryErr}
	}
	if len(args) != 1 {
		return mockUnitRow{err: errors.New("mock unit db: unexpected query arity")}
	}
	reference, ok := args[0].(string)
	if !ok {
		return mockUnitRow{err: errors.New("mock unit db: unexpected query argument")}
	}
	transaction, found := db.existing[payment.PaymentOrderID(reference)]
	return mockUnitRow{transaction: transaction, found: found}
}

type mockUnitTx struct {
	db     *mockUnitDB
	closed bool
}

func (tx *mockUnitTx) Begin(context.Context) (pgx.Tx, error) {
	return nil, errors.New("mock unit tx: nested transaction unsupported")
}

func (tx *mockUnitTx) Commit(context.Context) error {
	tx.closed = true
	return tx.db.commitErr
}

func (tx *mockUnitTx) Rollback(context.Context) error {
	tx.closed = true
	return nil
}

func (tx *mockUnitTx) CopyFrom(context.Context, pgx.Identifier, []string, pgx.CopyFromSource) (int64, error) {
	return 0, errors.New("mock unit tx: copy unsupported")
}

func (tx *mockUnitTx) SendBatch(context.Context, *pgx.Batch) pgx.BatchResults {
	return nil
}

func (tx *mockUnitTx) LargeObjects() pgx.LargeObjects {
	return pgx.LargeObjects{}
}

func (tx *mockUnitTx) Prepare(context.Context, string, string) (*pgconn.StatementDescription, error) {
	return nil, errors.New("mock unit tx: prepare unsupported")
}

func (tx *mockUnitTx) Exec(context.Context, string, ...any) (pgconn.CommandTag, error) {
	return pgconn.CommandTag{}, errors.New("mock unit tx: exec unsupported")
}

func (tx *mockUnitTx) Query(context.Context, string, ...any) (pgx.Rows, error) {
	return nil, errors.New("mock unit tx: query unsupported")
}

func (tx *mockUnitTx) QueryRow(_ context.Context, _ string, args ...any) pgx.Row {
	if tx.db.insertErr != nil {
		return mockUnitRow{err: tx.db.insertErr}
	}
	if len(args) != 6 {
		return mockUnitRow{err: errors.New("mock unit tx: unexpected insert arity")}
	}
	id, ok := args[0].(string)
	if !ok {
		return mockUnitRow{err: errors.New("mock unit tx: unexpected id")}
	}
	reference, ok := args[1].(string)
	if !ok {
		return mockUnitRow{err: errors.New("mock unit tx: unexpected reference")}
	}
	amount, ok := args[2].(int64)
	if !ok {
		return mockUnitRow{err: errors.New("mock unit tx: unexpected amount")}
	}
	currency, ok := args[3].(string)
	if !ok {
		return mockUnitRow{err: errors.New("mock unit tx: unexpected currency")}
	}
	result, ok := args[4].(string)
	if !ok {
		return mockUnitRow{err: errors.New("mock unit tx: unexpected result")}
	}
	processedAt, ok := args[5].(time.Time)
	if !ok {
		return mockUnitRow{err: errors.New("mock unit tx: unexpected processed time")}
	}

	candidate := payment.MockTransaction{
		ID:                  payment.MockTransactionID(id),
		PlatformReferenceID: payment.PaymentOrderID(reference),
		Amount:              amount,
		Currency:            currency,
		Result:              payment.Result(result),
		ProcessedAt:         processedAt,
	}
	if _, exists := tx.db.existing[candidate.PlatformReferenceID]; exists {
		return mockUnitRow{}
	}
	if !tx.db.discardInsert {
		tx.db.existing[candidate.PlatformReferenceID] = candidate
	}
	return mockUnitRow{transaction: candidate, found: true}
}

func (tx *mockUnitTx) Conn() *pgx.Conn {
	return nil
}

func mockUnitReference(t *testing.T) payment.PaymentOrderID {
	t.Helper()
	reference := payment.PaymentOrderID(payment.NewUUIDv4())
	if err := payment.ValidateUUIDv4(string(reference)); err != nil {
		t.Fatal("mock unit test: generated reference is not UUIDv4")
	}
	return reference
}

func TestMockUnitDuplicateReusesOriginalWithoutCommittedHook(t *testing.T) {
	reference := mockUnitReference(t)
	existing := payment.MockTransaction{
		ID:                  payment.MockTransactionID(payment.NewUUIDv4()),
		PlatformReferenceID: reference,
		Amount:              123,
		Currency:            "USD",
		Result:              payment.ResultSucceeded,
		ProcessedAt:         time.Now().UTC().Truncate(time.Microsecond),
	}
	db := newMockUnitDB()
	db.existing[reference] = existing
	control := &mockUnitControl{}
	channel := NewChannel(db, NewHooks(control))
	ctx := context.Background()

	got, err := channel.Pay(ctx, payment.MockPaymentRequest{
		PlatformReferenceID: reference,
		Amount:              123,
		Currency:            "USD",
	})
	if err != nil {
		t.Fatalf("duplicate pay failed (%s)", mockUnitErrorClass(err))
	}
	if got != existing {
		t.Fatal("duplicate pay did not return the original fact")
	}
	if db.beginCalls != 0 {
		t.Fatalf("duplicate pay opened %d transactions", db.beginCalls)
	}
	if control.paymentEntries != 1 || control.committed != 0 {
		t.Fatalf("duplicate hook counts: entry=%d committed=%d", control.paymentEntries, control.committed)
	}

	_, err = channel.Pay(ctx, payment.MockPaymentRequest{
		PlatformReferenceID: reference,
		Amount:              124,
		Currency:            "USD",
	})
	if !errors.Is(err, payment.ErrConflict) {
		t.Fatalf("conflicting duplicate returned %s", mockUnitErrorClass(err))
	}
	if db.beginCalls != 0 {
		t.Fatalf("conflicting duplicate opened %d transactions", db.beginCalls)
	}
	if control.paymentEntries != 2 || control.committed != 0 {
		t.Fatalf("conflicting duplicate hook counts: entry=%d committed=%d", control.paymentEntries, control.committed)
	}
}

func TestMockUnitInvalidGeneratorDoesNotBeginTransaction(t *testing.T) {
	reference := mockUnitReference(t)
	db := newMockUnitDB()
	control := &mockUnitControl{}
	channel := newChannel(db, NewHooks(control), func(payment.MockPaymentRequest) payment.Result {
		return payment.ResultUnknown
	})

	_, err := channel.Pay(context.Background(), payment.MockPaymentRequest{
		PlatformReferenceID: reference,
		Amount:              100,
		Currency:            "CNY",
	})
	if !errors.Is(err, payment.ErrUnavailable) {
		t.Fatalf("invalid generator returned %s", mockUnitErrorClass(err))
	}
	if db.beginCalls != 0 {
		t.Fatalf("invalid generator opened %d transactions", db.beginCalls)
	}
	if control.committed != 0 {
		t.Fatalf("invalid generator reached the committed hook")
	}
}

func TestMockUnitCanceledAndInvalidInputDoNotObserveOrTouchDB(t *testing.T) {
	reference := mockUnitReference(t)
	db := newMockUnitDB()
	control := &mockUnitControl{}
	channel := NewChannel(db, NewHooks(control))

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := channel.Pay(ctx, payment.MockPaymentRequest{
		PlatformReferenceID: reference,
		Amount:              100,
		Currency:            "CNY",
	})
	if !errors.Is(err, payment.ErrUnavailable) {
		t.Fatalf("canceled pay returned %s", mockUnitErrorClass(err))
	}
	if db.queryCalls != 0 || db.beginCalls != 0 || control.paymentEntries != 0 {
		t.Fatal("canceled pay touched the database or entry hook")
	}

	_, err = channel.Pay(context.Background(), payment.MockPaymentRequest{
		PlatformReferenceID: "not-a-uuid",
		Amount:              100,
		Currency:            "CNY",
	})
	if !errors.Is(err, payment.ErrInvalidInput) {
		t.Fatalf("invalid reference pay returned %s", mockUnitErrorClass(err))
	}
	if db.queryCalls != 0 || db.beginCalls != 0 || control.paymentEntries != 0 {
		t.Fatal("invalid reference pay touched the database or entry hook")
	}

	_, found, err := channel.FindTransactionByPlatformReference(ctx, reference)
	if found {
		t.Fatal("canceled query unexpectedly found a transaction")
	}
	if !errors.Is(err, payment.ErrUnavailable) {
		t.Fatalf("canceled query returned %s", mockUnitErrorClass(err))
	}
	if db.queryCalls != 0 || control.queryEntries != 0 {
		t.Fatal("canceled query touched the database or query hook")
	}

	_, found, err = channel.FindTransactionByPlatformReference(context.Background(), "not-a-uuid")
	if found {
		t.Fatal("invalid reference query unexpectedly found a transaction")
	}
	if !errors.Is(err, payment.ErrInvalidInput) {
		t.Fatalf("invalid reference query returned %s", mockUnitErrorClass(err))
	}
	if db.queryCalls != 0 || control.queryEntries != 0 {
		t.Fatal("invalid reference query touched the database or query hook")
	}
}

func TestMockUnitEntryHookFaultsStopBeforeDatabase(t *testing.T) {
	reference := mockUnitReference(t)
	db := newMockUnitDB()
	control := &mockUnitControl{
		paymentEntryErr: errors.New(mockUnitCanary),
		queryEntryErr:   errors.New(mockUnitCanary),
	}
	channel := NewChannel(db, NewHooks(control))

	_, err := channel.Pay(context.Background(), payment.MockPaymentRequest{
		PlatformReferenceID: reference,
		Amount:              100,
		Currency:            "CNY",
	})
	if !errors.Is(err, ErrControlFault) {
		t.Fatalf("payment entry fault returned %s", mockUnitErrorClass(err))
	}
	if db.queryCalls != 0 || db.beginCalls != 0 {
		t.Fatal("payment entry fault touched the database")
	}
	if control.paymentEntries != 1 || control.committed != 0 {
		t.Fatalf("payment entry fault hook counts: entry=%d committed=%d", control.paymentEntries, control.committed)
	}

	_, found, err := channel.FindTransactionByPlatformReference(context.Background(), reference)
	if found {
		t.Fatal("query entry fault unexpectedly found a transaction")
	}
	if !errors.Is(err, ErrControlFault) {
		t.Fatalf("query entry fault returned %s", mockUnitErrorClass(err))
	}
	if db.queryCalls != 0 {
		t.Fatal("query entry fault touched the database")
	}
	if control.queryEntries != 1 {
		t.Fatalf("expected one query entry observation, got %d", control.queryEntries)
	}
}

func TestMockUnitCommitUnknownExactReadbackCallsCommittedOnce(t *testing.T) {
	reference := mockUnitReference(t)
	db := newMockUnitDB()
	db.commitErr = errors.New("mock unit unknown commit")
	control := &mockUnitControl{}
	channel := NewChannel(db, NewHooks(control))

	got, err := channel.Pay(context.Background(), payment.MockPaymentRequest{
		PlatformReferenceID: reference,
		Amount:              200,
		Currency:            "USD",
	})
	if err != nil {
		t.Fatalf("commit unknown with exact readback failed (%s)", mockUnitErrorClass(err))
	}
	if got.PlatformReferenceID != reference || got.Amount != 200 || got.Currency != "USD" {
		t.Fatal("commit unknown readback returned mismatched facts")
	}
	if db.beginCalls != 1 || db.queryCalls != 2 {
		t.Fatalf("commit unknown calls: begin=%d query=%d", db.beginCalls, db.queryCalls)
	}
	if control.committed != 1 {
		t.Fatalf("expected one committed hook, got %d", control.committed)
	}
}

func TestMockUnitCommitUnknownWithoutVisibleFactIsIndeterminate(t *testing.T) {
	reference := mockUnitReference(t)
	db := newMockUnitDB()
	db.commitErr = errors.New("mock unit unknown commit")
	db.discardInsert = true
	control := &mockUnitControl{}
	channel := NewChannel(db, NewHooks(control))

	_, err := channel.Pay(context.Background(), payment.MockPaymentRequest{
		PlatformReferenceID: reference,
		Amount:              200,
		Currency:            "USD",
	})
	if !errors.Is(err, payment.ErrIndeterminate) {
		t.Fatalf("commit unknown without fact returned %s", mockUnitErrorClass(err))
	}
	if control.committed != 0 {
		t.Fatal("indeterminate commit reached the committed hook")
	}
}

func TestMockUnitCommitRollbackIsUnavailable(t *testing.T) {
	reference := mockUnitReference(t)
	db := newMockUnitDB()
	db.commitErr = pgx.ErrTxCommitRollback
	db.discardInsert = true
	control := &mockUnitControl{}
	channel := NewChannel(db, NewHooks(control))

	_, err := channel.Pay(context.Background(), payment.MockPaymentRequest{
		PlatformReferenceID: reference,
		Amount:              200,
		Currency:            "USD",
	})
	if !errors.Is(err, payment.ErrUnavailable) {
		t.Fatalf("known rollback returned %s", mockUnitErrorClass(err))
	}
	if control.committed != 0 {
		t.Fatal("known rollback reached the committed hook")
	}
}

func TestMockUnitCommitUnknownReadbackPreservesReplyHidden(t *testing.T) {
	reference := mockUnitReference(t)
	db := newMockUnitDB()
	db.commitErr = errors.New("mock unit unknown commit")
	control := &mockUnitControl{committedErr: ErrReplyHidden}
	channel := NewChannel(db, NewHooks(control))

	_, err := channel.Pay(context.Background(), payment.MockPaymentRequest{
		PlatformReferenceID: reference,
		Amount:              200,
		Currency:            "USD",
	})
	if !errors.Is(err, ErrReplyHidden) {
		t.Fatalf("readback swallowed reply hiding (%s)", mockUnitErrorClass(err))
	}
	if control.committed != 1 {
		t.Fatalf("expected one committed hook, got %d", control.committed)
	}
}

func TestMockUnitQueryNotFoundAndFailure(t *testing.T) {
	reference := mockUnitReference(t)
	db := newMockUnitDB()
	control := &mockUnitControl{}
	channel := NewChannel(db, NewHooks(control))

	transaction, found, err := channel.FindTransactionByPlatformReference(context.Background(), reference)
	if err != nil || found {
		t.Fatalf("missing query returned found=%t error=%s", found, mockUnitErrorClass(err))
	}
	if transaction.ID != "" {
		t.Fatal("missing query returned a transaction")
	}
	if control.queryEntries != 1 || db.queryCalls != 1 {
		t.Fatalf("missing query calls: hook=%d db=%d", control.queryEntries, db.queryCalls)
	}

	db.queryErr = errors.New(mockUnitCanary)
	_, found, err = channel.FindTransactionByPlatformReference(context.Background(), reference)
	if found {
		t.Fatal("failed query unexpectedly found a transaction")
	}
	if !errors.Is(err, payment.ErrUnavailable) {
		t.Fatalf("failed query returned %s", mockUnitErrorClass(err))
	}
	if control.queryEntries != 2 || db.queryCalls != 2 {
		t.Fatalf("failed query calls: hook=%d db=%d", control.queryEntries, db.queryCalls)
	}
}

func mockUnitErrorClass(err error) string {
	switch {
	case err == nil:
		return "none"
	case errors.Is(err, payment.ErrUnavailable):
		return "unavailable"
	case errors.Is(err, payment.ErrInvalidInput):
		return "invalid input"
	case errors.Is(err, payment.ErrConflict):
		return "conflict"
	case errors.Is(err, payment.ErrIndeterminate):
		return "indeterminate"
	case errors.Is(err, ErrReplyHidden):
		return "reply hidden"
	case errors.Is(err, ErrControlFault):
		return "control fault"
	default:
		return "unexpected"
	}
}
