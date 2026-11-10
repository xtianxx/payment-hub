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

func TestUS2Replay(t *testing.T) {
	repoRoot := us1RepoRoot(t)
	runID := us1RunID(t)
	dsn := us1TestDatabaseURL(t)
	binary := us1BuildApplication(t, repoRoot)

	t.Run("success", func(t *testing.T) {
		us2RunConfirmedReplay(t, repoRoot, runID, dsn, binary, "replay_success", testcontrol.DirectiveSucceeded, payment.ResultSucceeded, 100)
	})
	t.Run("failed", func(t *testing.T) {
		us2RunConfirmedReplay(t, repoRoot, runID, dsn, binary, "replay_failed", testcontrol.DirectiveFailed, payment.ResultFailed, 200)
	})
	t.Run("unknown", func(t *testing.T) {
		us2RunUnknownReplay(t, repoRoot, runID, dsn, binary)
	})
	t.Run("key_rotation", func(t *testing.T) {
		us2RunKeyRotation(t, repoRoot, runID, dsn, binary)
	})
}

func us2RunConfirmedReplay(t *testing.T, repoRoot, runID, dsn, binary, scenarioName string, directive testcontrol.Directive, wantResult payment.Result, amount int64) {
	t.Helper()
	facts := &us2ReportScenario{Name: scenarioName, Generations: []uint64{1}}
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
		t.Fatal("US2 replay observer expect failed")
	}
	process, addr, binding, postPayment, postRequestID := us2SubmitWithHold(
		t, artifacts, facts, observer, binary, repoRoot, dsn, runID,
		epoch, socket, clientID, businessOrderNo,
		us2APIKeyA, 1, directive, wantResult, amount,
	)
	beforeOrder := us1ReadOrderState(t, pool, clientID, businessOrderNo)
	beforeMock := us1ReadMockState(t, pool, binding.OrderID)
	orders, mocks, handling := us1CountBusinessRows(t, pool, clientID)
	if orders != 1 || mocks != 1 || handling != 1 {
		t.Fatalf("US2 replay setup row counts mismatch: orders=%d mock=%d handling=%d", orders, mocks, handling)
	}

	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	response, body, err := us2PostIntent(ctx, addr, us2APIKeyA, businessOrderNo, amount, "CNY")
	if err != nil {
		t.Fatal("US2 replay first POST failed")
	}
	firstReplay, firstRequestID := us2RequirePaymentResponse(t, response, body, http.StatusOK, binding.OrderID, businessOrderNo, amount, "CNY", wantResult, us2IssuesNone)
	us2RecordHTTP(t, artifacts, facts, 1, response.StatusCode, firstRequestID, binding.OrderID, string(firstReplay.Result))
	us1RequireSamePayment(t, firstReplay, postPayment)

	response, body, err = us2PostIntent(ctx, addr, us2APIKeyA, businessOrderNo, amount, "CNY")
	if err != nil {
		t.Fatal("US2 replay second POST failed")
	}
	secondReplay, secondRequestID := us2RequirePaymentResponse(t, response, body, http.StatusOK, binding.OrderID, businessOrderNo, amount, "CNY", wantResult, us2IssuesNone)
	us2RecordHTTP(t, artifacts, facts, 1, response.StatusCode, secondRequestID, binding.OrderID, string(secondReplay.Result))
	us1RequireSamePayment(t, secondReplay, postPayment)
	us2RequireUniqueRequestIDs(t, postRequestID, firstRequestID, secondRequestID)

	afterOrder, afterMock := us2VerifyStoredFacts(t, pool, artifacts, facts, 1, clientID, businessOrderNo, binding.OrderID, amount, wantResult)
	stableFacts := us2RequireFactsUnchanged(t, beforeOrder, afterOrder, beforeMock, afterMock)

	checks := us2ReportChecks{
		CountsBefore: us2BeforeCounts(&binding, uint64(orders), uint64(mocks), uint64(handling)),
		StableFacts:  stableFacts,
		LookupFault:  "none",
	}
	us2FinalizeScenario(t, artifacts, facts, scenarioName, observer, process, harness.Counts{Payment: 1, Query: 0}, []uint64{1}, checks)
}

