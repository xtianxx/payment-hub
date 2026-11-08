package harness

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"sync"
	"syscall"
	"testing"
	"time"
)

const (
	t020LifecycleMarker  = "PAYMENT_HUB_T020_LIFECYCLE_HELPER"
	t020LifecycleMode    = "PAYMENT_HUB_T020_LIFECYCLE_MODE"
	t020LifecycleResult  = "PAYMENT_HUB_T020_LIFECYCLE_RESULT"
	t020LifecycleArgRun  = "-test.run=^TestHarnessLifecycleHelper$"
	t020LifecycleExit    = "leader-exit"
	t020LifecycleHeldOK  = "held-valid"
	t020LifecycleHeldBad = "held-invalid"
	t020LifecycleEarly   = "early-bad"
	t020LifecycleEnv     = "env-check"
	t020LifecycleTermAck = "term-ack"
)

func t020LifecycleEnvFor(mode string, extra ...string) []string {
	env := []string{
		t020LifecycleMarker + "=1",
		t020LifecycleMode + "=" + mode,
		"TMPDIR=/tmp/opencode",
		"TZ=UTC",
		"GORACE=atexit_sleep_ms=0",
	}
	return append(env, extra...)
}

func t020StartLifecycleProcess(t *testing.T, artifacts *Artifacts, generation uint64, mode string, grace time.Duration, extraEnv ...string) (*Process, context.CancelFunc) {
	t.Helper()
	lifetime, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	process, err := StartProcess(lifetime, ProcessConfig{
		Path:       t020HelperPath(t),
		Dir:        t020TempDir(t),
		Args:       []string{t020LifecycleArgRun},
		Env:        t020LifecycleEnvFor(mode, extraEnv...),
		Generation: generation,
		Grace:      grace,
		Artifacts:  artifacts,
	})
	if err != nil {
		cancel()
		t.Fatal("start t020 lifecycle helper failed")
	}
	t.Cleanup(func() {
		cancel()
		ctx, stopCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer stopCancel()
		_, _ = process.Stop(ctx)
	})
	return process, cancel
}

type t020TermAck struct {
	path  string
	ready <-chan struct{}
	term  <-chan struct{}
	err   <-chan error
}

func t020TermAckSocket(t *testing.T) *t020TermAck {
	t.Helper()
	dir := t020TempDir(t)
	path := filepath.Join(dir, "ack")
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		t.Fatal("create t020 term ack socket failed")
	}
	listener.SetUnlinkOnClose(false)
	ready := make(chan struct{})
	term := make(chan struct{})
	errCh := make(chan error, 1)
	done := make(chan struct{})
	go func() {
		defer close(done)
		conn, err := listener.AcceptUnix()
		if err != nil {
			select {
			case errCh <- err:
			default:
			}
			return
		}
		defer conn.Close()
		if err := conn.SetReadDeadline(time.Now().Add(5 * time.Second)); err != nil {
			select {
			case errCh <- err:
			default:
			}
			return
		}
		buffer := make([]byte, len("ready\n"))
		if _, err := io.ReadFull(conn, buffer); err != nil || string(buffer) != "ready\n" {
			select {
			case errCh <- errors.New("term ack ready protocol mismatch"):
			default:
			}
			return
		}
		close(ready)
		if err := conn.SetReadDeadline(time.Now().Add(5 * time.Second)); err != nil {
			select {
			case errCh <- err:
			default:
			}
			return
		}
		buffer = make([]byte, len("term\n"))
		if _, err := io.ReadFull(conn, buffer); err != nil || string(buffer) != "term\n" {
			select {
			case errCh <- errors.New("term ack protocol mismatch"):
			default:
			}
			return
		}
		close(term)
	}()
	t.Cleanup(func() {
		_ = listener.Close()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Errorf("t020 term ack listener did not stop")
		}
		_ = os.Remove(path)
	})
	return &t020TermAck{path: path, ready: ready, term: term, err: errCh}
}

func (a *t020TermAck) awaitReady(t *testing.T) {
	t.Helper()
	select {
	case <-a.ready:
	case err := <-a.err:
		_ = err
		t.Fatal("t020 term ack ready fixture failed")
	case <-time.After(5 * time.Second):
		t.Fatal("t020 term ack ready timed out")
	}
}

func (a *t020TermAck) awaitTerm(t *testing.T) {
	t.Helper()
	select {
	case <-a.term:
	case err := <-a.err:
		_ = err
		t.Fatal("t020 term ack fixture failed")
	case <-time.After(5 * time.Second):
		t.Fatal("t020 term ack timed out")
	}
}

