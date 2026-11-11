package harness

import (
	"context"
	"errors"
	"net"
	"strconv"
	"strings"
	"syscall"
	"time"

	"payment-hub/internal/testcontrol"
)

var errControlledPeer = errors.New("harness: controlled termination peer ownership mismatch")

// ProcessTerminalProof is an unconstructable proof produced only by a real
// harness Process Wait observation. Its fields are private on purpose: callers
// cannot fill booleans or substitute another process.
type ProcessTerminalProof struct {
	process        *Process
	pid            int
	generation     uint64
	runID          string
	epoch          string
	exit           Exit
	terminalClosed bool
}

// ControlledTerminationProof waits for the process terminal state and returns
// a proof bound to this exact Process object.
func (p *Process) ControlledTerminationProof(ctx context.Context) (ProcessTerminalProof, error) {
	if p == nil || p.terminal == nil || ctx == nil {
		return ProcessTerminalProof{}, ErrProcess
	}
	exit, _ := p.Wait(ctx)
	if !exit.Reaped || !exit.GroupGone || !exit.OutputComplete {
		return ProcessTerminalProof{}, ErrProcess
	}
	select {
	case <-p.terminal:
	default:
		return ProcessTerminalProof{}, ErrProcess
	}
	if p.cmd == nil || p.cmd.Process == nil || p.cfg.Artifacts == nil {
		return ProcessTerminalProof{}, ErrProcess
	}
	runID, epoch, generation, ok := processSessionIdentity(p)
	if !ok {
		return ProcessTerminalProof{}, ErrProcess
	}
	if p.cfg.Artifacts.cfg.RunID != runID {
		return ProcessTerminalProof{}, ErrProcess
	}
	return ProcessTerminalProof{
		process:        p,
		pid:            p.cmd.Process.Pid,
		generation:     generation,
		runID:          runID,
		epoch:          epoch,
		exit:           exit,
		terminalClosed: true,
	}, nil
}

func processSessionIdentity(p *Process) (string, string, uint64, bool) {
	if p == nil {
		return "", "", 0, false
	}
	var runID, epoch, generationText string
	var haveRun, haveEpoch, haveGeneration bool
	for _, entry := range p.cfg.Env {
		key, value, ok := strings.Cut(entry, "=")
		if !ok {
			continue
		}
		switch key {
		case "PAYMENT_HUB_TEST_RUN_ID":
			runID, haveRun = value, true
		case "PAYMENT_HUB_TEST_OBSERVER_EPOCH":
			epoch, haveEpoch = value, true
		case "PAYMENT_HUB_TEST_GENERATION":
			generationText, haveGeneration = value, true
		}
	}
	if !haveRun || !haveEpoch || !haveGeneration ||
		!observerValidLowerHex32(runID) || !observerValidLowerHex32(epoch) {
		return "", "", 0, false
	}
	generation, err := strconv.ParseUint(generationText, 10, 64)
	if err != nil || generation == 0 || strconv.FormatUint(generation, 10) != generationText {
		return "", "", 0, false
	}
	return runID, epoch, generation, true
}

// ControlledTerminationBinding is the frozen held-point identity captured
// before the controlled process termination.
type ControlledTerminationBinding struct {
	RunID       string
	Epoch       string
	Generation  uint64
	Point       testcontrol.Point
	OrderID     string
	Seq         uint64
	EventID     string
	PrefixCount int
}

// ControlledTerminationResult is the parent proof for the expected truncated
// generation. It never claims a receipt or exact counts.
type ControlledTerminationResult struct {
	Binding             ControlledTerminationBinding
	Exit                Exit
	ControlledTruncated bool
	NoReceipt           bool
	ExactCounts         bool
	ObservedPrefix      Counts
}

// ControlledTermination is the two-stage parent helper: Prepare binds the
// exact held connection and process intent before the kill; Terminate accepts
// only that same process's real terminal proof.
type ControlledTermination struct {
	observer *Observer
	barrier  *Barrier
	process  *Process
	binding  ControlledTerminationBinding
	conn     net.Conn
	peerPID  int
	pid      int
	runID    string
	epoch    string
	done     bool
}

