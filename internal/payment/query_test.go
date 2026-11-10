package payment

import (
	"context"
	"errors"
	"testing"
	"time"
)

const (
	queryTestOrderID      = PaymentOrderID("8c7d6e5f-4a3b-4c2d-9e1f-0a9b8c7d6e5f")
	queryTestOtherOrderID = PaymentOrderID("9d8e7f6a-5b4c-4d3e-8f2a-1b0c9d8e7f6a")
	queryTestMockTxID     = MockTransactionID("ab1c2d3e-4f5a-4b6c-9d7e-8f9a0b1c2d3e")
	queryTestOtherMockID  = MockTransactionID("cd3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f6a")
	queryTestRequestID    = RequestID("bc2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f")
	queryTestClientID     = "client_query"
	queryTestBusinessNo   = "M-QRY-1"
)

func queryTestTime() time.Time {
	return time.Date(2026, 11, 8, 12, 0, 0, 123456789, time.UTC)
}

func queryTestOldTime() time.Time {
	return time.Date(2026, 11, 8, 11, 0, 0, 0, time.UTC)
}

func queryTestUnknownOrder() PaymentOrder {
	return PaymentOrder{
		ID:              queryTestOrderID,
		ClientID:        queryTestClientID,
		BusinessOrderNo: queryTestBusinessNo,
		Amount:          100,
		Currency:        "CNY",
		Result:          ResultUnknown,
		RegisteredAt:    queryTestTime(),
	}
}

func queryTestConfirmedOrder(result Result) PaymentOrder {
	order := queryTestUnknownOrder()
	order.Result = result
	at := queryTestOldTime()
	tx := queryTestMockTxID
	order.ConfirmedAt = &at
	order.MockTransactionID = &tx
	return order
}

func queryTestInputByID() QueryInput {
	return QueryInput{ClientID: queryTestClientID, PaymentOrderID: queryTestOrderID}
}

func queryTestInputByBusiness() QueryInput {
	return QueryInput{ClientID: queryTestClientID, BusinessOrderNo: queryTestBusinessNo}
}

func queryTestTransaction(order PaymentOrder, result Result) MockTransaction {
	return MockTransaction{
		ID:                  queryTestMockTxID,
		PlatformReferenceID: order.ID,
		Amount:              order.Amount,
		Currency:            order.Currency,
		Result:              result,
		ProcessedAt:         queryTestTime(),
	}
}

type queryTestLookup struct {
	byIDCalls       int
	byBusinessCalls int

	byIDOrder PaymentOrder
	byIDFound bool
	byIDErr   error

	byBusinessOrder PaymentOrder
	byBusinessFound bool
	byBusinessErr   error

	byIDFn       func(context.Context, string, PaymentOrderID) (PaymentOrder, bool, error)
	byBusinessFn func(context.Context, string, string) (PaymentOrder, bool, error)

	byIDDeadline       time.Time
	byBusinessDeadline time.Time
	byIDCtxErr         error
	byBusinessCtxErr   error
}

func (f *queryTestLookup) FindOrderByID(ctx context.Context, clientID string, paymentOrderID PaymentOrderID) (PaymentOrder, bool, error) {
	f.byIDCalls++
	f.byIDDeadline, _ = ctx.Deadline()
	f.byIDCtxErr = ctx.Err()
	if f.byIDFn != nil {
		return f.byIDFn(ctx, clientID, paymentOrderID)
	}
	return f.byIDOrder, f.byIDFound, f.byIDErr
}

func (f *queryTestLookup) FindOrderByBusinessOrderNo(ctx context.Context, clientID string, businessOrderNo string) (PaymentOrder, bool, error) {
	f.byBusinessCalls++
	f.byBusinessDeadline, _ = ctx.Deadline()
	f.byBusinessCtxErr = ctx.Err()
	if f.byBusinessFn != nil {
		return f.byBusinessFn(ctx, clientID, businessOrderNo)
	}
	return f.byBusinessOrder, f.byBusinessFound, f.byBusinessErr
}

type queryTestMockLookup struct {
	calls       int
	reference   PaymentOrderID
	transaction MockTransaction
	found       bool
	err         error
	fn          func(context.Context, PaymentOrderID) (MockTransaction, bool, error)
	deadline    time.Time
	ctxErr      error
}

func (f *queryTestMockLookup) FindTransactionByPlatformReference(ctx context.Context, platformReferenceID PaymentOrderID) (MockTransaction, bool, error) {
	f.calls++
	f.reference = platformReferenceID
	f.deadline, _ = ctx.Deadline()
	f.ctxErr = ctx.Err()
	if f.fn != nil {
		return f.fn(ctx, platformReferenceID)
	}
	return f.transaction, f.found, f.err
}

type queryTestRecorder struct {
	calls       int
	observation Observation
	saved       SaveObservationResult
	result      SaveObservationResult
	err         error
	fn          func(context.Context, Observation) (SaveObservationResult, error)
	deadline    time.Time
	ctxErr      error
}

