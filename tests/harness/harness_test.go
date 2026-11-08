package harness

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"reflect"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"payment-hub/internal/testcontrol"
)

const (
	t020RunID      = obsTestRunID
	t020OrderID    = "123e4567-e89b-42d3-a456-426614174000"
	t020RequestID  = "223e4567-e89b-42d3-a456-426614174001"
	t020PaymentID  = "323e4567-e89b-42d3-a456-426614174002"
	t020ClientID   = "client-t020"
	t020BusinessNo = "order-t020"
	t020Canary     = "FAKE_T020_CANARY"
	t020SourceMark = "FAKE_T020_SOURCE_CONTENT"

	t020HelperMarker          = "PAYMENT_HUB_T020_HELPER"
	t020HelperMode            = "PAYMENT_HUB_T020_MODE"
	t020HelperReadySocket     = "PAYMENT_HUB_T020_READY_SOCKET"
	t020HelperSocket          = "PAYMENT_HUB_T020_SOCKET"
	t020HelperRunID           = "PAYMENT_HUB_T020_RUN_ID"
	t020HelperEpoch           = "PAYMENT_HUB_T020_EPOCH"
	t020HelperGeneration      = "PAYMENT_HUB_T020_GENERATION"
	t020HelperOrderID         = "PAYMENT_HUB_T020_ORDER_ID"
	t020HelperResultPath      = "PAYMENT_HUB_T020_RESULT"
	t020HelperArgRun          = "-test.run=^TestHarnessHelperProcess$"
	t020HelperModeExit0       = "exit0"
	t020HelperModeExit3       = "exit3"
	t020HelperModeGraceful    = "graceful"
	t020HelperModeIgnore      = "ignore"
	t020HelperModeObserver    = "observer"
	t020HelperModeLogs        = "logs-valid"
	t020HelperModeUnknown     = "logs-unknown"
	t020HelperModeDuplicate   = "logs-duplicate"
	t020HelperModeNull        = "logs-null"
	t020HelperModeWrong       = "logs-wrong-type"
	t020HelperModeTrunc       = "logs-truncated"
	t020HelperModeAggregate   = "logs-aggregate"
	t020HelperModeCanary      = "logs-canary"
	t020HelperModeExtra       = "logs-extra"
	t020HelperModeUTF8        = "logs-invalid-utf8"
	t020HelperModeLogsMax     = "logs-max-line"
	t020HelperModeLogsOver    = "logs-over-line"
	t020HelperModeGroupLeader = "group-leader"
	t020HelperModeGroupMember = "group-member"
)

type t020HelperResult struct {
	OK         bool   `json:"ok"`
	Generation uint64 `json:"generation"`
	LastSeq    uint64 `json:"last_seq"`
	RunID      string `json:"run_id"`
	Epoch      string `json:"epoch"`
}

type t020SourceEntry struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type t020ReportVersions struct {
	Go       string `json:"go"`
	Goos     string `json:"goos"`
	Goarch   string `json:"goarch"`
	Postgres string `json:"postgres"`
	Goose    string `json:"goose"`
}

type t020ReportSource struct {
	Manifest       []t020SourceEntry `json:"manifest"`
	ManifestDigest string            `json:"manifest_digest"`
	Unchanged      bool              `json:"unchanged"`
}

type t020ReportProcess struct {
	Generation     uint64 `json:"generation"`
	Code           int    `json:"code"`
	Signal         int    `json:"signal"`
	Canceled       bool   `json:"canceled"`
	TimedOut       bool   `json:"timed_out"`
	Forced         bool   `json:"forced"`
	Reaped         bool   `json:"reaped"`
	OutputComplete bool   `json:"output_complete"`
	GroupGone      bool   `json:"group_gone"`
	Waited         bool   `json:"waited"`
}

type t020ReportLog struct {
	Time           string `json:"time"`
	Level          string `json:"level"`
	Event          string `json:"event"`
	Stage          string `json:"stage"`
	RequestID      string `json:"request_id"`
	PaymentOrderID string `json:"payment_order_id"`
	ResultBefore   string `json:"result_before"`
	ResultAfter    string `json:"result_after"`
	Reason         string `json:"reason"`
	ErrorKind      string `json:"error_kind"`
	Generation     uint64 `json:"generation"`
	Stream         string `json:"stream"`
}

type t020ReportHTTP struct {
	Generation     uint64 `json:"generation"`
	Status         int    `json:"status"`
	RequestID      string `json:"request_id"`
	PaymentOrderID string `json:"payment_order_id"`
	Result         string `json:"result"`
}

type t020ReportSQL struct {
	Generation uint64 `json:"generation"`
	State      string `json:"state"`
	Rows       uint64 `json:"rows"`
}

type t020ReportBudget struct {
	Generation uint64 `json:"generation"`
	Stage      string `json:"stage"`
	LimitNS    int64  `json:"limit_ns"`
	ElapsedNS  int64  `json:"elapsed_ns"`
}

type t020ReportObserver struct {
	Epoch  string `json:"epoch"`
	Counts struct {
		Payment uint64 `json:"payment"`
		Query   uint64 `json:"query"`
	} `json:"counts"`
	Receipts []struct {
		RunID      string `json:"run_id"`
		Epoch      string `json:"epoch"`
		Generation uint64 `json:"generation"`
		LastSeq    uint64 `json:"last_seq"`
	} `json:"receipts"`
}

type t020Report struct {
	Schema             string              `json:"schema"`
	RunID              string              `json:"run_id"`
	CollectionComplete bool                `json:"collection_complete"`
	Faults             []string            `json:"faults"`
	Versions           t020ReportVersions  `json:"versions"`
	Source             t020ReportSource    `json:"source"`
	Processes          []t020ReportProcess `json:"processes"`
	Logs               []t020ReportLog     `json:"logs"`
	HTTP               []t020ReportHTTP    `json:"http"`
	SQL                []t020ReportSQL     `json:"sql"`
	Budgets            []t020ReportBudget  `json:"budgets"`
	Observer           *t020ReportObserver `json:"observer"`
}

func t020TempDir(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "pht020-")
	if err != nil {
		t.Fatal("create t020 temp directory failed")
	}
	if err := os.Chmod(dir, 0o700); err != nil {
		t.Fatal("secure t020 temp directory failed")
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(dir); err != nil {
			t.Errorf("remove t020 temp directory failed")
		}
	})
	return dir
}

func t020SourceFixture(t *testing.T) string {
	t.Helper()
	root := t020TempDir(t)
	files := map[string]string{
		"go.mod":                      "module fixture\n\ngo 1.27.3\n",
		"go.sum":                      "",
		"cmd/payment-hub/main.go":     "package main\nfunc main() {}\n",
		"internal/app/app.go":         "package app\nconst marker = \"" + t020SourceMark + "\"\n",
		"tests/harness/helper.go":     "package harness\n",
		"migrations/00001_create.sql": "-- fixture\n",
		"deploy/Dockerfile":           "FROM scratch\n",
		"scripts/check.sh":            "#!/bin/sh\nexit 0\n",
		".github/workflows/ci.yml":    "name: fixture\n",
	}
	for relative, content := range files {
		full := filepath.Join(root, filepath.FromSlash(relative))
		if err := os.MkdirAll(filepath.Dir(full), 0o700); err != nil {
			t.Fatal("create t020 source directory failed")
		}
		if err := os.WriteFile(full, []byte(content), 0o600); err != nil {
			t.Fatal("write t020 source fixture failed")
		}
	}
	return root
}

func t020ExpectedManifest(t *testing.T, sourceRoot string) ([]t020SourceEntry, string) {
	t.Helper()
	paths := []string{
		"go.mod",
		"go.sum",
		"cmd/payment-hub/main.go",
		"internal/app/app.go",
		"tests/harness/helper.go",
		"migrations/00001_create.sql",
		"deploy/Dockerfile",
		"scripts/check.sh",
		".github/workflows/ci.yml",
	}
	entries := make([]t020SourceEntry, 0, len(paths))
	for _, relative := range paths {
		data, err := os.ReadFile(filepath.Join(sourceRoot, filepath.FromSlash(relative)))
		if err != nil {
			t.Fatal("read t020 source fixture input failed")
		}
		sum := sha256.Sum256(data)
		entries = append(entries, t020SourceEntry{Path: relative, SHA256: hex.EncodeToString(sum[:])})
	}
	sort.Slice(entries, func(i, j int) bool {
		return entries[i].Path < entries[j].Path
	})
	var digestInput strings.Builder
	for _, entry := range entries {
		digestInput.WriteString(entry.Path)
		digestInput.WriteByte(0)
		digestInput.WriteString(entry.SHA256)
		digestInput.WriteByte(0)
	}
	digest := sha256.Sum256([]byte(digestInput.String()))
	return entries, hex.EncodeToString(digest[:])
}

func t020BeginArtifacts(t *testing.T, sourceRoot string) (*Artifacts, string) {
	t.Helper()
	root := t020TempDir(t)
	return t020BeginArtifactsAt(t, root, sourceRoot, t020RunID)
}

func t020BeginArtifactsAt(t *testing.T, root, sourceRoot, runID string) (*Artifacts, string) {
	t.Helper()
	artifacts, err := BeginArtifacts(ArtifactConfig{
		Root:       root,
		SourceRoot: sourceRoot,
		RunID:      runID,
	})
	if err != nil {
		t.Fatal("begin t020 artifacts failed")
	}
	return artifacts, filepath.Join(root, runID, "runtime-harness.json")
}

func t020HelperPath(t *testing.T) string {
	t.Helper()
	path, err := filepath.Abs(os.Args[0])
	if err != nil {
		t.Fatal("resolve t020 helper path failed")
	}
	return path
}

func t020HelperEnv(mode string, extra ...string) []string {
	env := []string{
		t020HelperMarker + "=1",
		t020HelperMode + "=" + mode,
		"TMPDIR=/tmp/opencode",
		"TZ=UTC",
		"GORACE=atexit_sleep_ms=0",
	}
	return append(env, extra...)
}

