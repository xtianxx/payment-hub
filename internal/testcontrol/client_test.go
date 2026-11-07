package testcontrol_test

// These tests exercise the frozen T019 Client surface through the real harness
// Observer and a small number of scripted private UDS peers. The scripted peers
// prove transport mechanics only; they never stand in for observer or SQL
// facts. Failure messages contain only static case names and safe categories.

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"payment-hub/internal/testcontrol"
	"payment-hub/tests/harness"
)

const (
	clientTestRunID = "0123456789abcdef0123456789abcdef"
	clientTestEpoch = "fedcba9876543210fedcba9876543210"

	clientTestOrderA = "123e4567-e89b-42d3-a456-426614174000"
	clientTestOrderB = "223e4567-e89b-42d3-b456-426614174001"

	clientTestCanary = "FAKE_T019_CLIENT_CANARY"

	clientTestBudget  = 5 * time.Second
	clientTestShort   = 400 * time.Millisecond
	clientTestAbsence = 200 * time.Millisecond
)

type clientTestSealResult struct {
	receipt testcontrol.Receipt
	err     error
}

func clientTestNewDirMode(t *testing.T, mode os.FileMode) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "phtc-")
	if err != nil {
		t.Fatal("create client test directory")
	}
	if err := os.Chmod(dir, mode); err != nil {
		t.Fatal("set client test directory mode")
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(dir); err != nil {
			t.Errorf("remove client test directory")
		}
	})
	return dir
}

func clientTestNewDir(t *testing.T) string {
	t.Helper()
	return clientTestNewDirMode(t, 0o700)
}

func clientTestStartObserver(t *testing.T, generations ...uint64) (*harness.Observer, string) {
	t.Helper()
	dir := clientTestNewDir(t)
	socket := filepath.Join(dir, "s")
	ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancel()
	obs, err := harness.StartObserver(ctx, harness.ObserverConfig{
		Socket:         socket,
		RunID:          clientTestRunID,
		Epoch:          clientTestEpoch,
		BarrierTimeout: clientTestBudget,
	})
	if err != nil {
		t.Fatal("start real observer for client test")
	}
	if obs == nil {
		t.Fatal("start real observer returned nil")
	}
	t.Cleanup(func() {
		clientTestCloseObserverOK(t, obs)
	})
	for _, generation := range generations {
		if err := obs.Expect(generation); err != nil {
			t.Fatal("expect client test generation")
		}
	}
	return obs, socket
}

func clientTestSession(socket string, generation uint64) testcontrol.Session {
	return testcontrol.Session{
		Socket:     socket,
		RunID:      clientTestRunID,
		Epoch:      clientTestEpoch,
		Generation: generation,
	}
}

func clientTestDial(t *testing.T, socket string, generation uint64) *testcontrol.Client {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancel()
	client, err := testcontrol.Dial(ctx, clientTestSession(socket, generation))
	if err != nil {
		t.Fatal("dial real test control socket")
	}
	if client == nil {
		t.Fatal("dial returned a nil client")
	}
	t.Cleanup(func() {
		clientTestCloseOK(t, client)
	})
	return client
}

func clientTestDialPeer(t *testing.T, peer *clientTestPeer, generation uint64) *testcontrol.Client {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancel()
	client, err := testcontrol.Dial(ctx, clientTestSession(peer.socket, generation))
	if err != nil {
		t.Fatal("dial scripted test control peer")
	}
	if client == nil {
		t.Fatal("dial returned a nil client")
	}
	t.Cleanup(func() {
		clientTestCloseOK(t, client)
	})
	return client
}

func clientTestCloseOK(t *testing.T, client *testcontrol.Client) {
	t.Helper()
	if err := client.Close(); err != nil {
		t.Error("close client failed")
	}
}

func clientTestCloseObserverOK(t *testing.T, obs *harness.Observer) {
	t.Helper()
	closeCtx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancel()
	if err := obs.Close(closeCtx); err != nil {
		t.Error("close real observer failed")
	}
}

func clientTestRequireFault(t *testing.T, err error) {
	t.Helper()
	if err == nil {
		t.Fatal("expected a fixed control fault, got success")
	}
	if !errors.Is(err, testcontrol.ErrControlFault) {
		t.Fatal("expected a fixed control fault")
	}
	if strings.Contains(err.Error(), clientTestCanary) {
		t.Fatal("control fault echoed rejected input")
	}
}

func clientTestRequireReplyHidden(t *testing.T, err error) {
	t.Helper()
	if err == nil {
		t.Fatal("expected a controlled reply-hidden result, got success")
	}
	if !errors.Is(err, testcontrol.ErrReplyHidden) {
		t.Fatal("expected a controlled reply-hidden result")
	}
	if strings.Contains(err.Error(), clientTestCanary) {
		t.Fatal("reply-hidden result echoed rejected input")
	}
}

func clientTestRequireZeroReceipt(t *testing.T, receipt testcontrol.Receipt) {
	t.Helper()
	if receipt != (testcontrol.Receipt{}) {
		t.Fatal("failed operation returned a non-zero receipt")
	}
}

func clientTestIsClosedPeerWrite(err error) bool {
	return errors.Is(err, syscall.EPIPE) ||
		errors.Is(err, syscall.ECONNRESET) ||
		errors.Is(err, syscall.ECONNABORTED)
}

func clientTestAssertSealFault(t *testing.T, client *testcontrol.Client) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancel()
	receipt, err := client.Seal(ctx)
	clientTestRequireFault(t, err)
	clientTestRequireZeroReceipt(t, receipt)
}

func clientTestExpectEnterFault(t *testing.T, client *testcontrol.Client, kind testcontrol.Kind, orderID string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancel()
	clientTestRequireFault(t, client.Enter(ctx, kind, orderID))
}

func clientTestExpectReachFault(t *testing.T, client *testcontrol.Client, point testcontrol.Point, orderID string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancel()
	clientTestRequireFault(t, client.Reach(ctx, point, orderID))
}

