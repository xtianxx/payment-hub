package postgres

import (
	"context"
	"errors"
	"reflect"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"payment-hub/internal/payment"
)

// registrationBudget is the complete short-transaction budget for one
// RegisterOrder call. It covers acquiring the connection, BEGIN, the insert or
// conflict read, COMMIT, rollback, and the postcommit observation hook; it is
// never renewed per SQL statement.
const registrationBudget = 2 * time.Second

const (
	registerOrderInsertSQL = `INSERT INTO payment_orders
		(id, client_id, business_order_no, amount, currency, result, mock_transaction_id, registered_at, confirmed_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
		ON CONFLICT (client_id, business_order_no) DO NOTHING
		RETURNING id::text, client_id, business_order_no, amount, currency, result,
		          mock_transaction_id::text, registered_at, confirmed_at`

	selectOrderByIDSQL = `SELECT id::text, client_id, business_order_no, amount, currency, result,
		mock_transaction_id::text, registered_at, confirmed_at
		FROM payment_orders
		WHERE id = $1 AND client_id = $2`

	selectOrderByBusinessKeySQL = `SELECT id::text, client_id, business_order_no, amount, currency, result,
		mock_transaction_id::text, registered_at, confirmed_at
		FROM payment_orders
		WHERE client_id = $1 AND business_order_no = $2`
)

// DB is the narrow database surface required by Store. A *pgxpool.Pool
// satisfies it directly. BeginTx is used only for the short registration and
// observation-save transactions; QueryRow supports independent committed
// read-back outside those transactions.
type DB interface {
	BeginTx(context.Context, pgx.TxOptions) (pgx.Tx, error)
	QueryRow(context.Context, string, ...any) pgx.Row
}

// queryRower is the read-only part shared by DB and pgx.Tx. It keeps the
// private scoped-read helpers usable both outside and inside a transaction
// without exposing a transaction or a CRUD surface.
type queryRower interface {
	QueryRow(context.Context, string, ...any) pgx.Row
}

// Store is the PostgreSQL persistence implementation of the payment ports.
type Store struct {
	db    DB
	hooks *Hooks
}

// NewStore returns a Store using the supplied narrow database handle. A nil
// database makes the port methods report ErrUnavailable; nil hooks are
// normalized to a safe no-op.
func NewStore(db DB, hooks *Hooks) *Store {
	if isNilDB(db) {
		db = nil
	}
	if hooks == nil {
		hooks = NewHooks(nil)
	}
	return &Store{db: db, hooks: hooks}
}

func isNilDB(db DB) bool {
	if db == nil {
		return true
	}
	value := reflect.ValueOf(db)
	switch value.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Ptr, reflect.Slice:
		return value.IsNil()
	default:
		return false
	}
}

var (
	_ payment.OrderRegistrar      = (*Store)(nil)
	_ payment.ObservationRecorder = (*Store)(nil)
)

// RegisterOrder performs the T029 Read Committed short registration
// transaction. Only a real insert whose commit is confirmed returns
// RegisterOutcomeCreated. A business-key conflict is read through a separate
// committed query after the insert transaction has ended and returns
// RegisterOutcomeReused; a commit whose outcome is unknown returns
// ErrIndeterminate and never grants Created or Reused eligibility.
func (s *Store) RegisterOrder(ctx context.Context, input payment.RegisterOrderInput) (payment.RegisterOrderResult, error) {
	if s == nil || isNilDB(s.db) {
		return payment.RegisterOrderResult{}, payment.ErrUnavailable
	}
	if ctx == nil {
		return payment.RegisterOrderResult{}, payment.ErrInvalidInput
	}
	if err := validateRegisterInput(input); err != nil {
		return payment.RegisterOrderResult{}, payment.ErrInvalidInput
	}
	if ctx.Err() != nil {
		return payment.RegisterOrderResult{}, payment.ErrUnavailable
	}

	opCtx, cancel := context.WithTimeout(ctx, registrationBudget)
	defer cancel()

	orderID := payment.PaymentOrderID(payment.NewUUIDv4())
	registeredAt := time.Now().UTC().Truncate(time.Microsecond)

	tx, err := s.db.BeginTx(opCtx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if err != nil {
		return payment.RegisterOrderResult{}, payment.ErrUnavailable
	}
	if tx == nil {
		return payment.RegisterOrderResult{}, payment.ErrUnavailable
	}
	// Rollback is registered after cancel so it runs before cancel and uses the
	// same still-live operation deadline. ErrTxClosed after a commit is
	// expected and ignored.
	defer tx.Rollback(opCtx)

	order, err := scanPaymentOrder(tx.QueryRow(opCtx, registerOrderInsertSQL,
		string(orderID),
		input.ClientID,
		input.BusinessOrderNo,
		input.Amount,
		input.Currency,
		string(payment.ResultUnknown),
		nil,
		registeredAt,
		nil,
	))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return s.reuseRegisteredOrder(opCtx, input, tx)
		}
		return payment.RegisterOrderResult{}, classifyRegistrationError(err)
	}

	if err := tx.Commit(opCtx); err != nil {
		if errors.Is(err, pgx.ErrTxCommitRollback) {
			return payment.RegisterOrderResult{}, payment.ErrUnavailable
		}
		// Any other commit error leaves the outcome unknown. Even if the row can
		// later be read, this call cannot claim Created eligibility.
		return payment.RegisterOrderResult{}, payment.ErrIndeterminate
	}

	if err := s.hooks.Registered(opCtx, order.ID); err != nil {
		if errors.Is(err, ErrReplyHidden) {
			return payment.RegisterOrderResult{}, ErrReplyHidden
		}
		return payment.RegisterOrderResult{}, payment.ErrIndeterminate
	}

	return payment.RegisterOrderResult{Order: order, Outcome: payment.RegisterOutcomeCreated}, nil
}

