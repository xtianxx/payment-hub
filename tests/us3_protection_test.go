//go:build integration

package tests

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"payment-hub/internal/payment"
	"payment-hub/internal/testcontrol"
)

func TestUS3Protection(t *testing.T) {
	repoRoot := us1RepoRoot(t)
	runID := us1RunID(t)
	dsn := us1TestDatabaseURL(t)
	binary := us1BuildApplication(t, repoRoot)

	t.Run("inflight failure", func(t *testing.T) {
		us3RunProtection(t, repoRoot, runID, dsn, binary, us3ProtectionCase{
			name: "protection_inflight_failure", directive: testcontrol.QueryScenarioFailure,
			wantIssues: []string{"MOCK_RESULT_UNCONFIRMED"}, wantDecision: "KEEP_CONFIRMED",
			protectionKind: "INFLIGHT_FAILURE",
		})
	})
	t.Run("inflight mismatch", func(t *testing.T) {
		us3RunProtection(t, repoRoot, runID, dsn, binary, us3ProtectionCase{
			name: "protection_inflight_mismatch", directive: testcontrol.QueryScenarioMismatchReference,
			wantIssues: []string{"MOCK_EVIDENCE_REJECTED"}, wantDecision: "REJECT_EVIDENCE",
			protectionKind: "INFLIGHT_MISMATCH",
		})
	})
	t.Run("confirmed trace failure", func(t *testing.T) {
		us3RunProtection(t, repoRoot, runID, dsn, binary, us3ProtectionCase{
			name: "query_confirmed_trace_failure", directive: testcontrol.QueryScenarioFailure,
			wantIssues: []string{"MOCK_RESULT_UNCONFIRMED", "TRACE_SAVE_ERROR"}, wantDecision: "KEEP_CONFIRMED",
			traceFailure: true,
		})
	})
}

type us3ProtectionCase struct {
	name           string
	directive      testcontrol.QueryScenario
	wantIssues     []string
	wantDecision   string
	protectionKind string
	traceFailure   bool
}

