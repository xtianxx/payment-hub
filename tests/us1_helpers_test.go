//go:build integration

package tests

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"

	"payment-hub/internal/payment"
	"payment-hub/internal/testcontrol"
	"payment-hub/tests/harness"
)

const (
	us1APIKey = "FAKE-us1_key.9~a+/b=="

	us1BuildBudget    = 60 * time.Second
	us1ReadyBudget    = 20 * time.Second
	us1RequestBudget  = 10 * time.Second
	us1StopBudget     = 30 * time.Second
	us1CleanupBudget  = 5 * time.Second
	us1BarrierBudget  = 5 * time.Second
	us1ApplicationDir = "cmd/payment-hub"

	us1RunIDEnv = "PAYMENT_HUB_TEST_RUN_ID"
	us1DSNEnv   = "PAYMENT_HUB_TEST_DATABASE_URL"
)

type us1Payment struct {
	PaymentID       string  `json:"payment_id"`
	BusinessOrderNo string  `json:"business_order_no"`
	Amount          int64   `json:"amount"`
	Currency        string  `json:"currency"`
	Result          string  `json:"result"`
	RegisteredAt    string  `json:"registered_at"`
	ConfirmedAt     *string `json:"confirmed_at"`
}

type us1ErrorBody struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

type us1Issue struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

type us1Envelope struct {
	RequestID string        `json:"request_id"`
	Payment   *us1Payment   `json:"payment"`
	Error     *us1ErrorBody `json:"error"`
	Issues    []us1Issue    `json:"issues"`
}

type us1OrderState struct {
	ID                string
	ClientID          string
	BusinessOrderNo   string
	Amount            int64
	Currency          string
	Result            string
	MockTransactionID string
	RegisteredAt      time.Time
	ConfirmedAt       *time.Time
}

type us1MockState struct {
	ID                  string
	PlatformReferenceID string
	Amount              int64
	Currency            string
	Result              string
	ProcessedAt         time.Time
}

type us1HandlingState struct {
	Source              string
	PlatformReferenceID string
	MockTransactionID   string
	Amount              int64
	Currency            string
	Result              string
	ResultBefore        string
	ResultAfter         string
	Decision            string
	ObservedAt          time.Time
}

func us1RunID(t *testing.T) string {
	t.Helper()
	runID := os.Getenv(us1RunIDEnv)
	if !us1LowerHex32(runID) {
		t.Fatal("US1 test run id fixture is invalid")
	}
	return runID
}

func us1Epoch(t *testing.T) string {
	t.Helper()
	epoch := strings.ReplaceAll(payment.NewUUIDv4(), "-", "")
	if !us1LowerHex32(epoch) {
		t.Fatal("US1 observer epoch generation failed")
	}
	return epoch
}

func us1UniqueClientID(t *testing.T) string {
	t.Helper()
	clientID := "us1-" + strings.ReplaceAll(payment.NewUUIDv4(), "-", "")
	if err := payment.ValidateClientID(clientID); err != nil {
		t.Fatal("US1 client id fixture is invalid")
	}
	return clientID
}

func us1UniqueBusinessOrderNo(t *testing.T) string {
	t.Helper()
	orderNo := "M" + strings.ReplaceAll(payment.NewUUIDv4(), "-", "")
	if err := payment.ValidateBusinessOrderNo(orderNo); err != nil {
		t.Fatal("US1 business order number fixture is invalid")
	}
	return orderNo
}

func us1LowerHex32(value string) bool {
	if len(value) != 32 {
		return false
	}
	for i := 0; i < len(value); i++ {
		c := value[i]
		if (c < '0' || c > '9') && (c < 'a' || c > 'f') {
			return false
		}
	}
	return true
}

func us1RepoRoot(t *testing.T) string {
	t.Helper()
	root, err := filepath.Abs("..")
	if err != nil || !filepath.IsAbs(root) {
		t.Fatal("US1 repository root resolution failed")
	}
	info, err := os.Stat(filepath.Join(root, "go.mod"))
	if err != nil || !info.Mode().IsRegular() {
		t.Fatal("US1 repository root is unavailable")
	}
	return root
}

func us1RealDirectory(t *testing.T, label, path string) string {
	t.Helper()
	if path == "" || !filepath.IsAbs(path) {
		t.Fatalf("US1 %s is not absolute", label)
	}
	info, err := os.Lstat(path)
	if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
		t.Fatalf("US1 %s is not a real directory", label)
	}
	return path
}

func us1IsolatedTempDir(t *testing.T) string {
	t.Helper()
	return us1RealDirectory(t, "TMPDIR", os.TempDir())
}

func us1BuildApplication(t *testing.T, repoRoot string) string {
	t.Helper()
	if runtime.Version() != "go1.27.3" || runtime.GOOS != "linux" || runtime.GOARCH != "amd64" {
		t.Fatal("US1 runtime is not the approved Go/linux/amd64 build")
	}
	goTool := filepath.Join(runtime.GOROOT(), "bin", "go")
	if !filepath.IsAbs(goTool) {
		t.Fatal("US1 Go tool path is not absolute")
	}
	info, err := os.Stat(goTool)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0o111 == 0 {
		t.Fatal("US1 Go tool is unavailable")
	}
	binary := filepath.Join(t.TempDir(), "payment-hub")
	buildCtx, cancelBuild := context.WithTimeout(context.Background(), us1BuildBudget)
	defer cancelBuild()
	command := exec.CommandContext(buildCtx, goTool, "build", "-mod=readonly", "-o", binary, "./"+us1ApplicationDir)
	command.Dir = repoRoot
	command.Env = us1BuildEnvironment(t)
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
	if err := command.Run(); err != nil {
		if errors.Is(buildCtx.Err(), context.DeadlineExceeded) {
			t.Fatal("US1 application build timed out")
		}
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			t.Fatalf("US1 application build failed (exit=%d)", exitErr.ExitCode())
		}
		t.Fatal("US1 application build could not start")
	}
	info, err = os.Stat(binary)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0o111 == 0 {
		t.Fatal("US1 application build output is unavailable")
	}
	return binary
}

func us1BuildEnvironment(t *testing.T) []string {
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
		"TMPDIR=" + us1IsolatedTempDir(t),
	}
	xdgConfigHome := os.Getenv("XDG_CONFIG_HOME")
	if xdgConfigHome == "" {
		t.Fatal("US1 build XDG_CONFIG_HOME is required")
	}
	env = append(env, "XDG_CONFIG_HOME="+us1RealDirectory(t, "XDG_CONFIG_HOME", xdgConfigHome))
	for _, name := range []string{"GOPATH", "GOMODCACHE", "GOCACHE"} {
		value := os.Getenv(name)
		if value == "" || !filepath.IsAbs(value) {
			t.Fatalf("US1 build cache %s must be an absolute path", name)
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

func us1FreeLoopbackAddr(t *testing.T) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal("US1 loopback port selection failed")
	}
	addr := listener.Addr().String()
	if err := listener.Close(); err != nil {
		t.Fatal("US1 loopback port release failed")
	}
	host, port, err := net.SplitHostPort(addr)
	if err != nil || host != "127.0.0.1" || port == "0" {
		t.Fatal("US1 loopback address is invalid")
	}
	return addr
}

func us1TestDatabaseURL(t *testing.T) string {
	t.Helper()
	pool := harness.OpenCommittedPostgresPool(t)
	if pool == nil {
		t.Fatal("US1 validated PostgreSQL pool is nil")
	}
	dsn, err := us1BuildAppDSN(pool.Config().ConnConfig)
	if err != nil {
		t.Fatal("US1 app database URL construction failed")
	}
	return dsn
}

func us1BuildAppDSN(conn *pgx.ConnConfig) (string, error) {
	if conn == nil || conn.Host == "" || strings.Contains(conn.Host, ",") ||
		conn.Port == 0 || conn.User == "" || conn.Password == "" ||
		conn.Database == "" || strings.Contains(conn.Database, "/") {
		return "", errors.New("invalid validated PostgreSQL config")
	}
	hostIP := net.ParseIP(conn.Host)
	if hostIP == nil || !hostIP.IsLoopback() {
		return "", errors.New("validated PostgreSQL config is not loopback")
	}
	values := url.Values{}
	values.Set("sslmode", "disable")
	appURL := url.URL{
		Scheme:   "postgres",
		User:     url.UserPassword(conn.User, conn.Password),
		Host:     net.JoinHostPort(conn.Host, strconv.Itoa(int(conn.Port))),
		Path:     "/" + conn.Database,
		RawQuery: values.Encode(),
	}
	return appURL.String(), nil
}