func clientTestEnterOK(t *testing.T, client *testcontrol.Client, kind testcontrol.Kind, orderID string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancel()
	if err := client.Enter(ctx, kind, orderID); err != nil {
		t.Fatal("client enter through the real observer failed")
	}
}

func clientTestReachOK(t *testing.T, client *testcontrol.Client, point testcontrol.Point, orderID string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancel()
	if err := client.Reach(ctx, point, orderID); err != nil {
		t.Fatal("client reach through the real observer failed")
	}
}

func clientTestSealOK(t *testing.T, client *testcontrol.Client) testcontrol.Receipt {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancel()
	receipt, err := client.Seal(ctx)
	if err != nil {
		t.Fatal("client seal through the real observer failed")
	}
	return receipt
}

// clientTestPeer is a scripted private UDS peer. It accepts one request per
// connection and lets the script decide whether to answer, drop, or corrupt the
// response.
type clientTestPeer struct {
	t        *testing.T
	dir      string
	socket   string
	listener *net.UnixListener
	accepted chan int
	done     chan struct{}

	mu     sync.Mutex
	conns  map[*net.UnixConn]struct{}
	wg     sync.WaitGroup
	script func(*clientTestPeer, int, *net.UnixConn)
}

func clientTestStartPeer(t *testing.T, script func(*clientTestPeer, int, *net.UnixConn)) *clientTestPeer {
	t.Helper()
	dir := clientTestNewDir(t)
	socket := filepath.Join(dir, "s")
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: socket, Net: "unix"})
	if err != nil {
		t.Fatal("create scripted test peer socket")
	}
	listener.SetUnlinkOnClose(false)
	if err := os.Chmod(socket, 0o600); err != nil {
		t.Fatal("secure scripted test peer socket")
	}
	peer := &clientTestPeer{
		t:        t,
		dir:      dir,
		socket:   socket,
		listener: listener,
		accepted: make(chan int, 8),
		done:     make(chan struct{}),
		conns:    make(map[*net.UnixConn]struct{}),
		script:   script,
	}
	go peer.acceptLoop()
	t.Cleanup(peer.close)
	return peer
}

func (p *clientTestPeer) acceptLoop() {
	defer close(p.done)
	for index := 1; ; index++ {
		conn, err := p.listener.AcceptUnix()
		if err != nil {
			return
		}
		p.mu.Lock()
		p.conns[conn] = struct{}{}
		p.mu.Unlock()
		p.accepted <- index
		p.wg.Add(1)
		go func(connectionIndex int, connection *net.UnixConn) {
			defer p.wg.Done()
			defer func() {
				_ = connection.Close()
				p.mu.Lock()
				delete(p.conns, connection)
				p.mu.Unlock()
			}()
			p.script(p, connectionIndex, connection)
		}(index, conn)
	}
}

func (p *clientTestPeer) close() {
	_ = p.listener.Close()
	p.mu.Lock()
	connections := make([]*net.UnixConn, 0, len(p.conns))
	for conn := range p.conns {
		connections = append(connections, conn)
	}
	p.mu.Unlock()
	for _, conn := range connections {
		_ = conn.Close()
	}
	select {
	case <-p.done:
	case <-time.After(clientTestBudget):
		p.t.Errorf("scripted test peer did not stop accepting")
	}
	p.mu.Lock()
	for conn := range p.conns {
		_ = conn.Close()
	}
	p.mu.Unlock()
	handlersDone := make(chan struct{})
	go func() {
		p.wg.Wait()
		close(handlersDone)
	}()
	select {
	case <-handlersDone:
	case <-time.After(clientTestBudget):
		p.t.Errorf("scripted test peer handlers did not stop")
	}
	_ = os.Remove(p.socket)
}

func (p *clientTestPeer) expectAccepted(t *testing.T, want int) {
	t.Helper()
	select {
	case got := <-p.accepted:
		if got != want {
			t.Fatal("scripted test peer saw an unexpected connection order")
		}
	case <-time.After(clientTestBudget):
		t.Fatal("scripted test peer did not accept the expected connection")
	}
}

func (p *clientTestPeer) requireNoMoreAccepted(t *testing.T) {
	t.Helper()
	select {
	case <-p.accepted:
		t.Fatal("client made an unexpected extra transport attempt")
	case <-time.After(clientTestAbsence):
	}
}

func clientTestReadRequest(t *testing.T, conn net.Conn) (testcontrol.Frame, []byte, bool) {
	t.Helper()
	if err := conn.SetReadDeadline(time.Now().Add(clientTestBudget)); err != nil {
		t.Errorf("scripted test peer could not set a read deadline")
		return testcontrol.Frame{}, nil, false
	}
	limit := int64(testcontrol.MaxFrameBytes) + 1
	reader := &io.LimitedReader{R: conn, N: limit}
	buf := make([]byte, 0, 256)
	tmp := make([]byte, 512)
	for {
		n, err := reader.Read(tmp)
		if n > 0 {
			buf = append(buf, tmp[:n]...)
		}
		if err != nil {
			if errors.Is(err, io.EOF) {
				break
			}
			t.Errorf("scripted test peer request read failed")
			return testcontrol.Frame{}, nil, false
		}
		if int64(len(buf)) >= limit {
			break
		}
	}
	frame, err := testcontrol.DecodeFrame(buf)
	if err != nil {
		t.Errorf("scripted test peer request decode failed")
		return testcontrol.Frame{}, nil, false
	}
	return frame, buf, true
}

func (p *clientTestPeer) nextRequest(t *testing.T, conn net.Conn) (testcontrol.Frame, []byte, bool) {
	t.Helper()
	return clientTestReadRequest(t, conn)
}