func t020StartHelper(t *testing.T, artifacts *Artifacts, generation uint64, mode string, grace time.Duration, extraEnv ...string) (*Process, context.CancelFunc) {
	t.Helper()
	dir := t020TempDir(t)
	lifetime, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	process, err := StartProcess(lifetime, ProcessConfig{
		Path:       t020HelperPath(t),
		Dir:        dir,
		Args:       []string{t020HelperArgRun},
		Env:        t020HelperEnv(mode, extraEnv...),
		Generation: generation,
		Grace:      grace,
		Artifacts:  artifacts,
	})
	if err != nil {
		cancel()
		t.Fatal("start t020 helper process failed")
	}
	t.Cleanup(func() {
		cancel()
		t020StopAndConfirm(t, process)
	})
	return process, cancel
}

func t020StopAndConfirm(t *testing.T, process *Process) {
	t.Helper()
	if process == nil {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	exit, err := process.Stop(ctx)
	if err != nil && !errors.Is(err, ErrProcess) && !errors.Is(err, ErrArtifacts) {
		t.Error("t020 process cleanup returned an unexpected error")
		return
	}
	if !exit.Reaped || !exit.GroupGone {
		t.Error("t020 process cleanup did not confirm reaping/group absence")
	}
}

type t020Ready struct {
	path  string
	ready <-chan struct{}
	err   <-chan error
}

func t020ReadySocket(t *testing.T) *t020Ready {
	t.Helper()
	dir := t020TempDir(t)
	path := filepath.Join(dir, "ready")
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		t.Fatal("create t020 ready socket failed")
	}
	listener.SetUnlinkOnClose(false)
	ready := make(chan struct{})
	errCh := make(chan error, 1)
	done := make(chan struct{})
	var mu sync.Mutex
	var conn net.Conn
	stopping := false
	sendErr := func(err error) {
		select {
		case errCh <- err:
		default:
		}
	}
	isStopping := func() bool {
		mu.Lock()
		defer mu.Unlock()
		return stopping
	}
	go func() {
		defer close(done)
		accepted, err := listener.AcceptUnix()
		if err != nil {
			if !isStopping() {
				sendErr(err)
			}
			return
		}
		mu.Lock()
		if stopping {
			mu.Unlock()
			_ = accepted.Close()
			return
		}
		conn = accepted
		mu.Unlock()
		defer func() {
			mu.Lock()
			if conn == accepted {
				conn = nil
			}
			mu.Unlock()
			_ = accepted.Close()
		}()
		if err := accepted.SetReadDeadline(time.Now().Add(5 * time.Second)); err != nil {
			sendErr(err)
			return
		}
		buffer, err := io.ReadAll(io.LimitReader(accepted, 33))
		if err != nil {
			if !isStopping() {
				sendErr(err)
			}
			return
		}
		if string(buffer) != "ready\n" {
			if !isStopping() {
				sendErr(errors.New("ready protocol mismatch"))
			}
			return
		}
		close(ready)
	}()
	t.Cleanup(func() {
		mu.Lock()
		stopping = true
		if conn != nil {
			_ = conn.Close()
		}
		mu.Unlock()
		_ = listener.Close()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Errorf("t020 ready listener did not stop")
		}
		_ = os.Remove(path)
	})
	return &t020Ready{path: path, ready: ready, err: errCh}
}

func t020AwaitReady(t *testing.T, ready *t020Ready) {
	t.Helper()
	select {
	case <-ready.ready:
	case err := <-ready.err:
		_ = err
		t.Fatal("t020 helper ready fixture failed")
	case <-time.After(5 * time.Second):
		t.Fatal("t020 helper ready handshake timed out")
	}
}

type t020GroupIdentity struct {
	Pid  int
	Pgid int
}

type t020GroupReady struct {
	path     string
	identity <-chan t020GroupIdentity
	err      <-chan error
}

func t020GroupReadySocket(t *testing.T) *t020GroupReady {
	t.Helper()
	dir := t020TempDir(t)
	path := filepath.Join(dir, "ready")
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		t.Fatal("create t020 group ready socket failed")
	}
	listener.SetUnlinkOnClose(false)
	identityCh := make(chan t020GroupIdentity, 1)
	errCh := make(chan error, 1)
	done := make(chan struct{})
	var mu sync.Mutex
	var conn net.Conn
	stopping := false
	sendErr := func(err error) {
		select {
		case errCh <- err:
		default:
		}
	}
	isStopping := func() bool {
		mu.Lock()
		defer mu.Unlock()
		return stopping
	}
	go func() {
		defer close(done)
		accepted, err := listener.AcceptUnix()
		if err != nil {
			if !isStopping() {
				sendErr(err)
			}
			return
		}
		mu.Lock()
		if stopping {
			mu.Unlock()
			_ = accepted.Close()
			return
		}
		conn = accepted
		mu.Unlock()
		defer func() {
			mu.Lock()
			if conn == accepted {
				conn = nil
			}
			mu.Unlock()
			_ = accepted.Close()
		}()
		if err := accepted.SetReadDeadline(time.Now().Add(5 * time.Second)); err != nil {
			sendErr(err)
			return
		}
		buffer, err := io.ReadAll(io.LimitReader(accepted, 129))
		if err != nil {
			if !isStopping() {
				sendErr(err)
			}
			return
		}
		fields := strings.Fields(strings.TrimSpace(string(buffer)))
		if len(buffer) > 128 || len(fields) != 3 || fields[0] != "ready" ||
			!strings.HasPrefix(fields[1], "pid=") || !strings.HasPrefix(fields[2], "pgid=") {
			if !isStopping() {
				sendErr(errors.New("group ready protocol mismatch"))
			}
			return
		}
		pid, pidErr := strconv.Atoi(strings.TrimPrefix(fields[1], "pid="))
		pgid, pgidErr := strconv.Atoi(strings.TrimPrefix(fields[2], "pgid="))
		if pidErr != nil || pgidErr != nil || pid <= 0 || pgid <= 0 {
			if !isStopping() {
				sendErr(errors.New("group ready identity invalid"))
			}
			return
		}
		select {
		case identityCh <- t020GroupIdentity{Pid: pid, Pgid: pgid}:
		default:
		}
	}()
	t.Cleanup(func() {
		mu.Lock()
		stopping = true
		if conn != nil {
			_ = conn.Close()
		}
		mu.Unlock()
		_ = listener.Close()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Errorf("t020 group ready listener did not stop")
		}
		_ = os.Remove(path)
	})
	return &t020GroupReady{path: path, identity: identityCh, err: errCh}
}

func t020AwaitGroupReady(t *testing.T, ready *t020GroupReady) t020GroupIdentity {
	t.Helper()
	select {
	case identity := <-ready.identity:
		return identity
	case err := <-ready.err:
		_ = err
		t.Fatal("t020 group ready fixture failed")
	case <-time.After(5 * time.Second):
		t.Fatal("t020 group ready handshake timed out")
	}
	return t020GroupIdentity{}
}

type t020DirectMember struct {
	cmd     *exec.Cmd
	done    chan struct{}
	state   *os.ProcessState
	waitErr error
}

func t020StartDirectMember(t *testing.T, pgid int, readyPath string) *t020DirectMember {
	t.Helper()
	cmd := exec.Command(t020HelperPath(t), t020HelperArgRun)
	cmd.Dir = t020TempDir(t)
	cmd.Env = t020HelperEnv(t020HelperModeGroupMember, t020HelperReadySocket+"="+readyPath)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pgid: pgid}
	if err := cmd.Start(); err != nil {
		t.Fatal("start t020 direct group member failed")
	}
	member := &t020DirectMember{cmd: cmd, done: make(chan struct{})}
	go func() {
		member.waitErr = cmd.Wait()
		member.state = cmd.ProcessState
		close(member.done)
	}()
	t.Cleanup(func() {
		member.killAndWait(t)
	})
	return member
}

func (m *t020DirectMember) killAndWait(t *testing.T) {
	t.Helper()
	if m == nil || m.cmd == nil {
		return
	}
	select {
	case <-m.done:
		return
	default:
	}
	if m.cmd.Process != nil {
		_ = m.cmd.Process.Kill()
	}
	select {
	case <-m.done:
	case <-time.After(5 * time.Second):
		t.Errorf("t020 direct member cleanup did not reap")
	}
}

func t020ReadHelperResult(t *testing.T, path string) t020HelperResult {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("read t020 helper result failed")
	}
	var result t020HelperResult
	if err := json.Unmarshal(data, &result); err != nil || !result.OK {
		t.Fatal("t020 helper observer generation failed")
	}
	return result
}

func t020RunObserverHelper(t *testing.T, artifacts *Artifacts, socket, runID, epoch string, generation uint64, orderID string) (Exit, t020HelperResult) {
	t.Helper()
	dir := t020TempDir(t)
	resultPath := filepath.Join(dir, "result.json")
	process, cancel := t020StartHelper(t, artifacts, generation, t020HelperModeObserver, time.Second,
		t020HelperSocket+"="+socket,
		t020HelperRunID+"="+runID,
		t020HelperEpoch+"="+epoch,
		t020HelperGeneration+"="+strconv.FormatUint(generation, 10),
		t020HelperOrderID+"="+orderID,
		t020HelperResultPath+"="+resultPath,
	)
	defer cancel()
	ctx, waitCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer waitCancel()
	exit, err := process.Wait(ctx)
	if err != nil {
		t.Fatal("wait t020 observer helper failed")
	}
	return exit, t020ReadHelperResult(t, resultPath)
}

func t020ReadReport(t *testing.T, path string) (t020Report, []byte) {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("read t020 report failed")
	}
	var report t020Report
	if err := json.Unmarshal(data, &report); err != nil {
		t.Fatal("decode t020 report failed")
	}
	return report, data
}

