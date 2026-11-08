package app

// This file specifies the T010/PR3 startup failure diagnostics contract:
// database open failure, ping retry exhaustion, test-mode connection/Begin
// failure, listen failure, and startup context cancellation must each produce
// one fixed, whitelisted JSON event. The diagnostic window is carved out of the
// original absolute startup deadline; it is not an additive budget and never
// extends process exit past that deadline. Raw errors, DSNs, SQL and
// credentials must never appear in the output.

import (
	"bytes"
	"context"
	"errors"
	"net"
	"net/http"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

const (
	startupDiagnosticCanary = "FAKE_T021_STARTUP_DIAGNOSTIC_CANARY"
	startupDiagnosticDSN    = "postgres://startup_user:" + startupDiagnosticCanary + "@127.0.0.1:5432/payment_hub_test?sslmode=disable"
)

// startupDiagnosticAssertSingle requires exactly one error-level dependency
// event with the expected stage and reason, a fixed error classification, and
// no sensitive marker. Failure messages never print the log content because it
// may contain the leak under test.
func startupDiagnosticAssertSingle(t *testing.T, logs []byte, wantStage, wantReason, wantErrorKind string) {
	t.Helper()

	objects := logTestObjects(t, "startup diagnostics", logs)
	if len(objects) != 1 {
		t.Fatalf("startup failure produced %d log objects, want 1", len(objects))
	}
	object := objects[0]
	logTestAssertAllowedFields(t, "startup diagnostics", object)
	logTestAssertUTCTime(t, "startup diagnostics", object)
	logTestWantString(t, "startup diagnostics", object, "level", string(LogLevelError))
	logTestWantString(t, "startup diagnostics", object, "event", string(LogEventDependency))
	logTestWantString(t, "startup diagnostics", object, "stage", wantStage)
	logTestWantString(t, "startup diagnostics", object, "reason", wantReason)
	logTestWantString(t, "startup diagnostics", object, "error_kind", wantErrorKind)

	startupDiagnosticAssertNoLeak(t, logs)
}

func startupDiagnosticAssertNoLeak(t *testing.T, logs []byte) {
	t.Helper()

	logTestAssertNoLeak(t, "startup diagnostics", logs, map[string]string{
		"canary":        startupDiagnosticCanary,
		"database URL":  startupDiagnosticDSN,
		"authorization": "Authorization: Bearer " + startupDiagnosticCanary,
		"raw SQL":       "SELECT * FROM " + startupDiagnosticCanary,
	})
}

func startupDiagnosticWaitClose(t *testing.T, db *lifecycleTestDB) {
	t.Helper()

	select {
	case <-db.closeDone:
	case <-time.After(time.Second):
		t.Fatal("startup failure did not close the pool")
	}
	if _, _, closeCalls, _ := db.snapshot(); closeCalls != 1 {
		t.Fatal("startup failure did not close the pool exactly once")
	}
}

func TestStartupDiagnosticsDatabaseOpenFailure(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	var logs bytes.Buffer
	deps := &lifecycleTestDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) {
			return nil, errors.New(startupDiagnosticCanary + " " + startupDiagnosticDSN)
		},
		listen: func(context.Context, string) (net.Listener, error) {
			t.Fatal("database open failure reached listen")
			return nil, errors.New("unreachable")
		},
		newServer: func(*http.Server) runtimeServer {
			t.Fatal("database open failure reached server creation")
			return nil
		},
	}

	err := runWith(context.Background(), lifecycleTestLookup(lifecycleTestBaseValues()), &logs, deps.deps(), lifecycleTestLimits())
	if !errors.Is(err, ErrStartup) {
		t.Fatal("database open failure did not return ErrStartup")
	}
	startupDiagnosticAssertSingle(t, logs.Bytes(), string(LogStagePersistence), string(LogReasonDependency), string(LogErrorKindInternal))
}

func TestStartupDiagnosticsPingRetryExhaustion(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	db := newLifecycleTestDB()
	db.pingFn = func(context.Context) error {
		return errors.New(startupDiagnosticCanary + " " + startupDiagnosticDSN)
	}
	deps := &lifecycleTestDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
		listen: func(context.Context, string) (net.Listener, error) {
			t.Fatal("ping retry exhaustion reached listen")
			return nil, errors.New("unreachable")
		},
		newServer: func(*http.Server) runtimeServer {
			t.Fatal("ping retry exhaustion reached server creation")
			return nil
		},
	}
	limits := lifecycleTestLimits()
	limits.startup = 150 * time.Millisecond
	limits.retry = 10 * time.Millisecond
	var logs bytes.Buffer

	err := runWith(context.Background(), lifecycleTestLookup(lifecycleTestBaseValues()), &logs, deps.deps(), limits)
	if !errors.Is(err, ErrStartup) {
		t.Fatal("ping retry exhaustion did not return ErrStartup")
	}
	startupDiagnosticAssertSingle(t, logs.Bytes(), string(LogStagePersistence), string(LogReasonTimeout), string(LogErrorKindTimeout))
	startupDiagnosticWaitClose(t, db)
}

