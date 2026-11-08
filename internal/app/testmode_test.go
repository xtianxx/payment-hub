package app

// These tests freeze the T019 test-mode loading and adapter surface. They use
// only obviously fake values. Failure messages contain static case names and
// fixed sentinels, never configuration values, paths, frames, or raw errors.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"payment-hub/internal/mock"
	"payment-hub/internal/payment"
	"payment-hub/internal/postgres"
	"payment-hub/internal/testcontrol"
	"payment-hub/tests/harness"
)

const (
	testmodeTestClientEnv     = "PAYMENT_HUB_CLIENT_ID"
	testmodeTestAPIKeyEnv     = "PAYMENT_HUB_API_KEY"
	testmodeTestModeEnv       = "PAYMENT_HUB_TEST_MODE"
	testmodeTestSocketEnv     = "PAYMENT_HUB_TEST_CONTROL_SOCKET"
	testmodeTestRunEnv        = "PAYMENT_HUB_TEST_RUN_ID"
	testmodeTestEpochEnv      = "PAYMENT_HUB_TEST_OBSERVER_EPOCH"
	testmodeTestGenerationEnv = "PAYMENT_HUB_TEST_GENERATION"

	testmodeTestRunID = "0123456789abcdef0123456789abcdef"
	testmodeTestEpoch = "fedcba9876543210fedcba9876543210"

	testmodeTestOrderA = "123e4567-e89b-42d3-a456-426614174000"
	testmodeTestOrderB = "223e4567-e89b-42d3-b456-426614174001"

	testmodeTestClientID = "fake-client-TM_01"
	testmodeTestAPIKey   = "FAKE-demo_key.9~a+/b=="

	testmodeTestCanary = "FAKE_T019_TESTMODE_CANARY"

	testmodeTestBudget = 5 * time.Second
)

type testmodeTestEnv struct {
	values map[string]string
	asked  []string
}

func (e *testmodeTestEnv) lookup(name string) (string, bool) {
	e.asked = append(e.asked, name)
	value, ok := e.values[name]
	return value, ok
}

func testmodeTestLoad(t *testing.T, values map[string]string) (TestModeConfig, error, []string) {
	t.Helper()
	env := &testmodeTestEnv{values: values}
	cfg, err := LoadTestModeConfig(env.lookup)
	return cfg, err, env.asked
}

func testmodeTestValidValues(socket string) map[string]string {
	return map[string]string{
		testmodeTestModeEnv:       "on",
		testmodeTestSocketEnv:     socket,
		testmodeTestRunEnv:        testmodeTestRunID,
		testmodeTestEpochEnv:      testmodeTestEpoch,
		testmodeTestGenerationEnv: "42",
	}
}

func testmodeTestAssertAsked(t *testing.T, asked []string, want ...string) {
	t.Helper()
	seen := make(map[string]bool, len(asked))
	for _, name := range asked {
		seen[name] = true
	}
	for _, name := range want {
		if !seen[name] {
			t.Fatal("test mode loader did not read an expected environment name")
		}
	}
	if len(seen) != len(want) {
		t.Fatal("test mode loader read an unexpected environment name")
	}
}

func testmodeTestRequireConfigError(t *testing.T, err error) {
	t.Helper()
	if err == nil {
		t.Fatal("expected a fixed test mode configuration error, got success")
	}
	if !errors.Is(err, ErrTestModeConfig) {
		t.Fatal("expected a fixed test mode configuration error")
	}
	if strings.Contains(err.Error(), testmodeTestCanary) {
		t.Fatal("test mode configuration error echoed a rejected value")
	}
}

func testmodeTestRequireControlFault(t *testing.T, err error) {
	t.Helper()
	if err == nil {
		t.Fatal("expected a fixed test control fault, got success")
	}
	if !errors.Is(err, testcontrol.ErrControlFault) {
		t.Fatal("expected a fixed test control fault")
	}
	if strings.Contains(err.Error(), testmodeTestCanary) {
		t.Fatal("test control fault echoed a rejected value")
	}
}

