package payment

import (
	"context"
	"errors"
	"testing"
	"time"
)

// The protocol budgets are deliberately repeated here instead of referencing
// the production constants: these tests freeze the externally promised
// windows and must fail if a production timeout is silently widened.
const (
	cancelBoundsMainBudget   = 10 * time.Second
	cancelBoundsMockBudget   = 3 * time.Second
	cancelBoundsFinishBudget = 2 * time.Second

	cancelBoundsDeadlineSlack = 100 * time.Millisecond
	cancelBoundsElapsedSlack  = 500 * time.Millisecond
	cancelBoundsAwaitTimeout  = 5 * time.Second

	cancelBoundsOrderID           = PaymentOrderID("a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d")
	cancelBoundsMockTransactionID = MockTransactionID("b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e")
	cancelBoundsRequestID         = RequestID("c3d4e5f6-a7b8-4c9d-8e0f-2a3b4c5d6e7f")
	cancelBoundsClientID          = "client_cancel_bounds"
	cancelBoundsBusinessOrderNo   = "M-CANCEL-BOUNDS-1"
)

func cancelBoundsTime() time.Time {
	return time.Date(2026, 11, 11, 9, 0, 0, 0, time.UTC)
}

func cancelBoundsUnknownOrder() PaymentOrder {
	return PaymentOrder{
		ID:              cancelBoundsOrderID,
		ClientID:        cancelBoundsClientID,
		BusinessOrderNo: cancelBoundsBusinessOrderNo,
		Amount:          100,
		Currency:        "CNY",
		Result:          ResultUnknown,
		RegisteredAt:    cancelBoundsTime(),
	}
}

func cancelBoundsSubmitInput() RegisterOrderInput {
	return RegisterOrderInput{
		ClientID:        cancelBoundsClientID,
		BusinessOrderNo: cancelBoundsBusinessOrderNo,
		Amount:          100,
		Currency:        "CNY",
	}
}

func cancelBoundsQueryInput() QueryInput {
	return QueryInput{ClientID: cancelBoundsClientID, PaymentOrderID: cancelBoundsOrderID}
}

func cancelBoundsTransaction(order PaymentOrder, result Result) MockTransaction {
	return MockTransaction{
		ID:                  cancelBoundsMockTransactionID,
		PlatformReferenceID: order.ID,
		Amount:              order.Amount,
		Currency:            order.Currency,
		Result:              result,
		ProcessedAt:         cancelBoundsTime(),
	}
}

type cancelBoundsRegistrar struct {
	calls         int
	callAt        time.Time
	deadline      time.Time
	ctxErrAtEntry error
	result        RegisterOrderResult
	fn            func(context.Context, RegisterOrderInput) (RegisterOrderResult, error)
}

func (f *cancelBoundsRegistrar) RegisterOrder(ctx context.Context, input RegisterOrderInput) (RegisterOrderResult, error) {
	f.calls++
	f.callAt = time.Now()
	f.deadline, _ = ctx.Deadline()
	f.ctxErrAtEntry = ctx.Err()
	if f.fn != nil {
		return f.fn(ctx, input)
	}
	return f.result, nil
}

type cancelBoundsPayer struct {
	calls         int
	callAt        time.Time
	deadline      time.Time
	ctxErrAtEntry error
	request       MockPaymentRequest
	transaction   MockTransaction
	err           error
	fn            func(context.Context, MockPaymentRequest) (MockTransaction, error)
}

func (f *cancelBoundsPayer) Pay(ctx context.Context, request MockPaymentRequest) (MockTransaction, error) {
	f.calls++
	f.callAt = time.Now()
	f.deadline, _ = ctx.Deadline()
	f.ctxErrAtEntry = ctx.Err()
	f.request = request
	if f.fn != nil {
		return f.fn(ctx, request)
	}
	return f.transaction, f.err
}

type cancelBoundsRecorder struct {
	calls         int
	callAt        time.Time
	deadline      time.Time
	ctxErrAtEntry error
	observation   Observation
	result        SaveObservationResult
	err           error
	fn            func(context.Context, Observation) (SaveObservationResult, error)
}

func (f *cancelBoundsRecorder) SaveObservation(ctx context.Context, observation Observation) (SaveObservationResult, error) {
	f.calls++
	f.callAt = time.Now()
	f.deadline, _ = ctx.Deadline()
	f.ctxErrAtEntry = ctx.Err()
	f.observation = observation
	if f.fn != nil {
		return f.fn(ctx, observation)
	}
	return f.result, f.err
}

