package app

import (
	"context"
	"errors"
	"strconv"

	"payment-hub/internal/mock"
	"payment-hub/internal/payment"
	"payment-hub/internal/postgres"
	"payment-hub/internal/testcontrol"
)

const (
	testModeEnv        = "PAYMENT_HUB_TEST_MODE"
	testModeSocketEnv  = "PAYMENT_HUB_TEST_CONTROL_SOCKET"
	testModeRunEnv     = "PAYMENT_HUB_TEST_RUN_ID"
	testModeEpochEnv   = "PAYMENT_HUB_TEST_OBSERVER_EPOCH"
	testModeGenEnv     = "PAYMENT_HUB_TEST_GENERATION"
	testModeVersionEnv = "PAYMENT_HUB_TEST_CONTROL_VERSION"
)

var (
	// ErrTestModeConfig is the fixed test-mode configuration failure.
	ErrTestModeConfig = errors.New("app: invalid test mode configuration")
	// ErrTestModeDisabled is returned when a disabled TestMode is used.
	ErrTestModeDisabled = errors.New("app: test mode disabled")
)

// TestModeConfig is the parsed test-mode switch and observer session.
type TestModeConfig struct {
	Enabled bool
	// Version is the explicit protocol opt-in. Zero keeps the legacy v1
	// boundary; v2 is required for query-control.
	Version int
	Session testcontrol.Session

	// lookup is retained only so OpenTestMode can read the explicit version
	// variable after mode has already been enabled. It is never serialized.
	lookup func(string) (string, bool)
}

// TestMode owns one private test-control Client and its consumer adapters.
// It is only constructed when test mode is enabled.
type TestMode struct {
	client        *testcontrol.Client
	version       int
	mockHooks     *mock.Hooks
	postgresHooks *postgres.Hooks
}

// LoadTestModeConfig reads only the documented test-mode variables. An unset
// mode or explicit off reads no other test configuration and performs no I/O.
func LoadTestModeConfig(lookup func(string) (string, bool)) (TestModeConfig, error) {
	if lookup == nil {
		return TestModeConfig{}, ErrTestModeConfig
	}
	mode, ok := lookup(testModeEnv)
	if !ok {
		return TestModeConfig{}, nil
	}
	switch mode {
	case "off":
		return TestModeConfig{}, nil
	case "on":
	default:
		return TestModeConfig{}, ErrTestModeConfig
	}

	socket, ok := lookup(testModeSocketEnv)
	if !ok || socket == "" {
		return TestModeConfig{}, ErrTestModeConfig
	}
	runID, ok := lookup(testModeRunEnv)
	if !ok || !validTestModeLowerHex32(runID) {
		return TestModeConfig{}, ErrTestModeConfig
	}
	epoch, ok := lookup(testModeEpochEnv)
	if !ok || !validTestModeLowerHex32(epoch) {
		return TestModeConfig{}, ErrTestModeConfig
	}
	generationText, ok := lookup(testModeGenEnv)
	if !ok {
		return TestModeConfig{}, ErrTestModeConfig
	}
	generation, ok := parseTestModeGeneration(generationText)
	if !ok {
		return TestModeConfig{}, ErrTestModeConfig
	}
	return TestModeConfig{
		Enabled: true,
		Session: testcontrol.Session{
			Socket:     socket,
			RunID:      runID,
			Epoch:      epoch,
			Generation: generation,
		},
		lookup: lookup,
	}, nil
}

// LoadTestModeControlVersion reads only the explicit protocol opt-in variable.
// It is called only after test mode is enabled, so a version value can never
// enable test mode by itself. An unset value keeps the zero-value v1 default.
func LoadTestModeControlVersion(lookup func(string) (string, bool)) (int, error) {
	if lookup == nil {
		return 0, ErrTestModeConfig
	}
	value, ok := lookup(testModeVersionEnv)
	if !ok {
		return 0, nil
	}
	switch value {
	case "1":
		return testcontrol.Version, nil
	case "2":
		return testcontrol.Version2, nil
	default:
		return 0, ErrTestModeConfig
	}
}

// OpenTestMode validates an enabled configuration and performs the private UDS
// Begin handshake. Disabled configuration returns a nil TestMode with no dial.
func OpenTestMode(ctx context.Context, cfg TestModeConfig) (*TestMode, error) {
	if !cfg.Enabled {
		return nil, nil
	}
	if cfg.Session.Socket == "" || !validTestModeLowerHex32(cfg.Session.RunID) ||
		!validTestModeLowerHex32(cfg.Session.Epoch) || cfg.Session.Generation == 0 {
		return nil, ErrTestModeConfig
	}
	version := cfg.Version
	if version == 0 && cfg.lookup != nil {
		loaded, err := LoadTestModeControlVersion(cfg.lookup)
		if err != nil {
			return nil, ErrTestModeConfig
		}
		version = loaded
	}
	if version == 0 {
		version = testcontrol.Version
	}
	if version != testcontrol.Version && version != testcontrol.Version2 {
		return nil, ErrTestModeConfig
	}
	session := cfg.Session
	session.Version = version
	client, err := testcontrol.Dial(ctx, session)
	if err != nil {
		if client != nil {
			_ = client.Close()
		}
		return nil, testcontrol.ErrControlFault
	}
	return &TestMode{
		client:        client,
		version:       version,
		mockHooks:     mock.NewHooks(&testModeMockControl{client: client, version: version}),
		postgresHooks: postgres.NewHooks(&testModePostgresControl{client: client}),
	}, nil
}

