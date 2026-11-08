package app

// D1 regression: a fast TestMode Seal failure after HTTP/gate quiescence must
// not skip database close. The positive case uses a real local UDS Observer
// closed before application shutdown, so Seal fails deterministically. The
// negative case keeps a readyz database user active and proves the pool is not
// closed while that user is still running.

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

	"payment-hub/internal/testcontrol"
)

func shutdownCleanupTestValues(socket string) map[string]string {
	values := lifecycleTestBaseValues()
	values[lifecycleTestModeEnv] = "on"
	values[lifecycleTestSocketEnv] = socket
	values[lifecycleTestRunEnv] = lifecycleTestRunID
	values[lifecycleTestEpochEnv] = lifecycleTestEpoch
	values[lifecycleTestGenEnv] = "1"
	return values
}

func shutdownCleanupTestCloseObserver(t *testing.T, observer interface{ Close(context.Context) error }) {
	t.Helper()

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := observer.Close(ctx); err != nil {
		t.Fatal("close observer before application shutdown")
	}
}

func TestShutdownCleanupSealFailureStillClosesDatabase(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	observer, socket := testmodeTestStartObserver(t, 1)
	values := shutdownCleanupTestValues(socket)

	db := newLifecycleTestDB()
	listener := newLifecycleTestListener()
	server := newLifecycleTestServer(&lifecycleTestOrder{})
	deps := &lifecycleTestDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
		listen:       func(context.Context, string) (net.Listener, error) { return listener, nil },
		newServer:    func(*http.Server) runtimeServer { return server },
	}
	limits := lifecycleTestLimits()
	limits.shutdown = 500 * time.Millisecond
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
	lifecycleTestWaitSignal(t, server.started, "runtime server did not start for seal failure test")

	shutdownCleanupTestCloseObserver(t, observer)

	start := time.Now()
	cancel()
	err := lifecycleTestWaitError(t, result, 2*time.Second)
	if !errors.Is(err, ErrShutdown) {
		t.Fatal("seal failure did not return ErrShutdown")
	}
	if elapsed := time.Since(start); elapsed >= time.Second {
		t.Fatal("seal failure cleanup exceeded the bounded exit budget")
	}
	if shutdownCalls, closeCalls := server.snapshot(); shutdownCalls == 0 || closeCalls != 0 {
		t.Fatal("seal failure did not use the normal bounded server shutdown")
	}
	if _, _, closeCalls, _ := db.snapshot(); closeCalls != 1 {
		t.Fatal("seal failure did not close the database exactly once")
	}
	select {
	case <-db.closeDone:
	default:
		t.Fatal("seal failure did not finish database close")
	}
}

func TestShutdownCleanupActiveDatabaseUserPreventsEarlyClose(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	observer, socket := testmodeTestStartObserver(t, 1)
	values := shutdownCleanupTestValues(socket)

	db := newLifecycleTestDB()
	pingStarted := make(chan struct{})
	releasePing := make(chan struct{})
	var pingOnce sync.Once
	var releaseOnce sync.Once
	release := func() {
		releaseOnce.Do(func() {
			close(releasePing)
		})
	}
	t.Cleanup(release)
	var pingMu sync.Mutex
	pingCount := 0
	db.pingFn = func(context.Context) error {
		pingMu.Lock()
		pingCount++
		current := pingCount
		pingMu.Unlock()
		if current == 1 {
			return nil
		}
		pingOnce.Do(func() {
			close(pingStarted)
		})
		<-releasePing
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
	lifecycleTestWaitSignal(t, server.started, "runtime server did not start for active user test")
	if captured == nil || captured.Handler == nil {
		t.Fatal("runtime server handler was not captured")
	}

	activeDone := make(chan struct{})
	go func() {
		recorder := httptest.NewRecorder()
		captured.Handler.ServeHTTP(recorder, httptest.NewRequest(http.MethodGet, "/readyz", nil))
		close(activeDone)
	}()
	lifecycleTestWaitSignal(t, pingStarted, "readyz checker did not enter the active database call")

	shutdownCleanupTestCloseObserver(t, observer)

	cancel()
	err := lifecycleTestWaitError(t, result, 2*time.Second)
	if !errors.Is(err, ErrShutdown) {
		t.Fatal("active database user did not produce ErrShutdown")
	}
	if _, _, closeCalls, _ := db.snapshot(); closeCalls != 0 {
		t.Fatal("runtime closed the pool while a database user was still active")
	}
	if _, err := observer.Snapshot(); !errors.Is(err, testcontrol.ErrUncertain) {
		t.Fatal("observer was sealed before the active database user ended")
	}

	release()
	select {
	case <-activeDone:
	case <-time.After(time.Second):
		t.Fatal("active readyz handler did not finish after release")
	}
	if _, _, closeCalls, _ := db.snapshot(); closeCalls != 0 {
		t.Fatal("runtime closed the pool after reporting an unconfirmed active user")
	}
}
