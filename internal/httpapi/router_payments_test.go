package httpapi_test

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"payment-hub/internal/httpapi"
	"payment-hub/internal/payment"
)

const (
	routerTestClientID    = "router-client"
	routerTestAPIKey      = "FAKE-demo_key.9~a+/b=="
	routerTestOrderID     = "123e4567-e89b-42d3-a456-426614174000"
	routerTestBusinessNo  = "biz-router-1001"
	routerTestBody        = `{"business_order_no":"biz-router-1001","amount":100,"currency":"CNY"}`
	routerTestPostPath    = "/api/v1/payments"
	routerTestPaymentPath = "/api/v1/payments/123e4567-e89b-42d3-a456-426614174000"
	routerTestQueryPath   = "/api/v1/payments?business_order_no=biz-router-1001"
)

func routerTestOrder(result payment.Result) payment.PaymentOrder {
	order := payment.PaymentOrder{
		ID:              routerTestOrderID,
		ClientID:        routerTestClientID,
		BusinessOrderNo: routerTestBusinessNo,
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

type routerTestSubmitFake struct {
	mu       sync.Mutex
	calls    int
	clientID string
	result   payment.SubmitResult
	err      error
}

func (fake *routerTestSubmitFake) Submit(_ context.Context, _ payment.RequestID, input payment.RegisterOrderInput) (payment.SubmitResult, error) {
	fake.mu.Lock()
	fake.calls++
	fake.clientID = input.ClientID
	fake.mu.Unlock()
	return fake.result, fake.err
}

func (fake *routerTestSubmitFake) snapshot() (int, string) {
	fake.mu.Lock()
	defer fake.mu.Unlock()
	return fake.calls, fake.clientID
}

type routerTestQueryFake struct {
	mu       sync.Mutex
	calls    int
	clientID string
	result   payment.QueryResult
	err      error
}

func (fake *routerTestQueryFake) Query(_ context.Context, _ payment.RequestID, input payment.QueryInput) (payment.QueryResult, error) {
	fake.mu.Lock()
	fake.calls++
	fake.clientID = input.ClientID
	fake.mu.Unlock()
	return fake.result, fake.err
}

func (fake *routerTestQueryFake) snapshot() (int, string) {
	fake.mu.Lock()
	defer fake.mu.Unlock()
	return fake.calls, fake.clientID
}

func routerTestRequest(method, target, body string, authenticated bool) *http.Request {
	var reader io.Reader
	if body != "" {
		reader = bytes.NewReader([]byte(body))
	}
	request := httptest.NewRequest(method, target, reader)
	if body != "" {
		request.Header.Set("Content-Type", "application/json")
	}
	if authenticated {
		request.Header.Set("Authorization", "Bearer "+routerTestAPIKey)
	}
	return request
}

func routerTestCall(router http.Handler, method, target, body string, authenticated bool) *httptest.ResponseRecorder {
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, routerTestRequest(method, target, body, authenticated))
	return recorder
}

func routerTestErrorCode(t *testing.T, recorder *httptest.ResponseRecorder) string {
	t.Helper()
	var envelope struct {
		Error *struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &envelope); err != nil {
		t.Fatal("business error response was not JSON")
	}
	if envelope.Error == nil {
		t.Fatal("business error response did not contain an error envelope")
	}
	return envelope.Error.Code
}

func TestPaymentRouterWiring(t *testing.T) {
	validConfig := httpapi.AuthConfig{ClientID: routerTestClientID, APIKey: routerTestAPIKey}

	t.Run("route surface authentication and readyz independence", func(t *testing.T) {
		checkCalls := 0
		check := func(ctx context.Context) error {
			checkCalls++
			if _, ok := ctx.Deadline(); !ok {
				t.Error("readyz checker did not receive a deadline")
			}
			return nil
		}
		submit := &routerTestSubmitFake{result: payment.SubmitResult{Order: routerTestOrder(payment.ResultUnknown), Created: true}}
		query := &routerTestQueryFake{result: payment.QueryResult{Order: routerTestOrder(payment.ResultSucceeded)}}
		router, err := httpapi.NewRouterWithPayments(check, validConfig, submit, query)
		if err != nil {
			t.Fatal("NewRouterWithPayments rejected valid dependencies")
		}
		if router == nil {
			t.Fatal("NewRouterWithPayments returned a nil router")
		}
		if gin.Mode() != gin.ReleaseMode {
			t.Fatal("NewRouterWithPayments did not select Gin release mode")
		}
		want := map[string]bool{
			http.MethodGet + " /readyz":                      false,
			http.MethodPost + " /api/v1/payments":            false,
			http.MethodGet + " /api/v1/payments":             false,
			http.MethodGet + " /api/v1/payments/:payment_id": false,
		}
		routes := router.Routes()
		if len(routes) != len(want) {
			t.Fatalf("route count = %d, want %d", len(routes), len(want))
		}
		for _, route := range routes {
			key := route.Method + " " + route.Path
			if _, ok := want[key]; !ok {
				t.Fatalf("unexpected route registered: %s", key)
			}
			want[key] = true
		}
		for key, seen := range want {
			if !seen {
				t.Fatalf("expected route is missing: %s", key)
			}
		}

		readyz := routerTestCall(router, http.MethodGet, "/readyz", "", false)
		if readyz.Code != http.StatusOK {
			t.Fatal("readyz without authentication did not return 200")
		}
		readyz = routerTestCall(router, http.MethodGet, "/readyz", "", false)
		if readyz.Code != http.StatusOK || checkCalls != 2 {
			t.Fatal("readyz did not remain independent of business authentication")
		}
		unauthorized := routerTestCall(router, http.MethodPost, routerTestPostPath, routerTestBody, false)
		if unauthorized.Code != http.StatusUnauthorized {
			t.Fatal("business POST without authentication did not return 401")
		}
		if calls, _ := submit.snapshot(); calls != 0 {
			t.Fatal("unauthenticated business POST reached the submit port")
		}

		created := routerTestCall(router, http.MethodPost, routerTestPostPath, routerTestBody, true)
		if created.Code != http.StatusCreated {
			t.Fatalf("authenticated business POST status = %d, want 201", created.Code)
		}
		if calls, clientID := submit.snapshot(); calls != 1 || clientID != routerTestClientID {
			t.Fatal("business POST did not use the authenticated client identity")
		}
		byID := routerTestCall(router, http.MethodGet, routerTestPaymentPath, "", true)
		if byID.Code != http.StatusOK {
			t.Fatalf("authenticated payment ID query status = %d, want 200", byID.Code)
		}
		byBusinessNo := routerTestCall(router, http.MethodGet, routerTestQueryPath, "", true)
		if byBusinessNo.Code != http.StatusOK {
			t.Fatalf("authenticated business order query status = %d, want 200", byBusinessNo.Code)
		}
		if calls, clientID := query.snapshot(); calls != 2 || clientID != routerTestClientID {
			t.Fatal("business queries did not use the authenticated client identity")
		}
		if checkCalls != 2 {
			t.Fatal("business requests invoked the readyz checker")
		}

		for _, probe := range []struct {
			method string
			target string
		}{
			{http.MethodPut, routerTestPostPath},
			{http.MethodDelete, routerTestPaymentPath},
			{http.MethodPost, routerTestPaymentPath},
			{http.MethodGet, routerTestPaymentPath + "/extra"},
			{http.MethodGet, "/api/v1/orders"},
			{http.MethodGet, "/payments"},
			{http.MethodGet, "/healthz"},
		} {
			recorder := routerTestCall(router, probe.method, probe.target, "", true)
			if recorder.Code != http.StatusNotFound {
				t.Fatalf("non-contract route %s %s returned %d, want 404", probe.method, probe.target, recorder.Code)
			}
		}
	})

	t.Run("readyz remains available when the checker is nil", func(t *testing.T) {
		submit := &routerTestSubmitFake{result: payment.SubmitResult{Order: routerTestOrder(payment.ResultUnknown), Created: true}}
		query := &routerTestQueryFake{result: payment.QueryResult{Order: routerTestOrder(payment.ResultSucceeded)}}
		router, err := httpapi.NewRouterWithPayments(nil, validConfig, submit, query)
		if err != nil {
			t.Fatal("NewRouterWithPayments rejected a nil readyz checker")
		}
		readyz := routerTestCall(router, http.MethodGet, "/readyz", "", false)
		if readyz.Code != http.StatusServiceUnavailable {
			t.Fatal("nil readyz checker did not fail closed")
		}
		created := routerTestCall(router, http.MethodPost, routerTestPostPath, routerTestBody, true)
		if created.Code != http.StatusCreated {
			t.Fatal("business route depended on the readyz checker")
		}
	})

	t.Run("registration fails closed without a partial router", func(t *testing.T) {
		submit := &routerTestSubmitFake{result: payment.SubmitResult{Order: routerTestOrder(payment.ResultUnknown)}}
		query := &routerTestQueryFake{result: payment.QueryResult{Order: routerTestOrder(payment.ResultUnknown)}}
		cases := []struct {
			name   string
			config httpapi.AuthConfig
			submit httpapi.PaymentSubmitter
			query  httpapi.PaymentQuerier
		}{
			{name: "invalid client id", config: httpapi.AuthConfig{APIKey: routerTestAPIKey}, submit: submit, query: query},
			{name: "invalid api key", config: httpapi.AuthConfig{ClientID: routerTestClientID}, submit: submit, query: query},
			{name: "nil submitter", config: validConfig, submit: nil, query: query},
			{name: "nil querier", config: validConfig, submit: submit, query: nil},
			{name: "nil dependencies", config: validConfig, submit: nil, query: nil},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				router, err := httpapi.NewRouterWithPayments(func(context.Context) error { return nil }, tc.config, tc.submit, tc.query)
				if err == nil {
					t.Fatal("NewRouterWithPayments accepted invalid dependencies")
				}
				if router != nil {
					t.Fatal("NewRouterWithPayments leaked a partial router")
				}
			})
		}
	})

	t.Run("business port unavailability maps to JSON 503", func(t *testing.T) {
		submit := &routerTestSubmitFake{err: payment.ErrUnavailable}
		query := &routerTestQueryFake{err: payment.ErrUnavailable}
		router, err := httpapi.NewRouterWithPayments(func(context.Context) error { return nil }, validConfig, submit, query)
		if err != nil {
			t.Fatal("NewRouterWithPayments rejected valid unavailable ports")
		}
		for _, call := range []struct {
			method string
			target string
			body   string
		}{
			{http.MethodPost, routerTestPostPath, routerTestBody},
			{http.MethodGet, routerTestPaymentPath, ""},
			{http.MethodGet, routerTestQueryPath, ""},
		} {
			recorder := routerTestCall(router, call.method, call.target, call.body, true)
			if recorder.Code != http.StatusServiceUnavailable {
				t.Fatalf("%s %s status = %d, want 503", call.method, call.target, recorder.Code)
			}
			if contentType := recorder.Header().Get("Content-Type"); contentType != "application/json" {
				t.Fatalf("%s %s content type = %q, want application/json", call.method, call.target, contentType)
			}
			if code := routerTestErrorCode(t, recorder); code != "PROCESSING_ERROR" {
				t.Fatalf("%s %s error code = %q, want PROCESSING_ERROR", call.method, call.target, code)
			}
		}
	})

	t.Run("old NewRouter remains readyz only", func(t *testing.T) {
		router := httpapi.NewRouter(nil)
		routes := router.Routes()
		if len(routes) != 1 || routes[0].Method != http.MethodGet || routes[0].Path != "/readyz" {
			t.Fatal("NewRouter route surface changed")
		}
		for _, call := range []struct {
			method string
			target string
		}{
			{http.MethodGet, routerTestPostPath},
			{http.MethodPost, routerTestPostPath},
			{http.MethodGet, routerTestPaymentPath},
		} {
			recorder := routerTestCall(router, call.method, call.target, "", true)
			if recorder.Code != http.StatusNotFound {
				t.Fatalf("old NewRouter exposed %s %s", call.method, call.target)
			}
		}
	})
}