func (p *clientTestPeer) writeRaw(t *testing.T, conn net.Conn, payload []byte) bool {
	t.Helper()
	if err := conn.SetWriteDeadline(time.Now().Add(clientTestBudget)); err != nil {
		t.Errorf("scripted test peer could not set a write deadline")
		return false
	}
	for len(payload) > 0 {
		n, err := conn.Write(payload)
		if err != nil || n == 0 {
			t.Errorf("scripted test peer response write failed")
			return false
		}
		payload = payload[n:]
	}
	return true
}

func (p *clientTestPeer) writeFrame(t *testing.T, conn net.Conn, frame testcontrol.Frame) bool {
	t.Helper()
	payload, err := testcontrol.EncodeFrame(frame)
	if err != nil {
		t.Errorf("scripted test peer response encode failed")
		return false
	}
	return p.writeRaw(t, conn, payload)
}

func clientTestAckFor(request testcontrol.Frame) testcontrol.Frame {
	ack := testcontrol.Frame{
		Version:    testcontrol.Version,
		Type:       testcontrol.TypeAck,
		RunID:      request.RunID,
		Epoch:      request.Epoch,
		Generation: request.Generation,
		EventID:    request.EventID,
		For:        request.Type,
	}
	switch request.Type {
	case testcontrol.TypeBegin:
	case testcontrol.TypeCall:
		ack.Seq = request.Seq
	case testcontrol.TypeReach:
		ack.Seq = request.Seq
		ack.Action = testcontrol.ActionContinue
	case testcontrol.TypeSeal:
		if request.ExpectedLastSeq != nil {
			expected := *request.ExpectedLastSeq
			ack.ExpectedLastSeq = &expected
		}
	}
	return ack
}

func TestClientRealObserverIndependentEventsAndSeal(t *testing.T) {
	obs, socket := clientTestStartObserver(t, 1)
	client := clientTestDial(t, socket, 1)

	// Repeating the same order and kind must allocate new local sequences.
	clientTestEnterOK(t, client, testcontrol.KindPayment, clientTestOrderA)
	clientTestEnterOK(t, client, testcontrol.KindPayment, clientTestOrderA)
	clientTestEnterOK(t, client, testcontrol.KindQuery, clientTestOrderA)
	clientTestReachOK(t, client, testcontrol.PointRegistered, clientTestOrderA)
	clientTestReachOK(t, client, testcontrol.PointRegistered, clientTestOrderA)

	receipt := clientTestSealOK(t, client)
	if receipt.LastSeq != 5 {
		t.Fatal("client seal did not close the locally allocated sequence")
	}
	if receipt.RunID != clientTestRunID || receipt.Epoch != clientTestEpoch || receipt.Generation != 1 {
		t.Fatal("client seal receipt identity mismatch")
	}

	again := clientTestSealOK(t, client)
	if again != receipt {
		t.Fatal("idempotent seal did not return the same receipt")
	}

	counts, err := obs.Snapshot()
	if err != nil {
		t.Fatal("observer snapshot after sealed client failed")
	}
	if counts.Payment != 2 || counts.Query != 1 {
		t.Fatal("observer did not count independent payment and query calls")
	}

	// The caller deliberately ignored these post-seal operation failures. They
	// still broke the trusted static boundary, so a later Seal must fail and
	// must not return a trusted receipt.
	clientTestExpectEnterFault(t, client, testcontrol.KindPayment, clientTestOrderB)
	clientTestExpectReachFault(t, client, testcontrol.PointRegistered, clientTestOrderB)
	clientTestAssertSealFault(t, client)
	if err := client.Close(); err != nil {
		t.Fatal("close sealed client failed")
	}
}

func TestClientConcurrentReachDoesNotBlockOtherEvents(t *testing.T) {
	obs, socket := clientTestStartObserver(t, 1)
	client := clientTestDial(t, socket, 1)

	barrier, err := obs.Arm(1, testcontrol.PointRegistered, clientTestOrderA)
	if err != nil {
		t.Fatal("arm concurrent reach barrier failed")
	}
	reachCtx, cancelReach := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancelReach()
	reachDone := make(chan error, 1)
	go func() {
		reachDone <- client.Reach(reachCtx, testcontrol.PointRegistered, clientTestOrderA)
	}()

	waitCtx, cancelWait := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancelWait()
	if _, err := barrier.Wait(waitCtx); err != nil {
		t.Fatal("wait for concurrent reach arrival failed")
	}

	// A pending Reach must not block independent Enter/Reach operations.
	clientTestEnterOK(t, client, testcontrol.KindPayment, clientTestOrderB)
	clientTestReachOK(t, client, testcontrol.PointMockCommitted, clientTestOrderB)

	if err := barrier.Release(testcontrol.ActionContinue); err != nil {
		t.Fatal("release concurrent reach barrier failed")
	}
	select {
	case err := <-reachDone:
		if err != nil {
			t.Fatal("concurrent reach through the real observer failed")
		}
	case <-time.After(clientTestBudget):
		t.Fatal("concurrent reach did not return after release")
	}

	receipt := clientTestSealOK(t, client)
	if receipt.LastSeq != 3 {
		t.Fatal("seal did not close all independent concurrent events")
	}
	counts, err := obs.Snapshot()
	if err != nil {
		t.Fatal("observer snapshot after concurrent events failed")
	}
	if counts.Payment != 1 || counts.Query != 0 {
		t.Fatal("observer counted concurrent events incorrectly")
	}
	clientTestCloseOK(t, client)
}

