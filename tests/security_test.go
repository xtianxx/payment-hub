//go:build integration

package tests

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"payment-hub/internal/app"
	"payment-hub/internal/httpapi"
	"payment-hub/internal/payment"
	"payment-hub/tests/harness"
)

const (
	secClientID      = "us1-boundary-client"
	secChallenge     = `Bearer realm="payment-hub"`
	secPostPath      = "/api/v1/payments"
	secCanaryMarker  = "T070_FAKE_LEAK_CANARY_5f2b7d"
	secCanaryAPIKey  = "FAKE-T070-wrong_key.9~a+/b=="
	secCanaryBodyTag = "T070_FAKE_BODY_CANARY_9c4e1a"
	secCanaryOrderNo = "T070 CANARY " + secCanaryMarker
	secProcessLife   = 60 * time.Second
	secProcessGrace  = 10 * time.Second
)

var secValidPaymentID = string(us1BoundaryOrder().ID)

// TestCrossEntrySecurity is the T070 cross-story security regression. It
// reuses the existing US1 HTTP, runtime-harness, PostgreSQL, and process
// helpers instead of duplicating the full story suites.
//
// A and C are self-contained and do not require PostgreSQL, a process, or
// runtime artifacts. B is the real-binary/fresh-PostgreSQL lane and D validates
// the runtime-harness report produced by B.
func TestCrossEntrySecurity(t *testing.T) {
	if !t.Run("A_fake_router_cross_entry_boundary", secRunFakeRouterBoundary) {
		return
	}
	if !t.Run("C_invalid_runtime_config_canary", secRunInvalidRuntimeConfig) {
		return
	}

	var expectation secReportExpectation
	if !t.Run("B_real_app_pg_canary_isolation", func(t *testing.T) {
		expectation = secRunRealAppCanaryIsolation(t)
	}) {
		return
	}
	t.Run("D_runtime_harness_report_schema", func(t *testing.T) {
		secValidateRuntimeHarnessReport(t, expectation)
	})
}

type secCountingBody struct {
	reader io.Reader
	reads  int
}

func (body *secCountingBody) Read(buffer []byte) (int, error) {
	body.reads++
	return body.reader.Read(buffer)
}

func (body *secCountingBody) Close() error {
	return nil
}

func secForbiddenMarkers() []string {
	return []string{
		secCanaryMarker,
		secCanaryAPIKey,
		secCanaryBodyTag,
		secCanaryOrderNo,
		us1APIKey,
		"Authorization",
		"Bearer ",
		"postgres://",
		"postgresql://",
		"password",
		"SELECT ",
		"INSERT ",
		"DELETE ",
		"UPDATE ",
	}
}

func secAssertNoLeak(t *testing.T, data []byte, extra ...string) {
	t.Helper()
	markers := append(secForbiddenMarkers(), extra...)
	for _, marker := range markers {
		if marker == "" {
			continue
		}
		if bytes.Contains(data, []byte(marker)) {
			t.Fatal("T070 sensitive marker leaked into checked output")
		}
	}
}

func secRequireError(t *testing.T, status int, header http.Header, body []byte, wantStatus int, wantCode string) string {
	t.Helper()
	if status != wantStatus {
		t.Fatalf("T070 error status mismatch: got %d want %d", status, wantStatus)
	}
	if !strings.HasPrefix(header.Get("Content-Type"), "application/json") {
		t.Fatal("T070 error response content type mismatch")
	}
	if !strings.Contains(header.Get("Cache-Control"), "no-store") {
		t.Fatal("T070 error response cache policy mismatch")
	}
	if header.Get("ETag") != "" || header.Get("Last-Modified") != "" {
		t.Fatal("T070 error response unexpectedly carried a validator")
	}
	if wantStatus == http.StatusUnauthorized {
		challenges := header.Values("WWW-Authenticate")
		if len(challenges) != 1 || challenges[0] != secChallenge {
			t.Fatal("T070 unauthorized response challenge mismatch")
		}
	}

	var raw map[string]json.RawMessage
	if err := json.Unmarshal(body, &raw); err != nil {
		t.Fatal("T070 error response is not a JSON object")
	}
	if len(raw) != 2 {
		t.Fatal("T070 error response must contain exactly request_id and error")
	}
	if _, ok := raw["request_id"]; !ok {
		t.Fatal("T070 error response is missing request_id")
	}
	if _, ok := raw["error"]; !ok {
		t.Fatal("T070 error response is missing error")
	}

	var envelope us1Envelope
	if err := json.Unmarshal(body, &envelope); err != nil {
		t.Fatal("T070 error response does not match the request_id plus error wrapper")
	}
	if envelope.RequestID == "" || payment.ValidateUUIDv4(envelope.RequestID) != nil {
		t.Fatal("T070 error response request_id is not a lowercase UUIDv4")
	}
	if envelope.Payment != nil || len(envelope.Issues) != 0 || envelope.Error == nil {
		t.Fatal("T070 error response envelope mismatch")
	}
	if envelope.Error.Code != wantCode || envelope.Error.Message == "" {
		t.Fatal("T070 error response code or message mismatch")
	}
	secAssertNoLeak(t, body)
	return envelope.RequestID
}

