//go:build integration

package tests

import (
	"context"
	"net/http"
	"testing"
	"time"

	"payment-hub/internal/payment"
	"payment-hub/internal/testcontrol"
)

func TestUS3Query(t *testing.T) {
	repoRoot := us1RepoRoot(t)
	runID := us1RunID(t)
	dsn := us1TestDatabaseURL(t)
	binary := us1BuildApplication(t, repoRoot)

	t.Run("submit unknown no transaction", func(t *testing.T) {
		us3RunSubmitUnknownNoTransaction(t, repoRoot, runID, dsn, binary)
	})
	t.Run("submit commit unknown", func(t *testing.T) {
		us3RunSubmitCommitUnknown(t, repoRoot, runID, dsn, binary)
	})
	t.Run("unknown confirmed", func(t *testing.T) {
		us3RunQueryCase(t, repoRoot, runID, dsn, binary, us3QueryCase{
			name: "query_unknown_confirmed", seedUnknown: true, seedMock: true,
			wantStatus: http.StatusOK, wantResult: payment.ResultSucceeded,
			wantDecision: "ACCEPT_CONFIRMATION", wantQueryDecision: "ACCEPT_CONFIRMATION",
			wantOutcome: testcontrol.QueryOutcomeFound, wantTrace: "SAVED",
			wantConfirmed: true, wantAfter: "SUCCEEDED", wantTransition: "UNKNOWN_TO_CONFIRMED",
			path: "BY_BUSINESS_ORDER_NO",
		})
	})
	t.Run("unknown failure", func(t *testing.T) {
		us3RunQueryCase(t, repoRoot, runID, dsn, binary, us3QueryCase{
			name: "query_unknown_failure", seedUnknown: true, seedMock: true,
			directive:  testcontrol.QueryScenarioFailure,
			wantStatus: http.StatusOK, wantResult: payment.ResultUnknown,
			wantIssues:   []string{"MOCK_RESULT_UNCONFIRMED"},
			wantDecision: "KEEP_UNKNOWN", wantQueryDecision: "KEEP_UNKNOWN",
			wantOutcome: testcontrol.QueryOutcomeUnavailable, wantTrace: "SAVED",
			wantAfter: "UNKNOWN", wantTransition: "UNCHANGED",
			path: "BY_BUSINESS_ORDER_NO",
		})
	})
	t.Run("unknown subtimeout", func(t *testing.T) {
		us3RunQueryCase(t, repoRoot, runID, dsn, binary, us3QueryCase{
			name: "query_unknown_subtimeout", seedUnknown: true, seedMock: true,
			directive:  testcontrol.QueryScenarioSubtimeout,
			wantStatus: http.StatusOK, wantResult: payment.ResultUnknown,
			wantIssues:   []string{"MOCK_RESULT_UNCONFIRMED"},
			wantDecision: "KEEP_UNKNOWN", wantQueryDecision: "KEEP_UNKNOWN",
			wantOutcome: testcontrol.QueryOutcomeSubtimeout, wantTrace: "SAVED",
			wantAfter: "UNKNOWN", wantTransition: "UNCHANGED",
			path: "BY_BUSINESS_ORDER_NO", subtimeout: true,
		})
	})
	t.Run("unknown not visible then visible", func(t *testing.T) {
		us3RunQueryCase(t, repoRoot, runID, dsn, binary, us3QueryCase{
			name: "query_unknown_not_visible", seedUnknown: true, seedMock: true,
			directive:  testcontrol.QueryScenarioNotVisible,
			wantStatus: http.StatusOK, wantResult: payment.ResultUnknown,
			wantIssues:   []string{"MOCK_RESULT_NOT_FOUND"},
			wantDecision: "KEEP_UNKNOWN", wantQueryDecision: "KEEP_UNKNOWN",
			wantOutcome: testcontrol.QueryOutcomeNotFound, wantTrace: "SAVED",
			wantAfter: "UNKNOWN", wantTransition: "UNKNOWN_TO_CONFIRMED",
			path: "BY_BUSINESS_ORDER_NO", secondNormal: true,
		})
	})
	t.Run("unknown no mock match", func(t *testing.T) {
		us3RunQueryCase(t, repoRoot, runID, dsn, binary, us3QueryCase{
			name: "query_unknown_no_mock_match", seedUnknown: true, seedMock: false,
			wantStatus: http.StatusOK, wantResult: payment.ResultUnknown,
			wantIssues:   []string{"MOCK_RESULT_NOT_FOUND"},
			wantDecision: "KEEP_UNKNOWN", wantQueryDecision: "KEEP_UNKNOWN",
			wantOutcome: testcontrol.QueryOutcomeNotFound, wantTrace: "SAVED",
			wantAfter: "UNKNOWN", wantTransition: "UNCHANGED",
			path: "BY_BUSINESS_ORDER_NO",
		})
	})
	t.Run("platform not found", func(t *testing.T) {
		us3RunNotFoundCase(t, repoRoot, runID, dsn, binary)
	})
	t.Run("confirmed plain", func(t *testing.T) {
		us3RunConfirmedPlain(t, repoRoot, runID, dsn, binary)
	})
	for _, mismatch := range []struct {
		name      string
		directive testcontrol.QueryScenario
	}{
		{"query_mismatch_reference", testcontrol.QueryScenarioMismatchReference},
		{"query_mismatch_amount", testcontrol.QueryScenarioMismatchAmount},
		{"query_mismatch_currency", testcontrol.QueryScenarioMismatchCurrency},
		{"query_mismatch_transaction", testcontrol.QueryScenarioMismatchTransaction},
	} {
		mismatch := mismatch
		t.Run(mismatch.name, func(t *testing.T) {
			us3RunQueryCase(t, repoRoot, runID, dsn, binary, us3QueryCase{
				name: mismatch.name, seedUnknown: true, seedMock: true,
				directive:  mismatch.directive,
				wantStatus: http.StatusOK, wantResult: payment.ResultUnknown,
				wantIssues:   []string{"MOCK_EVIDENCE_REJECTED"},
				wantDecision: "REJECT_EVIDENCE", wantQueryDecision: "REJECT_EVIDENCE",
				wantOutcome: testcontrol.QueryOutcomeMismatchApplied, wantTrace: "SAVED",
				wantAfter: "UNKNOWN", wantTransition: "UNCHANGED",
				path: "BY_BUSINESS_ORDER_NO",
			})
		})
	}
	t.Run("result commit unknown", func(t *testing.T) {
		us3RunResultCommitUnknown(t, repoRoot, runID, dsn, binary)
	})
}