func TestClientReplyHiddenDoesNotPoisonTrustedWindow(t *testing.T) {
	obs, socket := clientTestStartObserver(t, 1)
	client := clientTestDial(t, socket, 1)

	barrier, err := obs.Arm(1, testcontrol.PointRegistered, clientTestOrderA)
	if err != nil {
		t.Fatal("arm reply-hidden barrier failed")
	}
	reachCtx, cancelReach := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancelReach()
	reachDone := make(chan error, 1)
	go func() {
		reachDone <- client.Reach(reachCtx, testcontrol.PointRegistered, clientTestOrderA)
	}()

	waitCtx, cancelWait := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancelWait()
	if _, err := barrier.Wait(waitCtx); err != nil {
		t.Fatal("wait for reply-hidden reach arrival failed")
	}
	if err := barrier.Release(testcontrol.ActionReplyHidden); err != nil {
		t.Fatal("release reply_hidden at a real postcommit point failed")
	}
	select {
	case err := <-reachDone:
		clientTestRequireReplyHidden(t, err)
	case <-time.After(clientTestBudget):
		t.Fatal("reply-hidden reach did not return after release")
	}

	// A legitimate postcommit reply_hidden is not a tool fault: the same
	// trusted window must remain usable and sealable.
	clientTestEnterOK(t, client, testcontrol.KindPayment, clientTestOrderB)
	receipt := clientTestSealOK(t, client)
	if receipt.LastSeq != 2 {
		t.Fatal("seal after controlled reply hiding did not close the window")
	}
	counts, err := obs.Snapshot()
	if err != nil {
		t.Fatal("observer snapshot after controlled reply hiding failed")
	}
	if counts.Payment != 1 || counts.Query != 0 {
		t.Fatal("controlled reply hiding polluted observer counts")
	}
	clientTestCloseOK(t, client)
}

func TestClientDialStartupContextIsNotLifetime(t *testing.T) {
	obs, socket := clientTestStartObserver(t, 1)

	canceledCtx, cancelCanceled := context.WithCancel(context.Background())
	cancelCanceled()
	canceledClient, err := testcontrol.Dial(canceledCtx, clientTestSession(socket, 1))
	clientTestRequireFault(t, err)
	if canceledClient != nil {
		clientTestCloseOK(t, canceledClient)
		t.Fatal("dial returned a client with an error")
	}

	startCtx, cancelStart := context.WithCancel(context.Background())
	client, err := testcontrol.Dial(startCtx, clientTestSession(socket, 1))
	if err != nil {
		t.Fatal("dial with a startup context failed")
	}
	if client == nil {
		t.Fatal("dial returned a nil client")
	}
	t.Cleanup(func() {
		clientTestCloseOK(t, client)
	})
	cancelStart()

	clientTestEnterOK(t, client, testcontrol.KindPayment, clientTestOrderA)
	clientTestReachOK(t, client, testcontrol.PointRegistered, clientTestOrderA)
	receipt := clientTestSealOK(t, client)
	if receipt.LastSeq != 2 {
		t.Fatal("canceling the startup context terminated the client lifetime")
	}
	clientTestCloseObserverOK(t, obs)
	clientTestCloseOK(t, client)
}

func TestClientIndependentBeginNonces(t *testing.T) {
	obs, socket := clientTestStartObserver(t, 1, 2)
	clientOne := clientTestDial(t, socket, 1)
	clientTwo := clientTestDial(t, socket, 2)

	// The observer rejects a reused Begin nonce across generations, so two
	// successful Begin operations prove independent client nonces.
	clientTestEnterOK(t, clientOne, testcontrol.KindPayment, clientTestOrderA)
	clientTestEnterOK(t, clientTwo, testcontrol.KindPayment, clientTestOrderA)
	one := clientTestSealOK(t, clientOne)
	two := clientTestSealOK(t, clientTwo)
	if one.Generation != 1 || one.LastSeq != 1 {
		t.Fatal("first client did not seal its own generation")
	}
	if two.Generation != 2 || two.LastSeq != 1 {
		t.Fatal("second client did not seal its own generation")
	}
	counts, err := obs.Snapshot()
	if err != nil {
		t.Fatal("observer snapshot after independent clients failed")
	}
	if counts.Payment != 2 {
		t.Fatal("independent clients did not produce independent payment events")
	}
	clientTestCloseOK(t, clientOne)
	clientTestCloseOK(t, clientTwo)
}

func TestClientSealDrainsInflightOperations(t *testing.T) {
	obs, socket := clientTestStartObserver(t, 1)
	client := clientTestDial(t, socket, 1)

	barrier, err := obs.Arm(1, testcontrol.PointRegistered, clientTestOrderA)
	if err != nil {
		t.Fatal("arm in-flight seal barrier failed")
	}
	reachCtx, cancelReach := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancelReach()
	reachDone := make(chan error, 1)
	go func() {
		reachDone <- client.Reach(reachCtx, testcontrol.PointRegistered, clientTestOrderA)
	}()
	waitCtx, cancelWait := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancelWait()
	if _, err := barrier.Wait(waitCtx); err != nil {
		t.Fatal("wait for in-flight reach arrival failed")
	}

	sealCtx, cancelSeal := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancelSeal()
	sealDone := make(chan clientTestSealResult, 1)
	go func() {
		receipt, err := client.Seal(sealCtx)
		sealDone <- clientTestSealResult{receipt: receipt, err: err}
	}()
	select {
	case <-sealDone:
		t.Fatal("seal returned before the in-flight operation drained")
	case <-time.After(clientTestAbsence):
	}

	if err := barrier.Release(testcontrol.ActionContinue); err != nil {
		t.Fatal("release in-flight reach barrier failed")
	}
	select {
	case err := <-reachDone:
		if err != nil {
			t.Fatal("in-flight reach failed during seal drain")
		}
	case <-time.After(clientTestBudget):
		t.Fatal("in-flight reach did not return during seal drain")
	}
	select {
	case result := <-sealDone:
		if result.err != nil {
			t.Fatal("seal failed after the in-flight operation drained")
		}
		if result.receipt.LastSeq != 1 {
			t.Fatal("seal did not use the local sequence count")
		}
	case <-time.After(clientTestBudget):
		t.Fatal("seal did not return after the in-flight operation drained")
	}
	clientTestCloseOK(t, client)
}