func t020RequireTopKeys(t *testing.T, data []byte) {
	t.Helper()
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatal("decode t020 report object failed")
	}
	want := []string{"schema", "run_id", "collection_complete", "faults", "versions", "source", "processes", "logs", "http", "sql", "budgets", "observer"}
	if len(raw) != len(want) {
		t.Fatal("t020 report top-level key set mismatch")
	}
	for _, key := range want {
		if _, ok := raw[key]; !ok {
			t.Fatal("t020 report is missing a top-level key")
		}
	}
}

func t020RequireSourceKeys(t *testing.T, data []byte) {
	t.Helper()
	var raw struct {
		Source map[string]json.RawMessage `json:"source"`
	}
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatal("decode t020 report source failed")
	}
	want := []string{"manifest", "manifest_digest", "unchanged"}
	if len(raw.Source) != len(want) {
		t.Fatal("t020 report source key set mismatch")
	}
	for _, key := range want {
		if _, ok := raw.Source[key]; !ok {
			t.Fatal("t020 report source is missing a key")
		}
	}
}

func t020RequireLogKeys(t *testing.T, logs []map[string]json.RawMessage) {
	t.Helper()
	allowed := map[string]bool{
		"time": true, "level": true, "event": true, "stage": true,
		"request_id": true, "payment_order_id": true,
		"result_before": true, "result_after": true,
		"reason": true, "error_kind": true,
		"generation": true, "stream": true,
	}
	required := []string{"time", "level", "event", "stage", "generation", "stream"}
	for _, log := range logs {
		for key := range log {
			if !allowed[key] {
				t.Fatal("t020 report log has an unknown key")
			}
		}
		for _, key := range required {
			if _, ok := log[key]; !ok {
				t.Fatal("t020 report log is missing a required key")
			}
		}
		if _, ok := log["client_id"]; ok {
			t.Fatal("t020 report log retained client_id")
		}
		if _, ok := log["business_order_no"]; ok {
			t.Fatal("t020 report log retained business_order_no")
		}
	}
}

func t020HasFault(faults []string, want string) bool {
	for _, fault := range faults {
		if fault == want {
			return true
		}
	}
	return false
}

func t020HasAnyFault(faults []string, wants ...string) bool {
	for _, want := range wants {
		if t020HasFault(faults, want) {
			return true
		}
	}
	return false
}

func t020RequireExit(t *testing.T, exit Exit, generation uint64, code int, canceled, timedOut, forced bool) {
	t.Helper()
	if exit.Generation != generation {
		t.Fatal("t020 process exit generation mismatch")
	}
	if exit.Code != code {
		t.Fatal("t020 process exit code mismatch")
	}
	if exit.Canceled != canceled || exit.TimedOut != timedOut || exit.Forced != forced {
		t.Fatal("t020 process exit flags mismatch")
	}
	if !exit.Reaped || !exit.OutputComplete || !exit.GroupGone {
		t.Fatal("t020 process resources are not confirmed")
	}
}

func t020RequireProcessWaitError(t *testing.T, err error) {
	t.Helper()
	if !errors.Is(err, ErrProcess) {
		t.Fatal("t020 process error is not ErrProcess")
	}
}

func t020RequireReportFailure(t *testing.T, path string, wantFaults ...string) t020Report {
	t.Helper()
	if path == "" {
		t.Fatal("t020 fault report path is empty")
	}
	report, data := t020ReadReport(t, path)
	if report.CollectionComplete {
		t.Fatal("t020 fault report claims complete collection")
	}
	if !t020HasAnyFault(report.Faults, wantFaults...) {
		t.Fatal("t020 report is missing the expected fixed fault")
	}
	t020RequireTopKeys(t, data)
	return report
}

func TestArtifactsFrozenConstants(t *testing.T) {
	if ArtifactSchema != "payment-hub.runtime-harness.v1" {
		t.Fatal("frozen artifact schema changed")
	}
	if MaxLogLineBytes != 4096 {
		t.Fatal("frozen log line limit changed")
	}
	if MaxProcessOutputBytes != 65536 {
		t.Fatal("frozen process output limit changed")
	}
	if BudgetStartup != "startup" || BudgetReadyz != "readyz" ||
		BudgetRead != "read" || BudgetWrite != "write" ||
		BudgetShutdown != "shutdown" || BudgetBarrier != "barrier" ||
		BudgetProcess != "process" || BudgetCleanup != "cleanup" {
		t.Fatal("frozen budget stage value changed")
	}
	if ErrProcess == nil || ErrArtifacts == nil {
		t.Fatal("t020 sentinel is nil")
	}
	if errors.Is(ErrProcess, ErrArtifacts) || errors.Is(ErrArtifacts, ErrProcess) {
		t.Fatal("t020 sentinels are not distinct")
	}
}

func TestArtifactsReportShapeAndValidRecords(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, reportPath := t020BeginArtifacts(t, source)

	pending, pendingData := t020ReadReport(t, reportPath)
	t020RequireTopKeys(t, pendingData)
	t020RequireSourceKeys(t, pendingData)
	if pending.Schema != ArtifactSchema || pending.RunID != t020RunID {
		t.Fatal("t020 pending report identity mismatch")
	}
	if len(pending.Source.Manifest) == 0 || pending.Source.ManifestDigest == "" {
		t.Fatal("t020 pending report source manifest is empty")
	}
	if pending.CollectionComplete {
		t.Fatal("t020 pending report claims complete collection")
	}
	if pending.Observer != nil {
		t.Fatal("t020 pending report unexpectedly has an observer")
	}
	runDir := filepath.Dir(reportPath)
	dirInfo, err := os.Stat(runDir)
	if err != nil {
		t.Fatal("stat t020 run directory failed")
	}
	if dirInfo.Mode().Perm() != 0o700 {
		t.Fatal("t020 run directory mode is not 0700")
	}
	fileInfo, err := os.Stat(reportPath)
	if err != nil {
		t.Fatal("stat t020 report failed")
	}
	if fileInfo.Mode().Perm() != 0o600 {
		t.Fatal("t020 report mode is not 0600")
	}

	if err := artifacts.RecordHTTP(HTTPObservation{
		Generation:     1,
		Status:         200,
		RequestID:      t020RequestID,
		PaymentOrderID: t020PaymentID,
		Result:         "SUCCEEDED",
	}); err != nil {
		t.Fatal("record valid t020 HTTP observation failed")
	}
	if err := artifacts.RecordSQL(SQLObservation{Generation: 1, State: "00000", Rows: 1}); err != nil {
		t.Fatal("record valid t020 SQL observation failed")
	}
	if err := artifacts.RecordBudget(BudgetObservation{
		Generation: 1,
		Stage:      BudgetStartup,
		Limit:      time.Second,
		Elapsed:    100 * time.Millisecond,
	}); err != nil {
		t.Fatal("record valid t020 budget observation failed")
	}

	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)
	obsTestSealZero(t, h, 1)
	if err := artifacts.RecordObserver(h.obs); err != nil {
		t.Fatal("record valid t020 observer failed")
	}
	if err := h.close(); err != nil {
		t.Fatal("close valid t020 observer failed")
	}

	path, err := artifacts.Finish()
	if err != nil {
		t.Fatal("finish t020 artifacts failed")
	}
	if path != reportPath {
		t.Fatal("t020 finish path mismatch")
	}
	report, data := t020ReadReport(t, path)
	t020RequireTopKeys(t, data)
	t020RequireSourceKeys(t, data)
	if info, err := os.Stat(path); err != nil || info.Mode().Perm() != 0o600 {
		t.Fatal("t020 finished report mode is not 0600")
	}
	if !report.CollectionComplete {
		t.Fatal("t020 complete report is not marked complete")
	}
	if report.Schema != ArtifactSchema || report.RunID != t020RunID {
		t.Fatal("t020 report identity mismatch")
	}
	if len(report.Faults) != 0 {
		t.Fatal("t020 complete report has faults")
	}
	if runtime.Version() != "go1.27.3" || runtime.GOOS != "linux" || runtime.GOARCH != "amd64" {
		t.Fatal("t020 test environment is not the approved runtime")
	}
	if report.Versions.Go != runtime.Version() || report.Versions.Goos != runtime.GOOS || report.Versions.Goarch != runtime.GOARCH {
		t.Fatal("t020 report runtime versions mismatch")
	}
	if !report.Source.Unchanged || report.Source.ManifestDigest == "" || len(report.Source.Manifest) == 0 {
		t.Fatal("t020 report source manifest is incomplete")
	}
	expectedManifest, expectedDigest := t020ExpectedManifest(t, source)
	if !reflect.DeepEqual(report.Source.Manifest, expectedManifest) {
		t.Fatal("t020 report source manifest does not match the fixture")
	}
	if report.Source.ManifestDigest != expectedDigest {
		t.Fatal("t020 report source manifest digest mismatch")
	}
	if len(report.HTTP) != 1 || report.HTTP[0].Status != 200 || report.HTTP[0].Result != "SUCCEEDED" {
		t.Fatal("t020 report HTTP observation mismatch")
	}
	if len(report.SQL) != 1 || report.SQL[0].State != "00000" || report.SQL[0].Rows != 1 {
		t.Fatal("t020 report SQL observation mismatch")
	}
	if len(report.Budgets) != 1 || report.Budgets[0].Stage != string(BudgetStartup) ||
		report.Budgets[0].LimitNS != int64(time.Second) ||
		report.Budgets[0].ElapsedNS != int64(100*time.Millisecond) {
		t.Fatal("t020 report budget observation mismatch")
	}
	if report.Observer == nil || report.Observer.Epoch != obsTestEpoch ||
		report.Observer.Counts.Payment != 0 || report.Observer.Counts.Query != 0 ||
		len(report.Observer.Receipts) != 1 {
		t.Fatal("t020 report observer observation mismatch")
	}
	if bytes.Contains(data, []byte(source)) || bytes.Contains(data, []byte(t020SourceMark)) {
		t.Fatal("t020 report leaked source path or content")
	}
	if _, err := artifacts.Finish(); !errors.Is(err, ErrArtifacts) {
		t.Fatal("t020 duplicate finish did not return ErrArtifacts")
	}
}

