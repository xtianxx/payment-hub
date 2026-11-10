package payment

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
)

// idempotencySequenceRegistrar returns one queued result per call. It lets a
// test replay the same intent while the platform read returns a later current
// result than the first response observed.
type idempotencySequenceRegistrar struct {
	calls   int
	results []RegisterOrderResult
	errs    []error
}

func (r *idempotencySequenceRegistrar) RegisterOrder(context.Context, RegisterOrderInput) (RegisterOrderResult, error) {
	index := r.calls
	r.calls++
	var result RegisterOrderResult
	var err error
	if index < len(r.results) {
		result = r.results[index]
	}
	if index < len(r.errs) {
		err = r.errs[index]
	}
	return result, err
}

// TestPaymentIdempotency is the frozen US2 replay root. The same intent and
// parameters must never start a second payment for UNKNOWN, SUCCEEDED, or
// FAILED, and a replay must return the currently read order rather than the
// first HTTP response snapshot.
func TestPaymentIdempotency(t *testing.T) {
	cases := []struct {
		name         string
		payErr       error
		transaction  Result
		secondOrder  PaymentOrder
		wantFirst    Result
		wantSecond   Result
		wantFirstIss []string
	}{
		{
			name:        "succeeded_replay",
			transaction: ResultSucceeded,
			secondOrder: submitTestOrder(ResultSucceeded, 100),
			wantFirst:   ResultSucceeded,
			wantSecond:  ResultSucceeded,
		},
		{
			name:        "failed_replay",
			transaction: ResultFailed,
			secondOrder: submitTestOrder(ResultFailed, 100),
			wantFirst:   ResultFailed,
			wantSecond:  ResultFailed,
		},
		{
			name:         "unknown_replay",
			payErr:       errors.New("mock timeout secret"),
			secondOrder:  submitTestOrder(ResultUnknown, 100),
			wantFirst:    ResultUnknown,
			wantSecond:   ResultUnknown,
			wantFirstIss: []string{IssueCodeMockResultUnconfirmed},
		},
		{
			name:         "current_result_replaces_first_unknown",
			payErr:       errors.New("mock timeout secret"),
			secondOrder:  submitTestOrder(ResultSucceeded, 100),
			wantFirst:    ResultUnknown,
			wantSecond:   ResultSucceeded,
			wantFirstIss: []string{IssueCodeMockResultUnconfirmed},
		},
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			base := submitTestOrder(ResultUnknown, 100)
			registrar := &idempotencySequenceRegistrar{results: []RegisterOrderResult{
				{Order: base, Outcome: RegisterOutcomeCreated},
				{Order: tc.secondOrder, Outcome: RegisterOutcomeReused},
			}}
			lookup := &submitTestLookup{}
			recorder := &submitTestRecorder{save: submitTestDecideRecorder(base)}
			payer := &submitTestPayer{err: tc.payErr}
			if tc.transaction != "" {
				payer.transaction = submitTestTransaction(base, tc.transaction)
			}
			submitter := NewSubmitter(registrar, lookup, recorder, payer, context.Background())

			first, err := submitter.Submit(context.Background(), submitTestRequestID, submitTestInput())
			if err != nil {
				t.Fatalf("first submission returned an error")
			}
			if !first.Created {
				t.Fatalf("first submission must create the intent")
			}
			if first.Order.Result != tc.wantFirst {
				t.Fatalf("first result = %q, want %q", first.Order.Result, tc.wantFirst)
			}
			submitTestAssertIssues(t, first.Issues, tc.wantFirstIss...)

			second, err := submitter.Submit(context.Background(), submitTestRequestID, submitTestInput())
			if err != nil {
				t.Fatalf("replay returned an error")
			}
			if second.Created {
				t.Fatalf("replay must not create or recreate the intent")
			}
			if second.Order.Result != tc.wantSecond {
				t.Fatalf("replay result = %q, want current result %q", second.Order.Result, tc.wantSecond)
			}
			submitTestAssertNoIssues(t, second.Issues)

			if registrar.calls != 2 {
				t.Fatalf("registration calls = %d, want 2", registrar.calls)
			}
			if payer.calls != 1 {
				t.Fatalf("payment calls = %d, want exactly 1", payer.calls)
			}
			if recorder.calls != 1 {
				t.Fatalf("save calls = %d, want exactly 1", recorder.calls)
			}
			if lookup.byBusinessCalls != 0 || lookup.byIDCalls != 0 {
				t.Fatalf("replay must not perform an extra order lookup")
			}
		})
	}
}

