//go:build integration

package tests

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5"

	"payment-hub/internal/httpapi"
	"payment-hub/internal/payment"
	"payment-hub/tests/harness"
)

const (
	us1SafetyRunID   = "0123456789abcdef0123456789abcdef"
	us1SafetyEpoch   = "fedcba9876543210fedcba9876543210"
	us1SafetyOrderID = "123e4567-e89b-42d3-a456-426614174000"
	us1SafetyEventID = "0123456789abcdef0123456789abcdef:1"
)

func TestUS1ReportSafety(t *testing.T) {
	t.Run("valid six scenario report", func(t *testing.T) {
		root := t.TempDir()
		collector, err := us1NewReportCollector(root, us1SafetyRunID)
		if err != nil {
			t.Fatal("US1 report collector initialization failed")
		}
		for _, name := range us1ReportScenarioNames {
			scenario := us1SafetyScenario(name)
			if err := collector.record(scenario); err != nil {
				t.Fatalf("US1 report record failed for %s", name)
			}
		}
		wantPath := filepath.Join(root, us1SafetyRunID, "us1.json")
		if collector.path != wantPath {
			t.Fatal("US1 report path mismatch")
		}
		report, err := us1ReadReport(collector.path)
		if err != nil {
			t.Fatal("US1 report read failed")
		}
		if err := us1ValidateReport(report, us1SafetyRunID); err != nil {
			t.Fatal("US1 report validation failed")
		}
		if report.Schema != us1ReportSchema || report.RunID != us1SafetyRunID {
			t.Fatal("US1 report identity mismatch")
		}
		if len(report.Scenarios) != len(us1ReportScenarioNames) {
			t.Fatal("US1 report scenario count mismatch")
		}
		for index, name := range us1ReportScenarioNames {
			if report.Scenarios[index].Name != name {
				t.Fatal("US1 report scenario order mismatch")
			}
		}
		info, err := os.Stat(collector.path)
		if err != nil || info.Mode().Perm() != 0o600 {
			t.Fatal("US1 report permissions mismatch")
		}
		temps, err := filepath.Glob(filepath.Join(filepath.Dir(collector.path), ".us1-*.tmp"))
		if err != nil || len(temps) != 0 {
			t.Fatal("US1 report left a temporary file")
		}
	})

	t.Run("report path and root safety", func(t *testing.T) {
		root := t.TempDir()
		if err := us1ValidateArtifactRoot("relative"); err == nil {
			t.Fatal("US1 relative artifact root was accepted")
		}
		if err := us1ValidateArtifactRoot(filepath.Join(root, "missing")); err == nil {
			t.Fatal("US1 missing artifact root was accepted")
		}
		filePath := filepath.Join(root, "file")
		if err := os.WriteFile(filePath, []byte("x"), 0o600); err != nil {
			t.Fatal("US1 file fixture creation failed")
		}
		if err := us1ValidateArtifactRoot(filePath); err == nil {
			t.Fatal("US1 regular-file artifact root was accepted")
		}
		linkPath := filepath.Join(root, "link")
		if err := os.Symlink(root, linkPath); err != nil {
			t.Fatal("US1 symlink fixture creation failed")
		}
		if err := us1ValidateArtifactRoot(linkPath); err == nil {
			t.Fatal("US1 symlink artifact root was accepted")
		}
		if _, err := us1ReportPath(root, "../escape"); err == nil {
			t.Fatal("US1 escaping report path was accepted")
		}
		path, err := us1ReportPath(root, us1SafetyRunID)
		if err != nil {
			t.Fatal("US1 valid report path was rejected")
		}
		if path != filepath.Join(root, us1SafetyRunID, "us1.json") {
			t.Fatal("US1 valid report path mismatch")
		}
	})

	t.Run("repo root fixture", func(t *testing.T) {
		root := us1RepoRoot(t)
		if filepath.Base(root) != "payment-hub" {
			t.Fatal("US1 repo root basename mismatch")
		}
		info, err := os.Stat(filepath.Join(root, "go.mod"))
		if err != nil || !info.Mode().IsRegular() {
			t.Fatal("US1 repo root go.mod mismatch")
		}
		info, err = os.Stat(filepath.Join(root, "cmd", "payment-hub"))
		if err != nil || !info.IsDir() {
			t.Fatal("US1 repo root cmd/payment-hub mismatch")
		}
		workingDir, err := os.Getwd()
		if err != nil {
			t.Fatal("US1 repo root working directory unavailable")
		}
		if filepath.Dir(workingDir) != root {
			t.Fatal("US1 repo root is not the tests directory parent")
		}
	})

	t.Run("canonical app DSN from validated config", func(t *testing.T) {
		conn := &pgx.ConnConfig{
			Host:     "127.0.0.1",
			Port:     55432,
			Database: "payment_hub_test_0123456789abcdef0123456789abcdef",
			User:     "us1-user",
			Password: "fake-password",
			RuntimeParams: map[string]string{
				"timezone":          "UTC",
				"application_name":  "fixture",
				"search_path":       "public",
				"statement_timeout": "2000",
			},
		}
		dsn, err := us1BuildAppDSN(conn)
		if err != nil {
			t.Fatal("US1 canonical app DSN build failed")
		}
		parsed, err := url.Parse(dsn)
		if err != nil {
			t.Fatal("US1 canonical app DSN parse failed")
		}
		if parsed.Scheme != "postgres" || parsed.Hostname() != conn.Host ||
			parsed.Port() != strconv.Itoa(int(conn.Port)) ||
			parsed.Path != "/"+conn.Database {
			t.Fatal("US1 canonical app DSN target mismatch")
		}
		if parsed.User == nil || parsed.User.Username() != conn.User {
			t.Fatal("US1 canonical app DSN user mismatch")
		}
		if password, ok := parsed.User.Password(); !ok || password != conn.Password {
			t.Fatal("US1 canonical app DSN password mismatch")
		}
		query := parsed.Query()
		if len(query) != 1 || query.Get("sslmode") != "disable" {
			t.Fatal("US1 canonical app DSN query mismatch")
		}
		for _, key := range []string{"timezone", "application_name", "search_path", "statement_timeout"} {
			if query.Has(key) {
				t.Fatal("US1 canonical app DSN leaked a runtime parameter")
			}
		}
		invalid := []func(*pgx.ConnConfig){
			func(c *pgx.ConnConfig) { c.Host = "" },
			func(c *pgx.ConnConfig) { c.Host = "example.com" },
			func(c *pgx.ConnConfig) { c.Port = 0 },
			func(c *pgx.ConnConfig) { c.User = "" },
			func(c *pgx.ConnConfig) { c.Password = "" },
			func(c *pgx.ConnConfig) { c.Database = "" },
			func(c *pgx.ConnConfig) { c.Database = "a/b" },
		}
		for _, mutate := range invalid {
			candidate := *conn
			mutate(&candidate)
			if _, err := us1BuildAppDSN(&candidate); err == nil {
				t.Fatal("US1 invalid canonical app DSN config was accepted")
			}
		}
		if _, err := us1BuildAppDSN(nil); err == nil {
			t.Fatal("US1 nil canonical app DSN config was accepted")
		}
	})

	t.Run("raw response projection", func(t *testing.T) {
		gin.SetMode(gin.ReleaseMode)
		ports := &us1FakePorts{}
		router := gin.New()
		if err := httpapi.RegisterPaymentRoutes(router, httpapi.AuthConfig{
			ClientID: "us1-boundary-client",
			APIKey:   us1APIKey,
		}, ports, ports); err != nil {
			t.Fatal("US1 raw projection route registration failed")
		}
		server := httptest.NewServer(router)
		defer server.Close()
		addr := server.Listener.Addr().String()

		duplicate := "POST /api/v1/payments HTTP/1.1\r\nHost: " + addr +
			"\r\nAuthorization: Bearer " + us1APIKey +
			"\r\nContent-Type: application/json\r\nContent-Type: application/json\r\nContent-Length: " +
			strconv.Itoa(len(us1ValidPostBody)) + "\r\nConnection: close\r\n\r\n" + us1ValidPostBody
		ports.reset()
		status, requestID := us1RawAppProbe(t, addr, duplicate)
		if status != http.StatusUnsupportedMediaType {
			t.Fatalf("US1 raw duplicate content type status mismatch: got %d", status)
		}
		if requestID == "" || payment.ValidateUUIDv4(requestID) != nil {
			t.Fatal("US1 raw duplicate content type request_id mismatch")
		}
		if submit, query := ports.counts(); submit != 0 || query != 0 {
			t.Fatalf("US1 raw duplicate content type port counts mismatch: submit=%d query=%d", submit, query)
		}

		fakeLength := "POST /api/v1/payments HTTP/1.1\r\nHost: " + addr +
			"\r\nAuthorization: Bearer " + us1APIKey +
			"\r\nContent-Type: application/json\r\nContent-Length: 10\r\nConnection: close\r\n\r\n" +
			string(us1PaddedPostBody(4097))
		ports.reset()
		status, requestID = us1RawAppProbe(t, addr, fakeLength)
		if status != http.StatusBadRequest {
			t.Fatalf("US1 raw fake content length status mismatch: got %d", status)
		}
		if requestID == "" || payment.ValidateUUIDv4(requestID) != nil {
			t.Fatal("US1 raw fake content length request_id mismatch")
		}
		if submit, query := ports.counts(); submit != 0 || query != 0 {
			t.Fatalf("US1 raw fake content length port counts mismatch: submit=%d query=%d", submit, query)
		}

		malformed := "POST /api/v1/payments BAD\r\nHost: " + addr + "\r\nConnection: close\r\n\r\n"
		ports.reset()
		status = us1RawServerProbe(t, addr, malformed)
		if status != http.StatusBadRequest {
			t.Fatalf("US1 raw malformed request status mismatch: got %d", status)
		}
		if submit, query := ports.counts(); submit != 0 || query != 0 {
			t.Fatalf("US1 raw malformed request port counts mismatch: submit=%d query=%d", submit, query)
		}
	})

	t.Run("failure finalizer writes diagnosis once", func(t *testing.T) {
		root := t.TempDir()
		artifacts, err := harness.BeginArtifacts(harness.ArtifactConfig{
			Root:       root,
			SourceRoot: us1RepoRoot(t),
			RunID:      us1SafetyRunID,
		})
		if err != nil {
			t.Fatal("US1 finalizer artifacts begin failed")
		}
		state := &us1ScenarioState{
			artifacts:     artifacts,
			scenario:      "payment_success",
			root:          root,
			diagnosisPath: filepath.Join(root, "us1-diagnosis.json"),
			stage:         us1DiagnosisStageReady,
			reason:        us1DiagnosisReasonPending,
			processExit:   &harness.Exit{Code: 1, Canceled: true, Reaped: true, GroupGone: true, OutputComplete: true},
		}
		if err := state.finishFailure(t); !errors.Is(err, harness.ErrArtifacts) {
			t.Fatal("US1 finalizer failure finish did not return ErrArtifacts")
		}
		if err := state.finishFailure(t); err != nil {
			t.Fatal("US1 finalizer second finish was not a no-op")
		}
		if state.finishCalls != 1 {
			t.Fatal("US1 finalizer finished more than once")
		}
		diagnosis := us1ReadDiagnosis(t, state.diagnosisPath)
		if diagnosis.Scenario != "payment_success" ||
			diagnosis.Stage != us1DiagnosisStageReady ||
			diagnosis.Reason != us1DiagnosisReasonPending {
			t.Fatal("US1 finalizer diagnosis mismatch")
		}
		if diagnosis.ProcessExit == nil || diagnosis.ProcessExit.Code != 1 ||
			!diagnosis.ProcessExit.Canceled || !diagnosis.ProcessExit.Reaped ||
			!diagnosis.ProcessExit.GroupGone || !diagnosis.ProcessExit.OutputComplete {
			t.Fatal("US1 finalizer process exit mismatch")
		}
		complete, faults := us1ReadHarnessReport(t, filepath.Join(root, us1SafetyRunID, "runtime-harness.json"))
		if complete {
			t.Fatal("US1 finalizer failure report was marked complete")
		}
		if !us1ContainsString(faults, "invalid_record") {
			t.Fatal("US1 finalizer failure report lacked the fixed fault")
		}
	})

	t.Run("success finish is once", func(t *testing.T) {
		root := t.TempDir()
		artifacts, err := harness.BeginArtifacts(harness.ArtifactConfig{
			Root:       root,
			SourceRoot: us1RepoRoot(t),
			RunID:      us1SafetyRunID,
		})
		if err != nil {
			t.Fatal("US1 success finish artifacts begin failed")
		}
		state := &us1ScenarioState{
			artifacts:     artifacts,
			scenario:      "payment_success",
			root:          root,
			diagnosisPath: filepath.Join(root, "us1-diagnosis.json"),
			stage:         us1DiagnosisStageReport,
			reason:        us1DiagnosisReasonPending,
		}
		if err := state.finishSuccess(t); err != nil {
			t.Fatal("US1 success finish failed")
		}
		if err := state.finishFailure(t); err != nil {
			t.Fatal("US1 success second finish was not a no-op")
		}
		if state.finishCalls != 1 {
			t.Fatal("US1 success finish happened more than once")
		}
		diagnosis := us1ReadDiagnosis(t, state.diagnosisPath)
		if diagnosis.Reason != us1DiagnosisReasonSuccess {
			t.Fatal("US1 success diagnosis reason mismatch")
		}
		complete, _ := us1ReadHarnessReport(t, filepath.Join(root, us1SafetyRunID, "runtime-harness.json"))
		if !complete {
			t.Fatal("US1 success report was not marked complete")
		}
	})

	t.Run("SQL artifact contract", func(t *testing.T) {
		root := t.TempDir()
		artifacts, err := harness.BeginArtifacts(harness.ArtifactConfig{
			Root:       root,
			SourceRoot: us1RepoRoot(t),
			RunID:      us1SafetyRunID,
		})
		if err != nil {
			t.Fatal("US1 SQL contract artifacts begin failed")
		}
		facts := &us1ReportScenario{Name: "payment_success"}
		labels := []string{"ORDER", "MOCK", "TRACE"}
		for _, label := range labels {
			us1RecordSQL(t, artifacts, facts, 1, label, 1)
		}
		if len(facts.PG) != len(labels) {
			t.Fatalf("US1 typed SQL fact count mismatch: got %d want %d", len(facts.PG), len(labels))
		}
		for index, label := range labels {
			if facts.PG[index].State != label || facts.PG[index].Rows != 1 {
				t.Fatalf("US1 typed SQL fact mismatch at %d", index)
			}
		}
		path, err := artifacts.Finish()
		if err != nil || path == "" {
			t.Fatal("US1 SQL contract artifacts finish failed")
		}
		observations := us1ReadHarnessSQL(t, filepath.Join(root, us1SafetyRunID, "runtime-harness.json"))
		if len(observations) != len(labels) {
			t.Fatalf("US1 harness SQL count mismatch: got %d want %d", len(observations), len(labels))
		}
		for index, observation := range observations {
			if observation.Generation != 1 || observation.State != "00000" || observation.Rows != 1 {
				t.Fatalf("US1 harness SQL observation mismatch at %d", index)
			}
		}
		if us1ValidSQLTableLabel("BOGUS") {
			t.Fatal("US1 unknown SQL table label was accepted")
		}
		if len(facts.PG) != len(labels) || len(observations) != len(labels) {
			t.Fatal("US1 unknown SQL table label wrote a success fact")
		}
	})

	t.Run("duplicate scenario rejected", func(t *testing.T) {
		root := t.TempDir()
		collector, err := us1NewReportCollector(root, us1SafetyRunID)
		if err != nil {
			t.Fatal("US1 report collector initialization failed")
		}
		scenario := us1SafetyScenario("payment_success")
		if err := collector.record(scenario); err != nil {
			t.Fatal("US1 first scenario record failed")
		}
		if err := collector.record(scenario); err == nil {
			t.Fatal("US1 duplicate scenario was accepted")
		}
	})

	t.Run("invalid scenario fields rejected", func(t *testing.T) {
		cases := []struct {
			name   string
			mutate func(*us1ReportScenario)
		}{
			{name: "unknown name", mutate: func(s *us1ReportScenario) { s.Name = "bogus" }},
			{name: "invalid result", mutate: func(s *us1ReportScenario) { s.Result = "BOGUS" }},
			{name: "wrong generations", mutate: func(s *us1ReportScenario) { s.Generations = []uint64{2} }},
			{name: "invalid HTTP status", mutate: func(s *us1ReportScenario) { s.HTTP[0].Status = 99 }},
			{name: "invalid HTTP request id", mutate: func(s *us1ReportScenario) { s.HTTP[0].RequestID = "bogus" }},
			{name: "invalid HTTP result", mutate: func(s *us1ReportScenario) { s.HTTP[0].Result = "BOGUS" }},
			{name: "invalid PG state", mutate: func(s *us1ReportScenario) { s.PG[0].State = "BOGUS" }},
			{name: "invalid PG rows", mutate: func(s *us1ReportScenario) { s.PG[0].Rows = 2 }},
			{name: "observer untrusted", mutate: func(s *us1ReportScenario) { s.Observer.Trusted = false }},
			{name: "observer counts", mutate: func(s *us1ReportScenario) { s.Observer.Counts.Payment = 2 }},
			{name: "receipt identity", mutate: func(s *us1ReportScenario) { s.Observer.Receipts[0].RunID = "bogus" }},
			{name: "directive missing", mutate: func(s *us1ReportScenario) { s.Directive = nil }},
			{name: "directive result", mutate: func(s *us1ReportScenario) { s.Directive.Result = "UNKNOWN" }},
			{name: "directive order mismatch", mutate: func(s *us1ReportScenario) {
				s.Directive.Binding.OrderID = "223e4567-e89b-42d3-b456-426614174001"
			}},
			{name: "event id too long", mutate: func(s *us1ReportScenario) {
				s.Directive.Binding.EventID = strings.Repeat("a", 129)
			}},
			{name: "command kind", mutate: func(s *us1ReportScenario) {
				s.Commands = append(s.Commands, us1ReportCommand{Kind: "go_test", Exit: 0})
			}},
			{name: "command not run", mutate: func(s *us1ReportScenario) {
				s.Commands = append(s.Commands, us1ReportCommand{Kind: us1ReportCommandBuild, Exit: 0, NotRun: true})
			}},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				scenario := us1SafetyScenario("payment_success")
				tc.mutate(&scenario)
				if err := us1ValidateReportScenario(scenario, true); err == nil {
					t.Fatal("US1 invalid scenario was accepted")
				}
			})
		}

		t.Run("boundary observer", func(t *testing.T) {
			scenario := us1SafetyScenario("http_boundary")
			scenario.Observer = &us1ReportObserver{Trusted: true}
			if err := us1ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US1 boundary observer was accepted")
			}
		})
		t.Run("boundary pg", func(t *testing.T) {
			scenario := us1SafetyScenario("http_boundary")
			scenario.PG = []us1ReportPG{{State: "ORDER", Rows: 0}}
			if err := us1ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US1 boundary PG was accepted")
			}
		})
		t.Run("boundary generations", func(t *testing.T) {
			scenario := us1SafetyScenario("http_boundary")
			scenario.Generations = []uint64{1}
			if err := us1ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US1 boundary generations were accepted")
			}
		})
		t.Run("invalid observer missing", func(t *testing.T) {
			scenario := us1SafetyScenario("invalid_requests")
			scenario.Observer = nil
			if err := us1ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US1 invalid scenario without observer was accepted")
			}
		})
		t.Run("invalid directive present", func(t *testing.T) {
			scenario := us1SafetyScenario("invalid_requests")
			scenario.Directive = &us1ReportDirective{
				Result: "SUCCEEDED",
				Binding: us1ReportBinding{
					RunID:      us1SafetyRunID,
					Epoch:      us1SafetyEpoch,
					Generation: 1,
					CallSeq:    1,
					EventID:    us1SafetyEventID,
					OrderID:    us1SafetyOrderID,
				},
			}
			if err := us1ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US1 invalid scenario directive was accepted")
			}
		})
		t.Run("invalid payment id present", func(t *testing.T) {
			scenario := us1SafetyScenario("invalid_requests")
			scenario.HTTP[0].PaymentID = us1SafetyOrderID
			if err := us1ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US1 invalid scenario payment id was accepted")
			}
		})
	})

	t.Run("required arrays cannot be nil", func(t *testing.T) {
		cases := []struct {
			name   string
			mutate func(*us1ReportScenario)
		}{
			{name: "generations", mutate: func(s *us1ReportScenario) { s.Generations = nil }},
			{name: "http", mutate: func(s *us1ReportScenario) { s.HTTP = nil }},
			{name: "pg", mutate: func(s *us1ReportScenario) { s.PG = nil }},
			{name: "commands", mutate: func(s *us1ReportScenario) { s.Commands = nil }},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				scenario := us1SafetyScenario("http_boundary")
				tc.mutate(&scenario)
				if err := us1ValidateReportScenario(scenario, true); err == nil {
					t.Fatal("US1 nil required array was accepted")
				}
			})
		}

		scenario := us1SafetyScenario("http_boundary")
		scenario.Result = us1ReportResultFail
		scenario.Generations = []uint64{}
		scenario.HTTP = []us1ReportHTTP{}
		scenario.PG = []us1ReportPG{}
		scenario.Commands = []us1ReportCommand{}
		if err := us1ValidateReportScenario(scenario, false); err != nil {
			t.Fatal("US1 FAIL empty arrays were rejected")
		}
		scenario.PG = nil
		if err := us1ValidateReportScenario(scenario, false); err == nil {
			t.Fatal("US1 FAIL nil required array was accepted")
		}
	})

	t.Run("missing scenario rejected", func(t *testing.T) {
		report := us1Report{Schema: us1ReportSchema, RunID: us1SafetyRunID}
		for _, name := range us1ReportScenarioNames[:len(us1ReportScenarioNames)-1] {
			report.Scenarios = append(report.Scenarios, us1SafetyScenario(name))
		}
		if err := us1ValidateReport(report, us1SafetyRunID); err == nil {
			t.Fatal("US1 incomplete report was accepted")
		}
	})

	t.Run("wrong schema rejected", func(t *testing.T) {
		report := us1Report{Schema: "wrong", RunID: us1SafetyRunID}
		for _, name := range us1ReportScenarioNames {
			report.Scenarios = append(report.Scenarios, us1SafetyScenario(name))
		}
		if err := us1ValidateReport(report, us1SafetyRunID); err == nil {
			t.Fatal("US1 wrong-schema report was accepted")
		}
	})

	t.Run("missing report rejected", func(t *testing.T) {
		if _, err := us1ReadReport(filepath.Join(t.TempDir(), "missing", "us1.json")); err == nil {
			t.Fatal("US1 missing report was accepted")
		}
	})
}