func TestArtifactsRejectsInvalidRecordsAndLatches(t *testing.T) {
	cases := []struct {
		name   string
		record func(*Artifacts) error
	}{
		{
			name: "http_status_low",
			record: func(a *Artifacts) error {
				return a.RecordHTTP(HTTPObservation{Status: 99})
			},
		},
		{
			name: "http_status_high",
			record: func(a *Artifacts) error {
				return a.RecordHTTP(HTTPObservation{Status: 600})
			},
		},
		{
			name: "http_request_id",
			record: func(a *Artifacts) error {
				return a.RecordHTTP(HTTPObservation{Status: 200, RequestID: t020Canary})
			},
		},
		{
			name: "http_payment_id",
			record: func(a *Artifacts) error {
				return a.RecordHTTP(HTTPObservation{Status: 200, PaymentOrderID: t020Canary})
			},
		},
		{
			name: "http_result",
			record: func(a *Artifacts) error {
				return a.RecordHTTP(HTTPObservation{Status: 200, Result: t020Canary})
			},
		},
		{
			name: "sql_state",
			record: func(a *Artifacts) error {
				return a.RecordSQL(SQLObservation{State: t020Canary})
			},
		},
		{
			name: "budget_stage",
			record: func(a *Artifacts) error {
				return a.RecordBudget(BudgetObservation{Stage: BudgetStage(t020Canary), Limit: time.Second})
			},
		},
		{
			name: "budget_limit",
			record: func(a *Artifacts) error {
				return a.RecordBudget(BudgetObservation{Stage: BudgetStartup, Limit: 0})
			},
		},
		{
			name: "budget_elapsed",
			record: func(a *Artifacts) error {
				return a.RecordBudget(BudgetObservation{Stage: BudgetStartup, Limit: time.Second, Elapsed: -time.Nanosecond})
			},
		},
		{
			name: "budget_too_long",
			record: func(a *Artifacts) error {
				return a.RecordBudget(BudgetObservation{Stage: BudgetStartup, Limit: time.Hour + time.Nanosecond})
			},
		},
		{
			name: "observer_nil",
			record: func(a *Artifacts) error {
				return a.RecordObserver(nil)
			},
		},
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			source := t020SourceFixture(t)
			artifacts, _ := t020BeginArtifacts(t, source)
			if err := tc.record(artifacts); !errors.Is(err, ErrArtifacts) {
				t.Fatal("invalid t020 record did not return ErrArtifacts")
			}
			if err := artifacts.RecordHTTP(HTTPObservation{Status: 200}); !errors.Is(err, ErrArtifacts) {
				t.Fatal("t020 artifacts did not latch the rejected record")
			}
			path, err := artifacts.Finish()
			if !errors.Is(err, ErrArtifacts) {
				t.Fatal("t020 finish after invalid record did not fail")
			}
			if path == "" {
				t.Fatal("t020 fault report path is empty")
			}
			report, data := t020ReadReport(t, path)
			if report.CollectionComplete {
				t.Fatal("t020 invalid record report claims complete collection")
			}
			if !t020HasFault(report.Faults, "invalid_record") {
				t.Fatal("t020 invalid record fault category mismatch")
			}
			if bytes.Contains(data, []byte(t020Canary)) {
				t.Fatal("t020 report leaked rejected record input")
			}
			if len(report.HTTP) != 0 || len(report.SQL) != 0 || len(report.Budgets) != 0 {
				t.Fatal("t020 report stored a rejected record")
			}
		})
	}
}

func TestArtifactsRecordObserverRequiresTrustedSnapshot(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	h := obsTestStart(t, obsTestBarrier)
	if err := artifacts.RecordObserver(h.obs); !errors.Is(err, ErrArtifacts) {
		t.Fatal("uncertain t020 observer was accepted")
	}
	path, err := artifacts.Finish()
	if !errors.Is(err, ErrArtifacts) {
		t.Fatal("finish after uncertain t020 observer did not fail")
	}
	t020RequireReportFailure(t, path, "observer_uncertain")
}

func TestArtifactsSourceChangeFails(t *testing.T) {
	cases := []struct {
		name   string
		change func(*testing.T, string)
	}{
		{
			name: "modified",
			change: func(t *testing.T, source string) {
				path := filepath.Join(source, "internal", "app", "app.go")
				if err := os.WriteFile(path, []byte("package app\nconst marker = \"changed\"\n"), 0o600); err != nil {
					t.Fatal("modify t020 source fixture failed")
				}
			},
		},
		{
			name: "added_untracked_cmd",
			change: func(t *testing.T, source string) {
				path := filepath.Join(source, "cmd", "payment-hub", "extra.go")
				if err := os.WriteFile(path, []byte("package main\n"), 0o600); err != nil {
					t.Fatal("add t020 source fixture failed")
				}
			},
		},
		{
			name: "deleted",
			change: func(t *testing.T, source string) {
				path := filepath.Join(source, "tests", "harness", "helper.go")
				if err := os.Remove(path); err != nil {
					t.Fatal("delete t020 source fixture failed")
				}
			},
		},
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			source := t020SourceFixture(t)
			artifacts, _ := t020BeginArtifacts(t, source)
			tc.change(t, source)
			path, err := artifacts.Finish()
			if !errors.Is(err, ErrArtifacts) {
				t.Fatal("t020 finish after source change did not fail")
			}
			report := t020RequireReportFailure(t, path, "source_changed")
			if report.Source.Unchanged {
				t.Fatal("t020 report source change is not visible")
			}
			_, data := t020ReadReport(t, path)
			if bytes.Contains(data, []byte(t020SourceMark)) {
				t.Fatal("t020 report leaked source content")
			}
		})
	}
}

func TestArtifactsRejectsUnsafeRootsAndTargets(t *testing.T) {
	source := t020SourceFixture(t)
	root := t020TempDir(t)

	if _, err := BeginArtifacts(ArtifactConfig{Root: "relative", SourceRoot: source, RunID: t020RunID}); !errors.Is(err, ErrArtifacts) {
		t.Fatal("t020 relative root was accepted")
	}
	if _, err := BeginArtifacts(ArtifactConfig{Root: root, SourceRoot: "relative", RunID: t020RunID}); !errors.Is(err, ErrArtifacts) {
		t.Fatal("t020 relative source root was accepted")
	}
	if _, err := BeginArtifacts(ArtifactConfig{Root: root, SourceRoot: source, RunID: strings.ToUpper(t020RunID)}); !errors.Is(err, ErrArtifacts) {
		t.Fatal("t020 uppercase run ID was accepted")
	}
	if _, err := BeginArtifacts(ArtifactConfig{Root: root, SourceRoot: source, RunID: t020RunID[:31]}); !errors.Is(err, ErrArtifacts) {
		t.Fatal("t020 short run ID was accepted")
	}

	symlinkRoot := filepath.Join(t020TempDir(t), "root-link")
	if err := os.Symlink(root, symlinkRoot); err != nil {
		t.Fatal("create t020 root symlink failed")
	}
	if _, err := BeginArtifacts(ArtifactConfig{Root: symlinkRoot, SourceRoot: source, RunID: t020RunID}); !errors.Is(err, ErrArtifacts) {
		t.Fatal("t020 symlink root was accepted")
	}

	symlinkSource := filepath.Join(t020TempDir(t), "source-link")
	if err := os.Symlink(source, symlinkSource); err != nil {
		t.Fatal("create t020 source symlink failed")
	}
	if _, err := BeginArtifacts(ArtifactConfig{Root: root, SourceRoot: symlinkSource, RunID: t020RunID}); !errors.Is(err, ErrArtifacts) {
		t.Fatal("t020 symlink source root was accepted")
	}

	preexistingRoot := t020TempDir(t)
	if err := os.Mkdir(filepath.Join(preexistingRoot, t020RunID), 0o700); err != nil {
		t.Fatal("create t020 preexisting run directory failed")
	}
	if _, err := BeginArtifacts(ArtifactConfig{Root: preexistingRoot, SourceRoot: source, RunID: t020RunID}); !errors.Is(err, ErrArtifacts) {
		t.Fatal("t020 preexisting run directory was accepted")
	}

	reportRoot := t020TempDir(t)
	runDir := filepath.Join(reportRoot, t020RunID)
	if err := os.Mkdir(runDir, 0o700); err != nil {
		t.Fatal("create t020 report symlink run directory failed")
	}
	foreign := filepath.Join(t020TempDir(t), "foreign")
	if err := os.WriteFile(foreign, []byte("foreign"), 0o600); err != nil {
		t.Fatal("create t020 foreign report fixture failed")
	}
	if err := os.Symlink(foreign, filepath.Join(runDir, "runtime-harness.json")); err != nil {
		t.Fatal("create t020 report symlink failed")
	}
	if _, err := BeginArtifacts(ArtifactConfig{Root: reportRoot, SourceRoot: source, RunID: t020RunID}); !errors.Is(err, ErrArtifacts) {
		t.Fatal("t020 report symlink target was accepted")
	}
	data, err := os.ReadFile(foreign)
	if err != nil || string(data) != "foreign" {
		t.Fatal("t020 report symlink target changed")
	}
}

