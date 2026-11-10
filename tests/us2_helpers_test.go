//go:build integration

package tests

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"payment-hub/internal/payment"
	"payment-hub/internal/testcontrol"
	"payment-hub/tests/harness"
)

const (
	us2APIKeyA = "FAKE-us2_keyA.9~a+/b=="
	us2APIKeyB = "FAKE-us2_keyB.9~a+/b=="

	us2ReportSchema    = "payment-hub.us2.v1"
	us2ArtifactRootEnv = "PAYMENT_HUB_TEST_ARTIFACT_ROOT"
	us2ReportMaxBytes  = 64 * 1024
)

var us2ReportScenarioNames = []string{
	"replay_success",
	"replay_failed",
	"replay_unknown",
	"conflict_priority",
	"concurrency",
	"restart_success",
	"restart_failed",
	"key_rotation",
}

var us2ReportScenarioNameSet = map[string]bool{
	"replay_success":    true,
	"replay_failed":     true,
	"replay_unknown":    true,
	"conflict_priority": true,
	"concurrency":       true,
	"restart_success":   true,
	"restart_failed":    true,
	"key_rotation":      true,
}

// us2Report deliberately reuses the US1 field types so the base scenario shape
// cannot drift. It uses a separate collector, path, schema, and finalizer.
type us2Report struct {
	Schema    string              `json:"schema"`
	RunID     string              `json:"run_id"`
	Scenarios []us2ReportScenario `json:"scenarios"`
}

type us2ReportScenario struct {
	Name        string              `json:"name"`
	Result      string              `json:"result"`
	Generations []uint64            `json:"generations"`
	HTTP        []us1ReportHTTP     `json:"http"`
	PG          []us1ReportPG       `json:"pg"`
	Observer    *us1ReportObserver  `json:"observer"`
	Directive   *us1ReportDirective `json:"directive"`
	Commands    []us1ReportCommand  `json:"commands"`
	Checks      *us2ReportChecks    `json:"checks,omitempty"`
}

type us2ReportChecks struct {
	CountsBefore     us2ReportCounts `json:"counts_before"`
	CountsAfter      us2ReportCounts `json:"counts_after"`
	StableFacts      bool            `json:"stable_facts"`
	ReadonlyProbes   uint            `json:"readonly_probes"`
	LookupFault      string          `json:"lookup_fault"`
	WaiterCount      uint            `json:"waiter_count"`
	MixedWaiterCount uint            `json:"mixed_waiter_count"`
}

type us2ReportCounts struct {
	PaymentCalls uint64 `json:"payment_calls"`
	QueryCalls   uint64 `json:"query_calls"`
	OrderRows    uint64 `json:"order_rows"`
	MockRows     uint64 `json:"mock_rows"`
	TraceRows    uint64 `json:"trace_rows"`
}

func (scenario *us2ReportScenario) addHTTP(status int, requestID, paymentID, result string) {
	scenario.HTTP = append(scenario.HTTP, us1ReportHTTP{
		Status:    status,
		RequestID: requestID,
		PaymentID: paymentID,
		Result:    result,
	})
}

func (scenario *us2ReportScenario) addPG(state string, rows uint64) {
	scenario.PG = append(scenario.PG, us1ReportPG{State: state, Rows: rows})
}

func (scenario *us2ReportScenario) addCommand(kind string, exit int, notRun bool) {
	scenario.Commands = append(scenario.Commands, us1ReportCommand{Kind: kind, Exit: exit, NotRun: notRun})
}

type us2ReportCollector struct {
	mu        sync.Mutex
	root      string
	runID     string
	path      string
	scenarios map[string]us2ReportScenario
}

var (
	us2ProcessReportMu        sync.Mutex
	us2ProcessReportCollector *us2ReportCollector
)

func us2ReportPath(root, runID string) (string, error) {
	if err := us1ValidateArtifactRoot(root); err != nil {
		return "", err
	}
	if !us1LowerHex32(runID) {
		return "", errors.New("US2 report run id is invalid")
	}
	path := filepath.Join(root, runID, "us2.json")
	rel, err := filepath.Rel(root, path)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", errors.New("US2 report path escapes the artifact root")
	}
	return path, nil
}

func us2NewReportCollector(root, runID string) (*us2ReportCollector, error) {
	path, err := us2ReportPath(root, runID)
	if err != nil {
		return nil, err
	}
	runDir := filepath.Dir(path)
	if err := os.Mkdir(runDir, 0o700); err != nil && !errors.Is(err, os.ErrExist) {
		return nil, err
	}
	if err := us1ValidateArtifactRoot(runDir); err != nil {
		return nil, err
	}
	if _, err := os.Lstat(path); err == nil {
		return nil, errors.New("US2 report already exists")
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	return &us2ReportCollector{
		root:      root,
		runID:     runID,
		path:      path,
		scenarios: make(map[string]us2ReportScenario),
	}, nil
}

func us2ReportInit(t *testing.T, runID string) *us2ReportCollector {
	t.Helper()
	root := os.Getenv(us2ArtifactRootEnv)
	if root == "" {
		t.Fatalf("US2 report artifact root configuration %s is required", us2ArtifactRootEnv)
	}
	us2ProcessReportMu.Lock()
	defer us2ProcessReportMu.Unlock()
	if us2ProcessReportCollector != nil {
		if us2ProcessReportCollector.root != root || us2ProcessReportCollector.runID != runID {
			t.Fatal("US2 report collector identity changed")
		}
		return us2ProcessReportCollector
	}
	collector, err := us2NewReportCollector(root, runID)
	if err != nil {
		t.Fatal("US2 report collector initialization failed")
	}
	us2ProcessReportCollector = collector
	return collector
}

// us2ScenarioRoot is the US2-only inner artifact namespace. It must not share
// the US1 scenario directory: US1 and US2 both have restart_success and
// restart_failed, and harness.BeginArtifacts exclusively creates <root>/<runid>.
func us2ScenarioRoot(artifactRoot, runID, scenario string) string {
	return filepath.Join(artifactRoot, runID, "scenarios", "us2", scenario)
}

func us2ReportScenarioRoot(t *testing.T, runID, scenario string) string {
	t.Helper()
	if !us2ReportScenarioNameSet[scenario] {
		t.Fatal("US2 report scenario name is invalid")
	}
	collector := us2ReportInit(t, runID)
	root := us2ScenarioRoot(collector.root, collector.runID, scenario)
	if err := os.MkdirAll(root, 0o700); err != nil {
		t.Fatal("US2 scenario artifact root creation failed")
	}
	if err := us1ValidateArtifactRoot(root); err != nil {
		t.Fatal("US2 scenario artifact root is unsafe")
	}
	return root
}

func us2ReportRecord(t *testing.T, scenario *us2ReportScenario) {
	t.Helper()
	us2ProcessReportMu.Lock()
	collector := us2ProcessReportCollector
	us2ProcessReportMu.Unlock()
	if collector == nil {
		t.Fatal("US2 report collector is not initialized")
	}
	if err := collector.record(*scenario); err != nil {
		t.Fatal("US2 report scenario record failed")
	}
}

func (collector *us2ReportCollector) record(scenario us2ReportScenario) error {
	if collector == nil {
		return errors.New("nil US2 report collector")
	}
	collector.mu.Lock()
	defer collector.mu.Unlock()
	if _, exists := collector.scenarios[scenario.Name]; exists {
		return errors.New("duplicate US2 report scenario")
	}
	if err := us2ValidateReportScenario(scenario, scenario.Result == us1ReportResultPass); err != nil {
		return err
	}
	if scenario.Directive != nil && scenario.Directive.Binding.RunID != collector.runID {
		return errors.New("US2 directive run id does not match the collector")
	}
	if scenario.Observer != nil {
		for _, receipt := range scenario.Observer.Receipts {
			if receipt.RunID != collector.runID {
				return errors.New("US2 receipt run id does not match the collector")
			}
		}
	}
	collector.scenarios[scenario.Name] = scenario
	return collector.writeLocked()
}

func (collector *us2ReportCollector) writeLocked() error {
	report := us2Report{
		Schema:    us2ReportSchema,
		RunID:     collector.runID,
		Scenarios: make([]us2ReportScenario, 0, len(collector.scenarios)),
	}
	for _, name := range us2ReportScenarioNames {
		if scenario, ok := collector.scenarios[name]; ok {
			report.Scenarios = append(report.Scenarios, scenario)
		}
	}
	encoded, err := json.Marshal(report)
	if err != nil {
		return err
	}
	if len(encoded) > us2ReportMaxBytes {
		return errors.New("US2 report exceeds size bound")
	}
	dir := filepath.Dir(collector.path)
	tmp, err := os.CreateTemp(dir, ".us2-*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	removeTemp := true
	defer func() {
		if removeTemp {
			_ = os.Remove(tmpName)
		}
	}()
	if err := tmp.Chmod(0o600); err != nil {
		_ = tmp.Close()
		return err
	}
	if _, err := tmp.Write(encoded); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpName, collector.path); err != nil {
		return err
	}
	removeTemp = false
	return nil
}

func us2ReadReport(path string) (us2Report, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return us2Report{}, err
	}
	if len(data) == 0 || len(data) > us2ReportMaxBytes {
		return us2Report{}, errors.New("US2 report size is invalid")
	}
	var report us2Report
	if err := json.Unmarshal(data, &report); err != nil {
		return us2Report{}, err
	}
	return report, nil
}

