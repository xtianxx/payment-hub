package httpapi_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"payment-hub/internal/httpapi"
	"payment-hub/internal/payment"
)

func init() {
	gin.SetMode(gin.TestMode)
}

const (
	contractClientID    = "fake-client-CONTRACT_01"
	contractAPIKey      = "FAKE-contract_key.9~a+/b=="
	contractWrongAPIKey = "FAKE-contract_key.9~a+/c=="
	contractCanary      = "FAKE_LEAK_CANARY_HTTP_CONTRACT"
	contractBusinessNo  = "M100"
	contractChallenge   = `Bearer realm="payment-hub"`
)

var (
	contractOrderID      = payment.PaymentOrderID("8b3e19bd-909a-467d-94c7-bf713984f03c")
	contractRegisteredAt = time.Date(2026, 11, 6, 9, 0, 0, 0, time.UTC)
	contractConfirmedAt  = time.Date(2026, 11, 6, 9, 0, 1, 123456789, time.UTC)
	contractLowerUUIDv4  = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)
	contractPostPath     = "/api/v1/payments"
	contractGetByIDPath  = "/api/v1/payments/" + string(contractOrderID)
)

func contractFail(t *testing.T, category, format string, args ...any) {
	t.Helper()
	t.Fatalf("CONTRACT[%s] %s", category, fmt.Sprintf(format, args...))
}

func contractNoPanic(t *testing.T, category string, fn func()) {
	t.Helper()
	defer func() {
		if recover() != nil {
			contractFail(t, category, "unexpected panic")
		}
	}()
	fn()
}

type contractSubmitFake struct {
	t         *testing.T
	calls     int
	requestID payment.RequestID
	input     payment.RegisterOrderInput
	result    payment.SubmitResult
	err       error
}

func (f *contractSubmitFake) Submit(ctx context.Context, requestID payment.RequestID, input payment.RegisterOrderInput) (payment.SubmitResult, error) {
	f.calls++
	f.requestID = requestID
	f.input = input
	if f.t != nil {
		if ctx == nil {
			contractFail(f.t, "fixture_submit_context", "Submit received a nil context")
		}
		if payment.ValidateUUIDv4(string(requestID)) != nil {
			contractFail(f.t, "fixture_submit_request_id", "Submit received an invalid request ID")
		}
		if input.ClientID != contractClientID {
			contractFail(f.t, "fixture_submit_client_id", "Submit received an unexpected client ID")
		}
		if payment.ValidateBusinessOrderNo(input.BusinessOrderNo) != nil {
			contractFail(f.t, "fixture_submit_business_order_no", "Submit received an invalid business order number")
		}
	}
	// HTTP delegates business-value decisions to payment. The fake must behave
	// like that boundary: it accepts the syntax, records the call, and returns
	// a sanitized invalid-input classification instead of turning it into 201.
	if payment.ValidateAmount(input.Amount) != nil || payment.ValidateCurrency(input.Currency) != nil {
		return payment.SubmitResult{}, payment.ErrInvalidInput
	}
	return f.result, f.err
}

type contractQueryFake struct {
	t         *testing.T
	calls     int
	requestID payment.RequestID
	input     payment.QueryInput
	result    payment.QueryResult
	err       error
}

func (f *contractQueryFake) Query(ctx context.Context, requestID payment.RequestID, input payment.QueryInput) (payment.QueryResult, error) {
	f.calls++
	f.requestID = requestID
	f.input = input
	if f.t != nil {
		if ctx == nil {
			contractFail(f.t, "fixture_query_context", "Query received a nil context")
		}
		if payment.ValidateUUIDv4(string(requestID)) != nil {
			contractFail(f.t, "fixture_query_request_id", "Query received an invalid request ID")
		}
		if input.ClientID != contractClientID {
			contractFail(f.t, "fixture_query_client_id", "Query received an unexpected client ID")
		}
		hasID := input.PaymentOrderID != ""
		hasBusinessNo := input.BusinessOrderNo != ""
		if hasID == hasBusinessNo {
			contractFail(f.t, "fixture_query_selector", "Query must receive exactly one selector")
		}
		if hasID {
			if payment.ValidateUUIDv4(string(input.PaymentOrderID)) != nil {
				contractFail(f.t, "fixture_query_payment_id", "Query received an invalid payment ID")
			}
		} else if payment.ValidateBusinessOrderNo(input.BusinessOrderNo) != nil {
			contractFail(f.t, "fixture_query_business_order_no", "Query received an invalid business order number")
		}
	}
	return f.result, f.err
}

func contractOrder(result payment.Result) payment.PaymentOrder {
	order := payment.PaymentOrder{
		ID:              contractOrderID,
		ClientID:        contractClientID,
		BusinessOrderNo: contractBusinessNo,
		Amount:          100,
		Currency:        "CNY",
		Result:          result,
		RegisteredAt:    contractRegisteredAt,
	}
	if result != payment.ResultUnknown {
		at := contractConfirmedAt
		order.ConfirmedAt = &at
		mockID := payment.MockTransactionID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
		order.MockTransactionID = &mockID
	}
	return order
}

func contractSubmitCreated() payment.SubmitResult {
	return payment.SubmitResult{Order: contractOrder(payment.ResultUnknown), Created: true}
}

func contractQueryFound(result payment.Result) payment.QueryResult {
	return payment.QueryResult{Order: contractOrder(result)}
}

func contractNewRouter(t *testing.T, submit *contractSubmitFake, query *contractQueryFake) *gin.Engine {
	t.Helper()
	router := gin.New()
	if err := httpapi.RegisterPaymentRoutes(router, httpapi.AuthConfig{ClientID: contractClientID, APIKey: contractAPIKey}, submit, query); err != nil {
		contractFail(t, "fixture_route_registration", "RegisterPaymentRoutes rejected valid fakes: %v", err)
	}
	return router
}

type contractBodyReader struct {
	reader    *bytes.Reader
	reads     int
	bytesRead int
}

func (b *contractBodyReader) Read(p []byte) (int, error) {
	b.reads++
	n, err := b.reader.Read(p)
	b.bytesRead += n
	return n, err
}

func (b *contractBodyReader) Close() error { return nil }

func contractNewRequest(method, target string, body []byte) (*http.Request, *contractBodyReader) {
	var reader io.Reader
	var bodyReader *contractBodyReader
	if body != nil {
		bodyReader = &contractBodyReader{reader: bytes.NewReader(body)}
		reader = bodyReader
	}
	req := httptest.NewRequest(method, target, reader)
	if bodyReader != nil {
		req.Body = bodyReader
		req.ContentLength = int64(len(body))
		req.Header.Set("Content-Length", strconv.Itoa(len(body)))
	}
	req.Header.Set("Authorization", "Bearer "+contractAPIKey)
	if method == http.MethodPost {
		req.Header.Set("Content-Type", "application/json")
	}
	return req, bodyReader
}