type us3QueryCase struct {
	name              string
	seedUnknown       bool
	seedMock          bool
	directive         testcontrol.QueryScenario
	wantStatus        int
	wantResult        payment.Result
	wantIssues        []string
	wantDecision      string
	wantQueryDecision string
	wantOutcome       testcontrol.QueryOutcome
	wantTrace         string
	wantConfirmed     bool
	wantAfter         string
	wantTransition    string
	path              string
	subtimeout        bool
	secondNormal      bool
	expectedMockID    string
}

func us3RunQueryCase(t *testing.T, repoRoot, runID, dsn, binary string, tc us3QueryCase) {
	t.Helper()
	facts := &us3Scenario{Name: tc.name, Generations: []uint64{1}, Transport: "RESPONSE"}
	facts.Commands = []us1ReportCommand{{Kind: us1ReportCommandBuild, Exit: 0}}
	artifacts := us3BeginArtifacts(t, repoRoot, runID, tc.name)

	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	pool := us1OpenPool(t)
	t.Cleanup(func() { us1CleanupBusiness(t, pool, clientID, businessOrderNo) })

	orderID := ""
	mockID := ""
	if tc.seedMock {
		mockID = payment.NewUUIDv4()
	}
	if tc.seedUnknown {
		var seedMockID *string
		if tc.name == "query_mismatch_transaction" {
			seedMockID = &mockID
		}
		orderID = us3SeedOrder(t, pool, clientID, businessOrderNo, 100, payment.ResultUnknown, seedMockID, nil)
	}
	if tc.seedMock {
		us3SeedMock(t, pool, mockID, orderID, 100, payment.ResultSucceeded, time.Now().UTC())
		if tc.name == "query_mismatch_transaction" {
			tc.expectedMockID = mockID
			facts.expectedMockTransaction = mockID
		}
	}
	before := us3ReadOrderFact(t, pool, clientID, businessOrderNo)
	if tc.name == "query_mismatch_transaction" {
		if before.Result != "UNKNOWN" || before.MockTransactionID == nil ||
			*before.MockTransactionID != mockID || before.ConfirmedAt != nil {
			t.Fatalf("US3 mismatch-transaction seed is not UNKNOWN with the trusted Mock ID")
		}
	}
	us3CaptureBeforeCounts(t, pool, clientID, facts, 0, 0)

	socket := us1ObserverSocket(t)
	epoch := us1Epoch(t)
	observer := us3StartObserver(t, runID, epoch, socket)
	if err := observer.Expect(1); err != nil {
		t.Fatal("US3 query observer expect failed")
	}
	process, addr := us3StartGeneration(t, artifacts, binary, repoRoot, dsn, clientID, us2APIKeyA, socket, runID, epoch, 1)

	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	subtimeoutElapsed := time.Duration(0)
	var subtimeoutStart time.Time
	firstRequestID := ""
	secondRequestID := ""
	if tc.directive != "" {
		hold, err := observer.HoldQueryEntry(1)
		if err != nil {
			t.Fatal("US3 query hold arm failed")
		}
		type result struct {
			response *http.Response
			body     []byte
			err      error
		}
		ch := make(chan result, 1)
		go func() {
			response, body, err := us1GetByBusinessOrderNo(ctx, addr, us2APIKeyA, businessOrderNo)
			ch <- result{response: response, body: body, err: err}
		}()
		heldBinding, err := hold.Wait(ctx)
		if err != nil {
			t.Fatal("US3 query hold binding failed")
		}
		if tc.subtimeout {
			subtimeoutStart = time.Now()
		}
		if err := hold.Release(tc.directive); err != nil {
			t.Fatal("US3 query hold release failed")
		}
		if tc.subtimeout {
			subtimeoutElapsed = us3WaitQueryApplication(t, observer, 1, heldBinding, subtimeoutStart)
		}
		post := <-ch
		if post.err != nil {
			t.Fatal("US3 query GET failed")
		}
		paymentBody, requestID := us3RequirePaymentResponse(t, post.response, post.body, tc.wantStatus, orderID, businessOrderNo, 100, "CNY", tc.wantResult, tc.wantIssues...)
		us3RecordHTTP(t, artifacts, facts, 1, post.response.StatusCode, requestID, orderID, string(paymentBody.Result), tc.wantIssues...)
		us3RecordQueryHTTP(t, facts, 1, requestID, tc.path, paymentBody.Result != string(payment.ResultUnknown), us3QueryBindingFromHold(heldBinding), tc.wantIssues...)
		firstRequestID = requestID
	} else {
		beforeApplications := us3QueryApplicationSet(t, observer, 1)
		response, body, err := us1GetByBusinessOrderNo(ctx, addr, us2APIKeyA, businessOrderNo)
		if err != nil {
			t.Fatal("US3 query GET failed")
		}
		afterApplications := us3QueryApplicationSet(t, observer, 1)
		binding, ok := us3NewAppliedQueryBinding(t, observer, 1, beforeApplications, afterApplications, true)
		if !ok {
			t.Fatal("US3 query GET lacked an applied QueryApplication")
		}
		paymentBody, requestID := us3RequirePaymentResponse(t, response, body, tc.wantStatus, orderID, businessOrderNo, 100, "CNY", tc.wantResult, tc.wantIssues...)
		us3RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, orderID, string(paymentBody.Result), tc.wantIssues...)
		us3RecordQueryHTTP(t, facts, 1, requestID, tc.path, paymentBody.Result != string(payment.ResultUnknown), binding, tc.wantIssues...)
		firstRequestID = requestID
	}
	if tc.secondNormal {
		beforeApplications := us3QueryApplicationSet(t, observer, 1)
		response, body, err := us1GetByBusinessOrderNo(ctx, addr, us2APIKeyA, businessOrderNo)
		if err != nil {
			t.Fatal("US3 second normal GET failed")
		}
		afterApplications := us3QueryApplicationSet(t, observer, 1)
		binding, ok := us3NewAppliedQueryBinding(t, observer, 1, beforeApplications, afterApplications, true)
		if !ok {
			t.Fatal("US3 second normal GET lacked an applied QueryApplication")
		}
		paymentBody, requestID := us3RequirePaymentResponse(t, response, body, http.StatusOK, orderID, businessOrderNo, 100, "CNY", payment.ResultSucceeded)
		us3RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, orderID, string(paymentBody.Result))
		us3RecordQueryHTTP(t, facts, 1, requestID, tc.path, true, binding)
		secondRequestID = requestID
	}

	after := us3ReadOrderFact(t, pool, clientID, businessOrderNo)
	transition := us3ActualTransition(t, before, after)
	if transition != tc.wantTransition {
		t.Fatalf("US3 query transition mismatch: got %s want %s", transition, tc.wantTransition)
	}
	facts.Facts = us3Facts{Before: &before, After: &after, Transition: transition, StableFacts: us3FactsStable(us3Facts{Before: &before, After: &after, Transition: transition})}
	facts.Records = us3ReadRecords(t, pool, orderID)
	if tc.wantQueryDecision != "" && firstRequestID != "" {
		record, found := us3FindRecordByRequest(facts.Records, "QUERY", firstRequestID)
		if !found || record.Decision != tc.wantQueryDecision {
			t.Fatalf("US3 query actual decision mismatch: got %s want %s", record.Decision, tc.wantQueryDecision)
		}
	}
	if tc.secondNormal {
		record, found := us3FindRecordByRequest(facts.Records, "QUERY", secondRequestID)
		if !found || record.Decision != "ACCEPT_CONFIRMATION" {
			t.Fatalf("US3 second normal actual ACCEPT record is missing")
		}
	}
	orders, mocks, traces := us1CountBusinessRows(t, pool, clientID)
	facts.PG = nil
	us3RecordSQL(t, artifacts, facts, 1, "ORDER", uint64(orders))
	us3RecordSQL(t, artifacts, facts, 1, "MOCK", uint64(mocks))
	us3RecordSQL(t, artifacts, facts, 1, "TRACE", uint64(traces))
	facts.Checks = us3Checks{CountsExact: true, StableFacts: facts.Facts.StableFacts}
	if tc.subtimeout {
		elapsed := subtimeoutElapsed
		if elapsed == 0 {
			t.Fatal("US3 query subtimeout elapsed was not measured")
		}
		if elapsed < 250*time.Millisecond {
			t.Fatalf("US3 query subtimeout returned before the real child deadline: %s", elapsed)
		}
		if elapsed > 3*time.Second {
			t.Fatalf("US3 query subtimeout exceeded the real 3s Mock operation budget: %s", elapsed)
		}
		us3RecordBudget(t, artifacts, facts, 1, "CONTROLLED", "QUERY_SUBTIMEOUT", "APP",
			false, 250*time.Millisecond, elapsed)
		us3RecordBudget(t, artifacts, facts, 1, "NORMAL", "MOCK", "MOCK",
			true, 3*time.Second, elapsed)
	}
	facts.strictQueryApplications = true
	facts.Faults = us3QueryCaseFaults(tc)
	us3FinalizeScenario(t, artifacts, facts, observer, process, []uint64{1}, nil)
	us3RequireActualCalls(t, facts, 1, 0, queryCallCount(tc))
}