func secServe(t *testing.T, router http.Handler, request *http.Request) *httptest.ResponseRecorder {
	t.Helper()
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	return recorder
}

// secRunFakeRouterBoundary uses the actual router with spy business ports. A
// zero Submit/Query count proves that the HTTP boundary did not reach a
// downstream business port; it does not claim that a real SQL read or Mock
// payment was attempted or avoided.
func secRunFakeRouterBoundary(t *testing.T) {
	gin.SetMode(gin.ReleaseMode)
	ports := &us1FakePorts{}
	router := gin.New()
	if err := httpapi.RegisterPaymentRoutes(router, httpapi.AuthConfig{
		ClientID: secClientID,
		APIKey:   us1APIKey,
	}, ports, ports); err != nil {
		t.Fatal("T070 boundary route registration failed")
	}

	t.Run("authorization_cross_entry", func(t *testing.T) {
		entries := []struct {
			name        string
			method      string
			target      string
			body        string
			contentType string
		}{
			{"post", http.MethodPost, secPostPath, us1ValidPostBody, "application/json"},
			{"get_by_id", http.MethodGet, "/api/v1/payments/" + secValidPaymentID, "", ""},
			{"get_by_business", http.MethodGet, secPostPath + "?business_order_no=M100", "", ""},
		}
		authCases := []struct {
			name  string
			apply func(*http.Request)
		}{
			{"missing", func(*http.Request) {}},
			{"wrong", func(request *http.Request) {
				request.Header.Set("Authorization", "Bearer "+secCanaryAPIKey)
			}},
			{"duplicate_values", func(request *http.Request) {
				request.Header["Authorization"] = []string{"Bearer " + us1APIKey, "Bearer " + us1APIKey}
			}},
			{"duplicate_names", func(request *http.Request) {
				request.Header["Authorization"] = []string{"Bearer " + us1APIKey}
				request.Header["authorization"] = []string{"Bearer " + us1APIKey}
			}},
		}

		for _, entry := range entries {
			for _, authCase := range authCases {
				t.Run(entry.name+"_"+authCase.name, func(t *testing.T) {
					ports.reset()
					var body *secCountingBody
					var reader io.Reader
					if entry.body != "" {
						body = &secCountingBody{reader: strings.NewReader(entry.body)}
						reader = body
					}
					request := httptest.NewRequest(entry.method, entry.target, reader)
					if entry.contentType != "" {
						request.Header.Set("Content-Type", entry.contentType)
					}
					authCase.apply(request)
					recorder := secServe(t, router, request)
					secRequireError(t, recorder.Code, recorder.Header(), recorder.Body.Bytes(), http.StatusUnauthorized, "UNAUTHORIZED")
					if body != nil && body.reads != 0 {
						t.Fatal("T070 unauthorized request read the request body")
					}
					submit, query := ports.counts()
					if submit != 0 || query != 0 {
						t.Fatal("T070 unauthorized request reached a business port")
					}
				})
			}
		}
	})

	t.Run("input_rejection_cross_entry", func(t *testing.T) {
		cases := []struct {
			name        string
			method      string
			target      string
			body        string
			contentType string
			wantStatus  int
		}{
			{"invalid_json", http.MethodPost, secPostPath, "{", "application/json", http.StatusBadRequest},
			{"oversize", http.MethodPost, secPostPath, string(us1PaddedPostBody(4097)), "application/json", http.StatusRequestEntityTooLarge},
			{"missing_media_type", http.MethodPost, secPostPath, us1ValidPostBody, "", http.StatusUnsupportedMediaType},
			{"wrong_media_type", http.MethodPost, secPostPath, us1ValidPostBody, "text/plain", http.StatusUnsupportedMediaType},
			{"invalid_payment_id", http.MethodGet, secPostPath + "/not-a-uuid", "", "", http.StatusBadRequest},
			{"unknown_query", http.MethodGet, secPostPath + "?business_order_no=M100&page=1", "", "", http.StatusBadRequest},
			{"get_body", http.MethodGet, secPostPath + "?business_order_no=M100", "{}", "", http.StatusBadRequest},
		}
		for _, testCase := range cases {
			t.Run(testCase.name, func(t *testing.T) {
				ports.reset()
				request := httptest.NewRequest(testCase.method, testCase.target, strings.NewReader(testCase.body))
				if testCase.contentType != "" {
					request.Header.Set("Content-Type", testCase.contentType)
				}
				request.Header.Set("Authorization", "Bearer "+us1APIKey)
				recorder := secServe(t, router, request)
				secRequireError(t, recorder.Code, recorder.Header(), recorder.Body.Bytes(), testCase.wantStatus, "INVALID_REQUEST")
				submit, query := ports.counts()
				if submit != 0 || query != 0 {
					t.Fatal("T070 rejected input reached a business port")
				}
			})
		}
	})

	t.Run("business_error_contract", func(t *testing.T) {
		cases := []struct {
			name        string
			method      string
			target      string
			body        string
			contentType string
			submitErr   error
			queryErr    error
			wantStatus  int
			wantCode    string
			wantSubmit  int
			wantQuery   int
		}{
			{
				name:       "not_found",
				method:     http.MethodGet,
				target:     secPostPath + "?business_order_no=M100",
				queryErr:   payment.ErrNotFound,
				wantStatus: http.StatusNotFound,
				wantCode:   "PAYMENT_NOT_FOUND",
				wantQuery:  1,
			},
			{
				name:        "conflict",
				method:      http.MethodPost,
				target:      secPostPath,
				body:        us1ValidPostBody,
				contentType: "application/json",
				submitErr:   payment.ErrConflict,
				wantStatus:  http.StatusConflict,
				wantCode:    "PAYMENT_CONFLICT",
				wantSubmit:  1,
			},
			{
				name:        "invalid_business_value",
				method:      http.MethodPost,
				target:      secPostPath,
				body:        us1ValidPostBody,
				contentType: "application/json",
				submitErr:   payment.ErrInvalidInput,
				wantStatus:  http.StatusBadRequest,
				wantCode:    "INVALID_REQUEST",
				wantSubmit:  1,
			},
			{
				name:        "internal_error",
				method:      http.MethodPost,
				target:      secPostPath,
				body:        us1ValidPostBody,
				contentType: "application/json",
				submitErr:   errors.New("SELECT * FROM payment_orders WHERE password='" + secCanaryMarker + "'"),
				wantStatus:  http.StatusInternalServerError,
				wantCode:    "PROCESSING_ERROR",
				wantSubmit:  1,
			},
			{
				name:       "unavailable",
				method:     http.MethodGet,
				target:     secPostPath + "/" + secValidPaymentID,
				queryErr:   fmt.Errorf("%w: %s", payment.ErrUnavailable, secCanaryMarker),
				wantStatus: http.StatusServiceUnavailable,
				wantCode:   "PROCESSING_ERROR",
				wantQuery:  1,
			},
		}

		for _, testCase := range cases {
			t.Run(testCase.name, func(t *testing.T) {
				ports.reset()
				ports.submitErr = testCase.submitErr
				ports.queryErr = testCase.queryErr
				request := httptest.NewRequest(testCase.method, testCase.target, strings.NewReader(testCase.body))
				if testCase.contentType != "" {
					request.Header.Set("Content-Type", testCase.contentType)
				}
				request.Header.Set("Authorization", "Bearer "+us1APIKey)
				recorder := secServe(t, router, request)
				secRequireError(t, recorder.Code, recorder.Header(), recorder.Body.Bytes(), testCase.wantStatus, testCase.wantCode)
				submit, query := ports.counts()
				if submit != testCase.wantSubmit || query != testCase.wantQuery {
					t.Fatalf("T070 business error port counts mismatch: submit=%d query=%d", submit, query)
				}
				ports.submitErr = nil
				ports.queryErr = nil
			})
		}
	})
}