func TestStartupDiagnosticsMigrationAfterStartupExpiry(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	db := newLifecycleTestDB()
	db.checkFn = func(ctx context.Context) error {
		<-ctx.Done()
		return errors.New(startupDiagnosticCanary + " SELECT * FROM goose_db_version")
	}
	deps := &lifecycleTestDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
		listen: func(context.Context, string) (net.Listener, error) {
			t.Fatal("migration failure reached listen")
			return nil, errors.New("unreachable")
		},
		newServer: func(*http.Server) runtimeServer {
			t.Fatal("migration failure reached server creation")
			return nil
		},
	}
	limits := lifecycleTestLimits()
	limits.startup = 100 * time.Millisecond
	var logs bytes.Buffer

	err := runWith(context.Background(), lifecycleTestLookup(lifecycleTestBaseValues()), &logs, deps.deps(), limits)
	if !errors.Is(err, ErrStartup) {
		t.Fatal("migration failure after startup expiry did not return ErrStartup")
	}
	startupDiagnosticAssertSingle(t, logs.Bytes(), string(LogStagePersistence), string(LogReasonTimeout), string(LogErrorKindTimeout))
	startupDiagnosticWaitClose(t, db)
}

func TestStartupDiagnosticsTestModeFailure(t *testing.T) {
	t.Run("connection failure", func(t *testing.T) {
		lifecycleTestClearPGEnv(t)
		socket := filepath.Join(t.TempDir(), "missing-"+startupDiagnosticCanary)
		values := lifecycleTestBaseValues()
		values[lifecycleTestModeEnv] = "on"
		values[lifecycleTestSocketEnv] = socket
		values[lifecycleTestRunEnv] = lifecycleTestRunID
		values[lifecycleTestEpochEnv] = lifecycleTestEpoch
		values[lifecycleTestGenEnv] = "1"
		db := newLifecycleTestDB()
		deps := &lifecycleTestDeps{
			openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
			listen: func(context.Context, string) (net.Listener, error) {
				t.Fatal("test mode connection failure reached listen")
				return nil, errors.New("unreachable")
			},
			newServer: func(*http.Server) runtimeServer {
				t.Fatal("test mode connection failure reached server creation")
				return nil
			},
		}
		var logs bytes.Buffer

		err := runWith(context.Background(), lifecycleTestLookup(values), &logs, deps.deps(), lifecycleTestLimits())
		if !errors.Is(err, ErrStartup) {
			t.Fatal("test mode connection failure did not return ErrStartup")
		}
		startupDiagnosticAssertSingle(t, logs.Bytes(), string(LogStageEntry), string(LogReasonDependency), string(LogErrorKindInternal))
		startupDiagnosticWaitClose(t, db)
	})

	t.Run("begin failure", func(t *testing.T) {
		lifecycleTestClearPGEnv(t)
		_, socket := testmodeTestStartObserver(t, 1)
		values := lifecycleTestBaseValues()
		values[lifecycleTestModeEnv] = "on"
		values[lifecycleTestSocketEnv] = socket
		values[lifecycleTestRunEnv] = strings.Repeat("a", 32)
		values[lifecycleTestEpochEnv] = lifecycleTestEpoch
		values[lifecycleTestGenEnv] = "1"
		db := newLifecycleTestDB()
		deps := &lifecycleTestDeps{
			openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
			listen: func(context.Context, string) (net.Listener, error) {
				t.Fatal("test mode begin failure reached listen")
				return nil, errors.New("unreachable")
			},
			newServer: func(*http.Server) runtimeServer {
				t.Fatal("test mode begin failure reached server creation")
				return nil
			},
		}
		var logs bytes.Buffer

		err := runWith(context.Background(), lifecycleTestLookup(values), &logs, deps.deps(), lifecycleTestLimits())
		if !errors.Is(err, ErrStartup) {
			t.Fatal("test mode begin failure did not return ErrStartup")
		}
		startupDiagnosticAssertSingle(t, logs.Bytes(), string(LogStageEntry), string(LogReasonDependency), string(LogErrorKindInternal))
		startupDiagnosticWaitClose(t, db)
	})
}

