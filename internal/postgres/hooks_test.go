package postgres

// These tests freeze the T019 consumer-side postgres Control adapter. They
// never execute SQL, never construct a transaction wrapper, and never print a
// raw control error, SQL, or payload.

import (
	"context"
	"errors"
	"strings"
	"sync/atomic"
	"testing"

	"payment-hub/internal/payment"
)

const postgresHooksTestCanary = "FAKE_T019_POSTGRES_HOOKS_CANARY"

type postgresHooksTestControl struct {
	calls  int
	method string
	ctx    context.Context
	order  payment.PaymentOrderID
	err    error
}

func (c *postgresHooksTestControl) Registered(ctx context.Context, order payment.PaymentOrderID) error {
	c.calls++
	c.method = "registered"
	c.ctx = ctx
	c.order = order
	return c.err
}

func (c *postgresHooksTestControl) SavingResult(ctx context.Context, order payment.PaymentOrderID) error {
	c.calls++
	c.method = "saving_result"
	c.ctx = ctx
	c.order = order
	return c.err
}

func (c *postgresHooksTestControl) ResultCommitted(ctx context.Context, order payment.PaymentOrderID) error {
	c.calls++
	c.method = "result_committed"
	c.ctx = ctx
	c.order = order
	return c.err
}

var postgresHooksTestTypedNilCalled atomic.Bool

type postgresHooksTestTypedNilControl struct{}

func (*postgresHooksTestTypedNilControl) Registered(context.Context, payment.PaymentOrderID) error {
	postgresHooksTestTypedNilCalled.Store(true)
	return nil
}

func (*postgresHooksTestTypedNilControl) SavingResult(context.Context, payment.PaymentOrderID) error {
	postgresHooksTestTypedNilCalled.Store(true)
	return nil
}

func (*postgresHooksTestTypedNilControl) ResultCommitted(context.Context, payment.PaymentOrderID) error {
	postgresHooksTestTypedNilCalled.Store(true)
	return nil
}

func postgresHooksTestRequireExact(t *testing.T, err, want error) {
	t.Helper()
	if err != want {
		t.Fatal("hooks did not return the fixed consumer sentinel without wrapping")
	}
	if err != nil && strings.Contains(err.Error(), postgresHooksTestCanary) {
		t.Fatal("hooks echoed a raw control error")
	}
}

func TestHooksNilAndZeroValueAreNoOps(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	order := payment.PaymentOrderID("")

	var nilHooks *Hooks
	if err := nilHooks.Registered(ctx, order); err != nil {
		t.Fatal("nil hooks registered was not a no-op")
	}
	if err := nilHooks.SavingResult(ctx, order); err != nil {
		t.Fatal("nil hooks saving result was not a no-op")
	}
	if err := nilHooks.ResultCommitted(ctx, order); err != nil {
		t.Fatal("nil hooks result committed was not a no-op")
	}

	zero := &Hooks{}
	if err := zero.Registered(ctx, order); err != nil {
		t.Fatal("zero hooks registered was not a no-op")
	}
	if err := zero.SavingResult(ctx, order); err != nil {
		t.Fatal("zero hooks saving result was not a no-op")
	}
	if err := zero.ResultCommitted(ctx, order); err != nil {
		t.Fatal("zero hooks result committed was not a no-op")
	}

	fromNil := NewHooks(nil)
	if fromNil == nil {
		t.Fatal("NewHooks(nil) returned nil")
	}
	if err := fromNil.Registered(ctx, order); err != nil {
		t.Fatal("hooks built with a nil control were not a no-op")
	}
}

func TestHooksForwardsEnabledCalls(t *testing.T) {
	control := &postgresHooksTestControl{}
	hooks := NewHooks(control)
	if hooks == nil {
		t.Fatal("NewHooks returned nil")
	}
	ctx := context.WithValue(context.Background(), struct{}{}, "fake")
	order := payment.PaymentOrderID("FAKE_POSTGRES_ORDER")

	cases := []struct {
		name       string
		wantMethod string
		call       func(context.Context, payment.PaymentOrderID) error
	}{
		{name: "registered", wantMethod: "registered", call: hooks.Registered},
		{name: "saving result", wantMethod: "saving_result", call: hooks.SavingResult},
		{name: "result committed", wantMethod: "result_committed", call: hooks.ResultCommitted},
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
	control := &postgresHooksTestControl{}
	hooks := NewHooks(control)
	ctx := context.Background()
	order := payment.PaymentOrderID("FAKE_POSTGRES_ORDER")

	cases := []struct {
		name string
		call func(context.Context, payment.PaymentOrderID) error
	}{
		{name: "registered", call: hooks.Registered},
		{name: "saving result", call: hooks.SavingResult},
		{name: "result committed", call: hooks.ResultCommitted},
	}
	for _, tc := range cases {
		t.Run(tc.name+" unknown error", func(t *testing.T) {
			control.err = errors.New(postgresHooksTestCanary)
			postgresHooksTestRequireExact(t, tc.call(ctx, order), ErrControlFault)
		})
		t.Run(tc.name+" control fault", func(t *testing.T) {
			control.err = ErrControlFault
			postgresHooksTestRequireExact(t, tc.call(ctx, order), ErrControlFault)
		})
		t.Run(tc.name+" reply hidden", func(t *testing.T) {
			control.err = ErrReplyHidden
			// Registered and ResultCommitted are both postcommit points; only
			// SavingResult is a precommit point that must not hide a reply.
			if tc.name == "registered" || tc.name == "result committed" {
				postgresHooksTestRequireExact(t, tc.call(ctx, order), ErrReplyHidden)
				return
			}
			postgresHooksTestRequireExact(t, tc.call(ctx, order), ErrControlFault)
		})
	}
}

func TestHooksTypedNilControlIsNoOp(t *testing.T) {
	postgresHooksTestTypedNilCalled.Store(false)
	hooks := NewHooks((*postgresHooksTestTypedNilControl)(nil))
	if hooks == nil {
		t.Fatal("NewHooks with a typed-nil control returned nil")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	order := payment.PaymentOrderID("")
	if err := hooks.Registered(ctx, order); err != nil {
		t.Fatal("typed-nil control registered was not a no-op")
	}
	if err := hooks.SavingResult(ctx, order); err != nil {
		t.Fatal("typed-nil control saving result was not a no-op")
	}
	if err := hooks.ResultCommitted(ctx, order); err != nil {
		t.Fatal("typed-nil control result committed was not a no-op")
	}
	if postgresHooksTestTypedNilCalled.Load() {
		t.Fatal("hooks invoked a typed-nil control")
	}
}
