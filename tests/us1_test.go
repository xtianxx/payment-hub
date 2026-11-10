//go:build integration

package tests

import (
	"context"
	"net/http"
	"net/url"
	"strings"
	"testing"
	"time"

	"payment-hub/internal/payment"
	"payment-hub/internal/testcontrol"
	"payment-hub/tests/harness"
)

const us1ValidPostBody = `{"business_order_no":"M100","amount":100,"currency":"CNY"}`

func TestUS1Payment(t *testing.T) {
	repoRoot := us1RepoRoot(t)
	runID := us1RunID(t)
	dsn := us1TestDatabaseURL(t)
	binary := us1BuildApplication(t, repoRoot)

	t.Run("success", func(t *testing.T) {
		facts := &us1ReportScenario{Generations: []uint64{1}}
		facts.addCommand(us1ReportCommandBuild, 0, false)
		artifacts := us1BeginArtifacts(t, repoRoot, runID, "payment_success")
		us1RunPaymentScenario(t, artifacts, facts, binary, repoRoot, dsn, runID, us1Epoch(t), testcontrol.DirectiveSucceeded, payment.ResultSucceeded, 100)
	})
	t.Run("failed", func(t *testing.T) {
		facts := &us1ReportScenario{Generations: []uint64{1}}
		facts.addCommand(us1ReportCommandBuild, 0, false)
		artifacts := us1BeginArtifacts(t, repoRoot, runID, "payment_failed")
		us1RunPaymentScenario(t, artifacts, facts, binary, repoRoot, dsn, runID, us1Epoch(t), testcontrol.DirectiveFailed, payment.ResultFailed, 200)
	})
	t.Run("invalid_requests", func(t *testing.T) {
		facts := &us1ReportScenario{Generations: []uint64{1}}
		facts.addCommand(us1ReportCommandBuild, 0, false)
		artifacts := us1BeginArtifacts(t, repoRoot, runID, "invalid_requests")
		us1RunInvalidScenario(t, artifacts, facts, binary, repoRoot, dsn, runID, us1Epoch(t))
	})
}

func us1RunPaymentScenario(t *testing.T, artifacts *harness.Artifacts, facts *us1ReportScenario, binary, repoRoot, dsn, runID, epoch string, directive testcontrol.Directive, wantResult payment.Result, amount int64) {
	t.Helper()
	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	pool := us1OpenPool(t)
	t.Cleanup(func() { us1CleanupBusiness(t, pool, clientID, businessOrderNo) })

	socket := us1ObserverSocket(t)
	observer := us1StartObserver(t, runID, epoch, socket)
	if err := observer.Expect(1); err != nil {
		t.Fatal("US1 observer expect failed")
	}
	process, addr, binding, postPayment, postRequestID := us1SubmitWithHold(
		t, artifacts, facts, observer, binary, repoRoot, dsn, runID, epoch, socket,
		clientID, businessOrderNo, 1, directive, wantResult, amount,
	)

	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	response, body, err := us1GetByID(ctx, addr, us1APIKey, binding.OrderID)
	if err != nil {
		t.Fatal("US1 GET by payment id failed")
	}
	getIDPayment, getIDRequestID := us1RequirePaymentResponse(t, response, body, http.StatusOK, binding.OrderID, businessOrderNo, amount, "CNY", wantResult)
	us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, getIDRequestID, binding.OrderID, string(getIDPayment.Result))

	response, body, err = us1GetByBusinessOrderNo(ctx, addr, us1APIKey, businessOrderNo)
	if err != nil {
		t.Fatal("US1 GET by business order number failed")
	}
	getBusinessPayment, getBusinessRequestID := us1RequirePaymentResponse(t, response, body, http.StatusOK, binding.OrderID, businessOrderNo, amount, "CNY", wantResult)
	us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, getBusinessRequestID, binding.OrderID, string(getBusinessPayment.Result))

	if postRequestID == getIDRequestID || postRequestID == getBusinessRequestID || getIDRequestID == getBusinessRequestID {
		t.Fatal("US1 request_id was reused across requests")
	}
	us1RequireSamePayment(t, getIDPayment, postPayment)
	us1RequireSamePayment(t, getBusinessPayment, postPayment)
	us1VerifyStoredFacts(t, pool, artifacts, facts, 1, clientID, businessOrderNo, binding.OrderID, amount, wantResult)
	scenarioName := "payment_success"
	if wantResult == payment.ResultFailed {
		scenarioName = "payment_failed"
	}
	us1FinalizeScenario(t, artifacts, facts, scenarioName, observer, process, 1, []uint64{1})
}

