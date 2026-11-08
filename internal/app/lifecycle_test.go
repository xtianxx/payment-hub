package app

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"payment-hub/internal/testcontrol"
	"payment-hub/tests/harness"
)

const (
	lifecycleTestClientID    = "lifecycle-client"
	lifecycleTestAPIKey      = "FAKE-demo_key.9~a+/b=="
	lifecycleTestDatabase    = "postgres://lifecycle_user:fake-password@127.0.0.1:5432/payment_hub_test?sslmode=disable"
	lifecycleTestHTTPAddr    = "127.0.0.1:0"
	lifecycleTestCanary      = "FAKE_T021_LIFECYCLE_CANARY"
	lifecycleTestRunID       = "0123456789abcdef0123456789abcdef"
	lifecycleTestEpoch       = "fedcba9876543210fedcba9876543210"
	lifecycleTestModeEnv     = "PAYMENT_HUB_TEST_MODE"
	lifecycleTestSocketEnv   = "PAYMENT_HUB_TEST_CONTROL_SOCKET"
	lifecycleTestRunEnv      = "PAYMENT_HUB_TEST_RUN_ID"
	lifecycleTestEpochEnv    = "PAYMENT_HUB_TEST_OBSERVER_EPOCH"
	lifecycleTestGenEnv      = "PAYMENT_HUB_TEST_GENERATION"
	lifecycleTestIdentityID  = "PAYMENT_HUB_CLIENT_ID"
	lifecycleTestAPIKeyEnv   = "PAYMENT_HUB_API_KEY"
	lifecycleTestDatabaseEnv = "PAYMENT_HUB_DATABASE_URL"
	lifecycleTestAddrEnv     = "PAYMENT_HUB_HTTP_ADDR"
)

func lifecycleTestBaseValues() map[string]string {
	return map[string]string{
		lifecycleTestIdentityID:  lifecycleTestClientID,
		lifecycleTestAPIKeyEnv:   lifecycleTestAPIKey,
		lifecycleTestDatabaseEnv: lifecycleTestDatabase,
		lifecycleTestAddrEnv:     lifecycleTestHTTPAddr,
	}
}

func lifecycleTestLookup(values map[string]string) func(string) (string, bool) {
	return func(name string) (string, bool) {
		value, ok := values[name]
		return value, ok
	}
}

func lifecycleTestLimits() runtimeLimits {
	return runtimeLimits{
		startup:      2 * time.Second,
		probe:        100 * time.Millisecond,
		read:         200 * time.Millisecond,
		write:        300 * time.Millisecond,
		shutdown:     500 * time.Millisecond,
		closeReserve: 50 * time.Millisecond,
		retry:        10 * time.Millisecond,
	}
}

func lifecycleTestClearPGEnv(t *testing.T) {
	t.Helper()
	for _, entry := range os.Environ() {
		name, _, ok := strings.Cut(entry, "=")
		if ok && strings.HasPrefix(name, "PG") {
			t.Setenv(name, "")
		}
	}
}

func lifecycleTestWaitError(t *testing.T, result <-chan error, timeout time.Duration) error {
	t.Helper()
	select {
	case err := <-result:
		return err
	case <-time.After(timeout):
		t.Fatal("lifecycle test timed out waiting for runWith")
		return nil
	}
}

func lifecycleTestWaitSignal(t *testing.T, signal <-chan struct{}, message string) {
	t.Helper()
	select {
	case <-signal:
	case <-time.After(2 * time.Second):
		t.Fatal(message)
	}
}

func lifecycleTestStartObserver(t *testing.T) (*harness.Observer, string) {
	t.Helper()
	dir, err := os.MkdirTemp("", "lifecycle-obs-")
	if err != nil {
		t.Fatal("create lifecycle observer directory")
	}
	if err := os.Chmod(dir, 0o700); err != nil {
		t.Fatal("secure lifecycle observer directory")
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(dir); err != nil {
			t.Errorf("remove lifecycle observer directory")
		}
	})
	socket := filepath.Join(dir, "s")
	observer, err := harness.StartObserver(context.Background(), harness.ObserverConfig{
		Socket:         socket,
		RunID:          lifecycleTestRunID,
		Epoch:          lifecycleTestEpoch,
		BarrierTimeout: time.Second,
	})
	if err != nil {
		t.Fatal("start lifecycle observer")
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		if err := observer.Close(ctx); err != nil {
			t.Error("close lifecycle observer failed")
		}
	})
	if err := observer.Expect(1); err != nil {
		t.Fatal("expect lifecycle observer generation")
	}
	return observer, socket
}

type lifecycleTestOrder struct {
	mu     sync.Mutex
	events []string
}

func (order *lifecycleTestOrder) add(event string) {
	order.mu.Lock()
	order.events = append(order.events, event)
	order.mu.Unlock()
}

func (order *lifecycleTestOrder) before(first, second string) bool {
	order.mu.Lock()
	defer order.mu.Unlock()
	firstIndex := -1
	secondIndex := -1
	for index, event := range order.events {
		if event == first && firstIndex < 0 {
			firstIndex = index
		}
		if event == second && secondIndex < 0 {
			secondIndex = index
		}
	}
	return firstIndex >= 0 && secondIndex >= 0 && firstIndex < secondIndex
}

type lifecycleTestDB struct {
	mu            sync.Mutex
	pingCalls     int
	checkCalls    int
	closeCalls    int
	pingDeadlines []time.Time
	pingFn        func(context.Context) error
	checkFn       func(context.Context) error
	closeFn       func()
	closeOnce     sync.Once
	closeDone     chan struct{}
}

func newLifecycleTestDB() *lifecycleTestDB {
	return &lifecycleTestDB{closeDone: make(chan struct{})}
}

func (db *lifecycleTestDB) Ping(ctx context.Context) error {
	db.mu.Lock()
	db.pingCalls++
	if deadline, ok := ctx.Deadline(); ok {
		db.pingDeadlines = append(db.pingDeadlines, deadline)
	}
	fn := db.pingFn
	db.mu.Unlock()
	if fn != nil {
		return fn(ctx)
	}
	return nil
}

func (db *lifecycleTestDB) CheckMigrations(ctx context.Context) error {
	db.mu.Lock()
	db.checkCalls++
	fn := db.checkFn
	db.mu.Unlock()
	if fn != nil {
		return fn(ctx)
	}
	return nil
}

func (db *lifecycleTestDB) Close() {
	db.mu.Lock()
	db.closeCalls++
	fn := db.closeFn
	db.mu.Unlock()
	if fn != nil {
		fn()
	}
	db.closeOnce.Do(func() {
		close(db.closeDone)
	})
}

func (db *lifecycleTestDB) snapshot() (ping, check, close int, deadlines []time.Time) {
	db.mu.Lock()
	defer db.mu.Unlock()
	deadlines = append(deadlines, db.pingDeadlines...)
	return db.pingCalls, db.checkCalls, db.closeCalls, deadlines
}

type lifecycleTestListener struct {
	addr   net.Addr
	closed chan struct{}
	once   sync.Once
}

func newLifecycleTestListener() *lifecycleTestListener {
	return &lifecycleTestListener{
		addr:   &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 0},
		closed: make(chan struct{}),
	}
}

func (listener *lifecycleTestListener) Accept() (net.Conn, error) {
	<-listener.closed
	return nil, net.ErrClosed
}

func (listener *lifecycleTestListener) Close() error {
	listener.once.Do(func() {
		close(listener.closed)
	})
	return nil
}

func (listener *lifecycleTestListener) Addr() net.Addr {
	return listener.addr
}

