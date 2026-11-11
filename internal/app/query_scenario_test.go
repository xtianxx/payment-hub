package app

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"payment-hub/internal/mock"
	"payment-hub/internal/payment"
	"payment-hub/internal/testcontrol"
	"payment-hub/tests/harness"
)

// TestQueryScenarioAdapter freezes the app-private v2 query adapter: config
// version opt-in, scenario directive copy, and the exactly-once applied proof.
// It never opens PostgreSQL, business HTTP, the launcher, or the network.

const queryAdapterVersionEnv = "PAYMENT_HUB_TEST_CONTROL_VERSION"

func queryAdapterStartObserver(t *testing.T, version int, generation uint64) (*harness.Observer, string) {
	t.Helper()
	dir, err := os.MkdirTemp("", "phtm-query-")
	if err != nil {
		t.Fatal("create query adapter observer directory")
	}
	if err := os.Chmod(dir, 0o700); err != nil {
		_ = os.RemoveAll(dir)
		t.Fatal("secure query adapter observer directory")
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(dir); err != nil {
			t.Errorf("remove query adapter observer directory")
		}
	})
	socket := filepath.Join(dir, "s")
	ctx, cancel := context.WithTimeout(context.Background(), testmodeTestBudget)
	defer cancel()
	obs, err := harness.StartObserver(ctx, harness.ObserverConfig{
		Socket:         socket,
		RunID:          testmodeTestRunID,
		Epoch:          testmodeTestEpoch,
		BarrierTimeout: testmodeTestBudget,
		Version:        version,
	})
	if err != nil {
		t.Fatal("start real observer for query adapter")
	}
	t.Cleanup(func() {
		closeCtx, closeCancel := context.WithTimeout(context.Background(), testmodeTestBudget)
		defer closeCancel()
		if err := obs.Close(closeCtx); err != nil {
			t.Error("close query adapter observer failed")
		}
	})
	if err := obs.Expect(generation); err != nil {
		t.Fatal("expect query adapter generation")
	}
	return obs, socket
}

func queryAdapterOpen(t *testing.T, socket string, version int, generation uint64) *TestMode {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), testmodeTestBudget)
	defer cancel()
	tm, err := OpenTestMode(ctx, TestModeConfig{
		Enabled: true,
		Version: version,
		Session: testcontrol.Session{
			Socket:     socket,
			RunID:      testmodeTestRunID,
			Epoch:      testmodeTestEpoch,
			Generation: generation,
		},
	})
	if err != nil {
		t.Fatal("open query adapter test mode failed")
	}
	t.Cleanup(func() {
		testmodeTestCloseOK(t, tm)
	})
	return tm
}

func queryAdapterOutcome(t *testing.T, tm *TestMode, order string) (mock.QueryEntryOutcome, error) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), testmodeTestBudget)
	defer cancel()
	return tm.MockHooks().QueryEntryResult(ctx, payment.PaymentOrderID(order))
}

func queryAdapterSeal(t *testing.T, tm *TestMode) testcontrol.Receipt {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), testmodeTestBudget)
	defer cancel()
	receipt, err := tm.Seal(ctx)
	if err != nil {
		t.Fatal("query adapter seal failed")
	}
	return receipt
}