func us2ValidateReport(report us2Report, runID string) error {
	if !us1LowerHex32(runID) || report.Schema != us2ReportSchema || report.RunID != runID {
		return errors.New("US2 report identity mismatch")
	}
	if len(report.Scenarios) != len(us2ReportScenarioNames) {
		return errors.New("US2 report scenario count mismatch")
	}
	for index, name := range us2ReportScenarioNames {
		if report.Scenarios[index].Name != name {
			return errors.New("US2 report scenario order mismatch")
		}
	}
	for _, scenario := range report.Scenarios {
		if scenario.Result != us1ReportResultPass {
			return errors.New("US2 report contains a non-PASS scenario")
		}
		if err := us2ValidateReportScenario(scenario, true); err != nil {
			return err
		}
		if scenario.Directive != nil && scenario.Directive.Binding.RunID != runID {
			return errors.New("US2 directive run id does not match the report")
		}
		if scenario.Observer != nil {
			for _, receipt := range scenario.Observer.Receipts {
				if receipt.RunID != runID || !us1LowerHex32(receipt.Epoch) {
					return errors.New("US2 receipt identity does not match the report")
				}
			}
		}
	}
	return nil
}

func us2ValidateReportScenario(scenario us2ReportScenario, strict bool) error {
	if !us2ReportScenarioNameSet[scenario.Name] {
		return errors.New("invalid US2 scenario name")
	}
	if scenario.Generations == nil || scenario.HTTP == nil ||
		scenario.PG == nil || scenario.Commands == nil {
		return errors.New("US2 scenario required array is nil")
	}
	switch scenario.Result {
	case us1ReportResultPass, us1ReportResultFail, us1ReportResultNotRun:
	default:
		return errors.New("invalid US2 scenario result")
	}
	if !us2ValidReportGenerations(scenario.Name, scenario.Generations) {
		return errors.New("invalid US2 scenario generations")
	}
	for _, observation := range scenario.HTTP {
		if observation.Status < 100 || observation.Status > 599 {
			return errors.New("invalid US2 HTTP status")
		}
		if observation.RequestID != "" && !us1LowerHexUUIDv4(observation.RequestID) {
			return errors.New("invalid US2 HTTP request id")
		}
		if observation.PaymentID != "" && !us1LowerHexUUIDv4(observation.PaymentID) {
			return errors.New("invalid US2 HTTP payment id")
		}
		if !us1ValidReportResult(observation.Result) {
			return errors.New("invalid US2 HTTP result")
		}
	}
	for _, state := range scenario.PG {
		switch state.State {
		case "ORDER", "MOCK", "TRACE":
		default:
			return errors.New("invalid US2 PG state")
		}
	}
	for _, command := range scenario.Commands {
		switch command.Kind {
		case us1ReportCommandBuild, us1ReportCommandStop, us1ReportCommandProbe:
		default:
			return errors.New("invalid US2 command kind")
		}
		if strict && command.NotRun {
			return errors.New("US2 PASS report contains a not-run command")
		}
	}
	if !strict {
		return nil
	}
	if scenario.Observer == nil {
		return errors.New("US2 scenario lacks observer evidence")
	}
	if scenario.Directive == nil {
		if scenario.Name != "replay_unknown" {
			return errors.New("US2 scenario lacks directive evidence")
		}
	} else if err := us2ValidateDirective(scenario); err != nil {
		return err
	}
	if err := us2ValidateObserver(scenario); err != nil {
		return err
	}
	if err := us2ValidateChecks(scenario); err != nil {
		return err
	}
	switch scenario.Name {
	case "replay_success":
		return us2ValidateReplayConfirmedScenario(scenario, payment.ResultSucceeded)
	case "replay_failed":
		return us2ValidateReplayConfirmedScenario(scenario, payment.ResultFailed)
	case "replay_unknown":
		return us2ValidateReplayUnknownScenario(scenario)
	case "conflict_priority":
		return us2ValidateConflictScenario(scenario)
	case "concurrency":
		return us2ValidateConcurrencyScenario(scenario)
	case "restart_success":
		return us2ValidateRestartScenario(scenario, payment.ResultSucceeded)
	case "restart_failed":
		return us2ValidateRestartScenario(scenario, payment.ResultFailed)
	case "key_rotation":
		return us2ValidateKeyRotationScenario(scenario)
	default:
		return errors.New("invalid US2 scenario name")
	}
}

func us2ValidateDirective(scenario us2ReportScenario) error {
	directive := scenario.Directive
	if directive == nil {
		return errors.New("US2 scenario lacks a directive")
	}
	if directive.Result != string(payment.ResultSucceeded) && directive.Result != string(payment.ResultFailed) {
		return errors.New("US2 directive result is invalid")
	}
	binding := directive.Binding
	if !us1LowerHex32(binding.RunID) || !us1LowerHex32(binding.Epoch) ||
		binding.Generation == 0 || binding.Generation != scenario.Generations[0] ||
		binding.CallSeq == 0 ||
		!us1ValidReportEventID(binding.EventID, binding.CallSeq) ||
		!us1LowerHexUUIDv4(binding.OrderID) {
		return errors.New("US2 directive binding is invalid")
	}
	return nil
}