func us1AppEnv(t *testing.T, dsn, httpAddr, clientID, socket, runID, epoch string, generation uint64) []string {
	t.Helper()
	return []string{
		"PAYMENT_HUB_CLIENT_ID=" + clientID,
		"PAYMENT_HUB_API_KEY=" + us1APIKey,
		"PAYMENT_HUB_DATABASE_URL=" + dsn,
		"PAYMENT_HUB_HTTP_ADDR=" + httpAddr,
		"PAYMENT_HUB_TEST_MODE=on",
		"PAYMENT_HUB_TEST_CONTROL_SOCKET=" + socket,
		"PAYMENT_HUB_TEST_RUN_ID=" + runID,
		"PAYMENT_HUB_TEST_OBSERVER_EPOCH=" + epoch,
		"PAYMENT_HUB_TEST_GENERATION=" + strconv.FormatUint(generation, 10),
		"TMPDIR=" + us1IsolatedTempDir(t),
		"TZ=UTC",
	}
}

func us1StartApplication(t *testing.T, artifacts *harness.Artifacts, binary, repoRoot string, generation uint64, env []string, grace, lifetimeBudget time.Duration) *harness.Process {
	t.Helper()
	lifetime, cancelLifetime := context.WithTimeout(context.Background(), lifetimeBudget)
	process, err := harness.StartProcess(lifetime, harness.ProcessConfig{
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
		t.Fatalf("US1 process start failed (generation=%d)", generation)
	}
	if state := us1ScenarioStateFor(artifacts); state != nil {
		state.setStage(us1DiagnosisStageStart)
	}
	t.Cleanup(func() {
		cancelLifetime()
		ctx, cancel := context.WithTimeout(context.Background(), us1CleanupBudget)
		defer cancel()
		exit, err := process.Stop(ctx)
		if state := us1ScenarioStateFor(artifacts); state != nil {
			state.recordProcessExit(exit)
		}
		if err != nil && !errors.Is(err, harness.ErrProcess) && !errors.Is(err, harness.ErrArtifacts) {
			t.Errorf("US1 process cleanup returned an unexpected error")
		}
		if !exit.Reaped || !exit.GroupGone {
			t.Errorf("US1 process cleanup did not confirm reaping/group absence")
		}
	})
	return process
}

func us1StopProcess(t *testing.T, process *harness.Process) harness.Exit {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), us1StopBudget)
	defer cancel()
	exit, err := process.Stop(ctx)
	if !errors.Is(err, harness.ErrProcess) {
		t.Fatal("US1 process stop did not return controlled ErrProcess")
	}
	if exit.Code != 0 || !exit.Canceled || exit.Forced ||
		!exit.Reaped || !exit.OutputComplete || !exit.GroupGone {
		t.Fatal("US1 process graceful stop exit mismatch")
	}
	return exit
}

func us1WaitReady(t *testing.T, addr string) {
	t.Helper()
	overall, cancelOverall := context.WithTimeout(context.Background(), us1ReadyBudget)
	defer cancelOverall()
	readyURL := "http://" + addr + "/readyz"
	for {
		requestCtx, cancelRequest := context.WithTimeout(overall, time.Second)
		response, body, err := us1HTTPGet(requestCtx, readyURL, "")
		cancelRequest()
		if err == nil && response.StatusCode == 200 {
			if string(body) != `{"status":"ready"}` {
				t.Fatal("US1 readiness body mismatch")
			}
			if !strings.HasPrefix(response.Header.Get("Content-Type"), "application/json") {
				t.Fatal("US1 readiness content type mismatch")
			}
			if !strings.Contains(response.Header.Get("Cache-Control"), "no-store") {
				t.Fatal("US1 readiness cache policy mismatch")
			}
			return
		}
		if overall.Err() != nil {
			t.Fatal("US1 readiness did not complete within budget")
		}
		time.Sleep(25 * time.Millisecond)
	}
}

func us1ObserverSocket(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "us1-obs-")
	if err != nil {
		t.Fatal("US1 observer directory creation failed")
	}
	if err := os.Chmod(dir, 0o700); err != nil {
		_ = os.RemoveAll(dir)
		t.Fatal("US1 observer directory permissions failed")
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	return filepath.Join(dir, "s")
}

func us1StartObserver(t *testing.T, runID, epoch, socket string) *harness.Observer {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), us1BarrierBudget)
	defer cancel()
	observer, err := harness.StartObserver(ctx, harness.ObserverConfig{
		Socket:         socket,
		RunID:          runID,
		Epoch:          epoch,
		BarrierTimeout: us1BarrierBudget,
	})
	if err != nil {
		t.Fatal("US1 observer start failed")
	}
	if observer == nil {
		t.Fatal("US1 observer start returned nil")
	}
	t.Cleanup(func() { us1CloseObserver(t, observer) })
	return observer
}

func us1CloseObserver(t *testing.T, observer *harness.Observer) {
	t.Helper()
	if observer == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), us1CleanupBudget)
	defer cancel()
	if err := observer.Close(ctx); err != nil {
		t.Errorf("US1 observer close failed")
	}
}

func us1BeginArtifacts(t *testing.T, repoRoot, runID, scenario string) *harness.Artifacts {
	t.Helper()
	root := us1ReportScenarioRoot(t, runID, scenario)
	artifacts, err := harness.BeginArtifacts(harness.ArtifactConfig{
		Root:       root,
		SourceRoot: repoRoot,
		RunID:      runID,
	})
	if err != nil {
		t.Fatal("US1 artifacts begin failed")
	}
	state := us1RegisterScenarioState(artifacts, scenario, root)
	t.Cleanup(func() { _ = state.finishFailure(t) })
	return artifacts
}

func us1FinishArtifacts(t *testing.T, artifacts *harness.Artifacts) {
	t.Helper()
	if state := us1ScenarioStateFor(artifacts); state != nil {
		if err := state.finishSuccess(t); err != nil {
			t.Fatal("US1 artifacts finish failed")
		}
		return
	}
	path, err := artifacts.Finish()
	if err != nil {
		t.Fatal("US1 artifacts finish failed")
	}
	if path == "" {
		t.Fatal("US1 artifacts finish returned an empty path")
	}
}

func us1HTTPClient() *http.Client {
	return &http.Client{
		Transport: &http.Transport{DisableKeepAlives: true},
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
}

func us1Do(request *http.Request) (*http.Response, []byte, error) {
	client := us1HTTPClient()
	response, err := client.Do(request)
	if err != nil {
		return nil, nil, err
	}
	body, readErr := io.ReadAll(io.LimitReader(response.Body, 65536))
	closeErr := response.Body.Close()
	if readErr != nil {
		return response, nil, readErr
	}
	if closeErr != nil {
		return response, nil, closeErr
	}
	return response, body, nil
}

func us1HTTPGet(ctx context.Context, target, apiKey string) (*http.Response, []byte, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return nil, nil, err
	}
	if apiKey != "" {
		request.Header.Set("Authorization", "Bearer "+apiKey)
	}
	return us1Do(request)
}

func us1PostPayment(ctx context.Context, addr, apiKey, businessOrderNo string, amount int64, currency string) (*http.Response, []byte, error) {
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
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://"+addr+"/api/v1/payments", bytes.NewReader(payload))
	if err != nil {
		return nil, nil, err
	}
	request.Header.Set("Content-Type", "application/json")
	if apiKey != "" {
		request.Header.Set("Authorization", "Bearer "+apiKey)
	}
	return us1Do(request)
}

func us1GetByID(ctx context.Context, addr, apiKey, paymentID string) (*http.Response, []byte, error) {
	return us1HTTPGet(ctx, "http://"+addr+"/api/v1/payments/"+url.PathEscape(paymentID), apiKey)
}

func us1GetByBusinessOrderNo(ctx context.Context, addr, apiKey, businessOrderNo string) (*http.Response, []byte, error) {
	query := url.Values{}
	query.Set("business_order_no", businessOrderNo)
	return us1HTTPGet(ctx, "http://"+addr+"/api/v1/payments?"+query.Encode(), apiKey)
}

func us1ParseEnvelope(t *testing.T, body []byte) us1Envelope {
	t.Helper()
	var envelope us1Envelope
	if err := json.Unmarshal(body, &envelope); err != nil {
		t.Fatal("US1 response JSON parse failed")
	}
	return envelope
}

