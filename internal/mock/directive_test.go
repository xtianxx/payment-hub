package mock

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"

	"payment-hub/internal/payment"
)

// TestMockPaymentDirective freezes the trusted-test-mode per-Pay result
// directive and committed-consumption proof. It uses only the existing no-PG
// fake DB/Tx from transaction_unit_test.go plus a small concurrency-safe
// wrapper; it never opens PostgreSQL, the launcher, Docker, or the network.

type directiveCallbackCall struct {
	order  payment.PaymentOrderID
	result payment.Result
}

type directiveControl struct {
	mu                   sync.Mutex
	entryCalls           int
	queryEntryCalls      int
	legacyCommittedCalls int
	resultCalls          int
	outcomes             map[payment.PaymentOrderID]PaymentEntryOutcome
	defaultOutcome       PaymentEntryOutcome
	entryErr             error
	resultErr            error
	callbackErr          error
	nilCallback          bool
	callbackCalls        []directiveCallbackCall
}

func newDirectiveControl() *directiveControl {
	return &directiveControl{outcomes: make(map[payment.PaymentOrderID]PaymentEntryOutcome)}
}

func (c *directiveControl) PaymentEntry(context.Context, payment.PaymentOrderID) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.entryCalls++
	return c.entryErr
}

func (c *directiveControl) QueryEntry(context.Context, payment.PaymentOrderID) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.queryEntryCalls++
	return nil
}

func (c *directiveControl) Committed(context.Context, payment.PaymentOrderID) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.legacyCommittedCalls++
	return nil
}

func (c *directiveControl) PaymentEntryResult(_ context.Context, order payment.PaymentOrderID) (PaymentEntryOutcome, error) {
	c.mu.Lock()
	c.resultCalls++
	resultErr := c.resultErr
	outcome, ok := c.outcomes[order]
	if !ok {
		outcome = c.defaultOutcome
	}
	nilCallback := c.nilCallback
	c.mu.Unlock()

	if resultErr != nil {
		return PaymentEntryOutcome{}, resultErr
	}
	if !nilCallback {
		outcome.OnCommitted = func(_ context.Context, result payment.Result) error {
			c.mu.Lock()
			c.callbackCalls = append(c.callbackCalls, directiveCallbackCall{order: order, result: result})
			err := c.callbackErr
			c.mu.Unlock()
			return err
		}
	}
	return outcome, nil
}

func (c *directiveControl) setOutcome(reference payment.PaymentOrderID, outcome PaymentEntryOutcome) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.outcomes[reference] = outcome
}

func (c *directiveControl) counts() (entry, legacyCommitted, result int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.entryCalls, c.legacyCommittedCalls, c.resultCalls
}

func (c *directiveControl) callbacks() []directiveCallbackCall {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]directiveCallbackCall(nil), c.callbackCalls...)
}

func directiveResult(result payment.Result) *payment.Result {
	return &result
}

// directiveConcurrentDB serializes the existing mockUnitDB fake so the
// concurrency subtests are race-free while still exercising real interleaving
// of BeginTx, QueryRow, Commit, and Rollback.
type directiveConcurrentDB struct {
	mu    sync.Mutex
	inner *mockUnitDB
}

func newDirectiveConcurrentDB() *directiveConcurrentDB {
	return &directiveConcurrentDB{inner: newMockUnitDB()}
}

func (db *directiveConcurrentDB) BeginTx(ctx context.Context, txOptions pgx.TxOptions) (pgx.Tx, error) {
	db.mu.Lock()
	defer db.mu.Unlock()
	tx, err := db.inner.BeginTx(ctx, txOptions)
	if err != nil {
		return nil, err
	}
	return &directiveConcurrentTx{Tx: tx, mu: &db.mu}, nil
}

func (db *directiveConcurrentDB) QueryRow(ctx context.Context, sql string, args ...any) pgx.Row {
	db.mu.Lock()
	defer db.mu.Unlock()
	return db.inner.QueryRow(ctx, sql, args...)
}

func (db *directiveConcurrentDB) transaction(reference payment.PaymentOrderID) (payment.MockTransaction, bool) {
	db.mu.Lock()
	defer db.mu.Unlock()
	transaction, ok := db.inner.existing[reference]
	return transaction, ok
}

func (db *directiveConcurrentDB) rowCount(reference payment.PaymentOrderID) int {
	db.mu.Lock()
	defer db.mu.Unlock()
	if _, ok := db.inner.existing[reference]; ok {
		return 1
	}
	return 0
}

