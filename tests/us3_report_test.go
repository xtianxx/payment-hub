//go:build integration

package tests

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"payment-hub/internal/payment"
	"payment-hub/internal/testcontrol"
	"payment-hub/tests/harness"
)

const (
	us3SafetyRunID   = "0123456789abcdef0123456789abcdef"
	us3SafetyEpoch   = "fedcba9876543210fedcba9876543210"
	us3SafetyOrderID = "123e4567-e89b-42d3-a456-426614174000"
	us3SafetyEventID = "0123456789abcdef0123456789abcdef:1"
)

func TestUS3ReportSafety(t *testing.T) {
	t.Run("valid twenty-four scenario report", func(t *testing.T) {
		root := t.TempDir()
		collector, err := us3NewReportCollector(root, us3SafetyRunID)
		if err != nil {
			t.Fatal("US3 report collector initialization failed")
		}
		for _, name := range us3ScenarioNames {
			if err := collector.record(us3SafetyScenario(name)); err != nil {
				t.Fatalf("US3 report record failed for %s: %v", name, err)
			}
		}
		wantPath := filepath.Join(root, us3SafetyRunID, "us3.json")
		if collector.path != wantPath {
			t.Fatal("US3 report path mismatch")
		}
		report, err := us3ReadReport(collector.path)
		if err != nil {
			t.Fatal("US3 report read failed")
		}
		if err := us3ValidateReport(report, us3SafetyRunID); err != nil {
			t.Fatalf("US3 report validation failed: %v", err)
		}
		info, err := os.Stat(collector.path)
		if err != nil || info.Mode().Perm() != 0o600 {
			t.Fatal("US3 report permissions mismatch")
		}
		temps, err := filepath.Glob(filepath.Join(filepath.Dir(collector.path), ".us3-*.tmp"))
		if err != nil || len(temps) != 0 {
			t.Fatal("US3 report left a temporary file")
		}
	})

	t.Run("missing scenario rejected", func(t *testing.T) {
		report := us3Report{Schema: us3ReportSchema, RunID: us3SafetyRunID}
		for _, name := range us3ScenarioNames[:len(us3ScenarioNames)-1] {
			report.Scenarios = append(report.Scenarios, us3SafetyScenario(name))
		}
		if err := us3ValidateReport(report, us3SafetyRunID); err == nil {
			t.Fatal("US3 incomplete report was accepted")
		}
	})

	t.Run("fake partial rejected outside restart windows", func(t *testing.T) {
		scenario := us3SafetyScenario("query_unknown_confirmed")
		scenario.GenerationProofs[0].Mode = "controlled_truncated"
		scenario.GenerationProofs[0].Sealed = false
		scenario.GenerationProofs[0].Trusted = false
		scenario.GenerationProofs[0].Receipt = nil
		scenario.GenerationProofs[0].Counts = nil
		scenario.GenerationProofs[0].ExactCounts = false
		scenario.GenerationProofs[0].ControlledTermination = us3SafetyControlled()
		if err := us3ValidateScenario(scenario, true); err == nil {
			t.Fatal("US3 fake partial was accepted outside restart windows")
		}
	})

	t.Run("complete proof missing receipt rejected", func(t *testing.T) {
		scenario := us3SafetyScenario("query_unknown_confirmed")
		scenario.GenerationProofs[0].Receipt = nil
		if err := us3ValidateScenario(scenario, true); err == nil {
			t.Fatal("US3 complete proof without receipt was accepted")
		}
	})

	t.Run("query applications missing rejected", func(t *testing.T) {
		scenario := us3SafetyScenario("query_unknown_confirmed")
		scenario.Queries = []us3Query{}
		if err := us3ValidateScenario(scenario, true); err == nil {
			t.Fatal("US3 query scenario without QueryApplications was accepted")
		}
	})

	t.Run("unverified fault cleanup rejected", func(t *testing.T) {
		scenario := us3SafetyScenario("query_unknown_confirmed")
		scenario.Faults = []us3Fault{{
			Kind: "MOCK_QUERY_FAILURE", SQLState: "", SQLStateSource: "none",
			Cleanup: us3Cleanup{Kind: "UDS_SCENARIO", Removed: false, Verified: false},
		}}
		if err := us3ValidateScenario(scenario, true); err == nil {
			t.Fatal("US3 unverified fault cleanup was accepted")
		}
	})

	t.Run("wrong record rejected", func(t *testing.T) {
		scenario := us3SafetyScenario("query_unknown_confirmed")
		scenario.Records = []us3Record{{
			RecordID: payment.NewUUIDv4(), RequestID: payment.NewUUIDv4(),
			Source: "BOGUS", Decision: "KEEP_UNKNOWN", ResultBefore: "UNKNOWN", ResultAfter: "UNKNOWN",
		}}
		if err := us3ValidateScenario(scenario, true); err == nil {
			t.Fatal("US3 invalid record was accepted")
		}
	})

	t.Run("over-limit HTTP rejected", func(t *testing.T) {
		scenario := us3SafetyScenario("query_unknown_confirmed")
		for len(scenario.HTTP) <= us3HTTPMax {
			scenario.HTTP = append(scenario.HTTP, scenario.HTTP[0])
		}
		if err := us3ValidateScenario(scenario, true); err == nil {
			t.Fatal("US3 over-limit HTTP array was accepted")
		}
	})

	t.Run("unsafe identity not serialized", func(t *testing.T) {
		fact := us3SafetyFact()
		encoded, err := json.Marshal(fact)
		if err != nil {
			t.Fatal("US3 fact marshal failed")
		}
		text := string(encoded)
		if strings.Contains(text, "client_id") || strings.Contains(text, "business_order_no") {
			t.Fatal("US3 fact serialized raw client/business identity")
		}
		if !strings.Contains(text, "client_ref_sha256") || !strings.Contains(text, "business_ref_sha256") {
			t.Fatal("US3 fact lacks identity hashes")
		}
	})

	t.Run("exit host grace is not a pass budget", func(t *testing.T) {
		scenario := us3SafetyScenario("lifecycle_exit_budget")
		scenario.Budgets = []us3Budget{{
			Class: "NORMAL", Kind: "PROCESS_GRACE", Source: "HARNESS",
			PassBudget: true, LimitMS: 25000,
		}}
		if err := us3ValidateScenario(scenario, true); err == nil {
			t.Fatal("US3 host grace was accepted as a pass budget")
		}
	})

	t.Run("probe 503 cannot stand as primary", func(t *testing.T) {
		scenario := us3SafetyScenario("lifecycle_exit_budget")
		scenario.Transport = "RESPONSE"
		scenario.HTTP = []us3HTTP{{
			Kind: "SHUTDOWN_PROBE", Generation: 1, Status: 503,
			RequestID: payment.NewUUIDv4(),
		}}
		if err := us3ValidateScenario(scenario, true); err == nil {
			t.Fatal("US3 probe 503 was accepted as the primary response")
		}
	})

	t.Run("cancel unexpected 503 rejected", func(t *testing.T) {
		scenario := us3SafetyScenario("lifecycle_cancel_before_mock")
		scenario.Transport = "RESPONSE"
		scenario.HTTP = []us3HTTP{{
			Kind: "PRIMARY", Generation: 1, Status: 503,
			RequestID: payment.NewUUIDv4(),
		}}
		if err := us3ValidateScenario(scenario, true); err == nil {
			t.Fatal("US3 cancel unexpected 503 was accepted")
		}
	})

	t.Run("exit primary canceled with probe 503 allowed", func(t *testing.T) {
		scenario := us3SafetyScenario("lifecycle_exit_budget")
		scenario.Transport = "CLIENT_CANCELED"
		scenario.HTTP = []us3HTTP{{
			Kind: "SHUTDOWN_PROBE", Generation: 1, Status: 503,
			RequestID: payment.NewUUIDv4(),
		}}
		if err := us3ValidateScenario(scenario, true); err != nil {
			t.Fatal("US3 exit probe 503 was rejected")
		}
	})

	t.Run("exit primary canceled without response allowed", func(t *testing.T) {
		scenario := us3SafetyScenario("lifecycle_exit_budget")
		scenario.Transport = "CLIENT_CANCELED"
		scenario.HTTP = []us3HTTP{}
		if err := us3ValidateScenario(scenario, true); err != nil {
			t.Fatal("US3 exit primary canceled without response was rejected")
		}
	})

	t.Run("exit primary canceled with primary 503 rejected", func(t *testing.T) {
		scenario := us3SafetyScenario("lifecycle_exit_budget")
		scenario.Transport = "CLIENT_CANCELED"
		scenario.HTTP = []us3HTTP{{
			Kind: "PRIMARY", Generation: 1, Status: 503,
			RequestID: payment.NewUUIDv4(),
		}}
		if err := us3ValidateScenario(scenario, true); err == nil {
			t.Fatal("US3 exit primary canceled with PRIMARY 503 was accepted")
		}
	})

	t.Run("cancel before with probe rejected", func(t *testing.T) {
		scenario := us3SafetyScenario("lifecycle_cancel_before_mock")
		scenario.Transport = "CLIENT_CANCELED"
		scenario.HTTP = []us3HTTP{{
			Kind: "SHUTDOWN_PROBE", Generation: 1, Status: 503,
			RequestID: payment.NewUUIDv4(),
		}}
		if err := us3ValidateScenario(scenario, true); err == nil {
			t.Fatal("US3 cancel before probe was accepted")
		}
	})

	t.Run("probe 200 rejected", func(t *testing.T) {
		scenario := us3SafetyScenario("lifecycle_exit_budget")
		scenario.Transport = "CLIENT_CANCELED"
		scenario.HTTP = []us3HTTP{{
			Kind: "SHUTDOWN_PROBE", Generation: 1, Status: 200,
			RequestID: payment.NewUUIDv4(),
		}}
		if err := us3ValidateScenario(scenario, true); err == nil {
			t.Fatal("US3 probe 200 was accepted")
		}
	})

	t.Run("response transport only probe rejected", func(t *testing.T) {
		scenario := us3SafetyScenario("lifecycle_exit_budget")
		scenario.Transport = "RESPONSE"
		scenario.HTTP = []us3HTTP{{
			Kind: "SHUTDOWN_PROBE", Generation: 1, Status: 503,
			RequestID: payment.NewUUIDv4(),
		}}
		if err := us3ValidateScenario(scenario, true); err == nil {
			t.Fatal("US3 response transport without PRIMARY was accepted")
		}
	})

	t.Run("save record must bind to an actual record", func(t *testing.T) {
		scenario := us3SafetyScenario("query_result_commit_unknown")
		scenario.Save = &us3Save{
			Attempts: 1, RecordID: payment.NewUUIDv4(), CommitState: "INDETERMINATE",
			ApplicationReadback: "ORDER_ONLY", ExternalPGReadback: "ORDER_AND_RECORD",
			ReadbackVerified: true, ReadbackScope: "INDEPENDENT_ORDER_AND_RECORD",
			ReadbackSource: "APP_AND_EXTERNAL",
		}
		if err := us3ValidateScenario(scenario, true); err == nil {
			t.Fatal("US3 save record without an actual record was accepted")
		}
	})

	t.Run("submit commit save record id must be empty", func(t *testing.T) {
		scenario := us3SafetyScenario("submit_commit_unknown")
		scenario.Save = &us3Save{
			Attempts: 1, RecordID: payment.NewUUIDv4(), CommitState: "INDETERMINATE",
			ApplicationReadback: "ORDER_ONLY", ExternalPGReadback: "NONE",
			ReadbackVerified: true, ReadbackScope: "APPLICATION_ORDER_ONLY", ReadbackSource: "APP",
		}
		if err := us3ValidateScenario(scenario, true); err == nil {
			t.Fatal("US3 submit-commit save record id was accepted")
		}
	})

	t.Run("trace failure requires fixture probe sqlstate", func(t *testing.T) {
		scenario := us3SafetyScenario("query_confirmed_trace_failure")
		scenario.Trace = &us3Trace{
			Outcome: "FAILED", Issue: "TRACE_SAVE_ERROR", SQLState: "",
			SQLStateSource: "none", ConfirmedFactReturned: true,
		}
		if err := us3ValidateScenario(scenario, true); err == nil {
			t.Fatal("US3 trace failure without a fixture-probe SQLSTATE was accepted")
		}
	})

	t.Run("query confirmed_returned requires confirmed HTTP", func(t *testing.T) {
		scenario := us3SafetyScenario("query_unknown_failure")
		scenario.HTTP[0].Result = "UNKNOWN"
		scenario.Queries[0].ConfirmedReturned = true
		if err := us3ValidateScenario(scenario, true); err == nil {
			t.Fatal("US3 query confirmed_returned without confirmed HTTP was accepted")
		}
	})

	t.Run("controlled config budget is not wall pass", func(t *testing.T) {
		scenario := us3SafetyScenario("query_unknown_subtimeout")
		scenario.Budgets = []us3Budget{
			{Class: "CONTROLLED", Kind: "QUERY_SUBTIMEOUT", Source: "APP", PassBudget: false, LimitMS: 250, ElapsedMS: 251},
			{Class: "NORMAL", Kind: "MOCK", Source: "MOCK", PassBudget: true, LimitMS: 3000, ElapsedMS: 251},
		}
		if err := us3ValidateScenario(scenario, true); err != nil {
			t.Fatalf("US3 measured 251ms with 3s operation upper bound was rejected: %v", err)
		}
		scenario.Budgets[0].PassBudget = true
		if err := us3ValidateScenario(scenario, true); err == nil {
			t.Fatal("US3 controlled 250ms config budget was accepted as a wall pass")
		}
	})

	t.Run("not recorded only for actual trace failure", func(t *testing.T) {
		scenario := us3SafetyTraceFailureScenario()
		if err := us3ValidateScenario(scenario, true); err != nil {
			t.Fatalf("US3 narrow NOT_RECORDED trace failure was rejected: %v", err)
		}
		broken := us3SafetyTraceFailureScenario()
		broken.Faults = nil
		if err := us3ValidateScenario(broken, true); err == nil {
			t.Fatal("US3 NOT_RECORDED without trace fixture fault was accepted")
		}
		broken = us3SafetyTraceFailureScenario()
		broken.Name = "query_unknown_confirmed"
		if err := us3ValidateScenario(broken, true); err == nil {
			t.Fatal("US3 NOT_RECORDED outside trace failure was accepted")
		}
		broken = us3SafetyTraceFailureScenario()
		broken.Records = nil
		if err := us3ValidateScenario(broken, true); err == nil {
			t.Fatal("US3 NOT_RECORDED without later ACCEPT record was accepted")
		}
	})

	t.Run("transaction identity not wait count", func(t *testing.T) {
		backend := time.Date(2026, 11, 11, 0, 0, 0, 0, time.UTC)
		xact := backend.Add(time.Second)
		waiter := us3MockInsertWaiter{PID: 42, BackendStart: backend, XactStart: xact}
		if !us3WaiterIdentityGone(waiter, false, time.Time{}, nil) {
			t.Fatal("US3 missing backend was not treated as transaction gone")
		}
		if us3WaiterIdentityGone(waiter, true, backend, &xact) {
			t.Fatal("US3 same backend/xact identity was treated as gone")
		}
		newBackend := backend.Add(time.Minute)
		if !us3WaiterIdentityGone(waiter, true, newBackend, &xact) {
			t.Fatal("US3 reused PID with a new backend was not treated as gone")
		}
		if !us3WaiterIdentityGone(waiter, true, backend, nil) {
			t.Fatal("US3 ended transaction with nil xact_start was not treated as gone")
		}
		newXact := xact.Add(time.Minute)
		if !us3WaiterIdentityGone(waiter, true, backend, &newXact) {
			t.Fatal("US3 same backend with a new transaction was not treated as gone")
		}
	})

	t.Run("held response after hold is not acceptable", func(t *testing.T) {
		holdAt := time.Date(2026, 11, 11, 0, 0, 0, 0, time.UTC)
		if !us3HeldPostResponseBeforeHold(holdAt.Add(-time.Millisecond), holdAt) {
			t.Fatal("US3 pre-hold response was rejected")
		}
		if us3HeldPostResponseBeforeHold(holdAt, holdAt) {
			t.Fatal("US3 response at hold time was accepted")
		}
		if us3HeldPostResponseBeforeHold(holdAt.Add(time.Millisecond), holdAt) {
			t.Fatal("US3 post-hold response was accepted")
		}
	})

	t.Run("controlled restart requires gen2 query proof", func(t *testing.T) {
		for _, name := range []string{"restart_registered_unpaid", "restart_mock_committed_reply_lost"} {
			scenario := us3SafetyScenario(name)
			scenario.Queries = []us3Query{}
			scenario.Records = []us3Record{}
			scenario.Calls[1].QueryCalls = 0
			scenario.Checks.CountsAfter.QueryCalls = 0
			scenario.Checks.CountsAfter.TraceRows = 0
			scenario.PG[2].Rows = 0
			scenario.GenerationProofs[1].Counts.QueryCalls = 0
			scenario.GenerationProofs[1].Receipt.LastSeq = 0
			if err := us3ValidateScenario(scenario, true); err == nil {
				t.Fatalf("US3 %s accepted missing gen2 query/record/Seal proof", name)
			}

			scenario = us3SafetyScenario(name)
			scenario.Queries[0].Trusted = false
			if err := us3ValidateScenario(scenario, true); err == nil {
				t.Fatalf("US3 %s accepted untrusted gen2 query proof", name)
			}
		}
	})

	t.Run("reverse HTTP completion binds by call identity", func(t *testing.T) {
		firstBinding := us3QueryBinding{RunID: us3SafetyRunID, Epoch: us3SafetyEpoch, Generation: 1, CallSeq: 1, EventID: us3SafetyEventID, OrderID: us3SafetyOrderID}
		secondBinding := us3QueryBinding{RunID: us3SafetyRunID, Epoch: us3SafetyEpoch, Generation: 1, CallSeq: 2, EventID: us3SafetyRunID + ":2", OrderID: us3SafetyOrderID}
		firstRequestID := payment.NewUUIDv4()
		secondRequestID := payment.NewUUIDv4()
		firstFact := us3QueryHTTPFact{RequestID: firstRequestID, Path: "BY_ID", Confirmed: true, Issues: []string{"MOCK_RESULT_UNCONFIRMED", "TRACE_SAVE_ERROR"}, Binding: firstBinding}
		secondFact := us3QueryHTTPFact{RequestID: secondRequestID, Path: "BY_BUSINESS_ORDER_NO", Confirmed: true, Issues: []string{}, Binding: secondBinding}
		failureDirective := testcontrol.QueryScenarioFailure
		applications := []harness.QueryApplication{
			{RunID: us3SafetyRunID, Epoch: us3SafetyEpoch, Generation: 1, CallSeq: 1, CallEventID: us3SafetyEventID, OrderID: us3SafetyOrderID, Directive: &failureDirective, Outcome: testcontrol.QueryOutcomeUnavailable, AppliedSeq: 1},
			{RunID: us3SafetyRunID, Epoch: us3SafetyEpoch, Generation: 1, CallSeq: 2, CallEventID: us3SafetyRunID + ":2", OrderID: us3SafetyOrderID, Outcome: testcontrol.QueryOutcomeFound, AppliedSeq: 2},
		}
		queries, err := us3CorrelateQueryFacts([]us3QueryHTTPFact{secondFact, firstFact}, applications, true, true)
		if err != nil {
			t.Fatalf("US3 reverse HTTP completion correlation failed: %v", err)
		}
		if len(queries) != 2 || queries[0].RequestID != firstRequestID || queries[1].RequestID != secondRequestID {
			t.Fatalf("US3 reverse HTTP completion mapped the wrong request identities")
		}
		queries[0].traceFailure = true
		queries = us3ApplyActualQueryFacts(t, queries, []us3Record{us3SafetyAcceptRecord(secondRequestID)})
		if queries[0].Decision != "NOT_RECORDED" || queries[0].TraceOutcome != "FAILED" ||
			queries[0].ResultBefore != "" || queries[0].ResultAfter != "" {
			t.Fatalf("US3 first trace-failed request was not NOT_RECORDED")
		}
		if queries[1].Decision != "ACCEPT_CONFIRMATION" || queries[1].TraceOutcome != "SAVED" || queries[1].RequestID != secondRequestID {
			t.Fatalf("US3 later request was not bound to the actual ACCEPT record")
		}
		if _, err := us3CorrelateQueryFacts([]us3QueryHTTPFact{firstFact}, applications, true, true); err == nil {
			t.Fatal("US3 missing HTTP identity was accepted")
		}
		duplicate := firstFact
		duplicate.RequestID = payment.NewUUIDv4()
		if _, err := us3CorrelateQueryFacts([]us3QueryHTTPFact{firstFact, duplicate}, applications, true, true); err == nil {
			t.Fatal("US3 duplicate call identity with a different request was accepted")
		}
		crossed := firstFact
		crossed.Binding.OrderID = payment.NewUUIDv4()
		if _, err := us3CorrelateQueryFacts([]us3QueryHTTPFact{crossed, secondFact}, applications, true, true); err == nil {
			t.Fatal("US3 crossed order identity was accepted")
		}
		if _, found := us3FindRecordByRequest([]us3Record{us3SafetyAcceptRecord(firstRequestID)}, "QUERY", secondRequestID); found {
			t.Fatal("US3 wrong record request id was accepted")
		}
	})

	t.Run("joint no extra effect rejects extra order/payment/trace", func(t *testing.T) {
		before := us3Counts{OrderRows: 1, MockRows: 1, TraceRows: 0}
		after := us3Counts{OrderRows: 1, MockRows: 1, TraceRows: 1}
		queryRecord := us3Record{Source: "QUERY", Decision: "ACCEPT_CONFIRMATION"}
		if !us3JointNoExtraPaymentEffect(before, after, []us3Record{queryRecord}, true, true) {
			t.Fatal("US3 clean QUERY trace growth was rejected")
		}
		extraOrder := after
		extraOrder.OrderRows = 2
		if us3JointNoExtraPaymentEffect(before, extraOrder, []us3Record{queryRecord}, true, true) {
			t.Fatal("US3 extra order row was accepted")
		}
		extraPayment := append([]us3Record{queryRecord}, us3Record{Source: "PAYMENT", Decision: "KEEP_UNKNOWN"})
		if us3JointNoExtraPaymentEffect(before, after, extraPayment, true, true) {
			t.Fatal("US3 extra PAYMENT record was accepted")
		}
		if us3JointNoExtraPaymentEffect(before, after, nil, true, true) {
			t.Fatal("US3 trace growth without a QUERY record was accepted")
		}
		if us3JointNoExtraPaymentEffect(before, after, []us3Record{queryRecord, queryRecord}, true, true) {
			t.Fatal("US3 trace growth not matching QUERY record count was accepted")
		}
		if us3JointNoExtraPaymentEffect(before, after, []us3Record{queryRecord}, false, true) {
			t.Fatal("US3 new payment call was accepted")
		}
		if us3JointNoExtraPaymentEffect(before, after, []us3Record{queryRecord}, true, false) {
			t.Fatal("US3 changed original intent was accepted")
		}
	})
}