func secRunInvalidRuntimeConfig(t *testing.T) {
	validDSN := "postgres://fake:" + secCanaryMarker + "@127.0.0.1:1/payment_hub_test?sslmode=disable"
	validValues := func() map[string]string {
		return map[string]string{
			"PAYMENT_HUB_CLIENT_ID":    "t070-client",
			"PAYMENT_HUB_API_KEY":      us1APIKey,
			"PAYMENT_HUB_DATABASE_URL": validDSN,
			"PAYMENT_HUB_HTTP_ADDR":    "127.0.0.1:1",
		}
	}

	cases := []struct {
		name   string
		values map[string]string
	}{
		{
			name: "invalid_client_id",
			values: func() map[string]string {
				values := validValues()
				values["PAYMENT_HUB_CLIENT_ID"] = "客户-" + secCanaryMarker
				return values
			}(),
		},
		{
			name: "invalid_api_key",
			values: func() map[string]string {
				values := validValues()
				values["PAYMENT_HUB_API_KEY"] = "Bearer " + secCanaryMarker
				return values
			}(),
		},
		{
			name: "invalid_database_url",
			values: func() map[string]string {
				values := validValues()
				values["PAYMENT_HUB_DATABASE_URL"] = "postgres://fake:" + secCanaryMarker + "@127.0.0.1:1/payment_hub_test?sslmode=require"
				return values
			}(),
		},
		{
			name: "missing_database_url",
			values: func() map[string]string {
				values := validValues()
				delete(values, "PAYMENT_HUB_DATABASE_URL")
				return values
			}(),
		},
		{
			name: "invalid_test_mode",
			values: func() map[string]string {
				values := validValues()
				values["PAYMENT_HUB_TEST_MODE"] = "on"
				values["PAYMENT_HUB_TEST_CONTROL_SOCKET"] = "/tmp/" + secCanaryMarker
				return values
			}(),
		},
		{
			name: "injected_pg_environment",
			values: func() map[string]string {
				values := validValues()
				values["PGPASSWORD"] = secCanaryMarker
				return values
			}(),
		},
	}

	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			values := testCase.values
			var writer bytes.Buffer
			err := app.Run(context.Background(), func(name string) (string, bool) {
				value, ok := values[name]
				return value, ok
			}, &writer)
			if !errors.Is(err, app.ErrRuntimeConfig) || err.Error() != app.ErrRuntimeConfig.Error() {
				t.Fatal("T070 invalid configuration did not return the fixed ErrRuntimeConfig")
			}
			if writer.Len() != 0 {
				t.Fatal("T070 invalid configuration wrote runtime output")
			}
			secAssertNoLeak(t, []byte(err.Error()))
			secAssertNoLeak(t, writer.Bytes())
		})
	}
}