func us1RequirePaymentResponse(t *testing.T, response *http.Response, body []byte, wantStatus int, wantPaymentID, wantBusinessOrderNo string, wantAmount int64, wantCurrency string, wantResult payment.Result) (us1Payment, string) {
	t.Helper()
	if response == nil {
		t.Fatal("US1 payment response is nil")
	}
	if response.StatusCode != wantStatus {
		t.Fatalf("US1 payment response status mismatch: got %d want %d", response.StatusCode, wantStatus)
	}
	if !strings.HasPrefix(response.Header.Get("Content-Type"), "application/json") {
		t.Fatal("US1 payment response content type mismatch")
	}
	if !strings.Contains(response.Header.Get("Cache-Control"), "no-store") {
		t.Fatal("US1 payment response cache policy mismatch")
	}
	envelope := us1ParseEnvelope(t, body)
	if envelope.RequestID == "" {
		t.Fatal("US1 payment response lacks request_id")
	}
	if err := payment.ValidateUUIDv4(envelope.RequestID); err != nil {
		t.Fatal("US1 payment response request_id is not UUIDv4")
	}
	if envelope.Payment == nil || envelope.Error != nil {
		t.Fatal("US1 payment response envelope mismatch")
	}
	if len(envelope.Issues) != 0 {
		t.Fatal("US1 payment response unexpectedly carried issues")
	}
	paymentBody := *envelope.Payment
	if err := payment.ValidateUUIDv4(paymentBody.PaymentID); err != nil {
		t.Fatal("US1 payment_id is not UUIDv4")
	}
	if paymentBody.PaymentID != wantPaymentID {
		t.Fatal("US1 payment_id mismatch")
	}
	if paymentBody.BusinessOrderNo != wantBusinessOrderNo {
		t.Fatal("US1 business_order_no mismatch")
	}
	if paymentBody.Amount != wantAmount {
		t.Fatal("US1 amount mismatch")
	}
	if paymentBody.Currency != wantCurrency {
		t.Fatal("US1 currency mismatch")
	}
	if paymentBody.Result != string(wantResult) {
		t.Fatalf("US1 payment result mismatch: got %s want %s", paymentBody.Result, wantResult)
	}
	us1RequireRFC3339Z(t, paymentBody.RegisteredAt)
	if wantResult == payment.ResultSucceeded || wantResult == payment.ResultFailed {
		if paymentBody.ConfirmedAt == nil {
			t.Fatal("US1 confirmed payment lacks confirmed_at")
		}
		us1RequireRFC3339Z(t, *paymentBody.ConfirmedAt)
	} else if paymentBody.ConfirmedAt != nil {
		t.Fatal("US1 unknown payment unexpectedly carried confirmed_at")
	}
	if wantStatus == http.StatusCreated {
		wantLocation := "/api/v1/payments/" + paymentBody.PaymentID
		if response.Header.Get("Location") != wantLocation {
			t.Fatal("US1 created payment Location mismatch")
		}
	} else if response.Header.Get("Location") != "" {
		t.Fatal("US1 non-created payment unexpectedly carried Location")
	}
	return paymentBody, envelope.RequestID
}

func us1RequireErrorResponse(t *testing.T, response *http.Response, body []byte, wantStatus int, wantCode string) string {
	t.Helper()
	if response == nil {
		t.Fatal("US1 error response is nil")
	}
	if response.StatusCode != wantStatus {
		t.Fatalf("US1 error response status mismatch: got %d want %d", response.StatusCode, wantStatus)
	}
	if !strings.HasPrefix(response.Header.Get("Content-Type"), "application/json") {
		t.Fatal("US1 error response content type mismatch")
	}
	if !strings.Contains(response.Header.Get("Cache-Control"), "no-store") {
		t.Fatal("US1 error response cache policy mismatch")
	}
	envelope := us1ParseEnvelope(t, body)
	if envelope.RequestID == "" {
		t.Fatal("US1 error response lacks request_id")
	}
	if err := payment.ValidateUUIDv4(envelope.RequestID); err != nil {
		t.Fatal("US1 error response request_id is not UUIDv4")
	}
	if envelope.Payment != nil || envelope.Error == nil {
		t.Fatal("US1 error response envelope mismatch")
	}
	if envelope.Error.Code != wantCode {
		t.Fatalf("US1 error code mismatch: got %s want %s", envelope.Error.Code, wantCode)
	}
	if envelope.Error.Message == "" {
		t.Fatal("US1 error response lacks message")
	}
	if wantStatus == http.StatusUnauthorized {
		if response.Header.Get("WWW-Authenticate") != `Bearer realm="payment-hub"` {
			t.Fatal("US1 unauthorized response challenge mismatch")
		}
	}
	return envelope.RequestID
}

func us1RequireSamePayment(t *testing.T, got, want us1Payment) {
	t.Helper()
	if got.PaymentID != want.PaymentID ||
		got.BusinessOrderNo != want.BusinessOrderNo ||
		got.Amount != want.Amount ||
		got.Currency != want.Currency ||
		got.Result != want.Result ||
		got.RegisteredAt != want.RegisteredAt {
		t.Fatal("US1 payment identity or result changed")
	}
	switch {
	case got.ConfirmedAt == nil && want.ConfirmedAt == nil:
	case got.ConfirmedAt != nil && want.ConfirmedAt != nil && *got.ConfirmedAt == *want.ConfirmedAt:
	default:
		t.Fatal("US1 payment confirmed_at changed")
	}
}

func us1RequireRFC3339Z(t *testing.T, value string) time.Time {
	t.Helper()
	if !strings.HasSuffix(value, "Z") {
		t.Fatal("US1 timestamp is not UTC RFC3339")
	}
	parsed, err := time.Parse(time.RFC3339Nano, value)
	if err != nil {
		t.Fatal("US1 timestamp is not RFC3339")
	}
	return parsed
}

func us1RecordHTTP(t *testing.T, artifacts *harness.Artifacts, facts *us1ReportScenario, generation uint64, status int, requestID, paymentID, result string) {
	t.Helper()
	if err := artifacts.RecordHTTP(harness.HTTPObservation{
		Generation:     generation,
		Status:         status,
		RequestID:      requestID,
		PaymentOrderID: paymentID,
		Result:         result,
	}); err != nil {
		t.Fatal("US1 HTTP artifact record failed")
	}
	if facts != nil {
		facts.addHTTP(status, requestID, paymentID, result)
	}
}

func us1RecordSQL(t *testing.T, artifacts *harness.Artifacts, facts *us1ReportScenario, generation uint64, tableLabel string, rows uint64) {
	t.Helper()
	if !us1ValidSQLTableLabel(tableLabel) {
		us1SetScenarioStage(artifacts, us1DiagnosisStageReport)
		us1SetScenarioReason(artifacts, us1DiagnosisReasonInvalidRecord)
		t.Fatal("US1 SQL artifact record failed")
	}
	// The harness state is the SQLSTATE of the query that already succeeded.
	// Table labels belong only to the typed US1 report facts.
	if err := artifacts.RecordSQL(harness.SQLObservation{
		Generation: generation,
		State:      "00000",
		Rows:       rows,
	}); err != nil {
		us1SetScenarioStage(artifacts, us1DiagnosisStageReport)
		us1SetScenarioReason(artifacts, us1DiagnosisReasonInvalidRecord)
		t.Fatal("US1 SQL artifact record failed")
	}
	if facts != nil {
		facts.addPG(tableLabel, rows)
	}
}

func us1OpenPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	pool := harness.OpenCommittedPostgresPool(t)
	if pool == nil {
		t.Fatal("US1 PostgreSQL pool is nil")
	}
	return pool
}

func us1ReadOrderState(t *testing.T, pool *pgxpool.Pool, clientID, businessOrderNo string) us1OrderState {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	var state us1OrderState
	var confirmedAt pgtype.Timestamptz
	err := pool.QueryRow(ctx, `
		SELECT id::text, client_id, business_order_no, amount, currency, result,
		       COALESCE(mock_transaction_id::text, ''), registered_at, confirmed_at
		FROM payment_orders
		WHERE client_id = $1 AND business_order_no = $2
	`, clientID, businessOrderNo).Scan(
		&state.ID, &state.ClientID, &state.BusinessOrderNo, &state.Amount,
		&state.Currency, &state.Result, &state.MockTransactionID,
		&state.RegisteredAt, &confirmedAt,
	)
	if err != nil {
		t.Fatalf("US1 order read failed (%s)", us1SQLState(err))
	}
	if confirmedAt.Valid {
		value := confirmedAt.Time.UTC()
		state.ConfirmedAt = &value
	}
	return state
}

func us1ReadMockState(t *testing.T, pool *pgxpool.Pool, platformReferenceID string) us1MockState {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	var state us1MockState
	err := pool.QueryRow(ctx, `
		SELECT id::text, platform_reference_id::text, amount, currency, result, processed_at
		FROM mock_transactions
		WHERE platform_reference_id = $1
	`, platformReferenceID).Scan(
		&state.ID, &state.PlatformReferenceID, &state.Amount,
		&state.Currency, &state.Result, &state.ProcessedAt,
	)
	if err != nil {
		t.Fatalf("US1 mock transaction read failed (%s)", us1SQLState(err))
	}
	state.ProcessedAt = state.ProcessedAt.UTC()
	return state
}

