//go:build integration

package tests

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"payment-hub/internal/payment"
	"payment-hub/internal/testcontrol"
	"payment-hub/tests/harness"
)

const (
	demoReportSchema = "payment-hub.demo.v1"
	demoAmount       = int64(100)
	demoBudget       = 120 * time.Second
)

type demoStep struct {
	Step       string   `json:"step"`
	Status     int      `json:"status"`
	RequestID  string   `json:"request_id"`
	PaymentID  string   `json:"payment_id"`
	Result     string   `json:"result"`
	IssueCodes []string `json:"issue_codes"`
}

type demoReport struct {
	Schema        string     `json:"schema"`
	Steps         []demoStep `json:"steps"`
	TraceComplete bool       `json:"trace_complete"`
}

// TestHTTPDemo runs the fixed local HTTP demo against the real application for
// the three approved result expectations. The trusted observer prepares only
// the real success/failure result; the demo itself never sends a scenario field.
func TestHTTPDemo(t *testing.T) {
	repoRoot := us1RepoRoot(t)
	runID := us1RunID(t)
	artifactRoot := os.Getenv(us1ArtifactRootEnv)
	if artifactRoot == "" {
		t.Fatal("T080 demo artifact root configuration is required")
	}
	binary := us1BuildApplication(t, repoRoot)
	pool := us1OpenPool(t)
	dsn, err := us1BuildAppDSN(pool.Config().ConnConfig)
	if err != nil {
		t.Fatal("T080 demo database URL construction failed")
	}

	cases := []struct {
		name      string
		expect    string
		directive testcontrol.Directive
		unknown   bool
	}{
		{name: "succeeded", expect: "SUCCEEDED", directive: testcontrol.DirectiveSucceeded},
		{name: "failed", expect: "FAILED", directive: testcontrol.DirectiveFailed},
		{name: "unknown", expect: "UNKNOWN", unknown: true},
	}
	for _, testCase := range cases {
		testCase := testCase
		t.Run(testCase.name, func(t *testing.T) {
			demoRunScenario(
				t,
				repoRoot,
				runID,
				artifactRoot,
				binary,
				dsn,
				pool,
				testCase.name,
				testCase.expect,
				testCase.directive,
				testCase.unknown,
			)
		})
	}
}

func demoBeginArtifacts(t *testing.T, repoRoot, runID, artifactRoot, scenario string) *harness.Artifacts {
	t.Helper()
	root := filepath.Join(
		artifactRoot,
		runID,
		"scenarios",
		"demo_"+scenario,
	)
	if err := os.MkdirAll(root, 0o700); err != nil {
		t.Fatal("T080 demo artifact namespace creation failed")
	}
	if err := us1ValidateArtifactRoot(root); err != nil {
		t.Fatal("T080 demo artifact namespace is unsafe")
	}
	artifacts, err := harness.BeginArtifacts(harness.ArtifactConfig{
		Root:       root,
		SourceRoot: repoRoot,
		RunID:      runID,
	})
	if err != nil {
		t.Fatal("T080 demo artifacts begin failed")
	}
	return artifacts
}