type secReportExpectation struct {
	Path      string
	RunID     string
	Statuses  []int
	Forbidden []string
}

func secNormalAppEnv(t *testing.T, dsn, address, clientID string) []string {
	t.Helper()
	return []string{
		"PAYMENT_HUB_CLIENT_ID=" + clientID,
		"PAYMENT_HUB_API_KEY=" + us1APIKey,
		"PAYMENT_HUB_DATABASE_URL=" + dsn,
		"PAYMENT_HUB_HTTP_ADDR=" + address,
		"PAYMENT_HUB_TEST_MODE=off",
		"TMPDIR=" + us1IsolatedTempDir(t),
		"TZ=UTC",
	}
}

func secStartNormalApplication(t *testing.T, artifacts *harness.Artifacts, binary, repoRoot string, env []string) (*harness.Process, context.CancelFunc) {
	t.Helper()
	lifetime, cancelLifetime := context.WithTimeout(context.Background(), secProcessLife)
	process, err := harness.StartProcess(lifetime, harness.ProcessConfig{
		Path:       binary,
		Dir:        repoRoot,
		Env:        env,
		Generation: 1,
		Grace:      secProcessGrace,
		Artifacts:  artifacts,
	})
	if err != nil {
		cancelLifetime()
		t.Fatal("T070 normal-mode process start failed")
	}
	return process, cancelLifetime
}

func secRecordHTTP(t *testing.T, artifacts *harness.Artifacts, status int, requestID, paymentID, result string) {
	t.Helper()
	if err := artifacts.RecordHTTP(harness.HTTPObservation{
		Generation:     1,
		Status:         status,
		RequestID:      requestID,
		PaymentOrderID: paymentID,
		Result:         result,
	}); err != nil {
		t.Fatal("T070 HTTP artifact record failed")
	}
}

func secRecordSQL(t *testing.T, artifacts *harness.Artifacts, rows uint64) {
	t.Helper()
	if err := artifacts.RecordSQL(harness.SQLObservation{
		Generation: 1,
		State:      "00000",
		Rows:       rows,
	}); err != nil {
		t.Fatal("T070 SQL artifact record failed")
	}
}

func secCanaryRequestBody() string {
	return `{"business_order_no":"` + secCanaryMarker + `","amount":100,"currency":"CNY","client_id":"` + secCanaryBodyTag + `"}`
}