type t020Held struct {
	path    string
	ready   <-chan struct{}
	release chan struct{}
	err     <-chan error
}

func t020HeldSocket(t *testing.T) *t020Held {
	t.Helper()
	dir := t020TempDir(t)
	path := filepath.Join(dir, "held")
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if err != nil {
		t.Fatal("create t020 held socket failed")
	}
	listener.SetUnlinkOnClose(false)
	ready := make(chan struct{})
	release := make(chan struct{})
	errCh := make(chan error, 1)
	done := make(chan struct{})
	go func() {
		defer close(done)
		conn, err := listener.AcceptUnix()
		if err != nil {
			select {
			case errCh <- err:
			default:
			}
			return
		}
		defer conn.Close()
		if err := conn.SetReadDeadline(time.Now().Add(5 * time.Second)); err != nil {
			select {
			case errCh <- err:
			default:
			}
			return
		}
		buffer := make([]byte, len("ready\n"))
		if _, err := io.ReadFull(conn, buffer); err != nil || string(buffer) != "ready\n" {
			select {
			case errCh <- errors.New("held ready protocol mismatch"):
			default:
			}
			return
		}
		close(ready)
		select {
		case <-release:
			if err := conn.SetWriteDeadline(time.Now().Add(5 * time.Second)); err != nil {
				select {
				case errCh <- err:
				default:
				}
				return
			}
			if _, err := conn.Write([]byte("go\n")); err != nil {
				select {
				case errCh <- err:
				default:
				}
			}
		case <-time.After(10 * time.Second):
			select {
			case errCh <- errors.New("held release timed out"):
			default:
			}
		}
	}()
	t.Cleanup(func() {
		_ = listener.Close()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Errorf("t020 held listener did not stop")
		}
		_ = os.Remove(path)
	})
	return &t020Held{path: path, ready: ready, release: release, err: errCh}
}

func (h *t020Held) awaitReady(t *testing.T) {
	t.Helper()
	select {
	case <-h.ready:
	case err := <-h.err:
		_ = err
		t.Fatal("t020 held fixture failed")
	case <-time.After(5 * time.Second):
		t.Fatal("t020 held ready timed out")
	}
}

func (h *t020Held) releaseNow() {
	select {
	case <-h.release:
	default:
		close(h.release)
	}
}

func t020SafeWait(t *testing.T, process *Process, ctx context.Context) (Exit, error) {
	t.Helper()
	var exit Exit
	var err error
	func() {
		defer func() {
			if recover() != nil {
				t.Error("t020 process wait panicked")
			}
		}()
		exit, err = process.Wait(ctx)
	}()
	return exit, err
}

func t020SafeStop(t *testing.T, process *Process, ctx context.Context) (Exit, error) {
	t.Helper()
	var exit Exit
	var err error
	func() {
		defer func() {
			if recover() != nil {
				t.Error("t020 process stop panicked")
			}
		}()
		exit, err = process.Stop(ctx)
	}()
	return exit, err
}

func TestLifecycleZeroValueWaitStop(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	var zero Process
	start := time.Now()
	if _, err := t020SafeWait(t, &zero, ctx); !errors.Is(err, ErrProcess) {
		t.Fatal("zero t020 process wait did not return ErrProcess")
	}
	if time.Since(start) > 500*time.Millisecond {
		t.Fatal("zero t020 process wait blocked")
	}
	start = time.Now()
	if _, err := t020SafeStop(t, &zero, ctx); !errors.Is(err, ErrProcess) {
		t.Fatal("zero t020 process stop did not return ErrProcess")
	}
	if time.Since(start) > 500*time.Millisecond {
		t.Fatal("zero t020 process stop blocked")
	}
	var nilProcess *Process
	if _, err := nilProcess.Wait(ctx); !errors.Is(err, ErrProcess) {
		t.Fatal("nil t020 process wait did not return ErrProcess")
	}
	if _, err := nilProcess.Stop(ctx); !errors.Is(err, ErrProcess) {
		t.Fatal("nil t020 process stop did not return ErrProcess")
	}
}

func TestLifecycleDrainReadErrorIncomplete(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	ready := t020ReadySocket(t)
	process, cancel := t020StartHelper(t, artifacts, 1, t020HelperModeIgnore, 2*time.Second,
		t020HelperReadySocket+"="+ready.path)
	defer cancel()
	t020AwaitReady(t, ready)
	if process.stdoutRead == nil {
		t.Fatal("t020 process stdout read handle is missing")
	}
	if err := process.stdoutRead.Close(); err != nil {
		t.Fatal("close t020 process stdout read handle failed")
	}
	ctx, waitCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer waitCancel()
	exit, err := process.Wait(ctx)
	if !errors.Is(err, ErrProcess) && !errors.Is(err, ErrArtifacts) {
		t.Fatal("t020 stdout read failure did not fail the process")
	}
	if exit.OutputComplete || !exit.Reaped || !exit.GroupGone {
		t.Fatal("t020 stdout read failure marked output complete")
	}
	path, finishErr := artifacts.Finish()
	if !errors.Is(finishErr, ErrArtifacts) {
		t.Fatal("t020 stdout read failure produced a complete report")
	}
	t020RequireReportFailure(t, path, "process_incomplete")
}