func testmodeTestStartObserver(t *testing.T, generation uint64) (*harness.Observer, string) {
	t.Helper()
	dir, err := os.MkdirTemp("", "phtm-")
	if err != nil {
		t.Fatal("create test mode observer directory")
	}
	if err := os.Chmod(dir, 0o700); err != nil {
		t.Fatal("secure test mode observer directory")
	}
	t.Cleanup(func() {
		if err := os.RemoveAll(dir); err != nil {
			t.Errorf("remove test mode observer directory")
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
	})
	if err != nil {
		t.Fatal("start real observer for test mode")
	}
	if obs == nil {
		t.Fatal("start real observer returned nil")
	}
	t.Cleanup(func() {
		testmodeTestCloseObserverOK(t, obs)
	})
	if err := obs.Expect(generation); err != nil {
		t.Fatal("expect test mode generation")
	}
	return obs, socket
}

func testmodeTestCloseOK(t *testing.T, tm *TestMode) {
	t.Helper()
	if tm == nil {
		return
	}
	if err := tm.Close(); err != nil {
		t.Error("close test mode failed")
	}
}

func testmodeTestCloseObserverOK(t *testing.T, obs *harness.Observer) {
	t.Helper()
	closeCtx, cancel := context.WithTimeout(context.Background(), testmodeTestBudget)
	defer cancel()
	if err := obs.Close(closeCtx); err != nil {
		t.Error("close test mode observer failed")
	}
}

func testmodeTestArmAndCall(t *testing.T, obs *harness.Observer, generation uint64, point testcontrol.Point, orderID string, action testcontrol.Action, call func(context.Context) error) error {
	t.Helper()
	barrier, err := obs.Arm(generation, point, orderID)
	if err != nil {
		t.Fatal("arm test mode barrier failed")
	}
	ctx, cancel := context.WithTimeout(context.Background(), testmodeTestBudget)
	defer cancel()
	done := make(chan error, 1)
	go func() {
		done <- call(ctx)
	}()
	if _, err := barrier.Wait(ctx); err != nil {
		t.Fatal("test mode barrier wait failed")
	}
	if err := barrier.Release(action); err != nil {
		t.Fatal("test mode barrier release failed")
	}
	select {
	case err := <-done:
		return err
	case <-time.After(testmodeTestBudget):
		t.Fatal("test mode hook did not return after barrier release")
		return nil
	}
}

func TestLoadConfigIdentityContractUnchanged(t *testing.T) {
	values := map[string]string{
		testmodeTestClientEnv: testmodeTestClientID,
		testmodeTestAPIKeyEnv: testmodeTestAPIKey,
		testmodeTestModeEnv:   "on",
		testmodeTestSocketEnv: filepath.Join(os.TempDir(), "phtm-ignored.sock"),
	}
	env := &testmodeTestEnv{values: values}
	cfg, err := LoadConfig(env.lookup)
	if err != nil {
		t.Fatal("LoadConfig rejected valid fake identity configuration")
	}
	if cfg.ClientID != testmodeTestClientID || cfg.APIKey != testmodeTestAPIKey {
		t.Fatal("LoadConfig did not preserve the identity configuration")
	}
	testmodeTestAssertAsked(t, env.asked, testmodeTestClientEnv, testmodeTestAPIKeyEnv)
}

func TestLoadTestModeConfigOffReadsOnlyMode(t *testing.T) {
	cases := []struct {
		name   string
		values map[string]string
	}{
		{
			name:   "unset",
			values: map[string]string{},
		},
		{
			name: "explicit off",
			values: map[string]string{
				testmodeTestModeEnv: "off",
			},
		},
		{
			name: "off ignores remaining values",
			values: map[string]string{
				testmodeTestModeEnv:       "off",
				testmodeTestSocketEnv:     filepath.Join(testmodeTestCanary, "s"),
				testmodeTestRunEnv:        "not-a-run-id",
				testmodeTestEpochEnv:      "not-an-epoch",
				testmodeTestGenerationEnv: "not-a-generation",
			},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg, err, asked := testmodeTestLoad(t, tc.values)
			if err != nil {
				t.Fatal("off test mode configuration was rejected")
			}
			if cfg.Enabled {
				t.Fatal("off test mode configuration was reported enabled")
			}
			if cfg.Session != (testcontrol.Session{}) {
				t.Fatal("off test mode configuration populated a session")
			}
			testmodeTestAssertAsked(t, asked, testmodeTestModeEnv)
		})
	}
}