func secCanaryPaddedPostBody(size int) []byte {
	base := []byte(`{"business_order_no":"` + secCanaryMarker + `","amount":100,"currency":"CNY"}`)
	if size < len(base) {
		return nil
	}
	body := make([]byte, size)
	copy(body, base)
	for index := len(base); index < size; index++ {
		body[index] = ' '
	}
	return body
}

func secRunCanaryRequests(t *testing.T, address string, artifacts *harness.Artifacts) []int {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	statuses := make([]int, 0, 8)
	record := func(response *http.Response, body []byte, wantStatus int, wantCode string) {
		requestID := secRequireError(t, response.StatusCode, response.Header, body, wantStatus, wantCode)
		secRecordHTTP(t, artifacts, response.StatusCode, requestID, "", "")
		statuses = append(statuses, response.StatusCode)
	}
	do := func(request *http.Request, wantStatus int, wantCode string) {
		response, body, err := us1Do(request)
		if err != nil {
			t.Fatal("T070 canary request failed")
		}
		record(response, body, wantStatus, wantCode)
	}

	request, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://"+address+secPostPath, strings.NewReader(secCanaryRequestBody()))
	if err != nil {
		t.Fatal("T070 missing-auth canary request construction failed")
	}
	request.Header.Set("Content-Type", "application/json")
	do(request, http.StatusUnauthorized, "UNAUTHORIZED")

	request, err = http.NewRequestWithContext(ctx, http.MethodGet, "http://"+address+secPostPath+"/"+secValidPaymentID, nil)
	if err != nil {
		t.Fatal("T070 wrong-auth canary request construction failed")
	}
	request.Header.Set("Authorization", "Bearer "+secCanaryAPIKey)
	do(request, http.StatusUnauthorized, "UNAUTHORIZED")

	request, err = http.NewRequestWithContext(ctx, http.MethodGet, "http://"+address+secPostPath+"?business_order_no=M100", nil)
	if err != nil {
		t.Fatal("T070 duplicate-auth canary request construction failed")
	}
	request.Header["Authorization"] = []string{"Bearer " + us1APIKey, "Bearer " + us1APIKey}
	do(request, http.StatusUnauthorized, "UNAUTHORIZED")

	response, body, err := us1PostBytes(ctx, address, us1APIKey, []byte(secCanaryRequestBody()), "application/json")
	if err != nil {
		t.Fatal("T070 invalid POST canary request failed")
	}
	record(response, body, http.StatusBadRequest, "INVALID_REQUEST")

	response, body, err = us1GetByID(ctx, address, us1APIKey, secCanaryMarker)
	if err != nil {
		t.Fatal("T070 invalid payment id canary request failed")
	}
	record(response, body, http.StatusBadRequest, "INVALID_REQUEST")

	response, body, err = us1GetByBusinessOrderNo(ctx, address, us1APIKey, secCanaryOrderNo)
	if err != nil {
		t.Fatal("T070 invalid business order canary request failed")
	}
	record(response, body, http.StatusBadRequest, "INVALID_REQUEST")

	response, body, err = us1PostBytes(ctx, address, us1APIKey, secCanaryPaddedPostBody(4097), "application/json")
	if err != nil {
		t.Fatal("T070 oversize canary request failed")
	}
	record(response, body, http.StatusRequestEntityTooLarge, "INVALID_REQUEST")

	response, body, err = us1PostBytes(ctx, address, us1APIKey, []byte(secCanaryRequestBody()), "text/plain")
	if err != nil {
		t.Fatal("T070 media type canary request failed")
	}
	record(response, body, http.StatusUnsupportedMediaType, "INVALID_REQUEST")

	return statuses
}