func TestArtifactsRejectsSelectedSourceSymlinks(t *testing.T) {
	t.Run("file", func(t *testing.T) {
		source := t020SourceFixture(t)
		outside := filepath.Join(t020TempDir(t), "outside.sql")
		if err := os.WriteFile(outside, []byte("-- outside\n"), 0o600); err != nil {
			t.Fatal("create t020 outside source file failed")
		}
		target := filepath.Join(source, "migrations", "00001_create.sql")
		if err := os.Remove(target); err != nil {
			t.Fatal("remove t020 source file fixture failed")
		}
		if err := os.Symlink(outside, target); err != nil {
			t.Fatal("create t020 source file symlink failed")
		}
		root := t020TempDir(t)
		if _, err := BeginArtifacts(ArtifactConfig{Root: root, SourceRoot: source, RunID: t020RunID}); !errors.Is(err, ErrArtifacts) {
			t.Fatal("t020 selected source file symlink was accepted")
		}
	})

	t.Run("directory", func(t *testing.T) {
		source := t020SourceFixture(t)
		outside := t020TempDir(t)
		if err := os.WriteFile(filepath.Join(outside, "app.go"), []byte("package app\n"), 0o600); err != nil {
			t.Fatal("create t020 outside source directory fixture failed")
		}
		target := filepath.Join(source, "internal", "app")
		if err := os.RemoveAll(target); err != nil {
			t.Fatal("remove t020 source directory fixture failed")
		}
		if err := os.Symlink(outside, target); err != nil {
			t.Fatal("create t020 source directory symlink failed")
		}
		root := t020TempDir(t)
		if _, err := BeginArtifacts(ArtifactConfig{Root: root, SourceRoot: source, RunID: t020RunID}); !errors.Is(err, ErrArtifacts) {
			t.Fatal("t020 selected source directory symlink was accepted")
		}
	})
}

func TestArtifactsFinishRejectsReplacedReportSymlink(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, reportPath := t020BeginArtifacts(t, source)
	foreign := filepath.Join(t020TempDir(t), "foreign")
	if err := os.WriteFile(foreign, []byte("foreign"), 0o600); err != nil {
		t.Fatal("create t020 foreign report fixture failed")
	}
	if err := os.Remove(reportPath); err != nil {
		t.Fatal("remove t020 pending report fixture failed")
	}
	if err := os.Symlink(foreign, reportPath); err != nil {
		t.Fatal("create t020 replaced report symlink failed")
	}
	path, err := artifacts.Finish()
	if !errors.Is(err, ErrArtifacts) {
		t.Fatal("t020 finish followed a replaced report symlink")
	}
	_ = path
	data, err := os.ReadFile(foreign)
	if err != nil || string(data) != "foreign" {
		t.Fatal("t020 finish changed the foreign report target")
	}
	info, err := os.Lstat(reportPath)
	if err != nil || info.Mode()&os.ModeSymlink == 0 {
		t.Fatal("t020 finish removed or replaced the foreign report symlink")
	}
}

func TestArtifactsPreservesRootMode(t *testing.T) {
	root := t020TempDir(t)
	if err := os.Chmod(root, 0o755); err != nil {
		t.Fatal("set t020 root mode failed")
	}
	source := t020SourceFixture(t)
	artifacts, reportPath := t020BeginArtifactsAt(t, root, source, t020RunID)
	info, err := os.Stat(root)
	if err != nil || info.Mode().Perm() != 0o755 {
		t.Fatal("t020 begin changed the root mode")
	}
	if _, err := artifacts.Finish(); err != nil {
		t.Fatal("finish t020 artifacts failed")
	}
	info, err = os.Stat(root)
	if err != nil || info.Mode().Perm() != 0o755 {
		t.Fatal("t020 finish changed the root mode")
	}
	if _, err := os.Stat(reportPath); err != nil {
		t.Fatal("t020 report was not preserved")
	}
}

func TestArtifactsWriteFailureVisible(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, reportPath := t020BeginArtifacts(t, source)
	runDir := filepath.Dir(reportPath)
	t.Cleanup(func() {
		_ = os.Chmod(runDir, 0o700)
		_ = os.Chmod(reportPath, 0o600)
	})
	if err := os.Chmod(reportPath, 0o400); err != nil {
		t.Fatal("make t020 report read-only failed")
	}
	if err := os.Chmod(runDir, 0o500); err != nil {
		t.Fatal("make t020 run directory read-only failed")
	}
	path, err := artifacts.Finish()
	if !errors.Is(err, ErrArtifacts) {
		t.Fatal("t020 finish did not report a write failure")
	}
	if path != "" {
		t020RequireReportFailure(t, path, "write_failed")
	}
}

func TestArtifactsTypedRecordLimit(t *testing.T) {
	cases := []struct {
		name   string
		record func(*Artifacts) error
	}{
		{
			name: "budget",
			record: func(a *Artifacts) error {
				return a.RecordBudget(BudgetObservation{Stage: BudgetStartup, Limit: time.Second})
			},
		},
		{
			name: "http",
			record: func(a *Artifacts) error {
				return a.RecordHTTP(HTTPObservation{Status: 200})
			},
		},
		{
			name: "sql",
			record: func(a *Artifacts) error {
				return a.RecordSQL(SQLObservation{State: "00000"})
			},
		},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			source := t020SourceFixture(t)
			artifacts, _ := t020BeginArtifacts(t, source)
			for i := 0; i < testcontrol.MaxRecords; i++ {
				if err := tc.record(artifacts); err != nil {
					t.Fatal("t020 typed record below the limit failed")
				}
			}
			if err := tc.record(artifacts); !errors.Is(err, ErrArtifacts) {
				t.Fatal("t020 typed record above the limit was accepted")
			}
			path, err := artifacts.Finish()
			if !errors.Is(err, ErrArtifacts) {
				t.Fatal("t020 finish after typed record overflow did not fail")
			}
			report := t020RequireReportFailure(t, path, "invalid_record")
			if report.CollectionComplete {
				t.Fatal("t020 typed record overflow report claims complete collection")
			}
		})
	}
}

func TestProcessStartRequiresArtifactsAndValidConfig(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	helper := t020HelperPath(t)
	dir := t020TempDir(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	cases := []struct {
		name string
		ctx  context.Context
		cfg  ProcessConfig
	}{
		{
			name: "nil_artifacts",
			ctx:  ctx,
			cfg:  ProcessConfig{Path: helper, Dir: dir, Args: []string{t020HelperArgRun}, Env: t020HelperEnv(t020HelperModeExit0), Generation: 1},
		},
		{
			name: "zero_artifacts",
			ctx:  ctx,
			cfg:  ProcessConfig{Path: helper, Dir: dir, Args: []string{t020HelperArgRun}, Env: t020HelperEnv(t020HelperModeExit0), Generation: 1, Artifacts: &Artifacts{}},
		},
		{
			name: "relative_path",
			ctx:  ctx,
			cfg:  ProcessConfig{Path: "relative", Dir: dir, Args: []string{t020HelperArgRun}, Env: t020HelperEnv(t020HelperModeExit0), Generation: 1, Artifacts: artifacts},
		},
		{
			name: "empty_path",
			ctx:  ctx,
			cfg:  ProcessConfig{Dir: dir, Args: []string{t020HelperArgRun}, Env: t020HelperEnv(t020HelperModeExit0), Generation: 1, Artifacts: artifacts},
		},
		{
			name: "zero_generation",
			ctx:  ctx,
			cfg:  ProcessConfig{Path: helper, Dir: dir, Args: []string{t020HelperArgRun}, Env: t020HelperEnv(t020HelperModeExit0), Artifacts: artifacts},
		},
		{
			name: "negative_grace",
			ctx:  ctx,
			cfg:  ProcessConfig{Path: helper, Dir: dir, Args: []string{t020HelperArgRun}, Env: t020HelperEnv(t020HelperModeExit0), Generation: 1, Grace: -time.Second, Artifacts: artifacts},
		},
		{
			name: "duplicate_env",
			ctx:  ctx,
			cfg:  ProcessConfig{Path: helper, Dir: dir, Args: []string{t020HelperArgRun}, Env: append(t020HelperEnv(t020HelperModeExit0), "TZ=UTC"), Generation: 1, Artifacts: artifacts},
		},
		{
			name: "invalid_env",
			ctx:  ctx,
			cfg:  ProcessConfig{Path: helper, Dir: dir, Args: []string{t020HelperArgRun}, Env: append(t020HelperEnv(t020HelperModeExit0), "=value"), Generation: 1, Artifacts: artifacts},
		},
		{
			name: "ld_env",
			ctx:  ctx,
			cfg:  ProcessConfig{Path: helper, Dir: dir, Args: []string{t020HelperArgRun}, Env: append(t020HelperEnv(t020HelperModeExit0), "LD_PRELOAD=/tmp/none"), Generation: 1, Artifacts: artifacts},
		},
		{
			name: "nil_lifetime",
			ctx:  nil,
			cfg:  ProcessConfig{Path: helper, Dir: dir, Args: []string{t020HelperArgRun}, Env: t020HelperEnv(t020HelperModeExit0), Generation: 1, Artifacts: artifacts},
		},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			process, err := StartProcess(tc.ctx, tc.cfg)
			if !errors.Is(err, ErrProcess) {
				t020StopAndConfirm(t, process)
				t.Fatal("invalid t020 process config did not return ErrProcess")
			}
			if process != nil {
				t.Fatal("invalid t020 process config returned a process")
			}
		})
	}

	canceled, cancelLifetime := context.WithCancel(context.Background())
	cancelLifetime()
	process, err := StartProcess(canceled, ProcessConfig{
		Path: helper, Dir: dir, Args: []string{t020HelperArgRun},
		Env: t020HelperEnv(t020HelperModeExit0), Generation: 1, Artifacts: artifacts,
	})
	if !errors.Is(err, ErrProcess) || process != nil {
		t.Fatal("t020 canceled lifetime context was accepted")
	}

	var nilProcess *Process
	if _, err := nilProcess.Wait(ctx); !errors.Is(err, ErrProcess) {
		t.Fatal("nil t020 process wait did not return ErrProcess")
	}
	if _, err := nilProcess.Stop(ctx); !errors.Is(err, ErrProcess) {
		t.Fatal("nil t020 process stop did not return ErrProcess")
	}
}