// MockHooks returns nil when test mode is disabled or the receiver is nil.
func (tm *TestMode) MockHooks() *mock.Hooks {
	if tm == nil {
		return nil
	}
	return tm.mockHooks
}

// PostgresHooks returns nil when test mode is disabled or the receiver is nil.
func (tm *TestMode) PostgresHooks() *postgres.Hooks {
	if tm == nil {
		return nil
	}
	return tm.postgresHooks
}

// Seal closes the private observer window. A disabled or nil TestMode returns
// ErrTestModeDisabled with a zero Receipt.
func (tm *TestMode) Seal(ctx context.Context) (testcontrol.Receipt, error) {
	if tm == nil || tm.client == nil {
		return testcontrol.Receipt{}, ErrTestModeDisabled
	}
	receipt, err := tm.client.Seal(ctx)
	if err != nil {
		return testcontrol.Receipt{}, testcontrol.ErrControlFault
	}
	return receipt, nil
}

// Close releases owned test-mode resources without sealing. It is nil-safe.
func (tm *TestMode) Close() error {
	if tm == nil || tm.client == nil {
		return nil
	}
	return tm.client.Close()
}

type testModeMockControl struct {
	client  *testcontrol.Client
	version int
}

func (control *testModeMockControl) PaymentEntry(ctx context.Context, order payment.PaymentOrderID) error {
	if control == nil || control.client == nil {
		return nil
	}
	return mapTestModeMockError(control.client.Enter(ctx, testcontrol.KindPayment, string(order)), false)
}

// PaymentEntryResult records one payment entry through the new client API,
// copies the optional result directive into the closed payment.Result type, and
// returns a callback that sends the actual committed result back through the
// same single-use token. No directive still sends the bare legacy reach.
func (control *testModeMockControl) PaymentEntryResult(ctx context.Context, order payment.PaymentOrderID) (mock.PaymentEntryOutcome, error) {
	if control == nil || control.client == nil {
		return mock.PaymentEntryOutcome{}, mock.ErrControlFault
	}
	client := control.client
	token, err := client.PaymentEntry(ctx, string(order))
	if err != nil {
		return mock.PaymentEntryOutcome{}, mock.ErrControlFault
	}

	var result *payment.Result
	if directive, ok := token.Directive(); ok {
		mapped, valid := testModePaymentResult(directive)
		if !valid {
			return mock.PaymentEntryOutcome{}, mock.ErrControlFault
		}
		result = &mapped
	}

	return mock.PaymentEntryOutcome{
		Result: result,
		OnCommitted: func(ctx context.Context, actual payment.Result) error {
			applied, valid := testModeAppliedDirective(actual)
			if !valid {
				return mock.ErrControlFault
			}
			if err := client.MockCommitted(ctx, token, applied); err != nil {
				if errors.Is(err, testcontrol.ErrReplyHidden) {
					return mock.ErrReplyHidden
				}
				return mock.ErrControlFault
			}
			return nil
		},
	}, nil
}

// QueryEntryResult records one query entry. In v2 it uses the private
// QueryCall token and returns the selected scenario plus the exactly-once
// applied callback. In v1 it keeps the legacy Enter observation with a no-op
// applied callback.
func (control *testModeMockControl) QueryEntryResult(ctx context.Context, order payment.PaymentOrderID) (mock.QueryEntryOutcome, error) {
	if control == nil || control.client == nil {
		return mock.QueryEntryOutcome{}, mock.ErrControlFault
	}
	if control.version != testcontrol.Version2 {
		if err := control.client.Enter(ctx, testcontrol.KindQuery, string(order)); err != nil {
			return mock.QueryEntryOutcome{}, mock.ErrControlFault
		}
		return mock.QueryEntryOutcome{
			Scenario:  mock.QueryScenarioNormal,
			OnApplied: func(context.Context, mock.QueryOutcome) error { return nil },
		}, nil
	}

	client := control.client
	token, err := client.QueryEntry(ctx, string(order))
	if err != nil {
		return mock.QueryEntryOutcome{}, mock.ErrControlFault
	}
	scenario := mock.QueryScenarioNormal
	if directive, ok := token.Directive(); ok {
		mapped, valid := testModeQueryScenario(directive)
		if !valid {
			return mock.QueryEntryOutcome{}, mock.ErrControlFault
		}
		scenario = mapped
	}
	return mock.QueryEntryOutcome{
		Scenario: scenario,
		OnApplied: func(ctx context.Context, actual mock.QueryOutcome) error {
			mapped, valid := testModeQueryOutcome(actual)
			if !valid {
				return mock.ErrControlFault
			}
			if err := client.QueryApplied(ctx, token, mapped); err != nil {
				return mock.ErrControlFault
			}
			return nil
		},
	}, nil
}

