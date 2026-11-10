//go:build integration

package tests

import (
	"context"
	"net/http"
	"sync"
	"testing"
	"time"

	"payment-hub/internal/payment"
	"payment-hub/internal/testcontrol"
	"payment-hub/tests/harness"
)

func TestUS2Concurrency(t *testing.T) {
	repoRoot := us1RepoRoot(t)
	runID := us1RunID(t)
	dsn := us1TestDatabaseURL(t)
	binary := us1BuildApplication(t, repoRoot)

	const scenarioName = "concurrency"
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
		t.Fatal("US2 concurrency observer expect failed")
	}
	process, addr := us2StartGeneration(t, artifacts, binary, repoRoot, dsn, clientID, us2APIKeyA, socket, runID, epoch, 1)

	ordersBefore, mocksBefore, handlingBefore := us1CountBusinessRows(t, pool, clientID)
	if ordersBefore != 0 || mocksBefore != 0 || handlingBefore != 0 {
		t.Fatalf("US2 concurrency initial client rows are not empty: orders=%d mock=%d handling=%d", ordersBefore, mocksBefore, handlingBefore)
	}

	lockCtx, cancelLock := context.WithTimeout(context.Background(), 2*us1RequestBudget)
	tx, err := pool.Begin(lockCtx)
	if err != nil {
		cancelLock()
		t.Fatalf("US2 concurrency candidate transaction failed (%s)", us1SQLState(err))
	}
	var releaseOnce sync.Once
	releaseCandidate := func() {
		releaseOnce.Do(func() {
			rollbackCtx, rollbackCancel := context.WithTimeout(context.Background(), us1CleanupBudget)
			defer rollbackCancel()
			us2RequireRollback(t, tx.Rollback(rollbackCtx), "candidate")
			cancelLock()
		})
	}
	t.Cleanup(releaseCandidate)

	var blockerPID int
	if err := tx.QueryRow(lockCtx, "SELECT pg_backend_pid()").Scan(&blockerPID); err != nil {
		us2RequireRollback(t, tx.Rollback(lockCtx), "candidate pid")
		cancelLock()
		t.Fatalf("US2 concurrency blocker pid read failed (%s)", us1SQLState(err))
	}
	candidateID := payment.NewUUIDv4()
	us2CandidateInsert(t, lockCtx, tx, candidateID, clientID, businessOrderNo, 100)

	type postResult struct {
		response *http.Response
		body     []byte
		err      error
	}
	competition := make(chan postResult, 2)
	startCompetition := make(chan struct{})
	for index := 0; index < 2; index++ {
		go func() {
			<-startCompetition
			ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
			defer cancel()
			response, body, err := us2PostIntent(ctx, addr, us2APIKeyA, businessOrderNo, 100, "CNY")
			competition <- postResult{response: response, body: body, err: err}
		}()
	}
	close(startCompetition)

	waiterCount, waiterElapsed := us2WaitLockWaiters(t, pool, 2, blockerPID)
	if waiterCount != 2 {
		t.Fatalf("US2 concurrency waiter count mismatch: got %d want 2", waiterCount)
	}
	us2RecordBarrierBudget(t, artifacts, 1, us1BarrierBudget, waiterElapsed)

	hold, err := observer.HoldPaymentEntry(1)
	if err != nil {
		t.Fatal("US2 concurrency payment hold arm failed")
	}
	releaseCandidate()

	holdCtx, cancelHold := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancelHold()
	holdStart := time.Now()
	binding, err := hold.Wait(holdCtx)
	holdElapsed := time.Since(holdStart)
	if err != nil {
		t.Fatal("US2 concurrency payment hold wait failed")
	}
	if binding.Generation != 1 || binding.OrderID == "" ||
		payment.ValidateUUIDv4(binding.OrderID) != nil {
		t.Fatal("US2 concurrency payment binding is invalid")
	}
	if err := hold.Release(testcontrol.DirectiveSucceeded); err != nil {
		t.Fatal("US2 concurrency payment hold release failed")
	}
	us2RecordBarrierBudget(t, artifacts, 1, 2*time.Second, holdElapsed)
	facts.Directive = &us1ReportDirective{
		Result: string(payment.ResultSucceeded),
		Binding: us1ReportBinding{
			RunID:      binding.RunID,
			Epoch:      binding.Epoch,
			Generation: binding.Generation,
			CallSeq:    binding.CallSeq,
			EventID:    binding.EventID,
			OrderID:    binding.OrderID,
		},
	}

	created, ok := 0, 0
	var createdPayment us1Payment
	competitionRequestIDs := make([]string, 0, 2)
	for index := 0; index < 2; index++ {
		post := <-competition
		if post.err != nil {
			t.Fatal("US2 concurrency POST failed")
		}
		envelope := us1ParseEnvelope(t, post.body)
		if envelope.Payment == nil {
			t.Fatal("US2 concurrency response lacks a payment")
		}
		gotResult := payment.Result(envelope.Payment.Result)
		switch post.response.StatusCode {
		case http.StatusCreated:
			created++
			if gotResult != payment.ResultSucceeded {
				t.Fatal("US2 concurrency creation was not confirmed")
			}
		case http.StatusOK:
			ok++
			if gotResult != payment.ResultSucceeded && gotResult != payment.ResultUnknown {
				t.Fatal("US2 concurrency reuse result is invalid")
			}
		default:
			t.Fatalf("US2 concurrency unexpected status %d", post.response.StatusCode)
		}
		paymentBody, requestID := us2RequirePaymentResponse(t, post.response, post.body, post.response.StatusCode, binding.OrderID, businessOrderNo, 100, "CNY", gotResult, us2IssuesOptionalUnconfirmed)
		us2RecordHTTP(t, artifacts, facts, 1, post.response.StatusCode, requestID, binding.OrderID, string(paymentBody.Result))
		competitionRequestIDs = append(competitionRequestIDs, requestID)
		if post.response.StatusCode == http.StatusCreated {
			createdPayment = paymentBody
		}
	}
	if created != 1 || ok != 1 {
		t.Fatalf("US2 concurrency status set mismatch: created=%d reused=%d", created, ok)
	}
	us2RequireUniqueRequestIDs(t, competitionRequestIDs...)

	order := us2WaitOrderConfirmed(t, pool, clientID, businessOrderNo)
	us1RequireOrderState(t, order, clientID, businessOrderNo, binding.OrderID, 100, "CNY", payment.ResultSucceeded)
	if order.ID == candidateID {
		t.Fatal("US2 concurrency committed the uncommitted candidate row")
	}
	beforeOrder := order
	beforeMock := us1ReadMockState(t, pool, binding.OrderID)
	orders, mocks, handling := us1CountBusinessRows(t, pool, clientID)
	if orders != 1 || mocks != 1 || handling != 1 {
		t.Fatalf("US2 concurrency competition row counts mismatch: orders=%d mock=%d handling=%d", orders, mocks, handling)
	}

	// The mixed pair is held at the whole payment_orders table by SHARE mode:
	// INSERT requests RowExclusiveLock, so both valid-value requests must reach
	// the server and block before either can register or decide the conflict.
	mixedLockCtx, cancelMixedLock := context.WithTimeout(context.Background(), 2*us1RequestBudget)
	mixedTx, err := pool.Begin(mixedLockCtx)
	if err != nil {
		cancelMixedLock()
		t.Fatalf("US2 concurrency mixed SHARE transaction failed (%s)", us1SQLState(err))
	}
	var mixedBlockerPID int
	if err := mixedTx.QueryRow(mixedLockCtx, "SELECT pg_backend_pid()").Scan(&mixedBlockerPID); err != nil {
		us2RequireRollback(t, mixedTx.Rollback(mixedLockCtx), "mixed SHARE pid")
		cancelMixedLock()
		t.Fatalf("US2 concurrency mixed SHARE blocker pid read failed (%s)", us1SQLState(err))
	}
	if _, err := mixedTx.Exec(mixedLockCtx, "LOCK TABLE payment_orders IN SHARE MODE"); err != nil {
		us2RequireRollback(t, mixedTx.Rollback(mixedLockCtx), "mixed SHARE setup")
		cancelMixedLock()
		t.Fatalf("US2 concurrency mixed SHARE lock failed (%s)", us1SQLState(err))
	}
	var mixedReleaseOnce sync.Once
	releaseMixed := func() {
		mixedReleaseOnce.Do(func() {
			rollbackCtx, rollbackCancel := context.WithTimeout(context.Background(), us1CleanupBudget)
			defer rollbackCancel()
			us2RequireRollback(t, mixedTx.Rollback(rollbackCtx), "mixed SHARE lock")
			cancelMixedLock()
		})
	}
	t.Cleanup(releaseMixed)

	interleaved := make(chan postResult, 2)
	startInterleaved := make(chan struct{})
	go func() {
		<-startInterleaved
		ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
		defer cancel()
		response, body, err := us2PostIntent(ctx, addr, us2APIKeyA, businessOrderNo, 100, "CNY")
		interleaved <- postResult{response: response, body: body, err: err}
	}()
	go func() {
		<-startInterleaved
		ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
		defer cancel()
		response, body, err := us2PostIntent(ctx, addr, us2APIKeyA, businessOrderNo, 101, "CNY")
		interleaved <- postResult{response: response, body: body, err: err}
	}()
	close(startInterleaved)

	mixedWaiterCount, mixedWaiterElapsed := us2WaitTableInsertWaiters(t, pool, 2, mixedBlockerPID)
	if mixedWaiterCount != 2 {
		t.Fatalf("US2 concurrency mixed waiter count mismatch: got %d want 2", mixedWaiterCount)
	}
	us2RecordBarrierBudget(t, artifacts, 1, us1BarrierBudget, mixedWaiterElapsed)
	releaseMixed()

	sameSeen, conflictSeen := false, false
	interleavedRequestIDs := make([]string, 0, 2)
	for index := 0; index < 2; index++ {
		post := <-interleaved
		if post.err != nil {
			t.Fatal("US2 concurrency interleaved POST failed")
		}
		switch post.response.StatusCode {
		case http.StatusOK:
			same, requestID := us2RequirePaymentResponse(t, post.response, post.body, http.StatusOK, binding.OrderID, businessOrderNo, 100, "CNY", payment.ResultSucceeded, us2IssuesNone)
			us1RequireSamePayment(t, same, createdPayment)
			us2RecordHTTP(t, artifacts, facts, 1, post.response.StatusCode, requestID, binding.OrderID, string(same.Result))
			interleavedRequestIDs = append(interleavedRequestIDs, requestID)
			sameSeen = true
		case http.StatusConflict:
			requestID := us1RequireErrorResponse(t, post.response, post.body, http.StatusConflict, "PAYMENT_CONFLICT")
			us2RecordHTTP(t, artifacts, facts, 1, post.response.StatusCode, requestID, "", "")
			interleavedRequestIDs = append(interleavedRequestIDs, requestID)
			conflictSeen = true
		default:
			t.Fatalf("US2 concurrency interleaved unexpected status %d", post.response.StatusCode)
		}
	}
	if !sameSeen || !conflictSeen {
		t.Fatal("US2 concurrency interleaved request set is incomplete")
	}
	us2RequireUniqueRequestIDs(t, interleavedRequestIDs...)

	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	response, body, err := us1GetByID(ctx, addr, us2APIKeyA, binding.OrderID)
	if err != nil {
		t.Fatal("US2 concurrency final GET by payment id failed")
	}
	getID, getIDRequestID := us2RequirePaymentResponse(t, response, body, http.StatusOK, binding.OrderID, businessOrderNo, 100, "CNY", payment.ResultSucceeded, us2IssuesNone)
	us2RecordHTTP(t, artifacts, facts, 1, response.StatusCode, getIDRequestID, binding.OrderID, string(getID.Result))
	us1RequireSamePayment(t, getID, createdPayment)

	response, body, err = us1GetByBusinessOrderNo(ctx, addr, us2APIKeyA, businessOrderNo)
	if err != nil {
		t.Fatal("US2 concurrency final GET by business order number failed")
	}
	getBusiness, getBusinessRequestID := us2RequirePaymentResponse(t, response, body, http.StatusOK, binding.OrderID, businessOrderNo, 100, "CNY", payment.ResultSucceeded, us2IssuesNone)
	us2RecordHTTP(t, artifacts, facts, 1, response.StatusCode, getBusinessRequestID, binding.OrderID, string(getBusiness.Result))
	us1RequireSamePayment(t, getBusiness, createdPayment)
	us2RequireUniqueRequestIDs(t, append(competitionRequestIDs, append(interleavedRequestIDs, getIDRequestID, getBusinessRequestID)...)...)

	afterOrder, afterMock := us2VerifyStoredFacts(t, pool, artifacts, facts, 1, clientID, businessOrderNo, binding.OrderID, 100, payment.ResultSucceeded)
	stableFacts := us2RequireFactsUnchanged(t, beforeOrder, afterOrder, beforeMock, afterMock)

	checks := us2ReportChecks{
		CountsBefore:     us2BeforeCounts(nil, uint64(ordersBefore), uint64(mocksBefore), uint64(handlingBefore)),
		StableFacts:      stableFacts,
		LookupFault:      "none",
		WaiterCount:      uint(waiterCount),
		MixedWaiterCount: uint(mixedWaiterCount),
	}
	us2FinalizeScenario(t, artifacts, facts, scenarioName, observer, process, harness.Counts{Payment: 1, Query: 0}, []uint64{1}, checks)
}
