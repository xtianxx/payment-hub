//go:build integration

package tests

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"payment-hub/internal/payment"
	"payment-hub/tests/harness"
)

const (
	us2SafetyRunID   = "0123456789abcdef0123456789abcdef"
	us2SafetyEpoch   = "fedcba9876543210fedcba9876543210"
	us2SafetyOrderID = "123e4567-e89b-42d3-a456-426614174000"
	us2SafetyEventID = "0123456789abcdef0123456789abcdef:1"
)

func TestUS2ReportSafety(t *testing.T) {
	t.Run("valid eight scenario report", func(t *testing.T) {
		root := t.TempDir()
		collector, err := us2NewReportCollector(root, us2SafetyRunID)
		if err != nil {
			t.Fatal("US2 report collector initialization failed")
		}
		for _, name := range us2ReportScenarioNames {
			if err := collector.record(us2SafetyScenario(name)); err != nil {
				t.Fatalf("US2 report record failed for %s", name)
			}
		}
		wantPath := filepath.Join(root, us2SafetyRunID, "us2.json")
		if collector.path != wantPath {
			t.Fatal("US2 report path mismatch")
		}
		report, err := us2ReadReport(collector.path)
		if err != nil {
			t.Fatal("US2 report read failed")
		}
		if err := us2ValidateReport(report, us2SafetyRunID); err != nil {
			t.Fatalf("US2 report validation failed: %v", err)
		}
		if report.Schema != us2ReportSchema || report.RunID != us2SafetyRunID {
			t.Fatal("US2 report identity mismatch")
		}
		if len(report.Scenarios) != len(us2ReportScenarioNames) {
			t.Fatal("US2 report scenario count mismatch")
		}
		for index, name := range us2ReportScenarioNames {
			if report.Scenarios[index].Name != name {
				t.Fatal("US2 report scenario order mismatch")
			}
			if report.Scenarios[index].Checks == nil {
				t.Fatal("US2 report scenario lacks checks")
			}
		}
		info, err := os.Stat(collector.path)
		if err != nil || info.Mode().Perm() != 0o600 {
			t.Fatal("US2 report permissions mismatch")
		}
		temps, err := filepath.Glob(filepath.Join(filepath.Dir(collector.path), ".us2-*.tmp"))
		if err != nil || len(temps) != 0 {
			t.Fatal("US2 report left a temporary file")
		}
		if _, err := os.Stat(filepath.Join(filepath.Dir(collector.path), "us1.json")); !errors.Is(err, os.ErrNotExist) {
			t.Fatal("US2 report unexpectedly wrote us1.json")
		}
	})

	t.Run("report path and root safety", func(t *testing.T) {
		root := t.TempDir()
		if err := us1ValidateArtifactRoot("relative"); err == nil {
			t.Fatal("US2 relative artifact root was accepted")
		}
		if err := us1ValidateArtifactRoot(filepath.Join(root, "missing")); err == nil {
			t.Fatal("US2 missing artifact root was accepted")
		}
		filePath := filepath.Join(root, "file")
		if err := os.WriteFile(filePath, []byte("x"), 0o600); err != nil {
			t.Fatal("US2 file fixture creation failed")
		}
		if err := us1ValidateArtifactRoot(filePath); err == nil {
			t.Fatal("US2 regular-file artifact root was accepted")
		}
		linkPath := filepath.Join(root, "link")
		if err := os.Symlink(root, linkPath); err != nil {
			t.Fatal("US2 symlink fixture creation failed")
		}
		if err := us1ValidateArtifactRoot(linkPath); err == nil {
			t.Fatal("US2 symlink artifact root was accepted")
		}
		if _, err := us2ReportPath(root, "../escape"); err == nil {
			t.Fatal("US2 escaping report path was accepted")
		}
		if _, err := us2ReportPath(root, "0123"); err == nil {
			t.Fatal("US2 short run id was accepted")
		}
		path, err := us2ReportPath(root, us2SafetyRunID)
		if err != nil {
			t.Fatal("US2 valid report path was rejected")
		}
		if path != filepath.Join(root, us2SafetyRunID, "us2.json") {
			t.Fatal("US2 valid report path mismatch")
		}
		collector, err := us2NewReportCollector(root, us2SafetyRunID)
		if err != nil {
			t.Fatal("US2 report collector initialization failed")
		}
		if err := collector.record(us2SafetyScenario("replay_success")); err != nil {
			t.Fatal("US2 first scenario record failed")
		}
		if _, err := us2NewReportCollector(root, us2SafetyRunID); err == nil {
			t.Fatal("US2 existing report was accepted")
		}
	})

	t.Run("US1 same-name scenario artifact coexists", func(t *testing.T) {
		root := t.TempDir()
		sourceRoot := us1RepoRoot(t)
		runID := us2SafetyRunID
		us1Root := filepath.Join(root, runID, "scenarios", "restart_success")
		if err := os.MkdirAll(us1Root, 0o700); err != nil {
			t.Fatal("US1 scenario artifact root creation failed")
		}
		if err := us1ValidateArtifactRoot(us1Root); err != nil {
			t.Fatal("US1 scenario artifact root is unsafe")
		}
		us1Artifacts, err := harness.BeginArtifacts(harness.ArtifactConfig{
			Root:       us1Root,
			SourceRoot: sourceRoot,
			RunID:      runID,
		})
		if err != nil {
			t.Fatal("US1 scenario artifact begin failed")
		}
		if _, err := us1Artifacts.Finish(); err != nil {
			t.Fatal("US1 scenario artifact finish failed")
		}
		us1Report := filepath.Join(us1Root, runID, "runtime-harness.json")
		us1Before, err := os.ReadFile(us1Report)
		if err != nil {
			t.Fatal("US1 scenario artifact report read failed")
		}
		us1Hash := sha256.Sum256(us1Before)

		us2Artifacts := us2BeginScenarioArtifacts(t, root, sourceRoot, runID, "restart_success")
		us2Root := us2ScenarioRoot(root, runID, "restart_success")
		wantUS2Root := filepath.Join(root, runID, "scenarios", "us2", "restart_success")
		if us2Root != wantUS2Root {
			t.Fatalf("US2 scenario root layout mismatch: got %s want %s", us2Root, wantUS2Root)
		}
		if us2Root == us1Root {
			t.Fatal("US2 scenario root collides with the US1 scenario root")
		}
		requestID := payment.NewUUIDv4()
		if err := us2Artifacts.RecordHTTP(harness.HTTPObservation{
			Generation: 1,
			Status:     http.StatusOK,
			RequestID:  requestID,
		}); err != nil {
			t.Fatal("US2 scenario artifact HTTP record failed")
		}
		us2FinishArtifacts(t, us2Artifacts)

		us2Report := filepath.Join(us2Root, runID, "runtime-harness.json")
		us2Data, err := os.ReadFile(us2Report)
		if err != nil {
			t.Fatal("US2 scenario artifact report read failed")
		}
		if !bytes.Contains(us2Data, []byte(requestID)) {
			t.Fatal("US2 scenario artifact lacks its own observation")
		}
		us1After, err := os.ReadFile(us1Report)
		if err != nil {
			t.Fatal("US1 scenario artifact report reread failed")
		}
		if sha256.Sum256(us1After) != us1Hash {
			t.Fatal("US2 scenario artifact overwrote the US1 report")
		}
		if bytes.Contains(us1After, []byte(requestID)) {
			t.Fatal("US1 scenario artifact contains the US2 observation")
		}
	})

	t.Run("app env key parameterization", func(t *testing.T) {
		env := us2AppEnv(t, "postgres://example", "127.0.0.1:1", "us2-client", us2APIKeyB, "/tmp/socket", us2SafetyRunID, us2SafetyEpoch, 2)
		keyEntries, clientEntries, generationEntries := 0, 0, 0
		for _, entry := range env {
			switch {
			case strings.HasPrefix(entry, "PAYMENT_HUB_API_KEY="):
				keyEntries++
				if entry != "PAYMENT_HUB_API_KEY="+us2APIKeyB {
					t.Fatal("US2 app env key mismatch")
				}
			case strings.HasPrefix(entry, "PAYMENT_HUB_CLIENT_ID="):
				clientEntries++
				if entry != "PAYMENT_HUB_CLIENT_ID=us2-client" {
					t.Fatal("US2 app env client mismatch")
				}
			case strings.HasPrefix(entry, "PAYMENT_HUB_TEST_GENERATION="):
				generationEntries++
				if entry != "PAYMENT_HUB_TEST_GENERATION=2" {
					t.Fatal("US2 app env generation mismatch")
				}
			case strings.Contains(entry, us2APIKeyA):
				t.Fatal("US2 app env leaked the other key")
			}
		}
		if keyEntries != 1 || clientEntries != 1 || generationEntries != 1 {
			t.Fatal("US2 app env entry count mismatch")
		}
	})

	t.Run("duplicate scenario rejected", func(t *testing.T) {
		root := t.TempDir()
		collector, err := us2NewReportCollector(root, us2SafetyRunID)
		if err != nil {
			t.Fatal("US2 report collector initialization failed")
		}
		scenario := us2SafetyScenario("replay_success")
		if err := collector.record(scenario); err != nil {
			t.Fatal("US2 first scenario record failed")
		}
		if err := collector.record(scenario); err == nil {
			t.Fatal("US2 duplicate scenario was accepted")
		}
	})

	t.Run("invalid scenario fields rejected", func(t *testing.T) {
		cases := []struct {
			name   string
			mutate func(*us2ReportScenario)
		}{
			{name: "unknown name", mutate: func(s *us2ReportScenario) { s.Name = "bogus" }},
			{name: "invalid result", mutate: func(s *us2ReportScenario) { s.Result = "UNKNOWN" }},
			{name: "wrong generations", mutate: func(s *us2ReportScenario) { s.Generations = []uint64{2} }},
			{name: "invalid HTTP status", mutate: func(s *us2ReportScenario) { s.HTTP[0].Status = 99 }},
			{name: "invalid HTTP request id", mutate: func(s *us2ReportScenario) { s.HTTP[0].RequestID = "bogus" }},
			{name: "invalid HTTP payment id", mutate: func(s *us2ReportScenario) { s.HTTP[0].PaymentID = "bogus" }},
			{name: "invalid HTTP result", mutate: func(s *us2ReportScenario) { s.HTTP[0].Result = "BOGUS" }},
			{name: "invalid PG state", mutate: func(s *us2ReportScenario) { s.PG[0].State = "BOGUS" }},
			{name: "invalid PG rows", mutate: func(s *us2ReportScenario) { s.PG[0].Rows = 2 }},
			{name: "observer missing", mutate: func(s *us2ReportScenario) { s.Observer = nil }},
			{name: "observer untrusted", mutate: func(s *us2ReportScenario) { s.Observer.Trusted = false }},
			{name: "observer payment counts", mutate: func(s *us2ReportScenario) { s.Observer.Counts.Payment = 2 }},
			{name: "observer query counts", mutate: func(s *us2ReportScenario) { s.Observer.Counts.Query = 1 }},
			{name: "receipt identity", mutate: func(s *us2ReportScenario) { s.Observer.Receipts[0].RunID = "bogus" }},
			{name: "receipt generation", mutate: func(s *us2ReportScenario) { s.Observer.Receipts[0].Generation = 2 }},
			{name: "directive missing", mutate: func(s *us2ReportScenario) { s.Directive = nil }},
			{name: "directive result", mutate: func(s *us2ReportScenario) { s.Directive.Result = "UNKNOWN" }},
			{name: "directive order mismatch", mutate: func(s *us2ReportScenario) {
				s.Directive.Binding.OrderID = "223e4567-e89b-42d3-b456-426614174001"
			}},
			{name: "event id too long", mutate: func(s *us2ReportScenario) {
				s.Directive.Binding.EventID = strings.Repeat("a", 129)
			}},
			{name: "command kind", mutate: func(s *us2ReportScenario) {
				s.Commands = append(s.Commands, us1ReportCommand{Kind: "go_test", Exit: 0})
			}},
			{name: "command not run", mutate: func(s *us2ReportScenario) {
				s.Commands = append(s.Commands, us1ReportCommand{Kind: us1ReportCommandBuild, Exit: 0, NotRun: true})
			}},
			{name: "checks missing", mutate: func(s *us2ReportScenario) { s.Checks = nil }},
			{name: "checks before wrong", mutate: func(s *us2ReportScenario) { s.Checks.CountsBefore.PaymentCalls = 0 }},
			{name: "checks after wrong", mutate: func(s *us2ReportScenario) { s.Checks.CountsAfter.QueryCalls = 1 }},
			{name: "checks rows wrong", mutate: func(s *us2ReportScenario) { s.Checks.CountsAfter.MockRows = 0 }},
			{name: "checks stable false", mutate: func(s *us2ReportScenario) { s.Checks.StableFacts = false }},
			{name: "checks probes wrong", mutate: func(s *us2ReportScenario) { s.Checks.ReadonlyProbes = 1 }},
			{name: "checks fault invalid", mutate: func(s *us2ReportScenario) { s.Checks.LookupFault = "bogus" }},
			{name: "checks waiter wrong", mutate: func(s *us2ReportScenario) { s.Checks.WaiterCount = 1 }},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				scenario := us2SafetyScenario("replay_success")
				tc.mutate(&scenario)
				if err := us2ValidateReportScenario(scenario, true); err == nil {
					t.Fatal("US2 invalid scenario was accepted")
				}
			})
		}

		t.Run("unknown directive present", func(t *testing.T) {
			scenario := us2SafetyScenario("replay_unknown")
			scenario.Directive = &us1ReportDirective{
				Result: "SUCCEEDED",
				Binding: us1ReportBinding{
					RunID:      us2SafetyRunID,
					Epoch:      us2SafetyEpoch,
					Generation: 1,
					CallSeq:    1,
					EventID:    us2SafetyEventID,
					OrderID:    us2SafetyOrderID,
				},
			}
			if err := us2ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US2 unknown replay directive was accepted")
			}
		})
		t.Run("unknown 201 present", func(t *testing.T) {
			scenario := us2SafetyScenario("replay_unknown")
			scenario.HTTP = append(scenario.HTTP, us1ReportHTTP{
				Status: http.StatusCreated, RequestID: payment.NewUUIDv4(),
				PaymentID: us2SafetyOrderID, Result: "UNKNOWN",
			})
			if err := us2ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US2 unknown replay 201 was accepted")
			}
		})
		t.Run("unknown PG mock present", func(t *testing.T) {
			scenario := us2SafetyScenario("replay_unknown")
			scenario.PG[1].Rows = 1
			if err := us2ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US2 unknown replay PG mock row was accepted")
			}
		})
		t.Run("unknown checks created baseline", func(t *testing.T) {
			scenario := us2SafetyScenario("replay_unknown")
			scenario.Checks.CountsBefore = us2ReportCounts{
				PaymentCalls: 1, OrderRows: 1, MockRows: 1, TraceRows: 1,
			}
			if err := us2ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US2 unknown replay created baseline was accepted")
			}
		})
		t.Run("concurrency checks before", func(t *testing.T) {
			scenario := us2SafetyScenario("concurrency")
			scenario.Checks.CountsBefore.PaymentCalls = 1
			if err := us2ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US2 concurrency non-empty before baseline was accepted")
			}
		})
		t.Run("concurrency waiter count", func(t *testing.T) {
			scenario := us2SafetyScenario("concurrency")
			scenario.Checks.WaiterCount = 0
			if err := us2ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US2 concurrency without two waiters was accepted")
			}
		})
		t.Run("conflict probes", func(t *testing.T) {
			scenario := us2SafetyScenario("conflict_priority")
			scenario.Checks.ReadonlyProbes = 4
			if err := us2ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US2 conflict with four probes was accepted")
			}
		})
		t.Run("conflict fault", func(t *testing.T) {
			scenario := us2SafetyScenario("conflict_priority")
			scenario.Checks.LookupFault = "none"
			if err := us2ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US2 conflict without timeout fault was accepted")
			}
		})
		t.Run("normal conflict 500 cannot pass", func(t *testing.T) {
			scenario := us2SafetyScenario("conflict_priority")
			changed := false
			for index := range scenario.HTTP {
				if scenario.HTTP[index].Status == http.StatusServiceUnavailable {
					scenario.HTTP[index].Status = http.StatusInternalServerError
					changed = true
					break
				}
			}
			if !changed {
				t.Fatal("US2 conflict fixture lacks a 503 branch")
			}
			if err := us2ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US2 normal conflict 500 was accepted")
			}
		})
		t.Run("conflict missing failure branch", func(t *testing.T) {
			scenario := us2SafetyScenario("conflict_priority")
			filtered := make([]us1ReportHTTP, 0, len(scenario.HTTP))
			for _, observation := range scenario.HTTP {
				if observation.Status == http.StatusServiceUnavailable {
					continue
				}
				filtered = append(filtered, observation)
			}
			scenario.HTTP = filtered
			if err := us2ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US2 conflict without a lookup failure branch was accepted")
			}
		})
		t.Run("concurrency mixed waiter missing", func(t *testing.T) {
			data, err := json.Marshal(us2SafetyScenario("concurrency"))
			if err != nil {
				t.Fatal("US2 concurrency fixture marshal failed")
			}
			var raw map[string]any
			if err := json.Unmarshal(data, &raw); err != nil {
				t.Fatal("US2 concurrency fixture unmarshal failed")
			}
			checks, ok := raw["checks"].(map[string]any)
			if !ok {
				t.Fatal("US2 concurrency fixture lacks checks")
			}
			delete(checks, "mixed_waiter_count")
			mutated, err := json.Marshal(raw)
			if err != nil {
				t.Fatal("US2 concurrency mutation marshal failed")
			}
			var scenario us2ReportScenario
			if err := json.Unmarshal(mutated, &scenario); err != nil {
				return
			}
			if err := us2ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US2 concurrency missing mixed_waiter_count was accepted")
			}
		})
		t.Run("concurrency mixed waiter unobserved zero", func(t *testing.T) {
			scenario := us2SafetyScenario("concurrency")
			scenario.Checks.MixedWaiterCount = 0
			if err := us2ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US2 concurrency unobserved mixed waiter count was accepted")
			}
		})
		t.Run("concurrency mixed waiter one", func(t *testing.T) {
			scenario := us2SafetyScenario("concurrency")
			scenario.Checks.MixedWaiterCount = 1
			if err := us2ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US2 concurrency mixed waiter count one was accepted")
			}
		})
		t.Run("concurrency borrowed first waiter count", func(t *testing.T) {
			scenario := us2SafetyScenario("concurrency")
			scenario.Checks.WaiterCount = 2
			scenario.Checks.MixedWaiterCount = 0
			if err := us2ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US2 concurrency borrowed first waiter count was accepted")
			}
		})
		t.Run("concurrency mixed waiter bool", func(t *testing.T) {
			data, err := json.Marshal(us2SafetyScenario("concurrency"))
			if err != nil {
				t.Fatal("US2 concurrency fixture marshal failed")
			}
			var raw map[string]any
			if err := json.Unmarshal(data, &raw); err != nil {
				t.Fatal("US2 concurrency fixture unmarshal failed")
			}
			checks, ok := raw["checks"].(map[string]any)
			if !ok {
				t.Fatal("US2 concurrency fixture lacks checks")
			}
			checks["mixed_waiter_count"] = true
			mutated, err := json.Marshal(raw)
			if err != nil {
				t.Fatal("US2 concurrency bool mutation marshal failed")
			}
			var scenario us2ReportScenario
			if err := json.Unmarshal(mutated, &scenario); err != nil {
				return
			}
			if err := us2ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US2 concurrency bool mixed_waiter_count was accepted")
			}
		})
		t.Run("non-concurrency mixed waiter nonzero", func(t *testing.T) {
			scenario := us2SafetyScenario("replay_success")
			scenario.Checks.MixedWaiterCount = 1
			if err := us2ValidateReportScenario(scenario, true); err == nil {
				t.Fatal("US2 non-concurrency mixed waiter count was accepted")
			}
		})
	})

	t.Run("required arrays cannot be nil", func(t *testing.T) {
		cases := []struct {
			name   string
			mutate func(*us2ReportScenario)
		}{
			{name: "generations", mutate: func(s *us2ReportScenario) { s.Generations = nil }},
			{name: "http", mutate: func(s *us2ReportScenario) { s.HTTP = nil }},
			{name: "pg", mutate: func(s *us2ReportScenario) { s.PG = nil }},
			{name: "commands", mutate: func(s *us2ReportScenario) { s.Commands = nil }},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				scenario := us2SafetyScenario("replay_success")
				tc.mutate(&scenario)
				if err := us2ValidateReportScenario(scenario, true); err == nil {
					t.Fatal("US2 nil required array was accepted")
				}
			})
		}

		scenario := us2SafetyScenario("replay_success")
		scenario.Result = us1ReportResultFail
		scenario.Observer = nil
		scenario.Directive = nil
		scenario.Checks = nil
		if err := us2ValidateReportScenario(scenario, false); err != nil {
			t.Fatal("US2 FAIL non-strict scenario was rejected")
		}
		scenario.PG = nil
		if err := us2ValidateReportScenario(scenario, false); err == nil {
			t.Fatal("US2 FAIL nil required array was accepted")
		}
	})

	t.Run("missing scenario rejected", func(t *testing.T) {
		report := us2Report{Schema: us2ReportSchema, RunID: us2SafetyRunID}
		for _, name := range us2ReportScenarioNames[:len(us2ReportScenarioNames)-1] {
			report.Scenarios = append(report.Scenarios, us2SafetyScenario(name))
		}
		if err := us2ValidateReport(report, us2SafetyRunID); err == nil {
			t.Fatal("US2 incomplete report was accepted")
		}
	})

	t.Run("wrong schema rejected", func(t *testing.T) {
		report := us2Report{Schema: us1ReportSchema, RunID: us2SafetyRunID}
		for _, name := range us2ReportScenarioNames {
			report.Scenarios = append(report.Scenarios, us2SafetyScenario(name))
		}
		if err := us2ValidateReport(report, us2SafetyRunID); err == nil {
			t.Fatal("US2 wrong-schema report was accepted")
		}
	})

	t.Run("missing report rejected", func(t *testing.T) {
		if _, err := us2ReadReport(filepath.Join(t.TempDir(), "missing", "us2.json")); err == nil {
			t.Fatal("US2 missing report was accepted")
		}
	})

	t.Run("failure finalizer writes incomplete report", func(t *testing.T) {
		root := t.TempDir()
		artifacts, err := harness.BeginArtifacts(harness.ArtifactConfig{
			Root:       root,
			SourceRoot: us1RepoRoot(t),
			RunID:      us2SafetyRunID,
		})
		if err != nil {
			t.Fatal("US2 finalizer artifacts begin failed")
		}
		state := us2RegisterScenarioState(artifacts, "replay_success")
		if err := state.finishFailure(t); !errors.Is(err, harness.ErrArtifacts) {
			t.Fatal("US2 finalizer failure finish did not return ErrArtifacts")
		}
		if err := state.finishFailure(t); err != nil {
			t.Fatal("US2 finalizer second finish was not a no-op")
		}
		if state.finishCalls != 1 {
			t.Fatal("US2 finalizer finished more than once")
		}
		complete, faults := us1ReadHarnessReport(t, filepath.Join(root, us2SafetyRunID, "runtime-harness.json"))
		if complete {
			t.Fatal("US2 finalizer failure report was marked complete")
		}
		if !us1ContainsString(faults, "invalid_record") {
			t.Fatal("US2 finalizer failure report lacked the fixed fault")
		}
	})

	t.Run("success finish is once", func(t *testing.T) {
		root := t.TempDir()
		artifacts, err := harness.BeginArtifacts(harness.ArtifactConfig{
			Root:       root,
			SourceRoot: us1RepoRoot(t),
			RunID:      us2SafetyRunID,
		})
		if err != nil {
			t.Fatal("US2 success finish artifacts begin failed")
		}
		state := us2RegisterScenarioState(artifacts, "replay_success")
		if err := state.finishSuccess(t); err != nil {
			t.Fatal("US2 success finish failed")
		}
		if err := state.finishFailure(t); err != nil {
			t.Fatal("US2 success second finish was not a no-op")
		}
		if state.finishCalls != 1 {
			t.Fatal("US2 success finish happened more than once")
		}
		complete, _ := us1ReadHarnessReport(t, filepath.Join(root, us2SafetyRunID, "runtime-harness.json"))
		if !complete {
			t.Fatal("US2 success report was not marked complete")
		}
	})

	t.Run("SQL artifact contract", func(t *testing.T) {
		root := t.TempDir()
		artifacts, err := harness.BeginArtifacts(harness.ArtifactConfig{
			Root:       root,
			SourceRoot: us1RepoRoot(t),
			RunID:      us2SafetyRunID,
		})
		if err != nil {
			t.Fatal("US2 SQL contract artifacts begin failed")
		}
		facts := &us2ReportScenario{Name: "replay_success"}
		labels := []string{"ORDER", "MOCK", "TRACE"}
		for _, label := range labels {
			us2RecordSQL(t, artifacts, facts, 1, label, 1)
		}
		if len(facts.PG) != len(labels) {
			t.Fatalf("US2 typed SQL fact count mismatch: got %d want %d", len(facts.PG), len(labels))
		}
		for index, label := range labels {
			if facts.PG[index].State != label || facts.PG[index].Rows != 1 {
				t.Fatalf("US2 typed SQL fact mismatch at %d", index)
			}
		}
		path, err := artifacts.Finish()
		if err != nil || path == "" {
			t.Fatal("US2 SQL contract artifacts finish failed")
		}
		observations := us1ReadHarnessSQL(t, filepath.Join(root, us2SafetyRunID, "runtime-harness.json"))
		if len(observations) != len(labels) {
			t.Fatalf("US2 harness SQL count mismatch: got %d want %d", len(observations), len(labels))
		}
		for index, observation := range observations {
			if observation.Generation != 1 || observation.State != "00000" || observation.Rows != 1 {
				t.Fatalf("US2 harness SQL observation mismatch at %d", index)
			}
		}
		if us1ValidSQLTableLabel("BOGUS") {
			t.Fatal("US2 unknown SQL table label was accepted")
		}
	})
}

