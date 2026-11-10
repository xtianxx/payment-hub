//go:build integration

package tests

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"

	"payment-hub/internal/httpapi"
	"payment-hub/internal/payment"
	"payment-hub/internal/postgres"
	"payment-hub/internal/testcontrol"
	"payment-hub/tests/harness"
)

func TestUS2Conflict(t *testing.T) {
	repoRoot := us1RepoRoot(t)
	runID := us1RunID(t)
	dsn := us1TestDatabaseURL(t)
	binary := us1BuildApplication(t, repoRoot)

	t.Run("conflict_priority", func(t *testing.T) {
		us2RunConflictPriority(t, repoRoot, runID, dsn, binary)
	})
	t.Run("controlled_500_mapping", func(t *testing.T) {
		us2RunControlled500(t)
	})
}

func us2RunConflictPriority(t *testing.T, repoRoot, runID, dsn, binary string) {
	t.Helper()
	const scenarioName = "conflict_priority"
	facts := &us2ReportScenario{Name: scenarioName, Generations: []uint64{1}}
	facts.addCommand(us1ReportCommandBuild, 0, false)
	artifacts := us2BeginArtifacts(t, repoRoot, runID, scenarioName)

	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	newBusinessNo1 := us1UniqueBusinessOrderNo(t)
	newBusinessNo2 := us1UniqueBusinessOrderNo(t)
	pool := us1OpenPool(t)
	t.Cleanup(func() { us1CleanupClient(t, pool, clientID) })

	socket := us1ObserverSocket(t)
	epoch := us1Epoch(t)
	observer := us1StartObserver(t, runID, epoch, socket)
	if err := observer.Expect(1); err != nil {
		t.Fatal("US2 conflict observer expect failed")
	}
	process, addr, binding, postPayment, postRequestID := us2SubmitWithHold(
		t, artifacts, facts, observer, binary, repoRoot, dsn, runID,
		epoch, socket, clientID, businessOrderNo,
		us2APIKeyA, 1, testcontrol.DirectiveSucceeded, payment.ResultSucceeded, 100,
	)
	beforeOrder := us1ReadOrderState(t, pool, clientID, businessOrderNo)
	beforeMock := us1ReadMockState(t, pool, binding.OrderID)
	if beforeOrder.ID != postPayment.PaymentID ||
		beforeOrder.BusinessOrderNo != postPayment.BusinessOrderNo ||
		beforeOrder.Amount != postPayment.Amount ||
		beforeOrder.Currency != postPayment.Currency ||
		beforeOrder.Result != postPayment.Result {
		t.Fatal("US2 conflict setup response does not match the stored order")
	}
	orders, mocks, handling := us1CountBusinessRows(t, pool, clientID)
	if orders != 1 || mocks != 1 || handling != 1 {
		t.Fatalf("US2 conflict setup row counts mismatch: orders=%d mock=%d handling=%d", orders, mocks, handling)
	}

	requestIDs := []string{postRequestID}
	readonlyProbes := 0
	lookupFault := "none"
	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()

	release := us2LockPaymentOrders(t, pool)
	response, body, err := us1PostPayment(ctx, addr, "FAKE-us2_wrong_key.9~a+/b==", businessOrderNo, 100, "CNY")
	if err != nil {
		t.Fatal("US2 conflict unauthorized POST failed")
	}
	requestIDs = append(requestIDs, us1RequireErrorResponse(t, response, body, http.StatusUnauthorized, "UNAUTHORIZED"))
	us2RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestIDs[len(requestIDs)-1], "", "")

	response, body, err = us2PostRaw(ctx, addr, us2APIKeyA, us2StructuralBody(businessOrderNo, "1.5"))
	if err != nil {
		t.Fatal("US2 conflict decimal POST failed")
	}
	requestIDs = append(requestIDs, us1RequireErrorResponse(t, response, body, http.StatusBadRequest, "INVALID_REQUEST"))
	us2RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestIDs[len(requestIDs)-1], "", "")

	response, body, err = us2PostRaw(ctx, addr, us2APIKeyA, us2StructuralBody(businessOrderNo, `"100"`))
	if err != nil {
		t.Fatal("US2 conflict string-amount POST failed")
	}
	requestIDs = append(requestIDs, us1RequireErrorResponse(t, response, body, http.StatusBadRequest, "INVALID_REQUEST"))
	us2RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestIDs[len(requestIDs)-1], "", "")

	response, body, err = us2PostIntent(ctx, addr, us2APIKeyA, businessOrderNo, 0, "CNY")
	if err != nil {
		t.Fatal("US2 conflict lookup-failure POST failed")
	}
	lookupFailureRequestID, lookupFailureStatus := us2RequireProcessingError(t, response, body)
	if lookupFailureStatus != http.StatusServiceUnavailable {
		t.Fatalf("US2 conflict lookup failure must be 503, got %d", lookupFailureStatus)
	}
	// The whole-table lock is already held and this bounded request returned
	// the exact dependency-unavailable status; only now is the fault classified.
	readonlyProbes++
	lookupFault = "timeout"
	requestIDs = append(requestIDs, lookupFailureRequestID)
	us2RecordHTTP(t, artifacts, facts, 1, lookupFailureStatus, lookupFailureRequestID, "", "")
	release()

	response, body, err = us2PostIntent(ctx, addr, us2APIKeyA, businessOrderNo, 0, "CNY")
	if err != nil {
		t.Fatal("US2 conflict amount POST failed")
	}
	requestIDs = append(requestIDs, us1RequireErrorResponse(t, response, body, http.StatusConflict, "PAYMENT_CONFLICT"))
	us2RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestIDs[len(requestIDs)-1], "", "")
	readonlyProbes++

	response, body, err = us2PostIntent(ctx, addr, us2APIKeyA, businessOrderNo, 100, "EUR")
	if err != nil {
		t.Fatal("US2 conflict currency POST failed")
	}
	requestIDs = append(requestIDs, us1RequireErrorResponse(t, response, body, http.StatusConflict, "PAYMENT_CONFLICT"))
	us2RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestIDs[len(requestIDs)-1], "", "")
	readonlyProbes++

	response, body, err = us2PostIntent(ctx, addr, us2APIKeyA, newBusinessNo1, 0, "CNY")
	if err != nil {
		t.Fatal("US2 new invalid amount POST failed")
	}
	requestIDs = append(requestIDs, us1RequireErrorResponse(t, response, body, http.StatusBadRequest, "INVALID_REQUEST"))
	us2RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestIDs[len(requestIDs)-1], "", "")
	readonlyProbes++

	response, body, err = us2PostIntent(ctx, addr, us2APIKeyA, newBusinessNo2, 100, "EUR")
	if err != nil {
		t.Fatal("US2 new invalid currency POST failed")
	}
	requestIDs = append(requestIDs, us1RequireErrorResponse(t, response, body, http.StatusBadRequest, "INVALID_REQUEST"))
	us2RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestIDs[len(requestIDs)-1], "", "")
	readonlyProbes++
	us2RequireUniqueRequestIDs(t, requestIDs...)

	afterOrder, afterMock := us2VerifyStoredFacts(t, pool, artifacts, facts, 1, clientID, businessOrderNo, binding.OrderID, 100, payment.ResultSucceeded)
	stableFacts := us2RequireFactsUnchanged(t, beforeOrder, afterOrder, beforeMock, afterMock)

	checks := us2ReportChecks{
		CountsBefore:   us2BeforeCounts(&binding, uint64(orders), uint64(mocks), uint64(handling)),
		StableFacts:    stableFacts,
		ReadonlyProbes: uint(readonlyProbes),
		LookupFault:    lookupFault,
	}
	us2FinalizeScenario(t, artifacts, facts, scenarioName, observer, process, harness.Counts{Payment: 1, Query: 0}, []uint64{1}, checks)
}

