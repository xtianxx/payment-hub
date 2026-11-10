package app

import (
	"context"
	"reflect"

	"github.com/jackc/pgx/v5/pgxpool"

	"payment-hub/internal/httpapi"
	"payment-hub/internal/mock"
	"payment-hub/internal/payment"
	"payment-hub/internal/postgres"
)

// businessPoolProvider is the private seam used by the production business
// factory to reuse the single runtime pool. Only the production database
// wrapper implements it; old test fakes are not forced to grow this method.
type businessPoolProvider interface {
	BusinessPool() *pgxpool.Pool
}

// businessPool resolves the production pool from the already-open runtime
// database. A missing provider, a nil provider result, or a nil database is a
// startup failure and must never fall back to a readyz-only runtime.
func businessPool(db runtimeDatabase) (*pgxpool.Pool, error) {
	if db == nil {
		return nil, ErrStartup
	}
	provider, ok := db.(businessPoolProvider)
	if !ok || provider == nil {
		return nil, ErrStartup
	}
	pool := provider.BusinessPool()
	if pool == nil {
		return nil, ErrStartup
	}
	return pool, nil
}

// validBusinessPort rejects nil and typed-nil interface values before the
// runtime mounts business routes. A typed-nil port would otherwise pass a plain
// interface nil check and panic or silently fail inside a handler.
func validBusinessPort(port any) bool {
	if port == nil {
		return false
	}
	value := reflect.ValueOf(port)
	switch value.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Ptr, reflect.Slice:
		return !value.IsNil()
	default:
		return true
	}
}

// productionNewBusiness constructs the real business graph on the one runtime
// pool. The store, Mock channel, submitter, and query service all share the
// application work context so shutdown can drain them before sealing the
// observer and closing the pool. When test mode is disabled, the nil TestMode
// receiver returns nil hooks, which the store and channel normalize safely.
func productionNewBusiness(ctx context.Context, db runtimeDatabase, testMode *TestMode) (httpapi.PaymentSubmitter, httpapi.PaymentQuerier, error) {
	if ctx == nil || ctx.Err() != nil {
		return nil, nil, ErrStartup
	}
	pool, err := businessPool(db)
	if err != nil {
		return nil, nil, err
	}
	store := postgres.NewStore(pool, testMode.PostgresHooks())
	channel := mock.NewChannel(pool, testMode.MockHooks())
	submitter := payment.NewSubmitter(store, store, store, channel, ctx)
	query := payment.NewQueryService(store, store, channel, ctx)
	return submitter, query, nil
}

// gatedPaymentService is the app-private narrow port wrapper that admits every
// business call through the existing readiness gate. The HTTP layer receives
// only the httpapi port interfaces and never sees this wrapper or the gate.
// The gate is held for the complete service call, including the service's own
// bounded finish window, so shutdown cannot seal the observer or close the
// database while a business request is still finishing.
type gatedPaymentService struct {
	gate   *readyGate
	submit httpapi.PaymentSubmitter
	query  httpapi.PaymentQuerier
}

func newGatedPaymentService(gate *readyGate, submit httpapi.PaymentSubmitter, query httpapi.PaymentQuerier) (*gatedPaymentService, error) {
	if gate == nil || !validBusinessPort(submit) || !validBusinessPort(query) {
		return nil, ErrStartup
	}
	return &gatedPaymentService{gate: gate, submit: submit, query: query}, nil
}

func (service *gatedPaymentService) Submit(ctx context.Context, requestID payment.RequestID, input payment.RegisterOrderInput) (payment.SubmitResult, error) {
	if service == nil || service.gate == nil || !validBusinessPort(service.submit) {
		return payment.SubmitResult{}, payment.ErrUnavailable
	}
	done, admitted := service.gate.begin()
	if !admitted {
		return payment.SubmitResult{}, payment.ErrUnavailable
	}
	defer done()
	return service.submit.Submit(ctx, requestID, input)
}

func (service *gatedPaymentService) Query(ctx context.Context, requestID payment.RequestID, input payment.QueryInput) (payment.QueryResult, error) {
	if service == nil || service.gate == nil || !validBusinessPort(service.query) {
		return payment.QueryResult{}, payment.ErrUnavailable
	}
	done, admitted := service.gate.begin()
	if !admitted {
		return payment.QueryResult{}, payment.ErrUnavailable
	}
	defer done()
	return service.query.Query(ctx, requestID, input)
}
