//go:build integration

package harness

// This root proves only the T022 application lifecycle mechanism: the current
// cmd/payment-hub binary, the launcher-provided isolated PostgreSQL database,
// loopback /readyz, graceful SIGTERM, bad-config startup failure, and a unique
// empty search_path schema without Goose metadata. It does not claim payment
// HTTP, business repository behavior, container shutdown, or full MVP.
//
// The Artifacts root is a temporary tool-self-test directory. Its report is
// read back for assertions and then removed by t020TempDir cleanup; this test
// does not claim long-term report retention. The outer CI launcher retains its
// own safe PG JSON report.

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
)

const (
	t022ClientID = "fake-client-T022_01"
	t022APIKey   = "FAKE-demo_key.9~a+/b=="

	t022BuildBudget    = 30 * time.Second
	t022ReadyBudget    = 20 * time.Second
	t022RequestBudget  = 500 * time.Millisecond
	t022StopBudget     = 30 * time.Second
	t022BadExitBudget  = 10 * time.Second
	t022SchemaBudget   = 5 * time.Second
	t022CleanupBudget  = 5 * time.Second
	t022ApplicationDir = "cmd/payment-hub"
)

// TestApplicationLifecycleIntegration is the single new mandatory root for
// Pass B. Every scenario below runs in this root; none may be skipped.
func TestApplicationLifecycleIntegration(t *testing.T) {
	repoRoot := t022RepoRoot(t)
	runID := t022RunID(t)
	binary := t022BuildApplication(t, repoRoot)

	artifactRoot := t022AbsoluteTempDir(t)
	artifacts, err := BeginArtifacts(ArtifactConfig{
		Root:       artifactRoot,
		SourceRoot: repoRoot,
		RunID:      runID,
	})
	if err != nil {
		t.Fatal("application integration artifacts begin failed")
	}

	t.Run("graceful_readyz_sigterm", func(t *testing.T) {
		t022GracefulScenario(t, artifacts, binary, repoRoot)
	})
	t.Run("missing_identity_and_runtime", func(t *testing.T) {
		t022BadConfigScenarios(t, artifacts, binary, repoRoot)
	})
	t.Run("missing_goose_migrations", func(t *testing.T) {
		t022MissingMigrationScenario(t, artifacts, binary, repoRoot, runID, 4)
	})

	path, finishErr := artifacts.Finish()
	if finishErr != nil {
		t.Fatal("application integration artifacts finish failed")
	}
	report, data := t020ReadReport(t, path)
	if !report.CollectionComplete || len(report.Faults) != 0 {
		t.Fatal("application integration report claims incomplete collection")
	}
	if report.RunID != runID || !report.Source.Unchanged || len(report.Source.Manifest) == 0 {
		t.Fatal("application integration report source identity mismatch")
	}
	if len(report.Processes) != 4 {
		t.Fatal("application integration report process count mismatch")
	}
	processes := make(map[uint64]t020ReportProcess, len(report.Processes))
	for _, process := range report.Processes {
		processes[process.Generation] = process
		if !process.Waited || !process.Reaped || !process.OutputComplete || !process.GroupGone {
			t.Fatal("application integration report process resources are not confirmed")
		}
	}
	first, ok := processes[1]
	if !ok || first.Code != 0 || !first.Canceled || first.Forced {
		t.Fatal("application integration report graceful process mismatch")
	}
	for _, generation := range []uint64{2, 3, 4} {
		process, ok := processes[generation]
		if !ok || process.Code <= 0 || process.Canceled || process.Forced {
			t.Fatal("application integration report bad-config process mismatch")
		}
	}
	if len(report.Logs) == 0 {
		t.Fatal("application integration report retained no application logs")
	}
	foundFirstLog := false
	for _, log := range report.Logs {
		if log.Generation == 1 {
			foundFirstLog = true
			break
		}
	}
	if !foundFirstLog {
		t.Fatal("application integration report retained no graceful application log")
	}
	statuses := make(map[int]bool, len(report.HTTP))
	readyzFact := false
	for _, observation := range report.HTTP {
		if observation.Generation == 1 {
			statuses[observation.Status] = true
			if observation.Status == 200 {
				readyzFact = true
				if observation.Result != "" {
					t.Fatal("application integration readyz observation carried a payment result")
				}
			}
		}
	}
	if !readyzFact || !statuses[404] {
		t.Fatal("application integration report HTTP observations mismatch")
	}
	foundMigrationDiagnostic := false
	for _, log := range report.Logs {
		if log.Generation == 4 && log.Level == "error" && log.Event == "dependency" &&
			log.Stage == "persistence" && log.Reason == "dependency" {
			foundMigrationDiagnostic = true
			break
		}
	}
	if !foundMigrationDiagnostic {
		t.Fatal("application integration missing migration diagnostic event")
	}
	if bytes.Contains(data, []byte(t022APIKey)) ||
		bytes.Contains(data, []byte("Authorization")) ||
		bytes.Contains(data, []byte("postgresql://")) ||
		bytes.Contains(data, []byte("postgres://")) {
		t.Fatal("application integration report leaked credentials or DSN")
	}
}