func us2RunControlled500(t *testing.T) {
	t.Helper()
	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	pool := us1OpenPool(t)
	t.Cleanup(func() { us1CleanupClient(t, pool, clientID) })

	seededOrderID := us2SeedUnknownOrder(t, pool, clientID, businessOrderNo, 100)
	beforeOrder := us1ReadOrderState(t, pool, clientID, businessOrderNo)
	if beforeOrder.ID != seededOrderID || beforeOrder.Result != string(payment.ResultUnknown) {
		t.Fatal("US2 controlled 500 seed is not a valid UNKNOWN order")
	}
	orders, mocks, handling := us1CountBusinessRows(t, pool, clientID)
	if orders != 1 || mocks != 0 || handling != 0 {
		t.Fatalf("US2 controlled 500 seed row counts mismatch: orders=%d mock=%d handling=%d", orders, mocks, handling)
	}

	store := postgres.NewStore(pool, nil)
	lookup := &us2SpyLookup{store: store}
	registrar := &us2SpyRegistrar{err: payment.ErrRegistrationRejected}
	recorder := &us2SpyRecorder{}
	payer := &us2SpyPayer{}
	querier := &us2SpyQuerier{}
	lifecycle, cancelLifecycle := context.WithCancel(context.Background())
	defer cancelLifecycle()
	submitter := payment.NewSubmitter(registrar, lookup, recorder, payer, lifecycle)

	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	if err := httpapi.RegisterPaymentRoutes(router, httpapi.AuthConfig{
		ClientID: clientID,
		APIKey:   us2APIKeyA,
	}, submitter, querier); err != nil {
		t.Fatal("US2 controlled 500 route registration failed")
	}
	server := httptest.NewServer(router)
	defer server.Close()
	addr := server.Listener.Addr().String()

	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	response, body, err := us2PostIntent(ctx, addr, us2APIKeyA, businessOrderNo, 100, "CNY")
	if err != nil {
		t.Fatal("US2 controlled 500 POST failed")
	}
	requestID := us1RequireErrorResponse(t, response, body, http.StatusInternalServerError, "PROCESSING_ERROR")
	if response.Header.Get("Location") != "" {
		t.Fatal("US2 controlled 500 unexpectedly carried Location")
	}
	us2RequireUniqueRequestIDs(t, requestID)
	if registrar.calls != 1 || lookup.calls != 0 ||
		recorder.calls != 0 || payer.calls != 0 || querier.calls != 0 {
		t.Fatalf("US2 controlled 500 port calls mismatch: registrar=%d lookup=%d recorder=%d payer=%d query=%d",
			registrar.calls, lookup.calls, recorder.calls, payer.calls, querier.calls)
	}

	afterOrder := us1ReadOrderState(t, pool, clientID, businessOrderNo)
	us2RequireOrderUnchanged(t, beforeOrder, afterOrder)
	orders, mocks, handling = us1CountBusinessRows(t, pool, clientID)
	if orders != 1 || mocks != 0 || handling != 0 {
		t.Fatalf("US2 controlled 500 changed row counts: orders=%d mock=%d handling=%d", orders, mocks, handling)
	}
}

