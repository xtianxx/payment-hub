package mock

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"

	"payment-hub/internal/payment"
)

const (
	// mockOperationBudget is the total budget for one Mock operation. It is
	// always clamped by the caller's context deadline.
	mockOperationBudget = 3 * time.Second
	// mockDatabaseBudget covers one complete Mock database operation: pool
	// acquisition, every SQL statement, commit, rollback, and any bounded
	// commit-outcome read-back share this single window.
	mockDatabaseBudget = 2 * time.Second
)

const (
	selectMockTransactionSQL = `
		SELECT id::text, platform_reference_id::text, amount, currency, result, processed_at
		FROM mock_transactions
		WHERE platform_reference_id = $1
	`
	insertMockTransactionReturningSQL = `
		INSERT INTO mock_transactions
			(id, platform_reference_id, amount, currency, result, processed_at)
		VALUES ($1, $2, $3, $4, $5, $6)
		ON CONFLICT (platform_reference_id) DO NOTHING
		RETURNING id::text, platform_reference_id::text, amount, currency, result, processed_at
	`
)

var errInvalidMockResult = errors.New("mock: invalid stored result")

// DB is the narrow database surface used by Channel. A *pgxpool.Pool satisfies
// it directly. Pay uses BeginTx for the channel's own independent transaction;
// QueryRow serves the ordinary original-transaction lookup. The interface does
// not expose the platform order store or allow the channel to join the
// platform's payment transaction.
type DB interface {
	BeginTx(ctx context.Context, txOptions pgx.TxOptions) (pgx.Tx, error)
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
}

// Channel is the in-process Mock payment channel. It stores only its own
// success or failure facts in mock_transactions and never reads or mutates
// platform orders.
type Channel struct {
	db              DB
	hooks           *Hooks
	resultGenerator func(payment.MockPaymentRequest) payment.Result
}

var _ payment.MockPayer = (*Channel)(nil)

// NewChannel returns a Channel whose payment result is explicitly
// ResultSucceeded. It does not select a result from the environment, amount,
// currency, or order number.
func NewChannel(db DB, hooks *Hooks) *Channel {
	return newChannel(db, hooks, defaultMockResult)
}

// newChannel is the private test seam for injecting an explicit result. The
// production constructor uses defaultMockResult; tests must not control results
// through business fields or ambient configuration.
func newChannel(db DB, hooks *Hooks, resultGenerator func(payment.MockPaymentRequest) payment.Result) *Channel {
	if hooks == nil {
		hooks = NewHooks(nil)
	}
	if resultGenerator == nil {
		resultGenerator = defaultMockResult
	}
	return &Channel{
		db:              db,
		hooks:           hooks,
		resultGenerator: resultGenerator,
	}
}

func defaultMockResult(payment.MockPaymentRequest) payment.Result {
	return payment.ResultSucceeded
}

// Pay performs one independent Mock payment. It never reads or joins the
// platform order transaction. A new platform reference creates at most one Mock
// transaction; an existing reference returns the original fact without running
// the committed hook again.
func (c *Channel) Pay(ctx context.Context, request payment.MockPaymentRequest) (payment.MockTransaction, error) {
	if c == nil || c.db == nil || c.hooks == nil || c.resultGenerator == nil {
		return payment.MockTransaction{}, payment.ErrUnavailable
	}
	if ctx == nil || ctx.Err() != nil {
		return payment.MockTransaction{}, payment.ErrUnavailable
	}
	if err := payment.ValidateUUIDv4(string(request.PlatformReferenceID)); err != nil {
		return payment.MockTransaction{}, payment.ErrInvalidInput
	}

	operationCtx, cancelOperation := context.WithTimeout(ctx, mockOperationBudget)
	defer cancelOperation()

	// The entry observation must happen before any deduplication check so every
	// call, including a rejected duplicate, is counted independently. The
	// outcome is local to this Pay call and carries either a trusted directive
	// or the legacy-generator/no-directive path.
	outcome, err := c.hooks.PaymentEntryResult(operationCtx, request.PlatformReferenceID)
	if err != nil {
		return payment.MockTransaction{}, err
	}
	if outcome.OnCommitted == nil {
		return payment.MockTransaction{}, ErrControlFault
	}
	if operationCtx.Err() != nil {
		return payment.MockTransaction{}, payment.ErrUnavailable
	}

	databaseCtx, cancelDatabase := context.WithTimeout(operationCtx, mockDatabaseBudget)
	defer cancelDatabase()

	existing, found, err := findMockTransaction(databaseCtx, c.db, request.PlatformReferenceID)
	if err != nil {
		return payment.MockTransaction{}, payment.ErrUnavailable
	}
	if found {
		return reuseMockTransaction(existing, request)
	}

	var result payment.Result
	if outcome.Result != nil {
		result = *outcome.Result
		if !validMockResult(result) {
			return payment.MockTransaction{}, ErrControlFault
		}
	} else {
		result = c.resultGenerator(request)
		if !validMockResult(result) {
			return payment.MockTransaction{}, payment.ErrUnavailable
		}
	}

	candidate := payment.MockTransaction{
		ID:                  payment.MockTransactionID(payment.NewUUIDv4()),
		PlatformReferenceID: request.PlatformReferenceID,
		Amount:              request.Amount,
		Currency:            request.Currency,
		Result:              result,
		ProcessedAt:         time.Now().UTC().Truncate(time.Microsecond),
	}

	stored, inserted, err := c.insertMockTransaction(databaseCtx, candidate)
	if err != nil {
		return payment.MockTransaction{}, err
	}
	if !inserted {
		// A concurrent call inserted the same platform reference. Read and reuse
		// that committed fact; do not treat this call as a new commit.
		existing, found, err = findMockTransaction(databaseCtx, c.db, request.PlatformReferenceID)
		if err != nil {
			return payment.MockTransaction{}, payment.ErrUnavailable
		}
		if !found {
			return payment.MockTransaction{}, payment.ErrIndeterminate
		}
		return reuseMockTransaction(existing, request)
	}

	// The fact is independently committed. The per-Pay callback receives the
	// actual stored result, never the expected directive value. Reply hiding
	// and control faults are observations of the reply, never changes to the
	// stored payment fact.
	if err := outcome.OnCommitted(operationCtx, stored.Result); err != nil {
		return payment.MockTransaction{}, err
	}
	return stored, nil
}