func TestLifecycleLifetimeDuringLongGrace(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	ack := t020TermAckSocket(t)
	lifetime, cancelLifetime := context.WithTimeout(context.Background(), 30*time.Second)
	process, err := StartProcess(lifetime, ProcessConfig{
		Path:       t020HelperPath(t),
		Dir:        t020TempDir(t),
		Args:       []string{t020LifecycleArgRun},
		Env:        t020LifecycleEnvFor(t020LifecycleTermAck, t020HelperReadySocket+"="+ack.path),
		Generation: 1,
		Grace:      5 * time.Second,
		Artifacts:  artifacts,
	})
	if err != nil {
		cancelLifetime()
		t.Fatal("start t020 term ack helper failed")
	}
	t.Cleanup(func() {
		cancelLifetime()
		ctx, stopCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer stopCancel()
		_, _ = process.Stop(ctx)
	})
	ack.awaitReady(t)
	type result struct {
		exit Exit
		err  error
	}
	stopResult := make(chan result, 1)
	go func() {
		ctx, stopCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer stopCancel()
		exit, err := process.Stop(ctx)
		stopResult <- result{exit: exit, err: err}
	}()
	ack.awaitTerm(t)
	cancelLifetime()
	select {
	case got := <-stopResult:
		if !errors.Is(got.err, ErrProcess) || !got.exit.Forced ||
			!got.exit.Reaped || !got.exit.GroupGone {
			t.Fatal("t020 lifetime cancellation did not force the long grace stop")
		}
	case <-time.After(time.Second):
		t.Fatal("t020 lifetime cancellation did not shorten the long grace stop")
	}
}

func TestLifecycleWaitDeadlineFlags(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	ready := t020ReadySocket(t)
	process, cancel := t020StartHelper(t, artifacts, 1, t020HelperModeIgnore, 2*time.Second,
		t020HelperReadySocket+"="+ready.path)
	defer cancel()
	t020AwaitReady(t, ready)
	target, targetCancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer targetCancel()
	fallback, fallbackCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer fallbackCancel()
	exit, err := process.Wait(target)
	if !errors.Is(target.Err(), context.DeadlineExceeded) {
		t.Fatal("t020 wait target deadline did not expire")
	}
	if fallback.Err() != nil {
		t.Fatal("t020 wait cleanup exceeded the fallback budget")
	}
	if !errors.Is(err, ErrProcess) || !exit.Forced ||
		!exit.Reaped || !exit.OutputComplete || !exit.GroupGone {
		t.Fatal("t020 wait deadline did not reap actual resources")
	}
	if !exit.TimedOut || exit.Canceled {
		t.Fatal("t020 wait deadline flags did not distinguish timeout from cancellation")
	}
}

func TestLifecycleCollectorFaultSignalNonBlocking(t *testing.T) {
	dummy := &Process{
		events:   make(chan processEvent, processEventBuffer),
		terminal: make(chan struct{}),
	}
	for i := 0; i < processEventBuffer; i++ {
		dummy.events <- processEvent{kind: processEventStop}
	}
	collector := newOutputCollector(nil, 1, dummy)
	done := make(chan struct{})
	go func() {
		collector.feed("stdout", []byte("{bad}\n"))
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(500 * time.Millisecond):
		close(dummy.terminal)
		select {
		case <-done:
		case <-time.After(time.Second):
		}
		t.Fatal("t020 collector fault notification blocked on the process event channel")
	}
	close(dummy.terminal)
}