func conflictPriorityAssertNoSideEffects(t *testing.T, deps *submitTestDeps, wantBusinessLookups int) {
	t.Helper()
	if deps.reg.calls != 0 {
		t.Fatalf("business-value rejection must not register an order")
	}
	if deps.payer.calls != 0 {
		t.Fatalf("business-value rejection must not call the payer")
	}
	if deps.recorder.calls != 0 {
		t.Fatalf("business-value rejection must not save an observation")
	}
	if deps.lookup.byIDCalls != 0 {
		t.Fatalf("business-value rejection must not look up by ID")
	}
	if deps.lookup.byBusinessCalls != wantBusinessLookups {
		t.Fatalf("business lookup calls = %d, want %d", deps.lookup.byBusinessCalls, wantBusinessLookups)
	}
	if wantBusinessLookups > 0 && deps.lookup.byBusinessDeadline.IsZero() {
		t.Fatalf("business lookup must use a bounded context")
	}
}

// TestPaymentSubmitConflictPriority is the frozen US2 rejection-priority root.
// Structure and identity failures are rejected without any lookup. Business
// values that are safe to compare get exactly one ordinary business-key lookup:
// a different existing fact is a conflict, a reliable miss is invalid input,
// and lookup failures never become conflicts or reuse. A definite non-business
// registration rejection never recovers a same-parameter row into a 200.
func TestPaymentSubmitConflictPriority(t *testing.T) {
	existing := submitTestOrder(ResultUnknown, 100)

	t.Run("safe_input_rejected_before_business_lookup", func(t *testing.T) {
		valid := submitTestInput()
		cases := []struct {
			name      string
			mutate    func(*RegisterOrderInput)
			requestID RequestID
		}{
			{
				name:      "invalid_client_id",
				mutate:    func(in *RegisterOrderInput) { in.ClientID = "" },
				requestID: submitTestRequestID,
			},
			{
				name:      "invalid_business_order_no",
				mutate:    func(in *RegisterOrderInput) { in.BusinessOrderNo = "bad order" },
				requestID: submitTestRequestID,
			},
			{
				name:      "invalid_request_id",
				requestID: RequestID("not-a-uuid"),
			},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				deps := submitTestValidDeps(existing)
				input := valid
				if tc.mutate != nil {
					tc.mutate(&input)
				}
				got, err := deps.submitter(context.Background()).Submit(context.Background(), tc.requestID, input)
				if !errors.Is(err, ErrInvalidInput) {
					t.Fatalf("expected invalid input")
				}
				if got.Order.ID != "" || got.Created || len(got.Issues) != 0 {
					t.Fatalf("safe input rejection must not produce a result")
				}
				deps.assertNoCalls(t)
			})
		}
	})

	t.Run("existing_different_business_values_conflict", func(t *testing.T) {
		cases := []struct {
			name     string
			amount   int64
			currency string
		}{
			{name: "amount_below_range", amount: 0, currency: "CNY"},
			{name: "amount_above_range", amount: 1_000_001, currency: "CNY"},
			{name: "currency_not_cny", amount: 100, currency: "USD"},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				deps := submitTestValidDeps(existing)
				deps.lookup.byBusinessOrder = existing
				deps.lookup.byBusinessFound = true
				input := submitTestInput()
				input.Amount = tc.amount
				input.Currency = tc.currency

				got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, input)
				if !errors.Is(err, ErrConflict) {
					t.Fatalf("expected conflict from the reliable existing fact")
				}
				if got.Created {
					t.Fatalf("conflict must not be created")
				}
				if got.Order.ID != existing.ID || got.Order.Amount != existing.Amount || got.Order.Currency != existing.Currency {
					t.Fatalf("conflict must return the reliable existing fact")
				}
				conflictPriorityAssertNoSideEffects(t, deps, 1)
			})
		}
	})

	t.Run("not_found_is_invalid_input", func(t *testing.T) {
		deps := submitTestValidDeps(existing)
		deps.lookup.byBusinessFound = false
		input := submitTestInput()
		input.Amount = 0

		got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, input)
		if !errors.Is(err, ErrInvalidInput) {
			t.Fatalf("reliable miss must be invalid input")
		}
		if got.Order.ID != "" || got.Created || len(got.Issues) != 0 {
			t.Fatalf("reliable miss must not produce a result")
		}
		conflictPriorityAssertNoSideEffects(t, deps, 1)
	})

	t.Run("lookup_failure_classification", func(t *testing.T) {
		cases := []struct {
			name      string
			lookupErr error
			want      error
		}{
			{name: "dependency_unavailable", lookupErr: ErrUnavailable, want: ErrUnavailable},
			{name: "timeout", lookupErr: context.DeadlineExceeded, want: ErrUnavailable},
			{name: "unexpected_internal", lookupErr: errors.New("lookup secret"), want: ErrIndeterminate},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				deps := submitTestValidDeps(existing)
				// The lookup returns a plausible order alongside its error. That
				// order is not a fact and must never drive the decision.
				deps.lookup.byBusinessOrder = existing
				deps.lookup.byBusinessFound = true
				deps.lookup.byBusinessErr = tc.lookupErr
				input := submitTestInput()
				input.Amount = 0

				got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, input)
				if !errors.Is(err, tc.want) {
					t.Fatalf("lookup failure classified as %v, want %v", err, tc.want)
				}
				if errors.Is(err, ErrConflict) {
					t.Fatalf("lookup failure must not become a business conflict")
				}
				if strings.Contains(err.Error(), "secret") {
					t.Fatalf("lookup failure leaked a raw error")
				}
				if got.Order.ID != "" || got.Created || len(got.Issues) != 0 {
					t.Fatalf("lookup failure must not produce a result")
				}
				conflictPriorityAssertNoSideEffects(t, deps, 1)
			})
		}
	})

	t.Run("valid_values_follow_registration_path_without_prelookup", func(t *testing.T) {
		base := submitTestOrder(ResultUnknown, 100)
		deps := submitTestValidDeps(base)

		got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
		if err != nil {
			t.Fatalf("valid submission returned an error")
		}
		if !got.Created || got.Order.Result != ResultSucceeded {
			t.Fatalf("valid submission must follow the original created path")
		}
		if deps.reg.calls != 1 || deps.payer.calls != 1 || deps.recorder.calls != 1 {
			t.Fatalf("valid submission must register, pay, and save exactly once")
		}
		if deps.lookup.byBusinessCalls != 0 || deps.lookup.byIDCalls != 0 {
			t.Fatalf("valid submission must not add a pre-registration lookup")
		}
	})

	t.Run("registration_rejection_never_recovers_existing_row", func(t *testing.T) {
		cases := []struct {
			name        string
			registerErr error
		}{
			{name: "direct_sentinel", registerErr: ErrRegistrationRejected},
			{name: "wrapped_sentinel", registerErr: fmt.Errorf("non-business unique collision: %w", ErrRegistrationRejected)},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				deps := submitTestValidDeps(submitTestOrder(ResultUnknown, 100))
				deps.reg.result = RegisterOrderResult{}
				deps.reg.err = tc.registerErr
				// A same-parameter row is deliberately visible. A definite
				// non-business registration rejection must not read it back and
				// turn it into reuse, a 200, or a payment opportunity.
				deps.lookup.byBusinessOrder = submitTestOrder(ResultSucceeded, 100)
				deps.lookup.byBusinessFound = true

				got, err := deps.submitter(context.Background()).Submit(context.Background(), submitTestRequestID, submitTestInput())
				if err != ErrRegistrationRejected {
					t.Fatalf("expected the sanitized registration-rejected sentinel, got %v", err)
				}
				if errors.Is(err, ErrConflict) || errors.Is(err, ErrInvalidInput) {
					t.Fatalf("registration rejection must not be reported as a business conflict or invalid input")
				}
				if got.Order.ID != "" || got.Created || len(got.Issues) != 0 {
					t.Fatalf("registration rejection must not return a reused order")
				}
				if deps.reg.calls != 1 {
					t.Fatalf("registration attempts = %d, want 1", deps.reg.calls)
				}
				if deps.lookup.byBusinessCalls != 0 || deps.lookup.byIDCalls != 0 {
					t.Fatalf("registration rejection must not recover through a lookup")
				}
				if deps.payer.calls != 0 || deps.recorder.calls != 0 {
					t.Fatalf("registration rejection must not pay or save")
				}
			})
		}
	})
}
