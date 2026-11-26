package app

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"payment-hub/internal/httpapi"
	"payment-hub/internal/metrics"
	"payment-hub/internal/payment"
)

func counterLineValue(body, name string, labels ...string) string {
	for _, line := range strings.Split(body, "\n") {
		if !strings.HasPrefix(line, name+"{") {
			continue
		}
		matched := true
		for _, label := range labels {
			if !strings.Contains(line, label) {
				matched = false
				break
			}
		}
		if !matched {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) == 2 {
			return fields[1]
		}
	}
	return ""
}

func waitForMetricsBody(t *testing.T, address string) string {
	t.Helper()
	client := &http.Client{Timeout: 500 * time.Millisecond}
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		response, err := client.Get("http://" + address + "/metrics")
		if err == nil {
			body, readErr := io.ReadAll(response.Body)
			_ = response.Body.Close()
			if readErr == nil && response.StatusCode == http.StatusOK &&
				strings.Contains(string(body), "go_goroutines") {
				return string(body)
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("metrics endpoint did not become ready")
	return ""
}

func TestRunWithMetricsListener(t *testing.T) {
	businessListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen business: %v", err)
	}
	t.Cleanup(func() { _ = businessListener.Close() })
	metricsListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen metrics: %v", err)
	}
	t.Cleanup(func() { _ = metricsListener.Close() })

	values := lifecycleTestBaseValues()
	values[metricsAddrEnvName] = "127.0.0.1:19091"
	db := newLifecycleTestDB()
	deps := runtimeDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) {
			return db, nil
		},
		listen: func(_ context.Context, address string) (net.Listener, error) {
			if address == values[metricsAddrEnvName] {
				return metricsListener, nil
			}
			return businessListener, nil
		},
		newServer: func(server *http.Server) runtimeServer {
			return &lifecycleTestRealServer{server: server, started: make(chan struct{})}
		},
		newMetricsRuntime: func(context.Context, runtimeDatabase) (*metrics.Runtime, error) {
			return metrics.NewRuntime(), nil
		},
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	result := make(chan error, 1)
	go func() {
		result <- runWith(ctx, lifecycleTestLookup(values), io.Discard, deps, lifecycleTestLimits())
	}()

	body := waitForMetricsBody(t, metricsListener.Addr().String())
	if !strings.Contains(body, "process_cpu_seconds_total") {
		t.Fatalf("metrics output missing process collector: %s", body)
	}

	cancel()
	if err := lifecycleTestWaitError(t, result, 2*time.Second); err != nil {
		t.Fatalf("runWith returned %v, want nil", err)
	}
	closed := make(chan error, 1)
	go func() {
		_, acceptErr := metricsListener.Accept()
		closed <- acceptErr
	}()
	select {
	case err := <-closed:
		if err == nil {
			t.Fatal("metrics listener accepted a connection after shutdown")
		}
	case <-time.After(time.Second):
		t.Fatal("metrics listener was not closed on shutdown")
	}
}

func TestRunWithInvalidMetricsAddrFailsBeforeDatabase(t *testing.T) {
	values := lifecycleTestBaseValues()
	values[metricsAddrEnvName] = "0.0.0.0:9091"
	opened := false
	deps := runtimeDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) {
			opened = true
			return newLifecycleTestDB(), nil
		},
		listen: func(context.Context, string) (net.Listener, error) {
			return nil, errors.New("listen must not be called")
		},
		newServer: func(*http.Server) runtimeServer {
			return nil
		},
	}
	err := runWith(context.Background(), lifecycleTestLookup(values), io.Discard, deps, lifecycleTestLimits())
	if !errors.Is(err, ErrRuntimeConfig) {
		t.Fatalf("err = %v, want ErrRuntimeConfig", err)
	}
	if opened {
		t.Fatal("database was opened before metrics address validation")
	}
}

type blockingMetricsServer struct {
	shutdownOnce sync.Once
	shutdown     chan struct{}
	closeOnce    sync.Once
	closed       chan struct{}
}

func newBlockingMetricsServer() *blockingMetricsServer {
	return &blockingMetricsServer{
		shutdown: make(chan struct{}),
		closed:   make(chan struct{}),
	}
}

func (server *blockingMetricsServer) Serve(net.Listener) error {
	<-server.closed
	return http.ErrServerClosed
}

func (server *blockingMetricsServer) Shutdown(ctx context.Context) error {
	server.shutdownOnce.Do(func() { close(server.shutdown) })
	<-ctx.Done()
	return ctx.Err()
}