func contractDo(router *gin.Engine, req *http.Request) *httptest.ResponseRecorder {
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, req)
	return recorder
}

func contractAssertStatus(t *testing.T, recorder *httptest.ResponseRecorder, want int) {
	t.Helper()
	if recorder.Code != want {
		contractFail(t, "status", "status = %d, want %d", recorder.Code, want)
	}
}

func contractAssertJSONNoStore(t *testing.T, recorder *httptest.ResponseRecorder) {
	t.Helper()
	if got := recorder.Header().Get("Content-Type"); got != "application/json" {
		contractFail(t, "header_content_type", "Content-Type is not application/json")
	}
	if got := recorder.Header().Get("Cache-Control"); got != "no-store" {
		contractFail(t, "header_cache_control", "Cache-Control is not no-store")
	}
	if len(recorder.Header().Values("ETag")) != 0 {
		contractFail(t, "header_etag", "ETag must not be emitted")
	}
	if len(recorder.Header().Values("Last-Modified")) != 0 {
		contractFail(t, "header_last_modified", "Last-Modified must not be emitted")
	}
}

func contractDecodeObject(t *testing.T, recorder *httptest.ResponseRecorder) map[string]json.RawMessage {
	t.Helper()
	var top map[string]json.RawMessage
	if err := json.Unmarshal(recorder.Body.Bytes(), &top); err != nil || top == nil {
		contractFail(t, "body_json", "response body is not a single JSON object")
	}
	return top
}

func contractAssertExactKeys(t *testing.T, category string, object map[string]json.RawMessage, want ...string) {
	t.Helper()
	if len(object) != len(want) {
		contractFail(t, category, "object has %d keys, want %d", len(object), len(want))
	}
	for _, key := range want {
		if _, ok := object[key]; !ok {
			contractFail(t, category, "required key is missing")
		}
	}
}

func contractAssertRequestID(t *testing.T, raw json.RawMessage) string {
	t.Helper()
	var requestID string
	if err := json.Unmarshal(raw, &requestID); err != nil {
		contractFail(t, "request_id", "request_id is not a string")
	}
	if !contractLowerUUIDv4.MatchString(requestID) {
		contractFail(t, "request_id", "request_id is not a canonical lowercase UUIDv4")
	}
	return requestID
}

func contractAssertNoCanary(t *testing.T, recorder *httptest.ResponseRecorder) {
	t.Helper()
	if bytes.Contains(recorder.Body.Bytes(), []byte(contractCanary)) {
		contractFail(t, "message_leak", "response leaked the fake canary")
	}
}

func contractAssertUnauthorizedChallenge(t *testing.T, recorder *httptest.ResponseRecorder) {
	t.Helper()
	values := recorder.Header().Values("WWW-Authenticate")
	if len(values) != 1 || values[0] != contractChallenge {
		contractFail(t, "header_www_authenticate", "WWW-Authenticate challenge mismatch")
	}
}

func contractAssertErrorEnvelope(t *testing.T, recorder *httptest.ResponseRecorder, wantStatus int, wantCode string) {
	t.Helper()
	contractAssertStatus(t, recorder, wantStatus)
	contractAssertJSONNoStore(t, recorder)
	if wantStatus == http.StatusUnauthorized {
		contractAssertUnauthorizedChallenge(t, recorder)
	}
	top := contractDecodeObject(t, recorder)
	contractAssertExactKeys(t, "envelope_keys", top, "request_id", "error")
	if _, ok := top["payment"]; ok {
		contractFail(t, "envelope_exclusive", "error response must not contain payment")
	}
	if _, ok := top["issues"]; ok {
		contractFail(t, "envelope_exclusive", "error response must not contain issues")
	}
	contractAssertRequestID(t, top["request_id"])

	var errorObject map[string]json.RawMessage
	if err := json.Unmarshal(top["error"], &errorObject); err != nil {
		contractFail(t, "error_object", "error is not a JSON object")
	}
	contractAssertExactKeys(t, "error_fields", errorObject, "code", "message")
	var code, message string
	if err := json.Unmarshal(errorObject["code"], &code); err != nil {
		contractFail(t, "error_code", "error.code is not a string")
	}
	if err := json.Unmarshal(errorObject["message"], &message); err != nil {
		contractFail(t, "error_message", "error.message is not a string")
	}
	if code != wantCode {
		contractFail(t, "error_code", "error.code mismatch")
	}
	if message == "" {
		contractFail(t, "error_message", "error.message is empty")
	}
	contractAssertNoCanary(t, recorder)
}

func contractAssertBoundarySuccess(t *testing.T, recorder *httptest.ResponseRecorder, wantStatus int) {
	t.Helper()
	contractAssertStatus(t, recorder, wantStatus)
	contractAssertJSONNoStore(t, recorder)
	top := contractDecodeObject(t, recorder)
	if _, ok := top["error"]; ok {
		contractFail(t, "envelope_exclusive", "success response must not contain error")
	}
	if _, ok := top["request_id"]; !ok {
		contractFail(t, "envelope_keys", "success response is missing request_id")
	}
	if _, ok := top["payment"]; !ok {
		contractFail(t, "envelope_keys", "success response is missing payment")
	}
	contractAssertRequestID(t, top["request_id"])
	contractAssertNoCanary(t, recorder)
}

func contractAssertCalls(t *testing.T, submit *contractSubmitFake, query *contractQueryFake, wantSubmit, wantQuery int) {
	t.Helper()
	if submit.calls != wantSubmit {
		contractFail(t, "submit_calls", "Submit calls = %d, want %d", submit.calls, wantSubmit)
	}
	if query.calls != wantQuery {
		contractFail(t, "query_calls", "Query calls = %d, want %d", query.calls, wantQuery)
	}
}

type contractBoundaryCheck func(*testing.T, *http.Request, *contractBodyReader, *contractSubmitFake, *contractQueryFake)

func contractCheckNoBodyRead(t *testing.T, _ *http.Request, body *contractBodyReader, _ *contractSubmitFake, _ *contractQueryFake) {
	t.Helper()
	if body != nil && body.reads != 0 {
		contractFail(t, "body_reads", "request body was read before the rejection")
	}
}

func contractCheckReadRange(minBytes, maxBytes int) contractBoundaryCheck {
	return func(t *testing.T, _ *http.Request, body *contractBodyReader, _ *contractSubmitFake, _ *contractQueryFake) {
		t.Helper()
		if body == nil {
			contractFail(t, "body_reads", "expected a readable request body")
		}
		if body.bytesRead < minBytes || body.bytesRead > maxBytes {
			contractFail(t, "body_reads", "request body read %d bytes, want %d..%d", body.bytesRead, minBytes, maxBytes)
		}
	}
}