func us2ValidateObserver(scenario us2ReportScenario) error {
	observer := scenario.Observer
	if observer == nil || !observer.Trusted {
		return errors.New("US2 observer is not trusted")
	}
	if len(observer.Receipts) != len(scenario.Generations) {
		return errors.New("US2 observer receipt count is invalid")
	}
	var binding *us1ReportBinding
	if scenario.Directive != nil {
		binding = &scenario.Directive.Binding
	}
	for index, generation := range scenario.Generations {
		receipt := observer.Receipts[index]
		if !us1LowerHex32(receipt.RunID) || !us1LowerHex32(receipt.Epoch) ||
			receipt.Generation != generation {
			return errors.New("US2 observer receipt identity is invalid")
		}
		if binding != nil {
			if receipt.RunID != binding.RunID || receipt.Epoch != binding.Epoch {
				return errors.New("US2 observer receipt identity is invalid")
			}
			if generation == binding.Generation && receipt.LastSeq < binding.CallSeq {
				return errors.New("US2 observer receipt does not cover the directive binding")
			}
		}
	}
	return nil
}

func us2ValidateObserverCounts(scenario us2ReportScenario, wantPayment, wantQuery uint64) error {
	if scenario.Observer == nil ||
		scenario.Observer.Counts.Payment != wantPayment ||
		scenario.Observer.Counts.Query != wantQuery {
		return errors.New("US2 observer counts are invalid")
	}
	return nil
}

func us2ValidateReplayConfirmedScenario(scenario us2ReportScenario, wantResult payment.Result) error {
	if scenario.Directive == nil || scenario.Directive.Result != string(wantResult) {
		return errors.New("US2 replay directive result is invalid")
	}
	if len(scenario.HTTP) != 3 {
		return errors.New("US2 replay HTTP count is invalid")
	}
	created, ok := 0, 0
	for _, observation := range scenario.HTTP {
		if observation.PaymentID != scenario.Directive.Binding.OrderID {
			return errors.New("US2 replay HTTP id does not match the directive")
		}
		if observation.Result != string(wantResult) {
			return errors.New("US2 replay HTTP result is invalid")
		}
		switch observation.Status {
		case http.StatusCreated:
			created++
		case http.StatusOK:
			ok++
		default:
			return errors.New("US2 replay HTTP status is invalid")
		}
	}
	if created != 1 || ok != 2 {
		return errors.New("US2 replay HTTP status set is invalid")
	}
	if !us2ValidatePGCounts(scenario.PG, 1, 1, 1) {
		return errors.New("US2 replay PG counts are invalid")
	}
	if !us1ValidateCommands(scenario.Commands, false) {
		return errors.New("US2 replay commands are invalid")
	}
	return us2ValidateObserverCounts(scenario, 1, 0)
}

func us2ValidateReplayUnknownScenario(scenario us2ReportScenario) error {
	if scenario.Directive != nil {
		return errors.New("US2 unknown replay unexpectedly carried a directive")
	}
	if len(scenario.HTTP) != 2 {
		return errors.New("US2 unknown replay HTTP count is invalid")
	}
	var orderID string
	for _, observation := range scenario.HTTP {
		if observation.Status != http.StatusOK ||
			observation.Result != string(payment.ResultUnknown) {
			return errors.New("US2 unknown replay HTTP fact is invalid")
		}
		if observation.PaymentID == "" {
			return errors.New("US2 unknown replay lacks a payment id")
		}
		if orderID == "" {
			orderID = observation.PaymentID
		} else if observation.PaymentID != orderID {
			return errors.New("US2 unknown replay changed the payment id")
		}
	}
	if !us2ValidatePGCounts(scenario.PG, 1, 0, 0) {
		return errors.New("US2 unknown replay PG counts are invalid")
	}
	if !us1ValidateCommands(scenario.Commands, false) {
		return errors.New("US2 unknown replay commands are invalid")
	}
	return us2ValidateObserverCounts(scenario, 0, 0)
}

func us2ValidateConflictScenario(scenario us2ReportScenario) error {
	if len(scenario.HTTP) != 9 {
		return errors.New("US2 conflict HTTP count is invalid")
	}
	counts := make(map[int]int, len(scenario.HTTP))
	for _, observation := range scenario.HTTP {
		counts[observation.Status]++
		if observation.Status >= 200 && observation.Status < 300 {
			if observation.Status != http.StatusCreated ||
				observation.PaymentID != scenario.Directive.Binding.OrderID ||
				observation.Result != scenario.Directive.Result {
				return errors.New("US2 conflict setup observation is invalid")
			}
			continue
		}
		if observation.PaymentID != "" || observation.Result != "" {
			return errors.New("US2 conflict rejection carried a payment")
		}
	}
	if counts[http.StatusCreated] != 1 ||
		counts[http.StatusConflict] != 2 ||
		counts[http.StatusBadRequest] != 4 ||
		counts[http.StatusUnauthorized] != 1 ||
		counts[http.StatusInternalServerError] != 0 ||
		counts[http.StatusServiceUnavailable] != 1 {
		return errors.New("US2 conflict HTTP status set is invalid")
	}
	if !us2ValidatePGCounts(scenario.PG, 1, 1, 1) {
		return errors.New("US2 conflict PG counts are invalid")
	}
	if !us1ValidateCommands(scenario.Commands, false) {
		return errors.New("US2 conflict commands are invalid")
	}
	return us2ValidateObserverCounts(scenario, 1, 0)
}

func us2ValidateConcurrencyScenario(scenario us2ReportScenario) error {
	if len(scenario.HTTP) != 6 {
		return errors.New("US2 concurrency HTTP count is invalid")
	}
	counts := make(map[int]int, len(scenario.HTTP))
	confirmed := 0
	for _, observation := range scenario.HTTP {
		counts[observation.Status]++
		if observation.Status >= 200 && observation.Status < 300 {
			if observation.PaymentID != scenario.Directive.Binding.OrderID {
				return errors.New("US2 concurrency HTTP id does not match the directive")
			}
			if observation.Result != string(payment.ResultSucceeded) &&
				observation.Result != string(payment.ResultUnknown) {
				return errors.New("US2 concurrency HTTP result is invalid")
			}
			if observation.Status == http.StatusCreated &&
				observation.Result != string(payment.ResultSucceeded) {
				return errors.New("US2 concurrency creation was not confirmed")
			}
			if observation.Result == string(payment.ResultSucceeded) {
				confirmed++
			}
			continue
		}
		if observation.Status != http.StatusConflict ||
			observation.PaymentID != "" || observation.Result != "" {
			return errors.New("US2 concurrency conflict observation is invalid")
		}
	}
	if counts[http.StatusCreated] != 1 || counts[http.StatusOK] != 4 ||
		counts[http.StatusConflict] != 1 {
		return errors.New("US2 concurrency HTTP status set is invalid")
	}
	if confirmed < 3 {
		return errors.New("US2 concurrency lacks consistent confirmed results")
	}
	if !us2ValidatePGCounts(scenario.PG, 1, 1, 1) {
		return errors.New("US2 concurrency PG counts are invalid")
	}
	if !us1ValidateCommands(scenario.Commands, false) {
		return errors.New("US2 concurrency commands are invalid")
	}
	return us2ValidateObserverCounts(scenario, 1, 0)
}