func us3QueryCaseFaults(tc us3QueryCase) []us3Fault {
	kind := ""
	switch tc.name {
	case "query_unknown_failure":
		kind = "MOCK_QUERY_FAILURE"
	case "query_unknown_subtimeout":
		kind = "MOCK_QUERY_SUBTIMEOUT_250MS"
	case "query_unknown_not_visible":
		kind = "MOCK_QUERY_NOT_VISIBLE"
	case "query_mismatch_reference":
		kind = "MISMATCH_REFERENCE"
	case "query_mismatch_amount":
		kind = "MISMATCH_AMOUNT"
	case "query_mismatch_currency":
		kind = "MISMATCH_CURRENCY"
	case "query_mismatch_transaction":
		kind = "MISMATCH_TRANSACTION"
	}
	if kind == "" {
		return []us3Fault{}
	}
	return []us3Fault{{
		Kind: kind, SQLStateSource: "none",
		Cleanup: us3Cleanup{Kind: "UDS_SCENARIO", Removed: true, Verified: true},
	}}
}

func us3RunNotFoundCase(t *testing.T, repoRoot, runID, dsn, binary string) {
	t.Helper()
	facts := &us3Scenario{Name: "query_not_found", Generations: []uint64{1}, Transport: "RESPONSE"}
	facts.Commands = []us1ReportCommand{{Kind: us1ReportCommandBuild, Exit: 0}}
	artifacts := us3BeginArtifacts(t, repoRoot, runID, "query_not_found")
	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	pool := us1OpenPool(t)
	t.Cleanup(func() { us1CleanupBusiness(t, pool, clientID, businessOrderNo) })
	us3CaptureBeforeCounts(t, pool, clientID, facts, 0, 0)

	socket := us1ObserverSocket(t)
	epoch := us1Epoch(t)
	observer := us3StartObserver(t, runID, epoch, socket)
	if err := observer.Expect(1); err != nil {
		t.Fatal("US3 not-found observer expect failed")
	}
	process, addr := us3StartGeneration(t, artifacts, binary, repoRoot, dsn, clientID, us2APIKeyA, socket, runID, epoch, 1)
	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	response, body, err := us1GetByBusinessOrderNo(ctx, addr, us2APIKeyA, businessOrderNo)
	if err != nil {
		t.Fatal("US3 not-found GET failed")
	}
	requestID := us1RequireErrorResponse(t, response, body, http.StatusNotFound, "PAYMENT_NOT_FOUND")
	us3RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, "", "")
	orders, mocks, traces := us1CountBusinessRows(t, pool, clientID)
	if orders != 0 || mocks != 0 || traces != 0 {
		t.Fatalf("US3 not-found left business rows: order=%d mock=%d trace=%d", orders, mocks, traces)
	}
	facts.PG = nil
	us3RecordSQL(t, artifacts, facts, 1, "ORDER", uint64(orders))
	us3RecordSQL(t, artifacts, facts, 1, "MOCK", uint64(mocks))
	us3RecordSQL(t, artifacts, facts, 1, "TRACE", uint64(traces))
	facts.Facts = us3Facts{Transition: "NONE", StableFacts: true}
	facts.Checks = us3Checks{CountsExact: true, StableFacts: true}
	facts.Queries = []us3Query{}
	us3FinalizeScenario(t, artifacts, facts, observer, process, []uint64{1}, nil)
	us3RequireActualCalls(t, facts, 1, 0, 0)
}