func us1ReadHandlingState(t *testing.T, pool *pgxpool.Pool, paymentOrderID string) us1HandlingState {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	var count int
	if err := pool.QueryRow(ctx, `
		SELECT count(*) FROM result_handling_records
		WHERE payment_order_id = $1 AND source = 'PAYMENT'
	`, paymentOrderID).Scan(&count); err != nil {
		t.Fatalf("US1 handling count failed (%s)", us1SQLState(err))
	}
	if count != 1 {
		t.Fatalf("US1 expected exactly one PAYMENT handling record, got %d", count)
	}
	var state us1HandlingState
	err := pool.QueryRow(ctx, `
		SELECT source,
		       COALESCE(evidence->>'platform_reference_id', ''),
		       COALESCE(evidence->>'mock_transaction_id', ''),
		       COALESCE((evidence->>'amount')::bigint, 0),
		       COALESCE(evidence->>'currency', ''),
		       COALESCE(evidence->>'result', ''),
		       result_before, result_after, decision, observed_at
		FROM result_handling_records
		WHERE payment_order_id = $1 AND source = 'PAYMENT'
	`, paymentOrderID).Scan(
		&state.Source, &state.PlatformReferenceID, &state.MockTransactionID,
		&state.Amount, &state.Currency, &state.Result,
		&state.ResultBefore, &state.ResultAfter, &state.Decision, &state.ObservedAt,
	)
	if err != nil {
		t.Fatalf("US1 handling read failed (%s)", us1SQLState(err))
	}
	state.ObservedAt = state.ObservedAt.UTC()
	return state
}