func demoRunScenario(
	t *testing.T,
	repoRoot, runID, artifactRoot, binary, dsn string,
	pool *pgxpool.Pool,
	scenario, expect string,
	directive testcontrol.Directive,
	unknown bool,
) {
	t.Helper()
	clientID := us1UniqueClientID(t)
	businessOrderNo := us1UniqueBusinessOrderNo(t)
	artifacts := demoBeginArtifacts(t, repoRoot, runID, artifactRoot, scenario)
	t.Cleanup(func() {
		us1CleanupClient(t, pool, clientID)
		us1RequireNoBusinessRows(t, pool, clientID)
	})

	socket := us1ObserverSocket(t)
	epoch := us1Epoch(t)
	observer := us3StartObserver(t, runID, epoch, socket)
	if err := observer.Expect(1); err != nil {
		t.Fatal("T080 demo observer expect failed")
	}
	address := us1FreeLoopbackAddr(t)
	process := us1StartApplication(
		t,
		artifacts,
		binary,
		repoRoot,
		1,
		us3AppEnv(
			t,
			dsn,
			address,
			clientID,
			us1APIKey,
			socket,
			runID,
			epoch,
			1,
		),
		10*time.Second,
		60*time.Second,
	)
	us1WaitReady(t, address)

	report := demoRunScript(
		t,
		repoRoot,
		address,
		businessOrderNo,
		expect,
		observer,
		directive,
		unknown,
		pool,
	)
	demoRequireStoredFacts(t, pool, clientID, businessOrderNo, report, expect, unknown)

	us1StopProcess(t, process)
	wantPayment := uint64(1)
	wantQuery := uint64(0)
	if unknown {
		wantQuery = 2
	}
	counts, err := observer.Snapshot()
	if err != nil {
		t.Fatal("T080 demo observer snapshot failed")
	}
	if counts.Payment != wantPayment || counts.Query != wantQuery {
		t.Fatalf(
			"T080 demo observer counts mismatch: payment=%d query=%d",
			counts.Payment,
			counts.Query,
		)
	}
	if err := artifacts.RecordObserver(observer); err != nil {
		t.Fatal("T080 demo observer artifact record failed")
	}
	us1CloseObserver(t, observer)
	final, ok := observer.FinalSnapshot()
	if !ok || !final.Trusted {
		t.Fatal("T080 demo observer final snapshot is not trusted")
	}
	if final.Counts.Payment != wantPayment || final.Counts.Query != wantQuery {
		t.Fatalf(
			"T080 demo final observer counts mismatch: payment=%d query=%d",
			final.Counts.Payment,
			final.Counts.Query,
		)
	}

	us1CleanupClient(t, pool, clientID)
	us1RequireNoBusinessRows(t, pool, clientID)
	if _, err := artifacts.Finish(); err != nil {
		t.Fatal("T080 demo artifacts finish failed")
	}
}

func demoRunScript(
	t *testing.T,
	repoRoot, address, businessOrderNo, expect string,
	observer *harness.Observer,
	directive testcontrol.Directive,
	unknown bool,
	pool *pgxpool.Pool,
) demoReport {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), demoBudget)
	defer cancel()

	if unknown {
		releaseMock, blockerPID := us3LockMockTransactions(t, pool)
		started := demoStartScript(
			t, ctx, repoRoot, address, businessOrderNo, expect,
		)
		waiter := us3WaitSingleMockInsertWaiter(t, pool, blockerPID)
		elapsed := us3WaitMockTargetTransactionGone(t, pool, waiter)
		if elapsed < 2*time.Second-250*time.Millisecond || elapsed > 3*time.Second {
			t.Fatalf("T080 unknown fixture did not use the real Mock DB subtimeout: %s", elapsed)
		}
		releaseMock()
		us3RequireLockGone(t, pool, blockerPID, waiter.RelationOID)
		report := demoWaitScript(t, started, expect, unknown)
		us3RequireNoMockRowForOrder(
			t, pool, report.Steps[0].PaymentID, 500*time.Millisecond,
		)
		return report
	}

	hold, err := observer.HoldPaymentEntry(1)
	if err != nil {
		t.Fatal("T080 demo payment hold arm failed")
	}
	started := demoStartScript(
		t, ctx, repoRoot, address, businessOrderNo, expect,
	)
	waitCtx, cancelWait := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancelWait()
	binding, err := hold.Wait(waitCtx)
	if err != nil {
		t.Fatal("T080 demo payment hold wait failed")
	}
	if binding.Generation != 1 || binding.OrderID == "" ||
		payment.ValidateUUIDv4(binding.OrderID) != nil {
		t.Fatal("T080 demo payment binding mismatch")
	}
	if err := hold.Release(directive); err != nil {
		t.Fatal("T080 demo payment hold release failed")
	}
	report := demoWaitScript(t, started, expect, unknown)
	if report.Steps[0].PaymentID != binding.OrderID {
		t.Fatal("T080 demo payment binding did not match the script POST")
	}
	return report
}

