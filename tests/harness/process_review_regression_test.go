package harness

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"syscall"
	"testing"
	"time"
)

const (
	pr3ReviewHelperMarker = "PAYMENT_HUB_PR3_REVIEW_HELPER"
	pr3ReviewHelperMode   = "PAYMENT_HUB_PR3_REVIEW_MODE"
	pr3ReviewHelperArgRun = "-test.run=^TestProcessReviewHelper$"
	pr3ReviewEnvProbeArg  = "payment-hub-pr3-env-probe"
	pr3ReviewHostCanary   = "PAYMENT_HUB_PR3_HOST_CANARY"
	pr3ReviewLoaderCanary = "LD_LIBRARY_PATH"
	pr3ReviewMemberMode   = "term-recorder"
	pr3ReviewTermMarker   = "PAYMENT_HUB_PR3_TERM_MARKER"
)

type pr3EnvProbe struct {
	EnvCount     int  `json:"env_count"`
	HostCanary   bool `json:"host_canary"`
	LoaderCanary bool `json:"loader_canary"`
	GORACE       bool `json:"gorace"`
	HelperMarker bool `json:"helper_marker"`
}

// TestProcessReviewEmptyEnvDoesNotInheritHost covers the PR3 P1 review item:
// nil and empty explicit environments must both reach the child as a non-nil,
// zero-length environment instead of silently inheriting the host environment.
func TestProcessReviewEmptyEnvDoesNotInheritHost(t *testing.T) {
	source := t020SourceFixture(t)
	for _, tc := range []struct {
		name string
		env  []string
	}{
		{name: "nil_env", env: nil},
		{name: "empty_env", env: []string{}},
	} {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			artifacts, _ := t020BeginArtifacts(t, source)
			resultPath := filepath.Join(t020TempDir(t), "env-probe.json")
			t.Setenv(pr3ReviewHostCanary, "FAKE_PR3_REVIEW_HOST_CANARY")
			t.Setenv(pr3ReviewLoaderCanary, "/tmp/opencode/pr3-review-loader-canary")
			t.Setenv("GORACE", "atexit_sleep_ms=0")
			t.Setenv(pr3ReviewHelperMarker, "1")

			lifetime, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			process, err := StartProcess(lifetime, ProcessConfig{
				Path:       t020HelperPath(t),
				Dir:        t020TempDir(t),
				Args:       []string{pr3ReviewHelperArgRun, pr3ReviewEnvProbeArg, resultPath},
				Env:        tc.env,
				Generation: 1,
				Grace:      time.Second,
				Artifacts:  artifacts,
			})
			if err != nil {
				t.Fatal("start t020 review env probe failed")
			}
			ctx, waitCancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer waitCancel()
			exit, err := process.Wait(ctx)
			if err != nil {
				t.Fatal("wait t020 review env probe failed")
			}
			if !exit.Reaped || !exit.OutputComplete || !exit.GroupGone {
				t.Fatal("t020 review env probe resources are not confirmed")
			}

			data, err := os.ReadFile(resultPath)
			if err != nil {
				t.Fatal("read t020 review env probe result failed")
			}
			var probe pr3EnvProbe
			if err := json.Unmarshal(data, &probe); err != nil {
				t.Fatal("decode t020 review env probe result failed")
			}
			if probe.EnvCount != 0 {
				t.Fatalf("t020 review env probe observed %d ambient variables", probe.EnvCount)
			}
			if probe.HostCanary || probe.LoaderCanary || probe.GORACE || probe.HelperMarker {
				t.Fatalf("t020 review env probe inherited host variables: host=%t loader=%t gorace=%t marker=%t",
					probe.HostCanary, probe.LoaderCanary, probe.GORACE, probe.HelperMarker)
			}
			if _, err := artifacts.Finish(); err != nil {
				t.Fatal("finish t020 review env probe artifacts failed")
			}
		})
	}
}

type pr3ReviewMember struct {
	cmd  *exec.Cmd
	done chan struct{}
}

type pr3ReviewGroup struct {
	leader   *Process
	identity t020GroupIdentity
	member   *pr3ReviewMember
	memberID t020GroupIdentity
	marker   string
}