func secRunRealAppCanaryIsolation(t *testing.T) secReportExpectation {
	t.Helper()
	repoRoot := us1RepoRoot(t)
	runID := us1RunID(t)
	artifactRoot := os.Getenv(us1ArtifactRootEnv)
	if artifactRoot == "" {
		t.Fatal("T070 artifact root configuration is required")
	}
	scenarioRoot := filepath.Join(artifactRoot, runID, "scenarios", "cross_entry_security")
	if err := os.MkdirAll(scenarioRoot, 0o700); err != nil {
		t.Fatal("T070 scenario artifact root creation failed")
	}
	if err := us1ValidateArtifactRoot(scenarioRoot); err != nil {
		t.Fatal("T070 scenario artifact root is unsafe")
	}
	artifacts, err := harness.BeginArtifacts(harness.ArtifactConfig{
		Root:       scenarioRoot,
		SourceRoot: repoRoot,
		RunID:      runID,
	})
	if err != nil {
		t.Fatal("T070 runtime-harness artifacts begin failed")
	}

	pool := us1OpenPool(t)
	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	dsn, err := us1BuildAppDSN(pool.Config().ConnConfig)
	if err != nil {
		t.Fatal("T070 app database URL construction failed")
	}

	t.Cleanup(func() {
		us1CleanupClient(t, pool, clientID)
		us1RequireNoBusinessRows(t, pool, clientID)
	})

	binary := us1BuildApplication(t, repoRoot)
	address := us1FreeLoopbackAddr(t)
	env := secNormalAppEnv(t, dsn, address, clientID)
	process, cancelLifetime := secStartNormalApplication(t, artifacts, binary, repoRoot, env)
	t.Cleanup(func() {
		stopCtx, cancelStop := context.WithTimeout(context.Background(), us1StopBudget)
		defer cancelStop()
		exit, stopErr := process.Stop(stopCtx)
		if stopErr != nil && !errors.Is(stopErr, harness.ErrProcess) && !errors.Is(stopErr, harness.ErrArtifacts) {
			t.Errorf("T070 process cleanup returned an unexpected error")
		}
		if !exit.Reaped || !exit.GroupGone {
			t.Errorf("T070 process cleanup did not confirm reaping/group absence")
		}
		cancelLifetime()
	})

	us1WaitReady(t, address)

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	statuses := make([]int, 0, 11)

	response, body, err := us1PostPayment(ctx, address, us1APIKey, businessOrderNo, 100, "CNY")
	if err != nil {
		t.Fatal("T070 baseline POST failed")
	}
	envelope := us1ParseEnvelope(t, body)
	if envelope.Payment == nil {
		t.Fatal("T070 baseline POST did not return a payment")
	}
	paymentID := envelope.Payment.PaymentID
	baselinePayment, requestID := us1RequirePaymentResponse(t, response, body, http.StatusCreated, paymentID, businessOrderNo, 100, "CNY", payment.ResultSucceeded)
	secRecordHTTP(t, artifacts, response.StatusCode, requestID, baselinePayment.PaymentID, baselinePayment.Result)
	statuses = append(statuses, response.StatusCode)

	response, body, err = us1GetByID(ctx, address, us1APIKey, paymentID)
	if err != nil {
		t.Fatal("T070 baseline GET by id failed")
	}
	_, requestID = us1RequirePaymentResponse(t, response, body, http.StatusOK, paymentID, businessOrderNo, 100, "CNY", payment.ResultSucceeded)
	secRecordHTTP(t, artifacts, response.StatusCode, requestID, paymentID, string(payment.ResultSucceeded))
	statuses = append(statuses, response.StatusCode)

	response, body, err = us1GetByBusinessOrderNo(ctx, address, us1APIKey, businessOrderNo)
	if err != nil {
		t.Fatal("T070 baseline GET by business order failed")
	}
	_, requestID = us1RequirePaymentResponse(t, response, body, http.StatusOK, paymentID, businessOrderNo, 100, "CNY", payment.ResultSucceeded)
	secRecordHTTP(t, artifacts, response.StatusCode, requestID, paymentID, string(payment.ResultSucceeded))
	statuses = append(statuses, response.StatusCode)

	orderBefore := us1ReadOrderState(t, pool, clientID, businessOrderNo)
	mockBefore := us1ReadMockState(t, pool, paymentID)
	handlingStateBefore := us1ReadHandlingState(t, pool, paymentID)
	ordersBefore, mocksBefore, handlingRowsBefore := us1CountBusinessRows(t, pool, clientID)
	if ordersBefore != 1 || mocksBefore != 1 || handlingRowsBefore != 1 {
		t.Fatal("T070 baseline business row count mismatch")
	}
	secRecordSQL(t, artifacts, uint64(ordersBefore))
	secRecordSQL(t, artifacts, uint64(mocksBefore))
	secRecordSQL(t, artifacts, uint64(handlingRowsBefore))

	statuses = append(statuses, secRunCanaryRequests(t, address, artifacts)...)

	orderAfter := us1ReadOrderState(t, pool, clientID, businessOrderNo)
	mockAfter := us1ReadMockState(t, pool, paymentID)
	handlingStateAfter := us1ReadHandlingState(t, pool, paymentID)
	if !reflect.DeepEqual(orderBefore, orderAfter) ||
		!reflect.DeepEqual(mockBefore, mockAfter) ||
		!reflect.DeepEqual(handlingStateBefore, handlingStateAfter) {
		t.Fatal("T070 rejected canary request changed a stored business fact")
	}
	ordersAfter, mocksAfter, handlingRowsAfter := us1CountBusinessRows(t, pool, clientID)
	if ordersAfter != 1 || mocksAfter != 1 || handlingRowsAfter != 1 {
		t.Fatal("T070 rejected canary request changed a business row count")
	}
	secRecordSQL(t, artifacts, uint64(ordersAfter))
	secRecordSQL(t, artifacts, uint64(mocksAfter))
	secRecordSQL(t, artifacts, uint64(handlingRowsAfter))

	us1StopProcess(t, process)
	cancelLifetime()
	us1CleanupClient(t, pool, clientID)
	us1RequireNoBusinessRows(t, pool, clientID)

	reportPath, err := artifacts.Finish()
	if err != nil {
		t.Fatal("T070 runtime-harness finish failed")
	}
	if reportPath == "" {
		t.Fatal("T070 runtime-harness finish returned an empty path")
	}

	forbidden := append(secForbiddenMarkers(), pool.Config().ConnConfig.Password, dsn, clientID, businessOrderNo)
	return secReportExpectation{
		Path:      reportPath,
		RunID:     runID,
		Statuses:  statuses,
		Forbidden: forbidden,
	}
}

