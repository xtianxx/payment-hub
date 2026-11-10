//go:build integration

package tests

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"payment-hub/internal/httpapi"
	"payment-hub/internal/payment"
)

type us1FakePorts struct {
	mu          sync.Mutex
	submitCalls int
	queryCalls  int
	submitErr   error
	queryErr    error
}

func (ports *us1FakePorts) Submit(_ context.Context, _ payment.RequestID, input payment.RegisterOrderInput) (payment.SubmitResult, error) {
	ports.mu.Lock()
	ports.submitCalls++
	err := ports.submitErr
	ports.mu.Unlock()
	if err != nil {
		return payment.SubmitResult{}, err
	}
	order := us1BoundaryOrder()
	order.ClientID = input.ClientID
	order.BusinessOrderNo = input.BusinessOrderNo
	order.Amount = input.Amount
	order.Currency = input.Currency
	return payment.SubmitResult{Order: order, Created: true}, nil
}

func (ports *us1FakePorts) Query(context.Context, payment.RequestID, payment.QueryInput) (payment.QueryResult, error) {
	ports.mu.Lock()
	ports.queryCalls++
	err := ports.queryErr
	ports.mu.Unlock()
	if err != nil {
		return payment.QueryResult{}, err
	}
	return payment.QueryResult{Order: us1BoundaryOrder()}, nil
}

func (ports *us1FakePorts) reset() {
	ports.mu.Lock()
	defer ports.mu.Unlock()
	ports.submitCalls = 0
	ports.queryCalls = 0
}

func (ports *us1FakePorts) counts() (submit, query int) {
	ports.mu.Lock()
	defer ports.mu.Unlock()
	return ports.submitCalls, ports.queryCalls
}

func us1BoundaryOrder() payment.PaymentOrder {
	now := time.Date(2026, time.November, 9, 0, 0, 0, 0, time.UTC)
	return payment.PaymentOrder{
		ID:              payment.PaymentOrderID("123e4567-e89b-42d3-a456-426614174000"),
		ClientID:        "us1-boundary-client",
		BusinessOrderNo: "M100",
		Amount:          100,
		Currency:        "CNY",
		Result:          payment.ResultSucceeded,
		RegisteredAt:    now,
		ConfirmedAt:     &now,
	}
}