func us3SafetyScenario(name string) us3Scenario {
	fact := us3SafetyFact()
	scenario := us3Scenario{
		Name:        name,
		Result:      us1ReportResultPass,
		Generations: []uint64{1},
		Transport:   "RESPONSE",
		HTTP: []us3HTTP{{
			Kind: "PRIMARY", Generation: 1, Status: 200, RequestID: payment.NewUUIDv4(),
			PaymentID: us3SafetyOrderID, Result: "SUCCEEDED", Issues: []string{},
		}},
		PG:      []us1ReportPG{{State: "ORDER", Rows: 1}, {State: "MOCK", Rows: 1}, {State: "TRACE", Rows: 1}},
		Calls:   []us3Call{{Generation: 1, Exact: true}},
		Records: []us3Record{},
		Queries: []us3Query{},
		Faults:  []us3Fault{},
		Facts: us3Facts{
			Before: &fact, After: &fact, Transition: "UNCHANGED", StableFacts: true,
		},
		Checks: us3Checks{
			CountsBefore: us3Counts{OrderRows: 1, MockRows: 1, TraceRows: 1},
			CountsAfter:  us3Counts{OrderRows: 1, MockRows: 1, TraceRows: 1},
			CountsExact:  true, StableFacts: true,
		},
		GenerationProofs: []us3GenerationProof{us3SafetyCompleteProof(1)},
		Budgets:          []us3Budget{},
		Commands:         []us1ReportCommand{{Kind: us1ReportCommandBuild, Exit: 0}, {Kind: us1ReportCommandStop, Exit: 0}},
	}
	if us3QueryRequiredScenarios[name] {
		query := us3SafetyQuery()
		scenario.Queries = []us3Query{query}
		scenario.HTTP[0].RequestID = query.RequestID
		scenario.Records = []us3Record{us3SafetyAcceptRecord(query.RequestID)}
	}
	if name == "restart_registered_unpaid" || name == "restart_mock_committed_reply_lost" {
		scenario.Generations = []uint64{1, 2}
		scenario.Calls = []us3Call{{Generation: 1, Exact: false}, {Generation: 2, Exact: true, PaymentCalls: 0, QueryCalls: 1}}
		controlled := us3SafetyControlled()
		proof2 := us3SafetyCompleteProof(2)
		proof2.Counts = &us3Counts{QueryCalls: 1}
		scenario.GenerationProofs = []us3GenerationProof{
			{Generation: 1, Mode: "controlled_truncated", ExactCounts: false, ControlledTermination: controlled},
			proof2,
		}
		scenario.Checks.CountsExact = false
		query := us3SafetyQuery()
		query.Generation = 2
		query.RequestID = payment.NewUUIDv4()
		query.Binding.Generation = 2
		query.Binding.CallSeq = 1
		query.Binding.EventID = us3SafetyEventID
		query.Trusted = true
		queryHTTP := us3HTTP{Kind: "PRIMARY", Generation: 2, Status: 200, RequestID: query.RequestID, PaymentID: us3SafetyOrderID, Result: "SUCCEEDED", Issues: []string{}}
		if name == "restart_registered_unpaid" {
			query.Decision = "KEEP_UNKNOWN"
			query.ResultBefore = "UNKNOWN"
			query.ResultAfter = "UNKNOWN"
			query.TraceOutcome = "SAVED"
			query.ConfirmedReturned = false
			queryHTTP.Result = "UNKNOWN"
			queryHTTP.Issues = []string{"MOCK_RESULT_NOT_FOUND"}
			scenario.Records = []us3Record{us3SafetyKeepUnknownRecord(query.RequestID)}
		} else {
			query.Decision = "ACCEPT_CONFIRMATION"
			query.ResultBefore = "UNKNOWN"
			query.ResultAfter = "SUCCEEDED"
			query.TraceOutcome = "SAVED"
			query.ConfirmedReturned = true
			scenario.Records = []us3Record{us3SafetyAcceptRecord(query.RequestID)}
		}
		scenario.Queries = []us3Query{query}
		scenario.HTTP = append(scenario.HTTP, queryHTTP)
		scenario.Joint = &us3Joint{
			Kind: "CONTROLLED_TRUNCATED_RESTART", FirstGeneration: 1, SecondGeneration: 2,
			FirstMode: "controlled_truncated", SecondMode: "complete",
			FirstExactCounts: false, SecondExactCounts: true,
			SecondReceipt: proof2.Receipt,
			HeldPoint:     "REGISTERED", HeldBinding: &controlled.Binding,
			ProcessExit: &controlled.Exit, ObservedPrefix: us3Counts{},
			NoExtraPaymentEffect: true, NoNewPaymentCallAfterRestart: true, OriginalIntentSame: true,
			Limitation: "CROSS_GENERATION_EXACT_COUNTS_NOT_PROVABLE",
		}
	}
	if name == "restart_unknown_no_confirm" || name == "restart_unknown_then_confirmed" {
		scenario.Generations = []uint64{1, 2}
		scenario.Calls = []us3Call{{Generation: 1, Exact: true}, {Generation: 2, Exact: true}}
		scenario.GenerationProofs = []us3GenerationProof{us3SafetyCompleteProof(1), us3SafetyCompleteProof(2)}
	}
	if name == "lifecycle_cancel_before_mock" {
		scenario.Transport = "CLIENT_CANCELED"
		scenario.HTTP = []us3HTTP{}
		scenario.PG = []us1ReportPG{{State: "ORDER", Rows: 0}, {State: "MOCK", Rows: 0}, {State: "TRACE", Rows: 0}}
		scenario.Facts = us3Facts{Transition: "NONE", StableFacts: true}
		scenario.Checks = us3Checks{CountsExact: true, StableFacts: true}
		scenario.Calls = []us3Call{{Generation: 1, Exact: true}}
	}
	if name == "lifecycle_cancel_after_mock" {
		scenario.Transport = "CLIENT_CANCELED"
		scenario.HTTP = []us3HTTP{}
		unknownFact := us3SafetyFact()
		unknownFact.Result = "UNKNOWN"
		unknownFact.ConfirmedAt = nil
		scenario.Facts = us3Facts{Before: &unknownFact, After: &unknownFact, Transition: "UNCHANGED", StableFacts: true}
		scenario.PG = []us1ReportPG{{State: "ORDER", Rows: 1}, {State: "MOCK", Rows: 1}, {State: "TRACE", Rows: 0}}
		scenario.Checks = us3Checks{
			CountsBefore: us3Counts{},
			CountsAfter:  us3Counts{PaymentCalls: 1, OrderRows: 1, MockRows: 1, TraceRows: 0},
			CountsExact:  true, StableFacts: true,
		}
		scenario.Calls = []us3Call{{Generation: 1, Exact: true, PaymentCalls: 1}}
	}
	if name == "lifecycle_exit_budget" {
		scenario.Transport = "NO_RESPONSE"
		scenario.HTTP = []us3HTTP{}
		scenario.PG = []us1ReportPG{{State: "ORDER", Rows: 0}, {State: "MOCK", Rows: 0}, {State: "TRACE", Rows: 0}}
		scenario.Facts = us3Facts{Transition: "NONE", StableFacts: true}
		scenario.Checks = us3Checks{CountsExact: true, StableFacts: true}
		scenario.Calls = []us3Call{{Generation: 1, Exact: true}}
		scenario.Budgets = []us3Budget{
			{Class: "NORMAL", Kind: "EXIT", Source: "APP", PassBudget: true, LimitMS: 20000},
			{Class: "NORMAL", Kind: "PROCESS_GRACE", Source: "HARNESS", PassBudget: false, LimitMS: 25000},
		}
		scenario.Shutdown = &us3Shutdown{
			ExitElapsedMS: 10000, ExitUpperBoundMS: 20000, ObservationToleranceMS: 250,
			HostGraceMS: 25000, HostGracePassBudget: false,
			PoolClosedAfterUsers: true, PoolClosedAfterUsersSource: "APP_UNIT_EVIDENCE",
			NoUnmanagedGoroutine: true, NoUnmanagedGoroutineSource: "APP_UNIT_PLUS_GROUP_GONE",
		}
	}
	return scenario
}