func TestLifecycleStopContextShortensForce(t *testing.T) {
	t.Run("single_short_context", func(t *testing.T) {
		source := t020SourceFixture(t)
		artifacts, _ := t020BeginArtifacts(t, source)
		ready := t020ReadySocket(t)
		process, cancel := t020StartHelper(t, artifacts, 1, t020HelperModeIgnore, 2*time.Second,
			t020HelperReadySocket+"="+ready.path)
		defer cancel()
		t020AwaitReady(t, ready)
		stopCtx, stopCancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
		defer stopCancel()
		outer, outerCancel := context.WithTimeout(context.Background(), time.Second)
		defer outerCancel()
		start := time.Now()
		exit, err := process.Stop(stopCtx)
		if !errors.Is(stopCtx.Err(), context.DeadlineExceeded) {
			t.Fatal("short t020 stop context did not reach its deadline")
		}
		if outer.Err() != nil || time.Since(start) > 900*time.Millisecond {
			t.Fatal("short t020 stop context did not force cleanup in the outer budget")
		}
		if !errors.Is(err, ErrProcess) || !exit.Forced || !exit.Reaped || !exit.GroupGone {
			t.Fatal("short t020 stop context did not return the reaped forced exit")
		}
		if !exit.Canceled || !exit.TimedOut {
			t.Fatal("short t020 stop context did not retain explicit cancellation and deadline timeout")
		}
	})

	t.Run("concurrent_shorter_request", func(t *testing.T) {
		source := t020SourceFixture(t)
		artifacts, _ := t020BeginArtifacts(t, source)
		ready := t020ReadySocket(t)
		process, cancel := t020StartHelper(t, artifacts, 1, t020HelperModeIgnore, 5*time.Second,
			t020HelperReadySocket+"="+ready.path)
		defer cancel()
		t020AwaitReady(t, ready)
		type result struct {
			exit Exit
			err  error
		}
		longResult := make(chan result, 1)
		go func() {
			ctx, stopCancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer stopCancel()
			exit, err := process.Stop(ctx)
			longResult <- result{exit: exit, err: err}
		}()
		time.Sleep(20 * time.Millisecond)
		shortCtx, shortCancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
		defer shortCancel()
		start := time.Now()
		shortExit, shortErr := process.Stop(shortCtx)
		if !errors.Is(shortCtx.Err(), context.DeadlineExceeded) {
			t.Fatal("shorter concurrent t020 stop did not reach its deadline")
		}
		if time.Since(start) > time.Second || !shortExit.Forced {
			t.Fatal("shorter concurrent t020 stop did not shorten the force budget")
		}
		select {
		case long := <-longResult:
			if long.exit != shortExit || !errors.Is(long.err, ErrProcess) || !errors.Is(shortErr, ErrProcess) {
				t.Fatal("concurrent t020 stop requests did not share the forced exit")
			}
		case <-time.After(time.Second):
			t.Fatal("long t020 stop request was not shortened by the later request")
		}
	})
}

func TestLifecycleAdmissionFinish(t *testing.T) {
	t.Run("reserved_starting", func(t *testing.T) {
		source := t020SourceFixture(t)
		artifacts, _ := t020BeginArtifacts(t, source)
		if err := artifacts.reserveGeneration(1); err != nil {
			t.Fatal("reserve t020 generation failed")
		}
		path, err := artifacts.Finish()
		if !errors.Is(err, ErrArtifacts) {
			t.Fatal("finish accepted a reserved starting generation")
		}
		t020RequireReportFailure(t, path, "process_incomplete")
		if _, err := artifacts.Finish(); !errors.Is(err, ErrArtifacts) {
			t.Fatal("finish recovered after a starting admission")
		}
	})

	t.Run("record_finish_race", func(t *testing.T) {
		source := t020SourceFixture(t)
		artifacts, _ := t020BeginArtifacts(t, source)
		var wg sync.WaitGroup
		wg.Add(1)
		go func() {
			defer wg.Done()
			for i := 0; i < 512; i++ {
				_ = artifacts.RecordHTTP(HTTPObservation{Status: 200})
			}
		}()
		path, err := artifacts.Finish()
		wg.Wait()
		if err == nil {
			report, _ := t020ReadReport(t, path)
			if !report.CollectionComplete {
				t.Fatal("successful t020 finish reported incomplete collection")
			}
			return
		}
		if !errors.Is(err, ErrArtifacts) || path == "" {
			t.Fatal("t020 concurrent finish returned an unexpected error")
		}
	})
}