func TestProcessNormalAndNonZeroExit(t *testing.T) {
	for _, tc := range []struct {
		name string
		mode string
		code int
		err  bool
	}{
		{name: "exit_zero", mode: t020HelperModeExit0, code: 0},
		{name: "exit_three", mode: t020HelperModeExit3, code: 3, err: true},
	} {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			source := t020SourceFixture(t)
			artifacts, _ := t020BeginArtifacts(t, source)
			process, cancel := t020StartHelper(t, artifacts, 1, tc.mode, time.Second)
			defer cancel()
			ctx, waitCancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer waitCancel()
			exit, err := process.Wait(ctx)
			if tc.err {
				t020RequireProcessWaitError(t, err)
			} else if err != nil {
				t.Fatal("normal t020 process wait failed")
			}
			t020RequireExit(t, exit, 1, tc.code, false, false, false)
			path, finishErr := artifacts.Finish()
			if finishErr != nil {
				t.Fatal("finish t020 process artifacts failed")
			}
			report, data := t020ReadReport(t, path)
			if !report.CollectionComplete || len(report.Processes) != 1 {
				t.Fatal("t020 process report is incomplete")
			}
			if report.Processes[0].Code != tc.code || !report.Processes[0].Waited ||
				!report.Processes[0].Reaped || !report.Processes[0].OutputComplete ||
				!report.Processes[0].GroupGone {
				t.Fatal("t020 process report exit mismatch")
			}
			if bytes.Contains(data, []byte(t020HelperPath(t))) ||
				bytes.Contains(data, []byte(t020HelperArgRun)) ||
				bytes.Contains(data, []byte("PAYMENT_HUB_T020_")) {
				t.Fatal("t020 report leaked process path, args, or env")
			}
		})
	}
}

func TestProcessStopAndForcedKill(t *testing.T) {
	t.Run("graceful_sigterm", func(t *testing.T) {
		source := t020SourceFixture(t)
		artifacts, _ := t020BeginArtifacts(t, source)
		ready := t020ReadySocket(t)
		process, cancel := t020StartHelper(t, artifacts, 1, t020HelperModeGraceful, time.Second,
			t020HelperReadySocket+"="+ready.path)
		defer cancel()
		t020AwaitReady(t, ready)
		ctx, stopCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer stopCancel()
		exit, err := process.Stop(ctx)
		t020RequireProcessWaitError(t, err)
		t020RequireExit(t, exit, 1, 0, true, false, false)
		path, finishErr := artifacts.Finish()
		if finishErr != nil {
			t.Fatal("finish t020 graceful stop artifacts failed")
		}
		report, _ := t020ReadReport(t, path)
		if len(report.Processes) != 1 || !report.Processes[0].Canceled || report.Processes[0].Forced {
			t.Fatal("t020 graceful stop report mismatch")
		}
	})

	t.Run("forced_sigkill", func(t *testing.T) {
		source := t020SourceFixture(t)
		artifacts, _ := t020BeginArtifacts(t, source)
		ready := t020ReadySocket(t)
		process, cancel := t020StartHelper(t, artifacts, 1, t020HelperModeIgnore, 200*time.Millisecond,
			t020HelperReadySocket+"="+ready.path)
		defer cancel()
		t020AwaitReady(t, ready)
		ctx, stopCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer stopCancel()
		exit, err := process.Stop(ctx)
		t020RequireProcessWaitError(t, err)
		if !exit.Forced || exit.Signal != int(syscall.SIGKILL) ||
			!exit.Reaped || !exit.OutputComplete || !exit.GroupGone {
			t.Fatal("t020 forced stop did not reap the process group")
		}
		path, finishErr := artifacts.Finish()
		if finishErr != nil {
			t.Fatal("finish t020 forced stop artifacts failed")
		}
		report, _ := t020ReadReport(t, path)
		if len(report.Processes) != 1 || !report.Processes[0].Forced || !report.Processes[0].Waited {
			t.Fatal("t020 forced stop report mismatch")
		}
	})
}

func TestProcessConcurrentWaitStop(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	ready := t020ReadySocket(t)
	process, cancel := t020StartHelper(t, artifacts, 1, t020HelperModeGraceful, time.Second,
		t020HelperReadySocket+"="+ready.path)
	defer cancel()
	t020AwaitReady(t, ready)

	type result struct {
		exit Exit
		err  error
	}
	waitResult := make(chan result, 1)
	go func() {
		ctx, waitCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer waitCancel()
		exit, err := process.Wait(ctx)
		waitResult <- result{exit: exit, err: err}
	}()

	stopCtx, stopCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer stopCancel()
	stopExit, stopErr := process.Stop(stopCtx)
	t020RequireProcessWaitError(t, stopErr)

	select {
	case waited := <-waitResult:
		if waited.err == nil {
			t.Fatal("concurrent t020 wait did not report the stop")
		}
		t020RequireProcessWaitError(t, waited.err)
		if waited.exit != stopExit {
			t.Fatal("concurrent t020 wait and stop results differ")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("concurrent t020 wait did not finish")
	}
	t020RequireExit(t, stopExit, 1, 0, true, false, false)
	if _, err := artifacts.Finish(); err != nil {
		t.Fatal("finish concurrent t020 wait/stop artifacts failed")
	}
}

func TestProcessGroupReclamationIncludesMember(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	leaderReady := t020GroupReadySocket(t)
	process, cancel := t020StartHelper(t, artifacts, 1, t020HelperModeGroupLeader, 200*time.Millisecond,
		t020HelperReadySocket+"="+leaderReady.path)
	defer cancel()
	leader := t020AwaitGroupReady(t, leaderReady)
	if leader.Pid != leader.Pgid || leader.Pgid <= 1 || leader.Pgid == syscall.Getpgrp() {
		t.Fatal("t020 leader process group is unsafe")
	}

	memberReady := t020GroupReadySocket(t)
	member := t020StartDirectMember(t, leader.Pgid, memberReady.path)
	memberIdentity := t020AwaitGroupReady(t, memberReady)
	if memberIdentity.Pgid != leader.Pgid || memberIdentity.Pid != member.cmd.Process.Pid || memberIdentity.Pid == leader.Pid || memberIdentity.Pid <= 1 {
		t.Fatal("t020 member process group mismatch")
	}

	ctx, stopCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer stopCancel()
	exit, err := process.Stop(ctx)
	t020RequireProcessWaitError(t, err)
	if !exit.Forced || exit.Signal != int(syscall.SIGKILL) ||
		!exit.Reaped || !exit.OutputComplete || !exit.GroupGone {
		t.Fatal("t020 leader group was not force-reaped")
	}

	select {
	case <-member.done:
	case <-time.After(5 * time.Second):
		t.Fatal("t020 direct group member was not reaped")
	}
	if member.state == nil {
		t.Fatal("t020 direct group member state is missing")
	}
	status, ok := member.state.Sys().(syscall.WaitStatus)
	if !ok || !status.Signaled() || status.Signal() != syscall.SIGKILL {
		t.Fatal("t020 direct group member was not SIGKILLed")
	}
	if _, err := artifacts.Finish(); err != nil {
		t.Fatal("finish t020 group reclamation artifacts failed")
	}
}

func TestProcessWaitShortContextReaps(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	ready := t020ReadySocket(t)
	process, cancel := t020StartHelper(t, artifacts, 1, t020HelperModeIgnore, 200*time.Millisecond,
		t020HelperReadySocket+"="+ready.path)
	defer cancel()
	t020AwaitReady(t, ready)

	target, targetCancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer targetCancel()
	fallback, fallbackCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer fallbackCancel()
	type result struct {
		exit Exit
		err  error
	}
	waitResult := make(chan result, 1)
	go func() {
		exit, err := process.Wait(target)
		waitResult <- result{exit: exit, err: err}
	}()

	select {
	case waited := <-waitResult:
		if fallback.Err() != nil {
			t.Fatal("t020 short wait cleanup exceeded the fallback budget")
		}
		if !errors.Is(target.Err(), context.DeadlineExceeded) {
			t.Fatal("t020 short wait returned before its target deadline")
		}
		t020RequireProcessWaitError(t, waited.err)
		if !waited.exit.Forced || !waited.exit.Reaped ||
			!waited.exit.OutputComplete || !waited.exit.GroupGone {
			t.Fatal("t020 short wait context did not reap actual resources")
		}
	case <-fallback.Done():
		t.Fatal("t020 short wait context did not return after cleanup")
	}
	if _, err := artifacts.Finish(); err != nil {
		t.Fatal("finish t020 short wait artifacts failed")
	}
}

func TestProcessLifetimeCancelAndTimeout(t *testing.T) {
	t.Run("cancel", func(t *testing.T) {
		source := t020SourceFixture(t)
		artifacts, _ := t020BeginArtifacts(t, source)
		ready := t020ReadySocket(t)
		dir := t020TempDir(t)
		lifetime, cancelLifetime := context.WithTimeout(context.Background(), 10*time.Second)
		process, err := StartProcess(lifetime, ProcessConfig{
			Path: t020HelperPath(t), Dir: dir, Args: []string{t020HelperArgRun},
			Env:        t020HelperEnv(t020HelperModeIgnore, t020HelperReadySocket+"="+ready.path),
			Generation: 1, Grace: 200 * time.Millisecond, Artifacts: artifacts,
		})
		if err != nil {
			cancelLifetime()
			t.Fatal("start t020 cancel helper failed")
		}
		t.Cleanup(func() {
			cancelLifetime()
			t020StopAndConfirm(t, process)
		})
		t020AwaitReady(t, ready)
		cancelLifetime()
		ctx, waitCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer waitCancel()
		exit, waitErr := process.Wait(ctx)
		t020RequireProcessWaitError(t, waitErr)
		if !exit.Canceled || exit.TimedOut || !exit.Forced ||
			!exit.Reaped || !exit.OutputComplete || !exit.GroupGone {
			t.Fatal("t020 lifetime cancel exit mismatch")
		}
		path, finishErr := artifacts.Finish()
		if finishErr != nil {
			t.Fatal("finish t020 cancel artifacts failed")
		}
		report, _ := t020ReadReport(t, path)
		if len(report.Processes) != 1 || !report.Processes[0].Canceled || !report.Processes[0].Forced {
			t.Fatal("t020 cancel report mismatch")
		}
	})

	t.Run("timeout", func(t *testing.T) {
		source := t020SourceFixture(t)
		artifacts, _ := t020BeginArtifacts(t, source)
		ready := t020ReadySocket(t)
		dir := t020TempDir(t)
		lifetime, cancelLifetime := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancelLifetime()
		process, err := StartProcess(lifetime, ProcessConfig{
			Path: t020HelperPath(t), Dir: dir, Args: []string{t020HelperArgRun},
			Env:        t020HelperEnv(t020HelperModeIgnore, t020HelperReadySocket+"="+ready.path),
			Generation: 1, Grace: 200 * time.Millisecond, Artifacts: artifacts,
		})
		if err != nil {
			t.Fatal("start t020 timeout helper failed")
		}
		t.Cleanup(func() {
			t020StopAndConfirm(t, process)
		})
		t020AwaitReady(t, ready)
		ctx, waitCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer waitCancel()
		exit, waitErr := process.Wait(ctx)
		t020RequireProcessWaitError(t, waitErr)
		if !exit.TimedOut || !exit.Forced ||
			!exit.Reaped || !exit.OutputComplete || !exit.GroupGone {
			t.Fatal("t020 lifetime timeout exit mismatch")
		}
		path, finishErr := artifacts.Finish()
		if finishErr != nil {
			t.Fatal("finish t020 timeout artifacts failed")
		}
		report, _ := t020ReadReport(t, path)
		if len(report.Processes) != 1 || !report.Processes[0].TimedOut || !report.Processes[0].Forced {
			t.Fatal("t020 timeout report mismatch")
		}
	})
}

func TestProcessOutputProtocolAndLimits(t *testing.T) {
	valid := []struct {
		name        string
		mode        string
		wantLogs    int
		wantStreams []string
	}{
		{name: "valid", mode: t020HelperModeLogs, wantLogs: 2, wantStreams: []string{"stdout", "stderr"}},
		{name: "max_line", mode: t020HelperModeLogsMax, wantLogs: 1, wantStreams: []string{"stdout"}},
	}
	for _, tc := range valid {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			source := t020SourceFixture(t)
			artifacts, _ := t020BeginArtifacts(t, source)
			process, cancel := t020StartHelper(t, artifacts, 1, tc.mode, time.Second)
			defer cancel()
			ctx, waitCancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer waitCancel()
			exit, err := process.Wait(ctx)
			if err != nil {
				t.Fatal("valid t020 log helper wait failed")
			}
			t020RequireExit(t, exit, 1, 0, false, false, false)
			path, finishErr := artifacts.Finish()
			if finishErr != nil {
				t.Fatal("finish valid t020 log artifacts failed")
			}
			report, data := t020ReadReport(t, path)
			if !report.CollectionComplete || len(report.Logs) != tc.wantLogs {
				t.Fatal("t020 valid log report mismatch")
			}
			var raw struct {
				Logs []map[string]json.RawMessage `json:"logs"`
			}
			if err := json.Unmarshal(data, &raw); err != nil {
				t.Fatal("decode t020 report logs failed")
			}
			t020RequireLogKeys(t, raw.Logs)
			if bytes.Contains(data, []byte(t020ClientID)) || bytes.Contains(data, []byte(t020BusinessNo)) {
				t.Fatal("t020 report retained a free-form client identifier")
			}
			streams := map[string]bool{}
			for _, log := range report.Logs {
				if log.Generation != 1 {
					t.Fatal("t020 report log generation mismatch")
				}
				if log.Stream != "stdout" && log.Stream != "stderr" {
					t.Fatal("t020 report log stream is invalid")
				}
				streams[log.Stream] = true
				if _, err := time.Parse(time.RFC3339Nano, log.Time); err != nil {
					t.Fatal("t020 report log time is invalid")
				}
				switch log.Level {
				case "info", "warn", "error":
				default:
					t.Fatal("t020 report log level is invalid")
				}
			}
			for _, stream := range tc.wantStreams {
				if !streams[stream] {
					t.Fatal("t020 report did not retain the expected stream")
				}
			}
		})
	}

	invalid := []struct {
		name   string
		mode   string
		faults []string
	}{
		{name: "unknown", mode: t020HelperModeUnknown, faults: []string{"output_protocol"}},
		{name: "duplicate", mode: t020HelperModeDuplicate, faults: []string{"output_protocol"}},
		{name: "null", mode: t020HelperModeNull, faults: []string{"output_protocol"}},
		{name: "wrong_type", mode: t020HelperModeWrong, faults: []string{"output_protocol"}},
		{name: "truncated", mode: t020HelperModeTrunc, faults: []string{"output_protocol", "process_incomplete"}},
		{name: "over_line", mode: t020HelperModeLogsOver, faults: []string{"output_limit"}},
		{name: "aggregate", mode: t020HelperModeAggregate, faults: []string{"output_limit"}},
		{name: "canary", mode: t020HelperModeCanary, faults: []string{"output_protocol"}},
		{name: "extra", mode: t020HelperModeExtra, faults: []string{"output_protocol"}},
		{name: "invalid_utf8", mode: t020HelperModeUTF8, faults: []string{"output_protocol"}},
	}
	for _, tc := range invalid {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			source := t020SourceFixture(t)
			artifacts, _ := t020BeginArtifacts(t, source)
			process, cancel := t020StartHelper(t, artifacts, 1, tc.mode, time.Second)
			defer cancel()
			ctx, waitCancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer waitCancel()
			exit, err := process.Wait(ctx)
			if err == nil {
				t.Fatal("invalid t020 log output was accepted")
			}
			if !errors.Is(err, ErrProcess) && !errors.Is(err, ErrArtifacts) {
				t.Fatal("invalid t020 log output error is not fixed")
			}
			if exit.OutputComplete {
				t.Fatal("invalid t020 log output is marked complete")
			}
			if !exit.Reaped || !exit.GroupGone {
				t.Fatal("invalid t020 log output did not reap the process")
			}
			path, finishErr := artifacts.Finish()
			if !errors.Is(finishErr, ErrArtifacts) {
				t.Fatal("finish after invalid t020 log output did not fail")
			}
			t020RequireReportFailure(t, path, tc.faults...)
			_, data := t020ReadReport(t, path)
			if bytes.Contains(data, []byte(t020Canary)) ||
				bytes.Contains(data, []byte("raw extra line")) {
				t.Fatal("t020 report leaked malformed output")
			}
		})
	}
}