type lifecycleTestServer struct {
	order           *lifecycleTestOrder
	started         chan struct{}
	stop            chan struct{}
	startOnce       sync.Once
	stopOnce        sync.Once
	shutdownStarted chan struct{}
	shutdownOnce    sync.Once
	mu              sync.Mutex
	shutdownCalls   int
	closeCalls      int
	shutdownFn      func(context.Context) error
	closeFn         func() error
}

func newLifecycleTestServer(order *lifecycleTestOrder) *lifecycleTestServer {
	return &lifecycleTestServer{
		order:           order,
		started:         make(chan struct{}),
		stop:            make(chan struct{}),
		shutdownStarted: make(chan struct{}),
	}
}

func (server *lifecycleTestServer) Serve(net.Listener) error {
	server.order.add("server.serve")
	server.startOnce.Do(func() {
		close(server.started)
	})
	<-server.stop
	return http.ErrServerClosed
}

func (server *lifecycleTestServer) Shutdown(ctx context.Context) error {
	server.mu.Lock()
	server.shutdownCalls++
	server.mu.Unlock()
	server.order.add("server.shutdown")
	server.shutdownOnce.Do(func() {
		close(server.shutdownStarted)
	})
	if server.shutdownFn != nil {
		return server.shutdownFn(ctx)
	}
	server.stopOnce.Do(func() {
		close(server.stop)
	})
	return nil
}

func (server *lifecycleTestServer) Close() error {
	server.mu.Lock()
	server.closeCalls++
	server.mu.Unlock()
	server.order.add("server.close")
	server.stopOnce.Do(func() {
		close(server.stop)
	})
	if server.closeFn != nil {
		return server.closeFn()
	}
	return nil
}

func (server *lifecycleTestServer) snapshot() (shutdown, close int) {
	server.mu.Lock()
	defer server.mu.Unlock()
	return server.shutdownCalls, server.closeCalls
}

type lifecycleTestRealServer struct {
	server  *http.Server
	started chan struct{}
	once    sync.Once
}

func (server *lifecycleTestRealServer) Serve(listener net.Listener) error {
	server.once.Do(func() {
		close(server.started)
	})
	return server.server.Serve(listener)
}

func (server *lifecycleTestRealServer) Shutdown(ctx context.Context) error {
	return server.server.Shutdown(ctx)
}

func (server *lifecycleTestRealServer) Close() error {
	return server.server.Close()
}

type lifecycleTestDeps struct {
	mu           sync.Mutex
	openCalls    int
	listenCalls  int
	openDatabase func(context.Context, string) (runtimeDatabase, error)
	listen       func(context.Context, string) (net.Listener, error)
	newServer    func(*http.Server) runtimeServer
}

func (deps *lifecycleTestDeps) deps() runtimeDeps {
	return runtimeDeps{
		openDatabase: func(ctx context.Context, dsn string) (runtimeDatabase, error) {
			deps.mu.Lock()
			deps.openCalls++
			deps.mu.Unlock()
			return deps.openDatabase(ctx, dsn)
		},
		listen: func(ctx context.Context, addr string) (net.Listener, error) {
			deps.mu.Lock()
			deps.listenCalls++
			deps.mu.Unlock()
			return deps.listen(ctx, addr)
		},
		newServer: deps.newServer,
	}
}

func (deps *lifecycleTestDeps) snapshot() (open, listen int) {
	deps.mu.Lock()
	defer deps.mu.Unlock()
	return deps.openCalls, deps.listenCalls
}

type lifecycleTestBlockingWriter struct {
	mu        sync.Mutex
	calls     int
	first     chan struct{}
	release   chan struct{}
	more      chan struct{}
	done      chan struct{}
	firstOnce sync.Once
	moreOnce  sync.Once
	doneOnce  sync.Once
}

func newLifecycleTestBlockingWriter() *lifecycleTestBlockingWriter {
	return &lifecycleTestBlockingWriter{
		first:   make(chan struct{}),
		release: make(chan struct{}),
		more:    make(chan struct{}),
		done:    make(chan struct{}),
	}
}

func (writer *lifecycleTestBlockingWriter) Write(payload []byte) (int, error) {
	writer.mu.Lock()
	writer.calls++
	call := writer.calls
	writer.mu.Unlock()
	if call == 1 {
		writer.firstOnce.Do(func() {
			close(writer.first)
		})
		<-writer.release
		writer.doneOnce.Do(func() {
			close(writer.done)
		})
	}
	if call > 1 {
		writer.moreOnce.Do(func() {
			close(writer.more)
		})
	}
	return len(payload), nil
}

func (writer *lifecycleTestBlockingWriter) snapshot() int {
	writer.mu.Lock()
	defer writer.mu.Unlock()
	return writer.calls
}

func TestDefaultRuntimeLimits(t *testing.T) {
	limits := defaultRuntimeLimits()
	if limits.startup != 30*time.Second ||
		limits.probe != time.Second ||
		limits.read != 5*time.Second ||
		limits.write != 20*time.Second ||
		limits.shutdown != 20*time.Second ||
		limits.closeReserve != 2*time.Second ||
		limits.retry != 100*time.Millisecond {
		t.Fatal("default runtime limits do not match the frozen budget")
	}
}

func TestRunWithRejectsInvalidConfigurationBeforeOpen(t *testing.T) {
	baseLimits := lifecycleTestLimits()
	cases := []struct {
		name      string
		nilCtx    bool
		nilLookup bool
		nilWriter bool
		nilDeps   bool
		mutate    func(map[string]string)
		pgEnv     string
		limits    func(runtimeLimits) runtimeLimits
	}{
		{name: "nil context", nilCtx: true},
		{name: "nil lookup", nilLookup: true},
		{name: "nil writer", nilWriter: true},
		{name: "missing client identity", mutate: func(values map[string]string) { delete(values, lifecycleTestIdentityID) }},
		{name: "missing api key", mutate: func(values map[string]string) { delete(values, lifecycleTestAPIKeyEnv) }},
		{name: "invalid database URI", mutate: func(values map[string]string) { values[lifecycleTestDatabaseEnv] = lifecycleTestCanary }},
		{name: "invalid http address", mutate: func(values map[string]string) { values[lifecycleTestAddrEnv] = "127.0.0.1" }},
		{name: "invalid test mode", mutate: func(values map[string]string) { values[lifecycleTestModeEnv] = "on" }},
		{name: "polluted PG environment", pgEnv: "PGSERVICE"},
		{name: "nil dependencies", nilDeps: true},
		{name: "zero startup limit", limits: func(limits runtimeLimits) runtimeLimits { limits.startup = 0; return limits }},
		{name: "negative probe limit", limits: func(limits runtimeLimits) runtimeLimits { limits.probe = -time.Second; return limits }},
		{name: "reserve reaches shutdown", limits: func(limits runtimeLimits) runtimeLimits { limits.closeReserve = limits.shutdown; return limits }},
		{name: "zero retry limit", limits: func(limits runtimeLimits) runtimeLimits { limits.retry = 0; return limits }},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			lifecycleTestClearPGEnv(t)
			if tc.pgEnv != "" {
				t.Setenv(tc.pgEnv, "evil")
			}
			values := lifecycleTestBaseValues()
			if tc.mutate != nil {
				tc.mutate(values)
			}
			var ctx context.Context = context.Background()
			if tc.nilCtx {
				ctx = nil
			}
			var lookup func(string) (string, bool) = lifecycleTestLookup(values)
			if tc.nilLookup {
				lookup = nil
			}
			var writer io.Writer = io.Discard
			if tc.nilWriter {
				writer = nil
			}
			limits := baseLimits
			if tc.limits != nil {
				limits = tc.limits(limits)
			}
			var countersMu sync.Mutex
			opened := 0
			listened := 0
			var deps runtimeDeps
			if !tc.nilDeps {
				deps = runtimeDeps{
					openDatabase: func(context.Context, string) (runtimeDatabase, error) {
						countersMu.Lock()
						opened++
						countersMu.Unlock()
						return nil, errors.New("unexpected open")
					},
					listen: func(context.Context, string) (net.Listener, error) {
						countersMu.Lock()
						listened++
						countersMu.Unlock()
						return nil, errors.New("unexpected listen")
					},
					newServer: func(*http.Server) runtimeServer {
						return newLifecycleTestServer(&lifecycleTestOrder{})
					},
				}
			}
			err := runWith(ctx, lookup, writer, deps, limits)
			if !errors.Is(err, ErrRuntimeConfig) {
				t.Fatal("invalid runtime configuration did not return ErrRuntimeConfig")
			}
			countersMu.Lock()
			openedCount, listenedCount := opened, listened
			countersMu.Unlock()
			if openedCount != 0 || listenedCount != 0 {
				t.Fatal("invalid runtime configuration reached open or listen")
			}
			if strings.Contains(err.Error(), lifecycleTestCanary) {
				t.Fatal("runtime configuration error echoed rejected input")
			}
		})
	}
}

