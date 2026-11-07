package harness

import (
	"bytes"
	"context"
	"errors"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
)

var ErrProcess = errors.New("harness: process failure")

const (
	defaultProcessLifetime = 30 * time.Second
	defaultProcessGrace    = 2 * time.Second
	processReapBudget      = 3 * time.Second
	processEventBuffer     = 64
)

type ProcessConfig struct {
	Path       string
	Dir        string
	Args       []string
	Env        []string
	Generation uint64
	Grace      time.Duration
	Artifacts  *Artifacts
}

type Exit struct {
	Generation     uint64
	Code           int
	Signal         int
	Canceled       bool
	TimedOut       bool
	Forced         bool
	Reaped         bool
	OutputComplete bool
	GroupGone      bool
}

type processEventKind int

const (
	processEventStop processEventKind = iota
	processEventLeader
	processEventOutputEOF
	processEventGroupGone
)

type processEvent struct {
	kind     processEventKind
	canceled bool
	timedOut bool
	forceNow bool
	code     int
	signal   int
	stream   string
	gone     bool
	reaped   bool
}

// Process owns one child process and the process group created for it. A
// single coordinator goroutine owns every lifecycle transition; Wait and Stop
// only post requests and observe the same terminal result.
type Process struct {
	cfg       ProcessConfig
	artifacts *Artifacts
	grace     time.Duration
	lifetime  context.Context
	cancel    context.CancelFunc

	events   chan processEvent
	terminal chan struct{}

	cmd        *exec.Cmd
	pgid       int
	stdoutRead *os.File
	stderrRead *os.File
	collector  *outputCollector

	mu     sync.Mutex
	exit   Exit
	err    error
	waited bool
}

func StartProcess(lifetime context.Context, cfg ProcessConfig) (*Process, error) {
	if lifetime == nil || lifetime.Err() != nil || cfg.Artifacts == nil {
		return nil, ErrProcess
	}
	if cfg.Generation == 0 || cfg.Grace < 0 {
		return nil, ErrProcess
	}
	if !validProcessExecutable(cfg.Path) || !validProcessDir(cfg.Dir) ||
		!validProcessArgs(cfg.Args) || !validProcessEnv(cfg.Env) {
		return nil, ErrProcess
	}
	if err := cfg.Artifacts.reserveGeneration(cfg.Generation); err != nil {
		return nil, ErrProcess
	}

	grace := cfg.Grace
	if grace == 0 {
		grace = defaultProcessGrace
	}
	runContext := lifetime
	cancel := func() {}
	if _, ok := lifetime.Deadline(); !ok {
		runContext, cancel = context.WithTimeout(lifetime, defaultProcessLifetime)
	}

	stdoutRead, stdoutWrite, err := os.Pipe()
	if err != nil {
		cfg.Artifacts.failGeneration(cfg.Generation)
		cancel()
		return nil, ErrProcess
	}
	stderrRead, stderrWrite, err := os.Pipe()
	if err != nil {
		_ = stdoutRead.Close()
		_ = stdoutWrite.Close()
		cfg.Artifacts.failGeneration(cfg.Generation)
		cancel()
		return nil, ErrProcess
	}

	process := &Process{
		cfg:        cfg,
		artifacts:  cfg.Artifacts,
		grace:      grace,
		lifetime:   runContext,
		cancel:     cancel,
		events:     make(chan processEvent, processEventBuffer),
		terminal:   make(chan struct{}),
		stdoutRead: stdoutRead,
		stderrRead: stderrRead,
		exit:       Exit{Generation: cfg.Generation},
	}
	process.collector = newOutputCollector(cfg.Artifacts, cfg.Generation, process)

	cmd := exec.Command(cfg.Path, cfg.Args...)
	cmd.Dir = cfg.Dir
	cmd.Env = append([]string(nil), cfg.Env...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true, Pgid: 0}
	cmd.Stdout = stdoutWrite
	cmd.Stderr = stderrWrite
	process.cmd = cmd

	if err := cmd.Start(); err != nil {
		_ = stdoutRead.Close()
		_ = stdoutWrite.Close()
		_ = stderrRead.Close()
		_ = stderrWrite.Close()
		cfg.Artifacts.failGeneration(cfg.Generation)
		cancel()
		return nil, ErrProcess
	}
	_ = stdoutWrite.Close()
	_ = stderrWrite.Close()
	process.pgid = cmd.Process.Pid
	if err := cfg.Artifacts.registerProcess(process); err != nil {
		// The report is already sealed; the coordinator still owns bounded
		// cleanup of the started process.
		process.events <- processEvent{kind: processEventStop, forceNow: true}
	}

	go process.coordinator()
	go process.waitLeader()
	go process.drain(stdoutRead, "stdout")
	go process.drain(stderrRead, "stderr")
	return process, nil
}