func us1SafetyScenario(name string) us1ReportScenario {
	orderID := us1SafetyOrderID
	scenario := us1ReportScenario{
		Name:        name,
		Result:      us1ReportResultPass,
		Generations: []uint64{1},
	}
	switch name {
	case "payment_success", "payment_failed":
		result := "SUCCEEDED"
		if name == "payment_failed" {
			result = "FAILED"
		}
		scenario.HTTP = []us1ReportHTTP{
			{Status: 201, RequestID: payment.NewUUIDv4(), PaymentID: orderID, Result: result},
			{Status: 200, RequestID: payment.NewUUIDv4(), PaymentID: orderID, Result: result},
			{Status: 200, RequestID: payment.NewUUIDv4(), PaymentID: orderID, Result: result},
		}
		scenario.PG = []us1ReportPG{
			{State: "ORDER", Rows: 1},
			{State: "MOCK", Rows: 1},
			{State: "TRACE", Rows: 1},
		}
		scenario.Observer = &us1ReportObserver{
			Trusted: true,
			Counts:  us1ReportCounts{Payment: 1, Query: 0},
			Receipts: []us1ReportReceipt{
				{RunID: us1SafetyRunID, Epoch: us1SafetyEpoch, Generation: 1, LastSeq: 3},
			},
		}
		scenario.Directive = &us1ReportDirective{
			Result: result,
			Binding: us1ReportBinding{
				RunID:      us1SafetyRunID,
				Epoch:      us1SafetyEpoch,
				Generation: 1,
				CallSeq:    1,
				EventID:    us1SafetyEventID,
				OrderID:    orderID,
			},
		}
		scenario.Commands = []us1ReportCommand{
			{Kind: us1ReportCommandBuild, Exit: 0},
			{Kind: us1ReportCommandStop, Exit: 0},
		}
	case "invalid_requests":
		scenario.HTTP = []us1ReportHTTP{
			{Status: 401, RequestID: payment.NewUUIDv4()},
			{Status: 400, RequestID: payment.NewUUIDv4()},
			{Status: 413, RequestID: payment.NewUUIDv4()},
			{Status: 415, RequestID: payment.NewUUIDv4()},
		}
		scenario.PG = []us1ReportPG{
			{State: "ORDER", Rows: 0},
			{State: "MOCK", Rows: 0},
			{State: "TRACE", Rows: 0},
		}
		scenario.Observer = &us1ReportObserver{
			Trusted: true,
			Counts:  us1ReportCounts{Payment: 0, Query: 0},
			Receipts: []us1ReportReceipt{
				{RunID: us1SafetyRunID, Epoch: us1SafetyEpoch, Generation: 1, LastSeq: 0},
			},
		}
		scenario.Commands = []us1ReportCommand{
			{Kind: us1ReportCommandBuild, Exit: 0},
			{Kind: us1ReportCommandStop, Exit: 0},
		}
	case "restart_success", "restart_failed":
		result := "SUCCEEDED"
		if name == "restart_failed" {
			result = "FAILED"
		}
		scenario.Generations = []uint64{1, 2}
		scenario.HTTP = []us1ReportHTTP{
			{Status: 201, RequestID: payment.NewUUIDv4(), PaymentID: orderID, Result: result},
			{Status: 200, RequestID: payment.NewUUIDv4(), PaymentID: orderID, Result: result},
			{Status: 200, RequestID: payment.NewUUIDv4(), PaymentID: orderID, Result: result},
			{Status: 200, RequestID: payment.NewUUIDv4(), PaymentID: orderID, Result: result},
			{Status: 200, RequestID: payment.NewUUIDv4(), PaymentID: orderID, Result: result},
		}
		scenario.PG = []us1ReportPG{
			{State: "ORDER", Rows: 1},
			{State: "MOCK", Rows: 1},
			{State: "TRACE", Rows: 1},
		}
		scenario.Observer = &us1ReportObserver{
			Trusted: true,
			Counts:  us1ReportCounts{Payment: 1, Query: 0},
			Receipts: []us1ReportReceipt{
				{RunID: us1SafetyRunID, Epoch: us1SafetyEpoch, Generation: 1, LastSeq: 3},
				{RunID: us1SafetyRunID, Epoch: us1SafetyEpoch, Generation: 2, LastSeq: 2},
			},
		}
		scenario.Directive = &us1ReportDirective{
			Result: result,
			Binding: us1ReportBinding{
				RunID:      us1SafetyRunID,
				Epoch:      us1SafetyEpoch,
				Generation: 1,
				CallSeq:    1,
				EventID:    us1SafetyEventID,
				OrderID:    orderID,
			},
		}
		scenario.Commands = []us1ReportCommand{
			{Kind: us1ReportCommandBuild, Exit: 0},
			{Kind: us1ReportCommandStop, Exit: 0},
			{Kind: us1ReportCommandStop, Exit: 0},
		}
	case "http_boundary":
		scenario.Generations = []uint64{}
		scenario.PG = []us1ReportPG{}
		scenario.HTTP = []us1ReportHTTP{
			{Status: 201, RequestID: payment.NewUUIDv4(), PaymentID: orderID, Result: "SUCCEEDED"},
			{Status: 401, RequestID: payment.NewUUIDv4()},
			{Status: 413, RequestID: payment.NewUUIDv4()},
			{Status: 415, RequestID: payment.NewUUIDv4()},
			{Status: 400, RequestID: ""},
		}
		scenario.Commands = []us1ReportCommand{
			{Kind: us1ReportCommandProbe, Exit: 0},
		}
	}
	return scenario
}