type secRuntimeManifestEntry struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type secRuntimeSource struct {
	Manifest       []secRuntimeManifestEntry `json:"manifest"`
	ManifestDigest string                    `json:"manifest_digest"`
	Unchanged      bool                      `json:"unchanged"`
}

type secRuntimeVersions struct {
	Go       string `json:"go"`
	Goos     string `json:"goos"`
	Goarch   string `json:"goarch"`
	Postgres string `json:"postgres,omitempty"`
	Goose    string `json:"goose,omitempty"`
}

type secRuntimeProcess struct {
	Generation     uint64 `json:"generation"`
	Code           int    `json:"code"`
	Signal         int    `json:"signal"`
	Canceled       bool   `json:"canceled"`
	TimedOut       bool   `json:"timed_out"`
	Forced         bool   `json:"forced"`
	Reaped         bool   `json:"reaped"`
	OutputComplete bool   `json:"output_complete"`
	GroupGone      bool   `json:"group_gone"`
	Waited         bool   `json:"waited"`
}

type secRuntimeLog struct {
	Time           string `json:"time"`
	Level          string `json:"level"`
	Event          string `json:"event"`
	Stage          string `json:"stage"`
	RequestID      string `json:"request_id,omitempty"`
	PaymentOrderID string `json:"payment_order_id,omitempty"`
	ResultBefore   string `json:"result_before,omitempty"`
	ResultAfter    string `json:"result_after,omitempty"`
	Reason         string `json:"reason,omitempty"`
	ErrorKind      string `json:"error_kind,omitempty"`
	Generation     uint64 `json:"generation"`
	Stream         string `json:"stream"`
}

type secRuntimeHTTP struct {
	Generation     uint64 `json:"generation"`
	Status         int    `json:"status"`
	RequestID      string `json:"request_id,omitempty"`
	PaymentOrderID string `json:"payment_order_id,omitempty"`
	Result         string `json:"result,omitempty"`
}

type secRuntimeSQL struct {
	Generation uint64 `json:"generation"`
	State      string `json:"state"`
	Rows       uint64 `json:"rows"`
}

type secRuntimeBudget struct {
	Generation uint64 `json:"generation"`
	Stage      string `json:"stage"`
	LimitNS    int64  `json:"limit_ns"`
	ElapsedNS  int64  `json:"elapsed_ns"`
}

type secRuntimeCounts struct {
	Payment uint64 `json:"payment"`
	Query   uint64 `json:"query"`
}

type secRuntimeReceipt struct {
	RunID      string `json:"run_id"`
	Epoch      string `json:"epoch"`
	Generation uint64 `json:"generation"`
	LastSeq    uint64 `json:"last_seq"`
}

type secRuntimeObserver struct {
	Epoch    string              `json:"epoch"`
	Counts   secRuntimeCounts    `json:"counts"`
	Receipts []secRuntimeReceipt `json:"receipts"`
}

type secRuntimeReport struct {
	Schema             string              `json:"schema"`
	RunID              string              `json:"run_id"`
	CollectionComplete bool                `json:"collection_complete"`
	Faults             []string            `json:"faults"`
	Versions           secRuntimeVersions  `json:"versions"`
	Source             secRuntimeSource    `json:"source"`
	Processes          []secRuntimeProcess `json:"processes"`
	Logs               []secRuntimeLog     `json:"logs"`
	HTTP               []secRuntimeHTTP    `json:"http"`
	SQL                []secRuntimeSQL     `json:"sql"`
	Budgets            []secRuntimeBudget  `json:"budgets"`
	Observer           *secRuntimeObserver `json:"observer"`
}