type cancelBoundsLookup struct {
	byIDCalls       int
	byBusinessCalls int

	byIDCallAt         time.Time
	byBusinessCallAt   time.Time
	byIDDeadline       time.Time
	byBusinessDeadline time.Time
	byIDCtxErr         error
	byBusinessCtxErr   error

	byIDOrder       PaymentOrder
	byIDFound       bool
	byIDErr         error
	byBusinessOrder PaymentOrder
	byBusinessFound bool
	byBusinessErr   error

	byIDFn       func(context.Context, string, PaymentOrderID) (PaymentOrder, bool, error)
	byBusinessFn func(context.Context, string, string) (PaymentOrder, bool, error)
}

func (f *cancelBoundsLookup) FindOrderByID(ctx context.Context, clientID string, paymentOrderID PaymentOrderID) (PaymentOrder, bool, error) {
	f.byIDCalls++
	f.byIDCallAt = time.Now()
	f.byIDDeadline, _ = ctx.Deadline()
	f.byIDCtxErr = ctx.Err()
	if f.byIDFn != nil {
		return f.byIDFn(ctx, clientID, paymentOrderID)
	}
	return f.byIDOrder, f.byIDFound, f.byIDErr
}

func (f *cancelBoundsLookup) FindOrderByBusinessOrderNo(ctx context.Context, clientID string, businessOrderNo string) (PaymentOrder, bool, error) {
	f.byBusinessCalls++
	f.byBusinessCallAt = time.Now()
	f.byBusinessDeadline, _ = ctx.Deadline()
	f.byBusinessCtxErr = ctx.Err()
	if f.byBusinessFn != nil {
		return f.byBusinessFn(ctx, clientID, businessOrderNo)
	}
	return f.byBusinessOrder, f.byBusinessFound, f.byBusinessErr
}

type cancelBoundsMockLookup struct {
	calls         int
	callAt        time.Time
	deadline      time.Time
	ctxErrAtEntry error
	reference     PaymentOrderID
	transaction   MockTransaction
	found         bool
	err           error
	fn            func(context.Context, PaymentOrderID) (MockTransaction, bool, error)
}

func (f *cancelBoundsMockLookup) FindTransactionByPlatformReference(ctx context.Context, platformReferenceID PaymentOrderID) (MockTransaction, bool, error) {
	f.calls++
	f.callAt = time.Now()
	f.deadline, _ = ctx.Deadline()
	f.ctxErrAtEntry = ctx.Err()
	f.reference = platformReferenceID
	if f.fn != nil {
		return f.fn(ctx, platformReferenceID)
	}
	return f.transaction, f.found, f.err
}

type cancelBoundsSubmitDeps struct {
	registrar *cancelBoundsRegistrar
	lookup    *cancelBoundsLookup
	recorder  *cancelBoundsRecorder
	payer     *cancelBoundsPayer
}

func cancelBoundsSubmitDepsFor(order PaymentOrder) *cancelBoundsSubmitDeps {
	return &cancelBoundsSubmitDeps{
		registrar: &cancelBoundsRegistrar{
			result: RegisterOrderResult{Order: order, Outcome: RegisterOutcomeCreated},
		},
		lookup: &cancelBoundsLookup{
			byIDOrder:       order,
			byIDFound:       true,
			byBusinessOrder: order,
			byBusinessFound: true,
		},
		recorder: &cancelBoundsRecorder{fn: cancelBoundsDecideRecorder(order)},
		payer:    &cancelBoundsPayer{transaction: cancelBoundsTransaction(order, ResultSucceeded)},
	}
}

func (d *cancelBoundsSubmitDeps) submitter(lifecycle context.Context) *Submitter {
	return NewSubmitter(d.registrar, d.lookup, d.recorder, d.payer, lifecycle)
}

type cancelBoundsQueryDeps struct {
	lookup   *cancelBoundsLookup
	recorder *cancelBoundsRecorder
	mock     *cancelBoundsMockLookup
}

func cancelBoundsQueryDepsFor(order PaymentOrder) *cancelBoundsQueryDeps {
	return &cancelBoundsQueryDeps{
		lookup: &cancelBoundsLookup{
			byIDOrder:       order,
			byIDFound:       true,
			byBusinessOrder: order,
			byBusinessFound: true,
		},
		recorder: &cancelBoundsRecorder{fn: cancelBoundsDecideRecorder(order)},
		mock: &cancelBoundsMockLookup{
			transaction: cancelBoundsTransaction(order, ResultSucceeded),
			found:       true,
		},
	}
}

func (d *cancelBoundsQueryDeps) service(lifecycle context.Context) *QueryService {
	return NewQueryService(d.lookup, d.recorder, d.mock, lifecycle)
}