func t022GracefulScenario(t *testing.T, artifacts *Artifacts, binary, repoRoot string) {
	t.Helper()
	publicDSN := t022DatabaseDSN(t, "")
	readyAddr := t022FreeLoopbackAddr(t)
	readyEnv := t022AppEnv(t, publicDSN, readyAddr, true, true)
	readyProcess := t022StartApplication(t, artifacts, binary, repoRoot, 1, readyEnv, 25*time.Second, 60*time.Second, true)
	t022WaitApplicationReady(t, readyProcess, readyAddr)
	t022RequireApplicationRoute404(t, readyAddr)
	if err := artifacts.RecordHTTP(HTTPObservation{Generation: 1, Status: 200}); err != nil {
		t.Fatal("application integration readyz HTTP record failed")
	}
	if err := artifacts.RecordHTTP(HTTPObservation{Generation: 1, Status: 404}); err != nil {
		t.Fatal("application integration route-miss HTTP record failed")
	}

	stopCtx, stopCancel := context.WithTimeout(context.Background(), t022StopBudget)
	stopExit, stopErr := readyProcess.Stop(stopCtx)
	stopCancel()
	if !errors.Is(stopErr, ErrProcess) {
		t.Fatal("application integration graceful stop did not return controlled ErrProcess")
	}
	if stopExit.Code != 0 || !stopExit.Canceled || stopExit.Forced ||
		!stopExit.Reaped || !stopExit.OutputComplete || !stopExit.GroupGone {
		t.Fatal("application integration graceful stop exit mismatch")
	}
	waitCtx, waitCancel := context.WithTimeout(context.Background(), t022SchemaBudget)
	waitExit, waitErr := readyProcess.Wait(waitCtx)
	waitCancel()
	if waitExit != stopExit || !errors.Is(waitErr, ErrProcess) {
		t.Fatal("application integration stop and wait did not share the terminal exit")
	}
}

func t022BadConfigScenarios(t *testing.T, artifacts *Artifacts, binary, repoRoot string) {
	t.Helper()
	publicDSN := t022DatabaseDSN(t, "")
	missingIdentityAddr := t022FreeLoopbackAddr(t)
	missingIdentityEnv := t022AppEnv(t, publicDSN, missingIdentityAddr, false, true)
	missingIdentity := t022StartApplication(t, artifacts, binary, repoRoot, 2, missingIdentityEnv, 5*time.Second, 15*time.Second, true)
	t022WaitNonZeroExit(t, missingIdentity)
	t022RequireNoHTTP(t, missingIdentityAddr)
	t022RequireLoopbackFree(t, missingIdentityAddr)

	missingRuntimeAddr := t022FreeLoopbackAddr(t)
	missingRuntimeEnv := t022AppEnv(t, publicDSN, missingRuntimeAddr, true, false)
	missingRuntime := t022StartApplication(t, artifacts, binary, repoRoot, 3, missingRuntimeEnv, 5*time.Second, 15*time.Second, true)
	t022WaitNonZeroExit(t, missingRuntime)
	t022RequireNoHTTP(t, missingRuntimeAddr)
	t022RequireLoopbackFree(t, missingRuntimeAddr)
}