func TestRunWithStartupAndShutdownOrder(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	order := &lifecycleTestOrder{}
	db := newLifecycleTestDB()
	db.pingFn = func(context.Context) error {
		order.add("db.ping")
		return nil
	}
	db.checkFn = func(context.Context) error {
		order.add("db.check")
		return nil
	}
	db.closeFn = func() {
		order.add("db.close")
	}
	listener := newLifecycleTestListener()
	server := newLifecycleTestServer(order)
	deps := &lifecycleTestDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) {
			order.add("db.open")
			return db, nil
		},
		listen: func(context.Context, string) (net.Listener, error) {
			order.add("listen")
			return listener, nil
		},
		newServer: func(*http.Server) runtimeServer {
			order.add("server.new")
			return server
		},
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	t.Cleanup(func() {
		_ = listener.Close()
	})
	result := make(chan error, 1)
	go func() {
		result <- runWith(ctx, lifecycleTestLookup(lifecycleTestBaseValues()), io.Discard, deps.deps(), lifecycleTestLimits())
	}()
	lifecycleTestWaitSignal(t, server.started, "runtime server did not start")
	cancel()
	if err := lifecycleTestWaitError(t, result, 2*time.Second); err != nil {
		t.Fatal("normal runtime shutdown returned an error")
	}
	openCalls, listenCalls := deps.snapshot()
	if openCalls != 1 || listenCalls != 1 {
		t.Fatal("runtime did not open exactly one database and listener")
	}
	pingCalls, checkCalls, closeCalls, _ := db.snapshot()
	if pingCalls == 0 || checkCalls != 1 || closeCalls != 1 {
		t.Fatal("runtime startup or pool close did not run exactly once")
	}
	if !order.before("db.open", "db.ping") ||
		!order.before("db.ping", "db.check") ||
		!order.before("db.check", "listen") ||
		!order.before("listen", "server.serve") ||
		!order.before("server.shutdown", "db.close") {
		t.Fatal("runtime startup or shutdown order is incorrect")
	}
}

func TestRunWithPingRetriesShareStartupDeadline(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	db := newLifecycleTestDB()
	var mu sync.Mutex
	attempts := 0
	db.pingFn = func(context.Context) error {
		mu.Lock()
		attempts++
		current := attempts
		mu.Unlock()
		if current < 3 {
			return errors.New("fake ping failure")
		}
		return nil
	}
	listener := newLifecycleTestListener()
	server := newLifecycleTestServer(&lifecycleTestOrder{})
	deps := &lifecycleTestDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
		listen:       func(context.Context, string) (net.Listener, error) { return listener, nil },
		newServer:    func(*http.Server) runtimeServer { return server },
	}
	limits := lifecycleTestLimits()
	limits.startup = 500 * time.Millisecond
	limits.probe = 50 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	t.Cleanup(func() {
		_ = listener.Close()
	})
	result := make(chan error, 1)
	go func() {
		result <- runWith(ctx, lifecycleTestLookup(lifecycleTestBaseValues()), io.Discard, deps.deps(), limits)
	}()
	lifecycleTestWaitSignal(t, server.started, "runtime server did not start after ping retry")
	cancel()
	if err := lifecycleTestWaitError(t, result, 2*time.Second); err != nil {
		t.Fatal("runtime with retrying ping returned an error")
	}
	pingCalls, _, _, deadlines := db.snapshot()
	if pingCalls < 3 {
		t.Fatal("runtime did not retry the startup ping")
	}
	if len(deadlines) < 2 {
		t.Fatal("startup ping retries did not record deadlines")
	}
	for _, deadline := range deadlines[1:] {
		if !deadline.Equal(deadlines[0]) {
			t.Fatal("startup ping retries did not share one deadline")
		}
	}
}

func TestRunWithMigrationFailureDoesNotListen(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	db := newLifecycleTestDB()
	db.checkFn = func(context.Context) error {
		return errors.New(lifecycleTestCanary + " SELECT * FROM goose_db_version")
	}
	listener := newLifecycleTestListener()
	server := newLifecycleTestServer(&lifecycleTestOrder{})
	newServerCalls := 0
	deps := &lifecycleTestDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
		listen:       func(context.Context, string) (net.Listener, error) { return listener, nil },
		newServer: func(*http.Server) runtimeServer {
			newServerCalls++
			return server
		},
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var logs bytes.Buffer
	result := make(chan error, 1)
	go func() {
		result <- runWith(ctx, lifecycleTestLookup(lifecycleTestBaseValues()), &logs, deps.deps(), lifecycleTestLimits())
	}()
	err := lifecycleTestWaitError(t, result, 2*time.Second)
	if !errors.Is(err, ErrStartup) {
		t.Fatal("migration check failure did not return ErrStartup")
	}
	if strings.Contains(err.Error(), lifecycleTestCanary) {
		t.Fatal("migration failure echoed a raw error")
	}
	_, listenCalls := deps.snapshot()
	_, _, closeCalls, _ := db.snapshot()
	if listenCalls != 0 || newServerCalls != 0 {
		t.Fatal("migration check failure reached listen or server creation")
	}
	if closeCalls != 1 {
		t.Fatal("migration check failure did not close the opened pool")
	}

	logText := logs.String()
	if strings.Contains(logText, lifecycleTestCanary) ||
		strings.Contains(logText, "SELECT") ||
		strings.Contains(logText, "goose_db_version") {
		t.Fatal("migration failure log echoed SQL or the raw error")
	}
	dependencyEvents := 0
	for _, line := range strings.Split(strings.TrimSpace(logText), "\n") {
		if line == "" {
			continue
		}
		var entry map[string]any
		if err := json.Unmarshal([]byte(line), &entry); err != nil {
			t.Fatal("migration failure log line is not JSON")
		}
		if entry["event"] != "dependency" || entry["stage"] != "persistence" {
			continue
		}
		if entry["level"] != "error" {
			continue
		}
		dependencyEvents++
		if entry["reason"] != "dependency" {
			t.Fatal("migration dependency event has the wrong reason")
		}
		errorKind, _ := entry["error_kind"].(string)
		switch errorKind {
		case "timeout", "canceled", "database", "http", "internal":
		default:
			t.Fatal("migration dependency event lacks a fixed error classification")
		}
	}
	if dependencyEvents != 1 {
		t.Fatal("migration failure did not produce exactly one dependency/persistence event")
	}
}

