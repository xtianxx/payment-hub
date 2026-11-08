package harness

import (
	"context"
	"errors"
	"net"
	"os"
	"testing"
	"time"

	"payment-hub/internal/testcontrol"
)

// TestArtifactsFinishRejectsUnclosedObserverAfterHalfFrame is the D2
// combination regression. It seals a generation, attaches the live observer,
// confirms the observer accepted a half-frame connection, and then calls
// Finish without closing the observer. Finish must not produce a complete
// report because the observer has not been finally drained.
func TestArtifactsFinishRejectsUnclosedObserverAfterHalfFrame(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)
	obsTestSealZero(t, h, 1)
	if err := artifacts.RecordObserver(h.obs); err != nil {
		t.Fatal("record sealed t020 observer failed")
	}

	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	var dialer net.Dialer
	partial, err := dialer.DialContext(ctx, "unix", h.socket)
	if err != nil {
		t.Fatal("dial t020 half-frame connection failed")
	}
	defer partial.Close()
	if _, err := partial.Write([]byte(`{"v":1,"type":"begin"`)); err != nil {
		t.Fatal("write t020 half-frame request failed")
	}

	deadline := time.Now().Add(2 * time.Second)
	for {
		h.obs.mu.Lock()
		accepted := len(h.obs.conns) > 0
		h.obs.mu.Unlock()
		if accepted {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("t020 observer did not accept the half-frame connection")
		}
		time.Sleep(time.Millisecond)
	}

	path, err := artifacts.Finish()
	if !errors.Is(err, ErrArtifacts) {
		t.Fatal("t020 finish accepted an unclosed observer with an in-flight connection")
	}
	report, _ := t020ReadReport(t, path)
	if report.CollectionComplete {
		t.Fatal("t020 finish reported complete collection for an unclosed observer")
	}
	if !t020HasAnyFault(report.Faults, "observer_uncertain") {
		t.Fatal("t020 finish did not record the observer uncertainty")
	}
	if err := h.close(); err != nil {
		t.Fatal("close t020 half-frame observer failed")
	}
}

// TestArtifactsFinishAcceptsClosedTrustedObserver covers the normal path: the
// observer is sealed, attached, explicitly closed, and only then archived.
func TestArtifactsFinishAcceptsClosedTrustedObserver(t *testing.T) {
	source := t020SourceFixture(t)
	artifacts, _ := t020BeginArtifacts(t, source)
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)
	call := obsTestCallFrame(1, obsTestBegin1, 1, testcontrol.KindPayment, obsTestOrderA)
	resp := obsTestMustDo(t, h, call)
	obsTestCheckAck(t, call, resp)
	if _, err := h.obs.Seal(1, 1); err != nil {
		t.Fatal("seal t020 observer generation failed")
	}
	if err := artifacts.RecordObserver(h.obs); err != nil {
		t.Fatal("record trusted t020 observer failed")
	}
	if err := h.close(); err != nil {
		t.Fatal("close trusted t020 observer failed")
	}

	path, err := artifacts.Finish()
	if err != nil {
		t.Fatal("finish with a closed trusted t020 observer failed")
	}
	report, _ := t020ReadReport(t, path)
	if !report.CollectionComplete || report.Observer == nil {
		t.Fatal("closed trusted t020 observer report is incomplete")
	}
	if report.Observer.Counts.Payment != 1 || len(report.Observer.Receipts) != 1 {
		t.Fatal("closed trusted t020 observer report mismatch")
	}
}

func TestObserverFinalSnapshotUnavailableBeforeClose(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	runID, epoch := h.obs.Identity()
	if runID != obsTestRunID || epoch != obsTestEpoch {
		t.Fatal("t020 observer identity mismatch")
	}
	if _, ok := h.obs.FinalSnapshot(); ok {
		t.Fatal("t020 final snapshot was available before close")
	}
	obsTestExpectOpen(t, h, 1, obsTestBegin1)
	obsTestSealZero(t, h, 1)
	if err := h.close(); err != nil {
		t.Fatal("close t020 observer failed")
	}
	final, ok := h.obs.FinalSnapshot()
	if !ok {
		t.Fatal("t020 final snapshot was unavailable after a successful close")
	}
	if !final.Trusted || final.RunID != obsTestRunID || final.Epoch != obsTestEpoch {
		t.Fatal("t020 final snapshot identity or trust mismatch")
	}
	if len(final.Receipts) != 1 || final.Receipts[0].Generation != 1 || final.Receipts[0].LastSeq != 0 {
		t.Fatal("t020 final snapshot receipt mismatch")
	}
}