func us3SafetyTraceFailureScenario() us3Scenario {
	scenario := us3SafetyScenario("query_confirmed_trace_failure")
	before := us3SafetyFact()
	before.Result = "UNKNOWN"
	before.ConfirmedAt = nil
	after := us3SafetyFact()
	scenario.Facts = us3Facts{Before: &before, After: &after, Transition: "UNKNOWN_TO_CONFIRMED", StableFacts: false}
	scenario.Checks.StableFacts = false
	scenario.Trace = &us3Trace{
		Outcome: "FAILED", Issue: "TRACE_SAVE_ERROR", SQLState: "P0001",
		SQLStateSource: "fixture_probe", ConfirmedFactReturned: true,
	}
	target := "order:" + strings.ReplaceAll(us3SafetyOrderID, "-", "")
	scenario.Faults = []us3Fault{{
		Kind: "TRACE_INSERT_SQL", TargetID: &target, SQLState: "P0001", SQLStateSource: "fixture_probe",
		Cleanup: us3Cleanup{Kind: "TRIGGER_FUNCTION", Removed: true, Verified: true},
	}}
	firstRequestID := payment.NewUUIDv4()
	secondRequestID := payment.NewUUIDv4()
	scenario.HTTP = []us3HTTP{
		{Kind: "PRIMARY", Generation: 1, Status: 200, RequestID: firstRequestID, PaymentID: us3SafetyOrderID, Result: "SUCCEEDED", Issues: []string{"TRACE_SAVE_ERROR"}},
		{Kind: "PRIMARY", Generation: 1, Status: 200, RequestID: secondRequestID, PaymentID: us3SafetyOrderID, Result: "SUCCEEDED", Issues: []string{}},
	}
	scenario.Records = []us3Record{us3SafetyAcceptRecord(secondRequestID)}
	scenario.Queries = []us3Query{
		{
			Generation: 1, RequestID: firstRequestID, Path: "BY_ID", QueryCallSeq: 1, Directive: "failure", Outcome: "unavailable",
			Applied: true, ApplyCount: 1, Decision: "NOT_RECORDED", ResultBefore: "", ResultAfter: "",
			TraceOutcome: "FAILED", ConfirmedReturned: true, NewMockCalls: 0,
			Binding: &us3QueryBinding{RunID: us3SafetyRunID, Epoch: us3SafetyEpoch, Generation: 1, CallSeq: 1, EventID: us3SafetyEventID, OrderID: us3SafetyOrderID},
			Trusted: true,
		},
		{
			Generation: 1, RequestID: secondRequestID, Path: "BY_BUSINESS_ORDER_NO", QueryCallSeq: 2, Directive: "normal", Outcome: "found",
			Applied: true, ApplyCount: 1, Decision: "ACCEPT_CONFIRMATION", ResultBefore: "UNKNOWN", ResultAfter: "SUCCEEDED",
			TraceOutcome: "SAVED", ConfirmedReturned: true, NewMockCalls: 0,
			Binding: &us3QueryBinding{RunID: us3SafetyRunID, Epoch: us3SafetyEpoch, Generation: 1, CallSeq: 2, EventID: us3SafetyRunID + ":2", OrderID: us3SafetyOrderID},
			Trusted: true,
		},
	}
	return scenario
}