func cancelBoundsDecideRecorder(base PaymentOrder) func(context.Context, Observation) (SaveObservationResult, error) {
	return func(_ context.Context, observation Observation) (SaveObservationResult, error) {
		updated, record, err := DecideObservation(base, observation, cancelBoundsTime())
		if err != nil {
			return SaveObservationResult{}, err
		}
		return SaveObservationResult{Order: updated, Record: record}, nil
	}
}

type cancelBoundsSubmitOutcome struct {
	result SubmitResult
	err    error
}

type cancelBoundsQueryOutcome struct {
	result QueryResult
	err    error
}

func cancelBoundsStartSubmit(service *Submitter, ctx context.Context, requestID RequestID, input RegisterOrderInput) <-chan cancelBoundsSubmitOutcome {
	done := make(chan cancelBoundsSubmitOutcome, 1)
	go func() {
		result, err := service.Submit(ctx, requestID, input)
		done <- cancelBoundsSubmitOutcome{result: result, err: err}
	}()
	return done
}

func cancelBoundsStartQuery(service *QueryService, ctx context.Context, requestID RequestID, input QueryInput) <-chan cancelBoundsQueryOutcome {
	done := make(chan cancelBoundsQueryOutcome, 1)
	go func() {
		result, err := service.Query(ctx, requestID, input)
		done <- cancelBoundsQueryOutcome{result: result, err: err}
	}()
	return done
}

func cancelBoundsAwait[T any](t *testing.T, done <-chan T, what string) T {
	t.Helper()
	select {
	case got := <-done:
		return got
	case <-time.After(cancelBoundsAwaitTimeout):
		t.Fatalf("%s did not finish", what)
		var zero T
		return zero
	}
}

func cancelBoundsWaitClosed(t *testing.T, ch <-chan struct{}, what string) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(2 * time.Second):
		t.Fatalf("%s was not reached", what)
	}
}

func cancelBoundsAssertDeadline(t *testing.T, got time.Time, callAt time.Time, want time.Duration, what string) time.Duration {
	t.Helper()
	if got.IsZero() {
		t.Fatalf("%s deadline is missing", what)
	}
	delta := got.Sub(callAt)
	if delta < want-cancelBoundsDeadlineSlack || delta > want+cancelBoundsDeadlineSlack {
		t.Fatalf("%s deadline delta = %s, want %s +/- %s", what, delta, want, cancelBoundsDeadlineSlack)
	}
	return delta
}

func cancelBoundsAssertElapsed(t *testing.T, got, want time.Duration, what string) {
	t.Helper()
	if got < want-cancelBoundsDeadlineSlack || got > want+cancelBoundsElapsedSlack {
		t.Fatalf("%s elapsed = %s, want %s -%s +%s", what, got, want, cancelBoundsDeadlineSlack, cancelBoundsElapsedSlack)
	}
}

func cancelBoundsAssertSameDeadline(t *testing.T, first, second time.Time, what string) {
	t.Helper()
	if first.IsZero() || second.IsZero() {
		t.Fatalf("%s deadlines must both be set", what)
	}
	if !first.Equal(second) {
		t.Fatalf("%s deadlines differ: %s vs %s", what, first, second)
	}
}

func cancelBoundsAssertIssueCodes(t *testing.T, got []Issue, want ...string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("expected %d issues, got %d", len(want), len(got))
	}
	for i := range want {
		if got[i].Code != want[i] {
			t.Fatalf("issue %d code = %q, want %q", i, got[i].Code, want[i])
		}
	}
}