// PrepareControlledTermination freezes the exact held point, connection, and
// process identity before the process is terminated.
func (o *Observer) PrepareControlledTermination(barrier *Barrier, process *Process) (*ControlledTermination, error) {
	if o == nil || barrier == nil || process == nil ||
		process.cmd == nil || process.cmd.Process == nil || process.cfg.Artifacts == nil {
		return nil, testcontrol.ErrControlFault
	}
	runID, epoch, generation, ok := processSessionIdentity(process)
	if !ok || process.cfg.Artifacts.cfg.RunID != runID {
		return nil, testcontrol.ErrControlFault
	}

	o.mu.Lock()
	defer o.mu.Unlock()
	if o.closing {
		return nil, testcontrol.ErrControlFault
	}
	if o.uncertain {
		return nil, testcontrol.ErrUncertain
	}
	if barrier.observer != o || !barrier.arrived || barrier.released || barrier.canceled ||
		barrier.event == nil || barrier.conn == nil {
		return nil, testcontrol.ErrControlFault
	}
	if barrier.gen != generation || runID != o.cfg.RunID || epoch != o.cfg.Epoch {
		return nil, testcontrol.ErrControlFault
	}
	if !observerValidControlledPoint(barrier.point) {
		return nil, testcontrol.ErrControlFault
	}
	event := barrier.event
	if event.typ != testcontrol.TypeReach || event.point != barrier.point ||
		event.orderID != barrier.orderID || event.seq != barrier.seq ||
		event.eventID != barrier.reach.EventID ||
		event.paymentCallSeq != nil || event.queryCallSeq != nil {
		return nil, testcontrol.ErrControlFault
	}
	state, ok := o.generations[barrier.gen]
	if !ok || state.status != observerGenerationOpen {
		return nil, testcontrol.ErrControlFault
	}
	for seq := uint64(1); seq <= barrier.seq; seq++ {
		if _, exists := state.events[seq]; !exists {
			return nil, testcontrol.ErrControlFault
		}
	}
	select {
	case <-process.terminal:
		return nil, testcontrol.ErrControlFault
	default:
	}
	heldConn := barrier.conn
	peerPID, err := unixPeerPID(heldConn)
	if err != nil || peerPID != process.cmd.Process.Pid {
		return nil, testcontrol.ErrControlFault
	}
	if barrier.conn != heldConn {
		return nil, testcontrol.ErrControlFault
	}
	barrier.frozen = true
	return &ControlledTermination{
		observer: o,
		barrier:  barrier,
		process:  process,
		binding: ControlledTerminationBinding{
			RunID:       o.cfg.RunID,
			Epoch:       o.cfg.Epoch,
			Generation:  barrier.gen,
			Point:       barrier.point,
			OrderID:     barrier.orderID,
			Seq:         barrier.seq,
			EventID:     event.eventID,
			PrefixCount: int(barrier.seq),
		},
		conn:    heldConn,
		peerPID: peerPID,
		pid:     process.cmd.Process.Pid,
		runID:   runID,
		epoch:   epoch,
	}, nil
}

func unixPeerPID(conn net.Conn) (int, error) {
	unixConn, ok := conn.(*net.UnixConn)
	if !ok {
		return 0, errControlledPeer
	}
	raw, err := unixConn.SyscallConn()
	if err != nil {
		return 0, errControlledPeer
	}
	var credential *syscall.Ucred
	var credentialErr error
	if err := raw.Control(func(fd uintptr) {
		credential, credentialErr = syscall.GetsockoptUcred(int(fd), syscall.SOL_SOCKET, syscall.SO_PEERCRED)
	}); err != nil {
		return 0, errControlledPeer
	}
	if credentialErr != nil || credential == nil {
		return 0, errControlledPeer
	}
	return int(credential.Pid), nil
}