func t022RealDirectory(t *testing.T, label, path string) string {
	t.Helper()
	if path == "" || !filepath.IsAbs(path) {
		t.Fatalf("application integration %s is not absolute", label)
	}
	info, err := os.Lstat(path)
	if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		t.Fatalf("application integration %s is not a real directory", label)
	}
	return path
}

func t022IsolatedTempDir(t *testing.T) string {
	t.Helper()
	return t022RealDirectory(t, "TMPDIR", os.TempDir())
}

func t022RepoRoot(t *testing.T) string {
	t.Helper()
	root, err := filepath.Abs("../..")
	if err != nil || !filepath.IsAbs(root) {
		t.Fatal("application integration repository root resolution failed")
	}
	if info, err := os.Stat(filepath.Join(root, "go.mod")); err != nil || !info.Mode().IsRegular() {
		t.Fatal("application integration repository root is unavailable")
	}
	return root
}

func t022RunID(t *testing.T) string {
	t.Helper()
	runID := os.Getenv(testRunIDVariable)
	if !isLowerHex32(runID) {
		t.Fatal("application integration run id fixture is invalid")
	}
	return runID
}

func t022AbsoluteTempDir(t *testing.T) string {
	t.Helper()
	root := t020TempDir(t)
	absolute, err := filepath.Abs(root)
	if err != nil || !filepath.IsAbs(absolute) {
		t.Fatal("application integration temporary artifact root is invalid")
	}
	return absolute
}

func t022BuildApplication(t *testing.T, repoRoot string) string {
	t.Helper()
	if runtime.Version() != "go1.27.3" || runtime.GOOS != "linux" || runtime.GOARCH != "amd64" {
		t.Fatal("application integration runtime is not the approved Go/linux/amd64 build")
	}
	goTool := filepath.Join(runtime.GOROOT(), "bin", "go")
	if !filepath.IsAbs(goTool) {
		t.Fatal("application integration Go tool path is not absolute")
	}
	info, err := os.Stat(goTool)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0o111 == 0 {
		t.Fatal("application integration Go tool is unavailable")
	}
	binary := filepath.Join(t020TempDir(t), "payment-hub")
	buildCtx, cancelBuild := context.WithTimeout(context.Background(), t022BuildBudget)
	defer cancelBuild()
	command := exec.CommandContext(buildCtx, goTool, "build", "-mod=readonly", "-o", binary, "./"+t022ApplicationDir)
	command.Dir = repoRoot
	command.Env = t022BuildEnvironment(t)
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	command.WaitDelay = 3 * time.Second
	command.Cancel = func() error {
		if command.Process == nil || command.Process.Pid <= 1 || command.Process.Pid == syscall.Getpgrp() {
			return os.ErrProcessDone
		}
		err := syscall.Kill(-command.Process.Pid, syscall.SIGKILL)
		if errors.Is(err, syscall.ESRCH) {
			return os.ErrProcessDone
		}
		return err
	}
	command.Stdout = io.Discard
	command.Stderr = io.Discard
	err = command.Run()
	if err != nil {
		if errors.Is(buildCtx.Err(), context.DeadlineExceeded) {
			t.Fatal("application integration build timed out")
		}
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			t.Fatalf("application integration build failed (exit=%d)", exitErr.ExitCode())
		}
		t.Fatal("application integration build could not start")
	}
	info, err = os.Stat(binary)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0o111 == 0 {
		t.Fatal("application integration build output is unavailable")
	}
	return binary
}