func (server *blockingMetricsServer) Close() error {
	server.closeOnce.Do(func() { close(server.closed) })
	return nil
}

func waitForReadyz(t *testing.T, address string) {
	t.Helper()
	client := &http.Client{Timeout: 200 * time.Millisecond}
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		response, err := client.Get("http://" + address + "/readyz")
		if err == nil {
			_ = response.Body.Close()
			if response.StatusCode == http.StatusOK {
				return
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("readiness endpoint did not become ready")
}

func TestRunWithMetricsShutdownIsBoundedAndClosesDatabase(t *testing.T) {
	businessListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen business: %v", err)
	}
	t.Cleanup(func() { _ = businessListener.Close() })
	metricsListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen metrics: %v", err)
	}
	t.Cleanup(func() { _ = metricsListener.Close() })

	values := lifecycleTestBaseValues()
	values[metricsAddrEnvName] = "127.0.0.1:19092"
	db := newLifecycleTestDB()
	blocking := newBlockingMetricsServer()
	serverCalls := 0
	deps := runtimeDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) {
			return db, nil
		},
		listen: func(_ context.Context, address string) (net.Listener, error) {
			if address == values[metricsAddrEnvName] {
				return metricsListener, nil
			}
			return businessListener, nil
		},
		newServer: func(server *http.Server) runtimeServer {
			serverCalls++
			if serverCalls == 1 {
				return &lifecycleTestRealServer{server: server, started: make(chan struct{})}
			}
			return blocking
		},
		newMetricsRuntime: func(context.Context, runtimeDatabase) (*metrics.Runtime, error) {
			return metrics.NewRuntime(), nil
		},
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	result := make(chan error, 1)
	go func() {
		result <- runWith(ctx, lifecycleTestLookup(values), io.Discard, deps, lifecycleTestLimits())
	}()

	waitForReadyz(t, businessListener.Addr().String())
	start := time.Now()
	cancel()
	if err := lifecycleTestWaitError(t, result, 2*time.Second); err != nil {
		t.Fatalf("runWith returned %v, want nil", err)
	}
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Fatalf("shutdown took %v, want bounded", elapsed)
	}
	if _, _, closeCalls, _ := db.snapshot(); closeCalls != 1 {
		t.Fatalf("database close calls = %d, want 1", closeCalls)
	}
}

func TestRunWithMetricsListenerFailure(t *testing.T) {
	businessListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen business: %v", err)
	}
	t.Cleanup(func() { _ = businessListener.Close() })

	values := lifecycleTestBaseValues()
	values[metricsAddrEnvName] = "127.0.0.1:19093"
	db := newLifecycleTestDB()
	deps := runtimeDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) {
			return db, nil
		},
		listen: func(_ context.Context, address string) (net.Listener, error) {
			if address == values[metricsAddrEnvName] {
				return nil, errors.New("metrics bind failed")
			}
			return businessListener, nil
		},
		newServer: func(server *http.Server) runtimeServer {
			return &lifecycleTestRealServer{server: server, started: make(chan struct{})}
		},
		newMetricsRuntime: func(context.Context, runtimeDatabase) (*metrics.Runtime, error) {
			return metrics.NewRuntime(), nil
		},
	}

	err = runWith(context.Background(), lifecycleTestLookup(values), io.Discard, deps, lifecycleTestLimits())
	if !errors.Is(err, ErrStartup) {
		t.Fatalf("err = %v, want ErrStartup", err)
	}
	if _, _, closeCalls, _ := db.snapshot(); closeCalls != 1 {
		t.Fatalf("database close calls = %d, want 1", closeCalls)
	}
}

func TestRunWithMetricsRuntimeFailure(t *testing.T) {
	values := lifecycleTestBaseValues()
	values[metricsAddrEnvName] = "127.0.0.1:19094"
	db := newLifecycleTestDB()
	listenCalls := 0
	deps := runtimeDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) {
			return db, nil
		},
		listen: func(context.Context, string) (net.Listener, error) {
			listenCalls++
			return nil, errors.New("listen must not be called")
		},
		newServer: func(*http.Server) runtimeServer {
			return nil
		},
		newMetricsRuntime: func(context.Context, runtimeDatabase) (*metrics.Runtime, error) {
			return nil, errors.New("metrics factory failed")
		},
	}

	err := runWith(context.Background(), lifecycleTestLookup(values), io.Discard, deps, lifecycleTestLimits())
	if !errors.Is(err, ErrStartup) {
		t.Fatalf("err = %v, want ErrStartup", err)
	}
	if listenCalls != 0 {
		t.Fatalf("listen calls = %d, want 0", listenCalls)
	}
	if _, _, closeCalls, _ := db.snapshot(); closeCalls != 1 {
		t.Fatalf("database close calls = %d, want 1", closeCalls)
	}
}