func TestQueryScenarioAdapter(t *testing.T) {
	t.Run("version_config_is_explicit_and_off_reads_only_mode", func(t *testing.T) {
		env := &testmodeTestEnv{values: map[string]string{queryAdapterVersionEnv: "2"}}
		version, err := LoadTestModeControlVersion(env.lookup)
		if err != nil || version != 2 {
			t.Fatal("valid version 2 config was not preserved")
		}
		testmodeTestAssertAsked(t, env.asked, queryAdapterVersionEnv)

		env = &testmodeTestEnv{values: map[string]string{}}
		version, err = LoadTestModeControlVersion(env.lookup)
		if err != nil || version != 0 {
			t.Fatal("unset version did not keep the zero-value v1 default")
		}

		env = &testmodeTestEnv{values: map[string]string{queryAdapterVersionEnv: "3"}}
		if _, err := LoadTestModeControlVersion(env.lookup); !errors.Is(err, ErrTestModeConfig) {
			t.Fatal("unsupported protocol version was accepted")
		}

		off := map[string]string{testmodeTestModeEnv: "off", queryAdapterVersionEnv: "2"}
		cfg, err, asked := testmodeTestLoad(t, off)
		if err != nil || cfg.Enabled || cfg.Version != 0 {
			t.Fatal("off mode did not stay disabled")
		}
		testmodeTestAssertAsked(t, asked, testmodeTestModeEnv)
	})

	t.Run("v2_hold_scenario_apply_and_seal", func(t *testing.T) {
		obs, socket := queryAdapterStartObserver(t, testcontrol.Version2, 1)
		tm := queryAdapterOpen(t, socket, testcontrol.Version2, 1)
		hold, err := obs.HoldQueryEntry(1)
		if err != nil {
			t.Fatal("hold query entry failed")
		}
		type result struct {
			outcome mock.QueryEntryOutcome
			err     error
		}
		done := make(chan result, 1)
		go func() {
			outcome, err := queryAdapterOutcome(t, tm, testmodeTestOrderA)
			done <- result{outcome: outcome, err: err}
		}()
		ctx, cancel := context.WithTimeout(context.Background(), testmodeTestBudget)
		defer cancel()
		if _, err := hold.Wait(ctx); err != nil {
			t.Fatal("query hold wait failed")
		}
		if err := hold.Release(testcontrol.QueryScenarioMismatchAmount); err != nil {
			t.Fatal("query hold release failed")
		}
		select {
		case res := <-done:
			if res.err != nil {
				t.Fatal("query adapter entry failed")
			}
			if res.outcome.Scenario != mock.QueryScenarioMismatchAmount {
				t.Fatal("query adapter did not copy the scenario directive")
			}
			if res.outcome.OnApplied == nil {
				t.Fatal("query adapter returned a nil applied callback")
			}
			if err := res.outcome.OnApplied(ctx, mock.QueryOutcomeMismatchApplied); err != nil {
				t.Fatal("query adapter applied proof failed")
			}
		case <-time.After(testmodeTestBudget):
			t.Fatal("query adapter entry did not return after release")
		}
		receipt := queryAdapterSeal(t, tm)
		if receipt.LastSeq != 2 {
			t.Fatal("query adapter receipt sequence mismatch")
		}
		if _, counts, ok := obs.SealedGeneration(1); !ok || counts != (harness.Counts{Query: 1}) {
			t.Fatal("query adapter sealed generation counts mismatch")
		}
	})

	t.Run("v2_normal_query_requires_apply", func(t *testing.T) {
		obs, socket := queryAdapterStartObserver(t, testcontrol.Version2, 1)
		tm := queryAdapterOpen(t, socket, testcontrol.Version2, 1)
		outcome, err := queryAdapterOutcome(t, tm, testmodeTestOrderA)
		if err != nil {
			t.Fatal("normal query adapter entry failed")
		}
		if outcome.Scenario != mock.QueryScenarioNormal {
			t.Fatal("normal query adapter scenario mismatch")
		}
		ctx, cancel := context.WithTimeout(context.Background(), testmodeTestBudget)
		defer cancel()
		if _, err := tm.Seal(ctx); !errors.Is(err, testcontrol.ErrControlFault) {
			t.Fatal("query adapter seal accepted an unapplied normal query")
		}
		if _, _, ok := obs.SealedGeneration(1); ok {
			t.Fatal("unapplied normal query generation reported sealed")
		}
	})

	t.Run("v1_adapter_keeps_legacy_query_entry", func(t *testing.T) {
		obs, socket := queryAdapterStartObserver(t, testcontrol.Version, 1)
		tm := queryAdapterOpen(t, socket, 0, 1)
		outcome, err := queryAdapterOutcome(t, tm, testmodeTestOrderA)
		if err != nil {
			t.Fatal("v1 query adapter entry failed")
		}
		if outcome.Scenario != mock.QueryScenarioNormal || outcome.OnApplied == nil {
			t.Fatal("v1 query adapter did not use the legacy normal path")
		}
		if err := outcome.OnApplied(context.Background(), mock.QueryOutcomeUnavailable); err != nil {
			t.Fatal("v1 query adapter no-op applied failed")
		}
		queryAdapterSeal(t, tm)
		if _, counts, ok := obs.SealedGeneration(1); !ok || counts != (harness.Counts{Query: 1}) {
			t.Fatal("v1 legacy query generation counts mismatch")
		}
	})

	t.Run("v2_without_directive_is_still_normal_but_proven", func(t *testing.T) {
		obs, socket := queryAdapterStartObserver(t, testcontrol.Version2, 1)
		tm := queryAdapterOpen(t, socket, testcontrol.Version2, 1)
		outcome, err := queryAdapterOutcome(t, tm, testmodeTestOrderA)
		if err != nil {
			t.Fatal("normal v2 query adapter entry failed")
		}
		ctx, cancel := context.WithTimeout(context.Background(), testmodeTestBudget)
		defer cancel()
		if err := outcome.OnApplied(ctx, mock.QueryOutcomeFound); err != nil {
			t.Fatal("normal v2 query adapter applied failed")
		}
		queryAdapterSeal(t, tm)
		if _, counts, ok := obs.SealedGeneration(1); !ok || counts != (harness.Counts{Query: 1}) {
			t.Fatal("normal v2 query generation counts mismatch")
		}
	})
}