func us3SafetyKeepUnknownRecord(requestID string) us3Record {
	reason := "MOCK_RESULT_NOT_FOUND"
	return us3Record{
		RecordID: payment.NewUUIDv4(), RequestID: requestID, Source: "QUERY",
		Decision: "KEEP_UNKNOWN", ResultBefore: "UNKNOWN", ResultAfter: "UNKNOWN",
		ReasonCode: reason, Evidence: us3Evidence{Differences: []string{}},
	}
}

func us3SafetyAcceptRecord(requestID string) us3Record {
	platform := us3SafetyOrderID
	mockID := payment.NewUUIDv4()
	amount := int64(100)
	currency := "CNY"
	result := "SUCCEEDED"
	return us3Record{
		RecordID: payment.NewUUIDv4(), RequestID: requestID, Source: "QUERY",
		Decision: "ACCEPT_CONFIRMATION", ResultBefore: "UNKNOWN", ResultAfter: "SUCCEEDED",
		Evidence: us3Evidence{
			PlatformReferenceID: &platform, MockTransactionID: &mockID, Amount: &amount,
			Currency: &currency, Result: &result, Differences: []string{},
		},
	}
}

func us3SafetyFact() us3OrderFact {
	confirmed := "2026-11-11T00:00:01Z"
	return us3OrderFact{
		PaymentID:         us3SafetyOrderID,
		ClientRefSHA256:   strings.Repeat("a", 64),
		BusinessRefSHA256: strings.Repeat("b", 64),
		Amount:            100,
		Currency:          "CNY",
		Result:            "SUCCEEDED",
		RegisteredAt:      "2026-11-11T00:00:00Z",
		ConfirmedAt:       &confirmed,
	}
}