func pr3ReviewStartGroup(t *testing.T, artifacts *Artifacts, lifetime context.Context) *pr3ReviewGroup {
	t.Helper()
	leaderReady := t020GroupReadySocket(t)
	leader, err := StartProcess(lifetime, ProcessConfig{
		Path:       t020HelperPath(t),
		Dir:        t020TempDir(t),
		Args:       []string{t020LifecycleArgRun},
		Env:        t020LifecycleEnvFor(t020LifecycleExit, t020HelperReadySocket+"="+leaderReady.path),
		Generation: 1,
		Grace:      2 * time.Second,
		Artifacts:  artifacts,
	})
	if err != nil {
		t.Fatal("start t020 review leader failed")
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_, _ = leader.Stop(ctx)
	})
	identity := t020AwaitGroupReady(t, leaderReady)
	if identity.Pid != identity.Pgid || identity.Pgid <= 1 || identity.Pgid == syscall.Getpgrp() {
		t.Fatal("t020 review leader process group is unsafe")
	}

	memberReady := t020GroupReadySocket(t)
	marker := filepath.Join(t020TempDir(t), "term-marker")
	member := pr3ReviewStartMember(t, identity.Pgid, memberReady.path, marker)
	memberID := t020AwaitGroupReady(t, memberReady)
	if memberID.Pgid != identity.Pgid ||
		memberID.Pid != member.cmd.Process.Pid ||
		memberID.Pid == identity.Pid || memberID.Pid <= 1 {
		t.Fatal("t020 review member process group mismatch")
	}
	return &pr3ReviewGroup{
		leader: leader, identity: identity,
		member: member, memberID: memberID, marker: marker,
	}
}

func pr3ReviewStartMember(t *testing.T, pgid int, readyPath, markerPath string) *pr3ReviewMember {
	t.Helper()
	cmd := exec.Command(t020HelperPath(t), pr3ReviewHelperArgRun)
	cmd.Dir = t020TempDir(t)
	cmd.Env = []string{
		pr3ReviewHelperMarker + "=1",
		pr3ReviewHelperMode + "=" + pr3ReviewMemberMode,
		t020HelperReadySocket + "=" + readyPath,
		pr3ReviewTermMarker + "=" + markerPath,
		"TMPDIR=/tmp/opencode",
		"TZ=UTC",
	}
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pgid: pgid}
	if err := cmd.Start(); err != nil {
		t.Fatal("start t020 review member failed")
	}
	member := &pr3ReviewMember{cmd: cmd, done: make(chan struct{})}
	go func() {
		_ = cmd.Wait()
		close(member.done)
	}()
	t.Cleanup(func() {
		select {
		case <-member.done:
			return
		default:
		}
		if member.cmd.Process != nil {
			_ = member.cmd.Process.Kill()
		}
		select {
		case <-member.done:
		case <-time.After(5 * time.Second):
			t.Errorf("t020 review member cleanup did not reap")
		}
	})
	return member
}

func (g *pr3ReviewGroup) reapLeader(t *testing.T) {
	t.Helper()
	if err := syscall.Kill(g.identity.Pid, syscall.SIGUSR1); err != nil {
		t.Fatal("signal t020 review leader failed")
	}
	deadline := time.Now().Add(2 * time.Second)
	for {
		err := syscall.Kill(g.identity.Pid, 0)
		if errors.Is(err, syscall.ESRCH) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("t020 review leader was not reaped")
		}
		time.Sleep(5 * time.Millisecond)
	}
	// The OS reaps the leader in cmd.Wait before the coordinator consumes the
	// leader event. Give that event a bounded window so Stop/Wait below test
	// the post-leader path rather than a pending event.
	time.Sleep(100 * time.Millisecond)
	if err := syscall.Kill(g.memberID.Pid, 0); err != nil {
		t.Fatal("t020 review member died before stop")
	}
}

func (g *pr3ReviewGroup) requireNoTerminationSignal(t *testing.T) {
	t.Helper()
	if err := syscall.Kill(g.memberID.Pid, 0); err != nil {
		t.Fatal("t020 review member received a termination signal")
	}
	if _, err := os.Stat(g.marker); err == nil {
		t.Fatal("t020 review member received SIGTERM or SIGINT")
	} else if !errors.Is(err, os.ErrNotExist) {
		t.Fatal("stat t020 review member marker failed")
	}
}