func us2ValidateRestartScenario(scenario us2ReportScenario, wantResult payment.Result) error {
	if scenario.Directive.Result != string(wantResult) {
		return errors.New("US2 restart directive result is invalid")
	}
	if len(scenario.HTTP) != 6 {
		return errors.New("US2 restart HTTP count is invalid")
	}
	created, ok := 0, 0
	for _, observation := range scenario.HTTP {
		if observation.PaymentID != scenario.Directive.Binding.OrderID ||
			observation.Result != string(wantResult) {
			return errors.New("US2 restart HTTP fact is invalid")
		}
		switch observation.Status {
		case http.StatusCreated:
			created++
		case http.StatusOK:
			ok++
		default:
			return errors.New("US2 restart HTTP status is invalid")
		}
	}
	if created != 1 || ok != 5 {
		return errors.New("US2 restart HTTP status set is invalid")
	}
	if !us2ValidatePGCounts(scenario.PG, 1, 1, 1) {
		return errors.New("US2 restart PG counts are invalid")
	}
	if !us1ValidateCommands(scenario.Commands, true) {
		return errors.New("US2 restart commands are invalid")
	}
	return us2ValidateObserverCounts(scenario, 1, 0)
}

func us2ValidateKeyRotationScenario(scenario us2ReportScenario) error {
	if len(scenario.HTTP) != 4 {
		return errors.New("US2 key rotation HTTP count is invalid")
	}
	counts := make(map[int]int, len(scenario.HTTP))
	for _, observation := range scenario.HTTP {
		counts[observation.Status]++
		switch observation.Status {
		case http.StatusCreated, http.StatusOK:
			if observation.PaymentID != scenario.Directive.Binding.OrderID ||
				observation.Result != scenario.Directive.Result {
				return errors.New("US2 key rotation success fact is invalid")
			}
		case http.StatusUnauthorized:
			if observation.PaymentID != "" || observation.Result != "" {
				return errors.New("US2 key rotation rejection carried a payment")
			}
		default:
			return errors.New("US2 key rotation HTTP status is invalid")
		}
	}
	if counts[http.StatusCreated] != 1 || counts[http.StatusOK] != 2 ||
		counts[http.StatusUnauthorized] != 1 {
		return errors.New("US2 key rotation HTTP status set is invalid")
	}
	if !us2ValidatePGCounts(scenario.PG, 1, 1, 1) {
		return errors.New("US2 key rotation PG counts are invalid")
	}
	if !us1ValidateCommands(scenario.Commands, true) {
		return errors.New("US2 key rotation commands are invalid")
	}
	return us2ValidateObserverCounts(scenario, 1, 0)
}

func us2ValidReportGenerations(name string, generations []uint64) bool {
	switch name {
	case "replay_success", "replay_failed", "replay_unknown", "conflict_priority", "concurrency":
		return len(generations) == 1 && generations[0] == 1
	case "restart_success", "restart_failed", "key_rotation":
		return len(generations) == 2 && generations[0] == 1 && generations[1] == 2
	default:
		return false
	}
}

func us2ValidatePGCounts(states []us1ReportPG, orderRows, mockRows, traceRows uint64) bool {
	if len(states) != 3 {
		return false
	}
	want := map[string]uint64{
		"ORDER": orderRows,
		"MOCK":  mockRows,
		"TRACE": traceRows,
	}
	seen := make(map[string]bool, 3)
	for _, state := range states {
		expected, ok := want[state.State]
		if !ok || seen[state.State] || state.Rows != expected {
			return false
		}
		seen[state.State] = true
	}
	return true
}

func us2ExpectedChecks(name string) (us2ReportChecks, bool) {
	created := us2ReportCounts{
		PaymentCalls: 1,
		QueryCalls:   0,
		OrderRows:    1,
		MockRows:     1,
		TraceRows:    1,
	}
	switch name {
	case "replay_success", "replay_failed", "restart_success", "restart_failed", "key_rotation":
		return us2ReportChecks{
			CountsBefore: created,
			CountsAfter:  created,
			StableFacts:  true,
			LookupFault:  "none",
		}, true
	case "replay_unknown":
		unknown := us2ReportCounts{OrderRows: 1}
		return us2ReportChecks{
			CountsBefore: unknown,
			CountsAfter:  unknown,
			StableFacts:  true,
			LookupFault:  "none",
		}, true
	case "concurrency":
		return us2ReportChecks{
			CountsBefore:     us2ReportCounts{},
			CountsAfter:      created,
			StableFacts:      true,
			LookupFault:      "none",
			WaiterCount:      2,
			MixedWaiterCount: 2,
		}, true
	case "conflict_priority":
		return us2ReportChecks{
			CountsBefore:   created,
			CountsAfter:    created,
			StableFacts:    true,
			ReadonlyProbes: 5,
			LookupFault:    "timeout",
		}, true
	default:
		return us2ReportChecks{}, false
	}
}

func us2ValidateChecks(scenario us2ReportScenario) error {
	if scenario.Checks == nil {
		return errors.New("US2 scenario lacks checks evidence")
	}
	expected, ok := us2ExpectedChecks(scenario.Name)
	if !ok {
		return errors.New("US2 checks scenario name is invalid")
	}
	switch scenario.Checks.LookupFault {
	case "none", "timeout", "unavailable":
	default:
		return errors.New("US2 checks lookup fault is invalid")
	}
	if *scenario.Checks != expected {
		return errors.New("US2 checks evidence mismatch")
	}
	if !us2ChecksRowsMatchPG(scenario.Checks.CountsAfter, scenario.PG) {
		return errors.New("US2 checks after rows do not match PG")
	}
	if scenario.Observer == nil ||
		scenario.Checks.CountsAfter.PaymentCalls != scenario.Observer.Counts.Payment ||
		scenario.Checks.CountsAfter.QueryCalls != scenario.Observer.Counts.Query {
		return errors.New("US2 checks after calls do not match observer")
	}
	return nil
}

func us2ChecksRowsMatchPG(counts us2ReportCounts, states []us1ReportPG) bool {
	return us2ValidatePGCounts(states, counts.OrderRows, counts.MockRows, counts.TraceRows)
}

// us2BeforeCounts derives the baseline call counts from the already-confirmed
// setup payment binding and the absence of a setup query. Row counts are the
// caller's actual DB reads. This is not a midflight observer snapshot: the
// final observer total is checked separately and must not increase.
func us2BeforeCounts(binding *harness.PaymentBinding, orders, mocks, traces uint64) us2ReportCounts {
	paymentCalls := uint64(0)
	if binding != nil {
		paymentCalls = 1
	}
	return us2ReportCounts{
		PaymentCalls: paymentCalls,
		QueryCalls:   0,
		OrderRows:    orders,
		MockRows:     mocks,
		TraceRows:    traces,
	}
}

func us2CountsAfter(final harness.ObserverFinalSnapshot, facts *us2ReportScenario) (us2ReportCounts, error) {
	if facts == nil {
		return us2ReportCounts{}, errors.New("US2 final counts lack facts")
	}
	counts := us2ReportCounts{
		PaymentCalls: final.Counts.Payment,
		QueryCalls:   final.Counts.Query,
	}
	seen := make(map[string]bool, 3)
	for _, state := range facts.PG {
		switch state.State {
		case "ORDER":
			counts.OrderRows = state.Rows
		case "MOCK":
			counts.MockRows = state.Rows
		case "TRACE":
			counts.TraceRows = state.Rows
		default:
			return us2ReportCounts{}, errors.New("US2 final counts contain an unknown PG state")
		}
		if seen[state.State] {
			return us2ReportCounts{}, errors.New("US2 final counts contain a duplicate PG state")
		}
		seen[state.State] = true
	}
	if !seen["ORDER"] || !seen["MOCK"] || !seen["TRACE"] {
		return us2ReportCounts{}, errors.New("US2 final counts lack a PG state")
	}
	return counts, nil
}