type demoCommand struct {
	cmd    *exec.Cmd
	stdout bytes.Buffer
	stderr bytes.Buffer
	waited bool
}

func demoStartScript(
	t *testing.T,
	ctx context.Context,
	repoRoot, address, businessOrderNo, expect string,
) *demoCommand {
	t.Helper()
	args := []string{
		filepath.Join(repoRoot, "scripts", "demo.sh"),
		"--base-url",
		"http://" + address,
		"--business-order-no",
		businessOrderNo,
		"--amount",
		strconv.FormatInt(demoAmount, 10),
		"--expect-result",
		expect,
	}
	return demoStartOwnedCommand(
		t, ctx, "bash", args, repoRoot, demoScriptEnvironment(),
	)
}

func demoStartOwnedCommand(
	t *testing.T,
	ctx context.Context,
	name string,
	args []string,
	dir string,
	env []string,
) *demoCommand {
	t.Helper()
	command := exec.CommandContext(ctx, name, args...)
	command.Dir = dir
	command.Env = env
	command.WaitDelay = 250 * time.Millisecond
	started := &demoCommand{cmd: command}
	command.Stdout = &started.stdout
	command.Stderr = &started.stderr
	if err := command.Start(); err != nil {
		t.Fatal("T080 demo subprocess start failed")
	}
	t.Cleanup(func() { started.cleanup(t) })
	return started
}

func demoScriptEnvironment() []string {
	allowed := []string{"PATH", "HOME", "TMPDIR", "TZ", "LANG", "LC_ALL"}
	env := make([]string, 0, len(allowed)+2)
	for _, name := range allowed {
		if value, ok := os.LookupEnv(name); ok {
			env = append(env, name+"="+value)
		}
	}
	env = append(
		env,
		"PYTHONDONTWRITEBYTECODE=1",
		"PAYMENT_HUB_API_KEY="+us1APIKey,
	)
	return env
}

func (started *demoCommand) wait(t *testing.T) error {
	t.Helper()
	if started == nil || started.cmd == nil {
		t.Fatal("T080 demo subprocess is unavailable")
	}
	if started.waited {
		return nil
	}
	err := started.cmd.Wait()
	started.waited = true
	return err
}

func (started *demoCommand) cleanup(t *testing.T) {
	t.Helper()
	if started == nil || started.cmd == nil ||
		started.cmd.Process == nil || started.waited {
		return
	}
	_ = started.cmd.Process.Kill()
	err := started.cmd.Wait()
	started.waited = true
	var exitErr *exec.ExitError
	if err != nil && !errors.Is(err, os.ErrProcessDone) &&
		!errors.As(err, &exitErr) {
		t.Errorf("T080 demo subprocess cleanup failed")
	}
}

func demoWaitScript(
	t *testing.T,
	started *demoCommand,
	expect string,
	unknown bool,
) demoReport {
	t.Helper()
	if err := started.wait(t); err != nil {
		t.Fatalf("T080 demo script failed (exit=%d)", demoExitCode(err))
	}
	if started.stderr.Len() != 0 {
		t.Fatal("T080 demo script wrote unexpected stderr")
	}
	return demoDecodeReport(t, started.stdout.Bytes(), expect, unknown)
}

func demoExitCode(err error) int {
	var exitErr *exec.ExitError
	if errors.As(err, &exitErr) {
		return exitErr.ExitCode()
	}
	return -1
}