type directiveConcurrentTx struct {
	pgx.Tx
	mu *sync.Mutex
}

func (tx *directiveConcurrentTx) QueryRow(ctx context.Context, sql string, args ...any) pgx.Row {
	tx.mu.Lock()
	defer tx.mu.Unlock()
	return tx.Tx.QueryRow(ctx, sql, args...)
}

func (tx *directiveConcurrentTx) Commit(ctx context.Context) error {
	tx.mu.Lock()
	defer tx.mu.Unlock()
	return tx.Tx.Commit(ctx)
}

func (tx *directiveConcurrentTx) Rollback(ctx context.Context) error {
	tx.mu.Lock()
	defer tx.mu.Unlock()
	return tx.Tx.Rollback(ctx)
}

func TestMockPaymentDirective(t *testing.T) {
	t.Run("optional failure overrides generator and callback receives stored result", func(t *testing.T) {
		reference := mockUnitReference(t)
		db := newMockUnitDB()
		control := newDirectiveControl()
		control.defaultOutcome = PaymentEntryOutcome{Result: directiveResult(payment.ResultFailed)}
		generatorCalls := 0
		channel := newChannel(db, NewHooks(control), func(payment.MockPaymentRequest) payment.Result {
			generatorCalls++
			return payment.ResultSucceeded
		})

		got, err := channel.Pay(context.Background(), payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              111,
			Currency:            "USD",
		})
		if err != nil {
			t.Fatalf("optional failure pay failed (%s)", mockUnitErrorClass(err))
		}
		if got.Result != payment.ResultFailed {
			t.Fatalf("expected failed got %s", got.Result)
		}
		if generatorCalls != 0 {
			t.Fatalf("directive still invoked the generator %d times", generatorCalls)
		}
		stored, ok := db.existing[reference]
		if !ok {
			t.Fatal("optional failure did not persist a fact")
		}
		if stored.Result != payment.ResultFailed {
			t.Fatalf("expected failed stored result got %s", stored.Result)
		}
		callbacks := control.callbacks()
		if len(callbacks) != 1 {
			t.Fatalf("expected one committed callback, got %d", len(callbacks))
		}
		if callbacks[0].order != reference || callbacks[0].result != stored.Result {
			t.Fatalf("callback did not receive the stored result")
		}
		entry, legacyCommitted, resultCalls := control.counts()
		if entry != 0 || legacyCommitted != 0 || resultCalls != 1 {
			t.Fatalf("optional control counts: entry=%d legacy_committed=%d result=%d", entry, legacyCommitted, resultCalls)
		}
	})

	t.Run("nil directive keeps default success", func(t *testing.T) {
		reference := mockUnitReference(t)
		db := newMockUnitDB()
		control := newDirectiveControl()
		channel := NewChannel(db, NewHooks(control))

		got, err := channel.Pay(context.Background(), payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              112,
			Currency:            "USD",
		})
		if err != nil {
			t.Fatalf("nil directive pay failed (%s)", mockUnitErrorClass(err))
		}
		if got.Result != payment.ResultSucceeded {
			t.Fatalf("expected succeeded got %s", got.Result)
		}
		callbacks := control.callbacks()
		if len(callbacks) != 1 || callbacks[0].result != payment.ResultSucceeded {
			t.Fatalf("default callback did not receive the stored success")
		}
	})

	t.Run("nil directive keeps injected generator", func(t *testing.T) {
		reference := mockUnitReference(t)
		db := newMockUnitDB()
		control := newDirectiveControl()
		generatorCalls := 0
		channel := newChannel(db, NewHooks(control), func(payment.MockPaymentRequest) payment.Result {
			generatorCalls++
			return payment.ResultFailed
		})

		got, err := channel.Pay(context.Background(), payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              113,
			Currency:            "USD",
		})
		if err != nil {
			t.Fatalf("injected generator pay failed (%s)", mockUnitErrorClass(err))
		}
		if got.Result != payment.ResultFailed {
			t.Fatalf("expected failed got %s", got.Result)
		}
		if generatorCalls != 1 {
			t.Fatalf("expected one generator call, got %d", generatorCalls)
		}
		callbacks := control.callbacks()
		if len(callbacks) != 1 || callbacks[0].result != payment.ResultFailed {
			t.Fatalf("generator callback did not receive the stored failure")
		}
	})

	t.Run("nil zero and legacy hooks keep safe callbacks", func(t *testing.T) {
		reference := mockUnitReference(t)
		ctx := context.Background()

		var nilHooks *Hooks
		outcome, err := nilHooks.PaymentEntryResult(ctx, reference)
		if err != nil {
			t.Fatalf("nil hooks entry result failed (%s)", mockUnitErrorClass(err))
		}
		if outcome.Result != nil || outcome.OnCommitted == nil {
			t.Fatal("nil hooks did not return a nil result with a safe callback")
		}
		if err := outcome.OnCommitted(ctx, payment.ResultSucceeded); err != nil {
			t.Fatalf("nil hooks callback failed (%s)", mockUnitErrorClass(err))
		}

		zero := &Hooks{}
		outcome, err = zero.PaymentEntryResult(ctx, reference)
		if err != nil {
			t.Fatalf("zero hooks entry result failed (%s)", mockUnitErrorClass(err))
		}
		if outcome.Result != nil || outcome.OnCommitted == nil {
			t.Fatal("zero hooks did not return a nil result with a safe callback")
		}
		if err := outcome.OnCommitted(ctx, payment.ResultSucceeded); err != nil {
			t.Fatalf("zero hooks callback failed (%s)", mockUnitErrorClass(err))
		}

		legacy := &mockUnitControl{}
		legacyHooks := NewHooks(legacy)
		outcome, err = legacyHooks.PaymentEntryResult(ctx, reference)
		if err != nil {
			t.Fatalf("legacy entry result failed (%s)", mockUnitErrorClass(err))
		}
		if outcome.Result != nil || outcome.OnCommitted == nil {
			t.Fatal("legacy hooks did not return a nil result with a callback")
		}
		if legacy.paymentEntries != 1 {
			t.Fatalf("legacy entry called %d times", legacy.paymentEntries)
		}
		if err := outcome.OnCommitted(ctx, payment.ResultSucceeded); err != nil {
			t.Fatalf("legacy callback failed (%s)", mockUnitErrorClass(err))
		}
		if legacy.committed != 1 {
			t.Fatalf("legacy committed called %d times", legacy.committed)
		}

		rawLegacy := &mockUnitControl{committedErr: errors.New(mockUnitCanary)}
		rawHooks := NewHooks(rawLegacy)
		outcome, err = rawHooks.PaymentEntryResult(ctx, reference)
		if err != nil {
			t.Fatalf("raw legacy entry result failed (%s)", mockUnitErrorClass(err))
		}
		if err := outcome.OnCommitted(ctx, payment.ResultSucceeded); !errors.Is(err, ErrControlFault) {
			t.Fatalf("raw legacy callback returned %s", mockUnitErrorClass(err))
		}

		hiddenLegacy := &mockUnitControl{committedErr: ErrReplyHidden}
		hiddenHooks := NewHooks(hiddenLegacy)
		outcome, err = hiddenHooks.PaymentEntryResult(ctx, reference)
		if err != nil {
			t.Fatalf("hidden legacy entry result failed (%s)", mockUnitErrorClass(err))
		}
		if err := outcome.OnCommitted(ctx, payment.ResultSucceeded); !errors.Is(err, ErrReplyHidden) {
			t.Fatalf("hidden legacy callback returned %s", mockUnitErrorClass(err))
		}
	})

	t.Run("invalid directive nil callback and entry fault have no effect", func(t *testing.T) {
		t.Run("invalid result", func(t *testing.T) {
			reference := mockUnitReference(t)
			db := newMockUnitDB()
			control := newDirectiveControl()
			control.defaultOutcome = PaymentEntryOutcome{Result: directiveResult(payment.ResultUnknown)}
			channel := NewChannel(db, NewHooks(control))

			_, err := channel.Pay(context.Background(), payment.MockPaymentRequest{
				PlatformReferenceID: reference,
				Amount:              120,
				Currency:            "USD",
			})
			if !errors.Is(err, ErrControlFault) {
				t.Fatalf("invalid directive returned %s", mockUnitErrorClass(err))
			}
			if db.queryCalls != 0 || db.beginCalls != 0 {
				t.Fatal("invalid directive touched the database")
			}
			if len(control.callbacks()) != 0 {
				t.Fatal("invalid directive reached the committed callback")
			}
		})

		t.Run("nil callback", func(t *testing.T) {
			reference := mockUnitReference(t)
			db := newMockUnitDB()
			control := newDirectiveControl()
			control.nilCallback = true
			control.defaultOutcome = PaymentEntryOutcome{Result: directiveResult(payment.ResultFailed)}
			channel := NewChannel(db, NewHooks(control))

			_, err := channel.Pay(context.Background(), payment.MockPaymentRequest{
				PlatformReferenceID: reference,
				Amount:              121,
				Currency:            "USD",
			})
			if !errors.Is(err, ErrControlFault) {
				t.Fatalf("nil callback returned %s", mockUnitErrorClass(err))
			}
			if db.queryCalls != 0 || db.beginCalls != 0 {
				t.Fatal("nil callback touched the database")
			}
			if len(control.callbacks()) != 0 {
				t.Fatal("nil callback reached the committed callback")
			}
		})

		t.Run("entry fault", func(t *testing.T) {
			reference := mockUnitReference(t)
			db := newMockUnitDB()
			control := newDirectiveControl()
			control.resultErr = errors.New(mockUnitCanary)
			channel := NewChannel(db, NewHooks(control))

			_, err := channel.Pay(context.Background(), payment.MockPaymentRequest{
				PlatformReferenceID: reference,
				Amount:              122,
				Currency:            "USD",
			})
			if !errors.Is(err, ErrControlFault) {
				t.Fatalf("entry fault returned %s", mockUnitErrorClass(err))
			}
			if strings.Contains(err.Error(), mockUnitCanary) {
				t.Fatal("entry fault leaked a raw control error")
			}
			if db.queryCalls != 0 || db.beginCalls != 0 {
				t.Fatal("entry fault touched the database")
			}
			if len(control.callbacks()) != 0 {
				t.Fatal("entry fault reached the committed callback")
			}
		})
	})

	t.Run("callback errors are normalized and preserve committed fact", func(t *testing.T) {
		t.Run("raw callback error", func(t *testing.T) {
			reference := mockUnitReference(t)
			db := newMockUnitDB()
			control := newDirectiveControl()
			control.defaultOutcome = PaymentEntryOutcome{Result: directiveResult(payment.ResultFailed)}
			control.callbackErr = errors.New(mockUnitCanary)
			channel := NewChannel(db, NewHooks(control))

			_, err := channel.Pay(context.Background(), payment.MockPaymentRequest{
				PlatformReferenceID: reference,
				Amount:              130,
				Currency:            "USD",
			})
			if !errors.Is(err, ErrControlFault) {
				t.Fatalf("raw callback returned %s", mockUnitErrorClass(err))
			}
			if err.Error() == mockUnitCanary {
				t.Fatal("raw callback error leaked")
			}
			stored, ok := db.existing[reference]
			if !ok || stored.Result != payment.ResultFailed {
				t.Fatal("raw callback changed or removed the committed fact")
			}
			callbacks := control.callbacks()
			if len(callbacks) != 1 || callbacks[0].result != stored.Result {
				t.Fatal("raw callback did not receive the stored result")
			}
		})

		t.Run("reply hidden", func(t *testing.T) {
			reference := mockUnitReference(t)
			db := newMockUnitDB()
			control := newDirectiveControl()
			control.defaultOutcome = PaymentEntryOutcome{Result: directiveResult(payment.ResultFailed)}
			control.callbackErr = ErrReplyHidden
			channel := NewChannel(db, NewHooks(control))

			_, err := channel.Pay(context.Background(), payment.MockPaymentRequest{
				PlatformReferenceID: reference,
				Amount:              131,
				Currency:            "USD",
			})
			if !errors.Is(err, ErrReplyHidden) {
				t.Fatalf("reply hidden callback returned %s", mockUnitErrorClass(err))
			}
			stored, ok := db.existing[reference]
			if !ok || stored.Result != payment.ResultFailed {
				t.Fatal("reply hidden callback changed or removed the committed fact")
			}
			callbacks := control.callbacks()
			if len(callbacks) != 1 || callbacks[0].result != stored.Result {
				t.Fatal("reply hidden callback did not receive the stored result")
			}
		})
	})

	t.Run("early reuse does not change old fact or call callback", func(t *testing.T) {
		reference := mockUnitReference(t)
		existing := payment.MockTransaction{
			ID:                  payment.MockTransactionID(payment.NewUUIDv4()),
			PlatformReferenceID: reference,
			Amount:              140,
			Currency:            "USD",
			Result:              payment.ResultSucceeded,
			ProcessedAt:         time.Now().UTC().Truncate(time.Microsecond),
		}
		db := newMockUnitDB()
		db.existing[reference] = existing
		control := newDirectiveControl()
		control.defaultOutcome = PaymentEntryOutcome{Result: directiveResult(payment.ResultFailed)}
		channel := NewChannel(db, NewHooks(control))

		got, err := channel.Pay(context.Background(), payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              140,
			Currency:            "USD",
		})
		if err != nil {
			t.Fatalf("early reuse pay failed (%s)", mockUnitErrorClass(err))
		}
		if got != existing {
			t.Fatal("early reuse returned a different fact")
		}
		if db.beginCalls != 0 {
			t.Fatalf("early reuse opened %d transactions", db.beginCalls)
		}
		if len(control.callbacks()) != 0 {
			t.Fatal("early reuse invoked the committed callback")
		}
		stored, ok := db.existing[reference]
		if !ok || stored != existing {
			t.Fatal("early reuse changed the old fact")
		}

		_, err = channel.Pay(context.Background(), payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              141,
			Currency:            "USD",
		})
		if !errors.Is(err, payment.ErrConflict) {
			t.Fatalf("conflicting early reuse returned %s", mockUnitErrorClass(err))
		}
		if len(control.callbacks()) != 0 {
			t.Fatal("conflicting early reuse invoked the committed callback")
		}
		stored, ok = db.existing[reference]
		if !ok || stored != existing {
			t.Fatal("conflicting early reuse changed the old fact")
		}
	})

	t.Run("concurrent different outcomes do not cross", func(t *testing.T) {
		first := mockUnitReference(t)
		second := mockUnitReference(t)
		db := newDirectiveConcurrentDB()
		control := newDirectiveControl()
		control.setOutcome(first, PaymentEntryOutcome{Result: directiveResult(payment.ResultFailed)})
		control.setOutcome(second, PaymentEntryOutcome{Result: directiveResult(payment.ResultSucceeded)})
		channel := NewChannel(db, NewHooks(control))

		type payResult struct {
			reference   payment.PaymentOrderID
			transaction payment.MockTransaction
			err         error
		}
		results := make(chan payResult, 2)
		var wg sync.WaitGroup
		for _, reference := range []payment.PaymentOrderID{first, second} {
			wg.Add(1)
			go func(reference payment.PaymentOrderID) {
				defer wg.Done()
				transaction, err := channel.Pay(context.Background(), payment.MockPaymentRequest{
					PlatformReferenceID: reference,
					Amount:              150,
					Currency:            "USD",
				})
				results <- payResult{reference: reference, transaction: transaction, err: err}
			}(reference)
		}
		wg.Wait()
		close(results)

		expected := map[payment.PaymentOrderID]payment.Result{
			first:  payment.ResultFailed,
			second: payment.ResultSucceeded,
		}
		for result := range results {
			if result.err != nil {
				t.Fatalf("concurrent pay failed (%s)", mockUnitErrorClass(result.err))
			}
			if result.transaction.Result != expected[result.reference] {
				t.Fatalf("concurrent pay crossed results for %s: got %s", result.reference, result.transaction.Result)
			}
			stored, ok := db.transaction(result.reference)
			if !ok || stored.Result != expected[result.reference] {
				t.Fatalf("concurrent stored result crossed for %s", result.reference)
			}
		}
		callbacks := control.callbacks()
		if len(callbacks) != 2 {
			t.Fatalf("expected two committed callbacks, got %d", len(callbacks))
		}
		for _, callback := range callbacks {
			if callback.result != expected[callback.order] {
				t.Fatalf("callback crossed results for %s", callback.order)
			}
		}
	})

	t.Run("concurrent same reference reuses one fact and one callback", func(t *testing.T) {
		reference := mockUnitReference(t)
		db := newDirectiveConcurrentDB()
		control := newDirectiveControl()
		control.setOutcome(reference, PaymentEntryOutcome{Result: directiveResult(payment.ResultFailed)})
		channel := NewChannel(db, NewHooks(control))

		type payResult struct {
			transaction payment.MockTransaction
			err         error
		}
		results := make(chan payResult, 2)
		var wg sync.WaitGroup
		for i := 0; i < 2; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				transaction, err := channel.Pay(context.Background(), payment.MockPaymentRequest{
					PlatformReferenceID: reference,
					Amount:              160,
					Currency:            "USD",
				})
				results <- payResult{transaction: transaction, err: err}
			}()
		}
		wg.Wait()
		close(results)

		var firstID payment.MockTransactionID
		for result := range results {
			if result.err != nil {
				t.Fatalf("concurrent reuse pay failed (%s)", mockUnitErrorClass(result.err))
			}
			if result.transaction.Result != payment.ResultFailed {
				t.Fatalf("expected failed got %s", result.transaction.Result)
			}
			if firstID == "" {
				firstID = result.transaction.ID
			} else if result.transaction.ID != firstID {
				t.Fatal("concurrent reuse returned different transaction IDs")
			}
		}
		if db.rowCount(reference) != 1 {
			t.Fatal("concurrent reuse created more than one fact")
		}
		callbacks := control.callbacks()
		if len(callbacks) != 1 {
			t.Fatalf("expected one committed callback, got %d", len(callbacks))
		}
		if callbacks[0].result != payment.ResultFailed {
			t.Fatalf("reuse callback received %s", callbacks[0].result)
		}
	})

	t.Run("commit rollback does not call callback", func(t *testing.T) {
		reference := mockUnitReference(t)
		db := newMockUnitDB()
		db.commitErr = pgx.ErrTxCommitRollback
		db.discardInsert = true
		control := newDirectiveControl()
		control.defaultOutcome = PaymentEntryOutcome{Result: directiveResult(payment.ResultFailed)}
		channel := NewChannel(db, NewHooks(control))

		_, err := channel.Pay(context.Background(), payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              170,
			Currency:            "USD",
		})
		if !errors.Is(err, payment.ErrUnavailable) {
			t.Fatalf("commit rollback returned %s", mockUnitErrorClass(err))
		}
		if len(control.callbacks()) != 0 {
			t.Fatal("commit rollback invoked the committed callback")
		}
		if _, ok := db.existing[reference]; ok {
			t.Fatal("commit rollback left a stored fact")
		}
	})

	t.Run("commit unknown exact readback calls callback with stored result", func(t *testing.T) {
		reference := mockUnitReference(t)
		db := newMockUnitDB()
		db.commitErr = errors.New("mock directive unknown commit")
		control := newDirectiveControl()
		control.defaultOutcome = PaymentEntryOutcome{Result: directiveResult(payment.ResultFailed)}
		channel := NewChannel(db, NewHooks(control))

		got, err := channel.Pay(context.Background(), payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              171,
			Currency:            "USD",
		})
		if err != nil {
			t.Fatalf("commit unknown exact readback failed (%s)", mockUnitErrorClass(err))
		}
		if got.Result != payment.ResultFailed {
			t.Fatalf("expected failed got %s", got.Result)
		}
		stored, ok := db.existing[reference]
		if !ok || stored.Result != payment.ResultFailed {
			t.Fatal("commit unknown exact readback did not find the stored fact")
		}
		callbacks := control.callbacks()
		if len(callbacks) != 1 || callbacks[0].result != stored.Result {
			t.Fatal("commit unknown exact readback did not call the stored-result callback")
		}
	})

	t.Run("commit unknown without visible fact is indeterminate without callback", func(t *testing.T) {
		reference := mockUnitReference(t)
		db := newMockUnitDB()
		db.commitErr = errors.New("mock directive unknown commit")
		db.discardInsert = true
		control := newDirectiveControl()
		control.defaultOutcome = PaymentEntryOutcome{Result: directiveResult(payment.ResultFailed)}
		channel := NewChannel(db, NewHooks(control))

		_, err := channel.Pay(context.Background(), payment.MockPaymentRequest{
			PlatformReferenceID: reference,
			Amount:              172,
			Currency:            "USD",
		})
		if !errors.Is(err, payment.ErrIndeterminate) {
			t.Fatalf("commit unknown without fact returned %s", mockUnitErrorClass(err))
		}
		if len(control.callbacks()) != 0 {
			t.Fatal("indeterminate commit invoked the committed callback")
		}
		if _, ok := db.existing[reference]; ok {
			t.Fatal("indeterminate commit left a stored fact")
		}
	})

	t.Run("result pointer is copied before controller mutation", func(t *testing.T) {
		reference := mockUnitReference(t)
		original := payment.ResultSucceeded
		control := newDirectiveControl()
		control.defaultOutcome = PaymentEntryOutcome{Result: &original}
		hooks := NewHooks(control)

		outcome, err := hooks.PaymentEntryResult(context.Background(), reference)
		if err != nil {
			t.Fatalf("pointer copy entry failed (%s)", mockUnitErrorClass(err))
		}
		if outcome.Result == nil || *outcome.Result != payment.ResultSucceeded {
			t.Fatal("directive result was not returned")
		}
		original = payment.ResultFailed
		if *outcome.Result != payment.ResultSucceeded {
			t.Fatal("controller mutation changed the captured directive result")
		}
		if outcome.OnCommitted == nil {
			t.Fatal("directive callback is nil")
		}
	})
}