func us2RequireRollback(t *testing.T, err error, label string) {
	t.Helper()
	if err != nil && !errors.Is(err, pgx.ErrTxClosed) {
		t.Errorf("US2 %s rollback failed (%s)", label, us1SQLState(err))
	}
}

type us2ScenarioState struct {
	mu          sync.Mutex
	artifacts   *harness.Artifacts
	scenario    string
	finished    bool
	finishCalls int
}

var (
	us2ScenarioStatesMu sync.Mutex
	us2ScenarioStates   = make(map[*harness.Artifacts]*us2ScenarioState)
)

func us2RegisterScenarioState(artifacts *harness.Artifacts, scenario string) *us2ScenarioState {
	state := &us2ScenarioState{
		artifacts: artifacts,
		scenario:  scenario,
	}
	us2ScenarioStatesMu.Lock()
	us2ScenarioStates[artifacts] = state
	us2ScenarioStatesMu.Unlock()
	return state
}

func us2ScenarioStateFor(artifacts *harness.Artifacts) *us2ScenarioState {
	us2ScenarioStatesMu.Lock()
	defer us2ScenarioStatesMu.Unlock()
	return us2ScenarioStates[artifacts]
}

func (state *us2ScenarioState) finishSuccess(t *testing.T) error {
	return state.finish(t, true)
}

func (state *us2ScenarioState) finishFailure(t *testing.T) error {
	return state.finish(t, false)
}

func (state *us2ScenarioState) finish(t *testing.T, success bool) error {
	t.Helper()
	state.mu.Lock()
	if state.finished {
		state.mu.Unlock()
		return nil
	}
	state.finished = true
	state.finishCalls++
	state.mu.Unlock()

	latchedFault := false
	if !success {
		// Latch a fixed harness fault without fabricating a business observation.
		_ = state.artifacts.RecordHTTP(harness.HTTPObservation{Status: 0})
		latchedFault = true
	}
	_, finishErr := state.artifacts.Finish()
	if finishErr != nil && !latchedFault {
		return finishErr
	}
	return finishErr
}

func us2BeginScenarioArtifacts(t *testing.T, artifactRoot, repoRoot, runID, scenario string) *harness.Artifacts {
	t.Helper()
	if !us2ReportScenarioNameSet[scenario] {
		t.Fatal("US2 report scenario name is invalid")
	}
	root := us2ScenarioRoot(artifactRoot, runID, scenario)
	if err := os.MkdirAll(root, 0o700); err != nil {
		t.Fatal("US2 scenario artifact root creation failed")
	}
	if err := us1ValidateArtifactRoot(root); err != nil {
		t.Fatal("US2 scenario artifact root is unsafe")
	}
	artifacts, err := harness.BeginArtifacts(harness.ArtifactConfig{
		Root:       root,
		SourceRoot: repoRoot,
		RunID:      runID,
	})
	if err != nil {
		t.Fatal("US2 artifacts begin failed")
	}
	state := us2RegisterScenarioState(artifacts, scenario)
	t.Cleanup(func() { _ = state.finishFailure(t) })
	return artifacts
}

func us2BeginArtifacts(t *testing.T, repoRoot, runID, scenario string) *harness.Artifacts {
	t.Helper()
	collector := us2ReportInit(t, runID)
	return us2BeginScenarioArtifacts(t, collector.root, repoRoot, runID, scenario)
}

func us2FinishArtifacts(t *testing.T, artifacts *harness.Artifacts) {
	t.Helper()
	if state := us2ScenarioStateFor(artifacts); state != nil {
		if err := state.finishSuccess(t); err != nil {
			t.Fatal("US2 artifacts finish failed")
		}
		return
	}
	path, err := artifacts.Finish()
	if err != nil {
		t.Fatal("US2 artifacts finish failed")
	}
	if path == "" {
		t.Fatal("US2 artifacts finish returned an empty path")
	}
}

func us2RecordHTTP(t *testing.T, artifacts *harness.Artifacts, facts *us2ReportScenario, generation uint64, status int, requestID, paymentID, result string) {
	t.Helper()
	if err := artifacts.RecordHTTP(harness.HTTPObservation{
		Generation:     generation,
		Status:         status,
		RequestID:      requestID,
		PaymentOrderID: paymentID,
		Result:         result,
	}); err != nil {
		t.Fatal("US2 HTTP artifact record failed")
	}
	if facts != nil {
		facts.addHTTP(status, requestID, paymentID, result)
	}
}

func us2RequireUniqueRequestIDs(t *testing.T, ids ...string) {
	t.Helper()
	seen := make(map[string]bool, len(ids))
	for _, id := range ids {
		if id == "" || seen[id] {
			t.Fatal("US2 request_id was reused or empty")
		}
		seen[id] = true
	}
}

func us2RecordSQL(t *testing.T, artifacts *harness.Artifacts, facts *us2ReportScenario, generation uint64, tableLabel string, rows uint64) {
	t.Helper()
	if !us1ValidSQLTableLabel(tableLabel) {
		t.Fatal("US2 SQL artifact record failed")
	}
	if err := artifacts.RecordSQL(harness.SQLObservation{
		Generation: generation,
		State:      "00000",
		Rows:       rows,
	}); err != nil {
		t.Fatal("US2 SQL artifact record failed")
	}
	if facts != nil {
		facts.addPG(tableLabel, rows)
	}
}

func us2RecordBarrierBudget(t *testing.T, artifacts *harness.Artifacts, generation uint64, limit, elapsed time.Duration) {
	t.Helper()
	if err := artifacts.RecordBudget(harness.BudgetObservation{
		Generation: generation,
		Stage:      harness.BudgetBarrier,
		Limit:      limit,
		Elapsed:    elapsed,
	}); err != nil {
		t.Fatal("US2 barrier budget artifact record failed")
	}
}

func us2AppEnv(t *testing.T, dsn, httpAddr, clientID, apiKey, socket, runID, epoch string, generation uint64) []string {
	t.Helper()
	return []string{
		"PAYMENT_HUB_CLIENT_ID=" + clientID,
		"PAYMENT_HUB_API_KEY=" + apiKey,
		"PAYMENT_HUB_DATABASE_URL=" + dsn,
		"PAYMENT_HUB_HTTP_ADDR=" + httpAddr,
		"PAYMENT_HUB_TEST_MODE=on",
		"PAYMENT_HUB_TEST_CONTROL_SOCKET=" + socket,
		"PAYMENT_HUB_TEST_RUN_ID=" + runID,
		"PAYMENT_HUB_TEST_OBSERVER_EPOCH=" + epoch,
		"PAYMENT_HUB_TEST_GENERATION=" + fmt.Sprintf("%d", generation),
		"TMPDIR=" + us1IsolatedTempDir(t),
		"TZ=UTC",
	}
}

func us2StartGeneration(t *testing.T, artifacts *harness.Artifacts, binary, repoRoot, dsn, clientID, apiKey, socket, runID, epoch string, generation uint64) (*harness.Process, string) {
	t.Helper()
	addr := us1FreeLoopbackAddr(t)
	env := us2AppEnv(t, dsn, addr, clientID, apiKey, socket, runID, epoch, generation)
	process := us1StartApplication(t, artifacts, binary, repoRoot, generation, env, 10*time.Second, 60*time.Second)
	us1WaitReady(t, addr)
	return process, addr
}

type us2IssuePolicy int