func validProcessExecutable(path string) bool {
	if path == "" || !filepath.IsAbs(path) {
		return false
	}
	info, err := os.Lstat(path)
	if err != nil || info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
		return false
	}
	return info.Mode().Perm()&0o111 != 0
}

func validProcessDir(path string) bool {
	if path == "" || !filepath.IsAbs(path) {
		return false
	}
	info, err := os.Lstat(path)
	return err == nil && info.Mode()&os.ModeSymlink == 0 && info.IsDir()
}

func validProcessArgs(args []string) bool {
	for _, arg := range args {
		if strings.IndexByte(arg, 0) >= 0 {
			return false
		}
	}
	return true
}

func validProcessEnv(env []string) bool {
	seen := make(map[string]bool, len(env))
	for _, entry := range env {
		key, value, ok := strings.Cut(entry, "=")
		if !ok || !validProcessEnvKey(key) || seen[key] || strings.IndexByte(value, 0) >= 0 {
			return false
		}
		if strings.HasPrefix(key, "LD_") || strings.HasPrefix(key, "DYLD_") {
			return false
		}
		if key != "TMPDIR" && key != "TZ" && key != "GOMAXPROCS" && key != "GORACE" &&
			!strings.HasPrefix(key, "PAYMENT_HUB_") {
			return false
		}
		seen[key] = true
	}
	return true
}

func validProcessEnvKey(key string) bool {
	if key == "" {
		return false
	}
	for i := 0; i < len(key); i++ {
		c := key[i]
		if (c < 'A' || c > 'Z') && (c < 'a' || c > 'z') &&
			(c < '0' || c > '9') && c != '_' {
			return false
		}
	}
	return true
}

func (p *Process) sendEvent(event processEvent) {
	select {
	case p.events <- event:
	case <-p.terminal:
	}
}

func (p *Process) waitLeader() {
	waitErr := p.cmd.Wait()
	code, signal, reaped := processStateExit(p.cmd.ProcessState)
	_ = waitErr
	p.sendEvent(processEvent{kind: processEventLeader, code: code, signal: signal, reaped: reaped})
}

func processStateExit(state *os.ProcessState) (int, int, bool) {
	if state == nil {
		return 0, 0, false
	}
	status, ok := state.Sys().(syscall.WaitStatus)
	if !ok {
		return state.ExitCode(), 0, true
	}
	if status.Signaled() {
		return -1, int(status.Signal()), true
	}
	return status.ExitStatus(), 0, true
}

func (p *Process) drain(file *os.File, stream string) {
	defer file.Close()
	buffer := make([]byte, 32*1024)
	for {
		n, err := file.Read(buffer)
		if n > 0 {
			p.collector.feed(stream, buffer[:n])
		}
		if err != nil {
			if errors.Is(err, io.EOF) {
				p.collector.streamEnd(stream)
			} else {
				p.collector.readFailure(stream)
			}
			p.sendEvent(processEvent{kind: processEventOutputEOF, stream: stream})
			return
		}
	}
}

