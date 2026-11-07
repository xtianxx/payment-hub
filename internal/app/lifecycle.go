package app

import (
	"context"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"strconv"
	"sync"
	"time"

	"payment-hub/internal/httpapi"
)

var errReadyzAdmissionClosed = errors.New("app: readiness admission closed")

func runWith(ctx context.Context, lookup func(string) (string, bool), writer io.Writer, deps runtimeDeps, limits runtimeLimits) error {
	if ctx == nil || ctx.Err() != nil || lookup == nil || writer == nil ||
		deps.openDatabase == nil || deps.listen == nil || deps.newServer == nil ||
		!validRuntimeLimits(limits) {
		return ErrRuntimeConfig
	}
	if _, err := LoadConfig(lookup); err != nil {
		return ErrRuntimeConfig
	}
	databaseURL, ok := lookup(databaseURLEnvName)
	if !ok || databaseURL == "" {
		return ErrRuntimeConfig
	}
	httpAddr, ok := lookup(httpAddrEnvName)
	if !ok || httpAddr == "" {
		return ErrRuntimeConfig
	}
	if !validRuntimeHTTPAddr(httpAddr) || runtimePGEnvInjected(lookup) {
		return ErrRuntimeConfig
	}
	testModeConfig, err := LoadTestModeConfig(lookup)
	if err != nil {
		return ErrRuntimeConfig
	}
	if _, err := parseRuntimeDatabaseConfig(databaseURL); err != nil {
		return ErrRuntimeConfig
	}

	logger := newRuntimeLogger(writer)
	startupCtx, cancelStartup := context.WithTimeout(ctx, limits.startup)
	defer cancelStartup()

	db, err := deps.openDatabase(startupCtx, databaseURL)
	if err != nil {
		return ErrStartup
	}
	var testMode *TestMode
	var listener net.Listener
	startupComplete := false
	defer func() {
		if startupComplete {
			return
		}
		if testMode != nil {
			_ = testMode.Close()
		}
		if listener != nil {
			_ = listener.Close()
		}
		_ = closeRuntimeDatabaseStartup(db, startupCtx, limits)
	}()

	for {
		if err := db.Ping(startupCtx); err == nil {
			if startupCtx.Err() != nil {
				return ErrStartup
			}
			break
		}
		if startupCtx.Err() != nil {
			return ErrStartup
		}
		timer := time.NewTimer(limits.retry)
		select {
		case <-startupCtx.Done():
			timer.Stop()
			return ErrStartup
		case <-timer.C:
		}
	}

	if err := db.CheckMigrations(startupCtx); err != nil {
		_ = logger.log(startupCtx, LogRecord{
			Level:  LogLevelError,
			Event:  LogEventDependency,
			Stage:  LogStagePersistence,
			Reason: LogReasonDependency,
			Err:    err,
		})
		return ErrStartup
	}
	if startupCtx.Err() != nil {
		return ErrStartup
	}
	if testModeConfig.Enabled {
		testMode, err = OpenTestMode(startupCtx, testModeConfig)
		if err != nil {
			return ErrStartup
		}
		if startupCtx.Err() != nil {
			return ErrStartup
		}
	}
	listener, err = deps.listen(startupCtx, httpAddr)
	if err != nil {
		return ErrStartup
	}
	if startupCtx.Err() != nil {
		return ErrStartup
	}

	gate := newReadyGate()
	workCtx, cancelWork := context.WithCancel(context.Background())
	defer cancelWork()
	check := func(checkCtx context.Context) error {
		done, admitted := gate.begin()
		if !admitted {
			return errReadyzAdmissionClosed
		}
		defer done()
		probeCtx, cancel := context.WithTimeout(checkCtx, limits.probe)
		defer cancel()
		return db.Ping(probeCtx)
	}
	router := httpapi.NewRouter(check)
	server := &http.Server{
		Handler:           router,
		ReadHeaderTimeout: limits.read,
		ReadTimeout:       limits.read,
		WriteTimeout:      limits.write,
		IdleTimeout:       0,
		ErrorLog:          log.New(io.Discard, "", 0),
		BaseContext: func(net.Listener) context.Context {
			return workCtx
		},
	}
	runtimeServer := deps.newServer(server)
	if runtimeServer == nil {
		return ErrRuntime
	}
	if err := logger.log(startupCtx, LogRecord{
		Level:  LogLevelInfo,
		Event:  LogEventDependency,
		Stage:  LogStagePersistence,
		Reason: LogReasonDependency,
	}); err != nil {
		return ErrRuntime
	}
	if startupCtx.Err() != nil {
		return ErrStartup
	}
	startupComplete = true

	serveDone := make(chan struct{})
	var serveErr error
	go func() {
		serveErr = runtimeServer.Serve(listener)
		close(serveDone)
	}()

	var runtimeErr error
	select {
	case <-ctx.Done():
	case <-serveDone:
		runtimeErr = ErrRuntime
	}

	shutdownErr := shutdownRuntime(runtimeServer, gate, testMode, db, listener, cancelWork, serveDone, limits)
	if shutdownErr != nil {
		return shutdownErr
	}
	if runtimeErr != nil {
		return runtimeErr
	}
	select {
	case <-serveDone:
		if !errors.Is(serveErr, http.ErrServerClosed) {
			return ErrRuntime
		}
	default:
		return ErrShutdown
	}
	return nil
}

