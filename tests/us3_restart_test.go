//go:build integration

package tests

import (
	"context"
	"errors"
	"net/http"
	"testing"
	"time"

	"payment-hub/internal/payment"
	"payment-hub/internal/testcontrol"
	"payment-hub/tests/harness"
)

func TestUS3Restart(t *testing.T) {
	repoRoot := us1RepoRoot(t)
	runID := us1RunID(t)
	dsn := us1TestDatabaseURL(t)
	binary := us1BuildApplication(t, repoRoot)

	t.Run("registered unpaid", func(t *testing.T) {
		us3RunRestartWindow(t, repoRoot, runID, dsn, binary, testcontrol.PointRegistered)
	})
	t.Run("mock committed reply lost", func(t *testing.T) {
		us3RunRestartWindow(t, repoRoot, runID, dsn, binary, testcontrol.PointMockCommitted)
	})
	t.Run("unknown no confirm", func(t *testing.T) {
		us3RunRestartUnknown(t, repoRoot, runID, dsn, binary, false)
	})
	t.Run("unknown then confirmed", func(t *testing.T) {
		us3RunRestartUnknown(t, repoRoot, runID, dsn, binary, true)
	})
}

func us3RunRestartWindow(t *testing.T, repoRoot, runID, dsn, binary string, point testcontrol.Point) {
	t.Helper()
	scenarioName := "restart_registered_unpaid"
	if point == testcontrol.PointMockCommitted {
		scenarioName = "restart_mock_committed_reply_lost"
	}
	facts := &us3Scenario{Name: scenarioName, Generations: []uint64{1, 2}, Transport: "RESPONSE"}
	facts.Commands = []us1ReportCommand{{Kind: us1ReportCommandBuild, Exit: 0}}
	artifacts := us3BeginArtifacts(t, repoRoot, runID, scenarioName)
	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	pool := us1OpenPool(t)
	t.Cleanup(func() { us1CleanupBusiness(t, pool, clientID, businessOrderNo) })

	socket := us1ObserverSocket(t)
	epoch := us1Epoch(t)
	observer := us3StartObserver(t, runID, epoch, socket)
	if err := observer.Expect(1); err != nil {
		t.Fatal("US3 restart observer expect generation 1 failed")
	}
	process1, addr1 := us3StartGeneration(t, artifacts, binary, repoRoot, dsn, clientID, us2APIKeyA, socket, runID, epoch, 1)

	armStart := time.Now()
	barrier, err := observer.ArmNextCommittedPoint(1, point)
	if err != nil {
		t.Fatal("US3 restart next committed point arm failed")
	}
	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	type postResult struct {
		response *http.Response
		body     []byte
		err      error
		at       time.Time
	}
	postCh := make(chan postResult, 1)
	go func() {
		response, body, err := us1PostPayment(ctx, addr1, us2APIKeyA, businessOrderNo, 100, "CNY")
		postCh <- postResult{response: response, body: body, err: err, at: time.Now()}
	}()
	if _, err := barrier.Wait(ctx); err != nil {
		t.Fatal("US3 restart next committed point wait failed")
	}
	holdAt := time.Now()
	heldElapsed := holdAt.Sub(armStart)
	if heldElapsed >= us1BarrierBudget {
		t.Fatalf("US3 restart held point exceeded the 5s barrier budget: %s", heldElapsed)
	}
	binding, ok := barrier.Binding()
	if !ok {
		t.Fatal("US3 restart next committed point binding unavailable")
	}
	if binding.OrderID == "" || payment.ValidateUUIDv4(binding.OrderID) != nil {
		t.Fatal("US3 restart next committed point order id invalid")
	}
	intent, err := observer.PrepareControlledTermination(barrier, process1)
	if err != nil {
		t.Fatal("US3 restart controlled termination prepare failed")
	}
	// Force the real held process down promptly; the actual terminal state and
	// stop error are recorded instead of being discarded. A forced kill is a
	// legitimate controlled-termination outcome, not a graceful exit claim.
	stopCtx, stopCancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	stopExit, stopErr := process1.Stop(stopCtx)
	stopCancel()
	if !stopExit.Reaped || !stopExit.GroupGone || !stopExit.OutputComplete || !stopExit.Canceled {
		t.Fatalf("US3 restart stop terminal state mismatch: %+v", stopExit)
	}
	if !errors.Is(stopErr, harness.ErrProcess) {
		t.Fatalf("US3 restart stop returned an unexpected error class")
	}
	proofCtx, proofCancel := context.WithTimeout(context.Background(), 5*time.Second)
	proof, err := process1.ControlledTerminationProof(proofCtx)
	proofCancel()
	if err != nil {
		t.Fatal("US3 restart controlled termination proof failed")
	}
	controlled, err := intent.Terminate(proof)
	if err != nil {
		t.Fatal("US3 restart controlled termination terminate failed")
	}
	if controlled.Exit.Forced != stopExit.Forced || controlled.Exit.Signal != stopExit.Signal {
		t.Fatalf("US3 restart controlled termination exit differs from the actual Stop exit")
	}

	// Join the held request to its actual terminal state over the complete
	// post-hold to process-group-gone window. The original request deadline is
	// the only bound; there is no independent sleep or drain.
	var post postResult
	select {
	case post = <-postCh:
	case <-ctx.Done():
		post = <-postCh
	}
	postObserved := true
	postResponse := post.response != nil
	postBeforeHold := postResponse && us3HeldPostResponseBeforeHold(post.at, holdAt)
	if postResponse {
		if post.err != nil {
			t.Fatalf("US3 restart held POST returned both a response and a transport error")
		}
		if !postBeforeHold {
			t.Fatalf("US3 restart held POST produced a response after the hold")
		}
		status := post.response.StatusCode
		if status != http.StatusOK && status != http.StatusCreated {
			t.Fatalf("US3 restart pre-hold response status mismatch: %d", status)
		}
		replay, requestID := us3RequirePaymentResponse(t, post.response, post.body, status, binding.OrderID, businessOrderNo, 100, "CNY", payment.ResultUnknown)
		us3RecordHTTP(t, artifacts, facts, 1, status, requestID, binding.OrderID, string(replay.Result))
	} else if post.err == nil {
		t.Fatalf("US3 restart held POST ended without a response or a transport error")
	}

	before := us3ReadOrderFact(t, pool, clientID, businessOrderNo)
	mockResult := payment.Result("")
	mockID := ""
	if point == testcontrol.PointMockCommitted {
		mock := us1ReadMockState(t, pool, binding.OrderID)
		mockResult = payment.Result(mock.Result)
		mockID = mock.ID
	}
	ordersBefore, mocksBefore, tracesBefore := us1CountBusinessRows(t, pool, clientID)
	facts.countsBefore = us3Counts{
		PaymentCalls: controlled.ObservedPrefix.Payment, QueryCalls: controlled.ObservedPrefix.Query,
		OrderRows: uint64(ordersBefore), MockRows: uint64(mocksBefore), TraceRows: uint64(tracesBefore),
	}
	facts.countsBeforeSet = true

	if err := observer.Expect(2); err != nil {
		t.Fatal("US3 restart observer expect generation 2 failed")
	}
	process2, addr2 := us3StartGeneration(t, artifacts, binary, repoRoot, dsn, clientID, us2APIKeyA, socket, runID, epoch, 2)
	ctx2, cancel2 := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel2()
	response, body, err := us1PostPayment(ctx2, addr2, us2APIKeyA, businessOrderNo, 100, "CNY")
	if err != nil {
		t.Fatal("US3 restart generation 2 replay failed")
	}
	replay, replayRequestID := us3RequirePaymentResponse(t, response, body, http.StatusOK, binding.OrderID, businessOrderNo, 100, "CNY", payment.ResultUnknown)
	us3RecordHTTP(t, artifacts, facts, 2, response.StatusCode, replayRequestID, binding.OrderID, string(replay.Result))

	beforeApplications := us3QueryApplicationSet(t, observer, 2)
	response, body, err = us1GetByBusinessOrderNo(ctx2, addr2, us2APIKeyA, businessOrderNo)
	if err != nil {
		t.Fatal("US3 restart generation 2 GET failed")
	}
	afterApplications := us3QueryApplicationSet(t, observer, 2)
	queryBinding, queryApplied := us3NewAppliedQueryBinding(t, observer, 2, beforeApplications, afterApplications, true)
	if !queryApplied {
		t.Fatal("US3 restart generation 2 GET lacked an applied QueryApplication")
	}
	wantGET := payment.ResultUnknown
	wantIssues := []string{"MOCK_RESULT_NOT_FOUND"}
	if point == testcontrol.PointMockCommitted {
		wantGET = mockResult
		wantIssues = []string{}
	}
	query, queryRequestID := us3RequirePaymentResponse(t, response, body, http.StatusOK, binding.OrderID, businessOrderNo, 100, "CNY", wantGET, wantIssues...)
	us3RecordHTTP(t, artifacts, facts, 2, response.StatusCode, queryRequestID, binding.OrderID, string(query.Result), wantIssues...)
	us3RecordQueryHTTP(t, facts, 2, queryRequestID, "BY_BUSINESS_ORDER_NO", query.Result != string(payment.ResultUnknown), queryBinding)

	after := us3ReadOrderFact(t, pool, clientID, businessOrderNo)
	transition := us3ActualTransition(t, before, after)
	if transition != "UNCHANGED" && transition != "UNKNOWN_TO_CONFIRMED" {
		t.Fatalf("US3 restart order facts formed an unsupported transition: %s", transition)
	}
	facts.Facts = us3Facts{Before: &before, After: &after, Transition: transition, StableFacts: us3FactsStable(us3Facts{Before: &before, After: &after, Transition: transition})}
	facts.Records = us3ReadRecords(t, pool, binding.OrderID)
	record, found := us3FindRecordByRequest(facts.Records, "QUERY", queryRequestID)
	if !found {
		t.Fatalf("US3 restart generation 2 GET lacks its actual QUERY record")
	}
	if point == testcontrol.PointMockCommitted {
		if record.Decision != "ACCEPT_CONFIRMATION" || record.ResultBefore != "UNKNOWN" ||
			record.ResultAfter != string(mockResult) || record.ReasonCode != "" {
			t.Fatalf("US3 restart mock-committed QUERY record mismatch: %+v", record)
		}
		if record.Evidence.PlatformReferenceID == nil || *record.Evidence.PlatformReferenceID != binding.OrderID ||
			record.Evidence.MockTransactionID == nil || *record.Evidence.MockTransactionID != mockID ||
			record.Evidence.Amount == nil || *record.Evidence.Amount != 100 ||
			record.Evidence.Currency == nil || *record.Evidence.Currency != "CNY" ||
			record.Evidence.Result == nil || *record.Evidence.Result != string(mockResult) {
			t.Fatalf("US3 restart mock-committed QUERY evidence is not fully matched: %+v", record.Evidence)
		}
	} else {
		if record.Decision != "KEEP_UNKNOWN" || record.ResultBefore != "UNKNOWN" ||
			record.ResultAfter != "UNKNOWN" || record.ReasonCode != "MOCK_RESULT_NOT_FOUND" {
			t.Fatalf("US3 restart registered-unpaid QUERY record mismatch: %+v", record)
		}
	}
	orders, mocks, traces := us1CountBusinessRows(t, pool, clientID)
	facts.PG = nil
	us3RecordSQL(t, artifacts, facts, 2, "ORDER", uint64(orders))
	us3RecordSQL(t, artifacts, facts, 2, "MOCK", uint64(mocks))
	us3RecordSQL(t, artifacts, facts, 2, "TRACE", uint64(traces))
	facts.Checks = us3Checks{CountsExact: false, StableFacts: true}
	facts.Queries = []us3Query{}
	facts.strictQueryApplications = true
	facts.Joint = &us3Joint{
		Kind: "CONTROLLED_TRUNCATED_RESTART", FirstGeneration: 1, SecondGeneration: 2,
		HeldPoint:  string(point),
		Limitation: "CROSS_GENERATION_EXACT_COUNTS_NOT_PROVABLE",
	}
	cleanupVerified := stopExit.Reaped && stopExit.GroupGone && stopExit.OutputComplete
	facts.Faults = []us3Fault{{
		Kind: "PROCESS_TERMINATED", SQLStateSource: "none",
		Cleanup: us3Cleanup{Kind: "PROCESS_TERMINAL", Removed: cleanupVerified, Verified: cleanupVerified},
	}}
	postErrorClass := "NONE"
	if post.err != nil {
		postErrorClass = "TRANSPORT_ERROR"
	}
	us3WriteScenarioEvidence(t, runID, scenarioName, "termination-evidence", struct {
		Scenario        string         `json:"scenario"`
		Generation      uint64         `json:"generation"`
		HeldPoint       string         `json:"held_point"`
		HeldElapsedMS   uint64         `json:"held_elapsed_ms"`
		BarrierBudgetMS uint64         `json:"barrier_budget_ms"`
		HeldNotExpired  bool           `json:"held_not_expired"`
		StopErrorClass  string         `json:"stop_error_class"`
		Forced          bool           `json:"forced"`
		TimedOut        bool           `json:"timed_out"`
		Signal          int            `json:"signal"`
		Exit            us3ProcessExit `json:"exit"`
		PostObserved    bool           `json:"post_observed"`
		PostResponse    bool           `json:"post_response"`
		PostBeforeHold  bool           `json:"post_before_hold"`
		PostErrorClass  string         `json:"post_error_class"`
	}{
		Scenario: scenarioName, Generation: 1, HeldPoint: string(point),
		HeldElapsedMS: uint64(heldElapsed / time.Millisecond), BarrierBudgetMS: uint64(us1BarrierBudget / time.Millisecond),
		HeldNotExpired: heldElapsed < us1BarrierBudget, StopErrorClass: "ERR_PROCESS",
		Forced: stopExit.Forced, TimedOut: stopExit.TimedOut, Signal: stopExit.Signal,
		Exit:         us3ProcessExit{Code: stopExit.Code, Canceled: stopExit.Canceled, Reaped: stopExit.Reaped, GroupGone: stopExit.GroupGone, OutputComplete: stopExit.OutputComplete},
		PostObserved: postObserved, PostResponse: postResponse, PostBeforeHold: postBeforeHold, PostErrorClass: postErrorClass,
	})
	us3FinalizeScenario(t, artifacts, facts, observer, process2, []uint64{1, 2}, &controlled)
	call1, ok := us3FindCall(facts, 1)
	if !ok || call1.Exact || call1.PaymentCalls != controlled.ObservedPrefix.Payment ||
		call1.QueryCalls != controlled.ObservedPrefix.Query {
		t.Fatalf("US3 restart generation 1 prefix calls mismatch: %+v", call1)
	}
	us3RequireActualCalls(t, facts, 2, 0, 1)
}

