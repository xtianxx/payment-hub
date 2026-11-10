package payment

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"
)

const (
	submitTestOrderID       = PaymentOrderID("3f4a5b6c-7d8e-4f9a-8b0c-1d2e3f4a5b6c")
	submitTestOtherOrderID  = PaymentOrderID("4a5b6c7d-8e9f-4a0b-9c1d-2e3f4a5b6c7d")
	submitTestMockTxID      = MockTransactionID("5b6c7d8e-9f0a-4b1c-8d2e-3f4a5b6c7d8e")
	submitTestOtherMockID   = MockTransactionID("6c7d8e9f-0a1b-4c2d-9e3f-4a5b6c7d8e9f")
	submitTestRequestID     = RequestID("7d8e9f0a-1b2c-4d3e-8f4a-5b6c7d8e9f0a")
	submitTestObservationID = ResultHandlingRecordID("9a0b1c2d-3e4f-4a5b-8c6d-7e8f9a0b1c2d")
)

func submitTestTime() time.Time {
	return time.Date(2026, 11, 7, 10, 0, 0, 0, time.UTC)
}

func submitTestOldTime() time.Time {
	return time.Date(2026, 11, 7, 9, 0, 0, 0, time.UTC)
}

func submitTestInput() RegisterOrderInput {
	return RegisterOrderInput{
		ClientID:        "client_1",
		BusinessOrderNo: "M100",
		Amount:          100,
		Currency:        "CNY",
	}
}

func submitTestOrder(result Result, amount int64) PaymentOrder {
	order := PaymentOrder{
		ID:              submitTestOrderID,
		ClientID:        "client_1",
		BusinessOrderNo: "M100",
		Amount:          amount,
		Currency:        "CNY",
		Result:          result,
		RegisteredAt:    submitTestTime(),
	}
	if result != ResultUnknown {
		at := submitTestOldTime()
		tx := submitTestMockTxID
		order.ConfirmedAt = &at
		order.MockTransactionID = &tx
	}
	return order
}

func submitTestTransaction(order PaymentOrder, result Result) MockTransaction {
	return MockTransaction{
		ID:                  submitTestMockTxID,
		PlatformReferenceID: order.ID,
		Amount:              order.Amount,
		Currency:            order.Currency,
		Result:              result,
		ProcessedAt:         submitTestTime(),
	}
}

type submitTestRegistrar struct {
	calls  int
	input  RegisterOrderInput
	result RegisterOrderResult
	err    error
	onCall func(context.Context)
}

func (f *submitTestRegistrar) RegisterOrder(ctx context.Context, input RegisterOrderInput) (RegisterOrderResult, error) {
	f.calls++
	f.input = input
	if f.onCall != nil {
		f.onCall(ctx)
	}
	return f.result, f.err
}

type submitTestPayer struct {
	calls       int
	request     MockPaymentRequest
	transaction MockTransaction
	err         error
	pay         func(context.Context, MockPaymentRequest) (MockTransaction, error)
}

func (f *submitTestPayer) Pay(ctx context.Context, request MockPaymentRequest) (MockTransaction, error) {
	f.calls++
	f.request = request
	if f.pay != nil {
		return f.pay(ctx, request)
	}
	return f.transaction, f.err
}

type submitTestRecorder struct {
	calls       int
	observation Observation
	result      SaveObservationResult
	err         error
	deadline    time.Time
	ctxErr      error
	save        func(context.Context, Observation) (SaveObservationResult, error)
}

func (f *submitTestRecorder) SaveObservation(ctx context.Context, observation Observation) (SaveObservationResult, error) {
	f.calls++
	f.observation = observation
	f.deadline, _ = ctx.Deadline()
	f.ctxErr = ctx.Err()
	if f.save != nil {
		return f.save(ctx, observation)
	}
	return f.result, f.err
}

type submitTestLookup struct {
	byIDCalls          int
	byBusinessCalls    int
	byIDOrder          PaymentOrder
	byIDFound          bool
	byIDErr            error
	byBusinessOrder    PaymentOrder
	byBusinessFound    bool
	byBusinessErr      error
	byIDDeadline       time.Time
	byBusinessDeadline time.Time
	byIDFn             func(context.Context, string, PaymentOrderID) (PaymentOrder, bool, error)
	byBusinessFn       func(context.Context, string, string) (PaymentOrder, bool, error)
}

func (f *submitTestLookup) FindOrderByID(ctx context.Context, clientID string, paymentOrderID PaymentOrderID) (PaymentOrder, bool, error) {
	f.byIDCalls++
	f.byIDDeadline, _ = ctx.Deadline()
	if f.byIDFn != nil {
		return f.byIDFn(ctx, clientID, paymentOrderID)
	}
	return f.byIDOrder, f.byIDFound, f.byIDErr
}

func (f *submitTestLookup) FindOrderByBusinessOrderNo(ctx context.Context, clientID string, businessOrderNo string) (PaymentOrder, bool, error) {
	f.byBusinessCalls++
	f.byBusinessDeadline, _ = ctx.Deadline()
	if f.byBusinessFn != nil {
		return f.byBusinessFn(ctx, clientID, businessOrderNo)
	}
	return f.byBusinessOrder, f.byBusinessFound, f.byBusinessErr
}

func submitTestDecideRecorder(base PaymentOrder) func(context.Context, Observation) (SaveObservationResult, error) {
	return func(ctx context.Context, observation Observation) (SaveObservationResult, error) {
		updated, record, err := DecideObservation(base, observation, submitTestTime())
		if err != nil {
			return SaveObservationResult{}, err
		}
		return SaveObservationResult{Order: updated, Record: record}, nil
	}
}

func submitTestObservation(order PaymentOrder) Observation {
	return Observation{
		ID:             submitTestObservationID,
		PaymentOrderID: order.ID,
		ClientID:       order.ClientID,
		RequestID:      submitTestRequestID,
		Source:         SourcePayment,
		ObservedAt:     submitTestTime(),
	}
}

func submitTestSavedWithTransition(
	current PaymentOrder,
	observation Observation,
	decision HandlingDecision,
	before Result,
	after Result,
) SaveObservationResult {
	return SaveObservationResult{
		Order: current,
		Record: ResultHandlingRecord{
			ID:             observation.ID,
			PaymentOrderID: observation.PaymentOrderID,
			RequestID:      observation.RequestID,
			Source:         observation.Source,
			ObservedAt:     observation.ObservedAt,
			ResultBefore:   before,
			ResultAfter:    after,
			Decision:       decision,
		},
	}
}

type submitTestDeps struct {
	reg      *submitTestRegistrar
	lookup   *submitTestLookup
	recorder *submitTestRecorder
	payer    *submitTestPayer
}

func submitTestValidDeps(order PaymentOrder) *submitTestDeps {
	return &submitTestDeps{
		reg: &submitTestRegistrar{
			result: RegisterOrderResult{Order: order, Outcome: RegisterOutcomeCreated},
		},
		lookup: &submitTestLookup{},
		recorder: &submitTestRecorder{
			save: submitTestDecideRecorder(order),
		},
		payer: &submitTestPayer{
			transaction: submitTestTransaction(order, ResultSucceeded),
		},
	}
}