func us3SafetyQuery() us3Query {
	return us3Query{
		Generation: 1, RequestID: payment.NewUUIDv4(), Path: "BY_BUSINESS_ORDER_NO", QueryCallSeq: 1,
		Directive: "normal", Outcome: "found", Applied: true, ApplyCount: 1,
		Decision: "ACCEPT_CONFIRMATION", ResultBefore: "UNKNOWN", ResultAfter: "SUCCEEDED",
		TraceOutcome: "SAVED", ConfirmedReturned: true, NewMockCalls: 0,
		Binding: &us3QueryBinding{
			RunID: us3SafetyRunID, Epoch: us3SafetyEpoch, Generation: 1, CallSeq: 1,
			EventID: us3SafetyEventID, OrderID: us3SafetyOrderID,
		},
		Trusted: true,
	}
}

func us3SafetyCompleteProof(generation uint64) us3GenerationProof {
	return us3GenerationProof{
		Generation: generation, Mode: "complete", Sealed: true, Trusted: true,
		Receipt: &us3Receipt{RunID: us3SafetyRunID, Epoch: us3SafetyEpoch, Generation: generation, LastSeq: 1},
		Counts:  &us3Counts{}, ExactCounts: true,
	}
}

func us3SafetyControlled() *us3ControlledTermination {
	return &us3ControlledTermination{
		Binding: us3ControlledBinding{
			RunID: us3SafetyRunID, Epoch: us3SafetyEpoch, Generation: 1,
			Point: "REGISTERED", OrderID: us3SafetyOrderID, CallSeq: 1,
			EventID: us3SafetyEventID, PrefixCount: 1,
		},
		Exit:                us3ProcessExit{Reaped: true, GroupGone: true, OutputComplete: true},
		ControlledTruncated: true, NoReceipt: true, ExactCounts: false,
	}
}