func TestLifecycleFinishIncludesOutputCompletedDuringSeal(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	first, firstCancel := t020StartHelper(t, artifacts, 1, t020HelperModeExit0, time.Second)
	defer firstCancel()
	firstCtx, firstWaitCancel := context.WithTimeout(context.Background(), 5*time.Second)
	if _, err := first.Wait(firstCtx); err != nil {
		firstWaitCancel()
		t.Fatal("wait first t020 seal helper failed")
	}
	firstWaitCancel()

	held := t020HeldSocket(t)
	second, secondCancel := t020StartLifecycleProcess(t, artifacts, 2, t020LifecycleHeldOK, time.Second,
		t020HelperReadySocket+"="+held.path)
	defer secondCancel()
	held.awaitReady(t)

	artifacts.mu.Lock()
	var locked *Process
	for _, admission := range artifacts.admissions {
		if admission != nil && admission.generation == 1 {
			locked = admission.process
		}
	}
	artifacts.mu.Unlock()
	if locked == nil {
		t.Fatal("t020 seal first process is missing")
	}
	locked.mu.Lock()
	var unlockOnce sync.Once
	unlockFirst := func() {
		unlockOnce.Do(func() {
			locked.mu.Unlock()
		})
	}

	type finishResult struct {
		path string
		err  error
	}
	var finished finishResult
	finishDone := make(chan struct{})
	go func() {
		finished.path, finished.err = artifacts.Finish()
		close(finishDone)
	}()

	defer func() {
		held.releaseNow()
		waitCtx, waitCancel := context.WithTimeout(context.Background(), 5*time.Second)
		_, _ = second.Wait(waitCtx)
		waitCancel()
		unlockFirst()
		select {
		case <-finishDone:
		case <-time.After(5 * time.Second):
			t.Errorf("t020 seal finish did not complete during cleanup")
		}
	}()

	deadline := time.Now().Add(2 * time.Second)
	for {
		artifacts.mu.Lock()
		sealed := artifacts.sealed
		artifacts.mu.Unlock()
		if sealed {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("t020 seal finish did not pass the admission lock")
		}
		time.Sleep(time.Millisecond)
	}

	held.releaseNow()
	waitCtx, waitCancel := context.WithTimeout(context.Background(), 5*time.Second)
	secondExit, secondErr := second.Wait(waitCtx)
	waitCancel()
	if secondErr != nil || !secondExit.Reaped || !secondExit.OutputComplete || !secondExit.GroupGone {
		t.Fatal("t020 seal second helper did not finish cleanly")
	}
	unlockFirst()

	select {
	case <-finishDone:
	case <-time.After(5 * time.Second):
		t.Fatal("t020 seal finish did not complete")
	}
	if finished.err != nil {
		t.Fatal("t020 seal finish failed after late output completed")
	}
	report, _ := t020ReadReport(t, finished.path)
	if !report.CollectionComplete || len(report.Logs) != 2 {
		t.Fatal("t020 seal finish omitted output completed during sealing")
	}
	generations := map[uint64]bool{}
	for _, log := range report.Logs {
		generations[log.Generation] = true
	}
	if !generations[1] || !generations[2] {
		t.Fatal("t020 seal finish log generations mismatch")
	}
}

func TestLifecycleOutputAfterFailedFinish(t *testing.T) {
	t.Run("valid_late_output", func(t *testing.T) {
		source := t020SourceFixture(t)
		artifacts, _ := t020BeginArtifacts(t, source)
		held := t020HeldSocket(t)
		process, cancel := t020StartLifecycleProcess(t, artifacts, 1, t020LifecycleHeldOK, time.Second,
			t020HelperReadySocket+"="+held.path)
		defer cancel()
		held.awaitReady(t)
		path, err := artifacts.Finish()
		if !errors.Is(err, ErrArtifacts) {
			t.Fatal("finish accepted a held un-waited process")
		}
		t020RequireReportFailure(t, path, "process_incomplete")
		held.releaseNow()
		ctx, waitCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer waitCancel()
		exit, waitErr := process.Wait(ctx)
		if waitErr != nil || !exit.Reaped || !exit.OutputComplete || !exit.GroupGone {
			t.Fatal("valid late t020 output did not finish cleanly")
		}
		artifacts.mu.Lock()
		count := len(artifacts.logs)
		artifacts.mu.Unlock()
		if count != 1 {
			t.Fatal("valid late t020 output was not retained for collector cleanup")
		}
		if _, err := artifacts.Finish(); !errors.Is(err, ErrArtifacts) {
			t.Fatal("failed t020 finish was not permanently sealed")
		}
	})

	t.Run("invalid_late_output", func(t *testing.T) {
		source := t020SourceFixture(t)
		artifacts, _ := t020BeginArtifacts(t, source)
		held := t020HeldSocket(t)
		process, cancel := t020StartLifecycleProcess(t, artifacts, 1, t020LifecycleHeldBad, time.Second,
			t020HelperReadySocket+"="+held.path)
		defer cancel()
		held.awaitReady(t)
		path, err := artifacts.Finish()
		if !errors.Is(err, ErrArtifacts) {
			t.Fatal("finish accepted a held un-waited process")
		}
		t020RequireReportFailure(t, path, "process_incomplete")
		held.releaseNow()
		ctx, waitCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer waitCancel()
		exit, waitErr := process.Wait(ctx)
		if !errors.Is(waitErr, ErrArtifacts) && !errors.Is(waitErr, ErrProcess) {
			t.Fatal("invalid late t020 output did not fail")
		}
		if exit.OutputComplete || !exit.Reaped || !exit.GroupGone {
			t.Fatal("invalid late t020 output exit mismatch")
		}
		if _, err := artifacts.Finish(); !errors.Is(err, ErrArtifacts) {
			t.Fatal("failed t020 finish was not permanently sealed")
		}
	})
}

