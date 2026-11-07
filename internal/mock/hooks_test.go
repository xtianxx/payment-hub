package mock

// These tests freeze the T019 consumer-side mock Control adapter. They never
// execute SQL, never construct a business repository, and never print a raw
// control error or payload.

import (
	"context"
	"errors"
	"strings"
	"sync/atomic"
	"testing"

	"payment-hub/internal/payment"
)

const mockHooksTestCanary = "FAKE_T019_MOCK_HOOKS_CANARY"

type mockHooksTestControl struct {
	calls  int
	method string
	ctx    context.Context
	order  payment.PaymentOrderID
	err    error
}

func (c *mockHooksTestControl) PaymentEntry(ctx context.Context, order payment.PaymentOrderID) error {
	c.calls++
	c.method = "payment_entry"
	c.ctx = ctx
	c.order = order
	return c.err
}

func (c *mockHooksTestControl) QueryEntry(ctx context.Context, order payment.PaymentOrderID) error {
	c.calls++
	c.method = "query_entry"
	c.ctx = ctx
	c.order = order
	return c.err
}

func (c *mockHooksTestControl) Committed(ctx context.Context, order payment.PaymentOrderID) error {
	c.calls++
	c.method = "committed"
	c.ctx = ctx
	c.order = order
	return c.err
}

var mockHooksTestTypedNilCalled atomic.Bool

type mockHooksTestTypedNilControl struct{}

func (*mockHooksTestTypedNilControl) PaymentEntry(context.Context, payment.PaymentOrderID) error {
	mockHooksTestTypedNilCalled.Store(true)
	return nil
}

func (*mockHooksTestTypedNilControl) QueryEntry(context.Context, payment.PaymentOrderID) error {
	mockHooksTestTypedNilCalled.Store(true)
	return nil
}

func (*mockHooksTestTypedNilControl) Committed(context.Context, payment.PaymentOrderID) error {
	mockHooksTestTypedNilCalled.Store(true)
	return nil
}

func mockHooksTestRequireExact(t *testing.T, err, want error) {
	t.Helper()
	if err != want {
		t.Fatal("hooks did not return the fixed consumer sentinel without wrapping")
	}
	if err != nil && strings.Contains(err.Error(), mockHooksTestCanary) {
		t.Fatal("hooks echoed a raw control error")
	}
}

func TestHooksNilAndZeroValueAreNoOps(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	order := payment.PaymentOrderID("")

	var nilHooks *Hooks
	if err := nilHooks.PaymentEntry(ctx, order); err != nil {
		t.Fatal("nil hooks payment entry was not a no-op")
	}
	if err := nilHooks.QueryEntry(ctx, order); err != nil {
		t.Fatal("nil hooks query entry was not a no-op")
	}
	if err := nilHooks.Committed(ctx, order); err != nil {
		t.Fatal("nil hooks committed was not a no-op")
	}

	zero := &Hooks{}
	if err := zero.PaymentEntry(ctx, order); err != nil {
		t.Fatal("zero hooks payment entry was not a no-op")
	}
	if err := zero.QueryEntry(ctx, order); err != nil {
		t.Fatal("zero hooks query entry was not a no-op")
	}
	if err := zero.Committed(ctx, order); err != nil {
		t.Fatal("zero hooks committed was not a no-op")
	}

	fromNil := NewHooks(nil)
	if fromNil == nil {
		t.Fatal("NewHooks(nil) returned nil")
	}
	if err := fromNil.PaymentEntry(ctx, order); err != nil {
		t.Fatal("hooks built with a nil control were not a no-op")
	}
}

func TestHooksForwardsEnabledCalls(t *testing.T) {
	control := &mockHooksTestControl{}
	hooks := NewHooks(control)
	if hooks == nil {
		t.Fatal("NewHooks returned nil")
	}
	ctx := context.WithValue(context.Background(), struct{}{}, "fake")
	order := payment.PaymentOrderID("FAKE_MOCK_ORDER")

	cases := []struct {
		name       string
		wantMethod string
		call       func(context.Context, payment.PaymentOrderID) error
	}{
		{name: "payment entry", wantMethod: "payment_entry", call: hooks.PaymentEntry},
		{name: "query entry", wantMethod: "query_entry", call: hooks.QueryEntry},
		{name: "committed", wantMethod: "committed", call: hooks.Committed},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			control.calls = 0
			control.method = ""
			control.ctx = nil
			control.order = ""
			if err := tc.call(ctx, order); err != nil {
				t.Fatal("enabled hook call failed")
			}
			if control.calls != 1 {
				t.Fatal("enabled hook did not forward exactly one call")
			}
			if control.method != tc.wantMethod {
				t.Fatal("enabled hook forwarded to the wrong control method")
			}
			if control.ctx != ctx {
				t.Fatal("enabled hook did not forward the original context")
			}
			if control.order != order {
				t.Fatal("enabled hook did not forward the exact order ID")
			}
		})
	}
}

func TestHooksNormalizesErrorsAndReplyHidden(t *testing.T) {
	control := &mockHooksTestControl{}
	hooks := NewHooks(control)
	ctx := context.Background()
	order := payment.PaymentOrderID("FAKE_MOCK_ORDER")

	cases := []struct {
		name string
		call func(context.Context, payment.PaymentOrderID) error
	}{
		{name: "payment entry", call: hooks.PaymentEntry},
		{name: "query entry", call: hooks.QueryEntry},
		{name: "committed", call: hooks.Committed},
	}
	for _, tc := range cases {
		t.Run(tc.name+" unknown error", func(t *testing.T) {
			control.err = errors.New(mockHooksTestCanary)
			mockHooksTestRequireExact(t, tc.call(ctx, order), ErrControlFault)
		})
		t.Run(tc.name+" control fault", func(t *testing.T) {
			control.err = ErrControlFault
			mockHooksTestRequireExact(t, tc.call(ctx, order), ErrControlFault)
		})
		t.Run(tc.name+" reply hidden", func(t *testing.T) {
			control.err = ErrReplyHidden
			if tc.name == "committed" {
				mockHooksTestRequireExact(t, tc.call(ctx, order), ErrReplyHidden)
				return
			}
			mockHooksTestRequireExact(t, tc.call(ctx, order), ErrControlFault)
		})
	}
}

func TestHooksTypedNilControlIsNoOp(t *testing.T) {
	mockHooksTestTypedNilCalled.Store(false)
	hooks := NewHooks((*mockHooksTestTypedNilControl)(nil))
	if hooks == nil {
		t.Fatal("NewHooks with a typed-nil control returned nil")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	order := payment.PaymentOrderID("")
	if err := hooks.PaymentEntry(ctx, order); err != nil {
		t.Fatal("typed-nil control payment entry was not a no-op")
	}
	if err := hooks.QueryEntry(ctx, order); err != nil {
		t.Fatal("typed-nil control query entry was not a no-op")
	}
	if err := hooks.Committed(ctx, order); err != nil {
		t.Fatal("typed-nil control committed was not a no-op")
	}
	if mockHooksTestTypedNilCalled.Load() {
		t.Fatal("hooks invoked a typed-nil control")
	}
}