func TestLoadTestModeConfigRejectsInvalidMode(t *testing.T) {
	modes := []string{"", "true", "ON", "On", " on", "on ", "auto", "1", "on\n"}

	for _, mode := range modes {
		t.Run("mode "+strconv.Quote(mode), func(t *testing.T) {
			values := testmodeTestValidValues(filepath.Join(os.TempDir(), "phtm.sock"))
			values[testmodeTestModeEnv] = mode
			_, err, _ := testmodeTestLoad(t, values)
			testmodeTestRequireConfigError(t, err)
		})
	}
}

func TestLoadTestModeConfigOnParsesStrictly(t *testing.T) {
	t.Run("valid values are preserved", func(t *testing.T) {
		socket := filepath.Join(os.TempDir(), "phtm test.sock")
		cfg, err, asked := testmodeTestLoad(t, testmodeTestValidValues(socket))
		if err != nil {
			t.Fatal("valid on test mode configuration was rejected")
		}
		if !cfg.Enabled {
			t.Fatal("on test mode configuration was not enabled")
		}
		if cfg.Session.Socket != socket || cfg.Session.RunID != testmodeTestRunID ||
			cfg.Session.Epoch != testmodeTestEpoch || cfg.Session.Generation != 42 {
			t.Fatal("on test mode configuration was not preserved exactly")
		}
		testmodeTestAssertAsked(t, asked,
			testmodeTestModeEnv, testmodeTestSocketEnv, testmodeTestRunEnv,
			testmodeTestEpochEnv, testmodeTestGenerationEnv)
	})

	t.Run("maximum generation", func(t *testing.T) {
		values := testmodeTestValidValues(filepath.Join(os.TempDir(), "phtm.sock"))
		values[testmodeTestGenerationEnv] = strconv.FormatUint(^uint64(0), 10)
		cfg, err, _ := testmodeTestLoad(t, values)
		if err != nil {
			t.Fatal("maximum canonical generation was rejected")
		}
		if cfg.Session.Generation != ^uint64(0) {
			t.Fatal("maximum canonical generation was not preserved")
		}
	})

	t.Run("nil lookup", func(t *testing.T) {
		_, err := LoadTestModeConfig(nil)
		testmodeTestRequireConfigError(t, err)
	})

	t.Run("missing values have no defaults", func(t *testing.T) {
		for _, name := range []string{
			testmodeTestSocketEnv, testmodeTestRunEnv,
			testmodeTestEpochEnv, testmodeTestGenerationEnv,
		} {
			values := testmodeTestValidValues(filepath.Join(os.TempDir(), "phtm.sock"))
			delete(values, name)
			_, err, _ := testmodeTestLoad(t, values)
			testmodeTestRequireConfigError(t, err)
		}
	})

	t.Run("empty values have no defaults", func(t *testing.T) {
		for _, name := range []string{
			testmodeTestSocketEnv, testmodeTestRunEnv,
			testmodeTestEpochEnv, testmodeTestGenerationEnv,
		} {
			values := testmodeTestValidValues(filepath.Join(os.TempDir(), "phtm.sock"))
			values[name] = ""
			_, err, _ := testmodeTestLoad(t, values)
			testmodeTestRequireConfigError(t, err)
		}
	})

	t.Run("generation is canonical positive decimal", func(t *testing.T) {
		invalid := []string{
			"", "0", "00", "01", "+1", "-1", " 1", "1 ", "1\n",
			"0x10", "1.0", "1e3", "١٢",
			"18446744073709551616", "99999999999999999999999999",
			testmodeTestCanary,
		}
		for _, generation := range invalid {
			values := testmodeTestValidValues(filepath.Join(os.TempDir(), "phtm.sock"))
			values[testmodeTestGenerationEnv] = generation
			_, err, _ := testmodeTestLoad(t, values)
			testmodeTestRequireConfigError(t, err)
		}
	})

	t.Run("run and epoch are lowercase hex 32", func(t *testing.T) {
		invalid := []string{
			"", "0", "0123456789abcdef0123456789abcde",
			"0123456789abcdef0123456789abcdef0",
			"0123456789ABCDEF0123456789ABCDEF",
			"0123456789abcdef0123456789abcdeg",
			"0123456789abcdef-0123456789abcde",
			" " + testmodeTestRunID,
		}
		for _, value := range invalid {
			values := testmodeTestValidValues(filepath.Join(os.TempDir(), "phtm.sock"))
			values[testmodeTestRunEnv] = value
			_, err, _ := testmodeTestLoad(t, values)
			testmodeTestRequireConfigError(t, err)

			values = testmodeTestValidValues(filepath.Join(os.TempDir(), "phtm.sock"))
			values[testmodeTestEpochEnv] = value
			_, err, _ = testmodeTestLoad(t, values)
			testmodeTestRequireConfigError(t, err)
		}
	})

	t.Run("lookup false is treated as missing", func(t *testing.T) {
		values := testmodeTestValidValues(filepath.Join(os.TempDir(), "phtm.sock"))
		lookup := func(name string) (string, bool) {
			value, ok := values[name]
			if name == testmodeTestSocketEnv {
				return value, false
			}
			return value, ok
		}
		_, err := LoadTestModeConfig(lookup)
		testmodeTestRequireConfigError(t, err)
	})
}