func TestProcessFinishRequiresWait(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	process, cancel := t020StartHelper(t, artifacts, 1, t020HelperModeExit0, time.Second)
	defer cancel()
	path, err := artifacts.Finish()
	if !errors.Is(err, ErrArtifacts) {
		t.Fatal("t020 finish accepted an un-waited process")
	}
	report := t020RequireReportFailure(t, path, "process_incomplete")
	if report.CollectionComplete {
		t.Fatal("t020 un-waited process report claims complete collection")
	}
	ctx, waitCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer waitCancel()
	if _, err := process.Wait(ctx); err != nil {
		t.Fatal("wait t020 un-waited process failed")
	}
	if _, err := artifacts.Finish(); !errors.Is(err, ErrArtifacts) {
		t.Fatal("t020 latched finish recovered after wait")
	}
}

func TestProcessDuplicateGenerationRejected(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	process, cancel := t020StartHelper(t, artifacts, 1, t020HelperModeExit0, time.Second)
	defer cancel()
	ctx, waitCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer waitCancel()
	if _, err := process.Wait(ctx); err != nil {
		t.Fatal("wait first t020 process failed")
	}
	secondCtx, secondCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer secondCancel()
	second, err := StartProcess(secondCtx, ProcessConfig{
		Path: t020HelperPath(t), Dir: t020TempDir(t), Args: []string{t020HelperArgRun},
		Env: t020HelperEnv(t020HelperModeExit0), Generation: 1, Artifacts: artifacts,
	})
	if !errors.Is(err, ErrProcess) || second != nil {
		t020StopAndConfirm(t, second)
		t.Fatal("t020 duplicate process generation was accepted")
	}
	if _, err := artifacts.Finish(); err != nil {
		t.Fatal("finish t020 artifacts after duplicate generation failed")
	}
}

func TestProcessObserverAcrossGenerations(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	h := obsTestStart(t, obsTestBarrier)

	for _, generation := range []uint64{101, 102} {
		if err := h.obs.Expect(generation); err != nil {
			t.Fatal("expect t020 observer generation failed")
		}
		exit, result := t020RunObserverHelper(t, artifacts, h.socket, obsTestRunID, obsTestEpoch, generation, t020OrderID)
		t020RequireExit(t, exit, generation, 0, false, false, false)
		if result.Generation != generation || result.LastSeq != 2 ||
			result.RunID != obsTestRunID || result.Epoch != obsTestEpoch {
			t.Fatal("t020 helper observer receipt mismatch")
		}
		counts, err := h.obs.Snapshot()
		if err != nil {
			t.Fatal("snapshot t020 parent observer failed")
		}
		want := generation - 100
		if counts.Payment != want || counts.Query != want {
			t.Fatal("t020 parent observer counts mismatch")
		}
	}

	if err := artifacts.RecordObserver(h.obs); err != nil {
		t.Fatal("record t020 parent observer failed")
	}
	if err := h.close(); err != nil {
		t.Fatal("close t020 parent observer failed")
	}
	path, err := artifacts.Finish()
	if err != nil {
		t.Fatal("finish t020 two-generation artifacts failed")
	}
	report, data := t020ReadReport(t, path)
	if !report.CollectionComplete || len(report.Processes) != 2 || len(report.Logs) != 2 {
		t.Fatal("t020 two-generation report is incomplete")
	}
	if report.Observer == nil || report.Observer.Counts.Payment != 2 || report.Observer.Counts.Query != 2 ||
		len(report.Observer.Receipts) != 2 {
		t.Fatal("t020 two-generation observer report mismatch")
	}
	generations := map[uint64]bool{}
	for _, process := range report.Processes {
		generations[process.Generation] = true
	}
	if !generations[101] || !generations[102] {
		t.Fatal("t020 two-generation process generation mismatch")
	}
	for _, receipt := range report.Observer.Receipts {
		if receipt.RunID != obsTestRunID || receipt.Epoch != obsTestEpoch || receipt.LastSeq != 2 {
			t.Fatal("t020 two-generation observer receipt mismatch")
		}
	}
	if bytes.Contains(data, []byte(h.socket)) {
		t.Fatal("t020 report leaked the observer socket")
	}
}

