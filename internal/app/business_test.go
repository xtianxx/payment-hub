package app

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"payment-hub/internal/httpapi"
	"payment-hub/internal/payment"
)

const (
	businessTestOrderID     = "123e4567-e89b-42d3-a456-426614174000"
	businessTestBusinessNo  = "biz-runtime-1001"
	businessTestBody        = `{"business_order_no":"biz-runtime-1001","amount":100,"currency":"CNY"}`
	businessTestPostPath    = "/api/v1/payments"
	businessTestPaymentPath = "/api/v1/payments/123e4567-e89b-42d3-a456-426614174000"
	businessTestQueryPath   = "/api/v1/payments?business_order_no=biz-runtime-1001"
)

func businessTestOrder(result payment.Result) payment.PaymentOrder {
	order := payment.PaymentOrder{
		ID:              businessTestOrderID,
		ClientID:        lifecycleTestClientID,
		BusinessOrderNo: businessTestBusinessNo,
		Amount:          100,
		Currency:        "CNY",
		Result:          result,
		RegisteredAt:    time.Date(2026, 11, 9, 6, 0, 0, 0, time.UTC),
	}
	if result != payment.ResultUnknown {
		confirmedAt := time.Date(2026, 11, 9, 6, 0, 1, 0, time.UTC)
		mockID := payment.MockTransactionID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
		order.ConfirmedAt = &confirmedAt
		order.MockTransactionID = &mockID
	}
	return order
}

func businessTestNewRequest(method, target, body string, authenticated bool) *http.Request {
	var reader io.Reader
	if body != "" {
		reader = strings.NewReader(body)
	}
	request := httptest.NewRequest(method, target, reader)
	if body != "" {
		request.Header.Set("Content-Type", "application/json")
	}
	if authenticated {
		request.Header.Set("Authorization", "Bearer "+lifecycleTestAPIKey)
	}
	return request
}

func businessTestServe(handler http.Handler, request *http.Request) *httptest.ResponseRecorder {
	recorder := httptest.NewRecorder()
	handler.ServeHTTP(recorder, request)
	return recorder
}

type businessTestSubmitFake struct {
	mu          sync.Mutex
	calls       int
	requestID   payment.RequestID
	input       payment.RegisterOrderInput
	result      payment.SubmitResult
	err         error
	entered     chan struct{}
	release     chan struct{}
	enteredOnce sync.Once
	after       func()
}

func (fake *businessTestSubmitFake) Submit(ctx context.Context, requestID payment.RequestID, input payment.RegisterOrderInput) (payment.SubmitResult, error) {
	fake.mu.Lock()
	fake.calls++
	fake.requestID = requestID
	fake.input = input
	fake.mu.Unlock()
	if fake.entered != nil {
		fake.enteredOnce.Do(func() { close(fake.entered) })
	}
	if fake.release != nil {
		<-fake.release
	}
	if fake.after != nil {
		fake.after()
	}
	return fake.result, fake.err
}

func (fake *businessTestSubmitFake) snapshot() (int, payment.RequestID, payment.RegisterOrderInput) {
	fake.mu.Lock()
	defer fake.mu.Unlock()
	return fake.calls, fake.requestID, fake.input
}

type businessTestQueryFake struct {
	mu          sync.Mutex
	calls       int
	requestID   payment.RequestID
	input       payment.QueryInput
	result      payment.QueryResult
	err         error
	entered     chan struct{}
	release     chan struct{}
	enteredOnce sync.Once
	after       func()
}

func (fake *businessTestQueryFake) Query(ctx context.Context, requestID payment.RequestID, input payment.QueryInput) (payment.QueryResult, error) {
	fake.mu.Lock()
	fake.calls++
	fake.requestID = requestID
	fake.input = input
	fake.mu.Unlock()
	if fake.entered != nil {
		fake.enteredOnce.Do(func() { close(fake.entered) })
	}
	if fake.release != nil {
		<-fake.release
	}
	if fake.after != nil {
		fake.after()
	}
	return fake.result, fake.err
}