func TestRunWithStartupCleanupIsBounded(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	db := newLifecycleTestDB()
	db.checkFn = func(context.Context) error {
		return errors.New(lifecycleTestCanary)
	}
	closeStarted := make(chan struct{})
	releaseClose := make(chan struct{})
	var releaseOnce sync.Once
	release := func() {
		releaseOnce.Do(func() {
			close(releaseClose)
		})
	}
	t.Cleanup(release)
	db.closeFn = func() {
		close(closeStarted)
		<-releaseClose
	}
	listener := newLifecycleTestListener()
	server := newLifecycleTestServer(&lifecycleTestOrder{})
	deps := &lifecycleTestDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
		listen:       func(context.Context, string) (net.Listener, error) { return listener, nil },
		newServer:    func(*http.Server) runtimeServer { return server },
	}
	limits := lifecycleTestLimits()
	limits.startup = 150 * time.Millisecond
	limits.shutdown = time.Second
	limits.closeReserve = 50 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	start := time.Now()
	result := make(chan error, 1)
	go func() {
		result <- runWith(ctx, lifecycleTestLookup(lifecycleTestBaseValues()), io.Discard, deps.deps(), limits)
	}()
	lifecycleTestWaitSignal(t, closeStarted, "startup failure did not start pool cleanup")
	err := lifecycleTestWaitError(t, result, 2*time.Second)
	if !errors.Is(err, ErrStartup) {
		t.Fatal("startup failure did not return ErrStartup")
	}
	if elapsed := time.Since(start); elapsed >= time.Second {
		t.Fatal("startup cleanup exceeded the startup-remaining supervision budget")
	}
	release()
	select {
	case <-db.closeDone:
	case <-time.After(time.Second):
		t.Fatal("startup pool cleanup did not finish after release")
	}
}

func TestRunWithShutdownTimeoutDoesNotClosePoolWhileDBUserActive(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	order := &lifecycleTestOrder{}
	db := newLifecycleTestDB()
	var pingMu sync.Mutex
	pingCount := 0
	pingStarted := make(chan struct{})
	var pingStartedOnce sync.Once
	releasePing := make(chan struct{})
	var releasePingOnce sync.Once
	releasePingFunc := func() {
		releasePingOnce.Do(func() {
			close(releasePing)
		})
	}
	t.Cleanup(releasePingFunc)
	db.pingFn = func(context.Context) error {
		pingMu.Lock()
		pingCount++
		current := pingCount
		pingMu.Unlock()
		if current == 1 {
			return nil
		}
		pingStartedOnce.Do(func() {
			close(pingStarted)
		})
		<-releasePing
		return nil
	}
	db.closeFn = func() {
		order.add("db.close")
	}
	server := newLifecycleTestServer(order)
	server.shutdownFn = func(ctx context.Context) error {
		<-ctx.Done()
		return ctx.Err()
	}
	var captured *http.Server
	listener := newLifecycleTestListener()
	deps := &lifecycleTestDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
		listen:       func(context.Context, string) (net.Listener, error) { return listener, nil },
		newServer: func(srv *http.Server) runtimeServer {
			captured = srv
			return server
		},
	}
	limits := lifecycleTestLimits()
	limits.shutdown = 150 * time.Millisecond
	limits.closeReserve = 50 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	t.Cleanup(func() {
		_ = listener.Close()
	})
	result := make(chan error, 1)
	go func() {
		result <- runWith(ctx, lifecycleTestLookup(lifecycleTestBaseValues()), io.Discard, deps.deps(), limits)
	}()
	lifecycleTestWaitSignal(t, server.started, "runtime server did not start for active-user test")
	if captured == nil || captured.Handler == nil {
		t.Fatal("runtime server handler was not captured")
	}
	activeDone := make(chan struct{})
	go func() {
		recorder := httptest.NewRecorder()
		captured.Handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/readyz", nil))
		close(activeDone)
	}()
	lifecycleTestWaitSignal(t, pingStarted, "readyz checker did not enter the active DB call")
	cancel()
	lifecycleTestWaitSignal(t, server.shutdownStarted, "runtime did not start shutdown")
	pingMu.Lock()
	pingBeforeDirect := pingCount
	pingMu.Unlock()

	directDone := make(chan int, 1)
	go func() {
		recorder := httptest.NewRecorder()
		captured.Handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/readyz", nil))
		directDone <- recorder.Code
	}()
	select {
	case code := <-directDone:
		if code != http.StatusServiceUnavailable {
			t.Fatal("new readyz request was not rejected after admission closed")
		}
	case <-time.After(500 * time.Millisecond):
		t.Fatal("new readyz request blocked after admission closed")
	}
	pingMu.Lock()
	pingAfterDirect := pingCount
	pingMu.Unlock()
	if pingAfterDirect != pingBeforeDirect {
		t.Fatal("new readyz request reached the DB after admission closed")
	}

	err := lifecycleTestWaitError(t, result, 2*time.Second)
	if !errors.Is(err, ErrShutdown) {
		t.Fatal("active DB user did not produce ErrShutdown")
	}
	if shutdownCalls, closeCalls := server.snapshot(); shutdownCalls == 0 || closeCalls == 0 {
		t.Fatal("shutdown timeout did not force server close")
	}
	if _, _, closeCalls, _ := db.snapshot(); closeCalls != 0 {
		t.Fatal("runtime closed the pool while a DB user was still active")
	}
	releasePingFunc()
	select {
	case <-activeDone:
	case <-time.After(time.Second):
		t.Fatal("active readyz handler did not finish after release")
	}
	if _, _, closeCalls, _ := db.snapshot(); closeCalls != 0 {
		t.Fatal("runtime closed the pool after reporting an unconfirmed active user")
	}
}

func TestRunWithPoolCloseBlockingIsBounded(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	order := &lifecycleTestOrder{}
	db := newLifecycleTestDB()
	closeStarted := make(chan struct{})
	releaseClose := make(chan struct{})
	var releaseOnce sync.Once
	release := func() {
		releaseOnce.Do(func() {
			close(releaseClose)
		})
	}
	t.Cleanup(release)
	db.closeFn = func() {
		order.add("db.close")
		close(closeStarted)
		<-releaseClose
	}
	listener := newLifecycleTestListener()
	server := newLifecycleTestServer(order)
	deps := &lifecycleTestDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
		listen:       func(context.Context, string) (net.Listener, error) { return listener, nil },
		newServer:    func(*http.Server) runtimeServer { return server },
	}
	limits := lifecycleTestLimits()
	limits.shutdown = 150 * time.Millisecond
	limits.closeReserve = 50 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	t.Cleanup(func() {
		_ = listener.Close()
	})
	result := make(chan error, 1)
	go func() {
		result <- runWith(ctx, lifecycleTestLookup(lifecycleTestBaseValues()), io.Discard, deps.deps(), limits)
	}()
	lifecycleTestWaitSignal(t, server.started, "runtime server did not start for pool close test")
	cancel()
	lifecycleTestWaitSignal(t, closeStarted, "runtime did not start pool close")
	err := lifecycleTestWaitError(t, result, 2*time.Second)
	if !errors.Is(err, ErrShutdown) {
		t.Fatal("blocked pool close did not produce ErrShutdown")
	}
	release()
	select {
	case <-db.closeDone:
	case <-time.After(time.Second):
		t.Fatal("pool close did not finish after release")
	}
	if _, _, closeCalls, _ := db.snapshot(); closeCalls != 1 {
		t.Fatal("pool close was not attempted exactly once")
	}
	if !order.before("server.shutdown", "db.close") {
		t.Fatal("pool close ran before server shutdown")
	}
}