func us2RunUnknownReplay(t *testing.T, repoRoot, runID, dsn, binary string) {
	t.Helper()
	const scenarioName = "replay_unknown"
	facts := &us2ReportScenario{Name: scenarioName, Generations: []uint64{1}}
	facts.addCommand(us1ReportCommandBuild, 0, false)
	artifacts := us2BeginArtifacts(t, repoRoot, runID, scenarioName)

	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	pool := us1OpenPool(t)
	t.Cleanup(func() { us1CleanupBusiness(t, pool, clientID, businessOrderNo) })

	const amount int64 = 100
	seededOrderID := us2SeedUnknownOrder(t, pool, clientID, businessOrderNo, amount)
	beforeOrder := us1ReadOrderState(t, pool, clientID, businessOrderNo)
	if beforeOrder.ID != seededOrderID || beforeOrder.Result != string(payment.ResultUnknown) ||
		beforeOrder.MockTransactionID != "" || beforeOrder.ConfirmedAt != nil {
		t.Fatal("US2 unknown seed is not a valid UNKNOWN order")
	}
	orders, mocks, handling := us1CountBusinessRows(t, pool, clientID)
	if orders != 1 || mocks != 0 || handling != 0 {
		t.Fatalf("US2 unknown seed row counts mismatch: orders=%d mock=%d handling=%d", orders, mocks, handling)
	}

	socket := us1ObserverSocket(t)
	epoch := us1Epoch(t)
	observer := us1StartObserver(t, runID, epoch, socket)
	if err := observer.Expect(1); err != nil {
		t.Fatal("US2 unknown observer expect failed")
	}
	process, addr := us2StartGeneration(t, artifacts, binary, repoRoot, dsn, clientID, us2APIKeyA, socket, runID, epoch, 1)

	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	response, body, err := us2PostIntent(ctx, addr, us2APIKeyA, businessOrderNo, amount, "CNY")
	if err != nil {
		t.Fatal("US2 unknown first replay failed")
	}
	firstReplay, firstRequestID := us2RequirePaymentResponse(t, response, body, http.StatusOK, seededOrderID, businessOrderNo, amount, "CNY", payment.ResultUnknown, us2IssuesNone)
	us2RecordHTTP(t, artifacts, facts, 1, response.StatusCode, firstRequestID, seededOrderID, string(firstReplay.Result))

	response, body, err = us2PostIntent(ctx, addr, us2APIKeyA, businessOrderNo, amount, "CNY")
	if err != nil {
		t.Fatal("US2 unknown second replay failed")
	}
	secondReplay, secondRequestID := us2RequirePaymentResponse(t, response, body, http.StatusOK, seededOrderID, businessOrderNo, amount, "CNY", payment.ResultUnknown, us2IssuesNone)
	us2RecordHTTP(t, artifacts, facts, 1, response.StatusCode, secondRequestID, seededOrderID, string(secondReplay.Result))
	us1RequireSamePayment(t, secondReplay, firstReplay)
	us2RequireUniqueRequestIDs(t, firstRequestID, secondRequestID)

	afterOrder := us2VerifyUnknownReplay(t, pool, artifacts, facts, 1, clientID, businessOrderNo, seededOrderID, amount)
	stableFacts := us2RequireOrderUnchanged(t, beforeOrder, afterOrder)

	checks := us2ReportChecks{
		CountsBefore: us2BeforeCounts(nil, uint64(orders), uint64(mocks), uint64(handling)),
		StableFacts:  stableFacts,
		LookupFault:  "none",
	}
	us2FinalizeScenario(t, artifacts, facts, scenarioName, observer, process, harness.Counts{Payment: 0, Query: 0}, []uint64{1}, checks)
}

