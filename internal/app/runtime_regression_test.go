package app

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

var errRuntimeRegressionShutdown = errors.New("runtime regression shutdown failure")

type runtimeRegressionShutdownServer struct {
	base *lifecycleTestServer
}

func (server *runtimeRegressionShutdownServer) Serve(listener net.Listener) error {
	return server.base.Serve(listener)
}

func (server *runtimeRegressionShutdownServer) Shutdown(ctx context.Context) error {
	if err := server.base.Shutdown(ctx); err != nil {
		return err
	}
	return errRuntimeRegressionShutdown
}

func (server *runtimeRegressionShutdownServer) Close() error {
	return server.base.Close()
}

type runtimeRegressionHoldServer struct {
	base    *lifecycleTestServer
	entered chan struct{}
	release chan struct{}
	done    chan struct{}
}

func (server *runtimeRegressionHoldServer) Serve(listener net.Listener) error {
	err := server.base.Serve(listener)
	close(server.entered)
	<-server.release
	close(server.done)
	return err
}

func (server *runtimeRegressionHoldServer) Shutdown(ctx context.Context) error {
	return server.base.Shutdown(ctx)
}

func (server *runtimeRegressionHoldServer) Close() error {
	return server.base.Close()
}

type runtimeRegressionCloseErrorListener struct {
	base net.Listener
	err  error
}

func (listener *runtimeRegressionCloseErrorListener) Accept() (net.Conn, error) {
	return listener.base.Accept()
}

func (listener *runtimeRegressionCloseErrorListener) Close() error {
	return listener.err
}

func (listener *runtimeRegressionCloseErrorListener) Addr() net.Addr {
	return listener.base.Addr()
}

func TestRuntimeRejectsLateStartupSuccess(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	db := newLifecycleTestDB()
	listener := newLifecycleTestListener()
	server := newLifecycleTestServer(&lifecycleTestOrder{})
	expired := make(chan error, 1)
	newServerCalls := 0
	deps := &lifecycleTestDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
		listen: func(ctx context.Context, _ string) (net.Listener, error) {
			<-ctx.Done()
			expired <- ctx.Err()
			return listener, nil
		},
		newServer: func(*http.Server) runtimeServer {
			newServerCalls++
			return server
		},
	}
	limits := lifecycleTestLimits()
	limits.startup = 120 * time.Millisecond
	limits.shutdown = 500 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	result := make(chan error, 1)
	go func() {
		result <- runWith(ctx, lifecycleTestLookup(lifecycleTestBaseValues()), io.Discard, deps.deps(), limits)
	}()
	err := lifecycleTestWaitError(t, result, 2*time.Second)
	if !errors.Is(err, ErrStartup) {
		t.Fatal("late successful listen did not return ErrStartup")
	}
	select {
	case ctxErr := <-expired:
		if !errors.Is(ctxErr, context.DeadlineExceeded) {
			t.Fatal("late listen did not observe the expired startup context")
		}
	case <-time.After(time.Second):
		t.Fatal("late listen did not report the startup context result")
	}
	if newServerCalls != 0 {
		t.Fatal("late successful listen reached newServer")
	}
	select {
	case <-server.started:
		t.Fatal("late successful listen reached Serve")
	default:
	}
	select {
	case <-listener.closed:
	case <-time.After(time.Second):
		t.Fatal("late successful listen resource was not closed")
	}
	select {
	case <-db.closeDone:
	case <-time.After(time.Second):
		t.Fatal("late successful listen did not close the pool")
	}
	if _, _, closeCalls, _ := db.snapshot(); closeCalls != 1 {
		t.Fatal("late successful listen did not close the pool exactly once")
	}
}