func us2SafetyScenario(name string) us2ReportScenario {
	orderID := us2SafetyOrderID
	requestID := func() string { return payment.NewUUIDv4() }
	scenario := us2ReportScenario{
		Name:        name,
		Result:      us1ReportResultPass,
		Generations: []uint64{1},
	}
	checks, _ := us2ExpectedChecks(name)
	scenario.Checks = &checks
	switch name {
	case "replay_success", "replay_failed":
		result := string(payment.ResultSucceeded)
		if name == "replay_failed" {
			result = string(payment.ResultFailed)
		}
		scenario.HTTP = []us1ReportHTTP{
			{Status: http.StatusCreated, RequestID: requestID(), PaymentID: orderID, Result: result},
			{Status: http.StatusOK, RequestID: requestID(), PaymentID: orderID, Result: result},
			{Status: http.StatusOK, RequestID: requestID(), PaymentID: orderID, Result: result},
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
				{RunID: us2SafetyRunID, Epoch: us2SafetyEpoch, Generation: 1, LastSeq: 3},
			},
		}
		scenario.Directive = &us1ReportDirective{
			Result: result,
			Binding: us1ReportBinding{
				RunID:      us2SafetyRunID,
				Epoch:      us2SafetyEpoch,
				Generation: 1,
				CallSeq:    1,
				EventID:    us2SafetyEventID,
				OrderID:    orderID,
			},
		}
		scenario.Commands = []us1ReportCommand{
			{Kind: us1ReportCommandBuild, Exit: 0},
			{Kind: us1ReportCommandStop, Exit: 0},
		}
	case "replay_unknown":
		scenario.HTTP = []us1ReportHTTP{
			{Status: http.StatusOK, RequestID: requestID(), PaymentID: orderID, Result: string(payment.ResultUnknown)},
			{Status: http.StatusOK, RequestID: requestID(), PaymentID: orderID, Result: string(payment.ResultUnknown)},
		}
		scenario.PG = []us1ReportPG{
			{State: "ORDER", Rows: 1},
			{State: "MOCK", Rows: 0},
			{State: "TRACE", Rows: 0},
		}
		scenario.Observer = &us1ReportObserver{
			Trusted: true,
			Counts:  us1ReportCounts{Payment: 0, Query: 0},
			Receipts: []us1ReportReceipt{
				{RunID: us2SafetyRunID, Epoch: us2SafetyEpoch, Generation: 1, LastSeq: 1},
			},
		}
		scenario.Directive = nil
		scenario.Commands = []us1ReportCommand{
			{Kind: us1ReportCommandBuild, Exit: 0},
			{Kind: us1ReportCommandStop, Exit: 0},
		}
	case "conflict_priority":
		scenario.HTTP = []us1ReportHTTP{
			{Status: http.StatusCreated, RequestID: requestID(), PaymentID: orderID, Result: string(payment.ResultSucceeded)},
			{Status: http.StatusUnauthorized, RequestID: requestID()},
			{Status: http.StatusBadRequest, RequestID: requestID()},
			{Status: http.StatusBadRequest, RequestID: requestID()},
			{Status: http.StatusServiceUnavailable, RequestID: requestID()},
			{Status: http.StatusConflict, RequestID: requestID()},
			{Status: http.StatusConflict, RequestID: requestID()},
			{Status: http.StatusBadRequest, RequestID: requestID()},
			{Status: http.StatusBadRequest, RequestID: requestID()},
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
				{RunID: us2SafetyRunID, Epoch: us2SafetyEpoch, Generation: 1, LastSeq: 3},
			},
		}
		scenario.Directive = &us1ReportDirective{
			Result: string(payment.ResultSucceeded),
			Binding: us1ReportBinding{
				RunID:      us2SafetyRunID,
				Epoch:      us2SafetyEpoch,
				Generation: 1,
				CallSeq:    1,
				EventID:    us2SafetyEventID,
				OrderID:    orderID,
			},
		}
		scenario.Commands = []us1ReportCommand{
			{Kind: us1ReportCommandBuild, Exit: 0},
			{Kind: us1ReportCommandStop, Exit: 0},
		}
	case "concurrency":
		scenario.HTTP = []us1ReportHTTP{
			{Status: http.StatusCreated, RequestID: requestID(), PaymentID: orderID, Result: string(payment.ResultSucceeded)},
			{Status: http.StatusOK, RequestID: requestID(), PaymentID: orderID, Result: string(payment.ResultUnknown)},
			{Status: http.StatusOK, RequestID: requestID(), PaymentID: orderID, Result: string(payment.ResultSucceeded)},
			{Status: http.StatusConflict, RequestID: requestID()},
			{Status: http.StatusOK, RequestID: requestID(), PaymentID: orderID, Result: string(payment.ResultSucceeded)},
			{Status: http.StatusOK, RequestID: requestID(), PaymentID: orderID, Result: string(payment.ResultSucceeded)},
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
				{RunID: us2SafetyRunID, Epoch: us2SafetyEpoch, Generation: 1, LastSeq: 3},
			},
		}
		scenario.Directive = &us1ReportDirective{
			Result: string(payment.ResultSucceeded),
			Binding: us1ReportBinding{
				RunID:      us2SafetyRunID,
				Epoch:      us2SafetyEpoch,
				Generation: 1,
				CallSeq:    1,
				EventID:    us2SafetyEventID,
				OrderID:    orderID,
			},
		}
		scenario.Commands = []us1ReportCommand{
			{Kind: us1ReportCommandBuild, Exit: 0},
			{Kind: us1ReportCommandStop, Exit: 0},
		}
	case "restart_success", "restart_failed":
		result := string(payment.ResultSucceeded)
		if name == "restart_failed" {
			result = string(payment.ResultFailed)
		}
		scenario.Generations = []uint64{1, 2}
		scenario.HTTP = []us1ReportHTTP{
			{Status: http.StatusCreated, RequestID: requestID(), PaymentID: orderID, Result: result},
			{Status: http.StatusOK, RequestID: requestID(), PaymentID: orderID, Result: result},
			{Status: http.StatusOK, RequestID: requestID(), PaymentID: orderID, Result: result},
			{Status: http.StatusOK, RequestID: requestID(), PaymentID: orderID, Result: result},
			{Status: http.StatusOK, RequestID: requestID(), PaymentID: orderID, Result: result},
			{Status: http.StatusOK, RequestID: requestID(), PaymentID: orderID, Result: result},
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
				{RunID: us2SafetyRunID, Epoch: us2SafetyEpoch, Generation: 1, LastSeq: 3},
				{RunID: us2SafetyRunID, Epoch: us2SafetyEpoch, Generation: 2, LastSeq: 1},
			},
		}
		scenario.Directive = &us1ReportDirective{
			Result: result,
			Binding: us1ReportBinding{
				RunID:      us2SafetyRunID,
				Epoch:      us2SafetyEpoch,
				Generation: 1,
				CallSeq:    1,
				EventID:    us2SafetyEventID,
				OrderID:    orderID,
			},
		}
		scenario.Commands = []us1ReportCommand{
			{Kind: us1ReportCommandBuild, Exit: 0},
			{Kind: us1ReportCommandStop, Exit: 0},
			{Kind: us1ReportCommandStop, Exit: 0},
		}
	case "key_rotation":
		scenario.Generations = []uint64{1, 2}
		scenario.HTTP = []us1ReportHTTP{
			{Status: http.StatusCreated, RequestID: requestID(), PaymentID: orderID, Result: string(payment.ResultSucceeded)},
			{Status: http.StatusOK, RequestID: requestID(), PaymentID: orderID, Result: string(payment.ResultSucceeded)},
			{Status: http.StatusOK, RequestID: requestID(), PaymentID: orderID, Result: string(payment.ResultSucceeded)},
			{Status: http.StatusUnauthorized, RequestID: requestID()},
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
				{RunID: us2SafetyRunID, Epoch: us2SafetyEpoch, Generation: 1, LastSeq: 3},
				{RunID: us2SafetyRunID, Epoch: us2SafetyEpoch, Generation: 2, LastSeq: 1},
			},
		}
		scenario.Directive = &us1ReportDirective{
			Result: string(payment.ResultSucceeded),
			Binding: us1ReportBinding{
				RunID:      us2SafetyRunID,
				Epoch:      us2SafetyEpoch,
				Generation: 1,
				CallSeq:    1,
				EventID:    us2SafetyEventID,
				OrderID:    orderID,
			},
		}
		scenario.Commands = []us1ReportCommand{
			{Kind: us1ReportCommandBuild, Exit: 0},
			{Kind: us1ReportCommandStop, Exit: 0},
			{Kind: us1ReportCommandStop, Exit: 0},
		}
	}
	return scenario
}