func (d *submitTestDeps) submitter(lifecycle context.Context) *Submitter {
	return NewSubmitter(d.reg, d.lookup, d.recorder, d.payer, lifecycle)
}

func (d *submitTestDeps) assertNoCalls(t *testing.T) {
	t.Helper()
	if d.reg.calls != 0 || d.payer.calls != 0 || d.recorder.calls != 0 ||
		d.lookup.byIDCalls != 0 || d.lookup.byBusinessCalls != 0 {
		t.Fatalf("expected no dependency calls")
	}
}

func submitTestAssertIssues(t *testing.T, got []Issue, want ...string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("expected %d issues, got %d", len(want), len(got))
	}
	for i := range want {
		if got[i].Code != want[i] {
			t.Fatalf("unexpected issue code at %d", i)
		}
		if got[i].Message == "" {
			t.Fatalf("issue message must not be empty")
		}
	}
}

func submitTestAssertNoIssues(t *testing.T, got []Issue) {
	t.Helper()
	if len(got) != 0 {
		t.Fatalf("expected no issues")
	}
}

type submitTestAsyncResult struct {
	result SubmitResult
	err    error
}

func submitTestStart(s *Submitter, ctx context.Context, requestID RequestID, input RegisterOrderInput) <-chan submitTestAsyncResult {
	done := make(chan submitTestAsyncResult, 1)
	go func() {
		result, err := s.Submit(ctx, requestID, input)
		done <- submitTestAsyncResult{result: result, err: err}
	}()
	return done
}

func submitTestWaitClosed(t *testing.T, ch <-chan struct{}, what string) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(2 * time.Second):
		t.Fatalf("%s context was not canceled by lifecycle", what)
	}
}

func submitTestAwait(t *testing.T, done <-chan submitTestAsyncResult) submitTestAsyncResult {
	t.Helper()
	select {
	case result := <-done:
		return result
	case <-time.After(2 * time.Second):
		t.Fatalf("operation did not finish after lifecycle cancellation")
		return submitTestAsyncResult{}
	}
}

