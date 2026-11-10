//go:build integration

package tests

import (
	"context"
	"net/http"
	"testing"

	"payment-hub/internal/payment"
	"payment-hub/internal/testcontrol"
	"payment-hub/tests/harness"
)

func TestUS2Restart(t *testing.T) {
	repoRoot := us1RepoRoot(t)
	runID := us1RunID(t)
	dsn := us1TestDatabaseURL(t)
	binary := us1BuildApplication(t, repoRoot)

	t.Run("success", func(t *testing.T) {
		us2RunRestart(t, repoRoot, runID, dsn, binary, "restart_success", testcontrol.DirectiveSucceeded, payment.ResultSucceeded, 300)
	})
	t.Run("failed", func(t *testing.T) {
		us2RunRestart(t, repoRoot, runID, dsn, binary, "restart_failed", testcontrol.DirectiveFailed, payment.ResultFailed, 400)
	})
}

func us2RunRestart(t *testing.T, repoRoot, runID, dsn, binary, scenarioName string, directive testcontrol.Directive, wantResult payment.Result, amount int64) {
	t.Helper()
	facts := &us2ReportScenario{Name: scenarioName, Generations: []uint64{1, 2}}
	facts.addCommand(us1ReportCommandBuild, 0, false)
	artifacts := us2BeginArtifacts(t, repoRoot, runID, scenarioName)

	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	pool := us1OpenPool(t)
	t.Cleanup(func() { us1CleanupBusiness(t, pool, clientID, businessOrderNo) })

	socket := us1ObserverSocket(t)
	epoch := us1Epoch(t)
	observer := us1StartObserver(t, runID, epoch, socket)
	if err := observer.Expect(1); err != nil {
		t.Fatal("US2 restart observer expect generation 1 failed")
	}
	process1, addr1, binding, postPayment, postRequestID := us2SubmitWithHold(
		t, artifacts, facts, observer, binary, repoRoot, dsn, runID,
		epoch, socket, clientID, businessOrderNo,
		us2APIKeyA, 1, directive, wantResult, amount,
	)
	beforeOrder := us1ReadOrderState(t, pool, clientID, businessOrderNo)
	beforeMock := us1ReadMockState(t, pool, binding.OrderID)
	orders, mocks, handling := us1CountBusinessRows(t, pool, clientID)
	if orders != 1 || mocks != 1 || handling != 1 {
		t.Fatalf("US2 restart setup row counts mismatch: orders=%d mock=%d handling=%d", orders, mocks, handling)
	}

	ctx1, cancel1 := context.WithTimeout(context.Background(), us1RequestBudget)
	response, body, err := us1GetByID(ctx1, addr1, us2APIKeyA, binding.OrderID)
	if err != nil {
		t.Fatal("US2 restart generation 1 GET by payment id failed")
	}
	getID1, getID1RequestID := us2RequirePaymentResponse(t, response, body, http.StatusOK, binding.OrderID, businessOrderNo, amount, "CNY", wantResult, us2IssuesNone)
	us2RecordHTTP(t, artifacts, facts, 1, response.StatusCode, getID1RequestID, binding.OrderID, string(getID1.Result))

	response, body, err = us1GetByBusinessOrderNo(ctx1, addr1, us2APIKeyA, businessOrderNo)
	if err != nil {
		t.Fatal("US2 restart generation 1 GET by business order number failed")
	}
	getBusiness1, getBusiness1RequestID := us2RequirePaymentResponse(t, response, body, http.StatusOK, binding.OrderID, businessOrderNo, amount, "CNY", wantResult, us2IssuesNone)
	us2RecordHTTP(t, artifacts, facts, 1, response.StatusCode, getBusiness1RequestID, binding.OrderID, string(getBusiness1.Result))
	us1RequireSamePayment(t, getID1, postPayment)
	us1RequireSamePayment(t, getBusiness1, postPayment)
	cancel1()

	exit1 := us1StopProcess(t, process1)
	facts.addCommand(us1ReportCommandStop, exit1.Code, false)
	countsAfterFirst, err := observer.Snapshot()
	if err != nil {
		t.Fatal("US2 restart observer snapshot after generation 1 failed")
	}
	if countsAfterFirst.Payment != 1 || countsAfterFirst.Query != 0 {
		t.Fatalf("US2 restart generation 1 counts mismatch: %+v", countsAfterFirst)
	}

	if err := observer.Expect(2); err != nil {
		t.Fatal("US2 restart observer expect generation 2 failed")
	}
	process2, addr2 := us2StartGeneration(t, artifacts, binary, repoRoot, dsn, clientID, us2APIKeyA, socket, runID, epoch, 2)

	ctx2, cancel2 := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel2()
	response, body, err = us2PostIntent(ctx2, addr2, us2APIKeyA, businessOrderNo, amount, "CNY")
	if err != nil {
		t.Fatal("US2 restart generation 2 POST failed")
	}
	replay, replayRequestID := us2RequirePaymentResponse(t, response, body, http.StatusOK, binding.OrderID, businessOrderNo, amount, "CNY", wantResult, us2IssuesNone)
	us2RecordHTTP(t, artifacts, facts, 2, response.StatusCode, replayRequestID, binding.OrderID, string(replay.Result))
	us1RequireSamePayment(t, replay, postPayment)

	response, body, err = us1GetByID(ctx2, addr2, us2APIKeyA, binding.OrderID)
	if err != nil {
		t.Fatal("US2 restart generation 2 GET by payment id failed")
	}
	getID2, getID2RequestID := us2RequirePaymentResponse(t, response, body, http.StatusOK, binding.OrderID, businessOrderNo, amount, "CNY", wantResult, us2IssuesNone)
	us2RecordHTTP(t, artifacts, facts, 2, response.StatusCode, getID2RequestID, binding.OrderID, string(getID2.Result))

	response, body, err = us1GetByBusinessOrderNo(ctx2, addr2, us2APIKeyA, businessOrderNo)
	if err != nil {
		t.Fatal("US2 restart generation 2 GET by business order number failed")
	}
	getBusiness2, getBusiness2RequestID := us2RequirePaymentResponse(t, response, body, http.StatusOK, binding.OrderID, businessOrderNo, amount, "CNY", wantResult, us2IssuesNone)
	us2RecordHTTP(t, artifacts, facts, 2, response.StatusCode, getBusiness2RequestID, binding.OrderID, string(getBusiness2.Result))
	us1RequireSamePayment(t, getID2, postPayment)
	us1RequireSamePayment(t, getBusiness2, postPayment)
	us2RequireUniqueRequestIDs(t, postRequestID, getID1RequestID, getBusiness1RequestID, replayRequestID, getID2RequestID, getBusiness2RequestID)

	afterOrder, afterMock := us2VerifyStoredFacts(t, pool, artifacts, facts, 2, clientID, businessOrderNo, binding.OrderID, amount, wantResult)
	stableFacts := us2RequireFactsUnchanged(t, beforeOrder, afterOrder, beforeMock, afterMock)

	checks := us2ReportChecks{
		CountsBefore: us2BeforeCounts(&binding, uint64(orders), uint64(mocks), uint64(handling)),
		StableFacts:  stableFacts,
		LookupFault:  "none",
	}
	us2FinalizeScenario(t, artifacts, facts, scenarioName, observer, process2, harness.Counts{Payment: 1, Query: 0}, []uint64{1, 2}, checks)
}