func TestClientCloseDoesNotSeal(t *testing.T) {
	obs, socket := clientTestStartObserver(t, 1)
	client := clientTestDial(t, socket, 1)
	clientTestEnterOK(t, client, testcontrol.KindPayment, clientTestOrderA)

	if err := client.Close(); err != nil {
		t.Fatal("close unsealed client failed")
	}
	if err := client.Close(); err != nil {
		t.Fatal("repeated close was not safe")
	}

	// If Close had implicitly sealed, Snapshot would be valid with a count.
	if _, err := obs.Snapshot(); !errors.Is(err, testcontrol.ErrUncertain) {
		t.Fatal("close implicitly sealed the trusted window")
	}
	clientTestExpectEnterFault(t, client, testcontrol.KindPayment, clientTestOrderA)
	clientTestExpectReachFault(t, client, testcontrol.PointRegistered, clientTestOrderA)
	clientTestAssertSealFault(t, client)
}

func TestClientNilAndZeroValueSafe(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	var nilClient *testcontrol.Client
	clientTestRequireFault(t, nilClient.Enter(ctx, testcontrol.KindPayment, clientTestOrderA))
	clientTestRequireFault(t, nilClient.Reach(ctx, testcontrol.PointRegistered, clientTestOrderA))
	receipt, err := nilClient.Seal(ctx)
	clientTestRequireFault(t, err)
	clientTestRequireZeroReceipt(t, receipt)
	if err := nilClient.Close(); err != nil {
		t.Fatal("nil client close was not safe")
	}
	if err := nilClient.Close(); err != nil {
		t.Fatal("repeated nil client close was not safe")
	}

	var zero testcontrol.Client
	clientTestRequireFault(t, zero.Enter(ctx, testcontrol.KindPayment, clientTestOrderA))
	clientTestRequireFault(t, zero.Reach(ctx, testcontrol.PointRegistered, clientTestOrderA))
	zeroReceipt, zeroErr := zero.Seal(ctx)
	clientTestRequireFault(t, zeroErr)
	clientTestRequireZeroReceipt(t, zeroReceipt)
	if err := zero.Close(); err != nil {
		t.Fatal("zero value client close was not safe")
	}
	if err := zero.Close(); err != nil {
		t.Fatal("repeated zero value client close was not safe")
	}
}

func TestClientRejectsInvalidInputAndLatches(t *testing.T) {
	cases := []struct {
		name   string
		invoke func(*testing.T, *testcontrol.Client)
	}{
		{
			name: "invalid order",
			invoke: func(t *testing.T, client *testcontrol.Client) {
				clientTestExpectEnterFault(t, client, testcontrol.KindPayment, clientTestCanary)
			},
		},
		{
			name: "empty order",
			invoke: func(t *testing.T, client *testcontrol.Client) {
				clientTestExpectEnterFault(t, client, testcontrol.KindPayment, "")
			},
		},
		{
			name: "invalid kind",
			invoke: func(t *testing.T, client *testcontrol.Client) {
				clientTestExpectEnterFault(t, client, testcontrol.Kind(clientTestCanary), clientTestOrderA)
			},
		},
		{
			name: "invalid point",
			invoke: func(t *testing.T, client *testcontrol.Client) {
				clientTestExpectReachFault(t, client, testcontrol.Point(clientTestCanary), clientTestOrderA)
			},
		},
		{
			name: "canceled operation context",
			invoke: func(t *testing.T, client *testcontrol.Client) {
				ctx, cancel := context.WithCancel(context.Background())
				cancel()
				clientTestRequireFault(t, client.Enter(ctx, testcontrol.KindPayment, clientTestOrderA))
			},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, socket := clientTestStartObserver(t, 1)
			client := clientTestDial(t, socket, 1)
			tc.invoke(t, client)
			// A rejected input must prevent a later trusted seal even when the
			// caller ignored the original failure.
			clientTestAssertSealFault(t, client)
			clientTestCloseOK(t, client)
		})
	}
}

func clientTestListen(t *testing.T, dir, name string, mode os.FileMode) (*net.UnixListener, string) {
	t.Helper()
	socket := filepath.Join(dir, name)
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: socket, Net: "unix"})
	if err != nil {
		t.Fatal("create untrusted test socket")
	}
	listener.SetUnlinkOnClose(false)
	if err := os.Chmod(socket, mode); err != nil {
		t.Fatal("set untrusted test socket mode")
	}
	t.Cleanup(func() {
		_ = listener.Close()
		_ = os.Remove(socket)
	})
	return listener, socket
}

func clientTestAssertNoDial(t *testing.T, listener *net.UnixListener) {
	t.Helper()
	if err := listener.SetDeadline(time.Now().Add(clientTestAbsence)); err != nil {
		t.Fatal("set test listener deadline")
	}
	conn, err := listener.AcceptUnix()
	if err == nil {
		_ = conn.Close()
		t.Fatal("client dialed an untrusted socket")
	}
}