func TestObserverFinalSnapshotDeepCopyAndStable(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)
	obsTestSealZero(t, h, 1)
	obsTestExpectOpen(t, h, 2, obsTestBegin2)
	obsTestSealZero(t, h, 2)
	if err := h.close(); err != nil {
		t.Fatal("close t020 observer failed")
	}
	first, ok := h.obs.FinalSnapshot()
	if !ok || !first.Trusted || len(first.Receipts) != 2 {
		t.Fatal("t020 final snapshot is not trusted")
	}
	if first.Receipts[0].Generation != 1 || first.Receipts[1].Generation != 2 {
		t.Fatal("t020 final snapshot receipts are not sorted")
	}
	first.Receipts[0].Generation = 99
	first.Receipts = append(first.Receipts, testcontrol.Receipt{Generation: 100})
	first.Counts.Payment = 99
	second, ok := h.obs.FinalSnapshot()
	if !ok {
		t.Fatal("t020 repeated final snapshot failed")
	}
	if len(second.Receipts) != 2 || second.Receipts[0].Generation != 1 ||
		second.Receipts[1].Generation != 2 || second.Counts.Payment != 0 {
		t.Fatal("t020 final snapshot was not a defensive deep copy")
	}
	third, ok := h.obs.FinalSnapshot()
	if !ok || len(third.Receipts) != len(second.Receipts) {
		t.Fatal("t020 final snapshot changed between reads")
	}
	for i := range second.Receipts {
		if third.Receipts[i] != second.Receipts[i] {
			t.Fatal("t020 final snapshot receipts changed between reads")
		}
	}
}

func TestObserverCloseHalfFrameFinalUntrusted(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)
	obsTestSealZero(t, h, 1)

	ctx, cancel := context.WithTimeout(context.Background(), obsTestBudget)
	defer cancel()
	var dialer net.Dialer
	partial, err := dialer.DialContext(ctx, "unix", h.socket)
	if err != nil {
		t.Fatal("dial t020 half-frame connection failed")
	}
	defer partial.Close()
	if _, err := partial.Write([]byte(`{"v":1,"type":"begin"`)); err != nil {
		t.Fatal("write t020 half-frame request failed")
	}
	deadline := time.Now().Add(2 * time.Second)
	for {
		h.obs.mu.Lock()
		accepted := len(h.obs.conns) > 0
		h.obs.mu.Unlock()
		if accepted {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("t020 observer did not accept the half-frame connection")
		}
		time.Sleep(time.Millisecond)
	}
	if err := h.close(); err != nil {
		t.Fatal("close t020 half-frame observer failed")
	}
	final, ok := h.obs.FinalSnapshot()
	if !ok {
		t.Fatal("t020 half-frame close produced no final snapshot")
	}
	if final.Trusted {
		t.Fatal("t020 half-frame close produced a trusted final snapshot")
	}
	if _, err := h.obs.Snapshot(); !errors.Is(err, testcontrol.ErrUncertain) {
		t.Fatal("t020 half-frame close left a trusted snapshot")
	}
}

func TestObserverCloseDoesNotSealOpenGeneration(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)
	if err := h.close(); err != nil {
		t.Fatal("close t020 open-generation observer failed")
	}
	final, ok := h.obs.FinalSnapshot()
	if !ok {
		t.Fatal("t020 open-generation close produced no final snapshot")
	}
	if final.Trusted {
		t.Fatal("t020 open-generation close produced a trusted final snapshot")
	}
	if _, err := h.obs.Snapshot(); !errors.Is(err, testcontrol.ErrUncertain) {
		t.Fatal("t020 open-generation close left a trusted snapshot")
	}
	h.obs.mu.Lock()
	status := h.obs.generations[1].status
	h.obs.mu.Unlock()
	if status == observerGenerationSealed {
		t.Fatal("t020 close auto-sealed an open generation")
	}
}

func TestObserverCloseDeadlineHasNoFinal(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)
	obsTestSealZero(t, h, 1)
	h.obs.wg.Add(1)
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	err := h.obs.Close(ctx)
	cancel()
	h.obs.wg.Done()
	if !errors.Is(err, testcontrol.ErrControlFault) {
		t.Fatal("t020 deadline close did not return a control fault")
	}
	if _, ok := h.obs.FinalSnapshot(); ok {
		t.Fatal("t020 deadline close produced a final snapshot")
	}
	if err := h.obs.Close(context.Background()); !errors.Is(err, testcontrol.ErrControlFault) {
		t.Fatal("t020 close retry changed the original error")
	}
	if _, ok := h.obs.FinalSnapshot(); ok {
		t.Fatal("t020 close retry produced a final snapshot")
	}
	_ = h.close()
}

func TestObserverCloseSocketCleanupFailureHasNoFinal(t *testing.T) {
	h := obsTestStart(t, obsTestBarrier)
	obsTestExpectOpen(t, h, 1, obsTestBegin1)
	obsTestSealZero(t, h, 1)
	moved := h.socket + ".moved"
	if err := os.Rename(h.socket, moved); err != nil {
		t.Fatal("move t020 observer socket failed")
	}
	if err := os.WriteFile(h.socket, []byte("foreign"), 0o600); err != nil {
		t.Fatal("create t020 replacement socket path failed")
	}
	err := h.close()
	if !errors.Is(err, testcontrol.ErrControlFault) {
		t.Fatal("t020 socket cleanup failure did not return a control fault")
	}
	if _, ok := h.obs.FinalSnapshot(); ok {
		t.Fatal("t020 socket cleanup failure produced a final snapshot")
	}
}