const (
	us2IssuesNone us2IssuePolicy = iota
	us2IssuesOptionalUnconfirmed
)

func us2RequirePaymentResponse(t *testing.T, response *http.Response, body []byte, wantStatus int, wantPaymentID, wantBusinessOrderNo string, wantAmount int64, wantCurrency string, wantResult payment.Result, issues us2IssuePolicy) (us1Payment, string) {
	t.Helper()
	if response == nil {
		t.Fatal("US2 payment response is nil")
	}
	if response.StatusCode != wantStatus {
		t.Fatalf("US2 payment response status mismatch: got %d want %d", response.StatusCode, wantStatus)
	}
	if !strings.HasPrefix(response.Header.Get("Content-Type"), "application/json") {
		t.Fatal("US2 payment response content type mismatch")
	}
	if !strings.Contains(response.Header.Get("Cache-Control"), "no-store") {
		t.Fatal("US2 payment response cache policy mismatch")
	}
	envelope := us1ParseEnvelope(t, body)
	if envelope.RequestID == "" {
		t.Fatal("US2 payment response lacks request_id")
	}
	if err := payment.ValidateUUIDv4(envelope.RequestID); err != nil {
		t.Fatal("US2 payment response request_id is not UUIDv4")
	}
	if envelope.Payment == nil || envelope.Error != nil {
		t.Fatal("US2 payment response envelope mismatch")
	}
	paymentBody := *envelope.Payment
	if err := payment.ValidateUUIDv4(paymentBody.PaymentID); err != nil {
		t.Fatal("US2 payment_id is not UUIDv4")
	}
	if paymentBody.PaymentID != wantPaymentID {
		t.Fatal("US2 payment_id mismatch")
	}
	if paymentBody.BusinessOrderNo != wantBusinessOrderNo {
		t.Fatal("US2 business_order_no mismatch")
	}
	if paymentBody.Amount != wantAmount {
		t.Fatal("US2 amount mismatch")
	}
	if paymentBody.Currency != wantCurrency {
		t.Fatal("US2 currency mismatch")
	}
	if paymentBody.Result != string(wantResult) {
		t.Fatalf("US2 payment result mismatch: got %s want %s", paymentBody.Result, wantResult)
	}
	us1RequireRFC3339Z(t, paymentBody.RegisteredAt)
	if wantResult == payment.ResultSucceeded || wantResult == payment.ResultFailed {
		if paymentBody.ConfirmedAt == nil {
			t.Fatal("US2 confirmed payment lacks confirmed_at")
		}
		us1RequireRFC3339Z(t, *paymentBody.ConfirmedAt)
	} else if paymentBody.ConfirmedAt != nil {
		t.Fatal("US2 unknown payment unexpectedly carried confirmed_at")
	}
	if wantStatus == http.StatusCreated {
		wantLocation := "/api/v1/payments/" + paymentBody.PaymentID
		if response.Header.Get("Location") != wantLocation {
			t.Fatal("US2 created payment Location mismatch")
		}
	} else if response.Header.Get("Location") != "" {
		t.Fatal("US2 non-created payment unexpectedly carried Location")
	}
	unconfirmed := 0
	for _, issue := range envelope.Issues {
		if issue.Code != payment.IssueCodeMockResultUnconfirmed || issue.Message == "" {
			t.Fatal("US2 payment response carried an unexpected issue")
		}
		unconfirmed++
	}
	switch issues {
	case us2IssuesNone:
		if len(envelope.Issues) != 0 {
			t.Fatal("US2 payment response unexpectedly carried issues")
		}
	case us2IssuesOptionalUnconfirmed:
	default:
		t.Fatal("US2 payment issue policy is invalid")
	}
	return paymentBody, envelope.RequestID
}

func us2RequireProcessingError(t *testing.T, response *http.Response, body []byte) (string, int) {
	t.Helper()
	if response == nil {
		t.Fatal("US2 processing error response is nil")
	}
	if response.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("US2 processing error status mismatch: got %d want 503", response.StatusCode)
	}
	if !strings.HasPrefix(response.Header.Get("Content-Type"), "application/json") {
		t.Fatal("US2 processing error content type mismatch")
	}
	if !strings.Contains(response.Header.Get("Cache-Control"), "no-store") {
		t.Fatal("US2 processing error cache policy mismatch")
	}
	envelope := us1ParseEnvelope(t, body)
	if envelope.RequestID == "" || payment.ValidateUUIDv4(envelope.RequestID) != nil {
		t.Fatal("US2 processing error request_id mismatch")
	}
	if envelope.Payment != nil || envelope.Error == nil {
		t.Fatal("US2 processing error envelope mismatch")
	}
	if envelope.Error.Code != "PROCESSING_ERROR" || envelope.Error.Message == "" {
		t.Fatal("US2 processing error code/message mismatch")
	}
	if len(envelope.Issues) != 0 {
		t.Fatal("US2 processing error unexpectedly carried issues")
	}
	return envelope.RequestID, response.StatusCode
}

func us2PostIntent(ctx context.Context, addr, apiKey, businessOrderNo string, amount int64, currency string) (*http.Response, []byte, error) {
	payload, err := json.Marshal(struct {
		BusinessOrderNo string `json:"business_order_no"`
		Amount          int64  `json:"amount"`
		Currency        string `json:"currency"`
	}{
		BusinessOrderNo: businessOrderNo,
		Amount:          amount,
		Currency:        currency,
	})
	if err != nil {
		return nil, nil, err
	}
	return us1PostBytes(ctx, addr, apiKey, payload, "application/json")
}

func us2PostRaw(ctx context.Context, addr, apiKey string, body []byte) (*http.Response, []byte, error) {
	return us1PostBytes(ctx, addr, apiKey, body, "application/json")
}

func us2StructuralBody(businessOrderNo, amountLiteral string) []byte {
	return []byte(fmt.Sprintf(`{"business_order_no":%q,"amount":%s,"currency":"CNY"}`, businessOrderNo, amountLiteral))
}

func us2SubmitWithHold(
	t *testing.T,
	artifacts *harness.Artifacts,
	facts *us2ReportScenario,
	observer *harness.Observer,
	binary, repoRoot, dsn, runID, epoch, socket, clientID, businessOrderNo, apiKey string,
	generation uint64,
	directive testcontrol.Directive,
	wantResult payment.Result,
	amount int64,
) (*harness.Process, string, harness.PaymentBinding, us1Payment, string) {
	t.Helper()
	process, addr := us2StartGeneration(t, artifacts, binary, repoRoot, dsn, clientID, apiKey, socket, runID, epoch, generation)

	hold, err := observer.HoldPaymentEntry(generation)
	if err != nil {
		t.Fatal("US2 payment hold arm failed")
	}
	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()

	type postResult struct {
		response *http.Response
		body     []byte
		err      error
	}
	postCh := make(chan postResult, 1)
	go func() {
		response, body, err := us1PostPayment(ctx, addr, apiKey, businessOrderNo, amount, "CNY")
		postCh <- postResult{response: response, body: body, err: err}
	}()

	waitStart := time.Now()
	binding, err := hold.Wait(ctx)
	waitElapsed := time.Since(waitStart)
	if err != nil {
		t.Fatal("US2 payment hold wait failed")
	}
	if binding.Generation != generation {
		t.Fatal("US2 payment binding generation mismatch")
	}
	if binding.OrderID == "" || payment.ValidateUUIDv4(binding.OrderID) != nil {
		t.Fatal("US2 payment binding order id is invalid")
	}
	if err := hold.Release(directive); err != nil {
		t.Fatal("US2 payment hold release failed")
	}
	us2RecordBarrierBudget(t, artifacts, generation, 2*time.Second, waitElapsed)
	if facts != nil {
		facts.Directive = &us1ReportDirective{
			Result: string(directive),
			Binding: us1ReportBinding{
				RunID:      binding.RunID,
				Epoch:      binding.Epoch,
				Generation: binding.Generation,
				CallSeq:    binding.CallSeq,
				EventID:    binding.EventID,
				OrderID:    binding.OrderID,
			},
		}
	}

	post := <-postCh
	if post.err != nil {
		t.Fatal("US2 payment POST failed")
	}
	paymentBody, requestID := us2RequirePaymentResponse(t, post.response, post.body, http.StatusCreated, binding.OrderID, businessOrderNo, amount, "CNY", wantResult, us2IssuesNone)
	us2RecordHTTP(t, artifacts, facts, generation, post.response.StatusCode, requestID, binding.OrderID, string(paymentBody.Result))
	return process, addr, binding, paymentBody, requestID
}