func (p *Process) coordinator() {
	graceDeadline := time.Time{}
	forceDeadline := time.Time{}
	stopRequested := false
	leaderReaped := false
	leaderCode := 0
	leaderSignal := 0
	signalAllowed := true
	forced := false
	canceled := false
	timedOut := false
	outputFault := false
	groupGone := false
	groupWatchStarted := false
	stdoutEnded := false
	stderrEnded := false
	lifetimeC := p.lifetime.Done()
	faultC := p.collector.faultSignal

	var timer *time.Timer
	var timerC <-chan time.Time
	resetTimer := func() {
		next := time.Time{}
		pick := func(value time.Time) {
			if !value.IsZero() && (next.IsZero() || value.Before(next)) {
				next = value
			}
		}
		if stopRequested && !leaderReaped && !forced {
			pick(graceDeadline)
		}
		pick(forceDeadline)
		if timer != nil {
			timer.Stop()
		}
		if next.IsZero() {
			timerC = nil
			return
		}
		delay := time.Until(next)
		if delay < 0 {
			delay = 0
		}
		timer = time.NewTimer(delay)
		timerC = timer.C
	}
	enterForce := func() {
		if forced || leaderReaped || !signalAllowed {
			return
		}
		forced = true
		if safeProcessGroup(p.pgid) {
			_ = syscall.Kill(-p.pgid, syscall.SIGKILL)
		}
		if forceDeadline.IsZero() {
			forceDeadline = time.Now().Add(processReapBudget)
		}
	}
	startGroupWatch := func() {
		if groupWatchStarted || !safeProcessGroup(p.pgid) {
			return
		}
		groupWatchStarted = true
		if forceDeadline.IsZero() {
			forceDeadline = time.Now().Add(processReapBudget)
		}
		go p.watchGroup(forceDeadline)
	}
	terminal := func() bool {
		return leaderReaped && stdoutEnded && stderrEnded && groupGone
	}
	terminalize := func() {
		outputComplete := stdoutEnded && stderrEnded && !outputFault && !p.collector.isFaulted()
		exit := Exit{
			Generation:     p.cfg.Generation,
			Code:           leaderCode,
			Signal:         leaderSignal,
			Canceled:       canceled,
			TimedOut:       timedOut,
			Forced:         forced,
			Reaped:         leaderReaped,
			OutputComplete: outputComplete,
			GroupGone:      groupGone,
		}
		var err error
		if outputFault || p.collector.isFaulted() {
			err = ErrArtifacts
		} else if canceled || timedOut || forced || leaderCode != 0 ||
			!leaderReaped || !outputComplete || !groupGone {
			err = ErrProcess
		}
		_ = p.stdoutRead.Close()
		_ = p.stderrRead.Close()
		p.mu.Lock()
		p.exit = exit
		p.err = err
		p.mu.Unlock()
		close(p.terminal)
		p.cancel()
	}

	for {
		resetTimer()
		select {
		case event := <-p.events:
			switch event.kind {
			case processEventStop:
				if leaderReaped && !stopRequested {
					break
				}
				if event.canceled {
					canceled = true
				}
				if event.timedOut {
					timedOut = true
				}
				if !stopRequested {
					stopRequested = true
					if signalAllowed && safeProcessGroup(p.pgid) {
						_ = syscall.Kill(-p.pgid, syscall.SIGTERM)
					}
					graceDeadline = time.Now().Add(p.grace)
				}
				if event.forceNow && (graceDeadline.IsZero() || time.Now().Before(graceDeadline)) {
					graceDeadline = time.Now()
				}
				if !graceDeadline.IsZero() && !time.Now().Before(graceDeadline) {
					enterForce()
				}
			case processEventLeader:
				if !leaderReaped {
					leaderReaped = event.reaped
					leaderCode = event.code
					leaderSignal = event.signal
					signalAllowed = false
					if forceDeadline.IsZero() {
						forceDeadline = time.Now().Add(processReapBudget)
					}
					startGroupWatch()
				}
			case processEventOutputEOF:
				if event.stream == "stdout" {
					stdoutEnded = true
				} else if event.stream == "stderr" {
					stderrEnded = true
				}
			case processEventGroupGone:
				groupGone = event.gone
			}
		case <-faultC:
			faultC = nil
			outputFault = true
			if !stopRequested && !leaderReaped {
				stopRequested = true
				if signalAllowed && safeProcessGroup(p.pgid) {
					_ = syscall.Kill(-p.pgid, syscall.SIGTERM)
				}
				graceDeadline = time.Now().Add(p.grace)
			}
		case <-lifetimeC:
			lifetimeC = nil
			if !leaderReaped {
				if errors.Is(p.lifetime.Err(), context.DeadlineExceeded) {
					timedOut = true
				} else if errors.Is(p.lifetime.Err(), context.Canceled) {
					canceled = true
				}
				if !stopRequested {
					stopRequested = true
					if signalAllowed && safeProcessGroup(p.pgid) {
						_ = syscall.Kill(-p.pgid, syscall.SIGTERM)
					}
					graceDeadline = time.Now().Add(p.grace)
				} else {
					graceDeadline = time.Now()
					enterForce()
				}
			}
		case <-timerC:
			if stopRequested && !forced && !leaderReaped && !graceDeadline.IsZero() &&
				!time.Now().Before(graceDeadline) {
				enterForce()
			}
			if !forceDeadline.IsZero() && !time.Now().Before(forceDeadline) {
				terminalize()
				return
			}
		}
		if terminal() {
			terminalize()
			return
		}
	}
}

func (p *Process) watchGroup(deadline time.Time) {
	pgid := p.pgid
	if !safeProcessGroup(pgid) {
		p.sendEvent(processEvent{kind: processEventGroupGone})
		return
	}
	delay := 5 * time.Millisecond
	for {
		err := syscall.Kill(-pgid, 0)
		if errors.Is(err, syscall.ESRCH) {
			p.sendEvent(processEvent{kind: processEventGroupGone, gone: true})
			return
		}
		if !time.Now().Before(deadline) {
			p.sendEvent(processEvent{kind: processEventGroupGone})
			return
		}
		time.Sleep(delay)
		if delay < 50*time.Millisecond {
			delay *= 2
		}
	}
}

func safeProcessGroup(pgid int) bool {
	return pgid > 1 && pgid != syscall.Getpgrp()
}