func us1CountBusinessRows(t *testing.T, pool *pgxpool.Pool, clientID string) (orders, mocks, handling int) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM payment_orders WHERE client_id = $1`, clientID).Scan(&orders); err != nil {
		t.Fatalf("US1 order count failed (%s)", us1SQLState(err))
	}
	if err := pool.QueryRow(ctx, `
		SELECT count(*) FROM mock_transactions
		WHERE platform_reference_id IN (SELECT id FROM payment_orders WHERE client_id = $1)
	`, clientID).Scan(&mocks); err != nil {
		t.Fatalf("US1 mock count failed (%s)", us1SQLState(err))
	}
	if err := pool.QueryRow(ctx, `
		SELECT count(*) FROM result_handling_records
		WHERE payment_order_id IN (SELECT id FROM payment_orders WHERE client_id = $1)
	`, clientID).Scan(&handling); err != nil {
		t.Fatalf("US1 handling count failed (%s)", us1SQLState(err))
	}
	return orders, mocks, handling
}

func us1RequireNoBusinessRows(t *testing.T, pool *pgxpool.Pool, clientID string) {
	t.Helper()
	orders, mocks, handling := us1CountBusinessRows(t, pool, clientID)
	if orders != 0 || mocks != 0 || handling != 0 {
		t.Fatalf("US1 rejected request left rows: orders=%d mock=%d handling=%d", orders, mocks, handling)
	}
}

func us1CleanupClient(t *testing.T, pool *pgxpool.Pool, clientID string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), us1CleanupBudget)
	defer cancel()
	if _, err := pool.Exec(ctx, `
		DELETE FROM result_handling_records
		WHERE payment_order_id IN (SELECT id FROM payment_orders WHERE client_id = $1)
	`, clientID); err != nil {
		t.Errorf("US1 cleanup handling failed (%s)", us1SQLState(err))
	}
	if _, err := pool.Exec(ctx, `
		DELETE FROM mock_transactions
		WHERE platform_reference_id IN (SELECT id FROM payment_orders WHERE client_id = $1)
	`, clientID); err != nil {
		t.Errorf("US1 cleanup mock failed (%s)", us1SQLState(err))
	}
	if _, err := pool.Exec(ctx, `DELETE FROM payment_orders WHERE client_id = $1`, clientID); err != nil {
		t.Errorf("US1 cleanup order failed (%s)", us1SQLState(err))
	}
}

func us1CleanupBusiness(t *testing.T, pool *pgxpool.Pool, clientID, businessOrderNo string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), us1CleanupBudget)
	defer cancel()
	if _, err := pool.Exec(ctx, `
		DELETE FROM result_handling_records
		WHERE payment_order_id IN (
			SELECT id FROM payment_orders WHERE client_id = $1 AND business_order_no = $2
		)
	`, clientID, businessOrderNo); err != nil {
		t.Errorf("US1 cleanup handling failed (%s)", us1SQLState(err))
	}
	if _, err := pool.Exec(ctx, `
		DELETE FROM mock_transactions
		WHERE platform_reference_id IN (
			SELECT id FROM payment_orders WHERE client_id = $1 AND business_order_no = $2
		)
	`, clientID, businessOrderNo); err != nil {
		t.Errorf("US1 cleanup mock failed (%s)", us1SQLState(err))
	}
	if _, err := pool.Exec(ctx, `
		DELETE FROM payment_orders WHERE client_id = $1 AND business_order_no = $2
	`, clientID, businessOrderNo); err != nil {
		t.Errorf("US1 cleanup order failed (%s)", us1SQLState(err))
	}
}

func us1SQLState(err error) string {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code
	}
	if errors.Is(err, pgx.ErrNoRows) {
		return "no rows"
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return "timeout"
	}
	if errors.Is(err, context.Canceled) {
		return "canceled"
	}
	return "none"
}

func us1ResultForDirective(directive testcontrol.Directive) payment.Result {
	switch directive {
	case testcontrol.DirectiveSucceeded:
		return payment.ResultSucceeded
	case testcontrol.DirectiveFailed:
		return payment.ResultFailed
	default:
		return ""
	}
}

func us1PostBytes(ctx context.Context, addr, apiKey string, body []byte, contentType string) (*http.Response, []byte, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://"+addr+"/api/v1/payments", bytes.NewReader(body))
	if err != nil {
		return nil, nil, err
	}
	if contentType != "" {
		request.Header.Set("Content-Type", contentType)
	}
	if apiKey != "" {
		request.Header.Set("Authorization", "Bearer "+apiKey)
	}
	return us1Do(request)
}

func us1PaddedPostBody(size int) []byte {
	base := []byte(`{"business_order_no":"M100","amount":100,"currency":"CNY"}`)
	if size < len(base) {
		return nil
	}
	body := make([]byte, size)
	copy(body, base)
	for i := len(base); i < size; i++ {
		body[i] = ' '
	}
	return body
}

func us1SubmitWithHold(t *testing.T, artifacts *harness.Artifacts, facts *us1ReportScenario, observer *harness.Observer, binary, repoRoot, dsn, runID, epoch, socket, clientID, businessOrderNo string, generation uint64, directive testcontrol.Directive, wantResult payment.Result, amount int64) (*harness.Process, string, harness.PaymentBinding, us1Payment, string) {
	t.Helper()
	addr := us1FreeLoopbackAddr(t)
	env := us1AppEnv(t, dsn, addr, clientID, socket, runID, epoch, generation)
	process := us1StartApplication(t, artifacts, binary, repoRoot, generation, env, 10*time.Second, 60*time.Second)
	us1SetScenarioStage(artifacts, us1DiagnosisStageReady)
	us1WaitReady(t, addr)
	us1SetScenarioStage(artifacts, us1DiagnosisStageHold)

	hold, err := observer.HoldPaymentEntry(generation)
	if err != nil {
		t.Fatal("US1 payment hold arm failed")
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
		response, body, err := us1PostPayment(ctx, addr, us1APIKey, businessOrderNo, amount, "CNY")
		postCh <- postResult{response: response, body: body, err: err}
	}()

	waitStart := time.Now()
	binding, err := hold.Wait(ctx)
	waitElapsed := time.Since(waitStart)
	if err != nil {
		t.Fatal("US1 payment hold wait failed")
	}
	if binding.Generation != generation {
		t.Fatal("US1 payment binding generation mismatch")
	}
	if binding.OrderID == "" {
		t.Fatal("US1 payment binding lacks an order id")
	}
	if err := payment.ValidateUUIDv4(binding.OrderID); err != nil {
		t.Fatal("US1 payment binding order id is not UUIDv4")
	}
	if err := hold.Release(directive); err != nil {
		t.Fatal("US1 payment hold release failed")
	}
	if facts != nil {
		facts.Directive = &us1ReportDirective{
			Result: string(wantResult),
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
	if err := artifacts.RecordBudget(harness.BudgetObservation{
		Generation: generation,
		Stage:      harness.BudgetBarrier,
		Limit:      2 * time.Second,
		Elapsed:    waitElapsed,
	}); err != nil {
		t.Fatal("US1 barrier budget artifact record failed")
	}

	post := <-postCh
	if post.err != nil {
		t.Fatal("US1 payment POST failed")
	}
	paymentBody, requestID := us1RequirePaymentResponse(t, post.response, post.body, http.StatusCreated, binding.OrderID, businessOrderNo, amount, "CNY", wantResult)
	us1RecordHTTP(t, artifacts, facts, generation, post.response.StatusCode, requestID, binding.OrderID, string(paymentBody.Result))
	return process, addr, binding, paymentBody, requestID
}

func us1RequireOrderState(t *testing.T, state us1OrderState, clientID, businessOrderNo, paymentID string, amount int64, currency string, wantResult payment.Result) {
	t.Helper()
	if state.ID != paymentID {
		t.Fatal("US1 stored order id mismatch")
	}
	if state.ClientID != clientID {
		t.Fatal("US1 stored client id mismatch")
	}
	if state.BusinessOrderNo != businessOrderNo {
		t.Fatal("US1 stored business order number mismatch")
	}
	if state.Amount != amount {
		t.Fatal("US1 stored amount mismatch")
	}
	if state.Currency != currency {
		t.Fatal("US1 stored currency mismatch")
	}
	if state.Result != string(wantResult) {
		t.Fatalf("US1 stored result mismatch: got %s want %s", state.Result, wantResult)
	}
	if state.MockTransactionID == "" {
		t.Fatal("US1 stored order lacks mock transaction id")
	}
	if err := payment.ValidateUUIDv4(state.MockTransactionID); err != nil {
		t.Fatal("US1 stored mock transaction id is not UUIDv4")
	}
	if state.RegisteredAt.IsZero() || state.RegisteredAt.Location() != time.UTC {
		t.Fatal("US1 stored registered_at is not UTC")
	}
	if wantResult == payment.ResultSucceeded || wantResult == payment.ResultFailed {
		if state.ConfirmedAt == nil || state.ConfirmedAt.Location() != time.UTC {
			t.Fatal("US1 stored confirmed_at is missing or not UTC")
		}
	} else if state.ConfirmedAt != nil {
		t.Fatal("US1 unknown stored order unexpectedly has confirmed_at")
	}
}

func us1RequireMockState(t *testing.T, state us1MockState, paymentID string, amount int64, currency string, wantResult payment.Result) {
	t.Helper()
	if state.PlatformReferenceID != paymentID {
		t.Fatal("US1 stored mock platform reference mismatch")
	}
	if state.ID == "" || state.ID == state.PlatformReferenceID {
		t.Fatal("US1 stored mock id is not independent")
	}
	if err := payment.ValidateUUIDv4(state.ID); err != nil {
		t.Fatal("US1 stored mock id is not UUIDv4")
	}
	if state.Amount != amount {
		t.Fatal("US1 stored mock amount mismatch")
	}
	if state.Currency != currency {
		t.Fatal("US1 stored mock currency mismatch")
	}
	if state.Result != string(wantResult) {
		t.Fatalf("US1 stored mock result mismatch: got %s want %s", state.Result, wantResult)
	}
	if state.ProcessedAt.IsZero() || state.ProcessedAt.Location() != time.UTC {
		t.Fatal("US1 stored mock processed_at is not UTC")
	}
}

func us1RequireHandlingState(t *testing.T, state us1HandlingState, paymentID, mockID string, amount int64, currency string, wantResult payment.Result) {
	t.Helper()
	if state.Source != "PAYMENT" {
		t.Fatal("US1 stored handling source mismatch")
	}
	if state.PlatformReferenceID != paymentID {
		t.Fatal("US1 stored handling platform reference mismatch")
	}
	if state.MockTransactionID != mockID {
		t.Fatal("US1 stored handling mock transaction id mismatch")
	}
	if state.Amount != amount {
		t.Fatal("US1 stored handling amount mismatch")
	}
	if state.Currency != currency {
		t.Fatal("US1 stored handling currency mismatch")
	}
	if state.Result != string(wantResult) {
		t.Fatalf("US1 stored handling result mismatch: got %s want %s", state.Result, wantResult)
	}
	if state.ResultBefore != "UNKNOWN" {
		t.Fatal("US1 stored handling result_before mismatch")
	}
	if state.ResultAfter != string(wantResult) {
		t.Fatal("US1 stored handling result_after mismatch")
	}
	if state.Decision != "ACCEPT_CONFIRMATION" {
		t.Fatal("US1 stored handling decision mismatch")
	}
	if state.ObservedAt.IsZero() || state.ObservedAt.Location() != time.UTC {
		t.Fatal("US1 stored handling observed_at is not UTC")
	}
}

func us1VerifyStoredFacts(t *testing.T, pool *pgxpool.Pool, artifacts *harness.Artifacts, facts *us1ReportScenario, generation uint64, clientID, businessOrderNo, paymentID string, amount int64, wantResult payment.Result) {
	t.Helper()
	order := us1ReadOrderState(t, pool, clientID, businessOrderNo)
	mock := us1ReadMockState(t, pool, paymentID)
	handling := us1ReadHandlingState(t, pool, paymentID)
	us1RequireOrderState(t, order, clientID, businessOrderNo, paymentID, amount, "CNY", wantResult)
	us1RequireMockState(t, mock, paymentID, amount, "CNY", wantResult)
	us1RequireHandlingState(t, handling, paymentID, mock.ID, amount, "CNY", wantResult)
	orders, mocks, handlingCount := us1CountBusinessRows(t, pool, clientID)
	if orders != 1 || mocks != 1 || handlingCount != 1 {
		t.Fatalf("US1 stored row counts mismatch: orders=%d mock=%d handling=%d", orders, mocks, handlingCount)
	}
	if facts != nil {
		facts.PG = nil
	}
	us1RecordSQL(t, artifacts, facts, generation, "ORDER", uint64(orders))
	us1RecordSQL(t, artifacts, facts, generation, "MOCK", uint64(mocks))
	us1RecordSQL(t, artifacts, facts, generation, "TRACE", uint64(handlingCount))
}

func us1FinalizeScenario(t *testing.T, artifacts *harness.Artifacts, facts *us1ReportScenario, scenarioName string, observer *harness.Observer, process *harness.Process, wantPaymentCount uint64, wantGenerations []uint64) {
	t.Helper()
	us1SetScenarioStage(artifacts, us1DiagnosisStageReport)
	exit := us1StopProcess(t, process)
	if facts != nil {
		facts.addCommand(us1ReportCommandStop, exit.Code, false)
	}
	counts, err := observer.Snapshot()
	if err != nil {
		t.Fatal("US1 observer snapshot failed")
	}
	if counts.Payment != wantPaymentCount {
		t.Fatalf("US1 observer payment count mismatch: got %d want %d", counts.Payment, wantPaymentCount)
	}
	if err := artifacts.RecordObserver(observer); err != nil {
		t.Fatal("US1 observer artifact record failed")
	}
	us1CloseObserver(t, observer)
	final, ok := observer.FinalSnapshot()
	if !ok || !final.Trusted {
		t.Fatal("US1 observer final snapshot is not trusted")
	}
	if final.Counts.Payment != wantPaymentCount {
		t.Fatalf("US1 final payment count mismatch: got %d want %d", final.Counts.Payment, wantPaymentCount)
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
	}
	if len(final.Receipts) != len(wantGenerations) {
		t.Fatalf("US1 final receipt count mismatch: got %d want %d", len(final.Receipts), len(wantGenerations))
	}
	for index, generation := range wantGenerations {
		if final.Receipts[index].Generation != generation {
			t.Fatal("US1 final receipt generation mismatch")
		}
	}
	us1FinishArtifacts(t, artifacts)
	if facts != nil {
		facts.Name = scenarioName
		facts.Result = us1ReportResultPass
		us1ReportRecord(t, facts)
	}
}

const (
	us1ReportSchema       = "payment-hub.us1.v1"
	us1ArtifactRootEnv    = "PAYMENT_HUB_TEST_ARTIFACT_ROOT"
	us1ReportMaxBytes     = 64 * 1024
	us1ReportCommandBuild = "build"
	us1ReportCommandStop  = "process_stop"
	us1ReportCommandProbe = "http_probe"
	us1ReportResultPass   = "PASS"
	us1ReportResultFail   = "FAIL"
	us1ReportResultNotRun = "NOT_RUN"
)

var us1ReportScenarioNames = []string{
	"payment_success",
	"payment_failed",
	"invalid_requests",
	"restart_success",
	"restart_failed",
	"http_boundary",
}

var us1ReportScenarioNameSet = map[string]bool{
	"payment_success":  true,
	"payment_failed":   true,
	"invalid_requests": true,
	"restart_success":  true,
	"restart_failed":   true,
	"http_boundary":    true,
}

type us1Report struct {
	Schema    string              `json:"schema"`
	RunID     string              `json:"run_id"`
	Scenarios []us1ReportScenario `json:"scenarios"`
}

type us1ReportScenario struct {
	Name        string              `json:"name"`
	Result      string              `json:"result"`
	Generations []uint64            `json:"generations"`
	HTTP        []us1ReportHTTP     `json:"http"`
	PG          []us1ReportPG       `json:"pg"`
	Observer    *us1ReportObserver  `json:"observer"`
	Directive   *us1ReportDirective `json:"directive"`
	Commands    []us1ReportCommand  `json:"commands"`
}

type us1ReportHTTP struct {
	Status    int    `json:"status"`
	RequestID string `json:"request_id"`
	PaymentID string `json:"payment_id"`
	Result    string `json:"result"`
}

type us1ReportPG struct {
	State string `json:"state"`
	Rows  uint64 `json:"rows"`
}

type us1ReportObserver struct {
	Trusted  bool               `json:"trusted"`
	Counts   us1ReportCounts    `json:"counts"`
	Receipts []us1ReportReceipt `json:"receipts"`
}

type us1ReportCounts struct {
	Payment uint64 `json:"payment"`
	Query   uint64 `json:"query"`
}

type us1ReportReceipt struct {
	RunID      string `json:"run_id"`
	Epoch      string `json:"epoch"`
	Generation uint64 `json:"generation"`
	LastSeq    uint64 `json:"last_seq"`
}

type us1ReportDirective struct {
	Result  string           `json:"result"`
	Binding us1ReportBinding `json:"binding"`
}

type us1ReportBinding struct {
	RunID      string `json:"run_id"`
	Epoch      string `json:"epoch"`
	Generation uint64 `json:"generation"`
	CallSeq    uint64 `json:"call_seq"`
	EventID    string `json:"event_id"`
	OrderID    string `json:"order_id"`
}

type us1ReportCommand struct {
	Kind   string `json:"kind"`
	Exit   int    `json:"exit"`
	NotRun bool   `json:"not_run"`
}

func (scenario *us1ReportScenario) addHTTP(status int, requestID, paymentID, result string) {
	scenario.HTTP = append(scenario.HTTP, us1ReportHTTP{
		Status:    status,
		RequestID: requestID,
		PaymentID: paymentID,
		Result:    result,
	})
}

func (scenario *us1ReportScenario) addPG(state string, rows uint64) {
	scenario.PG = append(scenario.PG, us1ReportPG{State: state, Rows: rows})
}

func (scenario *us1ReportScenario) addCommand(kind string, exit int, notRun bool) {
	scenario.Commands = append(scenario.Commands, us1ReportCommand{Kind: kind, Exit: exit, NotRun: notRun})
}

type us1ReportCollector struct {
	mu        sync.Mutex
	root      string
	runID     string
	path      string
	scenarios map[string]us1ReportScenario
}

var (
	us1ProcessReportMu        sync.Mutex
	us1ProcessReportCollector *us1ReportCollector
)

func us1ValidateArtifactRoot(path string) error {
	if path == "" || !filepath.IsAbs(path) {
		return errors.New("US1 artifact root is not absolute")
	}
	clean := filepath.Clean(path)
	if clean != path {
		return errors.New("US1 artifact root is not canonical")
	}
	if clean == string(filepath.Separator) {
		return errors.New("US1 artifact root is not a target")
	}
	parts := strings.Split(strings.TrimPrefix(clean, string(filepath.Separator)), string(filepath.Separator))
	current := string(filepath.Separator)
	for index, part := range parts {
		if part == "" {
			continue
		}
		current = filepath.Join(current, part)
		info, err := os.Lstat(current)
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			return errors.New("US1 artifact root contains a symlink")
		}
		if index == len(parts)-1 {
			if !info.IsDir() {
				return errors.New("US1 artifact root is not a directory")
			}
			continue
		}
		if !info.IsDir() {
			return errors.New("US1 artifact root ancestor is not a directory")
		}
	}
	return nil
}

func us1ReportPath(root, runID string) (string, error) {
	if err := us1ValidateArtifactRoot(root); err != nil {
		return "", err
	}
	if !us1LowerHex32(runID) {
		return "", errors.New("US1 report run id is invalid")
	}
	path := filepath.Join(root, runID, "us1.json")
	rel, err := filepath.Rel(root, path)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", errors.New("US1 report path escapes the artifact root")
	}
	return path, nil
}

func us1NewReportCollector(root, runID string) (*us1ReportCollector, error) {
	path, err := us1ReportPath(root, runID)
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
		return nil, errors.New("US1 report already exists")
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	return &us1ReportCollector{
		root:      root,
		runID:     runID,
		path:      path,
		scenarios: make(map[string]us1ReportScenario),
	}, nil
}

func us1ReportInit(t *testing.T, runID string) *us1ReportCollector {
	t.Helper()
	root := os.Getenv(us1ArtifactRootEnv)
	if root == "" {
		t.Fatalf("US1 report artifact root configuration %s is required", us1ArtifactRootEnv)
	}
	us1ProcessReportMu.Lock()
	defer us1ProcessReportMu.Unlock()
	if us1ProcessReportCollector != nil {
		if us1ProcessReportCollector.root != root || us1ProcessReportCollector.runID != runID {
			t.Fatal("US1 report collector identity changed")
		}
		return us1ProcessReportCollector
	}
	collector, err := us1NewReportCollector(root, runID)
	if err != nil {
		t.Fatal("US1 report collector initialization failed")
	}
	us1ProcessReportCollector = collector
	return collector
}

func us1ReportScenarioRoot(t *testing.T, runID, scenario string) string {
	t.Helper()
	if !us1ReportScenarioNameSet[scenario] {
		t.Fatal("US1 report scenario name is invalid")
	}
	collector := us1ReportInit(t, runID)
	root := filepath.Join(collector.root, collector.runID, "scenarios", scenario)
	if err := os.MkdirAll(root, 0o700); err != nil {
		t.Fatal("US1 scenario artifact root creation failed")
	}
	if err := us1ValidateArtifactRoot(root); err != nil {
		t.Fatal("US1 scenario artifact root is unsafe")
	}
	return root
}

func us1ReportRecord(t *testing.T, scenario *us1ReportScenario) {
	t.Helper()
	us1ProcessReportMu.Lock()
	collector := us1ProcessReportCollector
	us1ProcessReportMu.Unlock()
	if collector == nil {
		t.Fatal("US1 report collector is not initialized")
	}
	if err := collector.record(*scenario); err != nil {
		t.Fatal("US1 report scenario record failed")
	}
}

func (collector *us1ReportCollector) record(scenario us1ReportScenario) error {
	if collector == nil {
		return errors.New("nil US1 report collector")
	}
	collector.mu.Lock()
	defer collector.mu.Unlock()
	if _, exists := collector.scenarios[scenario.Name]; exists {
		return errors.New("duplicate US1 report scenario")
	}
	if err := us1ValidateReportScenario(scenario, scenario.Result == us1ReportResultPass); err != nil {
		return err
	}
	if scenario.Directive != nil && scenario.Directive.Binding.RunID != collector.runID {
		return errors.New("US1 directive run id does not match the collector")
	}
	if scenario.Observer != nil {
		for _, receipt := range scenario.Observer.Receipts {
			if receipt.RunID != collector.runID {
				return errors.New("US1 receipt run id does not match the collector")
			}
		}
	}
	collector.scenarios[scenario.Name] = scenario
	return collector.writeLocked()
}

func (collector *us1ReportCollector) writeLocked() error {
	report := us1Report{
		Schema:    us1ReportSchema,
		RunID:     collector.runID,
		Scenarios: make([]us1ReportScenario, 0, len(collector.scenarios)),
	}
	for _, name := range us1ReportScenarioNames {
		if scenario, ok := collector.scenarios[name]; ok {
			report.Scenarios = append(report.Scenarios, scenario)
		}
	}
	encoded, err := json.Marshal(report)
	if err != nil {
		return err
	}
	if len(encoded) > us1ReportMaxBytes {
		return errors.New("US1 report exceeds size bound")
	}
	dir := filepath.Dir(collector.path)
	tmp, err := os.CreateTemp(dir, ".us1-*.tmp")
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

func us1ReadReport(path string) (us1Report, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return us1Report{}, err
	}
	if len(data) == 0 || len(data) > us1ReportMaxBytes {
		return us1Report{}, errors.New("US1 report size is invalid")
	}
	var report us1Report
	if err := json.Unmarshal(data, &report); err != nil {
		return us1Report{}, err
	}
	return report, nil
}

func us1ValidateReport(report us1Report, runID string) error {
	if !us1LowerHex32(runID) || report.Schema != us1ReportSchema || report.RunID != runID {
		return errors.New("US1 report identity mismatch")
	}
	if len(report.Scenarios) != len(us1ReportScenarioNames) {
		return errors.New("US1 report scenario count mismatch")
	}
	seen := make(map[string]bool, len(report.Scenarios))
	for _, scenario := range report.Scenarios {
		if seen[scenario.Name] {
			return errors.New("duplicate US1 report scenario")
		}
		seen[scenario.Name] = true
		if err := us1ValidateReportScenario(scenario, true); err != nil {
			return err
		}
		if scenario.Directive != nil && scenario.Directive.Binding.RunID != runID {
			return errors.New("US1 directive run id does not match the report")
		}
		if scenario.Observer != nil {
			for _, receipt := range scenario.Observer.Receipts {
				if receipt.RunID != runID || !us1LowerHex32(receipt.Epoch) {
					return errors.New("US1 receipt identity does not match the report")
				}
			}
		}
	}
	for _, name := range us1ReportScenarioNames {
		if !seen[name] {
			return errors.New("US1 report is missing a scenario")
		}
	}
	return nil
}

func us1ValidateReportScenario(scenario us1ReportScenario, strict bool) error {
	if !us1ReportScenarioNameSet[scenario.Name] {
		return errors.New("invalid US1 scenario name")
	}
	if scenario.Generations == nil || scenario.HTTP == nil ||
		scenario.PG == nil || scenario.Commands == nil {
		return errors.New("US1 scenario required array is nil")
	}
	switch scenario.Result {
	case us1ReportResultPass, us1ReportResultFail, us1ReportResultNotRun:
	default:
		return errors.New("invalid US1 scenario result")
	}
	if !us1ValidReportGenerations(scenario.Name, scenario.Generations) {
		return errors.New("invalid US1 scenario generations")
	}
	for _, observation := range scenario.HTTP {
		if observation.Status < 100 || observation.Status > 599 {
			return errors.New("invalid US1 HTTP status")
		}
		if observation.RequestID != "" && !us1LowerHexUUIDv4(observation.RequestID) {
			return errors.New("invalid US1 HTTP request id")
		}
		if observation.PaymentID != "" && !us1LowerHexUUIDv4(observation.PaymentID) {
			return errors.New("invalid US1 HTTP payment id")
		}
		if !us1ValidReportResult(observation.Result) {
			return errors.New("invalid US1 HTTP result")
		}
		if strict && scenario.Name != "http_boundary" && observation.RequestID == "" {
			return errors.New("missing US1 HTTP request id")
		}
		if strict && scenario.Name == "http_boundary" && observation.RequestID == "" && observation.Status != 400 {
			return errors.New("empty US1 HTTP request id outside a server reject")
		}
	}
	for _, state := range scenario.PG {
		switch state.State {
		case "ORDER", "MOCK", "TRACE":
		default:
			return errors.New("invalid US1 PG state")
		}
	}
	for _, command := range scenario.Commands {
		switch command.Kind {
		case us1ReportCommandBuild, us1ReportCommandStop, us1ReportCommandProbe:
		default:
			return errors.New("invalid US1 command kind")
		}
		if strict && command.NotRun {
			return errors.New("US1 PASS report contains a not-run command")
		}
	}
	if !strict {
		return nil
	}
	switch scenario.Name {
	case "payment_success", "payment_failed":
		return us1ValidatePaymentScenario(scenario, false)
	case "restart_success", "restart_failed":
		return us1ValidatePaymentScenario(scenario, true)
	case "invalid_requests":
		return us1ValidateInvalidScenario(scenario)
	case "http_boundary":
		return us1ValidateBoundaryScenario(scenario)
	default:
		return errors.New("invalid US1 scenario name")
	}
}

func us1ValidatePaymentScenario(scenario us1ReportScenario, restart bool) error {
	if scenario.Directive == nil || scenario.Observer == nil {
		return errors.New("US1 payment scenario lacks observer/directive")
	}
	if !us1ValidReportResult(scenario.Directive.Result) || scenario.Directive.Result == "" || scenario.Directive.Result == "UNKNOWN" {
		return errors.New("US1 payment directive result is invalid")
	}
	binding := scenario.Directive.Binding
	if !us1LowerHex32(binding.RunID) || !us1LowerHex32(binding.Epoch) ||
		binding.Generation == 0 || binding.Generation != scenario.Generations[0] ||
		binding.CallSeq == 0 ||
		!us1ValidReportEventID(binding.EventID, binding.CallSeq) ||
		!us1LowerHexUUIDv4(binding.OrderID) {
		return errors.New("US1 payment directive binding is invalid")
	}
	if !scenario.Observer.Trusted || scenario.Observer.Counts.Payment != 1 || scenario.Observer.Counts.Query != 0 {
		return errors.New("US1 payment observer counts are invalid")
	}
	wantReceipts := 1
	wantHTTP := 3
	wantCreated := 1
	wantOK := 2
	if restart {
		wantReceipts = 2
		wantHTTP = 5
		wantOK = 4
	}
	if len(scenario.Observer.Receipts) != wantReceipts {
		return errors.New("US1 payment receipt count is invalid")
	}
	for index, receipt := range scenario.Observer.Receipts {
		if receipt.RunID != binding.RunID || receipt.Epoch != binding.Epoch ||
			receipt.Generation != scenario.Generations[index] {
			return errors.New("US1 payment receipt identity is invalid")
		}
		if receipt.Generation == binding.Generation && (receipt.LastSeq == 0 || receipt.LastSeq < binding.CallSeq+1) {
			return errors.New("US1 payment receipt does not cover the directive binding")
		}
	}
	if len(scenario.HTTP) != wantHTTP {
		return errors.New("US1 payment HTTP count is invalid")
	}
	created, ok := 0, 0
	for _, observation := range scenario.HTTP {
		if observation.PaymentID != binding.OrderID {
			return errors.New("US1 payment HTTP id does not match directive")
		}
		if observation.Result != scenario.Directive.Result {
			return errors.New("US1 payment HTTP result does not match directive")
		}
		switch observation.Status {
		case http.StatusCreated:
			created++
		case http.StatusOK:
			ok++
		default:
			return errors.New("US1 payment HTTP status is invalid")
		}
	}
	if created != wantCreated || ok != wantOK {
		return errors.New("US1 payment HTTP status set is invalid")
	}
	if !us1ValidatePGCounts(scenario.PG, 1) {
		return errors.New("US1 payment PG counts are invalid")
	}
	if !us1ValidateCommands(scenario.Commands, restart) {
		return errors.New("US1 payment commands are invalid")
	}
	return nil
}

func us1ValidateInvalidScenario(scenario us1ReportScenario) error {
	if scenario.Directive != nil || scenario.Observer == nil {
		return errors.New("US1 invalid scenario observer/directive is invalid")
	}
	if !scenario.Observer.Trusted || scenario.Observer.Counts.Payment != 0 || scenario.Observer.Counts.Query != 0 {
		return errors.New("US1 invalid observer counts are invalid")
	}
	if len(scenario.Observer.Receipts) != 1 || scenario.Observer.Receipts[0].Generation != 1 ||
		scenario.Observer.Receipts[0].LastSeq != 0 ||
		!us1LowerHex32(scenario.Observer.Receipts[0].RunID) ||
		!us1LowerHex32(scenario.Observer.Receipts[0].Epoch) {
		return errors.New("US1 invalid observer receipt is invalid")
	}
	if len(scenario.HTTP) == 0 {
		return errors.New("US1 invalid scenario lacks HTTP observations")
	}
	for _, observation := range scenario.HTTP {
		if observation.Status < 400 || observation.PaymentID != "" {
			return errors.New("US1 invalid HTTP observation is invalid")
		}
	}
	if !us1ValidatePGCounts(scenario.PG, 0) {
		return errors.New("US1 invalid PG counts are invalid")
	}
	if !us1ValidateCommands(scenario.Commands, false) {
		return errors.New("US1 invalid commands are invalid")
	}
	return nil
}

func us1ValidateBoundaryScenario(scenario us1ReportScenario) error {
	if len(scenario.Generations) != 0 || len(scenario.PG) != 0 ||
		scenario.Observer != nil || scenario.Directive != nil {
		return errors.New("US1 boundary null/empty fields are invalid")
	}
	if len(scenario.HTTP) == 0 {
		return errors.New("US1 boundary scenario lacks HTTP observations")
	}
	statuses := make(map[int]bool, len(scenario.HTTP))
	for _, observation := range scenario.HTTP {
		statuses[observation.Status] = true
	}
	if !statuses[201] || !statuses[400] || !statuses[401] || !statuses[413] || !statuses[415] {
		return errors.New("US1 boundary status set is invalid")
	}
	for _, observation := range scenario.HTTP {
		if observation.Status >= 200 && observation.Status < 300 {
			if observation.PaymentID == "" {
				return errors.New("US1 boundary success lacks payment id")
			}
		} else if observation.PaymentID != "" {
			return errors.New("US1 boundary rejection carried a payment id")
		}
	}
	if len(scenario.Commands) != 1 || scenario.Commands[0].Kind != us1ReportCommandProbe ||
		scenario.Commands[0].Exit != 0 || scenario.Commands[0].NotRun {
		return errors.New("US1 boundary command is invalid")
	}
	return nil
}

func us1ValidatePGCounts(states []us1ReportPG, want uint64) bool {
	if len(states) != 3 {
		return false
	}
	seen := make(map[string]bool, 3)
	for _, state := range states {
		switch state.State {
		case "ORDER", "MOCK", "TRACE":
		default:
			return false
		}
		if seen[state.State] || state.Rows != want {
			return false
		}
		seen[state.State] = true
	}
	return true
}

func us1ValidateCommands(commands []us1ReportCommand, restart bool) bool {
	builds, stops, probes := 0, 0, 0
	for _, command := range commands {
		if command.NotRun || command.Exit != 0 {
			return false
		}
		switch command.Kind {
		case us1ReportCommandBuild:
			builds++
		case us1ReportCommandStop:
			stops++
		case us1ReportCommandProbe:
			probes++
		default:
			return false
		}
	}
	if builds != 1 || probes != 0 {
		return false
	}
	if restart {
		return stops == 2
	}
	return stops == 1
}

func us1ValidReportGenerations(name string, generations []uint64) bool {
	switch name {
	case "payment_success", "payment_failed", "invalid_requests":
		return len(generations) == 1 && generations[0] == 1
	case "restart_success", "restart_failed":
		return len(generations) == 2 && generations[0] == 1 && generations[1] == 2
	case "http_boundary":
		return len(generations) == 0
	default:
		return false
	}
}

func us1ValidReportResult(value string) bool {
	return value == "" || value == "UNKNOWN" || value == "SUCCEEDED" || value == "FAILED"
}

func us1LowerHexUUIDv4(value string) bool {
	if err := payment.ValidateUUIDv4(value); err != nil {
		return false
	}
	return strings.ToLower(value) == value
}

func us1ValidReportEventID(value string, callSeq uint64) bool {
	index := strings.LastIndexByte(value, ':')
	if index != 32 || callSeq == 0 || !us1LowerHex32(value[:index]) {
		return false
	}
	sequence := value[index+1:]
	parsed, err := strconv.ParseUint(sequence, 10, 64)
	if err != nil || parsed != callSeq {
		return false
	}
	return strconv.FormatUint(parsed, 10) == sequence
}

const (
	us1DiagnosisStageBuild  = "build"
	us1DiagnosisStageStart  = "start"
	us1DiagnosisStageReady  = "ready"
	us1DiagnosisStageHold   = "hold"
	us1DiagnosisStageReport = "report"

	us1DiagnosisReasonPending       = "pending"
	us1DiagnosisReasonSuccess       = "success"
	us1DiagnosisReasonTimeout       = "timeout"
	us1DiagnosisReasonEarlyExit     = "early_exit"
	us1DiagnosisReasonConfigShape   = "config_shape"
	us1DiagnosisReasonTransport     = "transport"
	us1DiagnosisReasonInvalidRecord = "invalid_record"
)

type us1DiagnosisProcessExit struct {
	Code           int  `json:"code"`
	Canceled       bool `json:"canceled"`
	Forced         bool `json:"forced"`
	Reaped         bool `json:"reaped"`
	GroupGone      bool `json:"group_gone"`
	OutputComplete bool `json:"output_complete"`
}

type us1ScenarioDiagnosis struct {
	Scenario    string                   `json:"scenario"`
	Stage       string                   `json:"stage"`
	Reason      string                   `json:"reason"`
	ProcessExit *us1DiagnosisProcessExit `json:"process_exit,omitempty"`
}

type us1ScenarioState struct {
	mu            sync.Mutex
	artifacts     *harness.Artifacts
	scenario      string
	root          string
	diagnosisPath string
	stage         string
	reason        string
	processExit   *harness.Exit
	finished      bool
	finishCalls   int
}

var (
	us1ScenarioStatesMu sync.Mutex
	us1ScenarioStates   = make(map[*harness.Artifacts]*us1ScenarioState)
)

func us1RegisterScenarioState(artifacts *harness.Artifacts, scenario, root string) *us1ScenarioState {
	state := &us1ScenarioState{
		artifacts:     artifacts,
		scenario:      scenario,
		root:          root,
		diagnosisPath: filepath.Join(root, "us1-diagnosis.json"),
		stage:         us1DiagnosisStageBuild,
		reason:        us1DiagnosisReasonPending,
	}
	if scenario == "http_boundary" {
		state.stage = us1DiagnosisStageReport
	}
	us1ScenarioStatesMu.Lock()
	us1ScenarioStates[artifacts] = state
	us1ScenarioStatesMu.Unlock()
	return state
}

func us1ScenarioStateFor(artifacts *harness.Artifacts) *us1ScenarioState {
	us1ScenarioStatesMu.Lock()
	defer us1ScenarioStatesMu.Unlock()
	return us1ScenarioStates[artifacts]
}

func us1SetScenarioStage(artifacts *harness.Artifacts, stage string) {
	if state := us1ScenarioStateFor(artifacts); state != nil {
		state.setStage(stage)
	}
}

func us1SetScenarioReason(artifacts *harness.Artifacts, reason string) {
	if state := us1ScenarioStateFor(artifacts); state != nil {
		state.setReason(reason)
	}
}

func (state *us1ScenarioState) setStage(stage string) {
	state.mu.Lock()
	defer state.mu.Unlock()
	if !state.finished {
		state.stage = stage
	}
}

func (state *us1ScenarioState) setReason(reason string) {
	state.mu.Lock()
	defer state.mu.Unlock()
	if !state.finished {
		state.reason = reason
	}
}

func (state *us1ScenarioState) recordProcessExit(exit harness.Exit) {
	state.mu.Lock()
	defer state.mu.Unlock()
	copied := exit
	state.processExit = &copied
}

func (state *us1ScenarioState) finishSuccess(t *testing.T) error {
	return state.finish(t, true)
}

func (state *us1ScenarioState) finishFailure(t *testing.T) error {
	return state.finish(t, false)
}

func (state *us1ScenarioState) finish(t *testing.T, success bool) error {
	state.mu.Lock()
	if state.finished {
		state.mu.Unlock()
		return nil
	}
	state.finished = true
	state.finishCalls++
	if success {
		state.stage = us1DiagnosisStageReport
		state.reason = us1DiagnosisReasonSuccess
	}
	stage := state.stage
	if !us1ValidDiagnosisStage(stage) {
		stage = us1DiagnosisStageReport
	}
	reason := state.reason
	if !us1ValidDiagnosisReason(reason) {
		reason = us1DiagnosisReasonPending
	}
	exit := state.processExit
	state.mu.Unlock()

	latchedFault := false
	if !success {
		// Latch a fixed harness fault without appending a fabricated observation.
		_ = state.artifacts.RecordHTTP(harness.HTTPObservation{Status: 0})
		latchedFault = true
	}
	_, finishErr := state.artifacts.Finish()
	if finishErr != nil && !latchedFault {
		reason = us1DiagnosisReasonInvalidRecord
	}
	state.writeDiagnosis(t, stage, reason, exit)
	return finishErr
}

func (state *us1ScenarioState) writeDiagnosis(t *testing.T, stage, reason string, exit *harness.Exit) {
	diagnosis := us1ScenarioDiagnosis{
		Scenario: state.scenario,
		Stage:    stage,
		Reason:   reason,
	}
	if exit != nil {
		diagnosis.ProcessExit = &us1DiagnosisProcessExit{
			Code:           exit.Code,
			Canceled:       exit.Canceled,
			Forced:         exit.Forced,
			Reaped:         exit.Reaped,
			GroupGone:      exit.GroupGone,
			OutputComplete: exit.OutputComplete,
		}
	}
	encoded, err := json.Marshal(diagnosis)
	if err != nil || len(encoded) > 4096 {
		t.Errorf("US1 diagnosis encoding failed")
		return
	}
	dir := filepath.Dir(state.diagnosisPath)
	tmp, err := os.CreateTemp(dir, ".us1-diagnosis-*.tmp")
	if err != nil {
		t.Errorf("US1 diagnosis file creation failed")
		return
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
		t.Errorf("US1 diagnosis permissions failed")
		return
	}
	if _, err := tmp.Write(encoded); err != nil {
		_ = tmp.Close()
		t.Errorf("US1 diagnosis write failed")
		return
	}
	if err := tmp.Close(); err != nil {
		t.Errorf("US1 diagnosis close failed")
		return
	}
	if err := os.Rename(tmpName, state.diagnosisPath); err != nil {
		t.Errorf("US1 diagnosis rename failed")
		return
	}
	removeTemp = false
}

func us1ValidDiagnosisStage(stage string) bool {
	switch stage {
	case us1DiagnosisStageBuild, us1DiagnosisStageStart, us1DiagnosisStageReady,
		us1DiagnosisStageHold, us1DiagnosisStageReport:
		return true
	default:
		return false
	}
}

func us1ValidDiagnosisReason(reason string) bool {
	switch reason {
	case us1DiagnosisReasonPending, us1DiagnosisReasonSuccess,
		us1DiagnosisReasonTimeout, us1DiagnosisReasonEarlyExit,
		us1DiagnosisReasonConfigShape, us1DiagnosisReasonTransport,
		us1DiagnosisReasonInvalidRecord:
		return true
	default:
		return false
	}
}