func TestRunWithWriterBlockingIsBounded(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	writer := newLifecycleTestBlockingWriter()
	var releaseOnce sync.Once
	release := func() {
		releaseOnce.Do(func() {
			close(writer.release)
		})
	}
	t.Cleanup(release)
	t.Cleanup(func() {
		release()
		select {
		case <-writer.done:
		case <-time.After(time.Second):
			t.Error("blocked writer did not return after cleanup release")
		}
	})
	db := newLifecycleTestDB()
	listener := newLifecycleTestListener()
	server := newLifecycleTestServer(&lifecycleTestOrder{})
	deps := &lifecycleTestDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
		listen:       func(context.Context, string) (net.Listener, error) { return listener, nil },
		newServer:    func(*http.Server) runtimeServer { return server },
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	t.Cleanup(func() {
		_ = listener.Close()
	})
	result := make(chan error, 1)
	go func() {
		result <- runWith(ctx, lifecycleTestLookup(lifecycleTestBaseValues()), writer, deps.deps(), lifecycleTestLimits())
	}()
	select {
	case <-writer.first:
	case <-server.started:
		cancel()
		lifecycleTestWaitSignal(t, writer.first, "blocked writer did not receive a log write")
	case <-time.After(2 * time.Second):
		t.Fatal("runtime did not attempt to write a log line")
	}
	cancel()
	err := lifecycleTestWaitError(t, result, 2*time.Second)
	if err == nil {
		t.Fatal("blocked writer did not produce a fixed runtime or shutdown error")
	}
	if !errors.Is(err, ErrRuntime) && !errors.Is(err, ErrShutdown) {
		t.Fatal("blocked writer did not produce a fixed sentinel")
	}
	release()
	select {
	case <-writer.done:
	case <-time.After(time.Second):
		t.Fatal("blocked writer did not return after release")
	}
	select {
	case <-writer.more:
		t.Fatal("runtime queued another log write after a blocked writer timeout")
	case <-time.After(100 * time.Millisecond):
	}
	if calls := writer.snapshot(); calls != 1 {
		t.Fatal("runtime wrote more than one log line after a blocked writer timeout")
	}
}

func TestRunWithTestModeHandshakeSealsAfterShutdown(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	dir, err := os.MkdirTemp("", "lifecycle-obs-")
	if err != nil {
		t.Fatal("create lifecycle observer directory")
	}
	if err := os.Chmod(dir, 0o700); err != nil {
		t.Fatal("secure lifecycle observer directory")
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(dir); err != nil {
			t.Errorf("remove lifecycle observer directory")
		}
	})
	socket := filepath.Join(dir, "s")
	observer, err := harness.StartObserver(context.Background(), harness.ObserverConfig{
		Socket:         socket,
		RunID:          lifecycleTestRunID,
		Epoch:          lifecycleTestEpoch,
		BarrierTimeout: time.Second,
	})
	if err != nil {
		t.Fatal("start lifecycle observer")
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		if err := observer.Close(ctx); err != nil {
			t.Error("close lifecycle observer failed")
		}
	})
	if err := observer.Expect(1); err != nil {
		t.Fatal("expect lifecycle observer generation")
	}
	values := lifecycleTestBaseValues()
	values[lifecycleTestModeEnv] = "on"
	values[lifecycleTestSocketEnv] = socket
	values[lifecycleTestRunEnv] = lifecycleTestRunID
	values[lifecycleTestEpochEnv] = lifecycleTestEpoch
	values[lifecycleTestGenEnv] = "1"
	db := newLifecycleTestDB()
	listener := newLifecycleTestListener()
	server := newLifecycleTestServer(&lifecycleTestOrder{})
	deps := &lifecycleTestDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
		listen:       func(context.Context, string) (net.Listener, error) { return listener, nil },
		newServer:    func(*http.Server) runtimeServer { return server },
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	t.Cleanup(func() {
		_ = listener.Close()
	})
	result := make(chan error, 1)
	go func() {
		result <- runWith(ctx, lifecycleTestLookup(values), io.Discard, deps.deps(), lifecycleTestLimits())
	}()
	lifecycleTestWaitSignal(t, server.started, "runtime server did not start with test mode enabled")
	cancel()
	if err := lifecycleTestWaitError(t, result, 2*time.Second); err != nil {
		t.Fatal("test-mode runtime shutdown returned an error")
	}
	counts, err := observer.Snapshot()
	if err != nil {
		t.Fatal("test-mode observer was not sealed after shutdown")
	}
	if counts.Payment != 0 || counts.Query != 0 {
		t.Fatal("test-mode shutdown produced unexpected observer counts")
	}
	if _, listenCalls := deps.snapshot(); listenCalls != 1 {
		t.Fatal("test-mode runtime did not listen exactly once")
	}
}

func TestRuntimeHTTPServerConfigFromLimits(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	db := newLifecycleTestDB()
	listener := newLifecycleTestListener()
	server := newLifecycleTestServer(&lifecycleTestOrder{})
	var captured *http.Server
	deps := &lifecycleTestDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
		listen:       func(context.Context, string) (net.Listener, error) { return listener, nil },
		newServer: func(srv *http.Server) runtimeServer {
			captured = srv
			return server
		},
	}
	limits := lifecycleTestLimits()
	limits.read = 150 * time.Millisecond
	limits.write = 400 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	t.Cleanup(func() {
		_ = listener.Close()
	})
	result := make(chan error, 1)
	go func() {
		result <- runWith(ctx, lifecycleTestLookup(lifecycleTestBaseValues()), io.Discard, deps.deps(), limits)
	}()
	lifecycleTestWaitSignal(t, server.started, "runtime server did not start for HTTP config test")
	if captured == nil {
		t.Fatal("runtime did not expose an http.Server to the test seam")
	}
	if captured.ReadHeaderTimeout != limits.read || captured.ReadTimeout != limits.read {
		t.Fatal("http.Server read timeouts do not match the runtime limits")
	}
	if captured.WriteTimeout != limits.write {
		t.Fatal("http.Server write timeout does not match the runtime limits")
	}
	if captured.IdleTimeout != 0 {
		t.Fatal("http.Server idle timeout was changed from the zero default")
	}
	if captured.Handler == nil {
		t.Fatal("http.Server has no handler")
	}
	if captured.ErrorLog == nil || captured.ErrorLog.Writer() == os.Stderr {
		t.Fatal("http.Server error log uses the raw default channel")
	}
	cancel()
	if err := lifecycleTestWaitError(t, result, 2*time.Second); err != nil {
		t.Fatal("HTTP config runtime shutdown returned an error")
	}
}

type lifecycleTestRealHTTP struct {
	cancel   context.CancelFunc
	result   <-chan error
	listener net.Listener
	server   *http.Server
	limits   runtimeLimits
	stopOnce sync.Once
}