func (fake *businessTestQueryFake) snapshot() (int, payment.RequestID, payment.QueryInput) {
	fake.mu.Lock()
	defer fake.mu.Unlock()
	return fake.calls, fake.requestID, fake.input
}

type businessTestNilSubmitter struct{}

func (*businessTestNilSubmitter) Submit(context.Context, payment.RequestID, payment.RegisterOrderInput) (payment.SubmitResult, error) {
	return payment.SubmitResult{}, payment.ErrUnavailable
}

type businessTestNilQuerier struct{}

func (*businessTestNilQuerier) Query(context.Context, payment.RequestID, payment.QueryInput) (payment.QueryResult, error) {
	return payment.QueryResult{}, payment.ErrUnavailable
}

type businessTestFactory struct {
	mu     sync.Mutex
	calls  int
	ctx    context.Context
	db     runtimeDatabase
	tm     *TestMode
	submit httpapi.PaymentSubmitter
	query  httpapi.PaymentQuerier
	err    error
	build  func(context.Context, runtimeDatabase, *TestMode) (httpapi.PaymentSubmitter, httpapi.PaymentQuerier, error)
}

func (factory *businessTestFactory) newBusiness(ctx context.Context, db runtimeDatabase, tm *TestMode) (httpapi.PaymentSubmitter, httpapi.PaymentQuerier, error) {
	factory.mu.Lock()
	factory.calls++
	factory.ctx = ctx
	factory.db = db
	factory.tm = tm
	factory.mu.Unlock()
	if factory.build != nil {
		return factory.build(ctx, db, tm)
	}
	return factory.submit, factory.query, factory.err
}

func (factory *businessTestFactory) snapshot() (int, context.Context, runtimeDatabase, *TestMode) {
	factory.mu.Lock()
	defer factory.mu.Unlock()
	return factory.calls, factory.ctx, factory.db, factory.tm
}

type businessTestPoolDB struct {
	pool *pgxpool.Pool
}

func (db *businessTestPoolDB) Ping(context.Context) error            { return nil }
func (db *businessTestPoolDB) CheckMigrations(context.Context) error { return nil }
func (db *businessTestPoolDB) Close()                                {}
func (db *businessTestPoolDB) BusinessPool() *pgxpool.Pool {
	if db == nil {
		return nil
	}
	return db.pool
}

type businessTestRuntime struct {
	ctx      context.Context
	cancel   context.CancelFunc
	result   <-chan error
	done     <-chan struct{}
	listener net.Listener
	server   *lifecycleTestServer
	captured *http.Server
	db       *lifecycleTestDB
	factory  *businessTestFactory
}

func businessTestStartRuntime(t *testing.T, db *lifecycleTestDB, factory *businessTestFactory, required bool, values map[string]string, limits runtimeLimits, server *lifecycleTestServer) *businessTestRuntime {
	t.Helper()
	listener := newLifecycleTestListener()
	var captured *http.Server
	deps := runtimeDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
		listen:       func(context.Context, string) (net.Listener, error) { return listener, nil },
		newServer: func(httpServer *http.Server) runtimeServer {
			captured = httpServer
			return server
		},
		newBusiness:      factory.newBusiness,
		businessRequired: required,
	}
	ctx, cancel := context.WithCancel(context.Background())
	result := make(chan error, 1)
	done := make(chan struct{})
	go func() {
		result <- runWith(ctx, lifecycleTestLookup(values), io.Discard, deps, limits)
		close(done)
	}()
	runtime := &businessTestRuntime{
		ctx:      ctx,
		cancel:   cancel,
		result:   result,
		done:     done,
		listener: listener,
		server:   server,
		db:       db,
		factory:  factory,
	}
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(2 * time.Second):
		}
		_ = listener.Close()
	})
	lifecycleTestWaitSignal(t, server.started, "runtime server did not start")
	runtime.captured = captured
	return runtime
}