func secLowerHex(value string, length int) bool {
	if len(value) != length {
		return false
	}
	for index := 0; index < len(value); index++ {
		character := value[index]
		if (character < '0' || character > '9') && (character < 'a' || character > 'f') {
			return false
		}
	}
	return true
}

func secValidateRuntimeHarnessReport(t *testing.T, expectation secReportExpectation) {
	t.Helper()
	if expectation.Path == "" {
		t.Fatal("T070 runtime-harness report path is empty")
	}
	data, err := os.ReadFile(expectation.Path)
	if err != nil || len(data) == 0 || len(data) > 1<<20 {
		t.Fatal("T070 runtime-harness report is unavailable or oversized")
	}

	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var report secRuntimeReport
	if err := decoder.Decode(&report); err != nil {
		t.Fatal("T070 runtime-harness report does not match the frozen schema")
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		t.Fatal("T070 runtime-harness report has trailing content")
	}

	if report.Schema != harness.ArtifactSchema || report.RunID != expectation.RunID {
		t.Fatal("T070 runtime-harness report identity mismatch")
	}
	if !report.CollectionComplete || len(report.Faults) != 0 {
		t.Fatal("T070 runtime-harness report collection is incomplete or faulted")
	}
	if report.Versions.Go != runtime.Version() || report.Versions.Goos != "linux" || report.Versions.Goarch != "amd64" {
		t.Fatal("T070 runtime-harness report runtime version mismatch")
	}
	if !report.Source.Unchanged || len(report.Source.Manifest) == 0 || !secLowerHex(report.Source.ManifestDigest, 64) {
		t.Fatal("T070 runtime-harness report source identity mismatch")
	}
	testSourceSeen := false
	for _, entry := range report.Source.Manifest {
		if entry.Path == "tests/security_test.go" {
			testSourceSeen = secLowerHex(entry.SHA256, 64)
		}
	}
	if !testSourceSeen {
		t.Fatal("T070 runtime-harness report does not include the frozen security test source")
	}

	if len(report.Processes) != 1 {
		t.Fatal("T070 runtime-harness report process count mismatch")
	}
	process := report.Processes[0]
	if process.Generation != 1 || process.Code != 0 || !process.Canceled || process.TimedOut ||
		process.Forced || !process.Reaped || !process.OutputComplete || !process.GroupGone || !process.Waited {
		t.Fatal("T070 runtime-harness report process completion mismatch")
	}

	startupLogSeen := false
	for _, log := range report.Logs {
		if log.Generation != 1 || (log.Stream != "stdout" && log.Stream != "stderr") {
			t.Fatal("T070 runtime-harness log record identity mismatch")
		}
		if log.Level == "info" && log.Event == "dependency" && log.Stage == "persistence" && log.Reason == "dependency" {
			startupLogSeen = true
		}
	}
	if !startupLogSeen {
		t.Fatal("T070 runtime-harness report lacks the real startup log")
	}

	if len(report.HTTP) != len(expectation.Statuses) {
		t.Fatal("T070 runtime-harness HTTP observation count mismatch")
	}
	paymentIDSeen := false
	resultSeen := false
	for index, observation := range report.HTTP {
		if observation.Generation != 1 || observation.Status != expectation.Statuses[index] {
			t.Fatal("T070 runtime-harness HTTP observation status mismatch")
		}
		if observation.RequestID == "" || payment.ValidateUUIDv4(observation.RequestID) != nil {
			t.Fatal("T070 runtime-harness HTTP observation request id mismatch")
		}
		if observation.PaymentOrderID != "" {
			paymentIDSeen = true
		}
		if observation.Result == string(payment.ResultSucceeded) {
			resultSeen = true
		}
	}
	if !paymentIDSeen || !resultSeen {
		t.Fatal("T070 runtime-harness HTTP observations lack the baseline payment facts")
	}

	if len(report.SQL) < 6 {
		t.Fatal("T070 runtime-harness SQL observation count mismatch")
	}
	for _, observation := range report.SQL {
		if observation.Generation != 1 || observation.State != "00000" || observation.Rows == 0 {
			t.Fatal("T070 runtime-harness SQL observation mismatch")
		}
	}
	if report.Observer != nil {
		t.Fatal("T070 normal-mode runtime unexpectedly recorded an observer")
	}

	secAssertNoLeak(t, data, expectation.Forbidden...)
}