func validRuntimeLimits(limits runtimeLimits) bool {
	return limits.startup > 0 &&
		limits.probe > 0 &&
		limits.read > 0 &&
		limits.write > 0 &&
		limits.shutdown > 0 &&
		limits.closeReserve >= 0 &&
		limits.closeReserve < limits.shutdown &&
		limits.retry > 0
}

func validRuntimeHTTPAddr(address string) bool {
	host, port, err := net.SplitHostPort(address)
	if err != nil || host == "" || port == "" {
		return false
	}
	_, err = strconv.ParseUint(port, 10, 16)
	return err == nil
}

func shutdownRuntime(server runtimeServer, gate *readyGate, testMode *TestMode, db runtimeDatabase, listener net.Listener, cancelWork context.CancelFunc, serveDone <-chan struct{}, limits runtimeLimits) error {
	gate.close()
	exitDeadline := time.Now().Add(limits.shutdown)
	exitCtx, cancelExit := context.WithDeadline(context.Background(), exitDeadline)
	defer cancelExit()

	shutdownCtx, cancelShutdown := context.WithDeadline(context.Background(), exitDeadline.Add(-limits.closeReserve))
	shutdownErr := server.Shutdown(shutdownCtx)
	cancelShutdown()

	forceCloseFailed := false
	if shutdownErr != nil {
		cancelWork()
		if err := server.Close(); err != nil && !errors.Is(err, net.ErrClosed) {
			forceCloseFailed = true
		}
	}
	listenerCloseFailed := false
	if listener != nil {
		if err := listener.Close(); err != nil && !errors.Is(err, net.ErrClosed) {
			listenerCloseFailed = true
		}
	}
	if forceCloseFailed || !gate.wait(exitCtx) || !waitServeDone(serveDone, exitCtx) {
		return ErrShutdown
	}
	if testMode != nil {
		if _, err := testMode.Seal(exitCtx); err != nil {
			_ = testMode.Close()
			return ErrShutdown
		}
		if err := testMode.Close(); err != nil {
			return ErrShutdown
		}
	}
	if !closeRuntimeDatabaseUntil(db, exitCtx) {
		return ErrShutdown
	}
	if shutdownErr != nil || listenerCloseFailed {
		return ErrShutdown
	}
	return nil
}

func waitServeDone(serveDone <-chan struct{}, ctx context.Context) bool {
	select {
	case <-serveDone:
		return true
	case <-ctx.Done():
		select {
		case <-serveDone:
			return true
		default:
			return false
		}
	}
}

func closeRuntimeDatabaseStartup(db runtimeDatabase, startupCtx context.Context, limits runtimeLimits) bool {
	wait := 2 * time.Second
	if deadline, ok := startupCtx.Deadline(); ok {
		if remaining := time.Until(deadline); remaining < wait {
			wait = remaining
		}
	}
	done := make(chan struct{})
	go func() {
		db.Close()
		close(done)
	}()
	if wait <= 0 {
		return false
	}
	timer := time.NewTimer(wait)
	defer timer.Stop()
	select {
	case <-done:
		return true
	case <-timer.C:
		select {
		case <-done:
			return true
		default:
			return false
		}
	}
}

func closeRuntimeDatabaseUntil(db runtimeDatabase, ctx context.Context) bool {
	done := make(chan struct{})
	go func() {
		db.Close()
		close(done)
	}()
	select {
	case <-done:
		return true
	case <-ctx.Done():
		select {
		case <-done:
			return true
		default:
			return false
		}
	}
}

type runtimeLogger struct {
	logger  *Logger
	mu      sync.Mutex
	blocked bool
}

func newRuntimeLogger(writer io.Writer) *runtimeLogger {
	return &runtimeLogger{logger: NewLogger(writer)}
}

func (logger *runtimeLogger) log(ctx context.Context, record LogRecord) error {
	logger.mu.Lock()
	if logger.blocked {
		logger.mu.Unlock()
		return ErrRuntime
	}
	logger.mu.Unlock()

	done := make(chan error, 1)
	go func() {
		done <- logger.logger.Log(record)
	}()
	select {
	case err := <-done:
		if err != nil {
			logger.mu.Lock()
			logger.blocked = true
			logger.mu.Unlock()
			return ErrRuntime
		}
		return nil
	case <-ctx.Done():
		logger.mu.Lock()
		logger.blocked = true
		logger.mu.Unlock()
		return ErrRuntime
	}
}

type readyGate struct {
	mu     sync.Mutex
	closed bool
	active int
	zero   chan struct{}
}

func newReadyGate() *readyGate {
	return &readyGate{}
}

func (gate *readyGate) begin() (func(), bool) {
	gate.mu.Lock()
	if gate.closed {
		gate.mu.Unlock()
		return nil, false
	}
	gate.active++
	gate.mu.Unlock()
	return func() {
		gate.mu.Lock()
		gate.active--
		if gate.active == 0 && gate.zero != nil {
			close(gate.zero)
			gate.zero = nil
		}
		gate.mu.Unlock()
	}, true
}

func (gate *readyGate) close() {
	gate.mu.Lock()
	gate.closed = true
	gate.mu.Unlock()
}

func (gate *readyGate) wait(ctx context.Context) bool {
	gate.mu.Lock()
	if gate.active == 0 {
		gate.mu.Unlock()
		return true
	}
	if gate.zero == nil {
		gate.zero = make(chan struct{})
	}
	zero := gate.zero
	gate.mu.Unlock()
	select {
	case <-zero:
		return true
	case <-ctx.Done():
		gate.mu.Lock()
		active := gate.active
		gate.mu.Unlock()
		return active == 0
	}
}