func us3RunProtection(t *testing.T, repoRoot, runID, dsn, binary string, tc us3ProtectionCase) {
	t.Helper()
	facts := &us3Scenario{Name: tc.name, Generations: []uint64{1}, Transport: "RESPONSE"}
	facts.Commands = []us1ReportCommand{{Kind: us1ReportCommandBuild, Exit: 0}}
	artifacts := us3BeginArtifacts(t, repoRoot, runID, tc.name)
	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	pool := us1OpenPool(t)
	t.Cleanup(func() { us1CleanupBusiness(t, pool, clientID, businessOrderNo) })

	orderID := us3SeedOrder(t, pool, clientID, businessOrderNo, 100, payment.ResultUnknown, nil, nil)
	us3SeedMock(t, pool, payment.NewUUIDv4(), orderID, 100, payment.ResultSucceeded, time.Now().UTC())
	before := us3ReadOrderFact(t, pool, clientID, businessOrderNo)
	us3CaptureBeforeCounts(t, pool, clientID, facts, 0, 0)

	socket := us1ObserverSocket(t)
	epoch := us1Epoch(t)
	observer := us3StartObserver(t, runID, epoch, socket)
	if err := observer.Expect(1); err != nil {
		t.Fatal("US3 protection observer expect failed")
	}
	process, addr := us3StartGeneration(t, artifacts, binary, repoRoot, dsn, clientID, us2APIKeyA, socket, runID, epoch, 1)

	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	hold, err := observer.HoldQueryEntry(1)
	if err != nil {
		t.Fatal("US3 protection first-query hold arm failed")
	}
	type result struct {
		response *http.Response
		body     []byte
		err      error
	}
	firstCh := make(chan result, 1)
	go func() {
		response, body, err := us1GetByID(ctx, addr, us2APIKeyA, orderID)
		firstCh <- result{response: response, body: body, err: err}
	}()
	holdStart := time.Now()
	heldBinding, err := hold.Wait(ctx)
	if err != nil {
		t.Fatal("US3 protection first-query hold wait failed")
	}

	beforeApplications := us3QueryApplicationSet(t, observer, 1)
	response, body, err := us1GetByBusinessOrderNo(ctx, addr, us2APIKeyA, businessOrderNo)
	if err != nil {
		t.Fatal("US3 protection second query failed")
	}
	afterApplications := us3QueryApplicationSet(t, observer, 1)
	secondBinding, ok := us3NewAppliedQueryBinding(t, observer, 1, beforeApplications, afterApplications, true)
	if !ok {
		t.Fatal("US3 protection second query lacked an applied QueryApplication")
	}
	secondPayment, secondRequestID := us3RequirePaymentResponse(t, response, body, http.StatusOK, orderID, businessOrderNo, 100, "CNY", payment.ResultSucceeded)
	us3RecordHTTP(t, artifacts, facts, 1, response.StatusCode, secondRequestID, orderID, string(secondPayment.Result))
	us3RecordQueryHTTP(t, facts, 1, secondRequestID, "BY_BUSINESS_ORDER_NO", true, secondBinding)

	traceSQLState := ""
	var cleanupTrace func() bool
	if tc.traceFailure {
		traceSQLState, cleanupTrace = us3InstallTraceFailTrigger(t, pool, orderID)
	}
	if err := hold.Release(tc.directive); err != nil {
		t.Fatal("US3 protection first-query release failed")
	}
	holdElapsed := time.Since(holdStart)
	first := <-firstCh
	if first.err != nil {
		t.Fatal("US3 protection first query failed")
	}
	firstPayment, firstRequestID := us3RequirePaymentResponse(t, first.response, first.body, http.StatusOK, orderID, businessOrderNo, 100, "CNY", payment.ResultSucceeded, tc.wantIssues...)
	us3RecordHTTP(t, artifacts, facts, 1, first.response.StatusCode, firstRequestID, orderID, string(firstPayment.Result), tc.wantIssues...)
	us3RecordQueryHTTP(t, facts, 1, firstRequestID, "BY_ID", true, us3QueryBindingFromHold(heldBinding), tc.wantIssues...)
	cleanupOK := true
	if cleanupTrace != nil {
		cleanupOK = cleanupTrace()
	}

	after := us3ReadOrderFact(t, pool, clientID, businessOrderNo)
	transition := us3ActualTransition(t, before, after)
	if transition != "UNKNOWN_TO_CONFIRMED" {
		t.Fatalf("US3 protection order facts transition mismatch: %s", transition)
	}
	facts.Facts = us3Facts{Before: &before, After: &after, Transition: transition, StableFacts: false}
	facts.Records = us3ReadRecords(t, pool, orderID)
	if tc.traceFailure {
		if _, found := us3FindRecordByRequest(facts.Records, "QUERY", firstRequestID); found {
			t.Fatalf("US3 trace failure left a handling record for the failed request %s", firstRequestID)
		}
		secondRecord, found := us3FindRecordByRequest(facts.Records, "QUERY", secondRequestID)
		if !found || secondRecord.Decision != "ACCEPT_CONFIRMATION" {
			t.Fatalf("US3 trace failure lacks the actual second ACCEPT record")
		}
		if len(facts.Records) != 1 {
			t.Fatalf("US3 trace failure record count mismatch: %d", len(facts.Records))
		}
	} else {
		firstRecord, found := us3FindRecordByRequest(facts.Records, "QUERY", firstRequestID)
		if !found || firstRecord.Decision != tc.wantDecision {
			t.Fatalf("US3 protection first actual decision mismatch: got %s want %s", firstRecord.Decision, tc.wantDecision)
		}
		secondRecord, found := us3FindRecordByRequest(facts.Records, "QUERY", secondRequestID)
		if !found || secondRecord.Decision != "ACCEPT_CONFIRMATION" {
			t.Fatalf("US3 protection second actual ACCEPT record is missing")
		}
		if len(facts.Records) != 2 {
			t.Fatalf("US3 protection record count mismatch: %d", len(facts.Records))
		}
	}
	orders, mocks, traces := us1CountBusinessRows(t, pool, clientID)
	facts.PG = nil
	us3RecordSQL(t, artifacts, facts, 1, "ORDER", uint64(orders))
	us3RecordSQL(t, artifacts, facts, 1, "MOCK", uint64(mocks))
	us3RecordSQL(t, artifacts, facts, 1, "TRACE", uint64(traces))
	facts.Checks = us3Checks{CountsExact: true, StableFacts: false}
	facts.strictQueryApplications = true
	if tc.traceFailure {
		facts.Trace = &us3Trace{
			Outcome: "FAILED", Issue: "TRACE_SAVE_ERROR", SQLState: traceSQLState,
			SQLStateSource: "fixture_probe", ConfirmedFactReturned: firstPayment.Result == string(payment.ResultSucceeded),
			NewLookup: false, ProofSource: "actual first-request HTTP TRACE_SAVE_ERROR + absent first record + actual second ACCEPT record",
		}
		targetToken := "order:" + strings.ReplaceAll(orderID, "-", "")
		facts.Faults = []us3Fault{{
			Kind: "TRACE_INSERT_SQL", TargetID: &targetToken, SQLState: traceSQLState, SQLStateSource: "fixture_probe",
			Cleanup: us3Cleanup{Kind: "TRIGGER_FUNCTION", Removed: cleanupOK, Verified: cleanupOK},
		}}
	} else {
		firstRecord, _ := us3FindRecordByRequest(facts.Records, "QUERY", firstRequestID)
		secondRecord, _ := us3FindRecordByRequest(facts.Records, "QUERY", secondRequestID)
		facts.Protection = &us3Protection{
			Kind: tc.protectionKind, InitialResult: before.Result,
			ConfirmedResult: secondPayment.Result, ProtectedResult: firstPayment.Result,
			FirstApplied: firstRecord.Decision, SecondApplied: secondRecord.Decision,
			OriginalFactPreserved: us3ImmutableFactsEqual(before, after),
		}
		facts.Faults = []us3Fault{{
			Kind: faultKindForProtection(tc.directive), SQLStateSource: "none",
			Cleanup: us3Cleanup{Kind: "UDS_SCENARIO", Removed: true, Verified: true},
		}}
	}
	us3RecordBudget(t, artifacts, facts, 1, "CONTROLLED", "BARRIER", "APP", holdElapsed <= 5*time.Second, 5*time.Second, holdElapsed)
	us3FinalizeScenario(t, artifacts, facts, observer, process, []uint64{1}, nil)
	us3RequireActualCalls(t, facts, 1, 0, 2)
}