func TestUS1HTTPBoundary(t *testing.T) {
	gin.SetMode(gin.ReleaseMode)
	repoRoot := us1RepoRoot(t)
	runID := us1RunID(t)
	facts := &us1ReportScenario{
		Generations: []uint64{},
		HTTP:        []us1ReportHTTP{},
		PG:          []us1ReportPG{},
		Commands:    []us1ReportCommand{},
	}
	artifacts := us1BeginArtifacts(t, repoRoot, runID, "http_boundary")

	ports := &us1FakePorts{}
	router := gin.New()
	if err := httpapi.RegisterPaymentRoutes(router, httpapi.AuthConfig{
		ClientID: "us1-boundary-client",
		APIKey:   us1APIKey,
	}, ports, ports); err != nil {
		t.Fatal("US1 boundary route registration failed")
	}
	server := httptest.NewUnstartedServer(router)
	server.Start()
	t.Cleanup(server.Close)
	addr := server.Listener.Addr().String()

	t.Run("actual 4096 accepted", func(t *testing.T) {
		ports.reset()
		response, body, err := us1PostBytes(context.Background(), addr, us1APIKey, us1PaddedPostBody(4096), "application/json")
		if err != nil {
			t.Fatal("US1 4096 POST failed")
		}
		paymentBody, requestID := us1RequirePaymentResponse(t, response, body, http.StatusCreated, string(us1BoundaryOrder().ID), "M100", 100, "CNY", payment.ResultSucceeded)
		us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, paymentBody.PaymentID, string(paymentBody.Result))
		submit, query := ports.counts()
		if submit != 1 || query != 0 {
			t.Fatalf("US1 4096 port counts mismatch: submit=%d query=%d", submit, query)
		}
	})

	t.Run("actual 4097 rejected", func(t *testing.T) {
		ports.reset()
		response, body, err := us1PostBytes(context.Background(), addr, us1APIKey, us1PaddedPostBody(4097), "application/json")
		if err != nil {
			t.Fatal("US1 4097 POST failed")
		}
		requestID := us1RequireErrorResponse(t, response, body, http.StatusRequestEntityTooLarge, "INVALID_REQUEST")
		us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, "", "")
		submit, query := ports.counts()
		if submit != 0 || query != 0 {
			t.Fatalf("US1 4097 port counts mismatch: submit=%d query=%d", submit, query)
		}
	})

	t.Run("stream chunked 4097 rejected", func(t *testing.T) {
		ports.reset()
		body := us1PaddedPostBody(4097)
		request, err := http.NewRequestWithContext(context.Background(), http.MethodPost, server.URL+"/api/v1/payments", io.NopCloser(bytes.NewReader(body)))
		if err != nil {
			t.Fatal("US1 chunked request construction failed")
		}
		request.TransferEncoding = []string{"chunked"}
		request.Header.Set("Content-Type", "application/json")
		request.Header.Set("Authorization", "Bearer "+us1APIKey)
		response, responseBody, err := us1Do(request)
		if err != nil {
			t.Fatal("US1 chunked POST failed")
		}
		requestID := us1RequireErrorResponse(t, response, responseBody, http.StatusRequestEntityTooLarge, "INVALID_REQUEST")
		us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, "", "")
		submit, query := ports.counts()
		if submit != 0 || query != 0 {
			t.Fatalf("US1 chunked port counts mismatch: submit=%d query=%d", submit, query)
		}
	})

	t.Run("duplicate content type rejected", func(t *testing.T) {
		ports.reset()
		request, err := http.NewRequestWithContext(context.Background(), http.MethodPost, server.URL+"/api/v1/payments", strings.NewReader(us1ValidPostBody))
		if err != nil {
			t.Fatal("US1 duplicate content type request construction failed")
		}
		request.Header["Content-Type"] = []string{"application/json", "application/json"}
		request.Header.Set("Authorization", "Bearer "+us1APIKey)
		response, body, err := us1Do(request)
		if err != nil {
			t.Fatal("US1 duplicate content type POST failed")
		}
		requestID := us1RequireErrorResponse(t, response, body, http.StatusUnsupportedMediaType, "INVALID_REQUEST")
		us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, "", "")
		submit, query := ports.counts()
		if submit != 0 || query != 0 {
			t.Fatalf("US1 duplicate content type port counts mismatch: submit=%d query=%d", submit, query)
		}
	})

	t.Run("duplicate content encoding rejected", func(t *testing.T) {
		ports.reset()
		request, err := http.NewRequestWithContext(context.Background(), http.MethodPost, server.URL+"/api/v1/payments", strings.NewReader(us1ValidPostBody))
		if err != nil {
			t.Fatal("US1 duplicate content encoding request construction failed")
		}
		request.Header.Set("Content-Type", "application/json")
		request.Header["Content-Encoding"] = []string{"identity", "gzip"}
		request.Header.Set("Authorization", "Bearer "+us1APIKey)
		response, body, err := us1Do(request)
		if err != nil {
			t.Fatal("US1 duplicate content encoding POST failed")
		}
		requestID := us1RequireErrorResponse(t, response, body, http.StatusUnsupportedMediaType, "INVALID_REQUEST")
		us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, "", "")
		submit, query := ports.counts()
		if submit != 0 || query != 0 {
			t.Fatalf("US1 duplicate content encoding port counts mismatch: submit=%d query=%d", submit, query)
		}
	})

	t.Run("GET body rejected", func(t *testing.T) {
		ports.reset()
		request, err := http.NewRequestWithContext(context.Background(), http.MethodGet, server.URL+"/api/v1/payments?business_order_no=M100", strings.NewReader("{}"))
		if err != nil {
			t.Fatal("US1 GET body request construction failed")
		}
		request.Header.Set("Authorization", "Bearer "+us1APIKey)
		response, body, err := us1Do(request)
		if err != nil {
			t.Fatal("US1 GET body failed")
		}
		requestID := us1RequireErrorResponse(t, response, body, http.StatusBadRequest, "INVALID_REQUEST")
		us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, "", "")
		submit, query := ports.counts()
		if submit != 0 || query != 0 {
			t.Fatalf("US1 GET body port counts mismatch: submit=%d query=%d", submit, query)
		}
	})

	t.Run("unknown query rejected", func(t *testing.T) {
		ports.reset()
		request, err := http.NewRequestWithContext(context.Background(), http.MethodGet, server.URL+"/api/v1/payments?business_order_no=M100&page=1", nil)
		if err != nil {
			t.Fatal("US1 unknown query request construction failed")
		}
		request.Header.Set("Authorization", "Bearer "+us1APIKey)
		response, body, err := us1Do(request)
		if err != nil {
			t.Fatal("US1 unknown query GET failed")
		}
		requestID := us1RequireErrorResponse(t, response, body, http.StatusBadRequest, "INVALID_REQUEST")
		us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, "", "")
		submit, query := ports.counts()
		if submit != 0 || query != 0 {
			t.Fatalf("US1 unknown query port counts mismatch: submit=%d query=%d", submit, query)
		}
	})

	t.Run("auth priority", func(t *testing.T) {
		ports.reset()
		request, err := http.NewRequestWithContext(context.Background(), http.MethodPost, server.URL+"/api/v1/payments", strings.NewReader("{"))
		if err != nil {
			t.Fatal("US1 auth priority POST construction failed")
		}
		request.Header.Set("Content-Type", "application/json")
		response, body, err := us1Do(request)
		if err != nil {
			t.Fatal("US1 auth priority POST failed")
		}
		requestID := us1RequireErrorResponse(t, response, body, http.StatusUnauthorized, "UNAUTHORIZED")
		us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, "", "")

		request, err = http.NewRequestWithContext(context.Background(), http.MethodGet, server.URL+"/api/v1/payments?business_order_no=M100", strings.NewReader("{}"))
		if err != nil {
			t.Fatal("US1 auth priority GET construction failed")
		}
		response, body, err = us1Do(request)
		if err != nil {
			t.Fatal("US1 auth priority GET failed")
		}
		requestID = us1RequireErrorResponse(t, response, body, http.StatusUnauthorized, "UNAUTHORIZED")
		us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, "", "")
		submit, query := ports.counts()
		if submit != 0 || query != 0 {
			t.Fatalf("US1 auth priority port counts mismatch: submit=%d query=%d", submit, query)
		}
	})

	t.Run("raw duplicate content type rejected", func(t *testing.T) {
		ports.reset()
		raw := "POST /api/v1/payments HTTP/1.1\r\nHost: " + addr +
			"\r\nAuthorization: Bearer " + us1APIKey +
			"\r\nContent-Type: application/json\r\nContent-Type: application/json\r\nContent-Length: " +
			strconv.Itoa(len(us1ValidPostBody)) + "\r\nConnection: close\r\n\r\n" + us1ValidPostBody
		status, requestID := us1RawAppProbe(t, addr, raw)
		if status != http.StatusUnsupportedMediaType {
			t.Fatalf("US1 raw duplicate content type status mismatch: got %d", status)
		}
		us1RecordHTTP(t, artifacts, facts, 1, status, requestID, "", "")
		submit, query := ports.counts()
		if submit != 0 || query != 0 {
			t.Fatalf("US1 raw duplicate content type port counts mismatch: submit=%d query=%d", submit, query)
		}
	})

	t.Run("raw malformed request line rejected", func(t *testing.T) {
		ports.reset()
		raw := "POST /api/v1/payments BAD\r\nHost: " + addr + "\r\nConnection: close\r\n\r\n"
		status := us1RawServerProbe(t, addr, raw)
		if status != http.StatusBadRequest {
			t.Fatalf("US1 raw malformed request status mismatch: got %d", status)
		}
		us1RecordHTTP(t, artifacts, facts, 1, status, "", "", "")
		submit, query := ports.counts()
		if submit != 0 || query != 0 {
			t.Fatalf("US1 raw malformed request port counts mismatch: submit=%d query=%d", submit, query)
		}
	})

	t.Run("raw fake content length rejected", func(t *testing.T) {
		ports.reset()
		raw := "POST /api/v1/payments HTTP/1.1\r\nHost: " + addr +
			"\r\nAuthorization: Bearer " + us1APIKey +
			"\r\nContent-Type: application/json\r\nContent-Length: 10\r\nConnection: close\r\n\r\n" +
			string(us1PaddedPostBody(4097))
		status, requestID := us1RawAppProbe(t, addr, raw)
		if status != http.StatusBadRequest {
			t.Fatalf("US1 raw fake content length status mismatch: got %d", status)
		}
		us1RecordHTTP(t, artifacts, facts, 1, status, requestID, "", "")
		submit, query := ports.counts()
		if submit != 0 || query != 0 {
			t.Fatalf("US1 raw fake content length port counts mismatch: submit=%d query=%d", submit, query)
		}
	})

	us1FinishArtifacts(t, artifacts)
	facts.Name = "http_boundary"
	if t.Failed() {
		facts.Result = us1ReportResultFail
		us1ReportRecord(t, facts)
		return
	}
	facts.addCommand(us1ReportCommandProbe, 0, false)
	facts.Result = us1ReportResultPass
	us1ReportRecord(t, facts)
}