// reuseRegisteredOrder ends the conflict insert transaction and then reads the
// existing order through an independent committed query. Reuse is returned
// only after the insert transaction has ended and the old row was read
// reliably.
func (s *Store) reuseRegisteredOrder(ctx context.Context, input payment.RegisterOrderInput, tx pgx.Tx) (payment.RegisterOrderResult, error) {
	if err := tx.Rollback(ctx); err != nil && !errors.Is(err, pgx.ErrTxClosed) {
		return payment.RegisterOrderResult{}, payment.ErrUnavailable
	}
	order, found, err := s.readOrderByBusinessKeyScoped(ctx, input.ClientID, input.BusinessOrderNo)
	if err != nil {
		return payment.RegisterOrderResult{}, err
	}
	if !found {
		return payment.RegisterOrderResult{}, payment.ErrUnavailable
	}
	return payment.RegisterOrderResult{Order: order, Outcome: payment.RegisterOutcomeReused}, nil
}

func validateRegisterInput(input payment.RegisterOrderInput) error {
	if payment.ValidateClientID(input.ClientID) != nil {
		return payment.ErrInvalidInput
	}
	if payment.ValidateBusinessOrderNo(input.BusinessOrderNo) != nil {
		return payment.ErrInvalidInput
	}
	if payment.ValidateAmount(input.Amount) != nil {
		return payment.ErrInvalidInput
	}
	if payment.ValidateCurrency(input.Currency) != nil {
		return payment.ErrInvalidInput
	}
	return nil
}

func classifyRegistrationError(err error) error {
	if err == nil {
		return nil
	}
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == "23505" {
		// A unique collision other than the targeted business-key conflict is
		// a definite non-business registration rejection. The primary-key and
		// mock-association keys fall here. It must not be recovered as intent
		// reuse or grant payment eligibility.
		return payment.ErrRegistrationRejected
	}
	return payment.ErrUnavailable
}

func scanPaymentOrder(row pgx.Row) (payment.PaymentOrder, error) {
	var (
		idText          string
		clientID        string
		businessOrderNo string
		amount          int64
		currency        string
		resultText      string
		mockIDText      *string
		registeredAt    time.Time
		confirmedAt     *time.Time
	)
	if err := row.Scan(
		&idText,
		&clientID,
		&businessOrderNo,
		&amount,
		&currency,
		&resultText,
		&mockIDText,
		&registeredAt,
		&confirmedAt,
	); err != nil {
		return payment.PaymentOrder{}, err
	}

	var mockID *payment.MockTransactionID
	if mockIDText != nil {
		id := payment.MockTransactionID(*mockIDText)
		mockID = &id
	}
	var confirmed *time.Time
	if confirmedAt != nil {
		at := confirmedAt.UTC()
		confirmed = &at
	}

	return payment.PaymentOrder{
		ID:                payment.PaymentOrderID(idText),
		ClientID:          clientID,
		BusinessOrderNo:   businessOrderNo,
		Amount:            amount,
		Currency:          currency,
		Result:            payment.Result(resultText),
		MockTransactionID: mockID,
		RegisteredAt:      registeredAt.UTC(),
		ConfirmedAt:       confirmed,
	}, nil
}

// readOrderByIDScoped is the private committed scoped read reused by the
// save-precheck/read-back paths. It is not the public T034 lookup port.
func (s *Store) readOrderByIDScoped(ctx context.Context, clientID string, orderID payment.PaymentOrderID) (payment.PaymentOrder, bool, error) {
	if s == nil || isNilDB(s.db) {
		return payment.PaymentOrder{}, false, payment.ErrUnavailable
	}
	return queryOrderByIDScoped(ctx, s.db, clientID, orderID)
}

func queryOrderByIDScoped(ctx context.Context, q queryRower, clientID string, orderID payment.PaymentOrderID) (payment.PaymentOrder, bool, error) {
	order, err := scanPaymentOrder(q.QueryRow(ctx, selectOrderByIDSQL, string(orderID), clientID))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return payment.PaymentOrder{}, false, nil
		}
		return payment.PaymentOrder{}, false, payment.ErrUnavailable
	}
	return order, true, nil
}

func (s *Store) readOrderByBusinessKeyScoped(ctx context.Context, clientID, businessOrderNo string) (payment.PaymentOrder, bool, error) {
	if s == nil || isNilDB(s.db) {
		return payment.PaymentOrder{}, false, payment.ErrUnavailable
	}
	return queryOrderByBusinessKeyScoped(ctx, s.db, clientID, businessOrderNo)
}

func queryOrderByBusinessKeyScoped(ctx context.Context, q queryRower, clientID, businessOrderNo string) (payment.PaymentOrder, bool, error) {
	order, err := scanPaymentOrder(q.QueryRow(ctx, selectOrderByBusinessKeySQL, clientID, businessOrderNo))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return payment.PaymentOrder{}, false, nil
		}
		return payment.PaymentOrder{}, false, payment.ErrUnavailable
	}
	return order, true, nil
}