func contractCheckSubmitInput(wantBusinessNo string, wantAmount int64, wantCurrency string) contractBoundaryCheck {
	return func(t *testing.T, _ *http.Request, _ *contractBodyReader, submit *contractSubmitFake, _ *contractQueryFake) {
		t.Helper()
		if submit.calls != 1 {
			return
		}
		if submit.input.ClientID != contractClientID {
			contractFail(t, "submit_input", "Submit did not receive the authenticated client ID")
		}
		if submit.input.BusinessOrderNo != wantBusinessNo {
			contractFail(t, "submit_input", "Submit did not receive the original business order number")
		}
		if submit.input.Amount != wantAmount {
			contractFail(t, "submit_input", "Submit did not receive the integer amount")
		}
		if submit.input.Currency != wantCurrency {
			contractFail(t, "submit_input", "Submit did not receive the exact currency")
		}
	}
}

func contractCheckQueryPaymentID(want string) contractBoundaryCheck {
	return func(t *testing.T, _ *http.Request, _ *contractBodyReader, _ *contractSubmitFake, query *contractQueryFake) {
		t.Helper()
		if query.calls != 1 {
			return
		}
		if query.input.ClientID != contractClientID {
			contractFail(t, "query_input", "Query did not receive the authenticated client ID")
		}
		if !strings.EqualFold(string(query.input.PaymentOrderID), want) {
			contractFail(t, "query_input", "Query did not receive the requested payment ID")
		}
		if query.input.BusinessOrderNo != "" {
			contractFail(t, "query_input", "Query received an unexpected business order number")
		}
	}
}

func contractCheckQueryBusinessNo(want string) contractBoundaryCheck {
	return func(t *testing.T, _ *http.Request, _ *contractBodyReader, _ *contractSubmitFake, query *contractQueryFake) {
		t.Helper()
		if query.calls != 1 {
			return
		}
		if query.input.ClientID != contractClientID {
			contractFail(t, "query_input", "Query did not receive the authenticated client ID")
		}
		if query.input.BusinessOrderNo != want {
			contractFail(t, "query_input", "Query did not receive the original business order number")
		}
		if query.input.PaymentOrderID != "" {
			contractFail(t, "query_input", "Query received an unexpected payment ID")
		}
	}
}

type contractBoundaryCase struct {
	name       string
	method     string
	target     string
	body       []byte
	prepare    func(*http.Request)
	wantStatus int
	wantCode   string
	wantSubmit int
	wantQuery  int
	check      contractBoundaryCheck
}

func contractRunBoundaryCase(t *testing.T, tc contractBoundaryCase) {
	t.Helper()
	submit := &contractSubmitFake{t: t, result: contractSubmitCreated()}
	query := &contractQueryFake{t: t, result: contractQueryFound(payment.ResultSucceeded)}
	router := contractNewRouter(t, submit, query)
	req, body := contractNewRequest(tc.method, tc.target, tc.body)
	if tc.prepare != nil {
		tc.prepare(req)
	}
	recorder := contractDo(router, req)
	if tc.wantCode != "" {
		contractAssertErrorEnvelope(t, recorder, tc.wantStatus, tc.wantCode)
	} else {
		contractAssertBoundarySuccess(t, recorder, tc.wantStatus)
	}
	contractAssertCalls(t, submit, query, tc.wantSubmit, tc.wantQuery)
	if tc.check != nil {
		tc.check(t, req, body, submit, query)
	}
}

func contractValidBody() []byte {
	return contractBodyWith(contractJSONString(contractBusinessNo), "100", contractJSONString("CNY"))
}

func contractBodyWith(orderNoJSON, amountJSON, currencyJSON string) []byte {
	return []byte(fmt.Sprintf(`{"business_order_no":%s,"amount":%s,"currency":%s}`, orderNoJSON, amountJSON, currencyJSON))
}

func contractJSONString(value string) string {
	encoded, _ := json.Marshal(value)
	return string(encoded)
}

func contractPaddedBody(t *testing.T, size int) []byte {
	t.Helper()
	base := contractValidBody()
	if len(base) > size {
		contractFail(t, "fixture_body_size", "base JSON body is larger than the requested boundary")
	}
	padded := make([]byte, 0, size)
	padded = append(padded, base...)
	padded = append(padded, bytes.Repeat([]byte(" "), size-len(base))...)
	return padded
}