func demoDecodeReport(
	t *testing.T, data []byte, expect string, unknown bool,
) demoReport {
	t.Helper()
	if len(data) == 0 || len(data) > 64*1024 {
		t.Fatal("T080 demo report size mismatch")
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	var report demoReport
	if err := decoder.Decode(&report); err != nil {
		t.Fatal("T080 demo report schema mismatch")
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		t.Fatal("T080 demo report has trailing content")
	}
	if report.Schema != demoReportSchema || len(report.Steps) != 5 {
		t.Fatal("T080 demo report identity mismatch")
	}
	steps := []string{"post", "get_by_id", "get_by_business", "replay", "conflict"}
	statuses := []int{201, 200, 200, 200, 409}
	seen := make(map[string]bool, len(report.Steps))
	paymentID := ""
	for index, step := range report.Steps {
		if step.Step != steps[index] || step.Status != statuses[index] {
			t.Fatal("T080 demo report step order or status mismatch")
		}
		if payment.ValidateUUIDv4(step.RequestID) != nil || seen[step.RequestID] {
			t.Fatal("T080 demo report request id mismatch")
		}
		seen[step.RequestID] = true
		if index < 4 {
			if step.Result != expect {
				t.Fatal("T080 demo report result mismatch")
			}
			if payment.ValidateUUIDv4(step.PaymentID) != nil {
				t.Fatal("T080 demo report payment id mismatch")
			}
			if paymentID == "" {
				paymentID = step.PaymentID
			} else if step.PaymentID != paymentID {
				t.Fatal("T080 demo report payment id changed")
			}
		} else if step.PaymentID != "" || step.Result != "" ||
			len(step.IssueCodes) != 0 {
			t.Fatal("T080 demo conflict step carried payment facts")
		}
	}
	if report.TraceComplete != !unknown {
		t.Fatal("T080 demo trace_complete mismatch")
	}
	issueSeen := false
	for _, step := range report.Steps {
		if len(step.IssueCodes) != 0 {
			issueSeen = true
		}
	}
	if issueSeen != unknown {
		t.Fatal("T080 demo issue_codes mismatch")
	}
	for _, forbidden := range []string{
		us1APIKey,
		"Authorization",
		"http://",
		"\"message\"",
		"\"body\"",
		"\"business_order_no\"",
	} {
		if bytes.Contains(data, []byte(forbidden)) {
			t.Fatal("T080 demo report leaked a non-whitelisted value")
		}
	}
	return report
}

func TestDemoScriptEnvironment(t *testing.T) {
	canaries := map[string]string{
		"PAYMENT_HUB_TEST_DATABASE_URL":  "postgres://CANARY_DEMO_DSN",
		"PGPASSWORD":                     "CANARY_DEMO_PG_PASSWORD",
		"PGHOST":                         "CANARY_DEMO_PG_HOST",
		"GITHUB_TOKEN":                   "CANARY_DEMO_GITHUB_TOKEN",
		"DOCKER_HOST":                    "tcp://CANARY_DEMO_DOCKER",
		"DOCKER_CONFIG":                  "CANARY_DEMO_DOCKER_CONFIG",
		"HTTP_PROXY":                     "http://CANARY_DEMO_PROXY",
		"AWS_SECRET_ACCESS_KEY":          "CANARY_DEMO_AWS_SECRET",
		"GOOGLE_APPLICATION_CREDENTIALS": "CANARY_DEMO_GOOGLE",
	}
	for name, value := range canaries {
		t.Setenv(name, value)
	}
	env := demoScriptEnvironment()
	allowed := map[string]bool{
		"PATH":                    true,
		"HOME":                    true,
		"TMPDIR":                  true,
		"TZ":                      true,
		"LANG":                    true,
		"LC_ALL":                  true,
		"PYTHONDONTWRITEBYTECODE": true,
		"PAYMENT_HUB_API_KEY":     true,
	}
	apiKeySeen := false
	for _, entry := range env {
		name, value, ok := strings.Cut(entry, "=")
		if !ok || !allowed[name] {
			t.Fatal("T080 demo environment contains an unexpected key")
		}
		if name == "PAYMENT_HUB_API_KEY" {
			if value != us1APIKey {
				t.Fatal("T080 demo environment API key mismatch")
			}
			apiKeySeen = true
			continue
		}
		for _, canary := range canaries {
			if strings.Contains(value, canary) {
				t.Fatal("T080 demo environment leaked an unrelated canary")
			}
		}
	}
	if !apiKeySeen {
		t.Fatal("T080 demo environment lacks the simulated API key")
	}
}

func TestDemoSubprocessCleanup(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	started := demoStartOwnedCommand(
		t,
		ctx,
		"bash",
		[]string{"-c", "exec sleep 30"},
		t.TempDir(),
		demoScriptEnvironment(),
	)
	cancel()
	started.cleanup(t)
	if !started.waited || started.cmd.ProcessState == nil {
		t.Fatal("T080 demo subprocess was not reaped")
	}
	if started.cmd.ProcessState.Sys() == nil {
		t.Fatal("T080 demo subprocess process state is not reaped")
	}
}

func demoRequireStoredFacts(
	t *testing.T,
	pool *pgxpool.Pool,
	clientID, businessOrderNo string,
	report demoReport,
	expect string,
	unknown bool,
) {
	t.Helper()
	paymentID := report.Steps[0].PaymentID
	order := us1ReadOrderState(t, pool, clientID, businessOrderNo)
	if order.ID != paymentID || order.ClientID != clientID ||
		order.BusinessOrderNo != businessOrderNo ||
		order.Amount != demoAmount || order.Currency != "CNY" ||
		order.Result != expect {
		t.Fatal("T080 stored order fact mismatch")
	}
	if unknown {
		if order.MockTransactionID != "" || order.ConfirmedAt != nil {
			t.Fatal("T080 unknown order unexpectedly has a Mock/confirmed fact")
		}
	} else if order.MockTransactionID == "" || order.ConfirmedAt == nil {
		t.Fatal("T080 known order lacks a Mock/confirmed fact")
	}

	orders, mocks, handling := us1CountBusinessRows(t, pool, clientID)
	wantMocks := 1
	wantHandling := 1
	if unknown {
		wantMocks = 0
		wantHandling = 3
	}
	if orders != 1 || mocks != wantMocks || handling != wantHandling {
		t.Fatalf(
			"T080 business row counts mismatch: orders=%d mocks=%d handling=%d",
			orders,
			mocks,
			handling,
		)
	}
	handlingState := us1ReadHandlingState(t, pool, paymentID)
	if handlingState.Source != "PAYMENT" ||
		handlingState.ResultBefore != "UNKNOWN" ||
		handlingState.ResultAfter != expect {
		t.Fatal("T080 handling record facts mismatch")
	}
	if unknown {
		if handlingState.Decision != "KEEP_UNKNOWN" {
			t.Fatal("T080 unknown handling decision mismatch")
		}
		records := us3ReadRecords(t, pool, paymentID)
		if len(records) != 3 {
			t.Fatalf("T080 unknown handling record count mismatch: %d", len(records))
		}
		queryRecords := 0
		for _, record := range records {
			if record.Source == "QUERY" {
				queryRecords++
			}
		}
		if queryRecords != 2 {
			t.Fatalf("T080 unknown query handling record count mismatch: %d", queryRecords)
		}
		record, found := us3FindRecordByDecision(records, "PAYMENT", "KEEP_UNKNOWN")
		if !found || record.ReasonCode != "MOCK_RESULT_UNCONFIRMED" {
			t.Fatal("T080 unknown handling reason mismatch")
		}
		return
	}
	if handlingState.Decision != "ACCEPT_CONFIRMATION" {
		t.Fatal("T080 known handling decision mismatch")
	}
	mock := us1ReadMockState(t, pool, paymentID)
	if mock.PlatformReferenceID != paymentID ||
		mock.Amount != demoAmount || mock.Currency != "CNY" ||
		mock.Result != expect {
		t.Fatal("T080 Mock transaction facts mismatch")
	}
}