func (c *Channel) insertMockTransaction(ctx context.Context, candidate payment.MockTransaction) (payment.MockTransaction, bool, error) {
	tx, err := c.db.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if err != nil {
		return payment.MockTransaction{}, false, payment.ErrUnavailable
	}

	txClosed := false
	defer func() {
		if !txClosed {
			// Rollback shares the same complete database window as the SQL,
			// commit, and read-back. A deadline expiry may close the connection;
			// pgxpool still releases its lease.
			_ = tx.Rollback(ctx)
		}
	}()

	row := tx.QueryRow(ctx, insertMockTransactionReturningSQL,
		string(candidate.ID),
		string(candidate.PlatformReferenceID),
		candidate.Amount,
		candidate.Currency,
		string(candidate.Result),
		candidate.ProcessedAt,
	)
	stored, found, err := scanMockTransaction(row)
	if err != nil {
		_ = tx.Rollback(ctx)
		txClosed = true
		return payment.MockTransaction{}, false, payment.ErrUnavailable
	}
	if !found {
		// ON CONFLICT (platform_reference_id) did nothing. Roll back the empty
		// transaction before the independent read of the existing fact.
		_ = tx.Rollback(ctx)
		txClosed = true
		return payment.MockTransaction{}, false, nil
	}

	if err := tx.Commit(ctx); err != nil {
		txClosed = true
		if errors.Is(err, pgx.ErrTxCommitRollback) {
			// PostgreSQL explicitly rolled the transaction back. There is no
			// committed effect to return.
			return payment.MockTransaction{}, false, payment.ErrUnavailable
		}
		return c.resolveUnknownCommit(ctx, candidate)
	}
	txClosed = true
	return stored, true, nil
}

// resolveUnknownCommit performs one bounded read-back after a non-rollback
// commit error. It returns success only when the exact candidate fact is
// visible. The caller still runs the committed hook once for that confirmed
// fact. Any read-back failure or mismatch is indeterminate, never a candidate
// success and never an automatic payment retry.
func (c *Channel) resolveUnknownCommit(ctx context.Context, candidate payment.MockTransaction) (payment.MockTransaction, bool, error) {
	stored, found, err := findMockTransaction(ctx, c.db, candidate.PlatformReferenceID)
	if err != nil || !found {
		return payment.MockTransaction{}, false, payment.ErrIndeterminate
	}
	if stored.ID != candidate.ID ||
		stored.PlatformReferenceID != candidate.PlatformReferenceID ||
		stored.Amount != candidate.Amount ||
		stored.Currency != candidate.Currency ||
		stored.Result != candidate.Result ||
		!stored.ProcessedAt.Equal(candidate.ProcessedAt) {
		return payment.MockTransaction{}, false, payment.ErrIndeterminate
	}
	return stored, true, nil
}

func findMockTransaction(ctx context.Context, db DB, platformReferenceID payment.PaymentOrderID) (payment.MockTransaction, bool, error) {
	return scanMockTransaction(db.QueryRow(ctx, selectMockTransactionSQL, string(platformReferenceID)))
}

func scanMockTransaction(row pgx.Row) (payment.MockTransaction, bool, error) {
	var (
		id, platformReference, currency, result string
		amount                                  int64
		processedAt                             time.Time
	)
	err := row.Scan(&id, &platformReference, &amount, &currency, &result, &processedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return payment.MockTransaction{}, false, nil
	}
	if err != nil {
		return payment.MockTransaction{}, false, err
	}

	transaction := payment.MockTransaction{
		ID:                  payment.MockTransactionID(id),
		PlatformReferenceID: payment.PaymentOrderID(platformReference),
		Amount:              amount,
		Currency:            currency,
		Result:              payment.Result(result),
		ProcessedAt:         processedAt.UTC(),
	}
	if !validMockResult(transaction.Result) {
		return payment.MockTransaction{}, false, errInvalidMockResult
	}
	return transaction, true, nil
}

func validMockResult(result payment.Result) bool {
	return result == payment.ResultSucceeded || result == payment.ResultFailed
}

// reuseMockTransaction returns the original fact for an identical duplicate.
// A conflicting amount or currency is rejected without rewriting the original
// row. This is only the Mock's own duplicate defense; it does not replace the
// platform's payment-eligibility rules.
func reuseMockTransaction(existing payment.MockTransaction, request payment.MockPaymentRequest) (payment.MockTransaction, error) {
	if existing.Amount != request.Amount || existing.Currency != request.Currency {
		return payment.MockTransaction{}, payment.ErrConflict
	}
	return existing, nil
}