func TestPaymentRequestBoundary(t *testing.T) {
	t.Run("route surface", func(t *testing.T) {
		submit := &contractSubmitFake{t: t, result: contractSubmitCreated()}
		query := &contractQueryFake{t: t, result: contractQueryFound(payment.ResultSucceeded)}
		router := contractNewRouter(t, submit, query)
		want := map[string]bool{
			http.MethodPost + " /api/v1/payments":            false,
			http.MethodGet + " /api/v1/payments":             false,
			http.MethodGet + " /api/v1/payments/:payment_id": false,
		}
		routes := router.Routes()
		if len(routes) != len(want) {
			contractFail(t, "route_surface", "route count = %d, want %d", len(routes), len(want))
		}
		for _, route := range routes {
			key := route.Method + " " + route.Path
			if _, ok := want[key]; !ok {
				contractFail(t, "route_surface", "unexpected route registered")
			}
			want[key] = true
		}
		for key, seen := range want {
			if !seen {
				contractFail(t, "route_surface", "expected route is missing: %s", key)
			}
		}
	})

	t.Run("route registration fails closed", func(t *testing.T) {
		validConfig := httpapi.AuthConfig{ClientID: contractClientID, APIKey: contractAPIKey}
		submit := &contractSubmitFake{result: contractSubmitCreated()}
		query := &contractQueryFake{result: contractQueryFound(payment.ResultUnknown)}
		cases := []struct {
			name   string
			router *gin.Engine
			config httpapi.AuthConfig
			submit httpapi.PaymentSubmitter
			query  httpapi.PaymentQuerier
		}{
			{"nil_router", nil, validConfig, submit, query},
			{"invalid_client_id", gin.New(), httpapi.AuthConfig{ClientID: "", APIKey: contractAPIKey}, submit, query},
			{"invalid_api_key", gin.New(), httpapi.AuthConfig{ClientID: contractClientID, APIKey: ""}, submit, query},
			{"nil_submitter", gin.New(), validConfig, nil, query},
			{"nil_querier", gin.New(), validConfig, submit, nil},
			{"nil_dependencies", gin.New(), validConfig, nil, nil},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				var err error
				contractNoPanic(t, "route_registration_panic", func() {
					err = httpapi.RegisterPaymentRoutes(tc.router, tc.config, tc.submit, tc.query)
				})
				if err == nil {
					contractFail(t, "route_registration_error", "RegisterPaymentRoutes accepted invalid input")
				}
				if tc.router != nil && len(tc.router.Routes()) != 0 {
					contractFail(t, "route_registration_leak", "invalid registration left routes behind")
				}
			})
		}
	})

	t.Run("readyz surface unchanged", func(t *testing.T) {
		router := httpapi.NewRouter(func(context.Context) error { return nil })
		routes := router.Routes()
		if len(routes) != 1 || routes[0].Method != http.MethodGet || routes[0].Path != "/readyz" {
			contractFail(t, "readyz_surface", "NewRouter route surface changed")
		}
	})

	t.Run("auth priority", func(t *testing.T) {
		delAuth := func(req *http.Request) { req.Header.Del("Authorization") }
		cases := []contractBoundaryCase{
			{name: "missing_authorization", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: delAuth, wantStatus: http.StatusUnauthorized, wantCode: "UNAUTHORIZED", check: contractCheckNoBodyRead},
			{name: "missing_authorization_oversized", method: http.MethodPost, target: contractPostPath, body: contractPaddedBody(t, 4097), prepare: delAuth, wantStatus: http.StatusUnauthorized, wantCode: "UNAUTHORIZED", check: contractCheckNoBodyRead},
			{name: "missing_authorization_bad_media", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: func(req *http.Request) {
				req.Header.Del("Authorization")
				req.Header.Set("Content-Type", "text/plain")
			}, wantStatus: http.StatusUnauthorized, wantCode: "UNAUTHORIZED", check: contractCheckNoBodyRead},
			{name: "missing_authorization_bad_json", method: http.MethodPost, target: contractPostPath, body: []byte("{"), prepare: delAuth, wantStatus: http.StatusUnauthorized, wantCode: "UNAUTHORIZED", check: contractCheckNoBodyRead},
			{name: "wrong_key", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer "+contractWrongAPIKey)
			}, wantStatus: http.StatusUnauthorized, wantCode: "UNAUTHORIZED", check: contractCheckNoBodyRead},
			{name: "wrong_key_oversized", method: http.MethodPost, target: contractPostPath, body: contractPaddedBody(t, 4097), prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer "+contractWrongAPIKey)
			}, wantStatus: http.StatusUnauthorized, wantCode: "UNAUTHORIZED", check: contractCheckNoBodyRead},
			{name: "wrong_key_bad_media_bad_body", method: http.MethodPost, target: contractPostPath, body: []byte("{"), prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer "+contractWrongAPIKey)
				req.Header.Set("Content-Type", "text/plain")
			}, wantStatus: http.StatusUnauthorized, wantCode: "UNAUTHORIZED", check: contractCheckNoBodyRead},
			{name: "duplicate_authorization", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: func(req *http.Request) {
				req.Header.Add("Authorization", "Bearer "+contractAPIKey)
			}, wantStatus: http.StatusUnauthorized, wantCode: "UNAUTHORIZED", check: contractCheckNoBodyRead},
			{name: "basic_scheme", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Basic ZmFrZQ==")
			}, wantStatus: http.StatusUnauthorized, wantCode: "UNAUTHORIZED", check: contractCheckNoBodyRead},
			{name: "empty_bearer_token", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer ")
			}, wantStatus: http.StatusUnauthorized, wantCode: "UNAUTHORIZED", check: contractCheckNoBodyRead},
			{name: "malformed_bearer", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer"+contractAPIKey)
			}, wantStatus: http.StatusUnauthorized, wantCode: "UNAUTHORIZED", check: contractCheckNoBodyRead},
			{name: "empty_authorization", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "")
			}, wantStatus: http.StatusUnauthorized, wantCode: "UNAUTHORIZED", check: contractCheckNoBodyRead},
			{name: "get_by_id_missing_authorization", method: http.MethodGet, target: contractGetByIDPath, prepare: delAuth, wantStatus: http.StatusUnauthorized, wantCode: "UNAUTHORIZED", check: contractCheckNoBodyRead},
			{name: "get_by_id_wrong_key_non_empty_body", method: http.MethodGet, target: contractGetByIDPath, body: []byte("x"), prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer "+contractWrongAPIKey)
			}, wantStatus: http.StatusUnauthorized, wantCode: "UNAUTHORIZED", check: contractCheckNoBodyRead},
			{name: "get_collection_missing_authorization_bad_query", method: http.MethodGet, target: contractPostPath + "?page=1", prepare: delAuth, wantStatus: http.StatusUnauthorized, wantCode: "UNAUTHORIZED", check: contractCheckNoBodyRead},
			{name: "get_collection_wrong_key_duplicate_query", method: http.MethodGet, target: contractPostPath + "?business_order_no=M100&business_order_no=M101", prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer "+contractWrongAPIKey)
			}, wantStatus: http.StatusUnauthorized, wantCode: "UNAUTHORIZED", check: contractCheckNoBodyRead},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) { contractRunBoundaryCase(t, tc) })
		}
	})

	t.Run("auth accepted forms", func(t *testing.T) {
		cases := []contractBoundaryCase{
			{name: "lowercase_scheme", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "bearer "+contractAPIKey)
			}, wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput(contractBusinessNo, 100, "CNY")},
			{name: "uppercase_scheme", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "BEARER "+contractAPIKey)
			}, wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput(contractBusinessNo, 100, "CNY")},
			{name: "lowercase_header_name", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: func(req *http.Request) {
				req.Header.Del("Authorization")
				req.Header["authorization"] = []string{"Bearer " + contractAPIKey}
			}, wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput(contractBusinessNo, 100, "CNY")},
			{name: "multiple_spaces_after_scheme", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: func(req *http.Request) {
				req.Header.Set("Authorization", "Bearer     "+contractAPIKey)
			}, wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput(contractBusinessNo, 100, "CNY")},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) { contractRunBoundaryCase(t, tc) })
		}
	})

	t.Run("POST media type", func(t *testing.T) {
		setContentType := func(value string) func(*http.Request) {
			return func(req *http.Request) { req.Header.Set("Content-Type", value) }
		}
		duplicateContentType := func(req *http.Request) {
			req.Header.Add("Content-Type", "application/json")
		}
		duplicateDifferentCase := func(req *http.Request) {
			req.Header["content-type"] = []string{"application/json"}
		}
		cases := []contractBoundaryCase{
			{name: "missing", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: func(req *http.Request) { req.Header.Del("Content-Type") }, wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "text_plain", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setContentType("text/plain"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "form_urlencoded", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setContentType("application/x-www-form-urlencoded"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "charset_utf16", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setContentType("application/json; charset=utf-16"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "charset_iso88591", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setContentType("application/json; charset=iso-8859-1"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "extra_parameter_after_charset", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setContentType("application/json; charset=utf-8; profile=x"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "extra_parameter_before_charset", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setContentType("application/json; profile=x; charset=utf-8"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "duplicate_charset_parameter", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setContentType("application/json; charset=utf-8; charset=utf-8"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "duplicate_charset_different_case", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setContentType("application/json; charset=utf-8; Charset=UTF-8"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "duplicate_header", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: duplicateContentType, wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "duplicate_header_different_case", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: duplicateDifferentCase, wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "multiple_media_types", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setContentType("application/json, text/plain"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "repeated_media_type", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setContentType("application/json, application/json"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "json_patch_media_type", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setContentType("application/json-patch+json"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "wildcard_subtype", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setContentType("application/*"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "missing_slash", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setContentType("applicationjson"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "empty_charset_parameter", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setContentType("application/json; charset"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "empty_charset_value", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setContentType("application/json; charset="), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "exact_application_json", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput(contractBusinessNo, 100, "CNY")},
			{name: "charset_utf8", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setContentType("application/json; charset=utf-8"), wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput(contractBusinessNo, 100, "CNY")},
			{name: "charset_utf8_no_space", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setContentType("application/json;charset=utf-8"), wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput(contractBusinessNo, 100, "CNY")},
			{name: "case_insensitive_media_and_charset", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setContentType("Application/JSON; Charset=UTF-8"), wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput(contractBusinessNo, 100, "CNY")},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) { contractRunBoundaryCase(t, tc) })
		}
	})

	t.Run("POST content encoding", func(t *testing.T) {
		setEncoding := func(value string) func(*http.Request) {
			return func(req *http.Request) { req.Header.Set("Content-Encoding", value) }
		}
		cases := []contractBoundaryCase{
			{name: "absent", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput(contractBusinessNo, 100, "CNY")},
			{name: "identity", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setEncoding("identity"), wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput(contractBusinessNo, 100, "CNY")},
			{name: "identity_uppercase", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setEncoding("Identity"), wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput(contractBusinessNo, 100, "CNY")},
			{name: "gzip", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setEncoding("gzip"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "br", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setEncoding("br"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "deflate", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setEncoding("deflate"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "identity_then_gzip", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setEncoding("identity, gzip"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "gzip_then_identity", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setEncoding("gzip, identity"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "repeated_identity", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setEncoding("identity, identity"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "duplicate_identity_header", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: func(req *http.Request) {
				req.Header.Set("Content-Encoding", "identity")
				req.Header.Add("Content-Encoding", "identity")
			}, wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "empty_value", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setEncoding(""), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "unknown_coding", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), prepare: setEncoding("x-identity"), wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) { contractRunBoundaryCase(t, tc) })
		}
	})

	t.Run("POST query parameters", func(t *testing.T) {
		cases := []contractBoundaryCase{
			{name: "client_id", method: http.MethodPost, target: contractPostPath + "?client_id=other", body: contractValidBody(), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "page", method: http.MethodPost, target: contractPostPath + "?page=1", body: contractValidBody(), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "business_order_no", method: http.MethodPost, target: contractPostPath + "?business_order_no=M100", body: contractValidBody(), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "amount", method: http.MethodPost, target: contractPostPath + "?amount=100", body: contractValidBody(), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "currency", method: http.MethodPost, target: contractPostPath + "?currency=CNY", body: contractValidBody(), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "empty_unknown", method: http.MethodPost, target: contractPostPath + "?foo=", body: contractValidBody(), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "multiple_unknown", method: http.MethodPost, target: contractPostPath + "?client_id=other&business_order_no=M100", body: contractValidBody(), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "empty_query_marker", method: http.MethodPost, target: contractPostPath + "?", body: contractValidBody(), wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput(contractBusinessNo, 100, "CNY")},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) { contractRunBoundaryCase(t, tc) })
		}
	})

	t.Run("POST JSON structure", func(t *testing.T) {
		valid := contractValidBody()
		invalidUTF8 := append([]byte(`{"business_order_no":"M`), 0xff)
		invalidUTF8 = append(invalidUTF8, []byte(`100","amount":100,"currency":"CNY"}`)...)
		cases := []contractBoundaryCase{
			{name: "nil_body", method: http.MethodPost, target: contractPostPath, body: nil, wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "empty_body", method: http.MethodPost, target: contractPostPath, body: []byte{}, wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "whitespace_only", method: http.MethodPost, target: contractPostPath, body: []byte(" \n\t\r "), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "top_level_null", method: http.MethodPost, target: contractPostPath, body: []byte("null"), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "top_level_array", method: http.MethodPost, target: contractPostPath, body: []byte("[]"), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "top_level_object_empty", method: http.MethodPost, target: contractPostPath, body: []byte("{}"), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "top_level_string", method: http.MethodPost, target: contractPostPath, body: []byte(`"M100"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "top_level_number", method: http.MethodPost, target: contractPostPath, body: []byte("100"), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "top_level_boolean", method: http.MethodPost, target: contractPostPath, body: []byte("true"), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "multiple_objects", method: http.MethodPost, target: contractPostPath, body: append(append([]byte{}, valid...), valid...), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "trailing_non_whitespace", method: http.MethodPost, target: contractPostPath, body: append(append([]byte{}, valid...), []byte("x")...), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "trailing_object", method: http.MethodPost, target: contractPostPath, body: append(append([]byte{}, valid...), []byte("{}")...), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "utf8_bom", method: http.MethodPost, target: contractPostPath, body: append([]byte{0xef, 0xbb, 0xbf}, valid...), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "invalid_utf8", method: http.MethodPost, target: contractPostPath, body: invalidUTF8, wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "json_comment", method: http.MethodPost, target: contractPostPath, body: append(append([]byte{}, valid...), []byte("/*x*/")...), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "leading_and_trailing_whitespace", method: http.MethodPost, target: contractPostPath, body: append(append([]byte(" \n\t"), valid...), []byte(" \r\n\t")...), wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput(contractBusinessNo, 100, "CNY")},
			{name: "trailing_newline", method: http.MethodPost, target: contractPostPath, body: append(append([]byte{}, valid...), '\n'), wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput(contractBusinessNo, 100, "CNY")},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) { contractRunBoundaryCase(t, tc) })
		}
	})

	t.Run("POST field presence and duplicates", func(t *testing.T) {
		cases := []contractBoundaryCase{
			{name: "missing_business_order_no", method: http.MethodPost, target: contractPostPath, body: []byte(`{"amount":100,"currency":"CNY"}`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "missing_amount", method: http.MethodPost, target: contractPostPath, body: []byte(`{"business_order_no":"M100","currency":"CNY"}`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "missing_currency", method: http.MethodPost, target: contractPostPath, body: []byte(`{"business_order_no":"M100","amount":100}`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "null_business_order_no", method: http.MethodPost, target: contractPostPath, body: contractBodyWith("null", "100", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "null_amount", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "null", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "null_currency", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "100", "null"), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "unknown_client_id", method: http.MethodPost, target: contractPostPath, body: []byte(`{"business_order_no":"M100","amount":100,"currency":"CNY","client_id":"other"}`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "unknown_payment_id", method: http.MethodPost, target: contractPostPath, body: []byte(`{"business_order_no":"M100","amount":100,"currency":"CNY","payment_id":"8b3e19bd-909a-467d-94c7-bf713984f03c"}`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "unknown_result", method: http.MethodPost, target: contractPostPath, body: []byte(`{"business_order_no":"M100","amount":100,"currency":"CNY","result":"SUCCEEDED"}`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "unknown_case_variant", method: http.MethodPost, target: contractPostPath, body: []byte(`{"Business_Order_No":"M100","amount":100,"currency":"CNY"}`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "duplicate_business_order_no", method: http.MethodPost, target: contractPostPath, body: []byte(`{"business_order_no":"M100","business_order_no":"M100","amount":100,"currency":"CNY"}`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "duplicate_amount", method: http.MethodPost, target: contractPostPath, body: []byte(`{"business_order_no":"M100","amount":100,"amount":100,"currency":"CNY"}`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "duplicate_currency", method: http.MethodPost, target: contractPostPath, body: []byte(`{"business_order_no":"M100","amount":100,"currency":"CNY","currency":"CNY"}`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "duplicate_escaped_key", method: http.MethodPost, target: contractPostPath, body: []byte(`{"business_order_no":"M100","business_order_\u006e\u006f":"M100","amount":100,"currency":"CNY"}`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) { contractRunBoundaryCase(t, tc) })
		}
	})

	t.Run("POST field types", func(t *testing.T) {
		cases := []contractBoundaryCase{
			{name: "business_order_no_number", method: http.MethodPost, target: contractPostPath, body: contractBodyWith("100", "100", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "business_order_no_boolean", method: http.MethodPost, target: contractPostPath, body: contractBodyWith("true", "100", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "business_order_no_array", method: http.MethodPost, target: contractPostPath, body: contractBodyWith("[]", "100", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "business_order_no_object", method: http.MethodPost, target: contractPostPath, body: contractBodyWith("{}", "100", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "amount_string", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, `"100"`, `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "amount_boolean", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "true", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "amount_array", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "[]", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "amount_object", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "{}", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "currency_number", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "100", "100"), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "currency_boolean", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "100", "true"), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "currency_array", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "100", "[]"), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "currency_object", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "100", "{}"), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) { contractRunBoundaryCase(t, tc) })
		}
	})

	t.Run("POST business order no", func(t *testing.T) {
		valid64 := strings.Repeat("A", 64)
		invalid65 := strings.Repeat("A", 65)
		cases := []contractBoundaryCase{
			{name: "valid", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(contractJSONString("M100"), "100", `"CNY"`), wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput("M100", 100, "CNY")},
			{name: "case_preserved", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(contractJSONString("m100"), "100", `"CNY"`), wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput("m100", 100, "CNY")},
			{name: "valid_64_chars", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(contractJSONString(valid64), "100", `"CNY"`), wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput(valid64, 100, "CNY")},
			{name: "valid_hyphen_underscore", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(contractJSONString("M-100_A"), "100", `"CNY"`), wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput("M-100_A", 100, "CNY")},
			{name: "empty", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(contractJSONString(""), "100", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "too_long", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(contractJSONString(invalid65), "100", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "leading_space", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(contractJSONString(" M100"), "100", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "trailing_space", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(contractJSONString("M100 "), "100", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "slash", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(contractJSONString("M/100"), "100", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "dot", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(contractJSONString("M.100"), "100", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "inner_space", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(contractJSONString("M 100"), "100", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "non_ascii", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(contractJSONString("订单100"), "100", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "colon", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(contractJSONString("M:100"), "100", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "newline", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(contractJSONString("M100\n"), "100", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "tab", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(contractJSONString("M100\t"), "100", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) { contractRunBoundaryCase(t, tc) })
		}
	})

	t.Run("POST amount", func(t *testing.T) {
		cases := []contractBoundaryCase{
			{name: "minimum", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "1", `"CNY"`), wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput("M100", 1, "CNY")},
			{name: "typical", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "100", `"CNY"`), wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput("M100", 100, "CNY")},
			{name: "maximum", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "1000000", `"CNY"`), wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput("M100", 1000000, "CNY")},
			{name: "zero", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "0", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", wantSubmit: 1, check: contractCheckSubmitInput("M100", 0, "CNY")},
			{name: "negative", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "-1", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", wantSubmit: 1, check: contractCheckSubmitInput("M100", -1, "CNY")},
			{name: "above_maximum", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "1000001", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", wantSubmit: 1, check: contractCheckSubmitInput("M100", 1000001, "CNY")},
			{name: "string", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, `"100"`, `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "decimal", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "100.0", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "exponent", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "1e2", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "uppercase_exponent", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "1E2", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "trailing_decimal_zeros", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "100.00", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "decimal_one", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "1.0", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "exponent_zero", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "1e0", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "negative_zero", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "-0", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "int64_overflow", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "9223372036854775808", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "uint64_overflow", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "18446744073709551616", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "exponent_overflow", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "1e999", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "leading_zero", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "01", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "plus_sign", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "+100", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "fraction", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "0.1", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "underscore", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "1_000", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "null", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "null", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "boolean", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "true", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "array", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "[]", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "object", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "{}", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "exponent_plus", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "1e+2", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "exponent_minus", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "1e-2", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "decimal_exponent", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "1.5e2", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) { contractRunBoundaryCase(t, tc) })
		}
	})

	t.Run("POST currency", func(t *testing.T) {
		cases := []contractBoundaryCase{
			{name: "CNY", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "100", `"CNY"`), wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput("M100", 100, "CNY")},
			{name: "lowercase", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "100", `"cny"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", wantSubmit: 1, check: contractCheckSubmitInput("M100", 100, "cny")},
			{name: "mixed_case", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "100", `"Cny"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", wantSubmit: 1, check: contractCheckSubmitInput("M100", 100, "Cny")},
			{name: "trailing_space", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "100", `"CNY "`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", wantSubmit: 1, check: contractCheckSubmitInput("M100", 100, "CNY ")},
			{name: "leading_space", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "100", `" CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", wantSubmit: 1, check: contractCheckSubmitInput("M100", 100, " CNY")},
			{name: "USD", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "100", `"USD"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", wantSubmit: 1, check: contractCheckSubmitInput("M100", 100, "USD")},
			{name: "empty", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "100", `""`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", wantSubmit: 1, check: contractCheckSubmitInput("M100", 100, "")},
			{name: "newline", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "100", `"CNY\n"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", wantSubmit: 1, check: contractCheckSubmitInput("M100", 100, "CNY\n")},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) { contractRunBoundaryCase(t, tc) })
		}
	})

	t.Run("POST business value delegation", func(t *testing.T) {
		cases := []contractBoundaryCase{
			{name: "amount_below_range_reaches_submit", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "0", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", wantSubmit: 1, check: contractCheckSubmitInput("M100", 0, "CNY")},
			{name: "amount_negative_reaches_submit", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "-1", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", wantSubmit: 1, check: contractCheckSubmitInput("M100", -1, "CNY")},
			{name: "amount_above_range_reaches_submit", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "1000001", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", wantSubmit: 1, check: contractCheckSubmitInput("M100", 1000001, "CNY")},
			{name: "currency_non_cny_reaches_submit", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "100", `"USD"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", wantSubmit: 1, check: contractCheckSubmitInput("M100", 100, "USD")},
			{name: "amount_decimal_rejected_before_submit", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "100.0", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", wantSubmit: 0},
			{name: "amount_exponent_rejected_before_submit", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "1e2", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", wantSubmit: 0},
			{name: "amount_negative_zero_rejected_before_submit", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "-0", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", wantSubmit: 0},
			{name: "amount_overflow_rejected_before_submit", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(`"M100"`, "9223372036854775808", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", wantSubmit: 0},
			{name: "business_order_no_format_rejected_before_submit", method: http.MethodPost, target: contractPostPath, body: contractBodyWith(contractJSONString("M 100"), "100", `"CNY"`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", wantSubmit: 0},
			{name: "unknown_field_rejected_before_submit", method: http.MethodPost, target: contractPostPath, body: []byte(`{"business_order_no":"M100","amount":100,"currency":"CNY","client_id":"other"}`), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", wantSubmit: 0},
			{name: "valid_values_reach_submit_once", method: http.MethodPost, target: contractPostPath, body: contractValidBody(), wantStatus: http.StatusCreated, wantSubmit: 1, check: contractCheckSubmitInput(contractBusinessNo, 100, "CNY")},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) { contractRunBoundaryCase(t, tc) })
		}
	})

	t.Run("POST body size", func(t *testing.T) {
		exact4096 := contractPaddedBody(t, 4096)
		over4097 := contractPaddedBody(t, 4097)
		cases := []contractBoundaryCase{
			{name: "exact_4096", method: http.MethodPost, target: contractPostPath, body: exact4096, wantStatus: http.StatusCreated, wantSubmit: 1, check: func(t *testing.T, _ *http.Request, body *contractBodyReader, submit *contractSubmitFake, _ *contractQueryFake) {
				contractCheckReadRange(1, 4096)(t, nil, body, submit, nil)
				contractCheckSubmitInput(contractBusinessNo, 100, "CNY")(t, nil, nil, submit, nil)
			}},
			{name: "over_4097", method: http.MethodPost, target: contractPostPath, body: over4097, wantStatus: http.StatusRequestEntityTooLarge, wantCode: "INVALID_REQUEST", check: contractCheckReadRange(0, 4097)},
			{name: "unknown_length_4096", method: http.MethodPost, target: contractPostPath, body: exact4096, prepare: func(req *http.Request) {
				req.ContentLength = -1
				req.Header.Del("Content-Length")
			}, wantStatus: http.StatusCreated, wantSubmit: 1, check: func(t *testing.T, _ *http.Request, body *contractBodyReader, submit *contractSubmitFake, _ *contractQueryFake) {
				contractCheckReadRange(1, 4096)(t, nil, body, submit, nil)
				contractCheckSubmitInput(contractBusinessNo, 100, "CNY")(t, nil, nil, submit, nil)
			}},
			{name: "unknown_length_4097", method: http.MethodPost, target: contractPostPath, body: over4097, prepare: func(req *http.Request) {
				req.ContentLength = -1
				req.Header.Del("Content-Length")
			}, wantStatus: http.StatusRequestEntityTooLarge, wantCode: "INVALID_REQUEST", check: contractCheckReadRange(4097, 4097)},
			{name: "lying_small_length_4097", method: http.MethodPost, target: contractPostPath, body: over4097, prepare: func(req *http.Request) {
				req.ContentLength = 100
				req.Header.Set("Content-Length", "100")
			}, wantStatus: http.StatusRequestEntityTooLarge, wantCode: "INVALID_REQUEST", check: contractCheckReadRange(4097, 4097)},
			{name: "over_4097_bad_media_precedes_size", method: http.MethodPost, target: contractPostPath, body: over4097, prepare: func(req *http.Request) {
				req.Header.Set("Content-Type", "text/plain")
			}, wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "over_4097_bad_encoding_precedes_size", method: http.MethodPost, target: contractPostPath, body: over4097, prepare: func(req *http.Request) {
				req.Header.Set("Content-Encoding", "gzip")
			}, wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) { contractRunBoundaryCase(t, tc) })
		}
	})

	t.Run("POST rejection priority", func(t *testing.T) {
		exact4096 := contractPaddedBody(t, 4096)
		over4097 := contractPaddedBody(t, 4097)
		delAuth := func(req *http.Request) { req.Header.Del("Authorization") }
		setBadMedia := func(req *http.Request) { req.Header.Set("Content-Type", "text/plain") }
		setBadEncoding := func(req *http.Request) { req.Header.Set("Content-Encoding", "gzip") }
		unknownLength := func(req *http.Request) {
			req.ContentLength = -1
			req.Header.Del("Content-Length")
		}
		misleadingLength := func(req *http.Request) {
			req.ContentLength = 100
			req.Header.Set("Content-Length", "100")
		}
		cases := []contractBoundaryCase{
			{name: "query_actual_4097_413", method: http.MethodPost, target: contractPostPath + "?page=1", body: over4097, wantStatus: http.StatusRequestEntityTooLarge, wantCode: "INVALID_REQUEST", check: contractCheckReadRange(0, 4097)},
			{name: "query_exact_4096_400", method: http.MethodPost, target: contractPostPath + "?page=1", body: exact4096, wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST", check: contractCheckReadRange(0, 4096)},
			{name: "no_auth_query_oversize_401", method: http.MethodPost, target: contractPostPath + "?page=1", body: over4097, prepare: delAuth, wantStatus: http.StatusUnauthorized, wantCode: "UNAUTHORIZED", check: contractCheckNoBodyRead},
			{name: "bad_media_query_oversize_415", method: http.MethodPost, target: contractPostPath + "?page=1", body: over4097, prepare: setBadMedia, wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "bad_encoding_query_oversize_415", method: http.MethodPost, target: contractPostPath + "?page=1", body: over4097, prepare: setBadEncoding, wantStatus: http.StatusUnsupportedMediaType, wantCode: "INVALID_REQUEST", check: contractCheckNoBodyRead},
			{name: "unknown_length_query_oversize_413", method: http.MethodPost, target: contractPostPath + "?page=1", body: over4097, prepare: unknownLength, wantStatus: http.StatusRequestEntityTooLarge, wantCode: "INVALID_REQUEST", check: contractCheckReadRange(4097, 4097)},
			{name: "misleading_length_query_oversize_413", method: http.MethodPost, target: contractPostPath + "?page=1", body: over4097, prepare: misleadingLength, wantStatus: http.StatusRequestEntityTooLarge, wantCode: "INVALID_REQUEST", check: contractCheckReadRange(4097, 4097)},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) { contractRunBoundaryCase(t, tc) })
		}
	})

	t.Run("GET by payment id", func(t *testing.T) {
		upperID := strings.ToUpper(string(contractOrderID))
		cases := []contractBoundaryCase{
			{name: "lowercase_uuidv4", method: http.MethodGet, target: contractGetByIDPath, wantStatus: http.StatusOK, wantQuery: 1, check: contractCheckQueryPaymentID(string(contractOrderID))},
			{name: "uppercase_uuidv4", method: http.MethodGet, target: "/api/v1/payments/" + upperID, wantStatus: http.StatusOK, wantQuery: 1, check: contractCheckQueryPaymentID(upperID)},
			{name: "short", method: http.MethodGet, target: "/api/v1/payments/8b3e19bd-909a-467d-94c7-bf713984f03", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "long", method: http.MethodGet, target: "/api/v1/payments/8b3e19bd-909a-467d-94c7-bf713984f03ca", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "braces", method: http.MethodGet, target: "/api/v1/payments/%7B8b3e19bd-909a-467d-94c7-bf713984f03c%7D", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "no_hyphens", method: http.MethodGet, target: "/api/v1/payments/8b3e19bd909a467d94c7bf713984f03c", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "version_3", method: http.MethodGet, target: "/api/v1/payments/8b3e19bd-909a-367d-94c7-bf713984f03c", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "variant_7", method: http.MethodGet, target: "/api/v1/payments/8b3e19bd-909a-467d-74c7-bf713984f03c", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "non_hex", method: http.MethodGet, target: "/api/v1/payments/gb3e19bd-909a-467d-94c7-bf713984f03c", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "query_business_order_no", method: http.MethodGet, target: contractGetByIDPath + "?business_order_no=M100", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "query_page", method: http.MethodGet, target: contractGetByIDPath + "?page=1", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "query_client_id", method: http.MethodGet, target: contractGetByIDPath + "?client_id=other", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "non_empty_body", method: http.MethodGet, target: contractGetByIDPath, body: []byte("x"), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "non_empty_json_body", method: http.MethodGet, target: contractGetByIDPath, body: []byte("{}"), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "content_type_not_required", method: http.MethodGet, target: contractGetByIDPath, prepare: func(req *http.Request) {
				req.Header.Set("Content-Type", "text/plain")
			}, wantStatus: http.StatusOK, wantQuery: 1, check: contractCheckQueryPaymentID(string(contractOrderID))},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) { contractRunBoundaryCase(t, tc) })
		}
	})

	t.Run("GET by business order no", func(t *testing.T) {
		valid64 := strings.Repeat("A", 64)
		invalid65 := strings.Repeat("A", 65)
		cases := []contractBoundaryCase{
			{name: "valid", method: http.MethodGet, target: contractPostPath + "?business_order_no=M100", wantStatus: http.StatusOK, wantQuery: 1, check: contractCheckQueryBusinessNo("M100")},
			{name: "case_preserved", method: http.MethodGet, target: contractPostPath + "?business_order_no=m100", wantStatus: http.StatusOK, wantQuery: 1, check: contractCheckQueryBusinessNo("m100")},
			{name: "valid_64_chars", method: http.MethodGet, target: contractPostPath + "?business_order_no=" + valid64, wantStatus: http.StatusOK, wantQuery: 1, check: contractCheckQueryBusinessNo(valid64)},
			{name: "valid_hyphen_underscore", method: http.MethodGet, target: contractPostPath + "?business_order_no=M-100_A", wantStatus: http.StatusOK, wantQuery: 1, check: contractCheckQueryBusinessNo("M-100_A")},
			{name: "missing", method: http.MethodGet, target: contractPostPath, wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "empty_value", method: http.MethodGet, target: contractPostPath + "?business_order_no=", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "key_without_value", method: http.MethodGet, target: contractPostPath + "?business_order_no", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "duplicate_same_value", method: http.MethodGet, target: contractPostPath + "?business_order_no=M100&business_order_no=M100", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "duplicate_different_value", method: http.MethodGet, target: contractPostPath + "?business_order_no=M100&business_order_no=M101", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "duplicate_with_empty", method: http.MethodGet, target: contractPostPath + "?business_order_no=M100&business_order_no=", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "unknown_page", method: http.MethodGet, target: contractPostPath + "?page=1", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "unknown_limit", method: http.MethodGet, target: contractPostPath + "?limit=1", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "unknown_with_business_no", method: http.MethodGet, target: contractPostPath + "?business_order_no=M100&page=1", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "client_id_with_business_no", method: http.MethodGet, target: contractPostPath + "?business_order_no=M100&client_id=other", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "malformed_escape", method: http.MethodGet, target: contractPostPath, prepare: func(req *http.Request) { req.URL.RawQuery = "business_order_no=%zz" }, wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "leading_space", method: http.MethodGet, target: contractPostPath + "?business_order_no=%20M100", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "trailing_space", method: http.MethodGet, target: contractPostPath + "?business_order_no=M100%20", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "slash", method: http.MethodGet, target: contractPostPath + "?business_order_no=M%2F100", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "dot", method: http.MethodGet, target: contractPostPath + "?business_order_no=M.100", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "too_long", method: http.MethodGet, target: contractPostPath + "?business_order_no=" + invalid65, wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "non_ascii", method: http.MethodGet, target: contractPostPath + "?business_order_no=%E8%AE%A2%E5%8D%95100", wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "non_empty_body", method: http.MethodGet, target: contractPostPath + "?business_order_no=M100", body: []byte("x"), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "non_empty_json_body", method: http.MethodGet, target: contractPostPath + "?business_order_no=M100", body: []byte("{}"), wantStatus: http.StatusBadRequest, wantCode: "INVALID_REQUEST"},
			{name: "content_type_not_required", method: http.MethodGet, target: contractPostPath + "?business_order_no=M100", prepare: func(req *http.Request) {
				req.Header.Set("Content-Type", "text/plain")
			}, wantStatus: http.StatusOK, wantQuery: 1, check: contractCheckQueryBusinessNo("M100")},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) { contractRunBoundaryCase(t, tc) })
		}
	})
}