func boolCount(value bool) uint64 {
	if value {
		return 1
	}
	return 0
}

func queryCallCount(tc us3QueryCase) uint64 {
	if tc.secondNormal {
		return 2
	}
	return 1
}

func us3RunResultCommitUnknown(t *testing.T, repoRoot, runID, dsn, binary string) {
	t.Helper()
	facts := &us3Scenario{Name: "query_result_commit_unknown", Generations: []uint64{1}, Transport: "RESPONSE"}
	facts.Commands = []us1ReportCommand{{Kind: us1ReportCommandBuild, Exit: 0}}
	artifacts := us3BeginArtifacts(t, repoRoot, runID, "query_result_commit_unknown")
	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	pool := us1OpenPool(t)
	t.Cleanup(func() { us1CleanupBusiness(t, pool, clientID, businessOrderNo) })

	orderID := us3SeedOrder(t, pool, clientID, businessOrderNo, 100, payment.ResultUnknown, nil, nil)
	mockID := payment.NewUUIDv4()
	us3SeedMock(t, pool, mockID, orderID, 100, payment.ResultSucceeded, time.Now().UTC())
	before := us3ReadOrderFact(t, pool, clientID, businessOrderNo)
	us3CaptureBeforeCounts(t, pool, clientID, facts, 0, 0)

	socket := us1ObserverSocket(t)
	epoch := us1Epoch(t)
	observer := us3StartObserver(t, runID, epoch, socket)
	if err := observer.Expect(1); err != nil {
		t.Fatal("US3 result-commit observer expect failed")
	}
	process, addr := us3StartGeneration(t, artifacts, binary, repoRoot, dsn, clientID, us2APIKeyA, socket, runID, epoch, 1)
	barrier, err := observer.Arm(1, testcontrol.PointResultCommitted, orderID)
	if err != nil {
		t.Fatal("US3 result-commit barrier arm failed")
	}
	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	type result struct {
		response *http.Response
		body     []byte
		err      error
	}
	beforeApplications := us3QueryApplicationSet(t, observer, 1)
	ch := make(chan result, 1)
	go func() {
		response, body, err := us1GetByBusinessOrderNo(ctx, addr, us2APIKeyA, businessOrderNo)
		ch <- result{response: response, body: body, err: err}
	}()
	if _, err := barrier.Wait(ctx); err != nil {
		t.Fatal("US3 result-commit barrier wait failed")
	}
	if err := barrier.Release(testcontrol.ActionReplyHidden); err != nil {
		t.Fatal("US3 result-commit reply-hidden release failed")
	}
	post := <-ch
	if post.err != nil {
		t.Fatal("US3 result-commit GET failed")
	}
	afterApplications := us3QueryApplicationSet(t, observer, 1)
	binding, ok := us3NewAppliedQueryBinding(t, observer, 1, beforeApplications, afterApplications, true)
	if !ok {
		t.Fatal("US3 result-commit GET lacked an applied QueryApplication")
	}
	paymentBody, requestID := us3RequirePaymentResponse(t, post.response, post.body, http.StatusOK, orderID, businessOrderNo, 100, "CNY", payment.ResultSucceeded, "RESULT_SAVE_ERROR")
	us3RecordHTTP(t, artifacts, facts, 1, post.response.StatusCode, requestID, orderID, string(paymentBody.Result), "RESULT_SAVE_ERROR")
	us3RecordQueryHTTP(t, facts, 1, requestID, "BY_BUSINESS_ORDER_NO", true, binding)

	after := us3ReadOrderFact(t, pool, clientID, businessOrderNo)
	transition := us3ActualTransition(t, before, after)
	if transition != "UNKNOWN_TO_CONFIRMED" {
		t.Fatalf("US3 result-commit order facts transition mismatch: %s", transition)
	}
	facts.Facts = us3Facts{Before: &before, After: &after, Transition: transition, StableFacts: us3FactsStable(us3Facts{Before: &before, After: &after, Transition: transition})}
	facts.Records = us3ReadRecords(t, pool, orderID)
	record, found := us3FindRecordByRequest(facts.Records, "QUERY", requestID)
	if !found {
		t.Fatalf("US3 result-commit actual QUERY ACCEPT record for request %s is missing", requestID)
	}
	if record.Decision != "ACCEPT_CONFIRMATION" || record.ResultBefore != "UNKNOWN" || record.ResultAfter != "SUCCEEDED" {
		t.Fatalf("US3 result-commit actual QUERY record mismatch: decision=%s before=%s after=%s",
			record.Decision, record.ResultBefore, record.ResultAfter)
	}
	if record.Evidence.PlatformReferenceID == nil || *record.Evidence.PlatformReferenceID != orderID {
		t.Fatalf("US3 result-commit actual QUERY record is not bound to order %s", orderID)
	}
	orders, mocks, traces := us1CountBusinessRows(t, pool, clientID)
	facts.PG = nil
	us3RecordSQL(t, artifacts, facts, 1, "ORDER", uint64(orders))
	us3RecordSQL(t, artifacts, facts, 1, "MOCK", uint64(mocks))
	us3RecordSQL(t, artifacts, facts, 1, "TRACE", uint64(traces))
	facts.Checks = us3Checks{CountsExact: true, StableFacts: facts.Facts.StableFacts}
	facts.Save = &us3Save{
		Attempts: 1, RecordID: record.RecordID, CommitState: "INDETERMINATE",
		ApplicationReadback: "ORDER_ONLY", ExternalPGReadback: "ORDER_AND_RECORD",
		ReadbackVerified: true, ReadbackScope: "INDEPENDENT_ORDER_AND_RECORD",
		ReadbackSource: "APP_AND_EXTERNAL",
	}
	facts.Faults = []us3Fault{{
		Kind: "RESULT_COMMIT_REPLY_HIDDEN", SQLStateSource: "none",
		Cleanup: us3Cleanup{Kind: "UDS_SCENARIO", Removed: true, Verified: true},
	}}
	facts.strictQueryApplications = true
	us3FinalizeScenario(t, artifacts, facts, observer, process, []uint64{1}, nil)
	us3RequireActualCalls(t, facts, 1, 0, 1)
}