func TestRuntimeShutdownReportsUnconfirmedShutdown(t *testing.T) {
	t.Run("server shutdown error after stop", func(t *testing.T) {
		lifecycleTestClearPGEnv(t)
		db := newLifecycleTestDB()
		listener := newLifecycleTestListener()
		base := newLifecycleTestServer(&lifecycleTestOrder{})
		server := &runtimeRegressionShutdownServer{base: base}
		deps := &lifecycleTestDeps{
			openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
			listen:       func(context.Context, string) (net.Listener, error) { return listener, nil },
			newServer:    func(*http.Server) runtimeServer { return server },
		}
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		result := make(chan error, 1)
		go func() {
			result <- runWith(ctx, lifecycleTestLookup(lifecycleTestBaseValues()), io.Discard, deps.deps(), lifecycleTestLimits())
		}()
		lifecycleTestWaitSignal(t, base.started, "runtime server did not start for shutdown error test")
		cancel()
		err := lifecycleTestWaitError(t, result, 2*time.Second)
		if !errors.Is(err, ErrShutdown) {
			t.Fatal("successful cleanup after Shutdown error did not return ErrShutdown")
		}
		if _, _, closeCalls, _ := db.snapshot(); closeCalls != 1 {
			t.Fatal("shutdown error path did not close the pool exactly once")
		}
	})

	t.Run("listener close error", func(t *testing.T) {
		lifecycleTestClearPGEnv(t)
		db := newLifecycleTestDB()
		baseListener := newLifecycleTestListener()
		listener := &runtimeRegressionCloseErrorListener{base: baseListener, err: errRuntimeRegressionShutdown}
		server := newLifecycleTestServer(&lifecycleTestOrder{})
		deps := &lifecycleTestDeps{
			openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
			listen:       func(context.Context, string) (net.Listener, error) { return listener, nil },
			newServer:    func(*http.Server) runtimeServer { return server },
		}
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		result := make(chan error, 1)
		go func() {
			result <- runWith(ctx, lifecycleTestLookup(lifecycleTestBaseValues()), io.Discard, deps.deps(), lifecycleTestLimits())
		}()
		lifecycleTestWaitSignal(t, server.started, "runtime server did not start for listener close error test")
		cancel()
		err := lifecycleTestWaitError(t, result, 2*time.Second)
		if !errors.Is(err, ErrShutdown) {
			t.Fatal("listener close error was not reported as ErrShutdown")
		}
		if _, _, closeCalls, _ := db.snapshot(); closeCalls != 1 {
			t.Fatal("listener close error path did not close the pool exactly once")
		}
	})

	t.Run("already closed listener is benign", func(t *testing.T) {
		lifecycleTestClearPGEnv(t)
		db := newLifecycleTestDB()
		baseListener := newLifecycleTestListener()
		listener := &runtimeRegressionCloseErrorListener{base: baseListener, err: net.ErrClosed}
		server := newLifecycleTestServer(&lifecycleTestOrder{})
		deps := &lifecycleTestDeps{
			openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
			listen:       func(context.Context, string) (net.Listener, error) { return listener, nil },
			newServer:    func(*http.Server) runtimeServer { return server },
		}
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		result := make(chan error, 1)
		go func() {
			result <- runWith(ctx, lifecycleTestLookup(lifecycleTestBaseValues()), io.Discard, deps.deps(), lifecycleTestLimits())
		}()
		lifecycleTestWaitSignal(t, server.started, "runtime server did not start for repeated close test")
		cancel()
		if err := lifecycleTestWaitError(t, result, 2*time.Second); err != nil {
			t.Fatal("net.ErrClosed from listener close was not treated as benign")
		}
		if _, _, closeCalls, _ := db.snapshot(); closeCalls != 1 {
			t.Fatal("benign listener close did not close the pool exactly once")
		}
	})
}

func TestRuntimeShutdownWaitsForServeExit(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	db := newLifecycleTestDB()
	listener := newLifecycleTestListener()
	base := newLifecycleTestServer(&lifecycleTestOrder{})
	release := make(chan struct{})
	var releaseOnce sync.Once
	releaseServer := func() {
		releaseOnce.Do(func() {
			close(release)
		})
	}
	t.Cleanup(releaseServer)
	server := &runtimeRegressionHoldServer{
		base:    base,
		entered: make(chan struct{}),
		release: release,
		done:    make(chan struct{}),
	}
	deps := &lifecycleTestDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
		listen:       func(context.Context, string) (net.Listener, error) { return listener, nil },
		newServer:    func(*http.Server) runtimeServer { return server },
	}
	limits := lifecycleTestLimits()
	limits.shutdown = 200 * time.Millisecond
	limits.closeReserve = 50 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	result := make(chan error, 1)
	go func() {
		result <- runWith(ctx, lifecycleTestLookup(lifecycleTestBaseValues()), io.Discard, deps.deps(), limits)
	}()
	lifecycleTestWaitSignal(t, base.started, "runtime server did not start for Serve join test")
	cancel()
	lifecycleTestWaitSignal(t, server.entered, "Serve did not return from the server stop")
	err := lifecycleTestWaitError(t, result, 2*time.Second)
	if !errors.Is(err, ErrShutdown) {
		t.Fatal("unconfirmed Serve exit did not return ErrShutdown")
	}
	if _, _, closeCalls, _ := db.snapshot(); closeCalls != 0 {
		t.Fatal("pool was closed before Serve exit was confirmed")
	}
	releaseServer()
	select {
	case <-server.done:
	case <-time.After(time.Second):
		t.Fatal("held Serve did not finish after release")
	}
	if _, _, closeCalls, _ := db.snapshot(); closeCalls != 0 {
		t.Fatal("pool was closed after reporting an unconfirmed Serve exit")
	}
}

