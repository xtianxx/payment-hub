//go:build integration

package tests

import (
	"context"
	"net/http"
	"testing"
	"time"

	"payment-hub/internal/payment"
	"payment-hub/internal/testcontrol"
	"payment-hub/tests/harness"
)

func TestUS1Restart(t *testing.T) {
	repoRoot := us1RepoRoot(t)
	runID := us1RunID(t)
	dsn := us1TestDatabaseURL(t)
	binary := us1BuildApplication(t, repoRoot)

	t.Run("success", func(t *testing.T) {
		facts := &us1ReportScenario{Generations: []uint64{1, 2}}
		facts.addCommand(us1ReportCommandBuild, 0, false)
		artifacts := us1BeginArtifacts(t, repoRoot, runID, "restart_success")
		us1RunRestartScenario(t, artifacts, facts, binary, repoRoot, dsn, runID, us1Epoch(t), testcontrol.DirectiveSucceeded, payment.ResultSucceeded, 300)
	})
	t.Run("failed", func(t *testing.T) {
		facts := &us1ReportScenario{Generations: []uint64{1, 2}}
		facts.addCommand(us1ReportCommandBuild, 0, false)
		artifacts := us1BeginArtifacts(t, repoRoot, runID, "restart_failed")
		us1RunRestartScenario(t, artifacts, facts, binary, repoRoot, dsn, runID, us1Epoch(t), testcontrol.DirectiveFailed, payment.ResultFailed, 400)
	})
}

func us1RunRestartScenario(t *testing.T, artifacts *harness.Artifacts, facts *us1ReportScenario, binary, repoRoot, dsn, runID, epoch string, directive testcontrol.Directive, wantResult payment.Result, amount int64) {
	t.Helper()
	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	pool := us1OpenPool(t)
	t.Cleanup(func() { us1CleanupBusiness(t, pool, clientID, businessOrderNo) })

	socket := us1ObserverSocket(t)
	observer := us1StartObserver(t, runID, epoch, socket)
	if err := observer.Expect(1); err != nil {
		t.Fatal("US1 observer expect generation 1 failed")
	}
	process1, addr1, binding, postPayment, postRequestID := us1SubmitWithHold(
		t, artifacts, facts, observer, binary, repoRoot, dsn, runID, epoch, socket,
		clientID, businessOrderNo, 1, directive, wantResult, amount,
	)

	ctx1, cancel1 := context.WithTimeout(context.Background(), us1RequestBudget)
	response, body, err := us1GetByID(ctx1, addr1, us1APIKey, binding.OrderID)
	if err != nil {
		t.Fatal("US1 restart generation 1 GET by payment id failed")
	}
	getID1, getID1RequestID := us1RequirePaymentResponse(t, response, body, http.StatusOK, binding.OrderID, businessOrderNo, amount, "CNY", wantResult)
	us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, getID1RequestID, binding.OrderID, string(getID1.Result))

	response, body, err = us1GetByBusinessOrderNo(ctx1, addr1, us1APIKey, businessOrderNo)
	if err != nil {
		t.Fatal("US1 restart generation 1 GET by business order number failed")
	}
	getBusiness1, getBusiness1RequestID := us1RequirePaymentResponse(t, response, body, http.StatusOK, binding.OrderID, businessOrderNo, amount, "CNY", wantResult)
	us1RecordHTTP(t, artifacts, facts, 1, response.StatusCode, getBusiness1RequestID, binding.OrderID, string(getBusiness1.Result))

	if postRequestID == getID1RequestID || postRequestID == getBusiness1RequestID || getID1RequestID == getBusiness1RequestID {
		t.Fatal("US1 restart generation 1 request_id was reused")
	}
	us1RequireSamePayment(t, getID1, postPayment)
	us1RequireSamePayment(t, getBusiness1, postPayment)
	cancel1()
	us1VerifyStoredFacts(t, pool, artifacts, facts, 1, clientID, businessOrderNo, binding.OrderID, amount, wantResult)

	exit1 := us1StopProcess(t, process1)
	facts.addCommand(us1ReportCommandStop, exit1.Code, false)
	countsAfterFirst, err := observer.Snapshot()
	if err != nil {
		t.Fatal("US1 observer snapshot after generation 1 failed")
	}
	if countsAfterFirst.Payment != 1 {
		t.Fatalf("US1 generation 1 payment count mismatch: got %d want 1", countsAfterFirst.Payment)
	}

	if err := observer.Expect(2); err != nil {
		t.Fatal("US1 observer expect generation 2 failed")
	}
	addr2 := us1FreeLoopbackAddr(t)
	env2 := us1AppEnv(t, dsn, addr2, clientID, socket, runID, epoch, 2)
	process2 := us1StartApplication(t, artifacts, binary, repoRoot, 2, env2, 10*time.Second, 60*time.Second)
	us1WaitReady(t, addr2)

	ctx2, cancel2 := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel2()
	response, body, err = us1GetByID(ctx2, addr2, us1APIKey, binding.OrderID)
	if err != nil {
		t.Fatal("US1 restart generation 2 GET by payment id failed")
	}
	getID2, getID2RequestID := us1RequirePaymentResponse(t, response, body, http.StatusOK, binding.OrderID, businessOrderNo, amount, "CNY", wantResult)
	us1RecordHTTP(t, artifacts, facts, 2, response.StatusCode, getID2RequestID, binding.OrderID, string(getID2.Result))

	response, body, err = us1GetByBusinessOrderNo(ctx2, addr2, us1APIKey, businessOrderNo)
	if err != nil {
		t.Fatal("US1 restart generation 2 GET by business order number failed")
	}
	getBusiness2, getBusiness2RequestID := us1RequirePaymentResponse(t, response, body, http.StatusOK, binding.OrderID, businessOrderNo, amount, "CNY", wantResult)
	us1RecordHTTP(t, artifacts, facts, 2, response.StatusCode, getBusiness2RequestID, binding.OrderID, string(getBusiness2.Result))

	if postRequestID == getID2RequestID || postRequestID == getBusiness2RequestID ||
		getID1RequestID == getID2RequestID || getID1RequestID == getBusiness2RequestID ||
		getBusiness1RequestID == getID2RequestID || getBusiness1RequestID == getBusiness2RequestID ||
		getID2RequestID == getBusiness2RequestID {
		t.Fatal("US1 restart request_id was reused across generations")
	}
	us1RequireSamePayment(t, getID2, postPayment)
	us1RequireSamePayment(t, getBusiness2, postPayment)
	us1VerifyStoredFacts(t, pool, artifacts, facts, 2, clientID, businessOrderNo, binding.OrderID, amount, wantResult)

	scenarioName := "restart_success"
	if wantResult == payment.ResultFailed {
		scenarioName = "restart_failed"
	}
	us1FinalizeScenario(t, artifacts, facts, scenarioName, observer, process2, 1, []uint64{1, 2})
}