func us1RunInvalidScenario(t *testing.T, artifacts *harness.Artifacts, facts *us1ReportScenario, binary, repoRoot, dsn, runID, epoch string) {
	t.Helper()
	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	pool := us1OpenPool(t)
	t.Cleanup(func() { us1CleanupClient(t, pool, clientID) })

	socket := us1ObserverSocket(t)
	observer := us1StartObserver(t, runID, epoch, socket)
	if err := observer.Expect(1); err != nil {
		t.Fatal("US1 observer expect failed")
	}
	addr := us1FreeLoopbackAddr(t)
	env := us1AppEnv(t, dsn, addr, clientID, socket, runID, epoch, 1)
	process := us1StartApplication(t, artifacts, binary, repoRoot, 1, env, 10*time.Second, 60*time.Second)
	us1WaitReady(t, addr)

	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()

	response, body, err := us1PostPayment(ctx, addr, "", businessOrderNo, 100, "CNY")
	if err != nil {
		t.Fatal("US1 unauthorized POST failed")
	}
	requestID := us1RequireErrorResponse(t, response, body, http.StatusUnauthorized, "UNAUTHORIZED")
	us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, "", "")

	request, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://"+addr+"/api/v1/payments", strings.NewReader("{"))
	if err != nil {
		t.Fatal("US1 invalid JSON request construction failed")
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+us1APIKey)
	response, body, err = us1Do(request)
	if err != nil {
		t.Fatal("US1 invalid JSON POST failed")
	}
	requestID = us1RequireErrorResponse(t, response, body, http.StatusBadRequest, "INVALID_REQUEST")
	us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, "", "")

	response, body, err = us1PostBytes(ctx, addr, us1APIKey, us1PaddedPostBody(4097), "application/json")
	if err != nil {
		t.Fatal("US1 oversized POST failed")
	}
	requestID = us1RequireErrorResponse(t, response, body, http.StatusRequestEntityTooLarge, "INVALID_REQUEST")
	us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, "", "")

	response, body, err = us1PostBytes(ctx, addr, us1APIKey, []byte(us1ValidPostBody), "text/plain")
	if err != nil {
		t.Fatal("US1 wrong media type POST failed")
	}
	requestID = us1RequireErrorResponse(t, response, body, http.StatusUnsupportedMediaType, "INVALID_REQUEST")
	us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, "", "")

	request, err = http.NewRequestWithContext(ctx, http.MethodPost, "http://"+addr+"/api/v1/payments", strings.NewReader(us1ValidPostBody))
	if err != nil {
		t.Fatal("US1 content encoding request construction failed")
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Content-Encoding", "gzip")
	request.Header.Set("Authorization", "Bearer "+us1APIKey)
	response, body, err = us1Do(request)
	if err != nil {
		t.Fatal("US1 content encoding POST failed")
	}
	requestID = us1RequireErrorResponse(t, response, body, http.StatusUnsupportedMediaType, "INVALID_REQUEST")
	us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, "", "")

	request, err = http.NewRequestWithContext(ctx, http.MethodGet, "http://"+addr+"/api/v1/payments?business_order_no="+url.QueryEscape(businessOrderNo), strings.NewReader("{}"))
	if err != nil {
		t.Fatal("US1 GET body request construction failed")
	}
	request.Header.Set("Authorization", "Bearer "+us1APIKey)
	response, body, err = us1Do(request)
	if err != nil {
		t.Fatal("US1 GET with body failed")
	}
	requestID = us1RequireErrorResponse(t, response, body, http.StatusBadRequest, "INVALID_REQUEST")
	us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, "", "")

	request, err = http.NewRequestWithContext(ctx, http.MethodGet, "http://"+addr+"/api/v1/payments?business_order_no="+url.QueryEscape(businessOrderNo)+"&page=1", nil)
	if err != nil {
		t.Fatal("US1 unknown query request construction failed")
	}
	request.Header.Set("Authorization", "Bearer "+us1APIKey)
	response, body, err = us1Do(request)
	if err != nil {
		t.Fatal("US1 unknown query GET failed")
	}
	requestID = us1RequireErrorResponse(t, response, body, http.StatusBadRequest, "INVALID_REQUEST")
	us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, "", "")

	request, err = http.NewRequestWithContext(ctx, http.MethodGet, "http://"+addr+"/api/v1/payments", nil)
	if err != nil {
		t.Fatal("US1 missing query request construction failed")
	}
	request.Header.Set("Authorization", "Bearer "+us1APIKey)
	response, body, err = us1Do(request)
	if err != nil {
		t.Fatal("US1 missing query GET failed")
	}
	requestID = us1RequireErrorResponse(t, response, body, http.StatusBadRequest, "INVALID_REQUEST")
	us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, "", "")

	us1RequireNoBusinessRows(t, pool, clientID)
	us1RecordSQL(t, artifacts, facts, 1, "ORDER", 0)
	us1RecordSQL(t, artifacts, facts, 1, "MOCK", 0)
	us1RecordSQL(t, artifacts, facts, 1, "TRACE", 0)
	us1FinalizeScenario(t, artifacts, facts, "invalid_requests", observer, process, 0, []uint64{1})
}