func us2RunKeyRotation(t *testing.T, repoRoot, runID, dsn, binary string) {
	t.Helper()
	const scenarioName = "key_rotation"
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
		t.Fatal("US2 key rotation observer expect failed")
	}
	process1, _, binding, postPayment, postRequestID := us2SubmitWithHold(
		t, artifacts, facts, observer, binary, repoRoot, dsn, runID,
		epoch, socket, clientID, businessOrderNo,
		us2APIKeyA, 1, testcontrol.DirectiveSucceeded, payment.ResultSucceeded, 300,
	)
	beforeOrder := us1ReadOrderState(t, pool, clientID, businessOrderNo)
	beforeMock := us1ReadMockState(t, pool, binding.OrderID)
	orders, mocks, handling := us1CountBusinessRows(t, pool, clientID)
	if orders != 1 || mocks != 1 || handling != 1 {
		t.Fatalf("US2 key rotation setup row counts mismatch: orders=%d mock=%d handling=%d", orders, mocks, handling)
	}

	exit1 := us1StopProcess(t, process1)
	facts.addCommand(us1ReportCommandStop, exit1.Code, false)
	countsAfterFirst, err := observer.Snapshot()
	if err != nil {
		t.Fatal("US2 key rotation generation 1 snapshot failed")
	}
	if countsAfterFirst.Payment != 1 || countsAfterFirst.Query != 0 {
		t.Fatalf("US2 key rotation generation 1 counts mismatch: %+v", countsAfterFirst)
	}

	if err := observer.Expect(2); err != nil {
		t.Fatal("US2 key rotation observer expect generation 2 failed")
	}
	process2, addr2 := us2StartGeneration(t, artifacts, binary, repoRoot, dsn, clientID, us2APIKeyB, socket, runID, epoch, 2)

	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	response, body, err := us2PostIntent(ctx, addr2, us2APIKeyB, businessOrderNo, 300, "CNY")
	if err != nil {
		t.Fatal("US2 key rotation replay failed")
	}
	replay, replayRequestID := us2RequirePaymentResponse(t, response, body, http.StatusOK, binding.OrderID, businessOrderNo, 300, "CNY", payment.ResultSucceeded, us2IssuesNone)
	us2RecordHTTP(t, artifacts, facts, 2, response.StatusCode, replayRequestID, binding.OrderID, string(replay.Result))
	us1RequireSamePayment(t, replay, postPayment)

	response, body, err = us1GetByBusinessOrderNo(ctx, addr2, us2APIKeyB, businessOrderNo)
	if err != nil {
		t.Fatal("US2 key rotation GET failed")
	}
	query, queryRequestID := us2RequirePaymentResponse(t, response, body, http.StatusOK, binding.OrderID, businessOrderNo, 300, "CNY", payment.ResultSucceeded, us2IssuesNone)
	us2RecordHTTP(t, artifacts, facts, 2, response.StatusCode, queryRequestID, binding.OrderID, string(query.Result))
	us1RequireSamePayment(t, query, postPayment)

	response, body, err = us2PostIntent(ctx, addr2, us2APIKeyA, businessOrderNo, 300, "CNY")
	if err != nil {
		t.Fatal("US2 key rotation old-key POST failed")
	}
	oldKeyRequestID := us1RequireErrorResponse(t, response, body, http.StatusUnauthorized, "UNAUTHORIZED")
	us2RecordHTTP(t, artifacts, facts, 2, response.StatusCode, oldKeyRequestID, "", "")
	us2RequireUniqueRequestIDs(t, postRequestID, replayRequestID, queryRequestID, oldKeyRequestID)

	afterOrder, afterMock := us2VerifyStoredFacts(t, pool, artifacts, facts, 2, clientID, businessOrderNo, binding.OrderID, 300, payment.ResultSucceeded)
	stableFacts := us2RequireFactsUnchanged(t, beforeOrder, afterOrder, beforeMock, afterMock)

	checks := us2ReportChecks{
		CountsBefore: us2BeforeCounts(&binding, uint64(orders), uint64(mocks), uint64(handling)),
		StableFacts:  stableFacts,
		LookupFault:  "none",
	}
	us2FinalizeScenario(t, artifacts, facts, scenarioName, observer, process2, harness.Counts{Payment: 1, Query: 0}, []uint64{1, 2}, checks)
}