func TestClientRejectsUnsafeSockets(t *testing.T) {
	dialFault := func(t *testing.T, session testcontrol.Session) {
		t.Helper()
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		client, err := testcontrol.Dial(ctx, session)
		clientTestRequireFault(t, err)
		if client != nil {
			clientTestCloseOK(t, client)
			t.Fatal("dial returned a client with an error")
		}
	}

	t.Run("empty path", func(t *testing.T) {
		dialFault(t, clientTestSession("", 1))
	})

	t.Run("relative path", func(t *testing.T) {
		dialFault(t, clientTestSession(filepath.Join(clientTestCanary, "s"), 1))
	})

	t.Run("missing path", func(t *testing.T) {
		dir := clientTestNewDir(t)
		dialFault(t, clientTestSession(filepath.Join(dir, "missing"), 1))
	})

	t.Run("regular file", func(t *testing.T) {
		dir := clientTestNewDir(t)
		path := filepath.Join(dir, "regular")
		if err := os.WriteFile(path, []byte("not a socket"), 0o600); err != nil {
			t.Fatal("create regular test file")
		}
		dialFault(t, clientTestSession(path, 1))
		info, err := os.Stat(path)
		if err != nil {
			t.Fatal("regular test file disappeared")
		}
		if info.Mode().Perm() != 0o600 {
			t.Fatal("client changed the regular test file mode")
		}
	})

	t.Run("socket mode", func(t *testing.T) {
		dir := clientTestNewDir(t)
		listener, socket := clientTestListen(t, dir, "s", 0o644)
		dialFault(t, clientTestSession(socket, 1))
		clientTestAssertNoDial(t, listener)
		info, err := os.Stat(socket)
		if err != nil {
			t.Fatal("test socket disappeared")
		}
		if info.Mode().Perm() != 0o644 {
			t.Fatal("client changed an existing socket mode")
		}
	})

	t.Run("symlink", func(t *testing.T) {
		dir := clientTestNewDir(t)
		listener, target := clientTestListen(t, dir, "target", 0o600)
		link := filepath.Join(dir, "link")
		if err := os.Symlink(target, link); err != nil {
			t.Fatal("create test socket symlink")
		}
		dialFault(t, clientTestSession(link, 1))
		clientTestAssertNoDial(t, listener)
		info, err := os.Lstat(link)
		if err != nil {
			t.Fatal("test socket symlink disappeared")
		}
		if info.Mode()&os.ModeSymlink == 0 {
			t.Fatal("client replaced the test socket symlink")
		}
	})

	t.Run("non private parent", func(t *testing.T) {
		dir := clientTestNewDirMode(t, 0o755)
		listener, socket := clientTestListen(t, dir, "s", 0o600)
		dialFault(t, clientTestSession(socket, 1))
		clientTestAssertNoDial(t, listener)
		info, err := os.Stat(dir)
		if err != nil {
			t.Fatal("test socket directory disappeared")
		}
		if info.Mode().Perm() != 0o755 {
			t.Fatal("client changed an existing directory mode")
		}
	})

	t.Run("invalid run id", func(t *testing.T) {
		dir := clientTestNewDir(t)
		listener, socket := clientTestListen(t, dir, "s", 0o600)
		session := clientTestSession(socket, 1)
		session.RunID = clientTestCanary
		dialFault(t, session)
		clientTestAssertNoDial(t, listener)
	})

	t.Run("invalid epoch", func(t *testing.T) {
		dir := clientTestNewDir(t)
		listener, socket := clientTestListen(t, dir, "s", 0o600)
		session := clientTestSession(socket, 1)
		session.Epoch = clientTestCanary
		dialFault(t, session)
		clientTestAssertNoDial(t, listener)
	})

	t.Run("zero generation", func(t *testing.T) {
		dir := clientTestNewDir(t)
		listener, socket := clientTestListen(t, dir, "s", 0o600)
		dialFault(t, clientTestSession(socket, 0))
		clientTestAssertNoDial(t, listener)
	})
}

func TestClientTransportRetryReusesSameFrame(t *testing.T) {
	var mu sync.Mutex
	var begins [5][]byte
	var calls [5][]byte
	beginStored := make(chan struct{})
	callStored := make(chan struct{})
	peer := clientTestStartPeer(t, func(p *clientTestPeer, index int, conn *net.UnixConn) {
		request, raw, ok := p.nextRequest(t, conn)
		if !ok {
			return
		}
		switch index {
		case 1, 2:
			if request.Type != testcontrol.TypeBegin {
				t.Errorf("scripted test peer expected a begin frame")
				return
			}
			mu.Lock()
			begins[index] = append([]byte(nil), raw...)
			mu.Unlock()
			if index == 1 {
				close(beginStored)
				return
			}
			select {
			case <-beginStored:
			case <-time.After(clientTestBudget):
				t.Errorf("scripted test peer did not see a begin retry")
				return
			}
			mu.Lock()
			same := bytes.Equal(begins[1], begins[2])
			mu.Unlock()
			if !same {
				t.Errorf("begin retry did not reuse the original frame")
				return
			}
			p.writeFrame(t, conn, clientTestAckFor(request))
		case 3, 4:
			if request.Type != testcontrol.TypeCall {
				t.Errorf("scripted test peer expected a call frame")
				return
			}
			mu.Lock()
			calls[index] = append([]byte(nil), raw...)
			mu.Unlock()
			if index == 3 {
				close(callStored)
				return
			}
			select {
			case <-callStored:
			case <-time.After(clientTestBudget):
				t.Errorf("scripted test peer did not see a call retry")
				return
			}
			mu.Lock()
			same := bytes.Equal(calls[3], calls[4])
			mu.Unlock()
			if !same {
				t.Errorf("call retry did not reuse the original frame")
				return
			}
			p.writeFrame(t, conn, clientTestAckFor(request))
		default:
			t.Errorf("client made an unexpected transport attempt")
		}
	})

	client := clientTestDialPeer(t, peer, 1)
	peer.expectAccepted(t, 1)
	peer.expectAccepted(t, 2)
	clientTestEnterOK(t, client, testcontrol.KindPayment, clientTestOrderA)
	peer.expectAccepted(t, 3)
	peer.expectAccepted(t, 4)
	peer.requireNoMoreAccepted(t)
	clientTestCloseOK(t, client)
}

func TestClientUnconfirmedOutcomeBlocksSeal(t *testing.T) {
	var mu sync.Mutex
	var calls [4][]byte
	peer := clientTestStartPeer(t, func(p *clientTestPeer, index int, conn *net.UnixConn) {
		request, raw, ok := p.nextRequest(t, conn)
		if !ok {
			return
		}
		switch index {
		case 1:
			if request.Type != testcontrol.TypeBegin {
				t.Errorf("scripted test peer expected a begin frame")
				return
			}
			p.writeFrame(t, conn, clientTestAckFor(request))
		case 2, 3:
			if request.Type != testcontrol.TypeCall {
				t.Errorf("scripted test peer expected a call frame")
				return
			}
			mu.Lock()
			calls[index] = append([]byte(nil), raw...)
			mu.Unlock()
			// Drop both transport attempts without a response.
		default:
			t.Errorf("client exceeded the transport attempt budget")
		}
	})

	client := clientTestDialPeer(t, peer, 1)
	peer.expectAccepted(t, 1)
	ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancel()
	clientTestRequireFault(t, client.Enter(ctx, testcontrol.KindPayment, clientTestOrderA))
	peer.expectAccepted(t, 2)
	peer.expectAccepted(t, 3)
	peer.requireNoMoreAccepted(t)

	mu.Lock()
	twoAttempts := calls[2] != nil && calls[3] != nil
	same := twoAttempts && bytes.Equal(calls[2], calls[3])
	mu.Unlock()
	if !twoAttempts {
		t.Fatal("client did not use exactly two transport attempts")
	}
	if !same {
		t.Fatal("unconfirmed retry did not reuse the original frame")
	}

	// The caller ignoring the failed operation must not be able to seal a
	// window whose final delivery is unknown.
	clientTestAssertSealFault(t, client)
	clientTestCloseOK(t, client)
}