func us1ReadDiagnosis(t *testing.T, path string) us1ScenarioDiagnosis {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("US1 diagnosis read failed")
	}
	var diagnosis us1ScenarioDiagnosis
	if err := json.Unmarshal(data, &diagnosis); err != nil {
		t.Fatal("US1 diagnosis parse failed")
	}
	return diagnosis
}

type us1HarnessSQL struct {
	Generation uint64 `json:"generation"`
	State      string `json:"state"`
	Rows       uint64 `json:"rows"`
}

func us1ReadHarnessSQL(t *testing.T, path string) []us1HarnessSQL {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("US1 harness SQL report read failed")
	}
	var report struct {
		SQL []us1HarnessSQL `json:"sql"`
	}
	if err := json.Unmarshal(data, &report); err != nil {
		t.Fatal("US1 harness SQL report parse failed")
	}
	return report.SQL
}

func us1ValidSQLTableLabel(label string) bool {
	switch label {
	case "ORDER", "MOCK", "TRACE":
		return true
	default:
		return false
	}
}

func us1ReadHarnessReport(t *testing.T, path string) (bool, []string) {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("US1 harness report read failed")
	}
	var report struct {
		CollectionComplete bool     `json:"collection_complete"`
		Faults             []string `json:"faults"`
	}
	if err := json.Unmarshal(data, &report); err != nil {
		t.Fatal("US1 harness report parse failed")
	}
	return report.CollectionComplete, report.Faults
}

func us1ContainsString(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}