func TestOpenTestModeDisabledIsSafe(t *testing.T) {
	cases := []struct {
		name string
		cfg  TestModeConfig
	}{
		{name: "zero configuration", cfg: TestModeConfig{}},
		{
			name: "disabled ignores session values",
			cfg: TestModeConfig{Session: testcontrol.Session{
				Socket:     filepath.Join(testmodeTestCanary, "s"),
				RunID:      "not-a-run-id",
				Epoch:      "not-an-epoch",
				Generation: 0,
			}},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			openCtx, cancelOpen := context.WithTimeout(context.Background(), testmodeTestBudget)
			defer cancelOpen()
			tm, err := OpenTestMode(openCtx, tc.cfg)
			if err != nil {
				t.Fatal("disabled test mode open failed")
			}
			if tm.MockHooks() != nil {
				t.Fatal("disabled test mode returned non-nil mock hooks")
			}
			if tm.PostgresHooks() != nil {
				t.Fatal("disabled test mode returned non-nil postgres hooks")
			}

			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			receipt, err := tm.Seal(ctx)
			if !errors.Is(err, ErrTestModeDisabled) {
				t.Fatal("disabled test mode seal did not return the disabled sentinel")
			}
			if receipt != (testcontrol.Receipt{}) {
				t.Fatal("disabled test mode seal returned a non-zero receipt")
			}
			if err := tm.MockHooks().PaymentEntry(ctx, payment.PaymentOrderID(testmodeTestOrderA)); err != nil {
				t.Fatal("disabled mock hooks were not a safe no-op")
			}
			if err := tm.PostgresHooks().Registered(ctx, payment.PaymentOrderID(testmodeTestOrderA)); err != nil {
				t.Fatal("disabled postgres hooks were not a safe no-op")
			}
			if err := tm.Close(); err != nil {
				t.Fatal("disabled test mode close failed")
			}
			if err := tm.Close(); err != nil {
				t.Fatal("repeated disabled test mode close failed")
			}
		})
	}
}