// TestPaymentCancellationBounds freezes the default cancellation budgets and
// the boundaries around Mock entry and finish. It captures absolute context
// deadlines instead of waiting for the ten-second main window; the only full
// waits are one ordinary three-second Mock timeout and one two-second finish.
func TestPaymentCancellationBounds(t *testing.T) {
	t.Run("main_mock_and_finish_deadlines_are_bounded_and_ordered", func(t *testing.T) {
		base := cancelBoundsUnknownOrder()
		deps := cancelBoundsSubmitDepsFor(base)

		start := time.Now()
		got, err := deps.submitter(context.Background()).Submit(context.Background(), cancelBoundsRequestID, cancelBoundsSubmitInput())
		if err != nil {
			t.Fatalf("Submit returned error: %v", err)
		}
		if !got.Created || got.Order.Result != ResultSucceeded {
			t.Fatalf("expected a created confirmed order, got created=%t result=%q", got.Created, got.Order.Result)
		}
		cancelBoundsAssertIssueCodes(t, got.Issues)

		if deps.registrar.calls != 1 || deps.payer.calls != 1 || deps.recorder.calls != 1 {
			t.Fatalf("expected one registration, one Mock call, and one save; got %d/%d/%d",
				deps.registrar.calls, deps.payer.calls, deps.recorder.calls)
		}
		if deps.lookup.byIDCalls != 0 || deps.lookup.byBusinessCalls != 0 {
			t.Fatalf("successful save must not read back")
		}

		mainDelta := cancelBoundsAssertDeadline(t, deps.registrar.deadline, deps.registrar.callAt, cancelBoundsMainBudget, "main")
		mockDelta := cancelBoundsAssertDeadline(t, deps.payer.deadline, deps.payer.callAt, cancelBoundsMockBudget, "Mock")
		finishDelta := cancelBoundsAssertDeadline(t, deps.recorder.deadline, deps.recorder.callAt, cancelBoundsFinishBudget, "finish")

		if !deps.payer.deadline.Before(deps.registrar.deadline) {
			t.Fatalf("Mock deadline %s is not earlier than main deadline %s", deps.payer.deadline, deps.registrar.deadline)
		}
		if gap := deps.registrar.deadline.Sub(deps.payer.deadline); gap < 6*time.Second {
			t.Fatalf("Mock and main deadlines are too close: %s", gap)
		}
		t.Logf("cancel-bounds default: main_delta=%s mock_delta=%s finish_delta=%s elapsed=%s",
			mainDelta, mockDelta, finishDelta, time.Since(start))
	})

	t.Run("expired_before_mock_entry_skips_pay_and_save", func(t *testing.T) {
		base := cancelBoundsUnknownOrder()
		deps := cancelBoundsSubmitDepsFor(base)
		outerCtx, cancelOuter := context.WithTimeout(context.Background(), 100*time.Millisecond)
		defer cancelOuter()
		outerDeadline, _ := outerCtx.Deadline()

		registerStarted := make(chan struct{})
		deps.registrar.fn = func(ctx context.Context, input RegisterOrderInput) (RegisterOrderResult, error) {
			close(registerStarted)
			<-ctx.Done()
			return RegisterOrderResult{Order: base, Outcome: RegisterOutcomeCreated}, nil
		}

		done := cancelBoundsStartSubmit(deps.submitter(context.Background()), outerCtx, cancelBoundsRequestID, cancelBoundsSubmitInput())
		cancelBoundsWaitClosed(t, registerStarted, "registration start")
		got := cancelBoundsAwait(t, done, "submit before Mock")

		if !errors.Is(got.err, context.DeadlineExceeded) {
			t.Fatalf("expected the earlier outer deadline, got %v", got.err)
		}
		if !got.result.Created || got.result.Order.Result != ResultUnknown {
			t.Fatalf("expected the created unknown order, got created=%t result=%q", got.result.Created, got.result.Order.Result)
		}
		if deps.payer.calls != 0 || deps.recorder.calls != 0 {
			t.Fatalf("expired work must not enter Mock or save")
		}
		if deps.lookup.byIDCalls != 0 || deps.lookup.byBusinessCalls != 0 {
			t.Fatalf("expired work must not read back")
		}
		if !deps.registrar.deadline.Equal(outerDeadline) {
			t.Fatalf("main work deadline %s did not inherit the earlier outer deadline %s", deps.registrar.deadline, outerDeadline)
		}
		t.Logf("cancel-bounds pre-Mock deadline: outer_remaining=%s", deps.registrar.deadline.Sub(deps.registrar.callAt))
	})

	t.Run("expired_after_mock_entry_uses_one_independent_finish", func(t *testing.T) {
		base := cancelBoundsUnknownOrder()
		deps := cancelBoundsSubmitDepsFor(base)
		outerCtx, cancelOuter := context.WithTimeout(context.Background(), 100*time.Millisecond)
		defer cancelOuter()
		outerDeadline, _ := outerCtx.Deadline()

		payStarted := make(chan struct{})
		deps.payer.fn = func(ctx context.Context, request MockPaymentRequest) (MockTransaction, error) {
			close(payStarted)
			<-ctx.Done()
			return MockTransaction{}, ctx.Err()
		}

		done := cancelBoundsStartSubmit(deps.submitter(context.Background()), outerCtx, cancelBoundsRequestID, cancelBoundsSubmitInput())
		cancelBoundsWaitClosed(t, payStarted, "Mock start")
		got := cancelBoundsAwait(t, done, "submit after Mock entry")

		if got.err != nil {
			t.Fatalf("fixed unconfirmed answer must be saved, got error %v", got.err)
		}
		if got.result.Order.Result != ResultUnknown {
			t.Fatalf("expected unknown result, got %q", got.result.Order.Result)
		}
		cancelBoundsAssertIssueCodes(t, got.result.Issues, IssueCodeMockResultUnconfirmed)
		if deps.payer.calls != 1 || deps.recorder.calls != 1 {
			t.Fatalf("expected one Mock call and one save, got %d/%d", deps.payer.calls, deps.recorder.calls)
		}
		if deps.lookup.byIDCalls != 0 || deps.lookup.byBusinessCalls != 0 {
			t.Fatalf("fixed unconfirmed save must not read back")
		}
		if deps.payer.ctxErrAtEntry != nil {
			t.Fatalf("Mock must be entered before the outer deadline")
		}
		if !deps.payer.deadline.Equal(outerDeadline) {
			t.Fatalf("Mock deadline %s did not inherit the earlier outer deadline %s", deps.payer.deadline, outerDeadline)
		}
		if deps.recorder.ctxErrAtEntry != nil {
			t.Fatalf("finish must not inherit the expired work context")
		}
		finishDelta := cancelBoundsAssertDeadline(t, deps.recorder.deadline, deps.recorder.callAt, cancelBoundsFinishBudget, "finish")
		t.Logf("cancel-bounds post-Mock deadline: mock_remaining=%s finish_delta=%s",
			deps.payer.deadline.Sub(deps.payer.callAt), finishDelta)
	})

	t.Run("client_cancel_before_mock_entry_skips_pay_and_save", func(t *testing.T) {
		base := cancelBoundsUnknownOrder()
		deps := cancelBoundsSubmitDepsFor(base)
		clientCtx, cancelClient := context.WithCancel(context.Background())
		defer cancelClient()

		registerStarted := make(chan struct{})
		deps.registrar.fn = func(ctx context.Context, input RegisterOrderInput) (RegisterOrderResult, error) {
			close(registerStarted)
			<-ctx.Done()
			return RegisterOrderResult{Order: base, Outcome: RegisterOutcomeCreated}, nil
		}

		done := cancelBoundsStartSubmit(deps.submitter(context.Background()), clientCtx, cancelBoundsRequestID, cancelBoundsSubmitInput())
		cancelBoundsWaitClosed(t, registerStarted, "registration start")
		cancelClient()
		got := cancelBoundsAwait(t, done, "client cancel before Mock")

		if !errors.Is(got.err, context.Canceled) {
			t.Fatalf("expected client cancellation, got %v", got.err)
		}
		if !got.result.Created || got.result.Order.Result != ResultUnknown {
			t.Fatalf("expected the created unknown order, got created=%t result=%q", got.result.Created, got.result.Order.Result)
		}
		if deps.payer.calls != 0 || deps.recorder.calls != 0 {
			t.Fatalf("client cancellation before Mock must not call Mock or save")
		}
		if deps.lookup.byIDCalls != 0 || deps.lookup.byBusinessCalls != 0 {
			t.Fatalf("client cancellation before Mock must not read back")
		}
	})

	t.Run("client_cancel_during_mock_keeps_finish_independent_and_saves_once", func(t *testing.T) {
		base := cancelBoundsUnknownOrder()
		deps := cancelBoundsSubmitDepsFor(base)
		clientCtx, cancelClient := context.WithCancel(context.Background())
		defer cancelClient()

		payStarted := make(chan struct{})
		deps.payer.fn = func(ctx context.Context, request MockPaymentRequest) (MockTransaction, error) {
			close(payStarted)
			<-ctx.Done()
			return MockTransaction{}, ctx.Err()
		}

		done := cancelBoundsStartSubmit(deps.submitter(context.Background()), clientCtx, cancelBoundsRequestID, cancelBoundsSubmitInput())
		cancelBoundsWaitClosed(t, payStarted, "Mock start")
		cancelClient()
		got := cancelBoundsAwait(t, done, "client cancel during Mock")

		if got.err != nil {
			t.Fatalf("fixed unconfirmed answer must be saved, got error %v", got.err)
		}
		if got.result.Order.Result != ResultUnknown {
			t.Fatalf("expected unknown result, got %q", got.result.Order.Result)
		}
		cancelBoundsAssertIssueCodes(t, got.result.Issues, IssueCodeMockResultUnconfirmed)
		if deps.payer.calls != 1 || deps.recorder.calls != 1 {
			t.Fatalf("expected exactly one Mock call and one save, got %d/%d", deps.payer.calls, deps.recorder.calls)
		}
		if deps.lookup.byIDCalls != 0 || deps.lookup.byBusinessCalls != 0 {
			t.Fatalf("client cancellation must not add a readback or another query")
		}
		if deps.recorder.ctxErrAtEntry != nil {
			t.Fatalf("finish save must not inherit client cancellation")
		}
		finishDelta := cancelBoundsAssertDeadline(t, deps.recorder.deadline, deps.recorder.callAt, cancelBoundsFinishBudget, "finish")
		t.Logf("cancel-bounds client-cancel-during-Mock: finish_delta=%s", finishDelta)
	})

	t.Run("late_mock_answer_after_client_cancel_saves_once_without_new_pay", func(t *testing.T) {
		base := cancelBoundsUnknownOrder()
		deps := cancelBoundsSubmitDepsFor(base)
		clientCtx, cancelClient := context.WithCancel(context.Background())
		defer cancelClient()

		deps.payer.fn = func(ctx context.Context, request MockPaymentRequest) (MockTransaction, error) {
			cancelClient()
			<-ctx.Done()
			return cancelBoundsTransaction(base, ResultSucceeded), nil
		}

		got, err := deps.submitter(context.Background()).Submit(clientCtx, cancelBoundsRequestID, cancelBoundsSubmitInput())
		if err != nil {
			t.Fatalf("late fixed answer must be saved, got error %v", err)
		}
		if got.Order.Result != ResultSucceeded {
			t.Fatalf("expected the late answer to be saved, got %q", got.Order.Result)
		}
		cancelBoundsAssertIssueCodes(t, got.Issues)
		if deps.payer.calls != 1 || deps.recorder.calls != 1 {
			t.Fatalf("expected exactly one Pay and one save, got %d/%d", deps.payer.calls, deps.recorder.calls)
		}
		if deps.lookup.byIDCalls != 0 || deps.lookup.byBusinessCalls != 0 {
			t.Fatalf("late answer must not add a readback or another query")
		}
		if deps.recorder.ctxErrAtEntry != nil {
			t.Fatalf("finish save must not inherit client cancellation")
		}
		finishDelta := cancelBoundsAssertDeadline(t, deps.recorder.deadline, deps.recorder.callAt, cancelBoundsFinishBudget, "finish")
		t.Logf("cancel-bounds late-answer: finish_delta=%s", finishDelta)
	})

	t.Run("finish_window_is_one_absolute_two_second_budget_for_save_and_readback", func(t *testing.T) {
		base := cancelBoundsUnknownOrder()
		deps := cancelBoundsSubmitDepsFor(base)

		deps.recorder.fn = func(ctx context.Context, observation Observation) (SaveObservationResult, error) {
			<-ctx.Done()
			return SaveObservationResult{}, ctx.Err()
		}
		deps.lookup.byIDFn = func(ctx context.Context, clientID string, paymentOrderID PaymentOrderID) (PaymentOrder, bool, error) {
			return base, true, nil
		}

		start := time.Now()
		got, err := deps.submitter(context.Background()).Submit(context.Background(), cancelBoundsRequestID, cancelBoundsSubmitInput())
		elapsed := time.Since(start)

		if err != nil {
			t.Fatalf("readback must return the current reliable fact, got error %v", err)
		}
		if got.Order.Result != ResultUnknown {
			t.Fatalf("expected unknown result, got %q", got.Order.Result)
		}
		cancelBoundsAssertIssueCodes(t, got.Issues, IssueCodeResultSaveError)
		if deps.payer.calls != 1 || deps.recorder.calls != 1 || deps.lookup.byIDCalls != 1 {
			t.Fatalf("expected one Pay, one save, and one readback; got %d/%d/%d",
				deps.payer.calls, deps.recorder.calls, deps.lookup.byIDCalls)
		}
		if deps.lookup.byBusinessCalls != 0 {
			t.Fatalf("readback must not use another lookup path")
		}
		if deps.recorder.ctxErrAtEntry != nil {
			t.Fatalf("finish save must start with a live finish context")
		}
		saveDelta := cancelBoundsAssertDeadline(t, deps.recorder.deadline, deps.recorder.callAt, cancelBoundsFinishBudget, "save")
		cancelBoundsAssertSameDeadline(t, deps.recorder.deadline, deps.lookup.byIDDeadline, "save and readback")
		if !errors.Is(deps.lookup.byIDCtxErr, context.DeadlineExceeded) {
			t.Fatalf("readback must use the already expired shared deadline, got %v", deps.lookup.byIDCtxErr)
		}
		if readbackAfter := deps.lookup.byIDCallAt.Sub(start); readbackAfter < cancelBoundsFinishBudget-cancelBoundsDeadlineSlack {
			t.Fatalf("readback started too early: %s", readbackAfter)
		}
		cancelBoundsAssertElapsed(t, elapsed, cancelBoundsFinishBudget, "finish")
		t.Logf("cancel-bounds finish: save_delta=%s readback_after=%s elapsed=%s",
			saveDelta, deps.lookup.byIDCallAt.Sub(start), elapsed)
	})

	t.Run("mock_timeout_uses_full_default_three_second_window_and_finish_still_saves", func(t *testing.T) {
		base := cancelBoundsUnknownOrder()
		deps := cancelBoundsSubmitDepsFor(base)

		deps.payer.fn = func(ctx context.Context, request MockPaymentRequest) (MockTransaction, error) {
			<-ctx.Done()
			return MockTransaction{}, ctx.Err()
		}

		start := time.Now()
		got, err := deps.submitter(context.Background()).Submit(context.Background(), cancelBoundsRequestID, cancelBoundsSubmitInput())
		elapsed := time.Since(start)

		if err != nil {
			t.Fatalf("fixed unconfirmed answer must be saved, got error %v", err)
		}
		if got.Order.Result != ResultUnknown {
			t.Fatalf("expected unknown result, got %q", got.Order.Result)
		}
		cancelBoundsAssertIssueCodes(t, got.Issues, IssueCodeMockResultUnconfirmed)
		if deps.payer.calls != 1 || deps.recorder.calls != 1 {
			t.Fatalf("expected one Mock call and one save, got %d/%d", deps.payer.calls, deps.recorder.calls)
		}
		if deps.lookup.byIDCalls != 0 || deps.lookup.byBusinessCalls != 0 {
			t.Fatalf("Mock timeout must not add a readback or another query")
		}
		if deps.payer.ctxErrAtEntry != nil {
			t.Fatalf("Mock must start with a live deadline")
		}
		mockDelta := cancelBoundsAssertDeadline(t, deps.payer.deadline, deps.payer.callAt, cancelBoundsMockBudget, "Mock")
		finishDelta := cancelBoundsAssertDeadline(t, deps.recorder.deadline, deps.recorder.callAt, cancelBoundsFinishBudget, "finish")
		if deps.recorder.ctxErrAtEntry != nil {
			t.Fatalf("finish save must start with a live finish context")
		}
		cancelBoundsAssertElapsed(t, elapsed, cancelBoundsMockBudget, "Mock timeout")
		t.Logf("cancel-bounds mock-timeout: mock_delta=%s finish_delta=%s elapsed=%s", mockDelta, finishDelta, elapsed)
	})

	t.Run("query_cancel_before_mock_verification_skips_query_save_and_payment", func(t *testing.T) {
		base := cancelBoundsUnknownOrder()
		deps := cancelBoundsQueryDepsFor(base)
		clientCtx, cancelClient := context.WithCancel(context.Background())
		defer cancelClient()

		lookupStarted := make(chan struct{})
		deps.lookup.byIDFn = func(ctx context.Context, clientID string, paymentOrderID PaymentOrderID) (PaymentOrder, bool, error) {
			close(lookupStarted)
			<-ctx.Done()
			return base, true, nil
		}

		paySpy := &cancelBoundsPayer{}
		done := cancelBoundsStartQuery(deps.service(context.Background()), clientCtx, cancelBoundsRequestID, cancelBoundsQueryInput())
		cancelBoundsWaitClosed(t, lookupStarted, "lookup start")
		cancelClient()
		got := cancelBoundsAwait(t, done, "query cancel before Mock")

		if !errors.Is(got.err, context.Canceled) {
			t.Fatalf("expected client cancellation, got %v", got.err)
		}
		if got.result.Order.ID != "" || len(got.result.Issues) != 0 {
			t.Fatalf("canceled lookup must not produce a result")
		}
		if deps.mock.calls != 0 || deps.recorder.calls != 0 {
			t.Fatalf("query cancellation before Mock must not verify or save")
		}
		if paySpy.calls != 0 {
			t.Fatalf("query cancellation must not initiate a payment")
		}
	})

	t.Run("query_late_answer_after_client_cancel_saves_once_without_new_query_or_payment", func(t *testing.T) {
		base := cancelBoundsUnknownOrder()
		deps := cancelBoundsQueryDepsFor(base)
		clientCtx, cancelClient := context.WithCancel(context.Background())
		defer cancelClient()

		mockStarted := make(chan struct{})
		deps.mock.fn = func(ctx context.Context, platformReferenceID PaymentOrderID) (MockTransaction, bool, error) {
			close(mockStarted)
			cancelClient()
			<-ctx.Done()
			return cancelBoundsTransaction(base, ResultSucceeded), true, nil
		}

		paySpy := &cancelBoundsPayer{}
		done := cancelBoundsStartQuery(deps.service(context.Background()), clientCtx, cancelBoundsRequestID, cancelBoundsQueryInput())
		cancelBoundsWaitClosed(t, mockStarted, "Mock query start")
		got := cancelBoundsAwait(t, done, "query late answer")

		if got.err != nil {
			t.Fatalf("late fixed answer must be saved, got error %v", got.err)
		}
		if got.result.Order.Result != ResultSucceeded {
			t.Fatalf("expected the late answer to be saved, got %q", got.result.Order.Result)
		}
		cancelBoundsAssertIssueCodes(t, got.result.Issues)
		if deps.mock.calls != 1 || deps.recorder.calls != 1 || deps.lookup.byIDCalls != 1 {
			t.Fatalf("expected one lookup, one Mock query, and one save; got %d/%d/%d",
				deps.lookup.byIDCalls, deps.mock.calls, deps.recorder.calls)
		}
		if deps.lookup.byBusinessCalls != 0 {
			t.Fatalf("late answer must not add a readback or another lookup path")
		}
		if deps.recorder.ctxErrAtEntry != nil {
			t.Fatalf("finish save must not inherit client cancellation")
		}
		if paySpy.calls != 0 {
			t.Fatalf("query cancellation must not initiate a payment")
		}
		finishDelta := cancelBoundsAssertDeadline(t, deps.recorder.deadline, deps.recorder.callAt, cancelBoundsFinishBudget, "finish")
		t.Logf("cancel-bounds query-late-answer: finish_delta=%s", finishDelta)
	})

	t.Run("lifecycle_cancel_constrains_finish_for_submit_and_query", func(t *testing.T) {
		t.Run("submit", func(t *testing.T) {
			base := cancelBoundsUnknownOrder()
			deps := cancelBoundsSubmitDepsFor(base)
			lifecycle, cancelLifecycle := context.WithCancel(context.Background())
			defer cancelLifecycle()

			deps.payer.fn = func(ctx context.Context, request MockPaymentRequest) (MockTransaction, error) {
				cancelLifecycle()
				return cancelBoundsTransaction(base, ResultSucceeded), nil
			}
			var saveCtxErr error
			deps.recorder.fn = func(ctx context.Context, observation Observation) (SaveObservationResult, error) {
				saveCtxErr = ctx.Err()
				return SaveObservationResult{}, ctx.Err()
			}

			got, err := deps.submitter(lifecycle).Submit(context.Background(), cancelBoundsRequestID, cancelBoundsSubmitInput())
			if !errors.Is(err, ErrUnavailable) {
				t.Fatalf("expected lifecycle unavailability, got %v", err)
			}
			if got.Order.Result == ResultSucceeded {
				t.Fatalf("lifecycle cancellation must not return an uncommitted confirmation")
			}
			if deps.payer.calls != 1 || deps.recorder.calls != 1 {
				t.Fatalf("expected one Mock call and one save, got %d/%d", deps.payer.calls, deps.recorder.calls)
			}
			if deps.lookup.byIDCalls != 1 || deps.lookup.byBusinessCalls != 0 {
				t.Fatalf("expected one bounded readback attempt, got %d/%d", deps.lookup.byIDCalls, deps.lookup.byBusinessCalls)
			}
			if !errors.Is(deps.lookup.byIDCtxErr, context.Canceled) {
				t.Fatalf("lifecycle cancellation must cancel the readback context, got %v", deps.lookup.byIDCtxErr)
			}
			if saveCtxErr == nil {
				t.Fatalf("finish save context must be canceled by the lifecycle")
			}
		})

		t.Run("query", func(t *testing.T) {
			base := cancelBoundsUnknownOrder()
			deps := cancelBoundsQueryDepsFor(base)
			lifecycle, cancelLifecycle := context.WithCancel(context.Background())
			defer cancelLifecycle()

			deps.mock.fn = func(ctx context.Context, platformReferenceID PaymentOrderID) (MockTransaction, bool, error) {
				cancelLifecycle()
				return cancelBoundsTransaction(base, ResultSucceeded), true, nil
			}
			var saveCtxErr error
			deps.recorder.fn = func(ctx context.Context, observation Observation) (SaveObservationResult, error) {
				saveCtxErr = ctx.Err()
				return SaveObservationResult{}, ctx.Err()
			}

			got, err := deps.service(lifecycle).Query(context.Background(), cancelBoundsRequestID, cancelBoundsQueryInput())
			if !errors.Is(err, ErrUnavailable) {
				t.Fatalf("expected lifecycle unavailability, got %v", err)
			}
			if got.Order.Result == ResultSucceeded {
				t.Fatalf("lifecycle cancellation must not return an uncommitted confirmation")
			}
			if deps.mock.calls != 1 || deps.recorder.calls != 1 {
				t.Fatalf("expected one Mock query and one save, got %d/%d", deps.mock.calls, deps.recorder.calls)
			}
			if deps.lookup.byIDCalls != 1 {
				t.Fatalf("lifecycle cancellation must not start a readback; got %d initial lookups", deps.lookup.byIDCalls)
			}
			if saveCtxErr == nil {
				t.Fatalf("finish save context must be canceled by the lifecycle")
			}
		})
	})
}