func TestRuntimeListenHonorsContext(t *testing.T) {
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	listener, err := listenRuntime(canceled, "127.0.0.1:0")
	if listener != nil || !errors.Is(err, ErrStartup) {
		t.Fatal("canceled listen context did not fail with ErrStartup")
	}

	listener, err = listenRuntime(context.Background(), "127.0.0.1:0")
	if err != nil || listener == nil {
		t.Fatal("live loopback listen failed")
	}
	if err := listener.Close(); err != nil {
		t.Fatal("live loopback listener close failed")
	}
}

func TestRuntimeReadyzUsesProbeBudget(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	db := newLifecycleTestDB()
	var mu sync.Mutex
	pingCount := 0
	deadlineCh := make(chan time.Time, 1)
	db.pingFn = func(ctx context.Context) error {
		mu.Lock()
		pingCount++
		current := pingCount
		mu.Unlock()
		if current == 1 {
			return nil
		}
		if deadline, ok := ctx.Deadline(); ok {
			deadlineCh <- deadline
		}
		return nil
	}
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
	limits.probe = 50 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	result := make(chan error, 1)
	go func() {
		result <- runWith(ctx, lifecycleTestLookup(lifecycleTestBaseValues()), io.Discard, deps.deps(), limits)
	}()
	lifecycleTestWaitSignal(t, server.started, "runtime server did not start for probe budget test")
	if captured == nil || captured.Handler == nil {
		t.Fatal("runtime handler was not captured")
	}
	recorder := httptest.NewRecorder()
	captured.Handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if recorder.Code != http.StatusOK {
		t.Fatal("readyz did not succeed with a live probe")
	}
	select {
	case deadline := <-deadlineCh:
		remaining := time.Until(deadline)
		if remaining <= 0 || remaining > 200*time.Millisecond {
			t.Fatal("readyz Ping did not use the private probe budget")
		}
	case <-time.After(time.Second):
		t.Fatal("readyz Ping did not receive a deadline")
	}
	cancel()
	if err := lifecycleTestWaitError(t, result, 2*time.Second); err != nil {
		t.Fatal("probe budget runtime shutdown returned an error")
	}
}

var errRuntimeRegressionServe = errors.New("runtime regression serve failure")

type runtimeRegressionServeErrorServer struct {
	base *lifecycleTestServer
	err  error
}

func (server *runtimeRegressionServeErrorServer) Serve(listener net.Listener) error {
	_ = server.base.Serve(listener)
	return server.err
}

func (server *runtimeRegressionServeErrorServer) Shutdown(ctx context.Context) error {
	return server.base.Shutdown(ctx)
}

func (server *runtimeRegressionServeErrorServer) Close() error {
	return server.base.Close()
}

func TestRuntimePreservesServeErrorAfterCancellation(t *testing.T) {
	run := func(t *testing.T, serveErr error) error {
		t.Helper()
		lifecycleTestClearPGEnv(t)
		db := newLifecycleTestDB()
		listener := newLifecycleTestListener()
		base := newLifecycleTestServer(&lifecycleTestOrder{})
		server := &runtimeRegressionServeErrorServer{base: base, err: serveErr}
		deps := &lifecycleTestDeps{
			openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
			listen:       func(context.Context, string) (net.Listener, error) { return listener, nil },
			newServer:    func(*http.Server) runtimeServer { return server },
		}
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		result := make(chan error, 1)
		go func() {
			result <- runWith(ctx, lifecycleTestLookup(lifecycleTestBaseValues()), io.Discard, deps.deps(), lifecycleTestLimits())
		}()
		lifecycleTestWaitSignal(t, base.started, "runtime server did not start for Serve error preservation test")
		cancel()
		err := lifecycleTestWaitError(t, result, 2*time.Second)
		if _, _, closeCalls, _ := db.snapshot(); closeCalls != 1 {
			t.Fatal("Serve error preservation path did not close the pool exactly once")
		}
		return err
	}

	t.Run("unexpected serve error after cancellation", func(t *testing.T) {
		err := run(t, errRuntimeRegressionServe)
		if !errors.Is(err, ErrRuntime) {
			t.Fatal("unexpected Serve result after cancellation was not preserved as ErrRuntime")
		}
	})

	t.Run("server closed after cancellation", func(t *testing.T) {
		err := run(t, http.ErrServerClosed)
		if err != nil {
			t.Fatal("standard ErrServerClosed after cancellation was not treated as normal")
		}
	})
}