func t022BuildEnvironment(t *testing.T) []string {
	t.Helper()
	env := []string{
		"GOENV=off",
		"GOTOOLCHAIN=local",
		"GOWORK=off",
		"GOFLAGS=",
		"GOPROXY=off",
		"GOSUMDB=off",
		"GOPRIVATE=",
		"GONOPROXY=",
		"GONOSUMDB=",
		"CGO_ENABLED=1",
		"GOMAXPROCS=2",
		"TMPDIR=" + t022IsolatedTempDir(t),
	}
	xdgConfigHome := os.Getenv("XDG_CONFIG_HOME")
	if xdgConfigHome == "" {
		t.Fatal("application integration build XDG_CONFIG_HOME is required")
	}
	env = append(env, "XDG_CONFIG_HOME="+t022RealDirectory(t, "XDG_CONFIG_HOME", xdgConfigHome))
	for _, name := range []string{"GOPATH", "GOMODCACHE", "GOCACHE"} {
		value := os.Getenv(name)
		if value == "" || !filepath.IsAbs(value) {
			t.Fatalf("application integration build cache %s must be an absolute path", name)
		}
		env = append(env, name+"="+value)
	}
	for _, name := range []string{"PATH", "HOME"} {
		if value := os.Getenv(name); value != "" {
			env = append(env, name+"="+value)
		}
	}
	return env
}

func t022AppEnv(t *testing.T, dsn, httpAddr string, withIdentity, withDatabase bool) []string {
	t.Helper()
	env := []string{
		"PAYMENT_HUB_TEST_MODE=off",
		"PAYMENT_HUB_HTTP_ADDR=" + httpAddr,
		"TMPDIR=" + t022IsolatedTempDir(t),
		"TZ=UTC",
	}
	if withIdentity {
		env = append(env,
			"PAYMENT_HUB_CLIENT_ID="+t022ClientID,
			"PAYMENT_HUB_API_KEY="+t022APIKey,
		)
	}
	if withDatabase {
		env = append(env, "PAYMENT_HUB_DATABASE_URL="+dsn)
	}
	return env
}

func t022StartApplication(t *testing.T, artifacts *Artifacts, binary, repoRoot string, generation uint64, env []string, grace, lifetimeBudget time.Duration, expectOutputComplete bool) *Process {
	t.Helper()
	lifetime, cancelLifetime := context.WithTimeout(context.Background(), lifetimeBudget)
	process, err := StartProcess(lifetime, ProcessConfig{
		Path:       binary,
		Dir:        repoRoot,
		Args:       nil,
		Env:        env,
		Generation: generation,
		Grace:      grace,
		Artifacts:  artifacts,
	})
	if err != nil {
		cancelLifetime()
		t.Fatalf("application integration process start failed (generation=%d)", generation)
	}
	t.Cleanup(func() {
		cancelLifetime()
		ctx, cancel := context.WithTimeout(context.Background(), t022CleanupBudget)
		defer cancel()
		exit, err := process.Stop(ctx)
		if err != nil && !errors.Is(err, ErrProcess) && !errors.Is(err, ErrArtifacts) {
			t.Errorf("application integration process cleanup returned an unexpected error")
		}
		if !exit.Reaped || !exit.GroupGone {
			t.Errorf("application integration process cleanup did not confirm reaping/group absence")
		}
		if expectOutputComplete && !exit.OutputComplete {
			t.Errorf("application integration process cleanup did not confirm output completion")
		}
	})
	return process
}

func t022FreeLoopbackAddr(t *testing.T) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal("application integration loopback port selection failed")
	}
	addr := listener.Addr().String()
	if err := listener.Close(); err != nil {
		t.Fatal("application integration loopback port release failed")
	}
	host, port, err := net.SplitHostPort(addr)
	if err != nil || host != "127.0.0.1" || port == "0" {
		t.Fatal("application integration loopback address is invalid")
	}
	return addr
}