func (f *queryTestRecorder) SaveObservation(ctx context.Context, observation Observation) (SaveObservationResult, error) {
	f.calls++
	f.observation = observation
	f.deadline, _ = ctx.Deadline()
	f.ctxErr = ctx.Err()
	if f.fn != nil {
		result, err := f.fn(ctx, observation)
		f.saved = result
		return result, err
	}
	f.saved = f.result
	return f.result, f.err
}

type queryTestDeps struct {
	lookup   *queryTestLookup
	recorder *queryTestRecorder
	mock     *queryTestMockLookup
}

func queryTestValidDeps(order PaymentOrder) *queryTestDeps {
	return &queryTestDeps{
		lookup: &queryTestLookup{
			byIDOrder:       order,
			byIDFound:       true,
			byBusinessOrder: order,
			byBusinessFound: true,
		},
		recorder: &queryTestRecorder{},
		mock:     &queryTestMockLookup{},
	}
}

func (d *queryTestDeps) service(lifecycle context.Context) *QueryService {
	return NewQueryService(d.lookup, d.recorder, d.mock, lifecycle)
}

func (d *queryTestDeps) assertNoCalls(t *testing.T) {
	t.Helper()
	if d.lookup.byIDCalls != 0 || d.lookup.byBusinessCalls != 0 ||
		d.mock.calls != 0 || d.recorder.calls != 0 {
		t.Fatalf("expected no dependency calls")
	}
}

func queryTestDecideRecorder(base PaymentOrder) func(context.Context, Observation) (SaveObservationResult, error) {
	return func(ctx context.Context, observation Observation) (SaveObservationResult, error) {
		updated, record, err := DecideObservation(base, observation, queryTestTime())
		if err != nil {
			return SaveObservationResult{}, err
		}
		return SaveObservationResult{Order: updated, Record: record}, nil
	}
}

func queryTestSavedAccepted(observation Observation, order PaymentOrder, result Result) SaveObservationResult {
	return SaveObservationResult{
		Order: order,
		Record: ResultHandlingRecord{
			ID:             observation.ID,
			PaymentOrderID: observation.PaymentOrderID,
			RequestID:      observation.RequestID,
			Source:         observation.Source,
			ObservedAt:     observation.ObservedAt,
			ResultBefore:   ResultUnknown,
			ResultAfter:    result,
			Decision:       DecisionAcceptConfirmation,
		},
	}
}

func queryTestSavedReplayUnknownHistory(observation Observation, current PaymentOrder, decision HandlingDecision) SaveObservationResult {
	return SaveObservationResult{
		Order: current,
		Record: ResultHandlingRecord{
			ID:             observation.ID,
			PaymentOrderID: observation.PaymentOrderID,
			RequestID:      observation.RequestID,
			Source:         observation.Source,
			ObservedAt:     observation.ObservedAt,
			ResultBefore:   ResultUnknown,
			ResultAfter:    ResultUnknown,
			Decision:       decision,
		},
	}
}

func queryTestHasIssue(issues []Issue, code string) bool {
	for _, issue := range issues {
		if issue.Code == code {
			return true
		}
	}
	return false
}

func queryTestAssertIssues(t *testing.T, got []Issue, want ...string) {
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

func queryTestAssertNoIssues(t *testing.T, got []Issue) {
	t.Helper()
	if len(got) != 0 {
		t.Fatalf("expected no issues")
	}
}

type queryTestAsyncResult struct {
	result QueryResult
	err    error
}

func queryTestStart(service *QueryService, ctx context.Context, requestID RequestID, input QueryInput) <-chan queryTestAsyncResult {
	done := make(chan queryTestAsyncResult, 1)
	go func() {
		result, err := service.Query(ctx, requestID, input)
		done <- queryTestAsyncResult{result: result, err: err}
	}()
	return done
}

func queryTestWaitClosed(t *testing.T, ch <-chan struct{}, what string) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(2 * time.Second):
		t.Fatalf("%s context was not canceled", what)
	}
}

func queryTestAwait(t *testing.T, done <-chan queryTestAsyncResult) queryTestAsyncResult {
	t.Helper()
	select {
	case result := <-done:
		return result
	case <-time.After(2 * time.Second):
		t.Fatalf("query did not finish")
		return queryTestAsyncResult{}
	}
}