func us3RunRestartUnknown(t *testing.T, repoRoot, runID, dsn, binary string, confirm bool) {
	t.Helper()
	scenarioName := "restart_unknown_no_confirm"
	if confirm {
		scenarioName = "restart_unknown_then_confirmed"
	}
	facts := &us3Scenario{Name: scenarioName, Generations: []uint64{1, 2}, Transport: "RESPONSE"}
	facts.Commands = []us1ReportCommand{{Kind: us1ReportCommandBuild, Exit: 0}}
	artifacts := us3BeginArtifacts(t, repoRoot, runID, scenarioName)
	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	pool := us1OpenPool(t)
	t.Cleanup(func() { us1CleanupBusiness(t, pool, clientID, businessOrderNo) })

	socket := us1ObserverSocket(t)
	epoch := us1Epoch(t)
	observer := us3StartObserver(t, runID, epoch, socket)
	if err := observer.Expect(1); err != nil {
		t.Fatal("US3 restart-unknown observer expect generation 1 failed")
	}
	process1, addr1 := us3StartGeneration(t, artifacts, binary, repoRoot, dsn, clientID, us2APIKeyA, socket, runID, epoch, 1)
	barrier, err := observer.ArmNextCommittedPoint(1, testcontrol.PointMockCommitted)
	if err != nil {
		t.Fatal("US3 restart-unknown committed barrier arm failed")
	}
	ctx1, cancel1 := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel1()
	type postResult struct {
		response *http.Response
		body     []byte
		err      error
	}
	postCh := make(chan postResult, 1)
	go func() {
		response, body, err := us1PostPayment(ctx1, addr1, us2APIKeyA, businessOrderNo, 100, "CNY")
		postCh <- postResult{response: response, body: body, err: err}
	}()
	if _, err := barrier.Wait(ctx1); err != nil {
		t.Fatal("US3 restart-unknown committed barrier wait failed")
	}
	if err := barrier.Release(testcontrol.ActionReplyHidden); err != nil {
		t.Fatal("US3 restart-unknown reply-hidden release failed")
	}
	post := <-postCh
	if post.err != nil {
		t.Fatal("US3 restart-unknown generation 1 POST failed")
	}
	envelope := us1ParseEnvelope(t, post.body)
	if envelope.Payment == nil {
		t.Fatal("US3 restart-unknown generation 1 response lacks payment")
	}
	orderID := envelope.Payment.PaymentID
	firstPayment, firstRequestID := us3RequirePaymentResponse(t, post.response, post.body, http.StatusCreated, orderID, businessOrderNo, 100, "CNY", payment.ResultUnknown, "MOCK_RESULT_UNCONFIRMED")
	us3RecordHTTP(t, artifacts, facts, 1, post.response.StatusCode, firstRequestID, orderID, string(firstPayment.Result), "MOCK_RESULT_UNCONFIRMED")
	exit1 := us1StopProcess(t, process1)
	facts.Commands = append(facts.Commands, us1ReportCommand{Kind: us1ReportCommandStop, Exit: exit1.Code})
	receipt1, counts1, ok := observer.SealedGeneration(1)
	if !ok {
		t.Fatal("US3 restart-unknown generation 1 did not seal")
	}
	_ = receipt1
	mock := us1ReadMockState(t, pool, orderID)
	mockResult := payment.Result(mock.Result)
	before := us3ReadOrderFact(t, pool, clientID, businessOrderNo)
	ordersBefore, mocksBefore, tracesBefore := us1CountBusinessRows(t, pool, clientID)
	facts.countsBefore = us3Counts{
		PaymentCalls: counts1.Payment, QueryCalls: counts1.Query,
		OrderRows: uint64(ordersBefore), MockRows: uint64(mocksBefore), TraceRows: uint64(tracesBefore),
	}
	facts.countsBeforeSet = true

	if err := observer.Expect(2); err != nil {
		t.Fatal("US3 restart-unknown observer expect generation 2 failed")
	}
	process2, addr2 := us3StartGeneration(t, artifacts, binary, repoRoot, dsn, clientID, us2APIKeyA, socket, runID, epoch, 2)
	ctx2, cancel2 := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel2()
	wantGET := payment.ResultUnknown
	if confirm {
		wantGET = mockResult
	}
	requestID := ""
	if confirm {
		beforeApplications := us3QueryApplicationSet(t, observer, 2)
		response, body, err := us1GetByBusinessOrderNo(ctx2, addr2, us2APIKeyA, businessOrderNo)
		if err != nil {
			t.Fatal("US3 restart-unknown confirm GET failed")
		}
		afterApplications := us3QueryApplicationSet(t, observer, 2)
		queryBinding, queryApplied := us3NewAppliedQueryBinding(t, observer, 2, beforeApplications, afterApplications, true)
		if !queryApplied {
			t.Fatal("US3 restart-unknown confirm GET lacked an applied QueryApplication")
		}
		query, id := us3RequirePaymentResponse(t, response, body, http.StatusOK, orderID, businessOrderNo, 100, "CNY", wantGET)
		us3RecordHTTP(t, artifacts, facts, 2, response.StatusCode, id, orderID, string(query.Result))
		requestID = id
		us3RecordQueryHTTP(t, facts, 2, id, "BY_BUSINESS_ORDER_NO", query.Result != string(payment.ResultUnknown), queryBinding)
	} else {
		hold, err := observer.HoldQueryEntry(2)
		if err != nil {
			t.Fatal("US3 restart-unknown notvisible hold arm failed")
		}
		type result struct {
			response *http.Response
			body     []byte
			err      error
		}
		ch := make(chan result, 1)
		go func() {
			response, body, err := us1GetByBusinessOrderNo(ctx2, addr2, us2APIKeyA, businessOrderNo)
			ch <- result{response: response, body: body, err: err}
		}()
		heldBinding, err := hold.Wait(ctx2)
		if err != nil {
			t.Fatal("US3 restart-unknown notvisible hold wait failed")
		}
		if err := hold.Release(testcontrol.QueryScenarioNotVisible); err != nil {
			t.Fatal("US3 restart-unknown notvisible hold release failed")
		}
		got := <-ch
		if got.err != nil {
			t.Fatal("US3 restart-unknown notvisible GET failed")
		}
		query, id := us3RequirePaymentResponse(t, got.response, got.body, http.StatusOK, orderID, businessOrderNo, 100, "CNY", wantGET, "MOCK_RESULT_NOT_FOUND")
		us3RecordHTTP(t, artifacts, facts, 2, got.response.StatusCode, id, orderID, string(query.Result), "MOCK_RESULT_NOT_FOUND")
		requestID = id
		us3RecordQueryHTTP(t, facts, 2, id, "BY_BUSINESS_ORDER_NO", false, us3QueryBindingFromHold(heldBinding), "MOCK_RESULT_NOT_FOUND")
	}

	after := us3ReadOrderFact(t, pool, clientID, businessOrderNo)
	transition := us3ActualTransition(t, before, after)
	wantTransition := "UNCHANGED"
	if confirm {
		wantTransition = "UNKNOWN_TO_CONFIRMED"
	}
	if transition != wantTransition {
		t.Fatalf("US3 restart-unknown transition mismatch: got %s want %s", transition, wantTransition)
	}
	facts.Facts = us3Facts{Before: &before, After: &after, Transition: transition, StableFacts: us3FactsStable(us3Facts{Before: &before, After: &after, Transition: transition})}
	facts.Records = us3ReadRecords(t, pool, orderID)
	record, found := us3FindRecordByRequest(facts.Records, "QUERY", requestID)
	if !found {
		t.Fatalf("US3 restart-unknown actual QUERY record is missing")
	}
	wantDecision := "KEEP_UNKNOWN"
	if confirm {
		wantDecision = "ACCEPT_CONFIRMATION"
	}
	if record.Decision != wantDecision {
		t.Fatalf("US3 restart-unknown actual decision mismatch: got %s want %s", record.Decision, wantDecision)
	}
	orders, mocks, traces := us1CountBusinessRows(t, pool, clientID)
	facts.PG = nil
	us3RecordSQL(t, artifacts, facts, 2, "ORDER", uint64(orders))
	us3RecordSQL(t, artifacts, facts, 2, "MOCK", uint64(mocks))
	us3RecordSQL(t, artifacts, facts, 2, "TRACE", uint64(traces))
	facts.Checks = us3Checks{CountsExact: true, StableFacts: facts.Facts.StableFacts}
	facts.Queries = []us3Query{}
	facts.strictQueryApplications = true
	if !confirm {
		facts.Faults = []us3Fault{{
			Kind: "MOCK_QUERY_NOT_VISIBLE", SQLStateSource: "none",
			Cleanup: us3Cleanup{Kind: "UDS_SCENARIO", Removed: true, Verified: true},
		}}
	}
	us3FinalizeScenario(t, artifacts, facts, observer, process2, []uint64{1, 2}, nil)
	us3RequireActualCalls(t, facts, 1, 1, 0)
	us3RequireActualCalls(t, facts, 2, 0, 1)
}