func faultKindForProtection(directive testcontrol.QueryScenario) string {
	if directive == testcontrol.QueryScenarioFailure {
		return "MOCK_QUERY_FAILURE"
	}
	return "MISMATCH_REFERENCE"
}

func us3InstallTraceFailTrigger(t *testing.T, pool *pgxpool.Pool, orderID string) (string, func() bool) {
	t.Helper()
	suffix := strings.ReplaceAll(payment.NewUUIDv4(), "-", "")
	function := "us3_trace_fail_" + suffix
	trigger := function
	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	ddl := fmt.Sprintf(`
		CREATE FUNCTION %s() RETURNS trigger LANGUAGE plpgsql AS $$
		BEGIN
			IF NEW.payment_order_id = '%s'::uuid AND NEW.source = 'QUERY' THEN
				RAISE EXCEPTION 'US3 trace fixture failure' USING ERRCODE = 'P0001';
			END IF;
			RETURN NEW;
		END;
		$$;
		CREATE TRIGGER %s BEFORE INSERT ON result_handling_records
		FOR EACH ROW EXECUTE FUNCTION %s();
	`, function, orderID, trigger, function)
	if _, err := pool.Exec(ctx, ddl); err != nil {
		t.Fatalf("US3 trace failure trigger creation failed (%s)", us1SQLState(err))
	}
	probeErr := func() error {
		_, err := pool.Exec(ctx, `
			INSERT INTO result_handling_records
				(id, payment_order_id, request_id, source, observed_at, evidence,
				 result_before, result_after, decision, reason_code, reason_message)
			VALUES ($1, $2, $3, 'QUERY', $4, '{}'::jsonb, 'UNKNOWN', 'UNKNOWN', 'KEEP_UNKNOWN', NULL, NULL)
		`, payment.NewUUIDv4(), orderID, payment.NewUUIDv4(), time.Now().UTC())
		return err
	}()
	sqlstate := us1SQLState(probeErr)
	if probeErr == nil || sqlstate != "P0001" {
		drop := fmt.Sprintf("DROP TRIGGER IF EXISTS %s ON result_handling_records; DROP FUNCTION IF EXISTS %s();", trigger, function)
		_, _ = pool.Exec(ctx, drop)
		t.Fatalf("US3 trace fixture probe did not observe P0001 (got %s)", sqlstate)
	}
	cleanup := func() bool {
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), us1CleanupBudget)
		defer cleanupCancel()
		drop := fmt.Sprintf("DROP TRIGGER IF EXISTS %s ON result_handling_records; DROP FUNCTION IF EXISTS %s();", trigger, function)
		if _, err := pool.Exec(cleanupCtx, drop); err != nil {
			t.Errorf("US3 trace fixture cleanup failed (%s)", us1SQLState(err))
			return false
		}
		var triggers, functions int
		if err := pool.QueryRow(cleanupCtx, `
			SELECT count(*) FROM pg_trigger WHERE tgname = $1
		`, trigger).Scan(&triggers); err != nil {
			t.Errorf("US3 trace trigger catalog check failed (%s)", us1SQLState(err))
			return false
		}
		if err := pool.QueryRow(cleanupCtx, `
			SELECT count(*) FROM pg_proc WHERE proname = $1 AND pronamespace = current_schema()::regnamespace
		`, function).Scan(&functions); err != nil {
			t.Errorf("US3 trace function catalog check failed (%s)", us1SQLState(err))
			return false
		}
		if triggers != 0 || functions != 0 {
			t.Errorf("US3 trace fixture catalog objects remain: trigger=%d function=%d", triggers, functions)
			return false
		}
		return true
	}
	t.Cleanup(func() { _ = cleanup() })
	return sqlstate, cleanup
}