func observerValidControlledPoint(point testcontrol.Point) bool {
	return point == testcontrol.PointRegistered || point == testcontrol.PointMockCommitted
}

// Terminate consumes the real terminal proof and ends only the bound held
// connection as an expected controlled truncation.
func (c *ControlledTermination) Terminate(proof ProcessTerminalProof) (ControlledTerminationResult, error) {
	if c == nil || c.observer == nil || c.barrier == nil || c.process == nil || c.done {
		return ControlledTerminationResult{}, testcontrol.ErrControlFault
	}
	if proof.process != c.process || proof.pid != c.pid ||
		proof.generation != c.binding.Generation ||
		proof.runID != c.runID || proof.epoch != c.epoch ||
		!proof.terminalClosed || !proof.exit.Reaped || !proof.exit.GroupGone ||
		!proof.exit.OutputComplete {
		return ControlledTerminationResult{}, testcontrol.ErrControlFault
	}

	if c.conn == nil {
		return ControlledTerminationResult{}, testcontrol.ErrControlFault
	}
	peerPID, err := unixPeerPID(c.conn)
	if err != nil || peerPID != c.peerPID || peerPID != c.pid {
		return ControlledTerminationResult{}, testcontrol.ErrControlFault
	}

	o := c.observer
	barrier := c.barrier
	o.mu.Lock()
	if o.closing {
		o.mu.Unlock()
		return ControlledTerminationResult{}, testcontrol.ErrControlFault
	}
	if o.uncertain {
		o.mu.Unlock()
		return ControlledTerminationResult{}, testcontrol.ErrUncertain
	}
	if !barrier.arrived || barrier.released || barrier.canceled ||
		barrier.event == nil || barrier.conn == nil || barrier.conn != c.conn {
		o.mu.Unlock()
		return ControlledTerminationResult{}, testcontrol.ErrControlFault
	}
	state, ok := o.generations[barrier.gen]
	if !ok || state.status != observerGenerationOpen {
		o.mu.Unlock()
		return ControlledTerminationResult{}, testcontrol.ErrControlFault
	}
	if c.binding.RunID != o.cfg.RunID || c.binding.Epoch != o.cfg.Epoch ||
		c.binding.Generation != barrier.gen || c.binding.Point != barrier.point ||
		c.binding.OrderID != barrier.orderID || c.binding.Seq != barrier.seq ||
		c.binding.EventID != barrier.event.eventID {
		o.mu.Unlock()
		return ControlledTerminationResult{}, testcontrol.ErrControlFault
	}
	for seq := uint64(1); seq <= barrier.seq; seq++ {
		if _, exists := state.events[seq]; !exists {
			o.mu.Unlock()
			return ControlledTerminationResult{}, testcontrol.ErrControlFault
		}
	}

	conn := c.conn
	done := o.connDone[conn]
	o.expectedTerm[conn] = true
	barrier.canceled = true
	close(barrier.cancelCh)
	binding := c.binding
	state.status = observerGenerationTruncated
	state.truncated = &binding
	prefix := Counts{}
	for seq := uint64(1); seq <= barrier.seq; seq++ {
		event := state.events[seq]
		if event == nil || event.typ != testcontrol.TypeCall {
			continue
		}
		switch event.kind {
		case testcontrol.KindPayment:
			prefix.Payment++
		case testcontrol.KindQuery:
			prefix.Query++
		}
	}
	c.done = true
	o.mu.Unlock()

	_ = conn.Close()
	if done != nil {
		timer := time.NewTimer(2 * time.Second)
		defer timer.Stop()
		select {
		case <-done:
		case <-timer.C:
			return ControlledTerminationResult{}, testcontrol.ErrControlFault
		}
	}
	return ControlledTerminationResult{
		Binding:             binding,
		Exit:                proof.exit,
		ControlledTruncated: true,
		NoReceipt:           true,
		ExactCounts:         false,
		ObservedPrefix:      prefix,
	}, nil
}