func lifecycleTestStartRealHTTP(t *testing.T, handler http.Handler, limits runtimeLimits) *lifecycleTestRealHTTP {
	t.Helper()
	lifecycleTestClearPGEnv(t)
	db := newLifecycleTestDB()
	listenerCh := make(chan net.Listener, 1)
	realServer := &lifecycleTestRealServer{started: make(chan struct{})}
	deps := runtimeDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
		listen: func(context.Context, string) (net.Listener, error) {
			listener, err := net.Listen("tcp", "127.0.0.1:0")
			if err == nil {
				listenerCh <- listener
			}
			return listener, err
		},
		newServer: func(server *http.Server) runtimeServer {
			server.Handler = handler
			realServer.server = server
			return realServer
		},
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(func() {
		cancel()
	})
	t.Cleanup(func() {
		select {
		case listener := <-listenerCh:
			_ = listener.Close()
		default:
		}
	})
	result := make(chan error, 1)
	go func() {
		result <- runWith(ctx, lifecycleTestLookup(lifecycleTestBaseValues()), io.Discard, deps, limits)
	}()
	lifecycleTestWaitSignal(t, realServer.started, "real HTTP server did not start")
	var listener net.Listener
	select {
	case listener = <-listenerCh:
	case <-time.After(2 * time.Second):
		cancel()
		t.Fatal("real HTTP listener was not created")
	}
	h := &lifecycleTestRealHTTP{
		cancel:   cancel,
		result:   result,
		listener: listener,
		server:   realServer.server,
		limits:   limits,
	}
	t.Cleanup(func() {
		h.stop(t)
	})
	return h
}

func (h *lifecycleTestRealHTTP) stop(t *testing.T) {
	t.Helper()
	h.stopOnce.Do(func() {
		h.cancel()
		select {
		case err := <-h.result:
			if err != nil {
				t.Error("real HTTP runtime shutdown returned an error")
			}
		case <-time.After(2 * time.Second):
			t.Error("real HTTP runtime did not stop")
		}
		if h.listener != nil {
			_ = h.listener.Close()
		}
	})
}

func TestRuntimeHTTPReadHeaderDeadline(t *testing.T) {
	entered := make(chan struct{}, 1)
	handler := http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		entered <- struct{}{}
		w.WriteHeader(http.StatusOK)
	})
	limits := lifecycleTestLimits()
	limits.read = 150 * time.Millisecond
	limits.write = 400 * time.Millisecond
	h := lifecycleTestStartRealHTTP(t, handler, limits)
	defer h.stop(t)

	conn, err := net.Dial("tcp", h.listener.Addr().String())
	if err != nil {
		t.Fatal("dial real HTTP listener")
	}
	defer conn.Close()
	if _, err := io.WriteString(conn, "GET /readyz HTTP/1.1\r\nHost: test\r\n"); err != nil {
		t.Fatal("write partial HTTP header")
	}
	// The client read deadline is only an outer fallback; the target is the
	// server read-header budget. An immediate malformed-header close must not
	// masquerade as that timeout.
	if err := conn.SetReadDeadline(time.Now().Add(time.Second)); err != nil {
		t.Fatal("set real HTTP client read deadline")
	}
	start := time.Now()
	buf := make([]byte, 256)
	for {
		if _, err := conn.Read(buf); err != nil {
			break
		}
	}
	elapsed := time.Since(start)
	if elapsed < limits.read/2 {
		t.Fatal("slow header closed before the read-header budget")
	}
	if elapsed >= 800*time.Millisecond {
		t.Fatal("slow header was not closed by the read deadline")
	}
	select {
	case <-entered:
		t.Fatal("handler ran before the HTTP header completed")
	default:
	}
}

func TestRuntimeHTTPReadBodyDeadline(t *testing.T) {
	entered := make(chan struct{}, 1)
	bodyErr := make(chan error, 1)
	handler := http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		entered <- struct{}{}
		_, err := io.ReadAll(request.Body)
		bodyErr <- err
		w.WriteHeader(http.StatusOK)
	})
	limits := lifecycleTestLimits()
	limits.read = 150 * time.Millisecond
	limits.write = 400 * time.Millisecond
	// This test targets the read deadline, not shutdown. net/http may spend
	// 500ms avoiding a TCP reset after the unread body; leave cleanup room.
	limits.shutdown = 1500 * time.Millisecond
	h := lifecycleTestStartRealHTTP(t, handler, limits)
	defer h.stop(t)

	conn, err := net.Dial("tcp", h.listener.Addr().String())
	if err != nil {
		t.Fatal("dial real HTTP listener")
	}
	defer conn.Close()
	if _, err := io.WriteString(conn, "POST /readyz HTTP/1.1\r\nHost: test\r\nContent-Length: 10\r\n\r\nx"); err != nil {
		t.Fatal("write partial HTTP body")
	}
	lifecycleTestWaitSignal(t, entered, "handler did not run after the HTTP header completed")
	select {
	case err := <-bodyErr:
		var netErr net.Error
		if !errors.As(err, &netErr) || !netErr.Timeout() {
			t.Fatal("slow body read did not hit a net.Error timeout")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("slow body read did not return")
	}
}

func TestRuntimeHTTPWriteDeadlineDoesNotCancelHandler(t *testing.T) {
	entered := make(chan struct{}, 1)
	handlerContextErr := make(chan error, 1)
	release := make(chan struct{})
	var releaseOnce sync.Once
	releaseHandler := func() {
		releaseOnce.Do(func() {
			close(release)
		})
	}
	defer releaseHandler()
	handler := http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		entered <- struct{}{}
		<-release
		handlerContextErr <- request.Context().Err()
		_, _ = w.Write([]byte("late"))
	})
	limits := lifecycleTestLimits()
	limits.read = 2 * time.Second
	limits.write = 300 * time.Millisecond
	h := lifecycleTestStartRealHTTP(t, handler, limits)
	defer h.stop(t)

	conn, err := net.Dial("tcp", h.listener.Addr().String())
	if err != nil {
		t.Fatal("dial real HTTP listener")
	}
	defer conn.Close()
	if _, err := io.WriteString(conn, "GET /readyz HTTP/1.1\r\nHost: test\r\n\r\n"); err != nil {
		t.Fatal("write complete HTTP request")
	}
	lifecycleTestWaitSignal(t, entered, "handler did not run for write deadline test")
	<-time.After(2 * limits.write)
	releaseHandler()
	select {
	case err := <-handlerContextErr:
		if err != nil {
			t.Fatal("write deadline canceled the handler context")
		}
	case <-time.After(time.Second):
		t.Fatal("handler context result did not return")
	}
	if err := conn.SetReadDeadline(time.Now().Add(time.Second)); err != nil {
		t.Fatal("set real HTTP client read deadline")
	}
	start := time.Now()
	var received []byte
	buf := make([]byte, 256)
	for {
		n, err := conn.Read(buf)
		if n > 0 {
			received = append(received, buf[:n]...)
		}
		if err != nil {
			break
		}
	}
	if len(received) != 0 {
		t.Fatal("late handler response was delivered after the write deadline")
	}
	if elapsed := time.Since(start); elapsed >= time.Second {
		t.Fatal("write timeout did not close the client connection promptly")
	}

	t.Run("write budget starts after header", func(t *testing.T) {
		handlerDone := make(chan struct{})
		handler := http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("ok"))
			close(handlerDone)
		})
		limits := lifecycleTestLimits()
		limits.read = 2 * time.Second
		limits.write = 200 * time.Millisecond
		h := lifecycleTestStartRealHTTP(t, handler, limits)
		defer h.stop(t)

		conn, err := net.Dial("tcp", h.listener.Addr().String())
		if err != nil {
			t.Fatal("dial real HTTP listener")
		}
		defer conn.Close()
		if _, err := io.WriteString(conn, "GET /readyz HTTP/1.1\r\nHost: test\r\nConnection: close\r\n"); err != nil {
			t.Fatal("write partial HTTP header")
		}
		<-time.After(2 * limits.write)
		if _, err := io.WriteString(conn, "\r\n"); err != nil {
			t.Fatal("complete HTTP header")
		}
		if err := conn.SetReadDeadline(time.Now().Add(2 * time.Second)); err != nil {
			t.Fatal("set real HTTP client read deadline")
		}
		response, err := http.ReadResponse(bufio.NewReader(conn), &http.Request{Method: http.MethodGet})
		if err != nil {
			t.Fatal("write budget from accept suppressed the response")
		}
		defer response.Body.Close()
		body, err := io.ReadAll(response.Body)
		if err != nil {
			t.Fatal("read response body")
		}
		if response.StatusCode != http.StatusOK || string(body) != "ok" {
			t.Fatal("write budget did not start at header completion")
		}
		select {
		case <-handlerDone:
		case <-time.After(time.Second):
			t.Fatal("handler completion signal did not arrive")
		}
	})
}