func TestRunWithMetricsNilHandlerFailsClosed(t *testing.T) {
	values := lifecycleTestBaseValues()
	values[metricsAddrEnvName] = "127.0.0.1:19095"
	db := newLifecycleTestDB()
	deps := runtimeDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) {
			return db, nil
		},
		listen: func(context.Context, string) (net.Listener, error) {
			return nil, errors.New("listen must not be called")
		},
		newServer: func(*http.Server) runtimeServer {
			return nil
		},
		newMetricsRuntime: func(context.Context, runtimeDatabase) (*metrics.Runtime, error) {
			return &metrics.Runtime{}, nil
		},
	}

	err := runWith(context.Background(), lifecycleTestLookup(values), io.Discard, deps, lifecycleTestLimits())
	if !errors.Is(err, ErrStartup) {
		t.Fatalf("err = %v, want ErrStartup", err)
	}
	if _, _, closeCalls, _ := db.snapshot(); closeCalls != 1 {
		t.Fatalf("database close calls = %d, want 1", closeCalls)
	}
}

func TestMetricsListenerRecordsBusinessHTTP(t *testing.T) {
	businessListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen business: %v", err)
	}
	t.Cleanup(func() { _ = businessListener.Close() })
	metricsListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen metrics: %v", err)
	}
	t.Cleanup(func() { _ = metricsListener.Close() })

	values := lifecycleTestBaseValues()
	values[metricsAddrEnvName] = "127.0.0.1:19096"
	db := newLifecycleTestDB()
	submit := &businessTestSubmitFake{
		result: payment.SubmitResult{
			Order:   businessTestOrder(payment.ResultSucceeded),
			Created: true,
		},
	}
	query := &businessTestQueryFake{
		result: payment.QueryResult{Order: businessTestOrder(payment.ResultSucceeded)},
	}
	deps := runtimeDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) {
			return db, nil
		},
		listen: func(_ context.Context, address string) (net.Listener, error) {
			if address == values[metricsAddrEnvName] {
				return metricsListener, nil
			}
			return businessListener, nil
		},
		newServer: func(server *http.Server) runtimeServer {
			return &lifecycleTestRealServer{server: server, started: make(chan struct{})}
		},
		newMetricsRuntime: func(context.Context, runtimeDatabase) (*metrics.Runtime, error) {
			return metrics.NewRuntime(), nil
		},
		newBusiness: func(context.Context, runtimeDatabase, *TestMode) (httpapi.PaymentSubmitter, httpapi.PaymentQuerier, error) {
			return submit, query, nil
		},
		businessRequired: true,
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	result := make(chan error, 1)
	go func() {
		result <- runWith(ctx, lifecycleTestLookup(values), io.Discard, deps, lifecycleTestLimits())
	}()
	waitForReadyz(t, businessListener.Addr().String())

	request, err := http.NewRequest(
		http.MethodPost,
		"http://"+businessListener.Addr().String()+"/api/v1/payments",
		strings.NewReader(businessTestBody),
	)
	if err != nil {
		t.Fatalf("build request: %v", err)
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+lifecycleTestAPIKey)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatalf("post payment: %v", err)
	}
	_, _ = io.Copy(io.Discard, response.Body)
	_ = response.Body.Close()
	if response.StatusCode != http.StatusCreated {
		t.Fatalf("payment status = %d, want 201", response.StatusCode)
	}

	body := waitForMetricsBody(t, metricsListener.Addr().String())
	for _, want := range []string{
		`method="POST"`,
		`route="/api/v1/payments"`,
		`status="201"`,
	} {
		if !strings.Contains(body, want) {
			t.Fatalf("metrics missing %s: %s", want, body)
		}
	}
	if got := counterLineValue(body, "payment_hub_http_requests_total", `method="POST"`, `route="/api/v1/payments"`, `status="201"`); got != "1" {
		t.Fatalf("POST payment counter = %q, want 1", got)
	}

	cancel()
	if err := lifecycleTestWaitError(t, result, 2*time.Second); err != nil {
		t.Fatalf("runWith returned %v, want nil", err)
	}
}