func businessTestWaitSignal(t *testing.T, signal <-chan struct{}, message string) {
	t.Helper()
	select {
	case <-signal:
	case <-time.After(2 * time.Second):
		t.Fatal(message)
	}
}

func TestPaymentRuntimeWiring(t *testing.T) {
	t.Run("factory called once with shared database and work context", func(t *testing.T) {
		lifecycleTestClearPGEnv(t)
		db := newLifecycleTestDB()
		server := newLifecycleTestServer(&lifecycleTestOrder{})
		submit := &businessTestSubmitFake{result: payment.SubmitResult{Order: businessTestOrder(payment.ResultUnknown), Created: true}}
		query := &businessTestQueryFake{result: payment.QueryResult{Order: businessTestOrder(payment.ResultSucceeded)}}
		factory := &businessTestFactory{submit: submit, query: query}
		runtime := businessTestStartRuntime(t, db, factory, true, lifecycleTestBaseValues(), lifecycleTestLimits(), server)

		calls, factoryCtx, factoryDB, factoryTM := factory.snapshot()
		if calls != 1 {
			t.Fatalf("new business factory calls = %d, want 1", calls)
		}
		if factoryDB != runtimeDatabase(db) {
			t.Fatal("new business factory did not receive the opened runtime database")
		}
		if factoryTM != nil {
			t.Fatal("new business factory received an unexpected test mode")
		}
		if runtime.captured == nil {
			t.Fatal("runtime did not expose an http.Server to the test seam")
		}
		if runtime.captured.BaseContext(nil) != factoryCtx {
			t.Fatal("business factory did not receive the HTTP work context")
		}
		if factoryCtx.Err() != nil {
			t.Fatal("business factory received an already-canceled work context")
		}
		runtime.cancel()
		if err := lifecycleTestWaitError(t, runtime.result, 2*time.Second); err != nil {
			t.Fatal("runtime shutdown returned an error")
		}
		if factoryCtx.Err() == nil {
			t.Fatal("work context was not canceled after shutdown")
		}
	})

	t.Run("factory receives the opened test mode adapter", func(t *testing.T) {
		lifecycleTestClearPGEnv(t)
		observer, socket := testmodeTestStartObserver(t, 1)
		_ = observer
		db := newLifecycleTestDB()
		server := newLifecycleTestServer(&lifecycleTestOrder{})
		submit := &businessTestSubmitFake{result: payment.SubmitResult{Order: businessTestOrder(payment.ResultUnknown), Created: true}}
		query := &businessTestQueryFake{result: payment.QueryResult{Order: businessTestOrder(payment.ResultSucceeded)}}
		factory := &businessTestFactory{submit: submit, query: query}
		runtime := businessTestStartRuntime(t, db, factory, true, shutdownCleanupTestValues(socket), lifecycleTestLimits(), server)

		calls, _, _, testMode := factory.snapshot()
		if calls != 1 {
			t.Fatalf("new business factory calls = %d, want 1", calls)
		}
		if testMode == nil {
			t.Fatal("new business factory did not receive the opened test mode adapter")
		}
		if testMode.MockHooks() == nil || testMode.PostgresHooks() == nil {
			t.Fatal("new business factory received a test mode without its adapters")
		}
		runtime.cancel()
		if err := lifecycleTestWaitError(t, runtime.result, 2*time.Second); err != nil {
			t.Fatal("runtime shutdown returned an error")
		}
	})

	t.Run("config loaded once and used by business authentication", func(t *testing.T) {
		lifecycleTestClearPGEnv(t)
		values := lifecycleTestBaseValues()
		var countsMu sync.Mutex
		counts := map[string]int{}
		lookup := func(name string) (string, bool) {
			countsMu.Lock()
			counts[name]++
			countsMu.Unlock()
			value, ok := values[name]
			return value, ok
		}
		db := newLifecycleTestDB()
		server := newLifecycleTestServer(&lifecycleTestOrder{})
		submit := &businessTestSubmitFake{result: payment.SubmitResult{Order: businessTestOrder(payment.ResultUnknown), Created: true}}
		query := &businessTestQueryFake{result: payment.QueryResult{Order: businessTestOrder(payment.ResultSucceeded)}}
		factory := &businessTestFactory{submit: submit, query: query}
		listener := newLifecycleTestListener()
		var captured *http.Server
		deps := runtimeDeps{
			openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
			listen:       func(context.Context, string) (net.Listener, error) { return listener, nil },
			newServer: func(httpServer *http.Server) runtimeServer {
				captured = httpServer
				return server
			},
			newBusiness:      factory.newBusiness,
			businessRequired: true,
		}
		ctx, cancel := context.WithCancel(context.Background())
		result := make(chan error, 1)
		done := make(chan struct{})
		go func() {
			result <- runWith(ctx, lookup, io.Discard, deps, lifecycleTestLimits())
			close(done)
		}()
		t.Cleanup(func() {
			cancel()
			select {
			case <-done:
			case <-time.After(2 * time.Second):
			}
			_ = listener.Close()
		})
		lifecycleTestWaitSignal(t, server.started, "runtime server did not start")
		if calls, _, _, _ := factory.snapshot(); calls != 1 {
			t.Fatalf("new business factory calls = %d, want 1", calls)
		}
		if captured == nil || captured.Handler == nil {
			t.Fatal("runtime server handler was not captured")
		}

		readyz := businessTestServe(captured.Handler, businessTestNewRequest(http.MethodGet, "/readyz", "", false))
		if readyz.Code != http.StatusOK {
			t.Fatal("readyz was not independent of business authentication")
		}
		unauthorized := businessTestServe(captured.Handler, businessTestNewRequest(http.MethodPost, businessTestPostPath, businessTestBody, false))
		if unauthorized.Code != http.StatusUnauthorized {
			t.Fatal("business POST without authentication did not return 401")
		}
		if calls, _, _ := submit.snapshot(); calls != 0 {
			t.Fatal("unauthenticated business POST reached the submit port")
		}

		created := businessTestServe(captured.Handler, businessTestNewRequest(http.MethodPost, businessTestPostPath, businessTestBody, true))
		if created.Code != http.StatusCreated {
			t.Fatal("authenticated business POST did not return 201")
		}
		submitCalls, _, submitInput := submit.snapshot()
		if submitCalls != 1 || submitInput.ClientID != lifecycleTestClientID {
			t.Fatal("business POST did not use the single loaded client identity")
		}
		byID := businessTestServe(captured.Handler, businessTestNewRequest(http.MethodGet, businessTestPaymentPath, "", true))
		if byID.Code != http.StatusOK {
			t.Fatal("authenticated payment ID query did not return 200")
		}
		byBusinessNo := businessTestServe(captured.Handler, businessTestNewRequest(http.MethodGet, businessTestQueryPath, "", true))
		if byBusinessNo.Code != http.StatusOK {
			t.Fatal("authenticated business order query did not return 200")
		}
		queryCalls, _, queryInput := query.snapshot()
		if queryCalls != 2 || queryInput.ClientID != lifecycleTestClientID {
			t.Fatal("business queries did not use the single loaded client identity")
		}

		countsMu.Lock()
		clientReads := counts[lifecycleTestIdentityID]
		apiKeyReads := counts[lifecycleTestAPIKeyEnv]
		countsMu.Unlock()
		if clientReads != 1 || apiKeyReads != 1 {
			t.Fatalf("identity configuration reads = client %d, api key %d; want 1 each", clientReads, apiKeyReads)
		}
		cancel()
		if err := lifecycleTestWaitError(t, result, 2*time.Second); err != nil {
			t.Fatal("runtime shutdown returned an error")
		}
	})

	t.Run("required business factory failures stop startup", func(t *testing.T) {
		cases := []struct {
			name        string
			factory     *businessTestFactory
			allowConfig bool
		}{
			{name: "nil factory", factory: nil, allowConfig: true},
			{name: "factory error", factory: &businessTestFactory{err: errors.New("fake factory failure")}},
			{name: "typed nil submitter", factory: &businessTestFactory{submit: (*businessTestNilSubmitter)(nil), query: &businessTestQueryFake{}}},
			{name: "typed nil querier", factory: &businessTestFactory{submit: &businessTestSubmitFake{}, query: (*businessTestNilQuerier)(nil)}},
			{name: "pool unavailable", factory: &businessTestFactory{build: func(_ context.Context, db runtimeDatabase, _ *TestMode) (httpapi.PaymentSubmitter, httpapi.PaymentQuerier, error) {
				_, err := businessPool(db)
				return nil, nil, err
			}}},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				lifecycleTestClearPGEnv(t)
				db := newLifecycleTestDB()
				listener := newLifecycleTestListener()
				server := newLifecycleTestServer(&lifecycleTestOrder{})
				deps := runtimeDeps{
					openDatabase:     func(context.Context, string) (runtimeDatabase, error) { return db, nil },
					listen:           func(context.Context, string) (net.Listener, error) { return listener, nil },
					newServer:        func(*http.Server) runtimeServer { return server },
					businessRequired: true,
				}
				if tc.factory != nil {
					deps.newBusiness = tc.factory.newBusiness
				}
				ctx, cancel := context.WithCancel(context.Background())
				result := make(chan error, 1)
				go func() {
					result <- runWith(ctx, lifecycleTestLookup(lifecycleTestBaseValues()), io.Discard, deps, lifecycleTestLimits())
				}()
				var err error
				select {
				case err = <-result:
				case <-server.started:
					cancel()
					<-result
					t.Fatalf("%s: required business failure started a readyz-only runtime", tc.name)
				case <-time.After(2 * time.Second):
					cancel()
					<-result
					t.Fatalf("%s: required business failure did not stop startup", tc.name)
				}
				cancel()
				_ = listener.Close()
				if err == nil {
					t.Fatalf("%s: required business failure returned nil", tc.name)
				}
				if !errors.Is(err, ErrStartup) && !(tc.allowConfig && errors.Is(err, ErrRuntimeConfig)) {
					t.Fatalf("%s: required business failure did not return a startup failure", tc.name)
				}
				if errors.Is(err, ErrStartup) {
					if _, _, closeCalls, _ := db.snapshot(); closeCalls != 1 {
						t.Fatalf("%s: startup failure did not close the runtime database exactly once", tc.name)
					}
				}
				select {
				case <-server.started:
					t.Fatalf("%s: startup failure still served HTTP", tc.name)
				default:
				}
			})
		}
	})

	t.Run("old dependency literal keeps explicit readyz scope", func(t *testing.T) {
		lifecycleTestClearPGEnv(t)
		db := newLifecycleTestDB()
		listener := newLifecycleTestListener()
		server := newLifecycleTestServer(&lifecycleTestOrder{})
		deps := runtimeDeps{
			openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
			listen:       func(context.Context, string) (net.Listener, error) { return listener, nil },
			newServer:    func(*http.Server) runtimeServer { return server },
		}
		ctx, cancel := context.WithCancel(context.Background())
		result := make(chan error, 1)
		done := make(chan struct{})
		go func() {
			result <- runWith(ctx, lifecycleTestLookup(lifecycleTestBaseValues()), io.Discard, deps, lifecycleTestLimits())
			close(done)
		}()
		t.Cleanup(func() {
			cancel()
			select {
			case <-done:
			case <-time.After(2 * time.Second):
			}
			_ = listener.Close()
		})
		lifecycleTestWaitSignal(t, server.started, "runtime server did not start")
		cancel()
		if err := lifecycleTestWaitError(t, result, 2*time.Second); err != nil {
			t.Fatal("readyz-only runtime shutdown returned an error")
		}
	})

	t.Run("production pool seam reuses the runtime pool", func(t *testing.T) {
		pool := &pgxpool.Pool{}
		db := &businessTestPoolDB{pool: pool}
		got, err := businessPool(db)
		if err != nil || got != pool {
			t.Fatal("business pool seam did not return the same production pool")
		}
		if _, err := businessPool(newLifecycleTestDB()); !errors.Is(err, ErrStartup) {
			t.Fatal("business pool seam accepted a database without a business pool")
		}
		if _, err := businessPool(nil); !errors.Is(err, ErrStartup) {
			t.Fatal("business pool seam accepted a nil database")
		}
		if _, err := businessPool(&businessTestPoolDB{}); !errors.Is(err, ErrStartup) {
			t.Fatal("business pool seam accepted a nil production pool")
		}
		var typedNil *businessTestPoolDB
		if _, err := businessPool(typedNil); !errors.Is(err, ErrStartup) {
			t.Fatal("business pool seam accepted a typed nil provider")
		}
		runtimeDB := &pgxpoolRuntimeDatabase{pool: pool}
		got, err = businessPool(runtimeDB)
		if err != nil || got != pool {
			t.Fatal("production runtime database did not expose its same pool")
		}
	})

	t.Run("gated business ports hold admission until finish", func(t *testing.T) {
		gate := newReadyGate()
		submit := &businessTestSubmitFake{
			entered: make(chan struct{}),
			release: make(chan struct{}),
			result:  payment.SubmitResult{Order: businessTestOrder(payment.ResultUnknown), Created: true},
		}
		query := &businessTestQueryFake{result: payment.QueryResult{Order: businessTestOrder(payment.ResultSucceeded)}}
		service, err := newGatedPaymentService(gate, submit, query)
		if err != nil {
			t.Fatal("gated business service rejected valid ports")
		}
		done := make(chan error, 1)
		go func() {
			_, submitErr := service.Submit(context.Background(), payment.RequestID(payment.NewUUIDv4()), payment.RegisterOrderInput{
				ClientID:        lifecycleTestClientID,
				BusinessOrderNo: businessTestBusinessNo,
				Amount:          100,
				Currency:        "CNY",
			})
			done <- submitErr
		}()
		businessTestWaitSignal(t, submit.entered, "gated Submit did not reach the business port")
		activeCtx, cancelActive := context.WithTimeout(context.Background(), 20*time.Millisecond)
		if gate.wait(activeCtx) {
			cancelActive()
			t.Fatal("gate admission was not held while Submit was active")
		}
		cancelActive()
		close(submit.release)
		if err := <-done; err != nil {
			t.Fatal("gated Submit returned an unexpected error")
		}
		if calls, _, _ := submit.snapshot(); calls != 1 {
			t.Fatal("gated Submit did not call the business port exactly once")
		}
		releaseCtx, cancelRelease := context.WithTimeout(context.Background(), time.Second)
		if !gate.wait(releaseCtx) {
			cancelRelease()
			t.Fatal("gate admission was not released after Submit finished")
		}
		cancelRelease()

		gate = newReadyGate()
		query = &businessTestQueryFake{
			entered: make(chan struct{}),
			release: make(chan struct{}),
			result:  payment.QueryResult{Order: businessTestOrder(payment.ResultSucceeded)},
		}
		service, err = newGatedPaymentService(gate, submit, query)
		if err != nil {
			t.Fatal("gated business service rejected valid ports")
		}
		go func() {
			_, queryErr := service.Query(context.Background(), payment.RequestID(payment.NewUUIDv4()), payment.QueryInput{
				ClientID:        lifecycleTestClientID,
				BusinessOrderNo: businessTestBusinessNo,
			})
			done <- queryErr
		}()
		businessTestWaitSignal(t, query.entered, "gated Query did not reach the business port")
		activeCtx, cancelActive = context.WithTimeout(context.Background(), 20*time.Millisecond)
		if gate.wait(activeCtx) {
			cancelActive()
			t.Fatal("gate admission was not held while Query was active")
		}
		cancelActive()
		close(query.release)
		if err := <-done; err != nil {
			t.Fatal("gated Query returned an unexpected error")
		}
		if calls, _, _ := query.snapshot(); calls != 1 {
			t.Fatal("gated Query did not call the business port exactly once")
		}

		gate = newReadyGate()
		gate.close()
		submit = &businessTestSubmitFake{result: payment.SubmitResult{Order: businessTestOrder(payment.ResultUnknown)}}
		query = &businessTestQueryFake{result: payment.QueryResult{Order: businessTestOrder(payment.ResultUnknown)}}
		service, err = newGatedPaymentService(gate, submit, query)
		if err != nil {
			t.Fatal("gated business service rejected valid ports")
		}
		if _, err := service.Submit(context.Background(), payment.RequestID(payment.NewUUIDv4()), payment.RegisterOrderInput{}); !errors.Is(err, payment.ErrUnavailable) {
			t.Fatal("closed admission did not reject Submit as unavailable")
		}
		if _, err := service.Query(context.Background(), payment.RequestID(payment.NewUUIDv4()), payment.QueryInput{}); !errors.Is(err, payment.ErrUnavailable) {
			t.Fatal("closed admission did not reject Query as unavailable")
		}
		if calls, _, _ := submit.snapshot(); calls != 0 {
			t.Fatal("closed admission still called the Submit business port")
		}
		if calls, _, _ := query.snapshot(); calls != 0 {
			t.Fatal("closed admission still called the Query business port")
		}
	})

	t.Run("active business call delays database close", func(t *testing.T) {
		lifecycleTestClearPGEnv(t)
		order := &lifecycleTestOrder{}
		db := newLifecycleTestDB()
		db.closeFn = func() { order.add("db.close") }
		server := newLifecycleTestServer(order)
		submit := &businessTestSubmitFake{
			entered: make(chan struct{}),
			release: make(chan struct{}),
			result:  payment.SubmitResult{Order: businessTestOrder(payment.ResultUnknown), Created: true},
			after:   func() { order.add("business.finish") },
		}
		query := &businessTestQueryFake{result: payment.QueryResult{Order: businessTestOrder(payment.ResultSucceeded)}}
		factory := &businessTestFactory{submit: submit, query: query}
		var releaseOnce sync.Once
		release := func() { releaseOnce.Do(func() { close(submit.release) }) }
		t.Cleanup(release)
		runtime := businessTestStartRuntime(t, db, factory, true, lifecycleTestBaseValues(), lifecycleTestLimits(), server)

		if calls, _, _, _ := factory.snapshot(); calls != 1 {
			t.Fatalf("new business factory calls = %d, want 1", calls)
		}
		if runtime.captured == nil || runtime.captured.Handler == nil {
			t.Fatal("runtime server handler was not captured")
		}
		response := make(chan int, 1)
		go func() {
			recorder := businessTestServe(runtime.captured.Handler, businessTestNewRequest(http.MethodPost, businessTestPostPath, businessTestBody, true))
			response <- recorder.Code
		}()
		businessTestWaitSignal(t, submit.entered, "business POST did not enter the submit port")
		runtime.cancel()
		lifecycleTestWaitSignal(t, server.shutdownStarted, "runtime shutdown did not start")
		select {
		case err := <-runtime.result:
			t.Fatalf("runtime returned while a business call was still active: %v", err)
		default:
		}
		if _, _, closeCalls, _ := db.snapshot(); closeCalls != 0 {
			t.Fatal("runtime closed the database while a business call was still active")
		}
		_, factoryCtx, _, _ := factory.snapshot()
		if factoryCtx.Err() != nil {
			t.Fatal("normal shutdown canceled the work context before the business call finished")
		}
		release()
		select {
		case code := <-response:
			if code != http.StatusCreated {
				t.Fatalf("business response code = %d, want 201", code)
			}
		case <-time.After(2 * time.Second):
			t.Fatal("business handler did not finish after release")
		}
		if err := lifecycleTestWaitError(t, runtime.result, 2*time.Second); err != nil {
			t.Fatal("runtime shutdown returned an error")
		}
		if _, _, closeCalls, _ := db.snapshot(); closeCalls != 1 {
			t.Fatal("runtime did not close the database exactly once after business drain")
		}
		if !order.before("business.finish", "db.close") {
			t.Fatal("database closed before the business call finished")
		}
	})

	t.Run("forced server close does not bypass business admission", func(t *testing.T) {
		lifecycleTestClearPGEnv(t)
		order := &lifecycleTestOrder{}
		db := newLifecycleTestDB()
		db.closeFn = func() { order.add("db.close") }
		server := newLifecycleTestServer(order)
		server.shutdownFn = func(context.Context) error { return errors.New("fake shutdown failure") }
		submit := &businessTestSubmitFake{
			entered: make(chan struct{}),
			release: make(chan struct{}),
			result:  payment.SubmitResult{Order: businessTestOrder(payment.ResultUnknown), Created: true},
			after:   func() { order.add("business.finish") },
		}
		query := &businessTestQueryFake{result: payment.QueryResult{Order: businessTestOrder(payment.ResultSucceeded)}}
		factory := &businessTestFactory{submit: submit, query: query}
		var releaseOnce sync.Once
		release := func() { releaseOnce.Do(func() { close(submit.release) }) }
		t.Cleanup(release)
		limits := lifecycleTestLimits()
		limits.shutdown = 200 * time.Millisecond
		limits.closeReserve = 50 * time.Millisecond
		runtime := businessTestStartRuntime(t, db, factory, true, lifecycleTestBaseValues(), limits, server)

		if calls, _, _, _ := factory.snapshot(); calls != 1 {
			t.Fatalf("new business factory calls = %d, want 1", calls)
		}
		if runtime.captured == nil || runtime.captured.Handler == nil {
			t.Fatal("runtime server handler was not captured")
		}
		response := make(chan int, 1)
		go func() {
			recorder := businessTestServe(runtime.captured.Handler, businessTestNewRequest(http.MethodPost, businessTestPostPath, businessTestBody, true))
			response <- recorder.Code
		}()
		businessTestWaitSignal(t, submit.entered, "business POST did not enter the submit port")
		runtime.cancel()
		err := lifecycleTestWaitError(t, runtime.result, 2*time.Second)
		if !errors.Is(err, ErrShutdown) {
			t.Fatal("active business call did not produce ErrShutdown after forced close")
		}
		if shutdownCalls, closeCalls := server.snapshot(); shutdownCalls == 0 || closeCalls == 0 {
			t.Fatal("forced shutdown did not use server close")
		}
		if _, _, closeCalls, _ := db.snapshot(); closeCalls != 0 {
			t.Fatal("forced close bypassed business admission and closed the database early")
		}
		_, factoryCtx, _, _ := factory.snapshot()
		if factoryCtx.Err() == nil {
			t.Fatal("forced shutdown did not cancel the work context")
		}
		release()
		select {
		case <-response:
		case <-time.After(2 * time.Second):
			t.Fatal("business handler did not finish after forced-close release")
		}
		if _, _, closeCalls, _ := db.snapshot(); closeCalls != 0 {
			t.Fatal("forced shutdown closed the database after reporting ErrShutdown")
		}
	})
}