func TestParseRuntimeDatabaseConfigAcceptsRestrictedURI(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	config, err := parseRuntimeDatabaseConfig(lifecycleTestDatabase)
	if err != nil {
		t.Fatal("restricted runtime database URI was rejected")
	}
	if config == nil || config.ConnConfig == nil {
		t.Fatal("restricted runtime database URI returned no connection config")
	}
	if config.ConnConfig.Host != "127.0.0.1" ||
		config.ConnConfig.Port != 5432 ||
		config.ConnConfig.Database != "payment_hub_test" ||
		config.ConnConfig.User != "lifecycle_user" ||
		config.ConnConfig.Password != "fake-password" {
		t.Fatal("restricted runtime database URI was not preserved")
	}
	if config.ConnConfig.TLSConfig != nil || len(config.ConnConfig.Fallbacks) != 0 {
		t.Fatal("runtime database config retained TLS or fallback settings")
	}
	if config.ConnConfig.RuntimeParams["timezone"] != "UTC" {
		t.Fatal("runtime database config did not force UTC")
	}
	if config.ConnConfig.RuntimeParams["search_path"] != "public" {
		t.Fatal("runtime database config did not default search_path to public")
	}
}

func TestParseRuntimeDatabaseConfigUsesExplicitSchema(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	uri := "postgresql://lifecycle_user:fake-password@127.0.0.1:5432/payment_hub_test?sslmode=disable&search_path=tenant_1"
	config, err := parseRuntimeDatabaseConfig(uri)
	if err != nil {
		t.Fatal("explicit safe search_path was rejected")
	}
	if config == nil || config.ConnConfig == nil {
		t.Fatal("explicit safe search_path returned no connection config")
	}
	if config.ConnConfig.RuntimeParams["timezone"] != "UTC" ||
		config.ConnConfig.RuntimeParams["search_path"] != "tenant_1" {
		t.Fatal("explicit schema or UTC was not preserved")
	}
}

func TestParseRuntimeDatabaseConfigRejectsUnsafeInputs(t *testing.T) {
	base := "postgres://lifecycle_user:fake-password@127.0.0.1:5432/payment_hub_test"
	cases := []struct {
		name string
		uri  string
	}{
		{name: "empty", uri: ""},
		{name: "not a URI", uri: lifecycleTestCanary},
		{name: "wrong scheme", uri: "mysql://lifecycle_user:fake-password@127.0.0.1:5432/payment_hub_test?sslmode=disable"},
		{name: "missing user", uri: "postgres://127.0.0.1:5432/payment_hub_test?sslmode=disable"},
		{name: "missing password", uri: "postgres://lifecycle_user@127.0.0.1:5432/payment_hub_test?sslmode=disable"},
		{name: "missing host", uri: "postgres://lifecycle_user:fake-password@:5432/payment_hub_test?sslmode=disable"},
		{name: "missing port", uri: "postgres://lifecycle_user:fake-password@127.0.0.1/payment_hub_test?sslmode=disable"},
		{name: "missing database", uri: "postgres://lifecycle_user:fake-password@127.0.0.1:5432/?sslmode=disable"},
		{name: "missing sslmode", uri: base},
		{name: "unsupported sslmode", uri: base + "?sslmode=require"},
		{name: "prefer sslmode", uri: base + "?sslmode=prefer"},
		{name: "multi host", uri: "postgres://lifecycle_user:fake-password@127.0.0.1:5432,127.0.0.1:5433/payment_hub_test?sslmode=disable"},
		{name: "unix socket", uri: "postgres://lifecycle_user:fake-password@/payment_hub_test?host=/tmp&sslmode=disable"},
		{name: "fragment", uri: base + "?sslmode=disable#fragment"},
		{name: "unknown query", uri: base + "?sslmode=disable&application_name=evil"},
		{name: "duplicate sslmode", uri: base + "?sslmode=disable&sslmode=disable"},
		{name: "duplicate search path", uri: base + "?sslmode=disable&search_path=public&search_path=tenant_1"},
		{name: "service", uri: base + "?sslmode=disable&service=evil"},
		{name: "passfile", uri: base + "?sslmode=disable&passfile=/tmp/fake-pass"},
		{name: "tls root", uri: base + "?sslmode=disable&sslrootcert=/tmp/fake-root.crt"},
		{name: "tls cert", uri: base + "?sslmode=disable&sslcert=/tmp/fake.crt"},
		{name: "tls key", uri: base + "?sslmode=disable&sslkey=/tmp/fake.key"},
		{name: "tls password", uri: base + "?sslmode=disable&sslpassword=secret"},
		{name: "tls sni", uri: base + "?sslmode=disable&sslsni=1"},
		{name: "tls negotiation", uri: base + "?sslmode=disable&sslnegotiation=direct"},
		{name: "query user override", uri: base + "?sslmode=disable&user=other"},
		{name: "query host override", uri: base + "?sslmode=disable&host=evil"},
		{name: "query port override", uri: base + "?sslmode=disable&port=5433"},
		{name: "query database override", uri: base + "?sslmode=disable&dbname=other"},
		{name: "query password override", uri: base + "?sslmode=disable&password=other"},
		{name: "extra path segment", uri: base + "/extra?sslmode=disable"},
		{name: "schema uppercase", uri: base + "?sslmode=disable&search_path=Public"},
		{name: "schema hyphen", uri: base + "?sslmode=disable&search_path=bad-schema"},
		{name: "schema dot", uri: base + "?sslmode=disable&search_path=bad.schema"},
		{name: "schema comma", uri: base + "?sslmode=disable&search_path=public,tenant"},
		{name: "schema empty", uri: base + "?sslmode=disable&search_path="},
		{name: "schema leading digit", uri: base + "?sslmode=disable&search_path=1tenant"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			lifecycleTestClearPGEnv(t)
			config, err := parseRuntimeDatabaseConfig(tc.uri)
			if !errors.Is(err, ErrRuntimeConfig) {
				t.Fatal("unsafe runtime database URI did not return ErrRuntimeConfig")
			}
			if config != nil {
				t.Fatal("unsafe runtime database URI returned a config")
			}
			if strings.Contains(err.Error(), lifecycleTestCanary) {
				t.Fatal("runtime database config error echoed rejected input")
			}
		})
	}
}