func clientTestRunRejectedResponse(t *testing.T, respond func(*clientTestPeer, *net.UnixConn, testcontrol.Frame) bool) {
	t.Helper()
	peer := clientTestStartPeer(t, func(p *clientTestPeer, index int, conn *net.UnixConn) {
		request, _, ok := p.nextRequest(t, conn)
		if !ok {
			return
		}
		switch index {
		case 1:
			if request.Type != testcontrol.TypeBegin {
				t.Errorf("scripted test peer expected a begin frame")
				return
			}
			p.writeFrame(t, conn, clientTestAckFor(request))
		case 2:
			if request.Type != testcontrol.TypeCall {
				t.Errorf("scripted test peer expected a call frame")
				return
			}
			respond(p, conn, request)
		default:
			t.Errorf("client retried a known invalid response")
		}
	})

	client := clientTestDialPeer(t, peer, 1)
	peer.expectAccepted(t, 1)
	ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancel()
	clientTestRequireFault(t, client.Enter(ctx, testcontrol.KindPayment, clientTestOrderA))
	peer.expectAccepted(t, 2)
	peer.requireNoMoreAccepted(t)
	clientTestAssertSealFault(t, client)
	clientTestCloseOK(t, client)
}

func TestClientDoesNotRetryKnownFaultOrBadAck(t *testing.T) {
	t.Run("known fault", func(t *testing.T) {
		clientTestRunRejectedResponse(t, func(p *clientTestPeer, conn *net.UnixConn, _ testcontrol.Frame) bool {
			return p.writeFrame(t, conn, testcontrol.Frame{
				Version: testcontrol.Version,
				Type:    testcontrol.TypeFault,
				Code:    testcontrol.FaultProtocol,
			})
		})
	})

	t.Run("mismatched ack", func(t *testing.T) {
		clientTestRunRejectedResponse(t, func(p *clientTestPeer, conn *net.UnixConn, request testcontrol.Frame) bool {
			ack := clientTestAckFor(request)
			ack.For = testcontrol.TypeReach
			ack.Action = testcontrol.ActionContinue
			return p.writeFrame(t, conn, ack)
		})
	})

	t.Run("wrong identity ack", func(t *testing.T) {
		clientTestRunRejectedResponse(t, func(p *clientTestPeer, conn *net.UnixConn, request testcontrol.Frame) bool {
			ack := clientTestAckFor(request)
			ack.RunID = strings.Repeat("a", 32)
			return p.writeFrame(t, conn, ack)
		})
	})

	t.Run("extra response frame", func(t *testing.T) {
		clientTestRunRejectedResponse(t, func(p *clientTestPeer, conn *net.UnixConn, request testcontrol.Frame) bool {
			first, err := testcontrol.EncodeFrame(clientTestAckFor(request))
			if err != nil {
				t.Errorf("scripted test peer response encode failed")
				return false
			}
			second, err := testcontrol.EncodeFrame(clientTestAckFor(request))
			if err != nil {
				t.Errorf("scripted test peer response encode failed")
				return false
			}
			return p.writeRaw(t, conn, append(first, second...))
		})
	})

	t.Run("oversized response", func(t *testing.T) {
		clientTestRunRejectedResponse(t, func(p *clientTestPeer, conn *net.UnixConn, _ testcontrol.Frame) bool {
			return p.writeRaw(t, conn, bytes.Repeat([]byte("x"), testcontrol.MaxFrameBytes+1))
		})
	})
}

func TestClientRequiresCompleteResponseEOF(t *testing.T) {
	ackWritten := make(chan struct{})
	hold := make(chan struct{})
	var holdOnce sync.Once
	releaseHold := func() {
		holdOnce.Do(func() {
			close(hold)
		})
	}
	defer releaseHold()

	peer := clientTestStartPeer(t, func(p *clientTestPeer, index int, conn *net.UnixConn) {
		request, _, ok := p.nextRequest(t, conn)
		if !ok {
			return
		}
		switch index {
		case 1:
			if request.Type != testcontrol.TypeBegin {
				t.Errorf("scripted test peer expected a begin frame")
				return
			}
			p.writeFrame(t, conn, clientTestAckFor(request))
		case 2:
			if request.Type != testcontrol.TypeCall {
				t.Errorf("scripted test peer expected a call frame")
				return
			}
			if !p.writeFrame(t, conn, clientTestAckFor(request)) {
				return
			}
			close(ackWritten)
			<-hold
		default:
			t.Errorf("client made an unexpected transport attempt")
		}
	})

	client := clientTestDialPeer(t, peer, 1)
	peer.expectAccepted(t, 1)
	ctx, cancel := context.WithTimeout(context.Background(), clientTestShort)
	defer cancel()
	err := client.Enter(ctx, testcontrol.KindPayment, clientTestOrderA)
	clientTestRequireFault(t, err)
	peer.expectAccepted(t, 2)
	select {
	case <-ackWritten:
	case <-time.After(clientTestBudget):
		t.Fatal("scripted test peer did not write a response")
	}
	releaseHold()
	clientTestAssertSealFault(t, client)
	clientTestCloseOK(t, client)
}