func TestOpenTestModeRejectsBadConfigBeforeDial(t *testing.T) {
	configCases := []struct {
		name   string
		socket string
		runID  string
		epoch  string
		gen    uint64
	}{
		{name: "empty socket", socket: "", runID: testmodeTestRunID, epoch: testmodeTestEpoch, gen: 1},
		{name: "zero generation", socket: filepath.Join(os.TempDir(), "phtm-missing.sock"), runID: testmodeTestRunID, epoch: testmodeTestEpoch, gen: 0},
		{name: "invalid run", socket: filepath.Join(os.TempDir(), "phtm-missing.sock"), runID: testmodeTestCanary, epoch: testmodeTestEpoch, gen: 1},
		{name: "invalid epoch", socket: filepath.Join(os.TempDir(), "phtm-missing.sock"), runID: testmodeTestRunID, epoch: testmodeTestCanary, gen: 1},
	}

	for _, tc := range configCases {
		t.Run(tc.name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), testmodeTestBudget)
			defer cancel()
			tm, err := OpenTestMode(ctx, TestModeConfig{Enabled: true, Session: testcontrol.Session{
				Socket:     tc.socket,
				RunID:      tc.runID,
				Epoch:      tc.epoch,
				Generation: tc.gen,
			}})
			testmodeTestRequireConfigError(t, err)
			if tm != nil {
				testmodeTestCloseOK(t, tm)
			}
		})
	}

	t.Run("relative socket path", func(t *testing.T) {
		ctx, cancel := context.WithTimeout(context.Background(), testmodeTestBudget)
		defer cancel()
		tm, err := OpenTestMode(ctx, TestModeConfig{Enabled: true, Session: testcontrol.Session{
			Socket:     testmodeTestCanary + ".sock",
			RunID:      testmodeTestRunID,
			Epoch:      testmodeTestEpoch,
			Generation: 1,
		}})
		testmodeTestRequireControlFault(t, err)
		if tm != nil {
			testmodeTestCloseOK(t, tm)
		}
	})

	t.Run("missing socket path", func(t *testing.T) {
		dir, err := os.MkdirTemp("", "phtm-")
		if err != nil {
			t.Fatal("create missing socket test directory")
		}
		if err := os.Chmod(dir, 0o700); err != nil {
			t.Fatal("secure missing socket test directory")
		}
		defer func() {
			if err := os.RemoveAll(dir); err != nil {
				t.Errorf("remove missing socket test directory")
			}
		}()
		ctx, cancel := context.WithTimeout(context.Background(), testmodeTestBudget)
		defer cancel()
		tm, err := OpenTestMode(ctx, TestModeConfig{Enabled: true, Session: testcontrol.Session{
			Socket:     filepath.Join(dir, "missing"),
			RunID:      testmodeTestRunID,
			Epoch:      testmodeTestEpoch,
			Generation: 1,
		}})
		testmodeTestRequireControlFault(t, err)
		if tm != nil {
			testmodeTestCloseOK(t, tm)
		}
	})

	t.Run("regular file socket path", func(t *testing.T) {
		dir, err := os.MkdirTemp("", "phtm-")
		if err != nil {
			t.Fatal("create regular file test directory")
		}
		if err := os.Chmod(dir, 0o700); err != nil {
			t.Fatal("secure regular file test directory")
		}
		defer func() {
			if err := os.RemoveAll(dir); err != nil {
				t.Errorf("remove regular file test directory")
			}
		}()
		path := filepath.Join(dir, "regular")
		if err := os.WriteFile(path, []byte("not a socket"), 0o600); err != nil {
			t.Fatal("create regular file test socket path")
		}
		ctx, cancel := context.WithTimeout(context.Background(), testmodeTestBudget)
		defer cancel()
		tm, err := OpenTestMode(ctx, TestModeConfig{Enabled: true, Session: testcontrol.Session{
			Socket:     path,
			RunID:      testmodeTestRunID,
			Epoch:      testmodeTestEpoch,
			Generation: 1,
		}})
		testmodeTestRequireControlFault(t, err)
		if tm != nil {
			testmodeTestCloseOK(t, tm)
		}
	})
}