func TestStartupDiagnosticsListenFailure(t *testing.T) {
	t.Run("occupied address", func(t *testing.T) {
		lifecycleTestClearPGEnv(t)
		occupied, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal("create occupied loopback listener")
		}
		t.Cleanup(func() {
			if err := occupied.Close(); err != nil {
				t.Errorf("close occupied loopback listener")
			}
		})
		db := newLifecycleTestDB()
		deps := &lifecycleTestDeps{
			openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
			listen: func(context.Context, string) (net.Listener, error) {
				return net.Listen("tcp", occupied.Addr().String())
			},
			newServer: func(*http.Server) runtimeServer {
				t.Fatal("occupied listen failure reached server creation")
				return nil
			},
		}
		var logs bytes.Buffer

		err = runWith(context.Background(), lifecycleTestLookup(lifecycleTestBaseValues()), &logs, deps.deps(), lifecycleTestLimits())
		if !errors.Is(err, ErrStartup) {
			t.Fatal("occupied listen failure did not return ErrStartup")
		}
		startupDiagnosticAssertSingle(t, logs.Bytes(), string(LogStageEntry), string(LogReasonDependency), string(LogErrorKindInternal))
		startupDiagnosticWaitClose(t, db)
	})

	t.Run("fixture error with sensitive text", func(t *testing.T) {
		lifecycleTestClearPGEnv(t)
		db := newLifecycleTestDB()
		deps := &lifecycleTestDeps{
			openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
			listen: func(context.Context, string) (net.Listener, error) {
				return nil, errors.New(startupDiagnosticCanary + " " + startupDiagnosticDSN)
			},
			newServer: func(*http.Server) runtimeServer {
				t.Fatal("listen failure reached server creation")
				return nil
			},
		}
		var logs bytes.Buffer

		err := runWith(context.Background(), lifecycleTestLookup(lifecycleTestBaseValues()), &logs, deps.deps(), lifecycleTestLimits())
		if !errors.Is(err, ErrStartup) {
			t.Fatal("listen failure did not return ErrStartup")
		}
		startupDiagnosticAssertSingle(t, logs.Bytes(), string(LogStageEntry), string(LogReasonDependency), string(LogErrorKindInternal))
		startupDiagnosticWaitClose(t, db)
	})
}

func TestStartupDiagnosticsStartupCancellation(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	db := newLifecycleTestDB()
	pingStarted := make(chan struct{})
	var pingOnce sync.Once
	db.pingFn = func(ctx context.Context) error {
		pingOnce.Do(func() {
			close(pingStarted)
		})
		<-ctx.Done()
		return ctx.Err()
	}
	deps := &lifecycleTestDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
		listen: func(context.Context, string) (net.Listener, error) {
			t.Fatal("startup cancellation reached listen")
			return nil, errors.New("unreachable")
		},
		newServer: func(*http.Server) runtimeServer {
			t.Fatal("startup cancellation reached server creation")
			return nil
		},
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var logs bytes.Buffer
	result := make(chan error, 1)
	go func() {
		result <- runWith(ctx, lifecycleTestLookup(lifecycleTestBaseValues()), &logs, deps.deps(), lifecycleTestLimits())
	}()

	lifecycleTestWaitSignal(t, pingStarted, "startup ping did not start")
	cancel()
	err := lifecycleTestWaitError(t, result, 2*time.Second)
	if !errors.Is(err, ErrStartup) {
		t.Fatal("startup cancellation did not return ErrStartup")
	}
	startupDiagnosticAssertSingle(t, logs.Bytes(), string(LogStagePersistence), string(LogReasonCanceled), string(LogErrorKindCanceled))
	startupDiagnosticWaitClose(t, db)
}

func TestStartupDiagnosticsLateListenCancellation(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	db := newLifecycleTestDB()
	listener := newLifecycleTestListener()
	expired := make(chan error, 1)
	deps := &lifecycleTestDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
		listen: func(ctx context.Context, _ string) (net.Listener, error) {
			<-ctx.Done()
			expired <- ctx.Err()
			return listener, nil
		},
		newServer: func(*http.Server) runtimeServer {
			t.Fatal("late listen cancellation reached server creation")
			return nil
		},
	}
	limits := lifecycleTestLimits()
	limits.startup = 100 * time.Millisecond
	var logs bytes.Buffer

	err := runWith(context.Background(), lifecycleTestLookup(lifecycleTestBaseValues()), &logs, deps.deps(), limits)
	if !errors.Is(err, ErrStartup) {
		t.Fatal("late listen cancellation did not return ErrStartup")
	}
	select {
	case ctxErr := <-expired:
		if !errors.Is(ctxErr, context.DeadlineExceeded) {
			t.Fatal("late listen did not observe the expired startup context")
		}
	case <-time.After(time.Second):
		t.Fatal("late listen did not report the startup context result")
	}
	startupDiagnosticAssertSingle(t, logs.Bytes(), string(LogStageEntry), string(LogReasonTimeout), string(LogErrorKindTimeout))
	select {
	case <-listener.closed:
	case <-time.After(time.Second):
		t.Fatal("late listen resource was not closed after startup cancellation")
	}
	startupDiagnosticWaitClose(t, db)
}