func TestClientCancelInterruptsOwnedIO(t *testing.T) {
	requestSeen := make(chan struct{})
	probeResult := make(chan error, 1)
	releaseProbe := make(chan struct{})
	var releaseOnce sync.Once
	release := func() {
		releaseOnce.Do(func() {
			close(releaseProbe)
		})
	}
	defer release()

	peer := clientTestStartPeer(t, func(p *clientTestPeer, index int, conn *net.UnixConn) {
		request, _, ok := p.nextRequest(t, conn)
		if !ok {
			return
		}
		switch index {
		case 1:
			if request.Type != testcontrol.TypeBegin {
				t.Errorf("scripted test peer expected a begin frame")
				return
			}
			p.writeFrame(t, conn, clientTestAckFor(request))
		case 2:
			if request.Type != testcontrol.TypeCall {
				t.Errorf("scripted test peer expected a call frame")
				return
			}
			// Keep the server side open and silent after consuming the client
			// CloseWrite. This proves cancellation, not a half-close EOF.
			close(requestSeen)
			<-releaseProbe
			if err := conn.SetWriteDeadline(time.Now().Add(clientTestShort)); err != nil {
				probeResult <- err
				return
			}
			_, err := conn.Write([]byte{0})
			probeResult <- err
		default:
			t.Errorf("client made an unexpected transport attempt")
		}
	})

	client := clientTestDialPeer(t, peer, 1)
	peer.expectAccepted(t, 1)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	enterDone := make(chan error, 1)
	go func() {
		enterDone <- client.Enter(ctx, testcontrol.KindPayment, clientTestOrderA)
	}()
	select {
	case <-requestSeen:
	case <-time.After(clientTestBudget):
		t.Fatal("scripted test peer did not receive the call request")
	}
	peer.expectAccepted(t, 2)
	cancel()
	select {
	case err := <-enterDone:
		clientTestRequireFault(t, err)
	case <-time.After(time.Second):
		t.Fatal("explicit cancellation did not interrupt owned I/O within the short fallback")
	}

	release()
	select {
	case err := <-probeResult:
		if err == nil {
			t.Fatal("server write probe succeeded; client left its read end open")
		}
		if !clientTestIsClosedPeerWrite(err) {
			t.Fatal("server write probe did not observe a closed client connection")
		}
	case <-time.After(time.Second):
		t.Fatal("server write probe did not complete")
	}
	clientTestAssertSealFault(t, client)
	clientTestCloseOK(t, client)
}

func TestClientSealInProgressRejectsNewOperations(t *testing.T) {
	sealSeen := make(chan struct{})
	releaseSeal := make(chan struct{})
	var releaseOnce sync.Once
	release := func() {
		releaseOnce.Do(func() {
			close(releaseSeal)
		})
	}
	defer release()

	peer := clientTestStartPeer(t, func(p *clientTestPeer, index int, conn *net.UnixConn) {
		request, _, ok := p.nextRequest(t, conn)
		if !ok {
			return
		}
		switch index {
		case 1:
			if request.Type != testcontrol.TypeBegin {
				t.Errorf("scripted test peer expected a begin frame")
				return
			}
			p.writeFrame(t, conn, clientTestAckFor(request))
		case 2:
			if request.Type != testcontrol.TypeCall {
				t.Errorf("scripted test peer expected a call frame")
				return
			}
			p.writeFrame(t, conn, clientTestAckFor(request))
		case 3:
			if request.Type != testcontrol.TypeSeal || request.ExpectedLastSeq == nil || *request.ExpectedLastSeq != 1 {
				t.Errorf("seal did not use the local sequence count")
				return
			}
			close(sealSeen)
			<-releaseSeal
			// The ACK is valid, but a trusted Seal must not succeed after the
			// static boundary was broken by a new operation.
			payload, err := testcontrol.EncodeFrame(clientTestAckFor(request))
			if err != nil {
				t.Errorf("scripted test peer response encode failed")
				return
			}
			if err := conn.SetWriteDeadline(time.Now().Add(clientTestShort)); err != nil {
				return
			}
			_, _ = conn.Write(payload)
		default:
			t.Errorf("client allocated a new operation after seal started")
		}
	})

	client := clientTestDialPeer(t, peer, 1)
	peer.expectAccepted(t, 1)
	clientTestEnterOK(t, client, testcontrol.KindPayment, clientTestOrderA)
	peer.expectAccepted(t, 2)

	sealCtx, cancelSeal := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancelSeal()
	sealDone := make(chan clientTestSealResult, 1)
	go func() {
		receipt, err := client.Seal(sealCtx)
		sealDone <- clientTestSealResult{receipt: receipt, err: err}
	}()
	select {
	case <-sealSeen:
	case <-time.After(clientTestBudget):
		t.Fatal("scripted test peer did not receive the seal request")
	}
	peer.expectAccepted(t, 3)

	// New Enter/Reach after Seal started must be rejected locally without
	// allocating a sequence and must latch ControlFault.
	clientTestExpectEnterFault(t, client, testcontrol.KindPayment, clientTestOrderB)
	clientTestExpectReachFault(t, client, testcontrol.PointRegistered, clientTestOrderB)
	peer.requireNoMoreAccepted(t)

	release()
	select {
	case result := <-sealDone:
		clientTestRequireFault(t, result.err)
		clientTestRequireZeroReceipt(t, result.receipt)
	case <-time.After(clientTestBudget):
		t.Fatal("in-flight seal did not return after the valid acknowledgement")
	}

	// A subsequent Seal must not recover from the latched boundary violation.
	subCtx, cancelSub := context.WithTimeout(context.Background(), clientTestShort)
	receipt, err := client.Seal(subCtx)
	cancelSub()
	clientTestRequireFault(t, err)
	clientTestRequireZeroReceipt(t, receipt)
	peer.requireNoMoreAccepted(t)
	clientTestCloseOK(t, client)
}