func TestQueryPayment(t *testing.T) {
	t.Run("invalid_inputs_do_not_call_dependencies", func(t *testing.T) {
		confirmed := queryTestConfirmedOrder(ResultSucceeded)
		validID := queryTestInputByID()
		validBusiness := queryTestInputByBusiness()
		validRequest := queryTestRequestID

		cases := []struct {
			name      string
			requestID RequestID
			input     QueryInput
			ctx       context.Context
			want      error
		}{
			{
				name:      "invalid_client_id",
				requestID: validRequest,
				input:     QueryInput{ClientID: "", PaymentOrderID: queryTestOrderID},
				ctx:       context.Background(),
				want:      ErrInvalidInput,
			},
			{
				name:      "invalid_payment_order_id",
				requestID: validRequest,
				input:     QueryInput{ClientID: queryTestClientID, PaymentOrderID: "not-a-uuid"},
				ctx:       context.Background(),
				want:      ErrInvalidInput,
			},
			{
				name:      "invalid_business_order_no",
				requestID: validRequest,
				input:     QueryInput{ClientID: queryTestClientID, BusinessOrderNo: "bad order"},
				ctx:       context.Background(),
				want:      ErrInvalidInput,
			},
			{
				name:      "invalid_request_id",
				requestID: RequestID("not-a-uuid"),
				input:     validID,
				ctx:       context.Background(),
				want:      ErrInvalidInput,
			},
			{
				name:      "both_locators",
				requestID: validRequest,
				input: QueryInput{
					ClientID:        queryTestClientID,
					PaymentOrderID:  queryTestOrderID,
					BusinessOrderNo: queryTestBusinessNo,
				},
				ctx:  context.Background(),
				want: ErrInvalidInput,
			},
			{
				name:      "neither_locator",
				requestID: validRequest,
				input:     QueryInput{ClientID: queryTestClientID},
				ctx:       context.Background(),
				want:      ErrInvalidInput,
			},
			{
				name:      "nil_context",
				requestID: validRequest,
				input:     validID,
				ctx:       nil,
				want:      ErrInvalidInput,
			},
		}

		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				deps := queryTestValidDeps(confirmed)
				got, err := deps.service(context.Background()).Query(tc.ctx, tc.requestID, tc.input)
				if !errors.Is(err, tc.want) {
					t.Fatalf("expected error %v", tc.want)
				}
				if got.Order.ID != "" || len(got.Issues) != 0 {
					t.Fatalf("invalid input must not produce a result")
				}
				deps.assertNoCalls(t)
			})
		}

		t.Run("canceled_context", func(t *testing.T) {
			deps := queryTestValidDeps(confirmed)
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			got, err := deps.service(context.Background()).Query(ctx, validRequest, validID)
			if !errors.Is(err, context.Canceled) {
				t.Fatalf("expected canceled context")
			}
			if got.Order.ID != "" || len(got.Issues) != 0 {
				t.Fatalf("canceled context must not produce a result")
			}
			deps.assertNoCalls(t)
		})

		t.Run("nil_lifecycle", func(t *testing.T) {
			deps := queryTestValidDeps(confirmed)
			got, err := deps.service(nil).Query(context.Background(), validRequest, validID)
			if !errors.Is(err, ErrUnavailable) {
				t.Fatalf("expected unavailable lifecycle")
			}
			if got.Order.ID != "" || len(got.Issues) != 0 {
				t.Fatalf("nil lifecycle must not produce a result")
			}
			deps.assertNoCalls(t)
		})

		t.Run("canceled_lifecycle", func(t *testing.T) {
			deps := queryTestValidDeps(confirmed)
			lifecycle, cancel := context.WithCancel(context.Background())
			cancel()
			got, err := deps.service(lifecycle).Query(context.Background(), validRequest, validBusiness)
			if !errors.Is(err, ErrUnavailable) {
				t.Fatalf("expected unavailable lifecycle")
			}
			if got.Order.ID != "" || len(got.Issues) != 0 {
				t.Fatalf("canceled lifecycle must not produce a result")
			}
			deps.assertNoCalls(t)
		})

		t.Run("nil_dependencies_fail_closed", func(t *testing.T) {
			service := NewQueryService(nil, nil, nil, context.Background())
			if service == nil {
				t.Fatalf("expected a fail-closed service, got nil")
			}
			got, err := service.Query(context.Background(), validRequest, validID)
			if err == nil {
				t.Fatalf("expected an error")
			}
			if got.Order.ID != "" || len(got.Issues) != 0 {
				t.Fatalf("nil dependencies must not produce a result")
			}
		})

		t.Run("nil_receiver_fails_closed", func(t *testing.T) {
			var service *QueryService
			got, err := service.Query(context.Background(), validRequest, validID)
			if err == nil {
				t.Fatalf("expected an error")
			}
			if got.Order.ID != "" || len(got.Issues) != 0 {
				t.Fatalf("nil receiver must not produce a result")
			}
		})
	})

	t.Run("valid_locators_reach_lookup_and_confirmed_returns_directly", func(t *testing.T) {
		for _, result := range []Result{ResultSucceeded, ResultFailed} {
			result := result
			for _, locator := range []struct {
				name  string
				input QueryInput
				byID  bool
			}{
				{name: "by_id", input: queryTestInputByID(), byID: true},
				{name: "by_business_order_no", input: queryTestInputByBusiness(), byID: false},
			} {
				locator := locator
				t.Run(string(result)+"/"+locator.name, func(t *testing.T) {
					order := queryTestConfirmedOrder(result)
					deps := queryTestValidDeps(order)
					got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, locator.input)
					if err != nil {
						t.Fatalf("expected nil error")
					}
					if got.Order.Result != result {
						t.Fatalf("unexpected confirmed result")
					}
					queryTestAssertNoIssues(t, got.Issues)
					if locator.byID {
						if deps.lookup.byIDCalls != 1 || deps.lookup.byBusinessCalls != 0 {
							t.Fatalf("expected exactly one lookup by ID")
						}
					} else {
						if deps.lookup.byBusinessCalls != 1 || deps.lookup.byIDCalls != 0 {
							t.Fatalf("expected exactly one lookup by business order number")
						}
					}
					if deps.mock.calls != 0 || deps.recorder.calls != 0 {
						t.Fatalf("confirmed query must not call Mock or save")
					}
				})
			}
		}
	})

	t.Run("not_found_and_lookup_error_are_distinct", func(t *testing.T) {
		cases := []struct {
			name      string
			byID      bool
			lookupErr error
			want      error
		}{
			{name: "by_id_not_found", byID: true, want: ErrNotFound},
			{name: "by_business_not_found", byID: false, want: ErrNotFound},
			{name: "by_id_lookup_error", byID: true, lookupErr: ErrUnavailable, want: ErrUnavailable},
			{name: "by_business_lookup_error", byID: false, lookupErr: errors.New("storage fault")},
		}

		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				deps := queryTestValidDeps(queryTestUnknownOrder())
				input := queryTestInputByID()
				if tc.byID {
					deps.lookup.byIDFound = false
					deps.lookup.byIDErr = tc.lookupErr
				} else {
					input = queryTestInputByBusiness()
					deps.lookup.byBusinessFound = false
					deps.lookup.byBusinessErr = tc.lookupErr
				}
				got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, input)
				if tc.want != nil {
					if !errors.Is(err, tc.want) {
						t.Fatalf("expected error %v", tc.want)
					}
				} else {
					if err == nil {
						t.Fatalf("expected an error")
					}
					if errors.Is(err, ErrNotFound) {
						t.Fatalf("lookup error must not be reported as not found")
					}
				}
				if got.Order.ID != "" || len(got.Issues) != 0 {
					t.Fatalf("missing/error lookup must not produce a result")
				}
				if deps.mock.calls != 0 || deps.recorder.calls != 0 {
					t.Fatalf("missing/error lookup must not call Mock or save")
				}
			})
		}
	})

	t.Run("unknown_order_queries_mock_and_saves_confirmation", func(t *testing.T) {
		for _, result := range []Result{ResultSucceeded, ResultFailed} {
			result := result
			t.Run(string(result), func(t *testing.T) {
				base := queryTestUnknownOrder()
				deps := queryTestValidDeps(base)
				deps.mock.transaction = queryTestTransaction(base, result)
				deps.mock.found = true
				deps.recorder.fn = queryTestDecideRecorder(base)

				got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
				if err != nil {
					t.Fatalf("expected nil error")
				}
				if got.Order.Result != result {
					t.Fatalf("unexpected saved confirmation result")
				}
				queryTestAssertNoIssues(t, got.Issues)
				if deps.lookup.byIDCalls != 1 || deps.mock.calls != 1 || deps.recorder.calls != 1 {
					t.Fatalf("expected one lookup, one Mock query, and one save")
				}
				if deps.mock.reference != base.ID {
					t.Fatalf("Mock query did not use the platform order ID")
				}
				observation := deps.recorder.observation
				if err := ValidateUUIDv4(string(observation.ID)); err != nil {
					t.Fatalf("observation ID is not UUIDv4")
				}
				if observation.PaymentOrderID != base.ID || observation.ClientID != base.ClientID {
					t.Fatalf("observation association mismatch")
				}
				if observation.RequestID != queryTestRequestID || observation.Source != SourceQuery {
					t.Fatalf("observation request/source mismatch")
				}
				if err := ValidateUTC(observation.ObservedAt); err != nil {
					t.Fatalf("observation time is not UTC")
				}
				if observation.Candidate == nil || observation.Candidate.Result != result {
					t.Fatalf("expected a matching candidate")
				}
				if observation.Candidate.MockTransactionID == nil || *observation.Candidate.MockTransactionID != queryTestMockTxID {
					t.Fatalf("candidate Mock transaction ID mismatch")
				}
				if observation.EvidenceRejected || observation.Reason != nil {
					t.Fatalf("accepted evidence must not be marked rejected")
				}
				if observation.Evidence.PlatformReferenceID == nil || *observation.Evidence.PlatformReferenceID != base.ID {
					t.Fatalf("evidence platform reference mismatch")
				}
				if observation.Evidence.MockTransactionID == nil || *observation.Evidence.MockTransactionID != queryTestMockTxID {
					t.Fatalf("evidence Mock transaction ID mismatch")
				}
				if observation.Evidence.Amount == nil || *observation.Evidence.Amount != base.Amount {
					t.Fatalf("evidence amount mismatch")
				}
				if observation.Evidence.Currency == nil || *observation.Evidence.Currency != base.Currency {
					t.Fatalf("evidence currency mismatch")
				}
				if observation.Evidence.Result == nil || *observation.Evidence.Result != result {
					t.Fatalf("evidence result mismatch")
				}
				if len(observation.Evidence.Differences) != 0 {
					t.Fatalf("accepted evidence must not have differences")
				}
				if deps.recorder.saved.Record.Decision != DecisionAcceptConfirmation {
					t.Fatalf("expected storage to accept the confirmation")
				}
			})
		}
	})

	t.Run("unknown_not_found_keeps_unknown_and_reports_not_found", func(t *testing.T) {
		base := queryTestUnknownOrder()
		deps := queryTestValidDeps(base)
		deps.mock.found = false
		deps.recorder.fn = queryTestDecideRecorder(base)

		got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByBusiness())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if got.Order.Result != ResultUnknown {
			t.Fatalf("expected unknown order")
		}
		queryTestAssertIssues(t, got.Issues, IssueCodeMockResultNotFound)
		if deps.mock.calls != 1 || deps.recorder.calls != 1 {
			t.Fatalf("expected one Mock query and one save")
		}
		observation := deps.recorder.observation
		if observation.Candidate != nil || observation.EvidenceRejected {
			t.Fatalf("not-found query must not produce a candidate or rejection")
		}
		if observation.Reason == nil || observation.Reason.Code != IssueCodeMockResultNotFound {
			t.Fatalf("expected the not-found reason")
		}
		if observation.Evidence.Amount != nil || observation.Evidence.Result != nil {
			t.Fatalf("not-found query must not fabricate evidence")
		}
		if deps.recorder.saved.Record.Decision != DecisionKeepUnknown {
			t.Fatalf("expected storage to keep UNKNOWN")
		}
	})

	t.Run("unknown_mock_fault_keeps_unknown_and_reports_unconfirmed", func(t *testing.T) {
		base := queryTestUnknownOrder()
		deps := queryTestValidDeps(base)
		deps.mock.err = ErrUnavailable
		deps.recorder.fn = queryTestDecideRecorder(base)

		got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if got.Order.Result != ResultUnknown {
			t.Fatalf("expected unknown order")
		}
		queryTestAssertIssues(t, got.Issues, IssueCodeMockResultUnconfirmed)
		if deps.mock.calls != 1 || deps.recorder.calls != 1 {
			t.Fatalf("expected one Mock query and one save")
		}
		observation := deps.recorder.observation
		if observation.Candidate != nil || observation.EvidenceRejected {
			t.Fatalf("Mock fault must not produce a candidate or rejection")
		}
		if observation.Reason == nil || observation.Reason.Code != IssueCodeMockResultUnconfirmed {
			t.Fatalf("expected the unconfirmed reason")
		}
		if observation.Evidence.Amount != nil || observation.Evidence.Result != nil {
			t.Fatalf("Mock fault must not fabricate evidence")
		}
		if deps.recorder.saved.Record.Decision != DecisionKeepUnknown {
			t.Fatalf("expected storage to keep UNKNOWN")
		}
	})

	t.Run("unknown_mismatch_rejects_with_filtered_evidence", func(t *testing.T) {
		base := queryTestUnknownOrder()
		deps := queryTestValidDeps(base)
		deps.mock.transaction = queryTestTransaction(base, ResultSucceeded)
		deps.mock.transaction.Amount = 101
		deps.mock.found = true
		deps.recorder.fn = queryTestDecideRecorder(base)

		got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if got.Order.Result != ResultUnknown {
			t.Fatalf("mismatch must not confirm the order")
		}
		queryTestAssertIssues(t, got.Issues, IssueCodeMockEvidenceRejected)
		observation := deps.recorder.observation
		if !observation.EvidenceRejected || observation.Candidate != nil {
			t.Fatalf("mismatch must be rejected without a candidate")
		}
		if observation.Evidence.Amount == nil || *observation.Evidence.Amount != 101 {
			t.Fatalf("expected the actual mismatched amount")
		}
		if len(observation.Evidence.Differences) != 1 || observation.Evidence.Differences[0] != EvidenceFieldAmount {
			t.Fatalf("expected only the amount field difference")
		}
		if observation.Reason == nil || observation.Reason.Code != reasonCodeEvidenceMismatch {
			t.Fatalf("expected a controlled mismatch reason")
		}
		if deps.recorder.saved.Record.Decision != DecisionRejectEvidence {
			t.Fatalf("expected storage to reject the evidence")
		}
	})

	t.Run("save_error_new_confirmation_reads_back_and_reports_result_save", func(t *testing.T) {
		for _, readbackResult := range []Result{ResultUnknown, ResultSucceeded} {
			readbackResult := readbackResult
			t.Run(string(readbackResult), func(t *testing.T) {
				base := queryTestUnknownOrder()
				readback := queryTestUnknownOrder()
				if readbackResult != ResultUnknown {
					readback = queryTestConfirmedOrder(readbackResult)
				}
				deps := queryTestValidDeps(base)
				deps.mock.transaction = queryTestTransaction(base, ResultSucceeded)
				deps.mock.found = true
				confirmed := queryTestConfirmedOrder(ResultSucceeded)
				deps.recorder.err = ErrIndeterminate
				deps.recorder.result = SaveObservationResult{
					Order: confirmed,
					Record: ResultHandlingRecord{
						ID:             ResultHandlingRecordID(queryTestMockTxID),
						PaymentOrderID: base.ID,
						RequestID:      queryTestRequestID,
						Source:         SourceQuery,
						ObservedAt:     queryTestTime(),
						ResultBefore:   ResultUnknown,
						ResultAfter:    ResultSucceeded,
						Decision:       DecisionAcceptConfirmation,
					},
				}
				readbackCalls := 0
				readbackOrder := func() (PaymentOrder, bool, error) {
					readbackCalls++
					if readbackCalls == 1 {
						return base, true, nil
					}
					return readback, true, nil
				}
				deps.lookup.byIDFn = func(context.Context, string, PaymentOrderID) (PaymentOrder, bool, error) {
					return readbackOrder()
				}
				deps.lookup.byBusinessFn = func(context.Context, string, string) (PaymentOrder, bool, error) {
					return readbackOrder()
				}

				got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
				if err != nil {
					t.Fatalf("expected nil error")
				}
				if got.Order.Result != readbackResult {
					t.Fatalf("unexpected readback result")
				}
				queryTestAssertIssues(t, got.Issues, IssueCodeResultSaveError)
				if deps.lookup.byIDCalls+deps.lookup.byBusinessCalls != 2 || deps.mock.calls != 1 || deps.recorder.calls != 1 {
					t.Fatalf("expected initial lookup, Mock query, save, and one readback")
				}
			})
		}
	})

	t.Run("save_error_non_confirmation_reports_trace_save", func(t *testing.T) {
		cases := []struct {
			name      string
			mockFound bool
			mockErr   error
			mockTx    func(PaymentOrder) MockTransaction
			wantIssue string
		}{
			{
				name:      "not_found",
				mockFound: false,
				wantIssue: IssueCodeMockResultNotFound,
			},
			{
				name:      "mock_fault",
				mockErr:   ErrUnavailable,
				wantIssue: IssueCodeMockResultUnconfirmed,
			},
			{
				name:      "evidence_mismatch",
				mockFound: true,
				mockTx: func(order PaymentOrder) MockTransaction {
					tx := queryTestTransaction(order, ResultSucceeded)
					tx.Amount = 101
					return tx
				},
				wantIssue: IssueCodeMockEvidenceRejected,
			},
		}

		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				base := queryTestUnknownOrder()
				deps := queryTestValidDeps(base)
				deps.mock.found = tc.mockFound
				deps.mock.err = tc.mockErr
				if tc.mockTx != nil {
					deps.mock.transaction = tc.mockTx(base)
				}
				deps.recorder.err = ErrIndeterminate
				deps.lookup.byIDFn = func(context.Context, string, PaymentOrderID) (PaymentOrder, bool, error) {
					return base, true, nil
				}
				deps.lookup.byBusinessFn = func(context.Context, string, string) (PaymentOrder, bool, error) {
					return base, true, nil
				}

				got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
				if err != nil {
					t.Fatalf("expected nil error")
				}
				if got.Order.Result != ResultUnknown {
					t.Fatalf("expected unknown result")
				}
				queryTestAssertIssues(t, got.Issues, tc.wantIssue, IssueCodeTraceSaveError)
				if deps.mock.calls != 1 || deps.recorder.calls != 1 {
					t.Fatalf("expected one Mock query and one save")
				}
			})
		}
	})

	t.Run("save_error_readback_failure_is_safe", func(t *testing.T) {
		base := queryTestUnknownOrder()
		deps := queryTestValidDeps(base)
		deps.mock.transaction = queryTestTransaction(base, ResultSucceeded)
		deps.mock.found = true
		deps.recorder.err = ErrIndeterminate
		lookupCalls := 0
		readbackFailure := func() (PaymentOrder, bool, error) {
			lookupCalls++
			if lookupCalls == 1 {
				return base, true, nil
			}
			return PaymentOrder{}, false, ErrUnavailable
		}
		deps.lookup.byIDFn = func(context.Context, string, PaymentOrderID) (PaymentOrder, bool, error) {
			return readbackFailure()
		}
		deps.lookup.byBusinessFn = func(context.Context, string, string) (PaymentOrder, bool, error) {
			return readbackFailure()
		}

		got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
		if err == nil {
			t.Fatalf("expected an error")
		}
		if !errors.Is(err, ErrIndeterminate) && !errors.Is(err, ErrUnavailable) {
			t.Fatalf("expected an indeterminate or unavailable error")
		}
		if got.Order.ID != "" || len(got.Issues) != 0 {
			t.Fatalf("readback failure must not produce a result")
		}
	})

	t.Run("save_error_uses_single_finish_window", func(t *testing.T) {
		base := queryTestUnknownOrder()
		deps := queryTestValidDeps(base)
		deps.mock.transaction = queryTestTransaction(base, ResultSucceeded)
		deps.mock.found = true
		var recorderDeadline time.Time
		deps.recorder.fn = func(ctx context.Context, observation Observation) (SaveObservationResult, error) {
			recorderDeadline, _ = ctx.Deadline()
			<-ctx.Done()
			return SaveObservationResult{}, ctx.Err()
		}
		var readbackDeadline time.Time
		deps.lookup.byIDFn = func(ctx context.Context, clientID string, paymentOrderID PaymentOrderID) (PaymentOrder, bool, error) {
			readbackDeadline, _ = ctx.Deadline()
			return base, true, nil
		}
		deps.lookup.byBusinessFn = func(ctx context.Context, clientID string, businessOrderNo string) (PaymentOrder, bool, error) {
			readbackDeadline, _ = ctx.Deadline()
			return base, true, nil
		}

		start := time.Now()
		got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
		elapsed := time.Since(start)
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if got.Order.Result != ResultUnknown {
			t.Fatalf("expected unknown result")
		}
		queryTestAssertIssues(t, got.Issues, IssueCodeResultSaveError)
		if recorderDeadline.IsZero() || readbackDeadline.IsZero() {
			t.Fatalf("expected finish deadlines to be set")
		}
		if !recorderDeadline.Equal(readbackDeadline) {
			t.Fatalf("save and readback must share the same finish deadline")
		}
		if elapsed < 1500*time.Millisecond || elapsed >= 4*time.Second {
			t.Fatalf("finish window was not the expected bounded window")
		}
	})

	t.Run("client_cancel_after_mock_answer_uses_lifecycle_finish", func(t *testing.T) {
		base := queryTestUnknownOrder()
		deps := queryTestValidDeps(base)
		clientCtx, cancelClient := context.WithCancel(context.Background())
		defer cancelClient()
		deps.mock.fn = func(ctx context.Context, platformReferenceID PaymentOrderID) (MockTransaction, bool, error) {
			cancelClient()
			return queryTestTransaction(base, ResultSucceeded), true, nil
		}
		var saveCtxErr error
		deps.recorder.fn = func(ctx context.Context, observation Observation) (SaveObservationResult, error) {
			saveCtxErr = ctx.Err()
			return queryTestDecideRecorder(base)(ctx, observation)
		}

		got, err := deps.service(context.Background()).Query(clientCtx, queryTestRequestID, queryTestInputByID())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if got.Order.Result != ResultSucceeded {
			t.Fatalf("expected the Mock answer to be saved")
		}
		queryTestAssertNoIssues(t, got.Issues)
		if saveCtxErr != nil {
			t.Fatalf("finish save must not inherit client cancellation")
		}
	})

	t.Run("lifecycle_cancel_during_lookup_cancels_inflight_operation", func(t *testing.T) {
		base := queryTestUnknownOrder()
		deps := queryTestValidDeps(base)
		lifecycle, cancelLifecycle := context.WithCancel(context.Background())
		defer cancelLifecycle()
		clientCtx, cancelClient := context.WithCancel(context.Background())
		defer cancelClient()

		lookupStarted := make(chan struct{})
		lookupCtxDone := make(chan struct{})
		deps.lookup.byIDFn = func(ctx context.Context, clientID string, paymentOrderID PaymentOrderID) (PaymentOrder, bool, error) {
			close(lookupStarted)
			<-ctx.Done()
			close(lookupCtxDone)
			return PaymentOrder{}, false, ctx.Err()
		}

		done := queryTestStart(deps.service(lifecycle), clientCtx, queryTestRequestID, queryTestInputByID())
		queryTestWaitClosed(t, lookupStarted, "lookup start")
		cancelLifecycle()
		queryTestWaitClosed(t, lookupCtxDone, "lookup")
		got := queryTestAwait(t, done)
		if got.err == nil {
			t.Fatalf("expected lifecycle cancellation error")
		}
		if got.result.Order.ID != "" || len(got.result.Issues) != 0 {
			t.Fatalf("canceled lookup must not produce a result")
		}
		if deps.mock.calls != 0 || deps.recorder.calls != 0 {
			t.Fatalf("canceled lookup must not call Mock or save")
		}
	})

	t.Run("lifecycle_cancel_during_mock_cancels_inflight_operation", func(t *testing.T) {
		base := queryTestUnknownOrder()
		deps := queryTestValidDeps(base)
		lifecycle, cancelLifecycle := context.WithCancel(context.Background())
		defer cancelLifecycle()
		clientCtx, cancelClient := context.WithCancel(context.Background())
		defer cancelClient()

		mockStarted := make(chan struct{})
		mockCtxDone := make(chan struct{})
		deps.mock.fn = func(ctx context.Context, platformReferenceID PaymentOrderID) (MockTransaction, bool, error) {
			close(mockStarted)
			<-ctx.Done()
			close(mockCtxDone)
			return MockTransaction{}, false, ctx.Err()
		}
		deps.recorder.fn = func(ctx context.Context, observation Observation) (SaveObservationResult, error) {
			return SaveObservationResult{}, ctx.Err()
		}

		done := queryTestStart(deps.service(lifecycle), clientCtx, queryTestRequestID, queryTestInputByID())
		queryTestWaitClosed(t, mockStarted, "Mock start")
		cancelLifecycle()
		queryTestWaitClosed(t, mockCtxDone, "Mock")
		got := queryTestAwait(t, done)
		if got.err == nil {
			t.Fatalf("expected lifecycle cancellation error")
		}
		if got.result.Order.Result == ResultSucceeded || got.result.Order.Result == ResultFailed {
			t.Fatalf("canceled Mock must not produce a confirmation")
		}
		if deps.mock.calls != 1 {
			t.Fatalf("expected exactly one Mock query")
		}
		if deps.recorder.calls == 1 && deps.recorder.ctxErr == nil {
			t.Fatalf("finish save must not continue after lifecycle cancellation")
		}
	})

	t.Run("historical_replay_unknown_record_with_current_confirmed_is_accepted", func(t *testing.T) {
		for _, decision := range []HandlingDecision{DecisionKeepUnknown, DecisionRejectEvidence} {
			decision := decision
			t.Run(string(decision), func(t *testing.T) {
				base := queryTestUnknownOrder()
				current := queryTestConfirmedOrder(ResultSucceeded)
				deps := queryTestValidDeps(base)
				deps.mock.transaction = queryTestTransaction(base, ResultSucceeded)
				deps.mock.found = true
				deps.recorder.fn = func(_ context.Context, observation Observation) (SaveObservationResult, error) {
					return queryTestSavedReplayUnknownHistory(observation, current, decision), nil
				}

				got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
				if err != nil {
					t.Fatalf("expected replay acceptance")
				}
				if got.Order.Result != ResultSucceeded {
					t.Fatalf("expected the current confirmed result")
				}
				for _, issue := range got.Issues {
					if issue.Code == IssueCodeResultSaveError || issue.Code == IssueCodeTraceSaveError {
						t.Fatalf("replay must not report a save error")
					}
				}
				if deps.lookup.byIDCalls != 1 {
					t.Fatalf("replay must not perform an extra lookup")
				}
				if deps.mock.calls != 1 || deps.recorder.calls != 1 {
					t.Fatalf("expected one Mock query and one save")
				}
			})
		}
	})

	t.Run("confirmed_history_cannot_flip", func(t *testing.T) {
		base := queryTestUnknownOrder()
		current := queryTestConfirmedOrder(ResultFailed)
		deps := queryTestValidDeps(base)
		deps.mock.transaction = queryTestTransaction(base, ResultSucceeded)
		deps.mock.found = true
		deps.recorder.fn = func(_ context.Context, observation Observation) (SaveObservationResult, error) {
			saved := queryTestSavedAccepted(observation, queryTestConfirmedOrder(ResultSucceeded), ResultSucceeded)
			saved.Order = current
			saved.Record.ResultBefore = ResultUnknown
			saved.Record.ResultAfter = ResultSucceeded
			return saved, nil
		}

		got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByID())
		if err == nil {
			t.Fatalf("expected the flipped confirmation to be rejected")
		}
		if got.Order.Result == ResultSucceeded || got.Order.Result == ResultFailed {
			t.Fatalf("a rejected save must not return a new confirmation")
		}
	})

	t.Run("no_confirmed_query_calls_mock", func(t *testing.T) {
		order := queryTestConfirmedOrder(ResultSucceeded)
		deps := queryTestValidDeps(order)
		deps.mock.transaction = queryTestTransaction(order, ResultFailed)
		deps.mock.found = true

		got, err := deps.service(context.Background()).Query(context.Background(), queryTestRequestID, queryTestInputByBusiness())
		if err != nil {
			t.Fatalf("expected nil error")
		}
		if got.Order.Result != ResultSucceeded {
			t.Fatalf("confirmed query must not be changed by Mock")
		}
		queryTestAssertNoIssues(t, got.Issues)
		if deps.mock.calls != 0 || deps.recorder.calls != 0 {
			t.Fatalf("confirmed query must not call Mock or save")
		}
	})
}