func t022WaitApplicationReady(t *testing.T, process *Process, addr string) {
	t.Helper()
	overall, cancelOverall := context.WithTimeout(context.Background(), t022ReadyBudget)
	defer cancelOverall()
	readyURL := "http://" + addr + "/readyz"
	for {
		exit, _ := process.snapshotForReport()
		if exit.Reaped {
			t.Fatal("application integration process exited before readiness")
		}
		requestCtx, cancelRequest := context.WithTimeout(overall, t022RequestBudget)
		response, body, err := t022HTTPGet(requestCtx, readyURL)
		cancelRequest()
		if err == nil && response.StatusCode == 200 {
			if string(body) != `{"status":"ready"}` {
				t.Fatal("application integration readiness body mismatch")
			}
			if !strings.HasPrefix(response.Header.Get("Content-Type"), "application/json") {
				t.Fatal("application integration readiness content type mismatch")
			}
			if !strings.Contains(response.Header.Get("Cache-Control"), "no-store") {
				t.Fatal("application integration readiness cache policy mismatch")
			}
			return
		}
		if overall.Err() != nil {
			t.Fatal("application integration readiness did not complete within budget")
		}
		time.Sleep(25 * time.Millisecond)
	}
}

func t022RequireApplicationRoute404(t *testing.T, addr string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), t022SchemaBudget)
	defer cancel()
	response, _, err := t022HTTPGet(ctx, "http://"+addr+"/not-a-registered-route")
	if err != nil || response.StatusCode != 404 {
		t.Fatal("application integration non-ready route mismatch")
	}
}

func t022HTTPGet(ctx context.Context, target string) (*http.Response, []byte, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return nil, nil, err
	}
	client := &http.Client{
		Transport: &http.Transport{DisableKeepAlives: true},
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	response, err := client.Do(request)
	if err != nil {
		return nil, nil, err
	}
	body, readErr := io.ReadAll(io.LimitReader(response.Body, 4096))
	closeErr := response.Body.Close()
	if readErr != nil {
		return response, nil, readErr
	}
	if closeErr != nil {
		return response, nil, closeErr
	}
	return response, body, nil
}

func t022WaitNonZeroExit(t *testing.T, process *Process) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), t022BadExitBudget)
	defer cancel()
	exit, err := process.Wait(ctx)
	if !errors.Is(err, ErrProcess) {
		t.Fatal("application integration bad-config child did not exit nonzero")
	}
	if exit.Code <= 0 || exit.Canceled || exit.Forced ||
		!exit.Reaped || !exit.OutputComplete || !exit.GroupGone {
		t.Fatal("application integration bad-config child exit mismatch")
	}
}

func t022RequireNoHTTP(t *testing.T, addr string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), t022RequestBudget)
	defer cancel()
	response, _, _ := t022HTTPGet(ctx, "http://"+addr+"/readyz")
	if response != nil {
		t.Fatal("application integration bad-config child served HTTP")
	}
}

func t022RequireLoopbackFree(t *testing.T, addr string) {
	t.Helper()
	listener, err := net.Listen("tcp", addr)
	if err != nil {
		t.Fatal("application integration loopback port remained held after a negative child (fixture)")
	}
	if err := listener.Close(); err != nil {
		t.Fatal("application integration loopback port fixture cleanup failed")
	}
}