func TestSubmitPayment(t *testing.T) {
	t.Run("invalid_inputs_have_no_effect", func(t *testing.T) {
		valid := submitTestInput()
		cases := []struct {
			name               string
			mutate             func(*RegisterOrderInput)
			requestID          RequestID
			ctx                context.Context
			lifecycle          context.Context
			want               error
			wantBusinessLookup int
		}{
			{
				name:      "invalid_client_id",
				mutate:    func(in *RegisterOrderInput) { in.ClientID = "" },
				requestID: submitTestRequestID,
				ctx:       context.Background(),
				lifecycle: context.Background(),
				want:      ErrInvalidInput,
			},
			{
				name:      "invalid_business_order_no",
				mutate:    func(in *RegisterOrderInput) { in.BusinessOrderNo = "bad order" },
				requestID: submitTestRequestID,
				ctx:       context.Background(),
				lifecycle: context.Background(),
				want:      ErrInvalidInput,
			},
			{
				name:               "amount_zero",
				mutate:             func(in *RegisterOrderInput) { in.Amount = 0 },
				requestID:          submitTestRequestID,
				ctx:                context.Background(),
				lifecycle:          context.Background(),
				want:               ErrInvalidInput,
				wantBusinessLookup: 1,
			},
			{
				name:               "amount_negative",
				mutate:             func(in *RegisterOrderInput) { in.Amount = -1 },
				requestID:          submitTestRequestID,
				ctx:                context.Background(),
				lifecycle:          context.Background(),
				want:               ErrInvalidInput,
				wantBusinessLookup: 1,
			},
			{
				name:               "amount_above_maximum",
				mutate:             func(in *RegisterOrderInput) { in.Amount = 1_000_001 },
				requestID:          submitTestRequestID,
				ctx:                context.Background(),
				lifecycle:          context.Background(),
				want:               ErrInvalidInput,
				wantBusinessLookup: 1,
			},
			{
				name:               "currency_invalid",
				mutate:             func(in *RegisterOrderInput) { in.Currency = "USD" },
				requestID:          submitTestRequestID,
				ctx:                context.Background(),
				lifecycle:          context.Background(),
				want:               ErrInvalidInput,
				wantBusinessLookup: 1,
			},
			{
				name:      "invalid_request_id",
				requestID: RequestID("not-a-uuid"),
				ctx:       context.Background(),
				lifecycle: context.Background(),
				want:      ErrInvalidInput,
			},
			{
				name:      "nil_context",
				requestID: submitTestRequestID,
				ctx:       nil,
				lifecycle: context.Background(),
				want:      ErrInvalidInput,
			},
			{
				name:      "nil_lifecycle",
				requestID: submitTestRequestID,
				ctx:       context.Background(),
				lifecycle: nil,
				want:      ErrUnavailable,
			},
		}

		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				order := submitTestOrder(ResultUnknown, valid.Amount)
				deps := submitTestValidDeps(order)
				input := valid
				if tc.mutate != nil {
					tc.mutate(&input)
				}
				got, err := deps.submitter(tc.lifecycle).Submit(tc.ctx, tc.requestID, input)
				if !errors.Is(err, tc.want) {
					t.Fatalf("expected error %v", tc.want)
				}
				if got.Order.ID != "" || got.Created || len(got.Issues) != 0 {
					t.Fatalf("invalid input must not produce a result")
				}
				if deps.reg.calls != 0 || deps.payer.calls != 0 || deps.recorder.calls != 0 || deps.lookup.byIDCalls != 0 {
					t.Fatalf("invalid input must not create a side effect")
				}
				if deps.lookup.byBusinessCalls != tc.wantBusinessLookup {
					t.Fatalf("business lookup calls = %d, want %d", deps.lookup.byBusinessCalls, tc.wantBusinessLookup)
				}
				if tc.wantBusinessLookup > 0 && deps.lookup.byBusinessDeadline.IsZero() {
					t.Fatalf("business-value validation lookup must use a bounded context")
				}
			})
		}

		t.Run("canceled_context", func(t *testing.T) {
			order := submitTestOrder(ResultUnknown, valid.Amount)
			deps := submitTestValidDeps(order)
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			got, err := deps.submitter(context.Background()).Submit(ctx, submitTestRequestID, valid)
			if !errors.Is(err, context.Canceled) {
				t.Fatalf("expected canceled context")
			}
			if got.Order.ID != "" || got.Created || len(got.Issues) != 0 {
				t.Fatalf("canceled context must not produce a result")
			}
			deps.assertNoCalls(t)
		})

		t.Run("canceled_lifecycle", func(t *testing.T) {
			order := submitTestOrder(ResultUnknown, valid.Amount)
			deps := submitTestValidDeps(order)
			lifecycle, cancel := context.WithCancel(context.Background())
			cancel()
			got, err := deps.submitter(lifecycle).Submit(context.Background(), submitTestRequestID, valid)
			if !errors.Is(err, ErrUnavailable) {
				t.Fatalf("expected unavailable lifecycle")
			}
			if got.Order.ID != "" || got.Created || len(got.Issues) != 0 {
				t.Fatalf("canceled lifecycle must not produce a result")
			}
			deps.assertNoCalls(t)
		})
	})

	t.Run("created_calls_mock_once_and_saves_confirmation", func(t *testing.T) {
		cases := []struct {
			name   string
			amount int64
			result Result
		}{
			{name: "succeeded", amount: 100, result: ResultSucceeded},
			{name: "failed", amount: 100, result: ResultFailed},
			{name: "minimum_amount", amount: 1, result: ResultSucceeded},
			{name: "maximum_amount", amount: 1_000_000, result: ResultSucceeded},
		}

		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				input := submitTestInput()
				input.Amount = tc.amount
				order := submitTestOrder(ResultUnknown, tc.amount)
				deps := submitTestValidDeps(order)
				deps.payer.transaction = submitTestTransaction(order, tc.result)
				var saved SaveObservationResult
				deps.recorder.save = func(ctx context.Context, observation Observation) (SaveObservationResult, error) {
					result, err := submitTestDecideRecorder(order)(ctx, observation)
					if err != nil {
						return SaveObservationResult{}, err
					}
					saved = result
					return result, nil
				}

				got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, input)
				if err != nil {
					t.Fatalf("expected nil error")
				}
				if !got.Created {
					t.Fatalf("expected created result")
				}
				if got.Order.Result != tc.result {
					t.Fatalf("unexpected order result")
				}
				if got.Order.ConfirmedAt == nil || !got.Order.ConfirmedAt.Equal(submitTestTime()) {
					t.Fatalf("expected confirmed_at")
				}
				if got.Order.MockTransactionID == nil || *got.Order.MockTransactionID != submitTestMockTxID {
					t.Fatalf("expected mock transaction ID")
				}
				submitTestAssertNoIssues(t, got.Issues)

				if deps.reg.calls != 1 || deps.payer.calls != 1 || deps.recorder.calls != 1 {
					t.Fatalf("expected exactly one registration, payment, and save")
				}
				if deps.lookup.byIDCalls != 0 || deps.lookup.byBusinessCalls != 0 {
					t.Fatalf("successful submission must not use lookup")
				}
				if deps.payer.request.PlatformReferenceID != order.ID ||
					deps.payer.request.Amount != input.Amount ||
					deps.payer.request.Currency != input.Currency {
					t.Fatalf("mock request did not carry the actual submission values")
				}

				observation := deps.recorder.observation
				if err := ValidateUUIDv4(string(observation.ID)); err != nil {
					t.Fatalf("observation ID is not UUIDv4")
				}
				if observation.PaymentOrderID != order.ID || observation.ClientID != order.ClientID {
					t.Fatalf("observation association mismatch")
				}
				if observation.RequestID != submitTestRequestID || observation.Source != SourcePayment {
					t.Fatalf("observation request/source mismatch")
				}
				if err := ValidateUTC(observation.ObservedAt); err != nil {
					t.Fatalf("observation time is not UTC")
				}
				if observation.Candidate == nil {
					t.Fatalf("expected candidate observation")
				}
				if observation.Candidate.Result != tc.result {
					t.Fatalf("candidate result mismatch")
				}
				if observation.Candidate.MockTransactionID == nil || *observation.Candidate.MockTransactionID != submitTestMockTxID {
					t.Fatalf("candidate mock transaction ID mismatch")
				}
				if observation.EvidenceRejected || observation.Reason != nil {
					t.Fatalf("expected accepted evidence without reason")
				}
				if observation.Evidence.Result == nil || *observation.Evidence.Result != tc.result {
					t.Fatalf("observation evidence result mismatch")
				}
				if observation.Evidence.Amount == nil || *observation.Evidence.Amount != input.Amount {
					t.Fatalf("observation evidence amount mismatch")
				}
				if observation.Evidence.Currency == nil || *observation.Evidence.Currency != input.Currency {
					t.Fatalf("observation evidence currency mismatch")
				}

				oldSavedConfirmed := *saved.Order.ConfirmedAt
				*got.Order.ConfirmedAt = submitTestOldTime()
				if saved.Order.ConfirmedAt == nil || !saved.Order.ConfirmedAt.Equal(oldSavedConfirmed) {
					t.Fatalf("returned order shares confirmed_at pointer with saved result")
				}
			})
		}
	})

	t.Run("reused_orders_never_call_mock_or_recorder", func(t *testing.T) {
		for _, result := range []Result{ResultUnknown, ResultSucceeded, ResultFailed} {
			result := result
			t.Run(string(result), func(t *testing.T) {
				order := submitTestOrder(result, 100)
				deps := submitTestValidDeps(order)
				deps.reg.result = RegisterOrderResult{Order: order, Outcome: RegisterOutcomeReused}
				got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
				if err != nil {
					t.Fatalf("expected nil error")
				}
				if got.Created {
					t.Fatalf("reused order must not be created")
				}
				if got.Order.Result != result {
					t.Fatalf("reused order result mismatch")
				}
				submitTestAssertNoIssues(t, got.Issues)
				if deps.reg.calls != 1 {
					t.Fatalf("expected one registration call")
				}
				if deps.payer.calls != 0 || deps.recorder.calls != 0 ||
					deps.lookup.byIDCalls != 0 || deps.lookup.byBusinessCalls != 0 {
					t.Fatalf("reused order must not call Mock, save, or query")
				}
			})
		}

		t.Run("amount_conflict", func(t *testing.T) {
			order := submitTestOrder(ResultUnknown, 101)
			deps := submitTestValidDeps(order)
			deps.reg.result = RegisterOrderResult{Order: order, Outcome: RegisterOutcomeReused}
			got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
			if !errors.Is(err, ErrConflict) {
				t.Fatalf("expected conflict")
			}
			if got.Created {
				t.Fatalf("conflict must not be created")
			}
			if deps.payer.calls != 0 || deps.recorder.calls != 0 ||
				deps.lookup.byIDCalls != 0 || deps.lookup.byBusinessCalls != 0 {
				t.Fatalf("conflict must not call Mock, save, or query")
			}
		})

		t.Run("currency_conflict", func(t *testing.T) {
			order := submitTestOrder(ResultUnknown, 100)
			order.Currency = "USD"
			deps := submitTestValidDeps(order)
			deps.reg.result = RegisterOrderResult{Order: order, Outcome: RegisterOutcomeReused}
			got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
			if !errors.Is(err, ErrConflict) {
				t.Fatalf("expected conflict")
			}
			if got.Created {
				t.Fatalf("conflict must not be created")
			}
			if deps.payer.calls != 0 || deps.recorder.calls != 0 ||
				deps.lookup.byIDCalls != 0 || deps.lookup.byBusinessCalls != 0 {
				t.Fatalf("conflict must not call Mock, save, or query")
			}
		})
	})

	t.Run("registration_error_reads_back_without_created_or_payment", func(t *testing.T) {
		base := submitTestOrder(ResultUnknown, 100)
		confirmed := submitTestOrder(ResultSucceeded, 100)
		cases := []struct {
			name         string
			regErr       error
			lookupOrder  PaymentOrder
			lookupFound  bool
			lookupErr    error
			wantErr      error
			wantResult   Result
			wantIssue    string
			wantReturned bool
		}{
			{
				name:         "readback_unknown",
				regErr:       errors.New("registration commit secret"),
				lookupOrder:  base,
				lookupFound:  true,
				wantResult:   ResultUnknown,
				wantIssue:    IssueCodeResultSaveError,
				wantReturned: true,
			},
			{
				name:         "readback_confirmed",
				regErr:       errors.New("registration commit secret"),
				lookupOrder:  confirmed,
				lookupFound:  true,
				wantResult:   ResultSucceeded,
				wantIssue:    IssueCodeResultSaveError,
				wantReturned: true,
			},
			{
				name:    "readback_not_found",
				regErr:  errors.New("registration commit secret"),
				wantErr: ErrIndeterminate,
			},
			{
				name:    "readback_not_found_unavailable",
				regErr:  ErrUnavailable,
				wantErr: ErrUnavailable,
			},
			{
				name:      "readback_error",
				regErr:    errors.New("registration commit secret"),
				lookupErr: errors.New("lookup secret"),
				wantErr:   ErrIndeterminate,
			},
			{
				name:      "readback_error_unavailable",
				regErr:    ErrUnavailable,
				lookupErr: errors.New("lookup secret"),
				wantErr:   ErrUnavailable,
			},
			{
				name:        "readback_conflict_amount",
				regErr:      errors.New("registration commit secret"),
				lookupOrder: submitTestOrder(ResultUnknown, 101),
				lookupFound: true,
				wantErr:     ErrConflict,
			},
			{
				name:   "readback_conflict_currency",
				regErr: errors.New("registration commit secret"),
				lookupOrder: func() PaymentOrder {
					order := submitTestOrder(ResultUnknown, 100)
					order.Currency = "USD"
					return order
				}(),
				lookupFound: true,
				wantErr:     ErrConflict,
			},
		}

		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				deps := submitTestValidDeps(base)
				deps.reg.result = RegisterOrderResult{}
				deps.reg.err = tc.regErr
				deps.lookup.byBusinessOrder = tc.lookupOrder
				deps.lookup.byBusinessFound = tc.lookupFound
				deps.lookup.byBusinessErr = tc.lookupErr

				got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
				if tc.wantErr != nil {
					if !errors.Is(err, tc.wantErr) {
						t.Fatalf("expected error %v", tc.wantErr)
					}
					if got.Created {
						t.Fatalf("registration error must never be created")
					}
				} else {
					if err != nil {
						t.Fatalf("expected nil error")
					}
					if got.Created {
						t.Fatalf("readback must not be created")
					}
					if !tc.wantReturned {
						t.Fatalf("test case missing return expectation")
					}
					if got.Order.Result != tc.wantResult {
						t.Fatalf("unexpected readback result")
					}
					submitTestAssertIssues(t, got.Issues, tc.wantIssue)
					if strings.Contains(got.Issues[0].Message, "secret") {
						t.Fatalf("issue message leaked a raw error")
					}
				}
				if deps.reg.calls != 1 {
					t.Fatalf("expected one registration call")
				}
				if deps.payer.calls != 0 || deps.recorder.calls != 0 {
					t.Fatalf("registration error must not call Mock or save")
				}
				if deps.lookup.byBusinessCalls != 1 || deps.lookup.byIDCalls != 0 {
					t.Fatalf("expected one business-order readback")
				}
			})
		}
	})

	t.Run("cancel_after_created_before_mock_does_not_pay", func(t *testing.T) {
		base := submitTestOrder(ResultUnknown, 100)
		deps := submitTestValidDeps(base)
		ctx, cancel := context.WithCancel(context.Background())
		deps.reg.onCall = func(context.Context) { cancel() }

		got, err := deps.submitter(context.Background()).Submit(ctx, submitTestRequestID, submitTestInput())
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("expected canceled context")
		}
		if !got.Created || got.Order.Result != ResultUnknown {
			t.Fatalf("expected created unknown order")
		}
		if deps.payer.calls != 0 || deps.recorder.calls != 0 ||
			deps.lookup.byIDCalls != 0 || deps.lookup.byBusinessCalls != 0 {
			t.Fatalf("cancellation before Mock must not call Mock, save, or query")
		}
	})

	t.Run("lifecycle_cancel_after_created_before_mock_does_not_pay", func(t *testing.T) {
		base := submitTestOrder(ResultUnknown, 100)
		deps := submitTestValidDeps(base)
		lifecycle, cancel := context.WithCancel(context.Background())
		deps.reg.onCall = func(context.Context) { cancel() }

		got, err := deps.submitter(lifecycle).Submit(context.Background(), submitTestRequestID, submitTestInput())
		if !errors.Is(err, ErrUnavailable) {
			t.Fatalf("expected unavailable lifecycle")
		}
		if !got.Created || got.Order.Result != ResultUnknown {
			t.Fatalf("expected created unknown order")
		}
		if deps.payer.calls != 0 || deps.recorder.calls != 0 ||
			deps.lookup.byIDCalls != 0 || deps.lookup.byBusinessCalls != 0 {
			t.Fatalf("lifecycle cancellation before Mock must not call Mock, save, or query")
		}
	})

	t.Run("lifecycle_cancel_during_registration_cancels_operation", func(t *testing.T) {
		base := submitTestOrder(ResultUnknown, 100)
		deps := submitTestValidDeps(base)
		lifecycle, cancelLifecycle := context.WithCancel(context.Background())
		defer cancelLifecycle()
		clientCtx, cancelClient := context.WithCancel(context.Background())
		defer cancelClient()

		registerStarted := make(chan struct{})
		registerCtxDone := make(chan struct{})
		deps.reg.onCall = func(ctx context.Context) {
			close(registerStarted)
			<-ctx.Done()
			close(registerCtxDone)
			deps.reg.err = ctx.Err()
		}

		done := submitTestStart(deps.submitter(lifecycle), clientCtx, submitTestRequestID, submitTestInput())
		submitTestWaitClosed(t, registerStarted, "registration start")
		cancelLifecycle()
		submitTestWaitClosed(t, registerCtxDone, "registration")
		got := submitTestAwait(t, done)

		if got.err == nil {
			t.Fatalf("expected lifecycle cancellation error")
		}
		if got.result.Created {
			t.Fatalf("registration cancellation must not be created")
		}
		if deps.payer.calls != 0 || deps.recorder.calls != 0 {
			t.Fatalf("lifecycle cancellation during registration must not call Mock or save")
		}
	})

	t.Run("lifecycle_cancel_during_registration_blocks_pay_after_created", func(t *testing.T) {
		base := submitTestOrder(ResultUnknown, 100)
		deps := submitTestValidDeps(base)
		lifecycle, cancelLifecycle := context.WithCancel(context.Background())
		defer cancelLifecycle()
		clientCtx, cancelClient := context.WithCancel(context.Background())
		defer cancelClient()

		registerStarted := make(chan struct{})
		registerCtxDone := make(chan struct{})
		deps.reg.onCall = func(ctx context.Context) {
			close(registerStarted)
			<-ctx.Done()
			close(registerCtxDone)
		}

		done := submitTestStart(deps.submitter(lifecycle), clientCtx, submitTestRequestID, submitTestInput())
		submitTestWaitClosed(t, registerStarted, "registration start")
		cancelLifecycle()
		submitTestWaitClosed(t, registerCtxDone, "registration")
		got := submitTestAwait(t, done)

		if !errors.Is(got.err, ErrUnavailable) {
			t.Fatalf("expected unavailable lifecycle classification")
		}
		if !got.result.Created || got.result.Order.Result != ResultUnknown {
			t.Fatalf("expected created unknown order")
		}
		if deps.payer.calls != 0 || deps.recorder.calls != 0 {
			t.Fatalf("lifecycle cancellation after created registration must not call Mock or save")
		}
	})

	t.Run("lifecycle_cancel_during_mock_cancels_operation_and_finish", func(t *testing.T) {
		base := submitTestOrder(ResultUnknown, 100)
		deps := submitTestValidDeps(base)
		lifecycle, cancelLifecycle := context.WithCancel(context.Background())
		defer cancelLifecycle()
		clientCtx, cancelClient := context.WithCancel(context.Background())
		defer cancelClient()

		payStarted := make(chan struct{})
		payCtxDone := make(chan struct{})
		deps.payer.pay = func(ctx context.Context, request MockPaymentRequest) (MockTransaction, error) {
			close(payStarted)
			<-ctx.Done()
			close(payCtxDone)
			return MockTransaction{}, ctx.Err()
		}
		var recorderCtxErr error
		deps.recorder.save = func(ctx context.Context, observation Observation) (SaveObservationResult, error) {
			recorderCtxErr = ctx.Err()
			return SaveObservationResult{}, ctx.Err()
		}
		var lookupCtxErr error
		deps.lookup.byIDFn = func(ctx context.Context, clientID string, paymentOrderID PaymentOrderID) (PaymentOrder, bool, error) {
			lookupCtxErr = ctx.Err()
			return PaymentOrder{}, false, ctx.Err()
		}

		done := submitTestStart(deps.submitter(lifecycle), clientCtx, submitTestRequestID, submitTestInput())
		submitTestWaitClosed(t, payStarted, "mock start")
		cancelLifecycle()
		submitTestWaitClosed(t, payCtxDone, "mock")
		got := submitTestAwait(t, done)

		if got.err == nil {
			t.Fatalf("expected lifecycle cancellation error")
		}
		if got.result.Order.Result == ResultSucceeded {
			t.Fatalf("must not return a confirmation")
		}
		if deps.payer.calls != 1 {
			t.Fatalf("expected exactly one Mock call")
		}
		if deps.recorder.calls != 1 {
			t.Fatalf("expected one save attempt")
		}
		if deps.lookup.byIDCalls != 1 {
			t.Fatalf("expected one readback attempt")
		}
		if recorderCtxErr == nil {
			t.Fatalf("finish save context must already be canceled")
		}
		if lookupCtxErr == nil {
			t.Fatalf("finish readback context must already be canceled")
		}
	})

	t.Run("mock_error_keeps_unknown_with_fixed_issue", func(t *testing.T) {
		base := submitTestOrder(ResultUnknown, 100)
		deps := submitTestValidDeps(base)
		deps.payer.transaction = submitTestTransaction(base, ResultSucceeded)
		deps.payer.err = errors.New("mock timeout secret")

		got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if !got.Created || got.Order.Result != ResultUnknown {
			t.Fatalf("expected unknown order")
		}
		submitTestAssertIssues(t, got.Issues, IssueCodeMockResultUnconfirmed)
		if strings.Contains(got.Issues[0].Message, "secret") {
			t.Fatalf("issue message leaked a raw error")
		}
		if deps.payer.calls != 1 || deps.recorder.calls != 1 {
			t.Fatalf("expected one Mock call and one save")
		}
		if deps.lookup.byIDCalls != 0 || deps.lookup.byBusinessCalls != 0 {
			t.Fatalf("successful save must not use lookup")
		}
		observation := deps.recorder.observation
		if observation.Candidate != nil || observation.EvidenceRejected {
			t.Fatalf("mock error must not produce a candidate or rejection")
		}
		if observation.Evidence.Amount != nil || observation.Evidence.Result != nil {
			t.Fatalf("mock error must not fabricate evidence")
		}
		if observation.Reason == nil || observation.Reason.Code != reasonCodeUnconfirmed {
			t.Fatalf("expected fixed unconfirmed reason")
		}
	})

	t.Run("mock_error_does_not_downgrade_concurrent_confirmation", func(t *testing.T) {
		base := submitTestOrder(ResultUnknown, 100)
		confirmed := submitTestOrder(ResultSucceeded, 100)
		deps := submitTestValidDeps(base)
		deps.payer.err = errors.New("mock timeout secret")
		deps.recorder.save = func(_ context.Context, observation Observation) (SaveObservationResult, error) {
			return SaveObservationResult{
				Order: confirmed,
				Record: ResultHandlingRecord{
					ID:             observation.ID,
					PaymentOrderID: observation.PaymentOrderID,
					RequestID:      observation.RequestID,
					Source:         observation.Source,
					ObservedAt:     observation.ObservedAt,
					ResultBefore:   ResultSucceeded,
					ResultAfter:    ResultSucceeded,
					Decision:       DecisionKeepConfirmed,
				},
			}, nil
		}
		got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if got.Order.Result != ResultSucceeded {
			t.Fatalf("confirmed result must not be downgraded")
		}
		submitTestAssertIssues(t, got.Issues, IssueCodeMockResultUnconfirmed)
	})

	t.Run("mock_evidence_mismatch_is_rejected", func(t *testing.T) {
		base := submitTestOrder(ResultUnknown, 100)
		deps := submitTestValidDeps(base)
		deps.payer.transaction = submitTestTransaction(base, ResultSucceeded)
		deps.payer.transaction.Amount = 101
		var saved SaveObservationResult
		deps.recorder.save = func(ctx context.Context, observation Observation) (SaveObservationResult, error) {
			result, err := submitTestDecideRecorder(base)(ctx, observation)
			if err != nil {
				return SaveObservationResult{}, err
			}
			saved = result
			return result, nil
		}

		got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if !got.Created || got.Order.Result != ResultUnknown {
			t.Fatalf("expected unknown order")
		}
		submitTestAssertIssues(t, got.Issues, IssueCodeMockEvidenceRejected)
		if saved.Record.Decision != DecisionRejectEvidence {
			t.Fatalf("expected storage to reject evidence")
		}
		observation := deps.recorder.observation
		if !observation.EvidenceRejected || observation.Candidate != nil {
			t.Fatalf("mismatched evidence must be rejected without candidate")
		}
		if observation.Evidence.Amount == nil || *observation.Evidence.Amount != 101 {
			t.Fatalf("expected actual mismatched amount in evidence")
		}
		if !resultTestHasDiff(observation.Evidence.Differences, EvidenceFieldAmount) {
			t.Fatalf("expected amount difference")
		}
		if deps.payer.calls != 1 || deps.recorder.calls != 1 {
			t.Fatalf("expected one Mock call and one save")
		}
	})

	t.Run("save_error_never_returns_uncommitted_confirmation", func(t *testing.T) {
		base := submitTestOrder(ResultUnknown, 100)
		confirmed := submitTestOrder(ResultSucceeded, 100)
		cases := []struct {
			name         string
			saveResult   SaveObservationResult
			saveErr      error
			lookupOrder  PaymentOrder
			lookupFound  bool
			lookupErr    error
			wantErr      error
			wantResult   Result
			wantIssues   []string
			wantReturned bool
		}{
			{
				name:         "lookup_unknown",
				saveErr:      errors.New("save commit secret"),
				lookupOrder:  base,
				lookupFound:  true,
				wantResult:   ResultUnknown,
				wantIssues:   []string{IssueCodeResultSaveError},
				wantReturned: true,
			},
			{
				name:         "lookup_confirmed",
				saveErr:      errors.New("save commit secret"),
				lookupOrder:  confirmed,
				lookupFound:  true,
				wantResult:   ResultSucceeded,
				wantIssues:   []string{IssueCodeResultSaveError},
				wantReturned: true,
			},
			{
				name: "save_returned_confirmed_with_error",
				saveResult: SaveObservationResult{
					Order: confirmed,
					Record: ResultHandlingRecord{
						ID:             resultTestObsID,
						PaymentOrderID: base.ID,
						RequestID:      submitTestRequestID,
						Source:         SourcePayment,
						ObservedAt:     submitTestTime(),
						ResultBefore:   ResultUnknown,
						ResultAfter:    ResultSucceeded,
						Decision:       DecisionAcceptConfirmation,
					},
				},
				saveErr:      errors.New("save commit secret"),
				lookupOrder:  base,
				lookupFound:  true,
				wantResult:   ResultUnknown,
				wantIssues:   []string{IssueCodeResultSaveError},
				wantReturned: true,
			},
			{
				name:    "lookup_not_found",
				saveErr: errors.New("save commit secret"),
				wantErr: ErrIndeterminate,
			},
			{
				name:      "lookup_error",
				saveErr:   errors.New("save commit secret"),
				lookupErr: errors.New("lookup secret"),
				wantErr:   ErrIndeterminate,
			},
			{
				name:      "lookup_error_unavailable",
				saveErr:   errors.New("save commit secret"),
				lookupErr: ErrUnavailable,
				wantErr:   ErrUnavailable,
			},
			{
				name:    "save_unavailable_lookup_not_found",
				saveErr: ErrUnavailable,
				wantErr: ErrUnavailable,
			},
		}

		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				deps := submitTestValidDeps(base)
				deps.recorder.save = nil
				deps.recorder.result = tc.saveResult
				deps.recorder.err = tc.saveErr
				deps.lookup.byIDOrder = tc.lookupOrder
				deps.lookup.byIDFound = tc.lookupFound
				deps.lookup.byIDErr = tc.lookupErr

				got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
				if tc.wantErr != nil {
					if !errors.Is(err, tc.wantErr) {
						t.Fatalf("expected error %v", tc.wantErr)
					}
					if got.Order.ID != "" || got.Created || len(got.Issues) != 0 {
						t.Fatalf("unrecoverable save error must not return a result")
					}
				} else {
					if err != nil {
						t.Fatalf("expected nil error")
					}
					if !got.Created {
						t.Fatalf("expected created fact to be retained")
					}
					if got.Order.Result != tc.wantResult {
						t.Fatalf("unexpected recovered result")
					}
					submitTestAssertIssues(t, got.Issues, tc.wantIssues...)
					if strings.Contains(got.Issues[0].Message, "secret") {
						t.Fatalf("issue message leaked a raw error")
					}
				}
				if deps.payer.calls != 1 || deps.recorder.calls != 1 {
					t.Fatalf("expected one Mock call and one save attempt")
				}
				if tc.wantErr == nil {
					if deps.lookup.byIDCalls != 1 || deps.lookup.byBusinessCalls != 0 {
						t.Fatalf("expected one lookup by ID")
					}
				}
			})
		}
	})

	t.Run("replay_same_observation_accepts_later_confirmation", func(t *testing.T) {
		cases := []struct {
			name       string
			decision   HandlingDecision
			setupPayer func(*submitTestPayer)
			wantIssue  string
		}{
			{
				name:     "keep_unknown_then_confirmed",
				decision: DecisionKeepUnknown,
				setupPayer: func(payer *submitTestPayer) {
					payer.err = errors.New("mock timeout secret")
				},
				wantIssue: IssueCodeMockResultUnconfirmed,
			},
			{
				name:     "reject_evidence_then_confirmed",
				decision: DecisionRejectEvidence,
				setupPayer: func(payer *submitTestPayer) {
					payer.transaction = submitTestTransaction(submitTestOrder(ResultUnknown, 100), ResultSucceeded)
					payer.transaction.Amount = 101
				},
				wantIssue: IssueCodeMockEvidenceRejected,
			},
		}

		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				base := submitTestOrder(ResultUnknown, 100)
				current := submitTestOrder(ResultSucceeded, 100)
				deps := submitTestValidDeps(base)
				tc.setupPayer(deps.payer)
				deps.recorder.save = func(_ context.Context, observation Observation) (SaveObservationResult, error) {
					return submitTestSavedWithTransition(current, observation, tc.decision, ResultUnknown, ResultUnknown), nil
				}

				got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
				if err != nil {
					t.Fatalf("expected replay acceptance")
				}
				if !got.Created {
					t.Fatalf("expected created fact to be retained")
				}
				if got.Order.Result != ResultSucceeded {
					t.Fatalf("expected current confirmed result")
				}
				if got.Order.ConfirmedAt == nil || got.Order.MockTransactionID == nil {
					t.Fatalf("expected current confirmation facts")
				}
				if deps.payer.calls != 1 || deps.recorder.calls != 1 {
					t.Fatalf("expected one Mock call and one save")
				}
				if deps.lookup.byIDCalls != 0 || deps.lookup.byBusinessCalls != 0 {
					t.Fatalf("replay must not perform an extra lookup")
				}
				foundIssue := false
				for _, issue := range got.Issues {
					if issue.Code == IssueCodeResultSaveError || issue.Code == IssueCodeTraceSaveError {
						t.Fatalf("replay must not report a save error")
					}
					if issue.Code == tc.wantIssue {
						foundIssue = true
					}
				}
				if !foundIssue {
					t.Fatalf("expected the observation issue to be preserved")
				}
			})
		}
	})

	t.Run("validate_saved_result_replay_boundaries", func(t *testing.T) {
		base := submitTestOrder(ResultUnknown, 100)
		observation := submitTestObservation(base)
		currentSucceeded := submitTestOrder(ResultSucceeded, 100)
		currentFailed := submitTestOrder(ResultFailed, 100)
		currentUnknown := submitTestOrder(ResultUnknown, 100)

		makeSaved := func(current PaymentOrder, decision HandlingDecision, before, after Result) SaveObservationResult {
			return submitTestSavedWithTransition(current, observation, decision, before, after)
		}
		mutate := func(saved SaveObservationResult, change func(*SaveObservationResult)) SaveObservationResult {
			change(&saved)
			return saved
		}

		cases := []struct {
			name    string
			saved   SaveObservationResult
			wantErr bool
		}{
			{
				name:  "unknown_history_keep_unknown_current_succeeded",
				saved: makeSaved(currentSucceeded, DecisionKeepUnknown, ResultUnknown, ResultUnknown),
			},
			{
				name:  "unknown_history_reject_current_failed",
				saved: makeSaved(currentFailed, DecisionRejectEvidence, ResultUnknown, ResultUnknown),
			},
			{
				name:  "same_accept_confirmation",
				saved: makeSaved(currentSucceeded, DecisionAcceptConfirmation, ResultUnknown, ResultSucceeded),
			},
			{
				name:  "same_keep_confirmed",
				saved: makeSaved(currentSucceeded, DecisionKeepConfirmed, ResultSucceeded, ResultSucceeded),
			},
			{
				name:    "confirmed_history_opposite_current",
				saved:   makeSaved(currentFailed, DecisionKeepConfirmed, ResultSucceeded, ResultSucceeded),
				wantErr: true,
			},
			{
				name:    "confirmed_history_current_unknown",
				saved:   makeSaved(currentUnknown, DecisionKeepConfirmed, ResultSucceeded, ResultSucceeded),
				wantErr: true,
			},
			{
				name:    "unknown_history_accept_decision",
				saved:   makeSaved(currentSucceeded, DecisionAcceptConfirmation, ResultUnknown, ResultUnknown),
				wantErr: true,
			},
			{
				name:    "unknown_history_keep_confirmed_decision",
				saved:   makeSaved(currentSucceeded, DecisionKeepConfirmed, ResultUnknown, ResultUnknown),
				wantErr: true,
			},
			{
				name:    "unknown_history_reject_before_confirmed",
				saved:   makeSaved(currentSucceeded, DecisionRejectEvidence, ResultSucceeded, ResultUnknown),
				wantErr: true,
			},
			{
				name:    "unknown_history_keep_unknown_before_confirmed",
				saved:   makeSaved(currentSucceeded, DecisionKeepUnknown, ResultSucceeded, ResultUnknown),
				wantErr: true,
			},
			{
				name:    "confirmed_history_current_opposite_failed",
				saved:   makeSaved(currentFailed, DecisionAcceptConfirmation, ResultUnknown, ResultSucceeded),
				wantErr: true,
			},
			{
				name: "order_id_mismatch",
				saved: mutate(makeSaved(currentSucceeded, DecisionKeepUnknown, ResultUnknown, ResultUnknown), func(saved *SaveObservationResult) {
					saved.Order.ID = submitTestOtherOrderID
				}),
				wantErr: true,
			},
			{
				name: "order_client_mismatch",
				saved: mutate(makeSaved(currentSucceeded, DecisionKeepUnknown, ResultUnknown, ResultUnknown), func(saved *SaveObservationResult) {
					saved.Order.ClientID = "other_client"
				}),
				wantErr: true,
			},
			{
				name: "order_amount_mismatch",
				saved: mutate(makeSaved(currentSucceeded, DecisionKeepUnknown, ResultUnknown, ResultUnknown), func(saved *SaveObservationResult) {
					saved.Order.Amount = 101
				}),
				wantErr: true,
			},
			{
				name: "confirmed_without_confirmed_at",
				saved: mutate(makeSaved(currentSucceeded, DecisionKeepUnknown, ResultUnknown, ResultUnknown), func(saved *SaveObservationResult) {
					saved.Order.ConfirmedAt = nil
				}),
				wantErr: true,
			},
			{
				name: "invalid_mock_transaction_id",
				saved: mutate(makeSaved(currentSucceeded, DecisionKeepUnknown, ResultUnknown, ResultUnknown), func(saved *SaveObservationResult) {
					bad := MockTransactionID("not-a-uuid")
					saved.Order.MockTransactionID = &bad
				}),
				wantErr: true,
			},
			{
				name: "record_id_mismatch",
				saved: mutate(makeSaved(currentSucceeded, DecisionKeepUnknown, ResultUnknown, ResultUnknown), func(saved *SaveObservationResult) {
					saved.Record.ID = ResultHandlingRecordID("0f1e2d3c-4b5a-4c6d-9e8f-7a6b5c4d3e2f")
				}),
				wantErr: true,
			},
			{
				name: "record_request_mismatch",
				saved: mutate(makeSaved(currentSucceeded, DecisionKeepUnknown, ResultUnknown, ResultUnknown), func(saved *SaveObservationResult) {
					saved.Record.RequestID = RequestID("0f1e2d3c-4b5a-4c6d-9e8f-7a6b5c4d3e2f")
				}),
				wantErr: true,
			},
			{
				name: "record_source_mismatch",
				saved: mutate(makeSaved(currentSucceeded, DecisionKeepUnknown, ResultUnknown, ResultUnknown), func(saved *SaveObservationResult) {
					saved.Record.Source = SourceQuery
				}),
				wantErr: true,
			},
			{
				name: "invalid_decision",
				saved: mutate(makeSaved(currentSucceeded, DecisionKeepUnknown, ResultUnknown, ResultUnknown), func(saved *SaveObservationResult) {
					saved.Record.Decision = HandlingDecision("BOGUS")
				}),
				wantErr: true,
			},
			{
				name: "invalid_record_result",
				saved: mutate(makeSaved(currentSucceeded, DecisionKeepUnknown, ResultUnknown, ResultUnknown), func(saved *SaveObservationResult) {
					saved.Record.ResultAfter = Result("BOGUS")
				}),
				wantErr: true,
			},
		}

		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				err := validateSavedResult(tc.saved, base, observation)
				if tc.wantErr && err == nil {
					t.Fatalf("expected rejection")
				}
				if !tc.wantErr && err != nil {
					t.Fatalf("expected acceptance")
				}
			})
		}
	})

	t.Run("save_error_after_mock_error_reports_trace_save", func(t *testing.T) {
		base := submitTestOrder(ResultUnknown, 100)
		deps := submitTestValidDeps(base)
		deps.payer.err = errors.New("mock timeout secret")
		deps.recorder.save = nil
		deps.recorder.err = errors.New("trace save secret")
		deps.lookup.byIDOrder = base
		deps.lookup.byIDFound = true

		got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if got.Order.Result != ResultUnknown {
			t.Fatalf("expected unknown result")
		}
		submitTestAssertIssues(t, got.Issues, IssueCodeMockResultUnconfirmed, IssueCodeTraceSaveError)
		if deps.payer.calls != 1 || deps.recorder.calls != 1 || deps.lookup.byIDCalls != 1 {
			t.Fatalf("expected one Mock call, one save, and one lookup")
		}
	})

	t.Run("save_error_after_rejected_evidence_reports_trace_save", func(t *testing.T) {
		base := submitTestOrder(ResultUnknown, 100)
		deps := submitTestValidDeps(base)
		deps.payer.transaction = submitTestTransaction(base, ResultSucceeded)
		deps.payer.transaction.Amount = 101
		deps.recorder.save = nil
		deps.recorder.err = errors.New("trace save secret")
		deps.lookup.byIDOrder = base
		deps.lookup.byIDFound = true

		got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if got.Order.Result != ResultUnknown {
			t.Fatalf("expected unknown result")
		}
		submitTestAssertIssues(t, got.Issues, IssueCodeMockEvidenceRejected, IssueCodeTraceSaveError)
		if deps.payer.calls != 1 || deps.recorder.calls != 1 || deps.lookup.byIDCalls != 1 {
			t.Fatalf("expected one Mock call, one save, and one lookup")
		}
	})

	t.Run("save_error_uses_same_finish_deadline_for_readback", func(t *testing.T) {
		base := submitTestOrder(ResultUnknown, 100)
		deps := submitTestValidDeps(base)
		var recorderDeadline time.Time
		deps.recorder.save = func(ctx context.Context, observation Observation) (SaveObservationResult, error) {
			recorderDeadline, _ = ctx.Deadline()
			<-ctx.Done()
			return SaveObservationResult{}, ctx.Err()
		}
		var lookupDeadline time.Time
		deps.lookup.byIDFn = func(ctx context.Context, clientID string, paymentOrderID PaymentOrderID) (PaymentOrder, bool, error) {
			lookupDeadline, _ = ctx.Deadline()
			return base, true, nil
		}

		start := time.Now()
		got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
		elapsed := time.Since(start)
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if got.Order.Result != ResultUnknown {
			t.Fatalf("expected unknown result")
		}
		submitTestAssertIssues(t, got.Issues, IssueCodeResultSaveError)
		if recorderDeadline.IsZero() || lookupDeadline.IsZero() {
			t.Fatalf("expected finish deadlines to be set")
		}
		if !recorderDeadline.Equal(lookupDeadline) {
			t.Fatalf("save and readback must share the same finish deadline")
		}
		if elapsed < 1500*time.Millisecond || elapsed >= 4*time.Second {
			t.Fatalf("finish window was not the expected bounded window")
		}
	})

	t.Run("finish_save_survives_client_cancel_after_mock", func(t *testing.T) {
		base := submitTestOrder(ResultUnknown, 100)
		deps := submitTestValidDeps(base)
		ctx, cancel := context.WithCancel(context.Background())
		deps.payer.pay = func(ctx context.Context, request MockPaymentRequest) (MockTransaction, error) {
			cancel()
			return submitTestTransaction(base, ResultSucceeded), nil
		}
		var saveCtxErr error
		deps.recorder.save = func(ctx context.Context, observation Observation) (SaveObservationResult, error) {
			saveCtxErr = ctx.Err()
			return submitTestDecideRecorder(base)(ctx, observation)
		}

		got, err := deps.submitter(context.Background()).Submit(ctx, submitTestRequestID, submitTestInput())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if got.Order.Result != ResultSucceeded {
			t.Fatalf("expected confirmation to be saved")
		}
		submitTestAssertNoIssues(t, got.Issues)
		if saveCtxErr != nil {
			t.Fatalf("finish save must not inherit client cancellation")
		}
	})

	t.Run("lifecycle_cancel_during_finish_fails_without_confirmation", func(t *testing.T) {
		base := submitTestOrder(ResultUnknown, 100)
		deps := submitTestValidDeps(base)
		lifecycle, cancel := context.WithCancel(context.Background())
		deps.payer.pay = func(ctx context.Context, request MockPaymentRequest) (MockTransaction, error) {
			cancel()
			return submitTestTransaction(base, ResultSucceeded), nil
		}
		deps.recorder.save = func(ctx context.Context, observation Observation) (SaveObservationResult, error) {
			return SaveObservationResult{}, ctx.Err()
		}
		deps.lookup.byIDFn = func(ctx context.Context, clientID string, paymentOrderID PaymentOrderID) (PaymentOrder, bool, error) {
			return PaymentOrder{}, false, ctx.Err()
		}

		got, err := deps.submitter(lifecycle).Submit(context.Background(), submitTestRequestID, submitTestInput())
		if err == nil {
			t.Fatalf("expected lifecycle cancellation error")
		}
		if got.Order.Result == ResultSucceeded {
			t.Fatalf("must not return an uncommitted confirmation")
		}
	})

	t.Run("mock_timeout_is_bounded_and_keeps_unknown", func(t *testing.T) {
		base := submitTestOrder(ResultUnknown, 100)
		deps := submitTestValidDeps(base)
		deps.payer.pay = func(ctx context.Context, request MockPaymentRequest) (MockTransaction, error) {
			<-ctx.Done()
			return MockTransaction{}, ctx.Err()
		}

		start := time.Now()
		got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
		elapsed := time.Since(start)
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if got.Order.Result != ResultUnknown {
			t.Fatalf("expected unknown result")
		}
		submitTestAssertIssues(t, got.Issues, IssueCodeMockResultUnconfirmed)
		if elapsed < 2500*time.Millisecond || elapsed >= 5*time.Second {
			t.Fatalf("mock call was not bounded by the expected timeout")
		}
		if deps.payer.calls != 1 || deps.recorder.calls != 1 {
			t.Fatalf("expected one Mock call and one save")
		}
	})

	t.Run("cancel_during_mock_keeps_fixed_unknown_observation", func(t *testing.T) {
		base := submitTestOrder(ResultUnknown, 100)
		deps := submitTestValidDeps(base)
		ctx, cancel := context.WithCancel(context.Background())
		deps.payer.pay = func(payCtx context.Context, request MockPaymentRequest) (MockTransaction, error) {
			cancel()
			<-payCtx.Done()
			return MockTransaction{}, payCtx.Err()
		}

		got, err := deps.submitter(context.Background()).Submit(ctx, submitTestRequestID, submitTestInput())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if got.Order.Result != ResultUnknown {
			t.Fatalf("expected unknown result")
		}
		submitTestAssertIssues(t, got.Issues, IssueCodeMockResultUnconfirmed)
		if deps.payer.calls != 1 || deps.recorder.calls != 1 {
			t.Fatalf("expected one Mock call and one save")
		}
	})

	t.Run("mock_honors_outer_deadline", func(t *testing.T) {
		base := submitTestOrder(ResultUnknown, 100)
		deps := submitTestValidDeps(base)
		deps.payer.pay = func(ctx context.Context, request MockPaymentRequest) (MockTransaction, error) {
			<-ctx.Done()
			return MockTransaction{}, ctx.Err()
		}
		ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
		defer cancel()

		start := time.Now()
		got, err := deps.submitter(context.Background()).Submit(ctx, submitTestRequestID, submitTestInput())
		elapsed := time.Since(start)
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if got.Order.Result != ResultUnknown {
			t.Fatalf("expected unknown result")
		}
		submitTestAssertIssues(t, got.Issues, IssueCodeMockResultUnconfirmed)
		if elapsed >= 2*time.Second {
			t.Fatalf("mock call did not honor the outer deadline")
		}
	})
}