func TestStartupDiagnosticsWriterBlockingIsBounded(t *testing.T) {
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
		case <-writer.first:
			select {
			case <-writer.done:
			case <-time.After(time.Second):
				t.Error("blocked startup diagnostic writer did not return after cleanup release")
			}
		default:
		}
	})
	db := newLifecycleTestDB()
	db.pingFn = func(context.Context) error {
		return errors.New(startupDiagnosticCanary + " " + startupDiagnosticDSN)
	}
	deps := &lifecycleTestDeps{
		openDatabase: func(context.Context, string) (runtimeDatabase, error) { return db, nil },
		listen: func(context.Context, string) (net.Listener, error) {
			t.Fatal("ping exhaustion reached listen")
			return nil, errors.New("unreachable")
		},
		newServer: func(*http.Server) runtimeServer {
			t.Fatal("ping exhaustion reached server creation")
			return nil
		},
	}
	limits := lifecycleTestLimits()
	limits.startup = 100 * time.Millisecond
	limits.retry = 10 * time.Millisecond
	start := time.Now()
	result := make(chan error, 1)
	go func() {
		result <- runWith(context.Background(), lifecycleTestLookup(lifecycleTestBaseValues()), writer, deps.deps(), limits)
	}()

	select {
	case <-writer.first:
	case <-time.After(2 * time.Second):
		t.Fatal("startup failure did not attempt a diagnostic write")
	}
	err := lifecycleTestWaitError(t, result, 2*time.Second)
	if !errors.Is(err, ErrStartup) {
		t.Fatal("blocked startup diagnostic changed the startup error classification")
	}
	if elapsed := time.Since(start); elapsed >= time.Second {
		t.Fatal("blocked startup diagnostic exceeded the bounded exit budget")
	}
	if calls := writer.snapshot(); calls != 1 {
		t.Fatal("startup diagnostic attempted more than one write")
	}

	release()
	select {
	case <-writer.done:
	case <-time.After(time.Second):
		t.Fatal("blocked startup diagnostic writer did not return after release")
	}
	select {
	case <-writer.more:
		t.Fatal("runtime wrote again after a blocked startup diagnostic")
	case <-time.After(100 * time.Millisecond):
	}
	if calls := writer.snapshot(); calls != 1 {
		t.Fatal("runtime wrote more than one startup diagnostic after a blocked writer")
	}
	startupDiagnosticWaitClose(t, db)
}

func TestStartupDiagnosticsNormalPathUnchanged(t *testing.T) {
	lifecycleTestClearPGEnv(t)
	order := &lifecycleTestOrder{}
	db := newLifecycleTestDB()
	listener := newLifecycleTestListener()
	server := newLifecycleTestServer(order)
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
	var logs bytes.Buffer
	result := make(chan error, 1)
	go func() {
		result <- runWith(ctx, lifecycleTestLookup(lifecycleTestBaseValues()), &logs, deps.deps(), lifecycleTestLimits())
	}()

	lifecycleTestWaitSignal(t, server.started, "runtime server did not start")
	cancel()
	if err := lifecycleTestWaitError(t, result, 2*time.Second); err != nil {
		t.Fatal("normal runtime shutdown returned an error")
	}

	objects := logTestObjects(t, "normal startup", logs.Bytes())
	if len(objects) != 1 {
		t.Fatalf("normal startup wrote %d log objects, want 1", len(objects))
	}
	object := objects[0]
	logTestAssertAllowedFields(t, "normal startup", object)
	logTestAssertUTCTime(t, "normal startup", object)
	logTestWantString(t, "normal startup", object, "level", string(LogLevelInfo))
	logTestWantString(t, "normal startup", object, "event", string(LogEventDependency))
	logTestWantString(t, "normal startup", object, "stage", string(LogStagePersistence))
	logTestWantString(t, "normal startup", object, "reason", string(LogReasonDependency))
	logTestWantAbsent(t, "normal startup", object, "error_kind")
	startupDiagnosticAssertNoLeak(t, logs.Bytes())
}