func t022MissingMigrationScenario(t *testing.T, artifacts *Artifacts, binary, repoRoot, runID string, generation uint64) {
	t.Helper()
	admin := t022OpenAdmin(t)
	before := t022ReadPublicState(t, admin)
	schema, quoted := t022CreateSchema(t, admin, runID)
	t.Cleanup(func() {
		t022DropSchemaIfEmpty(t, schema, quoted)
	})
	dsn := t022DatabaseDSN(t, schema)
	addr := t022FreeLoopbackAddr(t)
	env := t022AppEnv(t, dsn, addr, true, true)
	process := t022StartApplication(t, artifacts, binary, repoRoot, generation, env, 5*time.Second, 15*time.Second, true)
	t022WaitNonZeroExit(t, process)
	t022RequireNoHTTP(t, addr)
	t022RequireLoopbackFree(t, addr)
	if !t022SchemaExists(t, admin, schema) {
		t.Fatal("application integration registered schema disappeared")
	}
	if count := t022SchemaRelationCount(t, admin, schema); count != 0 {
		t.Fatal("application integration auto-created objects in the empty schema")
	}
	after := t022ReadPublicState(t, admin)
	if !slices.Equal(after.applied, []int64{1, 2, 3}) || !slices.Equal(before.applied, after.applied) {
		t.Fatal("application integration public Goose versions changed")
	}
	if before.paymentOrders != after.paymentOrders || before.mockTransactions != after.mockTransactions ||
		before.resultHandlingRecords != after.resultHandlingRecords {
		t.Fatal("application integration public business row counts changed")
	}
}

func t022OpenAdmin(t *testing.T) *pgx.Conn {
	t.Helper()
	config := openPostgresConfig(t)
	ctx, cancel := context.WithTimeout(context.Background(), t022SchemaBudget)
	defer cancel()
	conn, err := pgx.ConnectConfig(ctx, config)
	if err != nil {
		t.Fatalf("application integration database connection failed (%s)", postgresErrorCategory(err))
	}
	t.Cleanup(func() {
		closeCtx, closeCancel := context.WithTimeout(context.Background(), t022CleanupBudget)
		defer closeCancel()
		if err := conn.Close(closeCtx); err != nil {
			t.Errorf("application integration admin connection close failed (%s)", postgresErrorCategory(err))
		}
	})
	return conn
}

type t022PublicState struct {
	applied               []int64
	paymentOrders         int64
	mockTransactions      int64
	resultHandlingRecords int64
}

func t022ReadPublicState(t *testing.T, conn *pgx.Conn) t022PublicState {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), t022SchemaBudget)
	defer cancel()
	rows, err := conn.Query(ctx, `SELECT version_id, is_applied FROM goose_db_version ORDER BY version_id`)
	if err != nil {
		t.Fatalf("application integration Goose state query failed (%s)", postgresErrorCategory(err))
	}
	state := t022PublicState{}
	for rows.Next() {
		var version int64
		var applied bool
		if err := rows.Scan(&version, &applied); err != nil {
			rows.Close()
			t.Fatal("application integration Goose state scan failed")
		}
		if applied && version > 0 {
			state.applied = append(state.applied, version)
		}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		t.Fatalf("application integration Goose state iteration failed (%s)", postgresErrorCategory(err))
	}
	rows.Close()
	if err := conn.QueryRow(ctx, `SELECT count(*) FROM payment_orders`).Scan(&state.paymentOrders); err != nil {
		t.Fatalf("application integration payment row count failed (%s)", postgresErrorCategory(err))
	}
	if err := conn.QueryRow(ctx, `SELECT count(*) FROM mock_transactions`).Scan(&state.mockTransactions); err != nil {
		t.Fatalf("application integration mock row count failed (%s)", postgresErrorCategory(err))
	}
	if err := conn.QueryRow(ctx, `SELECT count(*) FROM result_handling_records`).Scan(&state.resultHandlingRecords); err != nil {
		t.Fatalf("application integration result-handling row count failed (%s)", postgresErrorCategory(err))
	}
	return state
}