func us2SeedUnknownOrder(t *testing.T, pool *pgxpool.Pool, clientID, businessOrderNo string, amount int64) string {
	t.Helper()
	orderID := payment.NewUUIDv4()
	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	if _, err := pool.Exec(ctx, `
		INSERT INTO payment_orders
			(id, client_id, business_order_no, amount, currency, result, mock_transaction_id, registered_at, confirmed_at)
		VALUES ($1, $2, $3, $4, 'CNY', 'UNKNOWN', NULL, $5, NULL)
	`, orderID, clientID, businessOrderNo, amount, time.Now().UTC().Truncate(time.Microsecond)); err != nil {
		t.Fatalf("US2 unknown seed insert failed (%s)", us1SQLState(err))
	}
	return orderID
}

func us2VerifyUnknownReplay(t *testing.T, pool *pgxpool.Pool, artifacts *harness.Artifacts, facts *us2ReportScenario, generation uint64, clientID, businessOrderNo, paymentID string, amount int64) us1OrderState {
	t.Helper()
	order := us1ReadOrderState(t, pool, clientID, businessOrderNo)
	if order.ID != paymentID || order.ClientID != clientID ||
		order.BusinessOrderNo != businessOrderNo || order.Amount != amount ||
		order.Currency != "CNY" || order.Result != string(payment.ResultUnknown) ||
		order.MockTransactionID != "" || order.ConfirmedAt != nil {
		t.Fatal("US2 unknown seed order changed or is invalid")
	}
	orders, mocks, handling := us1CountBusinessRows(t, pool, clientID)
	if orders != 1 || mocks != 0 || handling != 0 {
		t.Fatalf("US2 unknown replay row counts mismatch: orders=%d mock=%d handling=%d", orders, mocks, handling)
	}
	if facts != nil {
		facts.PG = nil
	}
	us2RecordSQL(t, artifacts, facts, generation, "ORDER", uint64(orders))
	us2RecordSQL(t, artifacts, facts, generation, "MOCK", uint64(mocks))
	us2RecordSQL(t, artifacts, facts, generation, "TRACE", uint64(handling))
	return order
}

func us2VerifyStoredFacts(t *testing.T, pool *pgxpool.Pool, artifacts *harness.Artifacts, facts *us2ReportScenario, generation uint64, clientID, businessOrderNo, paymentID string, amount int64, wantResult payment.Result) (us1OrderState, us1MockState) {
	t.Helper()
	order := us1ReadOrderState(t, pool, clientID, businessOrderNo)
	mock := us1ReadMockState(t, pool, paymentID)
	us1RequireOrderState(t, order, clientID, businessOrderNo, paymentID, amount, "CNY", wantResult)
	us1RequireMockState(t, mock, paymentID, amount, "CNY", wantResult)
	orders, mocks, handling := us1CountBusinessRows(t, pool, clientID)
	if orders != 1 || mocks != 1 || handling != 1 {
		t.Fatalf("US2 stored row counts mismatch: orders=%d mock=%d handling=%d", orders, mocks, handling)
	}
	if wantResult == payment.ResultSucceeded || wantResult == payment.ResultFailed {
		record := us1ReadHandlingState(t, pool, paymentID)
		us1RequireHandlingState(t, record, paymentID, mock.ID, amount, "CNY", wantResult)
	}
	if facts != nil {
		facts.PG = nil
	}
	us2RecordSQL(t, artifacts, facts, generation, "ORDER", uint64(orders))
	us2RecordSQL(t, artifacts, facts, generation, "MOCK", uint64(mocks))
	us2RecordSQL(t, artifacts, facts, generation, "TRACE", uint64(handling))
	return order, mock
}

func us2OrderFactsStable(before, after us1OrderState) bool {
	if before.ID != after.ID ||
		before.ClientID != after.ClientID ||
		before.BusinessOrderNo != after.BusinessOrderNo ||
		before.Amount != after.Amount ||
		before.Currency != after.Currency ||
		before.Result != after.Result ||
		before.MockTransactionID != after.MockTransactionID ||
		!before.RegisteredAt.Equal(after.RegisteredAt) {
		return false
	}
	switch {
	case before.ConfirmedAt == nil && after.ConfirmedAt == nil:
		return true
	case before.ConfirmedAt != nil && after.ConfirmedAt != nil &&
		before.ConfirmedAt.Equal(*after.ConfirmedAt):
		return true
	default:
		return false
	}
}

func us2MockFactsStable(before, after us1MockState) bool {
	return before.ID == after.ID &&
		before.PlatformReferenceID == after.PlatformReferenceID &&
		before.Amount == after.Amount &&
		before.Currency == after.Currency &&
		before.Result == after.Result &&
		before.ProcessedAt.Equal(after.ProcessedAt)
}

// us2RequireOrderUnchanged returns true only after the actual before/after
// order identity, result, and time comparisons pass. The bool is an assertion
// result, not an independent database digest.
func us2RequireOrderUnchanged(t *testing.T, before, after us1OrderState) bool {
	t.Helper()
	if !us2OrderFactsStable(before, after) {
		t.Fatal("US2 stored order identity or result changed")
	}
	return true
}

// us2RequireFactsUnchanged returns true only after the actual before/after
// order and Mock fact comparisons pass. The bool is an assertion result, not
// an independent database digest.
func us2RequireFactsUnchanged(t *testing.T, beforeOrder, afterOrder us1OrderState, beforeMock, afterMock us1MockState) bool {
	t.Helper()
	if !us2OrderFactsStable(beforeOrder, afterOrder) ||
		!us2MockFactsStable(beforeMock, afterMock) {
		t.Fatal("US2 stored order or mock fact changed")
	}
	return true
}