func TestHarnessHelperProcess(t *testing.T) {
	if os.Getenv(t020HelperMarker) != "1" {
		return
	}
	mode := os.Getenv(t020HelperMode)
	switch mode {
	case t020HelperModeExit0:
		if !t020HelperWriteStdout(t020ValidLogLine()) {
			os.Exit(2)
		}
		os.Exit(0)
	case t020HelperModeExit3:
		if !t020HelperWriteStdout(t020ValidLogLine()) {
			os.Exit(2)
		}
		os.Exit(3)
	case t020HelperModeGraceful:
		ch := make(chan os.Signal, 1)
		signal.Notify(ch, syscall.SIGTERM)
		t020HelperReady()
		<-ch
		if !t020HelperWriteStdout(t020ValidLogLine()) {
			os.Exit(2)
		}
		os.Exit(0)
	case t020HelperModeIgnore:
		signal.Ignore(syscall.SIGTERM)
		t020HelperReady()
		for {
			time.Sleep(time.Hour)
		}
	case t020HelperModeGroupLeader, t020HelperModeGroupMember:
		signal.Ignore(syscall.SIGTERM)
		t020HelperGroupReady()
		for {
			time.Sleep(time.Hour)
		}
	case t020HelperModeObserver:
		t020HelperObserver()
	case t020HelperModeLogs:
		if !t020HelperWriteStdout(t020ValidLogLine()) ||
			!t020HelperWriteStderr(t020MinimalLogLine()) {
			os.Exit(2)
		}
		os.Exit(0)
	case t020HelperModeUnknown:
		if !t020HelperWriteStdout(t020UnknownLogLine()) {
			os.Exit(2)
		}
		os.Exit(0)
	case t020HelperModeDuplicate:
		if !t020HelperWriteStdout(t020DuplicateLogLine()) {
			os.Exit(2)
		}
		os.Exit(0)
	case t020HelperModeNull:
		if !t020HelperWriteStdout(t020NullLogLine()) {
			os.Exit(2)
		}
		os.Exit(0)
	case t020HelperModeWrong:
		if !t020HelperWriteStdout(t020WrongTypeLogLine()) {
			os.Exit(2)
		}
		os.Exit(0)
	case t020HelperModeTrunc:
		line := strings.TrimSuffix(t020MinimalLogLine(), "\n")
		if !t020HelperWriteStdout(line) {
			os.Exit(2)
		}
		os.Exit(0)
	case t020HelperModeLogsMax:
		line := t020PaddedLogLine(MaxLogLineBytes)
		if line == "" || !t020HelperWriteStdout(line) {
			os.Exit(2)
		}
		os.Exit(0)
	case t020HelperModeLogsOver:
		line := t020PaddedLogLine(MaxLogLineBytes + 1)
		if line == "" || !t020HelperWriteStdout(line) {
			os.Exit(2)
		}
		os.Exit(0)
	case t020HelperModeAggregate:
		for i := 0; i < 500; i++ {
			if !t020HelperWriteStdout(t020MinimalLogLine()) {
				os.Exit(2)
			}
		}
		for i := 0; i < 500; i++ {
			if !t020HelperWriteStderr(t020MinimalLogLine()) {
				os.Exit(2)
			}
		}
		os.Exit(0)
	case t020HelperModeCanary:
		if !t020HelperWriteStdout(t020CanaryLogLine()) {
			os.Exit(2)
		}
		os.Exit(0)
	case t020HelperModeExtra:
		if !t020HelperWriteStdout(t020MinimalLogLine()) ||
			!t020HelperWriteStdout("raw extra line\n") {
			os.Exit(2)
		}
		os.Exit(0)
	case t020HelperModeUTF8:
		if !t020HelperWriteStdout(t020InvalidUTF8LogLine()) {
			os.Exit(2)
		}
		os.Exit(0)
	default:
		os.Exit(2)
	}
}

func t020HelperWriteStdout(value string) bool {
	n, err := os.Stdout.WriteString(value)
	return err == nil && n == len(value)
}

func t020HelperWriteStderr(value string) bool {
	n, err := os.Stderr.WriteString(value)
	return err == nil && n == len(value)
}

func t020HelperReady() {
	path := os.Getenv(t020HelperReadySocket)
	conn, err := net.Dial("unix", path)
	if err != nil {
		os.Exit(2)
	}
	message := []byte("ready\n")
	n, err := conn.Write(message)
	if err != nil || n != len(message) {
		_ = conn.Close()
		os.Exit(2)
	}
	if err := conn.Close(); err != nil {
		os.Exit(2)
	}
}

func t020HelperGroupReady() {
	path := os.Getenv(t020HelperReadySocket)
	conn, err := net.Dial("unix", path)
	if err != nil {
		os.Exit(2)
	}
	message := []byte("ready pid=" + strconv.Itoa(os.Getpid()) + " pgid=" + strconv.Itoa(syscall.Getpgrp()) + "\n")
	n, err := conn.Write(message)
	if err != nil || n != len(message) {
		_ = conn.Close()
		os.Exit(2)
	}
	if err := conn.Close(); err != nil {
		os.Exit(2)
	}
}

func t020HelperObserver() {
	generation, err := strconv.ParseUint(os.Getenv(t020HelperGeneration), 10, 64)
	if err != nil || generation == 0 {
		os.Exit(2)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	client, err := testcontrol.Dial(ctx, testcontrol.Session{
		Socket:     os.Getenv(t020HelperSocket),
		RunID:      os.Getenv(t020HelperRunID),
		Epoch:      os.Getenv(t020HelperEpoch),
		Generation: generation,
	})
	if err != nil {
		_ = t020HelperWriteResult(false, generation, 0, "", "")
		os.Exit(2)
	}
	orderID := os.Getenv(t020HelperOrderID)
	if err := client.Enter(ctx, testcontrol.KindPayment, orderID); err != nil {
		_ = client.Close()
		_ = t020HelperWriteResult(false, generation, 0, "", "")
		os.Exit(2)
	}
	if err := client.Enter(ctx, testcontrol.KindQuery, orderID); err != nil {
		_ = client.Close()
		_ = t020HelperWriteResult(false, generation, 0, "", "")
		os.Exit(2)
	}
	receipt, err := client.Seal(ctx)
	closeErr := client.Close()
	if err != nil || closeErr != nil {
		_ = t020HelperWriteResult(false, generation, 0, "", "")
		os.Exit(2)
	}
	if err := t020HelperWriteResult(true, receipt.Generation, receipt.LastSeq, receipt.RunID, receipt.Epoch); err != nil {
		os.Exit(2)
	}
	if !t020HelperWriteStdout(t020MinimalLogLine()) {
		os.Exit(2)
	}
	os.Exit(0)
}

func t020HelperWriteResult(ok bool, generation, lastSeq uint64, runID, epoch string) error {
	path := os.Getenv(t020HelperResultPath)
	if path == "" {
		return errors.New("missing helper result path")
	}
	data, err := json.Marshal(t020HelperResult{
		OK: ok, Generation: generation, LastSeq: lastSeq, RunID: runID, Epoch: epoch,
	})
	if err != nil {
		return err
	}
	return os.WriteFile(path, data, 0o600)
}

func t020ValidLogLine() string {
	return `{"time":"` + time.Now().UTC().Format(time.RFC3339Nano) +
		`","level":"info","event":"request","stage":"entry","request_id":"` + t020RequestID +
		`","client_id":"` + t020ClientID + `","business_order_no":"` + t020BusinessNo +
		`","payment_order_id":"` + t020PaymentID +
		`","result_before":"UNKNOWN","result_after":"SUCCEEDED","reason":"internal","error_kind":"internal"}` + "\n"
}

func t020MinimalLogLine() string {
	return `{"time":"` + time.Now().UTC().Format(time.RFC3339Nano) +
		`","level":"info","event":"trace","stage":"entry"}` + "\n"
}

func t020UnknownLogLine() string {
	return `{"time":"` + time.Now().UTC().Format(time.RFC3339Nano) +
		`","level":"info","event":"request","stage":"entry","unknown":"` + t020Canary + `"}` + "\n"
}

func t020DuplicateLogLine() string {
	return `{"time":"` + time.Now().UTC().Format(time.RFC3339Nano) +
		`","level":"info","level":"warn","event":"request","stage":"entry"}` + "\n"
}

func t020NullLogLine() string {
	return `{"time":"` + time.Now().UTC().Format(time.RFC3339Nano) +
		`","level":null,"event":"request","stage":"entry"}` + "\n"
}

func t020WrongTypeLogLine() string {
	return `{"time":"` + time.Now().UTC().Format(time.RFC3339Nano) +
		`","level":7,"event":"request","stage":"entry"}` + "\n"
}

func t020CanaryLogLine() string {
	return `{"time":"` + time.Now().UTC().Format(time.RFC3339Nano) +
		`","level":"info","event":"request","stage":"entry","note":"` + t020Canary + `"}` + "\n"
}

func t020InvalidUTF8LogLine() string {
	return `{"time":"` + time.Now().UTC().Format(time.RFC3339Nano) +
		`","level":"info","event":"request","stage":"entry","client_id":"client-` +
		string([]byte{0xff}) + `"}` + "\n"
}

func t020PaddedLogLine(total int) string {
	base := strings.TrimSuffix(t020MinimalLogLine(), "\n")
	if total <= 1 || len(base) >= total {
		return ""
	}
	padding := total - 1 - len(base)
	return "{" + strings.Repeat(" ", padding) + strings.TrimPrefix(base, "{") + "\n"
}
