package httpapi_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"

	"payment-hub/internal/httpapi"
	"payment-hub/internal/payment"
)

func TestNewRouterAppliesMiddlewareOption(t *testing.T) {
	called := 0
	router := httpapi.NewRouter(
		func(context.Context) error { return nil },
		httpapi.WithMiddleware(func(c *gin.Context) {
			called++
			c.Next()
		}),
	)

	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if recorder.Code != http.StatusOK {
		t.Fatalf("readyz status = %d", recorder.Code)
	}
	if called != 1 {
		t.Fatalf("middleware calls = %d, want 1", called)
	}
}

func TestNewRouterWithPaymentsMiddlewareRunsBeforeAuth(t *testing.T) {
	called := 0
	router, err := httpapi.NewRouterWithPayments(
		func(context.Context) error { return nil },
		httpapi.AuthConfig{ClientID: routerTestClientID, APIKey: routerTestAPIKey},
		&routerTestSubmitFake{},
		&routerTestQueryFake{},
		httpapi.WithMiddleware(func(c *gin.Context) {
			called++
			c.Next()
		}),
	)
	if err != nil {
		t.Fatalf("build router: %v", err)
	}

	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, httptest.NewRequest(http.MethodPost, "/api/v1/payments", nil))
	if recorder.Code != http.StatusUnauthorized {
		t.Fatalf("missing-auth status = %d, want 401", recorder.Code)
	}
	if called != 1 {
		t.Fatalf("middleware calls = %d, want 1", called)
	}
}

func TestMetricsMiddlewarePreservesBusinessResponses(t *testing.T) {
	submit := &routerTestSubmitFake{
		result: payment.SubmitResult{
			Order:   routerTestOrder(payment.ResultSucceeded),
			Created: true,
		},
	}
	query := &routerTestQueryFake{
		result: payment.QueryResult{Order: routerTestOrder(payment.ResultSucceeded)},
	}
	router, err := httpapi.NewRouterWithPayments(
		func(context.Context) error { return nil },
		httpapi.AuthConfig{ClientID: routerTestClientID, APIKey: routerTestAPIKey},
		submit,
		query,
		httpapi.WithMiddleware(func(c *gin.Context) { c.Next() }),
	)
	if err != nil {
		t.Fatalf("build router: %v", err)
	}

	if created := routerTestCall(router, http.MethodPost, routerTestPostPath, routerTestBody, true); created.Code != http.StatusCreated {
		t.Fatalf("authenticated POST status = %d, want 201", created.Code)
	}
	if byID := routerTestCall(router, http.MethodGet, routerTestPaymentPath, "", true); byID.Code != http.StatusOK {
		t.Fatalf("payment ID query status = %d, want 200", byID.Code)
	}
	if unauthorized := routerTestCall(router, http.MethodPost, routerTestPostPath, routerTestBody, false); unauthorized.Code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated POST status = %d, want 401", unauthorized.Code)
	}
}