func testModeQueryScenario(value testcontrol.QueryScenario) (mock.QueryScenario, bool) {
	switch value {
	case testcontrol.QueryScenarioNormal:
		return mock.QueryScenarioNormal, true
	case testcontrol.QueryScenarioFailure:
		return mock.QueryScenarioFailure, true
	case testcontrol.QueryScenarioSubtimeout:
		return mock.QueryScenarioSubtimeout, true
	case testcontrol.QueryScenarioNotVisible:
		return mock.QueryScenarioNotVisible, true
	case testcontrol.QueryScenarioMismatchReference:
		return mock.QueryScenarioMismatchReference, true
	case testcontrol.QueryScenarioMismatchAmount:
		return mock.QueryScenarioMismatchAmount, true
	case testcontrol.QueryScenarioMismatchCurrency:
		return mock.QueryScenarioMismatchCurrency, true
	case testcontrol.QueryScenarioMismatchTransaction:
		return mock.QueryScenarioMismatchTransaction, true
	default:
		return "", false
	}
}

func testModeQueryOutcome(value mock.QueryOutcome) (testcontrol.QueryOutcome, bool) {
	switch value {
	case mock.QueryOutcomeFound:
		return testcontrol.QueryOutcomeFound, true
	case mock.QueryOutcomeNotFound:
		return testcontrol.QueryOutcomeNotFound, true
	case mock.QueryOutcomeUnavailable:
		return testcontrol.QueryOutcomeUnavailable, true
	case mock.QueryOutcomeSubtimeout:
		return testcontrol.QueryOutcomeSubtimeout, true
	case mock.QueryOutcomeMismatchApplied:
		return testcontrol.QueryOutcomeMismatchApplied, true
	default:
		return "", false
	}
}

func testModePaymentResult(directive testcontrol.Directive) (payment.Result, bool) {
	switch directive {
	case testcontrol.DirectiveSucceeded:
		return payment.ResultSucceeded, true
	case testcontrol.DirectiveFailed:
		return payment.ResultFailed, true
	default:
		return "", false
	}
}

func testModeAppliedDirective(result payment.Result) (testcontrol.Directive, bool) {
	switch result {
	case payment.ResultSucceeded:
		return testcontrol.DirectiveSucceeded, true
	case payment.ResultFailed:
		return testcontrol.DirectiveFailed, true
	default:
		return "", false
	}
}

func (control *testModeMockControl) QueryEntry(ctx context.Context, order payment.PaymentOrderID) error {
	if control == nil || control.client == nil {
		return nil
	}
	return mapTestModeMockError(control.client.Enter(ctx, testcontrol.KindQuery, string(order)), false)
}

func (control *testModeMockControl) Committed(ctx context.Context, order payment.PaymentOrderID) error {
	if control == nil || control.client == nil {
		return nil
	}
	return mapTestModeMockError(control.client.Reach(ctx, testcontrol.PointMockCommitted, string(order)), true)
}

func mapTestModeMockError(err error, allowReplyHidden bool) error {
	if err == nil {
		return nil
	}
	if allowReplyHidden && err == testcontrol.ErrReplyHidden {
		return mock.ErrReplyHidden
	}
	return mock.ErrControlFault
}

type testModePostgresControl struct {
	client *testcontrol.Client
}

func (control *testModePostgresControl) Registered(ctx context.Context, order payment.PaymentOrderID) error {
	if control == nil || control.client == nil {
		return nil
	}
	return mapTestModePostgresError(control.client.Reach(ctx, testcontrol.PointRegistered, string(order)), true)
}

func (control *testModePostgresControl) SavingResult(ctx context.Context, order payment.PaymentOrderID) error {
	if control == nil || control.client == nil {
		return nil
	}
	return mapTestModePostgresError(control.client.Reach(ctx, testcontrol.PointSavingResult, string(order)), false)
}

func (control *testModePostgresControl) ResultCommitted(ctx context.Context, order payment.PaymentOrderID) error {
	if control == nil || control.client == nil {
		return nil
	}
	return mapTestModePostgresError(control.client.Reach(ctx, testcontrol.PointResultCommitted, string(order)), true)
}

func mapTestModePostgresError(err error, allowReplyHidden bool) error {
	if err == nil {
		return nil
	}
	if allowReplyHidden && err == testcontrol.ErrReplyHidden {
		return postgres.ErrReplyHidden
	}
	return postgres.ErrControlFault
}

func validTestModeLowerHex32(value string) bool {
	if len(value) != 32 {
		return false
	}
	for i := 0; i < len(value); i++ {
		c := value[i]
		if (c < '0' || c > '9') && (c < 'a' || c > 'f') {
			return false
		}
	}
	return true
}

func parseTestModeGeneration(value string) (uint64, bool) {
	if value == "" {
		return 0, false
	}
	generation, err := strconv.ParseUint(value, 10, 64)
	if err != nil || generation == 0 {
		return 0, false
	}
	if strconv.FormatUint(generation, 10) != value {
		return 0, false
	}
	return generation, true
}