type us2SpyRegistrar struct {
	calls int
	err   error
}

func (registrar *us2SpyRegistrar) RegisterOrder(context.Context, payment.RegisterOrderInput) (payment.RegisterOrderResult, error) {
	registrar.calls++
	return payment.RegisterOrderResult{}, registrar.err
}

type us2SpyLookup struct {
	store payment.OrderLookup
	calls int
}

func (lookup *us2SpyLookup) FindOrderByID(ctx context.Context, clientID string, paymentOrderID payment.PaymentOrderID) (payment.PaymentOrder, bool, error) {
	lookup.calls++
	if lookup.store == nil {
		return payment.PaymentOrder{}, false, errors.New("unexpected lookup")
	}
	return lookup.store.FindOrderByID(ctx, clientID, paymentOrderID)
}

func (lookup *us2SpyLookup) FindOrderByBusinessOrderNo(ctx context.Context, clientID, businessOrderNo string) (payment.PaymentOrder, bool, error) {
	lookup.calls++
	if lookup.store == nil {
		return payment.PaymentOrder{}, false, errors.New("unexpected lookup")
	}
	return lookup.store.FindOrderByBusinessOrderNo(ctx, clientID, businessOrderNo)
}

type us2SpyRecorder struct {
	calls int
}

func (recorder *us2SpyRecorder) SaveObservation(context.Context, payment.Observation) (payment.SaveObservationResult, error) {
	recorder.calls++
	return payment.SaveObservationResult{}, errors.New("unexpected save")
}

type us2SpyPayer struct {
	calls int
}

func (payer *us2SpyPayer) Pay(context.Context, payment.MockPaymentRequest) (payment.MockTransaction, error) {
	payer.calls++
	return payment.MockTransaction{}, errors.New("unexpected payment")
}

type us2SpyQuerier struct {
	calls int
}

func (querier *us2SpyQuerier) Query(context.Context, payment.RequestID, payment.QueryInput) (payment.QueryResult, error) {
	querier.calls++
	return payment.QueryResult{}, errors.New("unexpected query")
}