func (p *Process) Wait(ctx context.Context) (Exit, error) {
	if p == nil || p.terminal == nil || ctx == nil {
		return Exit{}, ErrProcess
	}
	select {
	case <-p.terminal:
		return p.observed()
	default:
	}
	stopAfter := context.AfterFunc(ctx, func() {
		timedOut := errors.Is(ctx.Err(), context.DeadlineExceeded)
		canceled := !timedOut && errors.Is(ctx.Err(), context.Canceled)
		p.sendEvent(processEvent{kind: processEventStop, canceled: canceled, timedOut: timedOut, forceNow: true})
	})
	defer stopAfter()
	<-p.terminal
	return p.observed()
}

func (p *Process) Stop(ctx context.Context) (Exit, error) {
	if p == nil || p.terminal == nil || ctx == nil {
		return Exit{}, ErrProcess
	}
	select {
	case <-p.terminal:
		return p.observed()
	default:
	}
	p.sendEvent(processEvent{kind: processEventStop, canceled: true})
	stopAfter := context.AfterFunc(ctx, func() {
		timedOut := errors.Is(ctx.Err(), context.DeadlineExceeded)
		canceled := !timedOut && errors.Is(ctx.Err(), context.Canceled)
		p.sendEvent(processEvent{kind: processEventStop, canceled: canceled, timedOut: timedOut, forceNow: true})
	})
	defer stopAfter()
	<-p.terminal
	return p.observed()
}

func (p *Process) observed() (Exit, error) {
	p.mu.Lock()
	p.waited = true
	exit := p.exit
	err := p.err
	p.mu.Unlock()
	return exit, err
}

// snapshotForReport returns the last observed exit and whether an external
// Wait/Stop observed it. Finish uses the latter to reject un-waited processes.
func (p *Process) snapshotForReport() (Exit, bool) {
	if p == nil {
		return Exit{}, false
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.exit, p.waited
}

type outputCollector struct {
	artifacts  *Artifacts
	generation uint64
	process    *Process

	mu            sync.Mutex
	total         int
	faulted       bool
	faultCategory string
	buffers       map[string][]byte
	faultSignal   chan struct{}
	faultOnce     sync.Once
}

func newOutputCollector(artifacts *Artifacts, generation uint64, process *Process) *outputCollector {
	return &outputCollector{
		artifacts:   artifacts,
		generation:  generation,
		process:     process,
		buffers:     map[string][]byte{"stdout": nil, "stderr": nil},
		faultSignal: make(chan struct{}),
	}
}

func (c *outputCollector) feed(stream string, data []byte) {
	if len(data) == 0 {
		return
	}
	c.mu.Lock()
	if c.faulted {
		c.mu.Unlock()
		return
	}
	if c.total+len(data) > MaxProcessOutputBytes {
		c.faulted = true
		c.faultCategory = "output_limit"
		c.mu.Unlock()
		c.artifacts.latchFault("output_limit")
		c.notify()
		return
	}
	c.total += len(data)
	buffer := append(c.buffers[stream], data...)
	for {
		index := bytes.IndexByte(buffer, '\n')
		if index < 0 {
			break
		}
		line := buffer[:index+1]
		buffer = buffer[index+1:]
		if len(line) > MaxLogLineBytes {
			c.faulted = true
			c.faultCategory = "output_limit"
			buffer = nil
			break
		}
		log, err := parseLogLine(line)
		if err != nil {
			c.faulted = true
			c.faultCategory = "output_protocol"
			buffer = nil
			break
		}
		log.Generation = c.generation
		log.Stream = stream
		if err := c.artifacts.addLog(log); err != nil {
			c.faulted = true
			c.faultCategory = "output_limit"
			buffer = nil
			break
		}
	}
	if !c.faulted && len(buffer) >= MaxLogLineBytes {
		c.faulted = true
		c.faultCategory = "output_limit"
		buffer = nil
	}
	c.buffers[stream] = buffer
	faulted := c.faulted
	category := c.faultCategory
	c.mu.Unlock()
	if faulted {
		c.artifacts.latchFault(category)
		c.notify()
	}
}

func (c *outputCollector) readFailure(stream string) {
	c.mu.Lock()
	if !c.faulted {
		c.faulted = true
		c.faultCategory = "process_incomplete"
	}
	c.buffers[stream] = nil
	c.mu.Unlock()
	c.artifacts.latchFault("process_incomplete")
	c.notify()
}

func (c *outputCollector) streamEnd(stream string) {
	c.mu.Lock()
	if len(c.buffers[stream]) != 0 {
		if !c.faulted {
			c.faulted = true
			c.faultCategory = "output_protocol"
		}
		c.buffers[stream] = nil
	}
	faulted := c.faulted
	category := c.faultCategory
	c.mu.Unlock()
	if faulted {
		c.artifacts.latchFault(category)
		c.notify()
	}
}

func (c *outputCollector) notify() {
	c.faultOnce.Do(func() {
		close(c.faultSignal)
	})
}

func (c *outputCollector) isFaulted() bool {
	if c == nil {
		return false
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.faulted
}