func us2FinalizeScenario(t *testing.T, artifacts *harness.Artifacts, facts *us2ReportScenario, scenarioName string, observer *harness.Observer, process *harness.Process, wantCounts harness.Counts, wantGenerations []uint64, checks us2ReportChecks) {
	t.Helper()
	exit := us1StopProcess(t, process)
	if facts != nil {
		facts.addCommand(us1ReportCommandStop, exit.Code, false)
	}
	counts, err := observer.Snapshot()
	if err != nil {
		t.Fatal("US2 observer snapshot failed")
	}
	if counts != wantCounts {
		t.Fatalf("US2 observer counts mismatch: got %+v want %+v", counts, wantCounts)
	}
	if err := artifacts.RecordObserver(observer); err != nil {
		t.Fatal("US2 observer artifact record failed")
	}
	us1CloseObserver(t, observer)
	final, ok := observer.FinalSnapshot()
	if !ok || !final.Trusted {
		t.Fatal("US2 observer final snapshot is not trusted")
	}
	if final.Counts != wantCounts {
		t.Fatalf("US2 final observer counts mismatch: got %+v want %+v", final.Counts, wantCounts)
	}
	if len(final.Receipts) != len(wantGenerations) {
		t.Fatalf("US2 final receipt count mismatch: got %d want %d", len(final.Receipts), len(wantGenerations))
	}
	for index, generation := range wantGenerations {
		if final.Receipts[index].Generation != generation {
			t.Fatal("US2 final receipt generation mismatch")
		}
	}
	if facts != nil {
		receipts := make([]us1ReportReceipt, 0, len(final.Receipts))
		for _, receipt := range final.Receipts {
			receipts = append(receipts, us1ReportReceipt{
				RunID:      receipt.RunID,
				Epoch:      receipt.Epoch,
				Generation: receipt.Generation,
				LastSeq:    receipt.LastSeq,
			})
		}
		facts.Observer = &us1ReportObserver{
			Trusted:  final.Trusted,
			Counts:   us1ReportCounts{Payment: final.Counts.Payment, Query: final.Counts.Query},
			Receipts: receipts,
		}
		facts.Name = scenarioName
		actualAfter, err := us2CountsAfter(final, facts)
		if err != nil {
			t.Fatal("US2 final counts construction failed")
		}
		checks.CountsAfter = actualAfter
		facts.Checks = &checks
		if err := us2ValidateChecks(*facts); err != nil {
			t.Fatal("US2 final checks do not match expected")
		}
	}
	us2FinishArtifacts(t, artifacts)
	if facts != nil {
		facts.Name = scenarioName
		facts.Result = us1ReportResultPass
		us2ReportRecord(t, facts)
	}
}

func us2WaitLockWaiters(t *testing.T, pool *pgxpool.Pool, want, blockerPID int) (int, time.Duration) {
	t.Helper()
	start := time.Now()
	deadline := start.Add(us1BarrierBudget)
	for {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		var count int
		err := pool.QueryRow(ctx, `
			SELECT count(DISTINCT a.pid)
			FROM pg_locks l
			JOIN pg_stat_activity a ON a.pid = l.pid
			WHERE l.locktype = 'transactionid'
			  AND NOT l.granted
			  AND a.datname = current_database()
			  AND $1 = ANY(pg_blocking_pids(a.pid))
			  AND a.pid <> pg_backend_pid()
		`, blockerPID).Scan(&count)
		cancel()
		if err != nil {
			t.Fatalf("US2 PG waiter poll failed (%s)", us1SQLState(err))
		}
		if count >= want {
			return count, time.Since(start)
		}
		if time.Now().After(deadline) {
			t.Fatalf("US2 PG waiter poll timed out: got %d want %d", count, want)
		}
		runtime.Gosched()
	}
}

// us2WaitTableInsertWaiters counts only the current test database's INSERT
// waiters blocked by the specific SHARE-lock holder. The SHARE fixture is a
// whole payment_orders table lock in the isolated fresh test database and must
// not run concurrently with any other PG test.
func us2WaitTableInsertWaiters(t *testing.T, pool *pgxpool.Pool, want, blockerPID int) (int, time.Duration) {
	t.Helper()
	start := time.Now()
	deadline := start.Add(us1BarrierBudget)
	for {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		var count int
		err := pool.QueryRow(ctx, `
			SELECT count(DISTINCT a.pid)
			FROM pg_locks l
			JOIN pg_stat_activity a ON a.pid = l.pid
			JOIN pg_class c ON c.oid = l.relation
			WHERE l.locktype = 'relation'
			  AND l.mode = 'RowExclusiveLock'
			  AND NOT l.granted
			  AND c.oid = 'payment_orders'::regclass
			  AND a.datname = current_database()
			  AND $1 = ANY(pg_blocking_pids(a.pid))
			  AND a.pid <> pg_backend_pid()
		`, blockerPID).Scan(&count)
		cancel()
		if err != nil {
			t.Fatalf("US2 mixed PG waiter poll failed (%s)", us1SQLState(err))
		}
		if count >= want {
			return count, time.Since(start)
		}
		if time.Now().After(deadline) {
			t.Fatalf("US2 mixed PG waiter poll timed out: got %d want %d", count, want)
		}
		runtime.Gosched()
	}
}

func us2WaitOrderConfirmed(t *testing.T, pool *pgxpool.Pool, clientID, businessOrderNo string) us1OrderState {
	t.Helper()
	deadline := time.Now().Add(us1BarrierBudget)
	for {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		var result string
		err := pool.QueryRow(ctx, `
			SELECT result FROM payment_orders
			WHERE client_id = $1 AND business_order_no = $2
		`, clientID, businessOrderNo).Scan(&result)
		cancel()
		if err == nil {
			if result != string(payment.ResultUnknown) {
				return us1ReadOrderState(t, pool, clientID, businessOrderNo)
			}
		} else if !errors.Is(err, pgx.ErrNoRows) {
			t.Fatalf("US2 order confirmation poll failed (%s)", us1SQLState(err))
		}
		if time.Now().After(deadline) {
			t.Fatal("US2 order confirmation poll timed out")
		}
		runtime.Gosched()
	}
}

// us2LockPaymentOrders acquires ACCESS EXCLUSIVE on the whole payment_orders
// table in the caller's isolated fresh test database. It is not a client-row
// lock and must not run concurrently with any other PG test. The lock is
// always rolled back; rollback errors fail the test.
func us2LockPaymentOrders(t *testing.T, pool *pgxpool.Pool) func() {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 2*us1RequestBudget)
	tx, err := pool.Begin(ctx)
	if err != nil {
		cancel()
		t.Fatalf("US2 payment_orders lock transaction failed (%s)", us1SQLState(err))
	}
	if _, err := tx.Exec(ctx, "LOCK TABLE payment_orders IN ACCESS EXCLUSIVE MODE"); err != nil {
		us2RequireRollback(t, tx.Rollback(ctx), "payment_orders lock setup")
		cancel()
		t.Fatalf("US2 payment_orders lock failed (%s)", us1SQLState(err))
	}
	var once sync.Once
	release := func() {
		once.Do(func() {
			rollbackCtx, rollbackCancel := context.WithTimeout(context.Background(), us1CleanupBudget)
			defer rollbackCancel()
			us2RequireRollback(t, tx.Rollback(rollbackCtx), "payment_orders lock")
			cancel()
		})
	}
	t.Cleanup(release)
	return release
}

func us2CandidateInsert(t *testing.T, ctx context.Context, tx pgx.Tx, candidateID, clientID, businessOrderNo string, amount int64) {
	t.Helper()
	if _, err := tx.Exec(ctx, `
		INSERT INTO payment_orders
			(id, client_id, business_order_no, amount, currency, result, mock_transaction_id, registered_at, confirmed_at)
		VALUES ($1, $2, $3, $4, 'CNY', 'UNKNOWN', NULL, $5, NULL)
	`, candidateID, clientID, businessOrderNo, amount, time.Now().UTC().Truncate(time.Microsecond)); err != nil {
		t.Fatalf("US2 uncommitted candidate insert failed (%s)", us1SQLState(err))
	}
}