func TestLifecycleLeaderExitRevokesGroupSignal(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	leaderReady := t020GroupReadySocket(t)
	leader, cancel := t020StartLifecycleProcess(t, artifacts, 1, t020LifecycleExit, 2*time.Second,
		t020HelperReadySocket+"="+leaderReady.path)
	defer cancel()
	leaderIdentity := t020AwaitGroupReady(t, leaderReady)
	if leaderIdentity.Pid != leaderIdentity.Pgid || leaderIdentity.Pgid <= 1 ||
		leaderIdentity.Pgid == syscall.Getpgrp() {
		t.Fatal("t020 leader exit group is unsafe")
	}

	memberReady := t020GroupReadySocket(t)
	member := t020StartDirectMember(t, leaderIdentity.Pgid, memberReady.path)
	memberIdentity := t020AwaitGroupReady(t, memberReady)
	if memberIdentity.Pgid != leaderIdentity.Pgid ||
		memberIdentity.Pid != member.cmd.Process.Pid ||
		memberIdentity.Pid == leaderIdentity.Pid || memberIdentity.Pid <= 1 {
		t.Fatal("t020 leader exit member group mismatch")
	}

	if err := syscall.Kill(leaderIdentity.Pid, syscall.SIGUSR1); err != nil {
		t.Fatal("signal t020 leader exit failed")
	}
	deadline := time.Now().Add(2 * time.Second)
	for {
		err := syscall.Kill(leaderIdentity.Pid, 0)
		if errors.Is(err, syscall.ESRCH) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("t020 leader was not reaped after SIGUSR1")
		}
		time.Sleep(5 * time.Millisecond)
	}

	ctx, stopCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer stopCancel()
	exit, err := leader.Stop(ctx)
	if !errors.Is(err, ErrProcess) {
		t.Fatal("t020 stop after leader exit did not return ErrProcess")
	}
	if !exit.Reaped || !exit.OutputComplete {
		t.Fatal("t020 stop after leader exit lost the reaped exit")
	}
	if exit.GroupGone {
		t.Fatal("t020 stop after leader exit falsely confirmed group absence")
	}
	if err := syscall.Kill(member.cmd.Process.Pid, 0); err != nil {
		t.Fatal("t020 stop after leader exit killed the direct member")
	}

	member.killAndWait(t)
	groupDeadline := time.Now().Add(2 * time.Second)
	for {
		err := syscall.Kill(-leaderIdentity.Pgid, 0)
		if errors.Is(err, syscall.ESRCH) {
			break
		}
		if time.Now().After(groupDeadline) {
			t.Fatal("t020 leader exit group did not disappear after member cleanup")
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func TestLifecycleEarlyOutputFault(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	process, cancel := t020StartLifecycleProcess(t, artifacts, 1, t020LifecycleEarly, 200*time.Millisecond)
	defer cancel()
	ctx, waitCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer waitCancel()
	type result struct {
		exit Exit
		err  error
	}
	waited := make(chan result, 1)
	go func() {
		exit, err := process.Wait(ctx)
		waited <- result{exit: exit, err: err}
	}()
	select {
	case got := <-waited:
		if got.err == nil || got.exit.OutputComplete || !got.exit.Reaped || !got.exit.GroupGone {
			t.Fatal("early invalid t020 output did not fail and reap")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("early invalid t020 output did not finish within the outer budget")
	}
	if _, err := artifacts.Finish(); !errors.Is(err, ErrArtifacts) {
		t.Fatal("early invalid t020 output produced a complete report")
	}
}

func TestLifecycleObserverBinding(t *testing.T) {
	t.Run("unclosed_after_record", func(t *testing.T) {
		source := t020SourceFixture(t)
		artifacts, _ := t020BeginArtifacts(t, source)
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)
		obsTestSealZero(t, h, 1)
		if err := artifacts.RecordObserver(h.obs); err != nil {
			t.Fatal("record trusted t020 observer failed")
		}
		if err := h.obs.Expect(2); err != nil {
			t.Fatal("expect later t020 observer generation failed")
		}
		if err := h.close(); err != nil {
			t.Fatal("close unclosed t020 observer failed")
		}
		path, err := artifacts.Finish()
		if !errors.Is(err, ErrArtifacts) {
			t.Fatal("finish accepted an unclosed later observer generation")
		}
		t020RequireReportFailure(t, path, "observer_uncertain")
	})

	t.Run("replaced_instance", func(t *testing.T) {
		source := t020SourceFixture(t)
		artifacts, _ := t020BeginArtifacts(t, source)
		h := obsTestStart(t, obsTestBarrier)
		obsTestExpectOpen(t, h, 1, obsTestBegin1)
		obsTestSealZero(t, h, 1)
		if err := artifacts.RecordObserver(h.obs); err != nil {
			t.Fatal("record first trusted t020 observer failed")
		}
		other := t020StartLifecycleObserver(t, obsTestWrongEpoch)
		if err := other.obs.Expect(1); err != nil {
			t.Fatal("expect replaced t020 observer generation failed")
		}
		begin := obsTestBeginFrame(1, obsTestBegin1)
		begin.Epoch = obsTestWrongEpoch
		obsTestCheckAck(t, begin, obsTestMustDo(t, other, begin))
		if _, err := other.obs.Seal(1, 0); err != nil {
			t.Fatal("seal replaced t020 observer generation failed")
		}
		if err := artifacts.RecordObserver(other.obs); !errors.Is(err, ErrArtifacts) {
			t.Fatal("replaced t020 observer instance was accepted")
		}
		if _, err := artifacts.Finish(); !errors.Is(err, ErrArtifacts) {
			t.Fatal("finish accepted a replaced observer instance")
		}
	})
}

func t020StartLifecycleObserver(t *testing.T, epoch string) *obsTestHarness {
	t.Helper()
	dir := obsTestNewDir(t)
	socket := filepath.Join(dir, "s")
	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	observer, err := StartObserver(ctx, ObserverConfig{
		Socket:         socket,
		RunID:          obsTestRunID,
		Epoch:          epoch,
		BarrierTimeout: obsTestBarrier,
	})
	if err != nil {
		t.Fatal("start replaced t020 observer failed")
	}
	h := &obsTestHarness{t: t, obs: observer, socket: socket, dir: dir}
	t.Cleanup(h.cleanup)
	return h
}

func TestLifecycleArtifactBoundaries(t *testing.T) {
	t.Run("budget_elapsed_over_hour", func(t *testing.T) {
		source := t020SourceFixture(t)
		artifacts, _ := t020BeginArtifacts(t, source)
		if err := artifacts.RecordBudget(BudgetObservation{
			Stage: BudgetStartup, Limit: time.Second, Elapsed: time.Hour + time.Nanosecond,
		}); !errors.Is(err, ErrArtifacts) {
			t.Fatal("t020 budget elapsed over one hour was accepted")
		}
		if _, err := artifacts.Finish(); !errors.Is(err, ErrArtifacts) {
			t.Fatal("t020 finish recovered after an over-hour budget")
		}
	})

	t.Run("goose_without_v", func(t *testing.T) {
		source := t020SourceFixture(t)
		root := t020TempDir(t)
		if _, err := BeginArtifacts(ArtifactConfig{
			Root: root, SourceRoot: source, RunID: t020RunID,
			Versions: Versions{Goose: "3.29.0"},
		}); !errors.Is(err, ErrArtifacts) {
			t.Fatal("t020 goose version without v was accepted")
		}
	})

	t.Run("goose_with_v", func(t *testing.T) {
		source := t020SourceFixture(t)
		root := t020TempDir(t)
		artifacts, err := BeginArtifacts(ArtifactConfig{
			Root: root, SourceRoot: source, RunID: t020RunID,
			Versions: Versions{Postgres: "16.15", Goose: "v3.29.0"},
		})
		if err != nil {
			t.Fatal("approved t020 versions were rejected")
		}
		if _, err := artifacts.Finish(); err != nil {
			t.Fatal("finish with approved t020 versions failed")
		}
	})

	t.Run("ancestor_symlink", func(t *testing.T) {
		source := t020SourceFixture(t)
		base := t020TempDir(t)
		real := filepath.Join(base, "real")
		if err := os.Mkdir(real, 0o700); err != nil {
			t.Fatal("create t020 real root failed")
		}
		root := filepath.Join(real, "root")
		if err := os.Mkdir(root, 0o700); err != nil {
			t.Fatal("create t020 nested root failed")
		}
		link := filepath.Join(base, "link")
		if err := os.Symlink(real, link); err != nil {
			t.Fatal("create t020 ancestor symlink failed")
		}
		if _, err := BeginArtifacts(ArtifactConfig{
			Root: filepath.Join(link, "root"), SourceRoot: source, RunID: t020RunID,
		}); !errors.Is(err, ErrArtifacts) {
			t.Fatal("t020 ancestor symlink root was accepted")
		}
	})
}

func TestLifecycleExplicitEnvNoGorace(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	resultPath := filepath.Join(t020TempDir(t), "result.json")
	env := []string{
		t020LifecycleMarker + "=1",
		t020LifecycleMode + "=" + t020LifecycleEnv,
		t020LifecycleResult + "=" + resultPath,
		"TMPDIR=/tmp/opencode",
		"TZ=UTC",
	}
	lifetime, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	process, err := StartProcess(lifetime, ProcessConfig{
		Path:       t020HelperPath(t),
		Dir:        t020TempDir(t),
		Args:       []string{t020LifecycleArgRun},
		Env:        env,
		Generation: 1,
		Grace:      time.Second,
		Artifacts:  artifacts,
	})
	if err != nil {
		t.Fatal("start t020 env helper failed")
	}
	ctx, waitCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer waitCancel()
	if _, err := process.Wait(ctx); err != nil {
		t.Fatal("wait t020 env helper failed")
	}
	data, err := os.ReadFile(resultPath)
	if err != nil {
		t.Fatal("read t020 env helper result failed")
	}
	var result struct {
		GORACE bool `json:"gorace"`
	}
	if err := json.Unmarshal(data, &result); err != nil {
		t.Fatal("decode t020 env helper result failed")
	}
	if result.GORACE {
		t.Fatal("production injected GORACE into the explicit child environment")
	}
	if _, err := artifacts.Finish(); err != nil {
		t.Fatal("finish t020 env helper artifacts failed")
	}
}

func TestHarnessLifecycleHelper(t *testing.T) {
	if os.Getenv(t020LifecycleMarker) != "1" {
		return
	}
	mode := os.Getenv(t020LifecycleMode)
	switch mode {
	case t020LifecycleExit:
		ch := make(chan os.Signal, 1)
		signal.Notify(ch, syscall.SIGUSR1)
		signal.Ignore(syscall.SIGTERM)
		t020HelperGroupReady()
		<-ch
		os.Exit(0)
	case t020LifecycleHeldOK, t020LifecycleHeldBad:
		path := os.Getenv(t020HelperReadySocket)
		conn, err := net.Dial("unix", path)
		if err != nil {
			os.Exit(2)
		}
		if _, err := conn.Write([]byte("ready\n")); err != nil {
			_ = conn.Close()
			os.Exit(2)
		}
		buffer := make([]byte, len("go\n"))
		if _, err := io.ReadFull(conn, buffer); err != nil || string(buffer) != "go\n" {
			_ = conn.Close()
			os.Exit(2)
		}
		if err := conn.Close(); err != nil {
			os.Exit(2)
		}
		if mode == t020LifecycleHeldBad {
			if !t020HelperWriteStdout(t020UnknownLogLine()) {
				os.Exit(2)
			}
			os.Exit(0)
		}
		if !t020HelperWriteStdout(t020ValidLogLine()) {
			os.Exit(2)
		}
		os.Exit(0)
	case t020LifecycleEarly:
		signal.Ignore(syscall.SIGTERM)
		if !t020HelperWriteStdout("{bad-early-output}\n") {
			os.Exit(2)
		}
		for {
			time.Sleep(time.Hour)
		}
	case t020LifecycleTermAck:
		ch := make(chan os.Signal, 1)
		signal.Notify(ch, syscall.SIGTERM)
		path := os.Getenv(t020HelperReadySocket)
		conn, err := net.Dial("unix", path)
		if err != nil {
			os.Exit(2)
		}
		if _, err := conn.Write([]byte("ready\n")); err != nil {
			_ = conn.Close()
			os.Exit(2)
		}
		<-ch
		if _, err := conn.Write([]byte("term\n")); err != nil {
			_ = conn.Close()
			os.Exit(2)
		}
		if err := conn.Close(); err != nil {
			os.Exit(2)
		}
		for {
			time.Sleep(time.Hour)
		}
	case t020LifecycleEnv:
		_, present := os.LookupEnv("GORACE")
		data, err := json.Marshal(struct {
			GORACE bool `json:"gorace"`
		}{GORACE: present})
		if err != nil {
			os.Exit(2)
		}
		path := os.Getenv(t020LifecycleResult)
		if path == "" {
			os.Exit(2)
		}
		if err := os.WriteFile(path, data, 0o600); err != nil {
			os.Exit(2)
		}
		os.Exit(0)
	default:
		os.Exit(2)
	}
}