func pr3ReviewRequireNoGroupWatch(t *testing.T) {
	t.Helper()
	deadline := time.Now().Add(500 * time.Millisecond)
	for {
		buffer := make([]byte, 1<<20)
		n := runtime.Stack(buffer, true)
		if !bytes.Contains(buffer[:n], []byte("harness.(*Process).watchGroup")) {
			return
		}
		if time.Now().After(deadline) {
			t.Fatal("t020 review group watch outlived the terminal state")
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// TestProcessReviewLeaderReapedStopHonorsDeadline covers the PR3 P2 review
// item: after the leader is reaped, Stop must honor its context deadline for
// the remaining observation/drain window, record the cause, never signal the
// possibly reused historical PGID, and stop its group watcher.
func TestProcessReviewLeaderReapedStopHonorsDeadline(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	lifetime, cancelLifetime := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancelLifetime()
	group := pr3ReviewStartGroup(t, artifacts, lifetime)
	group.reapLeader(t)

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	start := time.Now()
	exit, err := group.leader.Stop(ctx)
	elapsed := time.Since(start)
	if !errors.Is(ctx.Err(), context.DeadlineExceeded) {
		t.Fatal("t020 review stop context did not reach its deadline")
	}
	if elapsed > time.Second {
		t.Fatalf("t020 review stop after leader exit ignored its deadline: elapsed=%v", elapsed)
	}
	if !errors.Is(err, ErrProcess) {
		t.Fatal("t020 review stop after leader exit did not return ErrProcess")
	}
	if !exit.Reaped || !exit.OutputComplete || exit.GroupGone {
		t.Fatal("t020 review stop after leader exit reported inaccurate resources")
	}
	if !exit.Canceled || !exit.TimedOut || exit.Forced {
		t.Fatal("t020 review stop after leader exit reported inaccurate causes")
	}
	group.requireNoTerminationSignal(t)
}

// TestProcessReviewLeaderReapedWaitHonorsDeadline is the Wait counterpart: the
// caller's deadline shortens the remaining post-leader observation window.
func TestProcessReviewLeaderReapedWaitHonorsDeadline(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	lifetime, cancelLifetime := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancelLifetime()
	group := pr3ReviewStartGroup(t, artifacts, lifetime)
	group.reapLeader(t)

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	start := time.Now()
	exit, err := group.leader.Wait(ctx)
	elapsed := time.Since(start)
	if !errors.Is(ctx.Err(), context.DeadlineExceeded) {
		t.Fatal("t020 review wait context did not reach its deadline")
	}
	if elapsed > time.Second {
		t.Fatalf("t020 review wait after leader exit ignored its deadline: elapsed=%v", elapsed)
	}
	if !errors.Is(err, ErrProcess) {
		t.Fatal("t020 review wait after leader exit did not return ErrProcess")
	}
	if !exit.Reaped || !exit.OutputComplete || exit.GroupGone {
		t.Fatal("t020 review wait after leader exit reported inaccurate resources")
	}
	if !exit.TimedOut || exit.Canceled || exit.Forced {
		t.Fatal("t020 review wait after leader exit reported inaccurate causes")
	}
	group.requireNoTerminationSignal(t)
}

// TestProcessReviewLeaderReapedLifetimeCancel covers the lifetime cancellation
// half of the same P2 item after the leader has already been reaped.
func TestProcessReviewLeaderReapedLifetimeCancel(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	lifetime, cancelLifetime := context.WithCancel(context.Background())
	defer cancelLifetime()
	group := pr3ReviewStartGroup(t, artifacts, lifetime)
	group.reapLeader(t)

	start := time.Now()
	cancelLifetime()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	exit, err := group.leader.Wait(ctx)
	elapsed := time.Since(start)
	if elapsed > time.Second {
		t.Fatalf("t020 review lifetime cancellation after leader exit was ignored: elapsed=%v", elapsed)
	}
	if !errors.Is(err, ErrProcess) {
		t.Fatal("t020 review lifetime cancellation after leader exit did not return ErrProcess")
	}
	if !exit.Reaped || exit.GroupGone || exit.TimedOut || exit.Forced {
		t.Fatal("t020 review lifetime cancellation after leader exit reported inaccurate resources")
	}
	if !exit.Canceled {
		t.Fatal("t020 review lifetime cancellation after leader exit was not recorded")
	}
	group.requireNoTerminationSignal(t)
}

// TestProcessReviewTerminalPublishedAfterGroupWatchJoined deterministically
// checks the terminal publication order: the group watcher must be joined
// before Wait/Stop can observe the terminal state. The test seam blocks the
// coordinator at the post-join point so the assertion does not depend on
// scheduler timing.
func TestProcessReviewTerminalPublishedAfterGroupWatchJoined(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	joined := make(chan struct{})
	release := make(chan struct{})
	releaseNow := func() {
		select {
		case <-release:
		default:
			close(release)
		}
	}
	defer releaseNow()

	lifetime, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	process, err := StartProcess(lifetime, ProcessConfig{
		Path:       t020HelperPath(t),
		Dir:        t020TempDir(t),
		Args:       []string{t020HelperArgRun},
		Env:        t020HelperEnv(t020HelperModeExit0),
		Generation: 1,
		Grace:      time.Second,
		Artifacts:  artifacts,
		testWatchGroupJoined: func() {
			close(joined)
			<-release
		},
	})
	if err != nil {
		t.Fatal("start t020 review terminal order helper failed")
	}
	select {
	case <-joined:
	case <-time.After(5 * time.Second):
		t.Fatal("t020 review terminalize did not reach the group watch join point")
	}
	select {
	case <-process.terminal:
		t.Fatal("t020 review terminal state was published before the group watch join")
	default:
	}
	releaseNow()
	ctx, waitCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer waitCancel()
	exit, err := process.Wait(ctx)
	if err != nil {
		t.Fatal("wait t020 review terminal order helper failed")
	}
	if !exit.Reaped || !exit.OutputComplete || !exit.GroupGone {
		t.Fatal("t020 review terminal order helper did not finish cleanly")
	}
}

// TestProcessReviewWatchGroupStopUnblocksFullEventChannel covers the join
// deadlock risk: a watcher blocked trying to publish a group event must still
// exit when terminalization closes its stop channel, before the terminal state
// is published.
func TestProcessReviewWatchGroupStopUnblocksFullEventChannel(t *testing.T) {
	process := &Process{
		events:   make(chan processEvent, 1),
		terminal: make(chan struct{}),
		pgid:     0,
	}
	process.events <- processEvent{kind: processEventGroupGone}
	defer close(process.terminal)
	stop := make(chan struct{})
	close(stop)
	done := make(chan struct{})
	go process.watchGroup(stop, done)
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("t020 review group watch did not stop with a full event channel")
	}
}

// TestProcessReviewHelper is the controlled child used by the regressions
// above. The environment probe is selected through argv so it also works when
// the child receives a genuinely empty environment.
func TestProcessReviewHelper(t *testing.T) {
	for i := 0; i+1 < len(os.Args); i++ {
		if os.Args[i] == pr3ReviewEnvProbeArg {
			pr3ReviewWriteEnvProbe(os.Args[i+1])
			os.Exit(0)
		}
	}
	if os.Getenv(pr3ReviewHelperMarker) != "1" {
		return
	}
	switch os.Getenv(pr3ReviewHelperMode) {
	case pr3ReviewMemberMode:
		pr3ReviewRunMember()
	default:
		os.Exit(2)
	}
}

func pr3ReviewWriteEnvProbe(path string) {
	count := 0
	for range os.Environ() {
		count++
	}
	_, host := os.LookupEnv(pr3ReviewHostCanary)
	_, loader := os.LookupEnv(pr3ReviewLoaderCanary)
	_, gorace := os.LookupEnv("GORACE")
	_, marker := os.LookupEnv(pr3ReviewHelperMarker)
	data, err := json.Marshal(pr3EnvProbe{
		EnvCount:     count,
		HostCanary:   host,
		LoaderCanary: loader,
		GORACE:       gorace,
		HelperMarker: marker,
	})
	if err != nil {
		os.Exit(2)
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		os.Exit(2)
	}
}

func pr3ReviewRunMember() {
	signals := make(chan os.Signal, 8)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGINT)
	t020HelperGroupReady()
	for range signals {
		path := os.Getenv(pr3ReviewTermMarker)
		if path != "" {
			_ = os.WriteFile(path, []byte("signal\n"), 0o600)
		}
	}
}