func us3RunConfirmedPlain(t *testing.T, repoRoot, runID, dsn, binary string) {
	t.Helper()
	facts := &us3Scenario{Name: "query_confirmed_plain", Generations: []uint64{1}, Transport: "RESPONSE"}
	facts.Commands = []us1ReportCommand{{Kind: us1ReportCommandBuild, Exit: 0}}
	artifacts := us3BeginArtifacts(t, repoRoot, runID, "query_confirmed_plain")
	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	pool := us1OpenPool(t)
	t.Cleanup(func() { us1CleanupBusiness(t, pool, clientID, businessOrderNo) })

	mockID := payment.NewUUIDv4()
	confirmedAt := time.Now().UTC()
	orderID := us3SeedOrder(t, pool, clientID, businessOrderNo, 100, payment.ResultSucceeded, &mockID, &confirmedAt)
	us3SeedMock(t, pool, mockID, orderID, 100, payment.ResultSucceeded, time.Now().UTC())
	before := us3ReadOrderFact(t, pool, clientID, businessOrderNo)
	us3CaptureBeforeCounts(t, pool, clientID, facts, 0, 0)

	socket := us1ObserverSocket(t)
	epoch := us1Epoch(t)
	observer := us3StartObserver(t, runID, epoch, socket)
	if err := observer.Expect(1); err != nil {
		t.Fatal("US3 confirmed-plain observer expect failed")
	}
	process, addr := us3StartGeneration(t, artifacts, binary, repoRoot, dsn, clientID, us2APIKeyA, socket, runID, epoch, 1)
	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	response, body, err := us1GetByBusinessOrderNo(ctx, addr, us2APIKeyA, businessOrderNo)
	if err != nil {
		t.Fatal("US3 confirmed-plain GET failed")
	}
	paymentBody, requestID := us3RequirePaymentResponse(t, response, body, http.StatusOK, orderID, businessOrderNo, 100, "CNY", payment.ResultSucceeded)
	us3RecordHTTP(t, artifacts, facts, 1, response.StatusCode, requestID, orderID, string(paymentBody.Result))

	after := us3ReadOrderFact(t, pool, clientID, businessOrderNo)
	transition := us3ActualTransition(t, before, after)
	if transition != "UNCHANGED" {
		t.Fatalf("US3 confirmed-plain order facts changed: %s", transition)
	}
	facts.Facts = us3Facts{Before: &before, After: &after, Transition: transition, StableFacts: true}
	facts.Records = us3ReadRecords(t, pool, orderID)
	if len(facts.Records) != 0 {
		t.Fatalf("US3 confirmed-plain wrote %d handling records", len(facts.Records))
	}
	orders, mocks, traces := us1CountBusinessRows(t, pool, clientID)
	facts.PG = nil
	us3RecordSQL(t, artifacts, facts, 1, "ORDER", uint64(orders))
	us3RecordSQL(t, artifacts, facts, 1, "MOCK", uint64(mocks))
	us3RecordSQL(t, artifacts, facts, 1, "TRACE", uint64(traces))
	facts.Checks = us3Checks{CountsExact: true, StableFacts: true}
	facts.Queries = []us3Query{}
	us3FinalizeScenario(t, artifacts, facts, observer, process, []uint64{1}, nil)
	us3RequireActualCalls(t, facts, 1, 0, 0)
}