func TestRunWithMetricsServerNilFailsClosed(t *testing.T) {
	businessListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen business: %v", err)
	}
	t.Cleanup(func() { _ = businessListener.Close() })
	metricsListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen metrics: %v", err)
	}
	t.Cleanup(func() { _ = metricsListener.Close() })

	values := lifecycleTestBaseValues()
	values[metricsAddrEnvName] = "127.0.0.1:19097"
	db := newLifecycleTestDB()
	serverCalls := 0
	deps := runtimeDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) {
			return db, nil
		},
		listen: func(_ context.Context, address string) (net.Listener, error) {
			if address == values[metricsAddrEnvName] {
				return metricsListener, nil
			}
			return businessListener, nil
		},
		newServer: func(server *http.Server) runtimeServer {
			serverCalls++
			if serverCalls == 1 {
				return &lifecycleTestRealServer{server: server, started: make(chan struct{})}
			}
			return nil
		},
		newMetricsRuntime: func(context.Context, runtimeDatabase) (*metrics.Runtime, error) {
			return metrics.NewRuntime(), nil
		},
	}

	err = runWith(context.Background(), lifecycleTestLookup(values), io.Discard, deps, lifecycleTestLimits())
	if !errors.Is(err, ErrRuntime) {
		t.Fatalf("err = %v, want ErrRuntime", err)
	}
	if _, _, closeCalls, _ := db.snapshot(); closeCalls != 1 {
		t.Fatalf("database close calls = %d, want 1", closeCalls)
	}
}

type failingServeServer struct {
	err error
}

func (server *failingServeServer) Serve(net.Listener) error       { return server.err }
func (server *failingServeServer) Shutdown(context.Context) error { return nil }
func (server *failingServeServer) Close() error                   { return nil }

func TestMetricsServeFailureIsLogged(t *testing.T) {
	businessListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen business: %v", err)
	}
	t.Cleanup(func() { _ = businessListener.Close() })
	metricsListener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen metrics: %v", err)
	}
	t.Cleanup(func() { _ = metricsListener.Close() })

	values := lifecycleTestBaseValues()
	values[metricsAddrEnvName] = "127.0.0.1:19098"
	db := newLifecycleTestDB()
	serveErr := errors.New("metrics serve canary failure")
	serverCalls := 0
	deps := runtimeDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) {
			return db, nil
		},
		listen: func(_ context.Context, address string) (net.Listener, error) {
			if address == values[metricsAddrEnvName] {
				return metricsListener, nil
			}
			return businessListener, nil
		},
		newServer: func(server *http.Server) runtimeServer {
			serverCalls++
			if serverCalls == 1 {
				return &lifecycleTestRealServer{server: server, started: make(chan struct{})}
			}
			return &failingServeServer{err: serveErr}
		},
		newMetricsRuntime: func(context.Context, runtimeDatabase) (*metrics.Runtime, error) {
			return metrics.NewRuntime(), nil
		},
	}

	var logs bytes.Buffer
	err = runWith(context.Background(), lifecycleTestLookup(values), &logs, deps, lifecycleTestLimits())
	if !errors.Is(err, ErrRuntime) {
		t.Fatalf("err = %v, want ErrRuntime", err)
	}
	if _, _, closeCalls, _ := db.snapshot(); closeCalls != 1 {
		t.Fatalf("database close calls = %d, want 1", closeCalls)
	}
	if strings.Contains(logs.String(), serveErr.Error()) {
		t.Fatal("serve failure log echoed the raw error text")
	}
	shutdownEvents := 0
	for _, line := range strings.Split(strings.TrimSpace(logs.String()), "\n") {
		if line == "" {
			continue
		}
		var entry map[string]any
		if err := json.Unmarshal([]byte(line), &entry); err != nil {
			t.Fatal("serve failure log line is not JSON")
		}
		if entry["event"] != "shutdown" || entry["stage"] != "shutdown" {
			continue
		}
		shutdownEvents++
		if entry["level"] != "error" || entry["reason"] != "internal" || entry["error_kind"] != "internal" {
			t.Fatalf("unexpected serve failure log entry: %v", entry)
		}
	}
	if shutdownEvents != 1 {
		t.Fatalf("shutdown events = %d, want 1\n%s", shutdownEvents, logs.String())
	}
}