func TestOpenTestModeHandshakeFault(t *testing.T) {
	const generation = uint64(9)
	_, socket := testmodeTestStartObserver(t, generation)

	ctx, cancel := context.WithTimeout(context.Background(), testmodeTestBudget)
	defer cancel()
	tm, err := OpenTestMode(ctx, TestModeConfig{Enabled: true, Session: testcontrol.Session{
		Socket:     socket,
		RunID:      strings.Repeat("a", 32),
		Epoch:      testmodeTestEpoch,
		Generation: generation,
	}})
	testmodeTestRequireControlFault(t, err)
	if tm != nil {
		testmodeTestCloseOK(t, tm)
	}
}

func TestOpenTestModeHandshakeAndHooks(t *testing.T) {
	const generation = uint64(7)
	obs, socket := testmodeTestStartObserver(t, generation)

	ctx, cancel := context.WithTimeout(context.Background(), testmodeTestBudget)
	defer cancel()
	tm, err := OpenTestMode(ctx, TestModeConfig{Enabled: true, Session: testcontrol.Session{
		Socket:     socket,
		RunID:      testmodeTestRunID,
		Epoch:      testmodeTestEpoch,
		Generation: generation,
	}})
	if err != nil {
		t.Fatal("open real test mode failed")
	}
	if tm == nil {
		t.Fatal("open real test mode returned nil")
	}
	t.Cleanup(func() {
		testmodeTestCloseOK(t, tm)
	})
	mockHooks := tm.MockHooks()
	if mockHooks == nil {
		t.Fatal("enabled test mode returned nil mock hooks")
	}
	postgresHooks := tm.PostgresHooks()
	if postgresHooks == nil {
		t.Fatal("enabled test mode returned nil postgres hooks")
	}

	committedErr := testmodeTestArmAndCall(t, obs, generation, testcontrol.PointMockCommitted, testmodeTestOrderA, testcontrol.ActionReplyHidden, func(callCtx context.Context) error {
		return mockHooks.Committed(callCtx, payment.PaymentOrderID(testmodeTestOrderA))
	})
	if !errors.Is(committedErr, mock.ErrReplyHidden) {
		t.Fatal("mock committed hook did not return the controlled reply-hidden sentinel")
	}

	hookCallCtx, cancelHookCalls := context.WithTimeout(context.Background(), testmodeTestBudget)
	defer cancelHookCalls()
	if err := mockHooks.PaymentEntry(hookCallCtx, payment.PaymentOrderID(testmodeTestOrderA)); err != nil {
		t.Fatal("mock payment entry hook failed")
	}
	if err := mockHooks.PaymentEntry(hookCallCtx, payment.PaymentOrderID(testmodeTestOrderB)); err != nil {
		t.Fatal("second mock payment entry hook failed")
	}
	if err := mockHooks.QueryEntry(hookCallCtx, payment.PaymentOrderID(testmodeTestOrderA)); err != nil {
		t.Fatal("mock query entry hook failed")
	}

	registeredErr := testmodeTestArmAndCall(t, obs, generation, testcontrol.PointRegistered, testmodeTestOrderB, testcontrol.ActionReplyHidden, func(callCtx context.Context) error {
		return postgresHooks.Registered(callCtx, payment.PaymentOrderID(testmodeTestOrderB))
	})
	if !errors.Is(registeredErr, postgres.ErrReplyHidden) {
		t.Fatal("postgres registered hook did not return the controlled reply-hidden sentinel")
	}

	savingErr := testmodeTestArmAndCall(t, obs, generation, testcontrol.PointSavingResult, testmodeTestOrderB, testcontrol.ActionContinue, func(callCtx context.Context) error {
		return postgresHooks.SavingResult(callCtx, payment.PaymentOrderID(testmodeTestOrderB))
	})
	if savingErr != nil {
		t.Fatal("postgres saving result hook failed")
	}

	committedPostgresErr := testmodeTestArmAndCall(t, obs, generation, testcontrol.PointResultCommitted, testmodeTestOrderB, testcontrol.ActionReplyHidden, func(callCtx context.Context) error {
		return postgresHooks.ResultCommitted(callCtx, payment.PaymentOrderID(testmodeTestOrderB))
	})
	if !errors.Is(committedPostgresErr, postgres.ErrReplyHidden) {
		t.Fatal("postgres result committed hook did not return the controlled reply-hidden sentinel")
	}

	sealCtx, cancelSeal := context.WithTimeout(context.Background(), testmodeTestBudget)
	defer cancelSeal()
	receipt, err := tm.Seal(sealCtx)
	if err != nil {
		t.Fatal("test mode seal failed")
	}
	if receipt.LastSeq != 7 {
		t.Fatal("test mode seal did not close the locally allocated sequence")
	}
	if receipt.RunID != testmodeTestRunID || receipt.Epoch != testmodeTestEpoch || receipt.Generation != generation {
		t.Fatal("test mode seal receipt identity mismatch")
	}
	counts, err := obs.Snapshot()
	if err != nil {
		t.Fatal("observer snapshot after test mode seal failed")
	}
	if counts.Payment != 2 || counts.Query != 1 {
		t.Fatal("test mode hooks did not forward distinct payment and query calls")
	}

	// After Seal the adapter must fail closed through the consumer sentinels.
	postSealCtx, cancelPostSeal := context.WithTimeout(context.Background(), testmodeTestBudget)
	defer cancelPostSeal()
	if err := mockHooks.PaymentEntry(postSealCtx, payment.PaymentOrderID(testmodeTestOrderA)); !errors.Is(err, mock.ErrControlFault) {
		t.Fatal("mock hooks after seal did not return the consumer control fault")
	}
	if err := postgresHooks.Registered(postSealCtx, payment.PaymentOrderID(testmodeTestOrderB)); !errors.Is(err, postgres.ErrControlFault) {
		t.Fatal("postgres hooks after seal did not return the consumer control fault")
	}
	if err := tm.Close(); err != nil {
		t.Fatal("close enabled test mode failed")
	}
	closeCtx, cancelClose := context.WithTimeout(context.Background(), testmodeTestBudget)
	defer cancelClose()
	if err := obs.Close(closeCtx); err != nil {
		t.Fatal("close real test mode observer failed")
	}
}