func TestParseRuntimeDatabaseConfigRejectsPGEnvironment(t *testing.T) {
	for _, name := range []string{"PGSERVICE", "PGSSLROOTCERT", "PGOPTIONS", "PGPASSFILE", "PGHOST"} {
		t.Run(name, func(t *testing.T) {
			lifecycleTestClearPGEnv(t)
			t.Setenv(name, "evil")
			config, err := parseRuntimeDatabaseConfig(lifecycleTestDatabase)
			if !errors.Is(err, ErrRuntimeConfig) {
				t.Fatal("polluted PG environment did not return ErrRuntimeConfig")
			}
			if config != nil {
				t.Fatal("polluted PG environment returned a config")
			}
			if strings.Contains(err.Error(), "evil") {
				t.Fatal("PG environment error echoed an environment value")
			}
		})
	}
	t.Run("empty PG environment value is ignored", func(t *testing.T) {
		lifecycleTestClearPGEnv(t)
		t.Setenv("PGHOST", "")
		if _, err := parseRuntimeDatabaseConfig(lifecycleTestDatabase); err != nil {
			t.Fatal("empty PG environment value was not ignored")
		}
	})
}

func TestParseRuntimeDatabaseConfigDoesNotReadHomeTLSFiles(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	home, err := os.MkdirTemp("", "lifecycle-home-")
	if err != nil {
		t.Fatal("create lifecycle home directory")
	}
	if err := os.Chmod(home, 0o700); err != nil {
		t.Fatal("secure lifecycle home directory")
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(home); err != nil {
			t.Errorf("remove lifecycle home directory")
		}
	})
	tlsDir := filepath.Join(home, ".postgresql")
	if err := os.Mkdir(tlsDir, 0o700); err != nil {
		t.Fatal("create fake home .postgresql directory")
	}
	if err := os.Chmod(tlsDir, 0o700); err != nil {
		t.Fatal("secure fake home .postgresql directory")
	}
	for _, name := range []string{"root.crt", "postgresql.crt", "postgresql.key"} {
		if err := os.WriteFile(filepath.Join(tlsDir, name), []byte(lifecycleTestCanary), 0o600); err != nil {
			t.Fatal("create fake home TLS file")
		}
	}
	t.Setenv("HOME", home)
	config, err := parseRuntimeDatabaseConfig(lifecycleTestDatabase)
	if err != nil {
		t.Fatal("safe canonical runtime config read an invalid home TLS file")
	}
	if config == nil {
		t.Fatal("safe canonical runtime config returned no config")
	}
}

func TestRunWithStartupContextExpiryIsBounded(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	db := newLifecycleTestDB()
	db.pingFn = func(ctx context.Context) error {
		<-ctx.Done()
		return ctx.Err()
	}
	closeStarted := make(chan struct{})
	releaseClose := make(chan struct{})
	var releaseOnce sync.Once
	release := func() {
		releaseOnce.Do(func() {
			close(releaseClose)
		})
	}
	t.Cleanup(release)
	db.closeFn = func() {
		close(closeStarted)
		<-releaseClose
	}
	listener := newLifecycleTestListener()
	server := newLifecycleTestServer(&lifecycleTestOrder{})
	deps := &lifecycleTestDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
		listen:       func(context.Context, string) (net.Listener, error) { return listener, nil },
		newServer:    func(*http.Server) runtimeServer { return server },
	}
	limits := lifecycleTestLimits()
	limits.startup = 2 * time.Second
	limits.shutdown = time.Second
	limits.closeReserve = 50 * time.Millisecond
	parent, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	start := time.Now()
	result := make(chan error, 1)
	go func() {
		result <- runWith(parent, lifecycleTestLookup(lifecycleTestBaseValues()), io.Discard, deps.deps(), limits)
	}()
	lifecycleTestWaitSignal(t, closeStarted, "startup context expiry did not start pool cleanup")
	err := lifecycleTestWaitError(t, result, 2*time.Second)
	if !errors.Is(err, ErrStartup) {
		t.Fatal("startup context expiry did not return ErrStartup")
	}
	if elapsed := time.Since(start); elapsed >= time.Second {
		t.Fatal("startup context expiry borrowed the exit shutdown budget")
	}
	release()
	select {
	case <-db.closeDone:
	case <-time.After(time.Second):
		t.Fatal("startup context expiry cleanup did not finish after release")
	}
}

func TestRunWithActiveDBUserDoesNotSealTestMode(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	observer, socket := lifecycleTestStartObserver(t)
	values := lifecycleTestBaseValues()
	values[lifecycleTestModeEnv] = "on"
	values[lifecycleTestSocketEnv] = socket
	values[lifecycleTestRunEnv] = lifecycleTestRunID
	values[lifecycleTestEpochEnv] = lifecycleTestEpoch
	values[lifecycleTestGenEnv] = "1"

	db := newLifecycleTestDB()
	var pingMu sync.Mutex
	pingCount := 0
	pingStarted := make(chan struct{})
	var pingStartedOnce sync.Once
	releasePing := make(chan struct{})
	var releasePingOnce sync.Once
	releasePingFunc := func() {
		releasePingOnce.Do(func() {
			close(releasePing)
		})
	}
	t.Cleanup(releasePingFunc)
	db.pingFn = func(context.Context) error {
		pingMu.Lock()
		pingCount++
		current := pingCount
		pingMu.Unlock()
		if current == 1 {
			return nil
		}
		pingStartedOnce.Do(func() {
			close(pingStarted)
		})
		<-releasePing
		return nil
	}
	server := newLifecycleTestServer(&lifecycleTestOrder{})
	server.shutdownFn = func(ctx context.Context) error {
		<-ctx.Done()
		return ctx.Err()
	}
	var captured *http.Server
	listener := newLifecycleTestListener()
	deps := &lifecycleTestDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
		listen:       func(context.Context, string) (net.Listener, error) { return listener, nil },
		newServer: func(srv *http.Server) runtimeServer {
			captured = srv
			return server
		},
	}
	limits := lifecycleTestLimits()
	limits.shutdown = 150 * time.Millisecond
	limits.closeReserve = 50 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	t.Cleanup(func() {
		_ = listener.Close()
	})
	result := make(chan error, 1)
	go func() {
		result <- runWith(ctx, lifecycleTestLookup(values), io.Discard, deps.deps(), limits)
	}()
	lifecycleTestWaitSignal(t, server.started, "test-mode server did not start for active-user test")
	if captured == nil || captured.Handler == nil {
		t.Fatal("test-mode server handler was not captured")
	}
	activeDone := make(chan struct{})
	go func() {
		recorder := httptest.NewRecorder()
		captured.Handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/readyz", nil))
		close(activeDone)
	}()
	lifecycleTestWaitSignal(t, pingStarted, "test-mode readyz checker did not enter the active DB call")
	cancel()
	lifecycleTestWaitSignal(t, server.shutdownStarted, "test-mode runtime did not start shutdown")
	err := lifecycleTestWaitError(t, result, 2*time.Second)
	if !errors.Is(err, ErrShutdown) {
		t.Fatal("active test-mode DB user did not produce ErrShutdown")
	}
	if _, err := observer.Snapshot(); !errors.Is(err, testcontrol.ErrUncertain) {
		t.Fatal("test-mode observer was sealed before the active DB user ended")
	}
	if _, _, closeCalls, _ := db.snapshot(); closeCalls != 0 {
		t.Fatal("test-mode runtime closed the pool before the active DB user ended")
	}
	releasePingFunc()
	select {
	case <-activeDone:
	case <-time.After(time.Second):
		t.Fatal("active test-mode readyz handler did not finish after release")
	}
	if _, err := observer.Snapshot(); !errors.Is(err, testcontrol.ErrUncertain) {
		t.Fatal("test-mode observer was sealed after reporting an unconfirmed active user")
	}
	if _, _, closeCalls, _ := db.snapshot(); closeCalls != 0 {
		t.Fatal("test-mode runtime closed the pool after reporting an unconfirmed active user")
	}
}