func us3RunSubmitUnknownNoTransaction(t *testing.T, repoRoot, runID, dsn, binary string) {
	t.Helper()
	facts := &us3Scenario{Name: "submit_unknown_no_transaction", Generations: []uint64{1}, Transport: "RESPONSE"}
	facts.Commands = []us1ReportCommand{{Kind: us1ReportCommandBuild, Exit: 0}}
	artifacts := us3BeginArtifacts(t, repoRoot, runID, facts.Name)
	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	pool := us1OpenPool(t)
	t.Cleanup(func() { us1CleanupBusiness(t, pool, clientID, businessOrderNo) })
	us3CaptureBeforeCounts(t, pool, clientID, facts, 0, 0)

	socket := us1ObserverSocket(t)
	epoch := us1Epoch(t)
	observer := us3StartObserver(t, runID, epoch, socket)
	if err := observer.Expect(1); err != nil {
		t.Fatal("US3 submit-unknown observer expect failed")
	}
	process, addr := us3StartGeneration(t, artifacts, binary, repoRoot, dsn, clientID, us2APIKeyA, socket, runID, epoch, 1)
	releaseMock, blockerPID := us3LockMockTransactions(t, pool)
	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	type result struct {
		response *http.Response
		body     []byte
		err      error
	}
	ch := make(chan result, 1)
	postStart := time.Now()
	go func() {
		response, body, err := us1PostPayment(ctx, addr, us2APIKeyA, businessOrderNo, 100, "CNY")
		ch <- result{response: response, body: body, err: err}
	}()
	waiter := us3WaitSingleMockInsertWaiter(t, pool, blockerPID)
	// Hold the lock until the captured backend/xact identity is gone; a
	// cancelled lock wait alone is not transaction termination.
	targetGoneElapsed := us3WaitMockTargetTransactionGone(t, pool, waiter)
	post := <-ch
	requestElapsed := time.Since(postStart)
	if requestElapsed > us1RequestBudget {
		t.Fatalf("US3 submit-unknown request exceeded the original request deadline: %s", requestElapsed)
	}
	if post.err != nil {
		t.Fatal("US3 submit-unknown POST failed")
	}
	envelope := us1ParseEnvelope(t, post.body)
	if envelope.Payment == nil {
		t.Fatal("US3 submit-unknown response lacks payment")
	}
	orderID := envelope.Payment.PaymentID
	paymentBody, requestID := us3RequirePaymentResponse(t, post.response, post.body, http.StatusCreated, orderID, businessOrderNo, 100, "CNY", payment.ResultUnknown, "MOCK_RESULT_UNCONFIRMED")
	us3RecordHTTP(t, artifacts, facts, 1, post.response.StatusCode, requestID, orderID, string(paymentBody.Result), "MOCK_RESULT_UNCONFIRMED")

	// The target transaction is already gone by captured identity. Release the
	// lock and prove the lock and waiter are absent before reading final facts.
	releaseMock()
	us3RequireLockGone(t, pool, blockerPID, waiter.RelationOID)
	us3RequireNoMockRowForOrder(t, pool, orderID, 500*time.Millisecond)

	after := us3ReadOrderFact(t, pool, clientID, businessOrderNo)
	if after.Result != "UNKNOWN" || after.MockTransactionID != nil || after.ConfirmedAt != nil {
		t.Fatalf("US3 submit-unknown order fact is not a clean UNKNOWN: %+v", after)
	}
	facts.Facts = us3Facts{After: &after, Transition: "CREATED_UNKNOWN", StableFacts: false}
	facts.Records = us3ReadRecords(t, pool, orderID)
	orders, mocks, traces := us1CountBusinessRows(t, pool, clientID)
	if orders != 1 || mocks != 0 || traces != 1 {
		t.Fatalf("US3 submit-unknown counts mismatch: order=%d mock=%d trace=%d", orders, mocks, traces)
	}
	record, found := us3FindRecordByDecision(facts.Records, "PAYMENT", "KEEP_UNKNOWN")
	if !found || len(facts.Records) != 1 {
		t.Fatalf("US3 submit-unknown actual PAYMENT KEEP_UNKNOWN record missing")
	}
	if record.ResultBefore != "UNKNOWN" || record.ResultAfter != "UNKNOWN" || record.ReasonCode != "MOCK_RESULT_UNCONFIRMED" {
		t.Fatalf("US3 submit-unknown actual PAYMENT record mismatch: before=%s after=%s reason=%s",
			record.ResultBefore, record.ResultAfter, record.ReasonCode)
	}
	facts.PG = nil
	us3RecordSQL(t, artifacts, facts, 1, "ORDER", uint64(orders))
	us3RecordSQL(t, artifacts, facts, 1, "MOCK", uint64(mocks))
	us3RecordSQL(t, artifacts, facts, 1, "TRACE", uint64(traces))
	facts.Checks = us3Checks{CountsExact: true, StableFacts: false}
	facts.Queries = []us3Query{}
	if targetGoneElapsed < 2*time.Second-250*time.Millisecond {
		t.Fatalf("US3 target transaction ended before the real 2s DB subtimeout window: %s", targetGoneElapsed)
	}
	if targetGoneElapsed > 3*time.Second {
		t.Fatalf("US3 target transaction exceeded the real 3s Mock operation budget: %s", targetGoneElapsed)
	}
	us3RecordBudget(t, artifacts, facts, 1, "CONTROLLED", "MOCK_DB_SUBTIMEOUT", "APP", false, 2*time.Second, targetGoneElapsed)
	us3RecordBudget(t, artifacts, facts, 1, "NORMAL", "MOCK", "MOCK", true, 3*time.Second, targetGoneElapsed)
	facts.Faults = []us3Fault{{
		Kind: "MOCK_INSERT_WAITER_HOLD", SQLStateSource: "none",
		Cleanup: us3Cleanup{Kind: "TABLE_SHARE_LOCK", Removed: true, Verified: true},
	}}
	us3WriteScenarioEvidence(t, runID, facts.Name, "lock-evidence", struct {
		RelationOID         uint32 `json:"relation_oid"`
		WaiterPID           int    `json:"waiter_pid"`
		BlockerPID          int    `json:"blocker_pid"`
		BackendStart        string `json:"backend_start"`
		XactStart           string `json:"xact_start"`
		QueryKind           string `json:"query_kind"`
		WaitEventType       string `json:"wait_event_type"`
		WaitEvent           string `json:"wait_event"`
		WaiterGoneElapsedMS uint64 `json:"waiter_gone_elapsed_ms"`
		LockGone            bool   `json:"lock_gone"`
		NoLateMockWindowMS  uint64 `json:"no_late_mock_window_ms"`
		NoLateMock          bool   `json:"no_late_mock"`
	}{
		RelationOID: waiter.RelationOID, WaiterPID: waiter.PID, BlockerPID: waiter.BlockerPID,
		BackendStart: waiter.BackendStart.Format(time.RFC3339Nano), XactStart: waiter.XactStart.Format(time.RFC3339Nano),
		QueryKind: "MOCK_INSERT", WaitEventType: waiter.WaitEventType, WaitEvent: waiter.WaitEvent,
		WaiterGoneElapsedMS: uint64(targetGoneElapsed / time.Millisecond),
		LockGone:            true, NoLateMockWindowMS: 500, NoLateMock: true,
	})
	us3FinalizeScenario(t, artifacts, facts, observer, process, []uint64{1}, nil)
	us3RequireActualCalls(t, facts, 1, 1, 0)
}