func TestTestModeZeroValueSafe(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	check := func(t *testing.T, tm *TestMode) {
		t.Helper()
		if tm.MockHooks() != nil {
			t.Fatal("zero test mode returned non-nil mock hooks")
		}
		if tm.PostgresHooks() != nil {
			t.Fatal("zero test mode returned non-nil postgres hooks")
		}
		receipt, err := tm.Seal(ctx)
		if !errors.Is(err, ErrTestModeDisabled) {
			t.Fatal("zero test mode seal did not return the disabled sentinel")
		}
		if receipt != (testcontrol.Receipt{}) {
			t.Fatal("zero test mode seal returned a non-zero receipt")
		}
		if err := tm.MockHooks().PaymentEntry(ctx, payment.PaymentOrderID(testmodeTestOrderA)); err != nil {
			t.Fatal("zero test mode mock hooks were not a safe no-op")
		}
		if err := tm.PostgresHooks().ResultCommitted(ctx, payment.PaymentOrderID(testmodeTestOrderA)); err != nil {
			t.Fatal("zero test mode postgres hooks were not a safe no-op")
		}
		if err := tm.Close(); err != nil {
			t.Fatal("zero test mode close failed")
		}
		if err := tm.Close(); err != nil {
			t.Fatal("repeated zero test mode close failed")
		}
	}

	var nilMode *TestMode
	check(t, nilMode)
	zero := &TestMode{}
	check(t, zero)
}