func t022CreateSchema(t *testing.T, conn *pgx.Conn, runID string) (string, string) {
	t.Helper()
	schema := fmt.Sprintf("t022_%s_%d", runID[:8], time.Now().UnixNano())
	if len(schema) > 63 {
		schema = schema[:63]
	}
	quoted := pgx.Identifier{schema}.Sanitize()
	ctx, cancel := context.WithTimeout(context.Background(), t022SchemaBudget)
	defer cancel()
	if _, err := conn.Exec(ctx, "CREATE SCHEMA "+quoted); err != nil {
		t.Fatalf("application integration schema creation failed (%s)", postgresErrorCategory(err))
	}
	return schema, quoted
}

func t022SchemaExists(t *testing.T, conn *pgx.Conn, schema string) bool {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), t022SchemaBudget)
	defer cancel()
	var exists bool
	err := conn.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = $1)`, schema).Scan(&exists)
	if err != nil {
		t.Fatalf("application integration schema existence failed (%s)", postgresErrorCategory(err))
	}
	return exists
}

func t022SchemaRelationCount(t *testing.T, conn *pgx.Conn, schema string) int64 {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), t022SchemaBudget)
	defer cancel()
	var count int64
	err := conn.QueryRow(ctx, `
		SELECT count(*)
		FROM pg_class c
		JOIN pg_namespace n ON n.oid = c.relnamespace
		WHERE n.nspname = $1
	`, schema).Scan(&count)
	if err != nil {
		t.Fatalf("application integration schema inspection failed (%s)", postgresErrorCategory(err))
	}
	return count
}

func t022DropSchemaIfEmpty(t *testing.T, schema, quoted string) {
	t.Helper()
	config := openPostgresConfig(t)
	ctx, cancel := context.WithTimeout(context.Background(), t022CleanupBudget)
	defer cancel()
	conn, err := pgx.ConnectConfig(ctx, config)
	if err != nil {
		t.Errorf("application integration schema cleanup connection failed (%s)", postgresErrorCategory(err))
		return
	}
	defer func() {
		closeCtx, closeCancel := context.WithTimeout(context.Background(), t022CleanupBudget)
		defer closeCancel()
		if err := conn.Close(closeCtx); err != nil {
			t.Errorf("application integration schema cleanup close failed (%s)", postgresErrorCategory(err))
		}
	}()
	var count int64
	err = conn.QueryRow(ctx, `
		SELECT count(*)
		FROM pg_class c
		JOIN pg_namespace n ON n.oid = c.relnamespace
		WHERE n.nspname = $1
	`, schema).Scan(&count)
	if err != nil {
		t.Errorf("application integration schema cleanup inspection failed (%s)", postgresErrorCategory(err))
		return
	}
	if count != 0 {
		t.Errorf("application integration schema cleanup left a non-empty registered schema to the PG instance")
		return
	}
	if _, err := conn.Exec(ctx, "DROP SCHEMA "+quoted); err != nil {
		t.Errorf("application integration schema cleanup drop failed (%s)", postgresErrorCategory(err))
		return
	}
	verifyCtx, verifyCancel := context.WithTimeout(context.Background(), t022CleanupBudget)
	defer verifyCancel()
	var exists bool
	if err := conn.QueryRow(verifyCtx, `SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = $1)`, schema).Scan(&exists); err != nil {
		t.Errorf("application integration schema cleanup existence check failed (%s)", postgresErrorCategory(err))
		return
	}
	if exists {
		t.Errorf("application integration schema cleanup schema still exists after drop")
	}
}

func t022DatabaseDSN(t *testing.T, schema string) string {
	t.Helper()
	config := openPostgresConfig(t)
	target := url.URL{
		Scheme: "postgres",
		User:   url.UserPassword(config.User, config.Password),
		Host:   net.JoinHostPort(config.Host, strconv.Itoa(int(config.Port))),
		Path:   "/" + config.Database,
	}
	query := url.Values{}
	query.Set("sslmode", "disable")
	if schema != "" {
		query.Set("search_path", schema)
	}
	target.RawQuery = query.Encode()
	return target.String()
}