func us1RawAppProbe(t *testing.T, addr, raw string) (int, string) {
	t.Helper()
	response, requestID := us1RawResponse(t, addr, raw)
	if !strings.HasPrefix(response.Header.Get("Content-Type"), "application/json") {
		t.Fatal("US1 raw app response content type mismatch")
	}
	if !strings.Contains(response.Header.Get("Cache-Control"), "no-store") {
		t.Fatal("US1 raw app response cache policy mismatch")
	}
	if requestID == "" || payment.ValidateUUIDv4(requestID) != nil {
		t.Fatal("US1 raw app response request_id mismatch")
	}
	return response.StatusCode, requestID
}

func us1RawServerProbe(t *testing.T, addr, raw string) int {
	t.Helper()
	response, _ := us1RawResponse(t, addr, raw)
	return response.StatusCode
}

func us1RawResponse(t *testing.T, addr, raw string) (*http.Response, string) {
	t.Helper()
	conn, err := net.DialTimeout("tcp", addr, 2*time.Second)
	if err != nil {
		t.Fatal("US1 raw connection failed")
	}
	defer func() { _ = conn.Close() }()
	if err := conn.SetDeadline(time.Now().Add(2 * time.Second)); err != nil {
		t.Fatal("US1 raw deadline failed")
	}
	writeDone := make(chan struct{})
	go func() {
		defer close(writeDone)
		payload := []byte(raw)
		for len(payload) > 0 {
			written, err := conn.Write(payload)
			if err != nil || written <= 0 {
				return
			}
			payload = payload[written:]
		}
	}()
	response, err := http.ReadResponse(bufio.NewReader(conn), &http.Request{Method: http.MethodPost})
	if err != nil {
		t.Fatal("US1 raw response read failed")
	}
	body, readErr := io.ReadAll(io.LimitReader(response.Body, 4097))
	closeErr := response.Body.Close()
	if readErr != nil || closeErr != nil || len(body) > 4096 {
		t.Fatal("US1 raw response body read failed")
	}
	select {
	case <-writeDone:
	case <-time.After(500 * time.Millisecond):
		_ = conn.Close()
		select {
		case <-writeDone:
		case <-time.After(500 * time.Millisecond):
		}
	}
	requestID := ""
	if len(body) > 0 {
		var envelope us1Envelope
		if err := json.Unmarshal(body, &envelope); err == nil {
			requestID = envelope.RequestID
			if envelope.Error == nil || envelope.Error.Code != "INVALID_REQUEST" {
				t.Fatal("US1 raw app response error code mismatch")
			}
		}
	}
	return response, requestID
}