func us3RunSubmitCommitUnknown(t *testing.T, repoRoot, runID, dsn, binary string) {
	t.Helper()
	facts := &us3Scenario{Name: "submit_commit_unknown", Generations: []uint64{1}, Transport: "RESPONSE"}
	facts.Commands = []us1ReportCommand{{Kind: us1ReportCommandBuild, Exit: 0}}
	artifacts := us3BeginArtifacts(t, repoRoot, runID, facts.Name)
	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	pool := us1OpenPool(t)
	t.Cleanup(func() { us1CleanupBusiness(t, pool, clientID, businessOrderNo) })
	us3CaptureBeforeCounts(t, pool, clientID, facts, 0, 0)

	socket := us1ObserverSocket(t)
	epoch := us1Epoch(t)
	observer := us3StartObserver(t, runID, epoch, socket)
	if err := observer.Expect(1); err != nil {
		t.Fatal("US3 submit-commit observer expect failed")
	}
	process, addr := us3StartGeneration(t, artifacts, binary, repoRoot, dsn, clientID, us2APIKeyA, socket, runID, epoch, 1)
	barrier, err := observer.ArmNextCommittedPoint(1, testcontrol.PointRegistered)
	if err != nil {
		t.Fatal("US3 submit-commit registration barrier arm failed")
	}
	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	type result struct {
		response *http.Response
		body     []byte
		err      error
	}
	ch := make(chan result, 1)
	go func() {
		response, body, err := us1PostPayment(ctx, addr, us2APIKeyA, businessOrderNo, 100, "CNY")
		ch <- result{response: response, body: body, err: err}
	}()
	if _, err := barrier.Wait(ctx); err != nil {
		t.Fatal("US3 submit-commit registration barrier wait failed")
	}
	binding, ok := barrier.Binding()
	if !ok || binding.OrderID == "" {
		t.Fatal("US3 submit-commit registration binding unavailable")
	}
	if err := barrier.Release(testcontrol.ActionReplyHidden); err != nil {
		t.Fatal("US3 submit-commit reply-hidden release failed")
	}
	post := <-ch
	if post.err != nil {
		t.Fatal("US3 submit-commit POST failed")
	}
	paymentBody, requestID := us3RequirePaymentResponse(t, post.response, post.body, http.StatusOK, binding.OrderID, businessOrderNo, 100, "CNY", payment.ResultUnknown, "RESULT_SAVE_ERROR")
	us3RecordHTTP(t, artifacts, facts, 1, post.response.StatusCode, requestID, binding.OrderID, string(paymentBody.Result), "RESULT_SAVE_ERROR")

	after := us3ReadOrderFact(t, pool, clientID, businessOrderNo)
	if after.PaymentID != binding.OrderID || after.BusinessRefSHA256 != us3RefSHA256(businessOrderNo) ||
		after.Amount != 100 || after.Currency != "CNY" || after.Result != "UNKNOWN" ||
		after.MockTransactionID != nil || after.ConfirmedAt != nil {
		t.Fatalf("US3 submit-commit PG order identity/result mismatch: %+v", after)
	}
	if paymentBody.PaymentID != after.PaymentID || paymentBody.BusinessOrderNo != businessOrderNo ||
		paymentBody.Amount != after.Amount || paymentBody.Currency != after.Currency ||
		paymentBody.Result != after.Result {
		t.Fatalf("US3 submit-commit HTTP readback does not match the independent PG order")
	}
	facts.Facts = us3Facts{After: &after, Transition: "CREATED_UNKNOWN", StableFacts: false}
	facts.Records = us3ReadRecords(t, pool, binding.OrderID)
	if len(facts.Records) != 0 {
		t.Fatalf("US3 submit-commit unexpectedly wrote %d handling records", len(facts.Records))
	}
	orders, mocks, traces := us1CountBusinessRows(t, pool, clientID)
	if orders != 1 || mocks != 0 || traces != 0 {
		t.Fatalf("US3 submit-commit counts mismatch: order=%d mock=%d trace=%d", orders, mocks, traces)
	}
	facts.PG = nil
	us3RecordSQL(t, artifacts, facts, 1, "ORDER", uint64(orders))
	us3RecordSQL(t, artifacts, facts, 1, "MOCK", uint64(mocks))
	us3RecordSQL(t, artifacts, facts, 1, "TRACE", uint64(traces))
	facts.Checks = us3Checks{CountsExact: true, StableFacts: false}
	facts.Queries = []us3Query{}
	facts.Save = &us3Save{
		Attempts: 1, CommitState: "INDETERMINATE",
		ApplicationReadback: "ORDER_ONLY", ExternalPGReadback: "NONE",
		ReadbackVerified: paymentBody.PaymentID == after.PaymentID &&
			paymentBody.BusinessOrderNo == businessOrderNo &&
			paymentBody.Amount == after.Amount && paymentBody.Currency == after.Currency &&
			paymentBody.Result == after.Result && after.Result == "UNKNOWN" &&
			after.MockTransactionID == nil && len(facts.Records) == 0 && mocks == 0,
		ReadbackScope: "APPLICATION_ORDER_ONLY", ReadbackSource: "APP",
	}
	if !facts.Save.ReadbackVerified {
		t.Fatal("US3 submit-commit readback was not verified against the independent order fact")
	}
	facts.Faults = []us3Fault{{
		Kind: "REGISTRATION_COMMIT_REPLY_LOST", SQLStateSource: "none",
		Cleanup: us3Cleanup{Kind: "UDS_SCENARIO", Removed: true, Verified: true},
	}}
	us3FinalizeScenario(t, artifacts, facts, observer, process, []uint64{1}, nil)
	us3RequireActualCalls(t, facts, 1, 0, 0)
}
