//go:build integration

package tests

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"

	"payment-hub/internal/payment"
	"payment-hub/internal/testcontrol"
	"payment-hub/tests/harness"
)

const (
	us3ReportSchema    = "payment-hub.us3.v1"
	us3ArtifactRootEnv = "PAYMENT_HUB_TEST_ARTIFACT_ROOT"
	us3ReportMaxBytes  = 256 * 1024
	us3ScenarioMax     = 32
	us3HTTPMax         = 16
	us3PGMax           = 8
	us3CallsMax        = 8
	us3RecordsMax      = 16
	us3QueriesMax      = 8
	us3FaultsMax       = 8
	us3ProofsMax       = 8
	us3BudgetsMax      = 8
	us3CommandsMax     = 8
	us3DifferencesMax  = 5
)

var us3ScenarioNames = []string{
	"submit_unknown_no_transaction",
	"submit_commit_unknown",
	"query_unknown_confirmed",
	"query_unknown_failure",
	"query_unknown_subtimeout",
	"query_unknown_not_visible",
	"query_unknown_no_mock_match",
	"query_mismatch_reference",
	"query_mismatch_amount",
	"query_mismatch_currency",
	"query_mismatch_transaction",
	"query_not_found",
	"query_confirmed_plain",
	"query_confirmed_trace_failure",
	"query_result_commit_unknown",
	"protection_inflight_failure",
	"protection_inflight_mismatch",
	"restart_registered_unpaid",
	"restart_mock_committed_reply_lost",
	"restart_unknown_no_confirm",
	"restart_unknown_then_confirmed",
	"lifecycle_cancel_before_mock",
	"lifecycle_cancel_after_mock",
	"lifecycle_exit_budget",
}

var us3ScenarioNameSet = func() map[string]bool {
	set := make(map[string]bool, len(us3ScenarioNames))
	for _, name := range us3ScenarioNames {
		set[name] = true
	}
	return set
}()

var us3ControlledTruncatedScenarios = map[string]bool{
	"restart_registered_unpaid":         true,
	"restart_mock_committed_reply_lost": true,
}

var us3QueryRequiredScenarios = map[string]bool{
	"query_unknown_confirmed":           true,
	"query_unknown_failure":             true,
	"query_unknown_subtimeout":          true,
	"query_unknown_not_visible":         true,
	"query_unknown_no_mock_match":       true,
	"query_mismatch_reference":          true,
	"query_mismatch_amount":             true,
	"query_mismatch_currency":           true,
	"query_mismatch_transaction":        true,
	"query_confirmed_trace_failure":     true,
	"query_result_commit_unknown":       true,
	"protection_inflight_failure":       true,
	"protection_inflight_mismatch":      true,
	"restart_unknown_no_confirm":        true,
	"restart_unknown_then_confirmed":    true,
	"restart_registered_unpaid":         true,
	"restart_mock_committed_reply_lost": true,
}

type us3Report struct {
	Schema    string        `json:"schema"`
	RunID     string        `json:"run_id"`
	Scenarios []us3Scenario `json:"scenarios"`
}

type us3Scenario struct {
	Name             string               `json:"name"`
	Result           string               `json:"result"`
	Generations      []uint64             `json:"generations"`
	Transport        string               `json:"transport"`
	HTTP             []us3HTTP            `json:"http"`
	PG               []us1ReportPG        `json:"pg"`
	Calls            []us3Call            `json:"calls"`
	Records          []us3Record          `json:"records"`
	Queries          []us3Query           `json:"queries"`
	Faults           []us3Fault           `json:"faults"`
	Facts            us3Facts             `json:"facts"`
	Checks           us3Checks            `json:"checks"`
	GenerationProofs []us3GenerationProof `json:"generation_proofs"`
	Joint            *us3Joint            `json:"joint,omitempty"`
	Protection       *us3Protection       `json:"protection,omitempty"`
	Save             *us3Save             `json:"save,omitempty"`
	Trace            *us3Trace            `json:"trace,omitempty"`
	Budgets          []us3Budget          `json:"budgets"`
	Cancel           *us3Cancel           `json:"cancel,omitempty"`
	Shutdown         *us3Shutdown         `json:"shutdown,omitempty"`
	Commands         []us1ReportCommand   `json:"commands"`

	// Measurement-only correlation state. These fields are never serialized:
	// the report may only contain values that were read back from the actual
	// HTTP response, PG row/record, observer application, or process.
	queryHTTP               map[uint64][]us3QueryHTTPFact
	strictQueryApplications bool
	expectedMockTransaction string
	countsBefore            us3Counts
	countsBeforeSet         bool
}

type us3QueryHTTPFact struct {
	RequestID string
	Path      string
	Confirmed bool
	Issues    []string
	Binding   us3QueryBinding
}

type us3HTTP struct {
	Kind       string   `json:"kind"`
	Generation uint64   `json:"generation"`
	Status     int      `json:"status"`
	RequestID  string   `json:"request_id"`
	PaymentID  string   `json:"payment_id"`
	Result     string   `json:"result"`
	Issues     []string `json:"issues"`
}

type us3Call struct {
	Generation   uint64 `json:"generation"`
	Exact        bool   `json:"exact"`
	PaymentCalls uint64 `json:"payment_calls"`
	QueryCalls   uint64 `json:"query_calls"`
}

type us3Counts struct {
	PaymentCalls uint64 `json:"payment_calls"`
	QueryCalls   uint64 `json:"query_calls"`
	OrderRows    uint64 `json:"order_rows"`
	MockRows     uint64 `json:"mock_rows"`
	TraceRows    uint64 `json:"trace_rows"`
}

type us3Checks struct {
	CountsBefore us3Counts `json:"counts_before"`
	CountsAfter  us3Counts `json:"counts_after"`
	CountsExact  bool      `json:"counts_exact"`
	StableFacts  bool      `json:"stable_facts"`
}

type us3Evidence struct {
	PlatformReferenceID *string  `json:"platform_reference_id,omitempty"`
	MockTransactionID   *string  `json:"mock_transaction_id,omitempty"`
	Amount              *int64   `json:"amount,omitempty"`
	Currency            *string  `json:"currency,omitempty"`
	Result              *string  `json:"result,omitempty"`
	Differences         []string `json:"differences"`
}

type us3Record struct {
	RecordID     string      `json:"record_id"`
	RequestID    string      `json:"request_id"`
	Source       string      `json:"source"`
	Decision     string      `json:"decision"`
	ResultBefore string      `json:"result_before"`
	ResultAfter  string      `json:"result_after"`
	ReasonCode   string      `json:"reason_code"`
	Evidence     us3Evidence `json:"evidence"`
}

type us3QueryBinding struct {
	RunID      string `json:"run_id"`
	Epoch      string `json:"epoch"`
	Generation uint64 `json:"generation"`
	CallSeq    uint64 `json:"call_seq"`
	EventID    string `json:"event_id"`
	OrderID    string `json:"order_id"`
}

type us3Query struct {
	Generation              uint64           `json:"generation"`
	RequestID               string           `json:"request_id"`
	Path                    string           `json:"path"`
	QueryCallSeq            uint64           `json:"query_call_seq"`
	Directive               string           `json:"directive"`
	Outcome                 string           `json:"outcome"`
	Applied                 bool             `json:"applied"`
	ApplyCount              uint             `json:"apply_count"`
	Decision                string           `json:"decision"`
	ResultBefore            string           `json:"result_before"`
	ResultAfter             string           `json:"result_after"`
	TraceOutcome            string           `json:"trace_outcome"`
	ConfirmedReturned       bool             `json:"confirmed_returned"`
	NewMockCalls            uint             `json:"new_mock_calls"`
	Binding                 *us3QueryBinding `json:"binding,omitempty"`
	ExpectedMockTransaction *string          `json:"expected_mock_transaction_id,omitempty"`
	Trusted                 bool             `json:"trusted"`

	confirmed    bool
	httpIssues   []string
	traceFailure bool
}

type us3Cleanup struct {
	Kind     string  `json:"kind"`
	TargetID *string `json:"target_id,omitempty"`
	Removed  bool    `json:"removed"`
	Verified bool    `json:"verified"`
}

type us3Fault struct {
	Kind              string     `json:"kind"`
	TargetID          *string    `json:"target_id,omitempty"`
	SQLState          string     `json:"sqlstate"`
	SQLStateSource    string     `json:"sqlstate_source"`
	FixtureCopy       bool       `json:"fixture_copy"`
	PlatformTableRead bool       `json:"platform_table_read"`
	Cleanup           us3Cleanup `json:"cleanup"`
}

type us3OrderFact struct {
	PaymentID         string  `json:"payment_id"`
	ClientRefSHA256   string  `json:"client_ref_sha256"`
	BusinessRefSHA256 string  `json:"business_ref_sha256"`
	Amount            int64   `json:"amount"`
	Currency          string  `json:"currency"`
	Result            string  `json:"result"`
	RegisteredAt      string  `json:"registered_at"`
	ConfirmedAt       *string `json:"confirmed_at,omitempty"`
	MockTransactionID *string `json:"mock_transaction_id,omitempty"`
}

type us3Facts struct {
	Before      *us3OrderFact `json:"before,omitempty"`
	After       *us3OrderFact `json:"after,omitempty"`
	Transition  string        `json:"transition"`
	StableFacts bool          `json:"stable_facts"`
}

type us3Receipt struct {
	RunID      string `json:"run_id"`
	Epoch      string `json:"epoch"`
	Generation uint64 `json:"generation"`
	LastSeq    uint64 `json:"last_seq"`
}

type us3ControlledBinding struct {
	RunID       string `json:"run_id"`
	Epoch       string `json:"epoch"`
	Generation  uint64 `json:"generation"`
	Point       string `json:"point"`
	OrderID     string `json:"order_id"`
	CallSeq     uint64 `json:"call_seq"`
	EventID     string `json:"event_id"`
	PrefixCount int    `json:"prefix_count"`
}

type us3ProcessExit struct {
	Code           int  `json:"code"`
	Canceled       bool `json:"canceled"`
	Reaped         bool `json:"reaped"`
	GroupGone      bool `json:"group_gone"`
	OutputComplete bool `json:"output_complete"`
}

type us3ControlledTermination struct {
	Binding             us3ControlledBinding `json:"binding"`
	Exit                us3ProcessExit       `json:"exit"`
	ControlledTruncated bool                 `json:"controlled_truncated"`
	NoReceipt           bool                 `json:"no_receipt"`
	ExactCounts         bool                 `json:"exact_counts"`
	ObservedPrefix      us3Counts            `json:"observed_prefix"`
}

type us3GenerationProof struct {
	Generation            uint64                    `json:"generation"`
	Mode                  string                    `json:"mode"`
	Sealed                bool                      `json:"sealed"`
	Trusted               bool                      `json:"trusted"`
	Receipt               *us3Receipt               `json:"receipt,omitempty"`
	Counts                *us3Counts                `json:"counts,omitempty"`
	ExactCounts           bool                      `json:"exact_counts"`
	ControlledTermination *us3ControlledTermination `json:"controlled_termination,omitempty"`
}

type us3Joint struct {
	Kind                         string                `json:"kind"`
	FirstGeneration              uint64                `json:"first_generation"`
	SecondGeneration             uint64                `json:"second_generation"`
	FirstMode                    string                `json:"first_mode"`
	SecondMode                   string                `json:"second_mode"`
	FirstExactCounts             bool                  `json:"first_exact_counts"`
	SecondExactCounts            bool                  `json:"second_exact_counts"`
	SecondReceipt                *us3Receipt           `json:"second_receipt"`
	HeldPoint                    string                `json:"held_point"`
	HeldBinding                  *us3ControlledBinding `json:"held_binding"`
	ProcessExit                  *us3ProcessExit       `json:"process_exit"`
	ObservedPrefix               us3Counts             `json:"observed_prefix"`
	NoExtraPaymentEffect         bool                  `json:"no_extra_payment_effect"`
	NoNewPaymentCallAfterRestart bool                  `json:"no_new_payment_call_after_restart"`
	OriginalIntentSame           bool                  `json:"original_intent_same"`
	Limitation                   string                `json:"limitation"`
}

type us3Protection struct {
	Kind                      string `json:"kind"`
	InitialResult             string `json:"initial_result"`
	ConfirmedResult           string `json:"confirmed_result"`
	ProtectedResult           string `json:"protected_result"`
	FirstApplied              string `json:"first_applied"`
	SecondApplied             string `json:"second_applied"`
	PostConfirmationMockCalls uint   `json:"post_confirmation_mock_calls"`
	OriginalFactPreserved     bool   `json:"original_fact_preserved"`
}

type us3Save struct {
	Attempts            uint   `json:"attempts"`
	RecordID            string `json:"record_id"`
	CommitState         string `json:"commit_state"`
	ApplicationReadback string `json:"application_readback"`
	ExternalPGReadback  string `json:"external_pg_readback"`
	ReadbackVerified    bool   `json:"readback_verified"`
	ReadbackScope       string `json:"readback_scope"`
	ReadbackSource      string `json:"readback_source"`
	CreatedClaimed      bool   `json:"created_claimed"`
	RollbackClaimed     bool   `json:"rollback_claimed"`
	CandidateIDLeaked   bool   `json:"candidate_id_leaked"`
}

type us3Trace struct {
	Outcome               string `json:"outcome"`
	Issue                 string `json:"issue"`
	SQLState              string `json:"sqlstate"`
	SQLStateSource        string `json:"sqlstate_source"`
	ConfirmedFactReturned bool   `json:"confirmed_fact_returned"`
	NewLookup             bool   `json:"new_lookup"`
	ProofSource           string `json:"proof_source"`
}

type us3Budget struct {
	Class      string `json:"class"`
	Kind       string `json:"kind"`
	Source     string `json:"source"`
	PassBudget bool   `json:"pass_budget"`
	LimitMS    uint64 `json:"limit_ms"`
	ElapsedMS  uint64 `json:"elapsed_ms"`
}

type us3Cancel struct {
	Phase                  string `json:"phase"`
	PostCancelPaymentCalls uint64 `json:"post_cancel_payment_calls"`
	PostCancelQueryCalls   uint64 `json:"post_cancel_query_calls"`
	FinishCheckedMock      bool   `json:"finish_checked_mock"`
	RollbackClaimed        bool   `json:"rollback_claimed"`
}

type us3Shutdown struct {
	PostShutdownPaymentCalls   uint64 `json:"post_shutdown_payment_calls"`
	PostShutdownQueryCalls     uint64 `json:"post_shutdown_query_calls"`
	RollbackClaimed            bool   `json:"rollback_claimed"`
	NoUnmanagedGoroutine       bool   `json:"no_unmanaged_goroutine"`
	NoUnmanagedGoroutineSource string `json:"no_unmanaged_goroutine_source"`
	PoolClosedAfterUsers       bool   `json:"pool_closed_after_users"`
	PoolClosedAfterUsersSource string `json:"pool_closed_after_users_source"`
	ExitElapsedMS              uint64 `json:"exit_elapsed_ms"`
	ExitUpperBoundMS           uint64 `json:"exit_upper_bound_ms"`
	ObservationToleranceMS     uint64 `json:"observation_tolerance_ms"`
	HostGraceMS                uint64 `json:"host_grace_ms"`
	HostGracePassBudget        bool   `json:"host_grace_pass_budget"`
}

func us3RefSHA256(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

func us3AppEnv(t *testing.T, dsn, httpAddr, clientID, apiKey, socket, runID, epoch string, generation uint64) []string {
	t.Helper()
	return []string{
		"PAYMENT_HUB_CLIENT_ID=" + clientID,
		"PAYMENT_HUB_API_KEY=" + apiKey,
		"PAYMENT_HUB_DATABASE_URL=" + dsn,
		"PAYMENT_HUB_HTTP_ADDR=" + httpAddr,
		"PAYMENT_HUB_TEST_MODE=on",
		"PAYMENT_HUB_TEST_CONTROL_VERSION=2",
		"PAYMENT_HUB_TEST_CONTROL_SOCKET=" + socket,
		"PAYMENT_HUB_TEST_RUN_ID=" + runID,
		"PAYMENT_HUB_TEST_OBSERVER_EPOCH=" + epoch,
		"PAYMENT_HUB_TEST_GENERATION=" + fmt.Sprintf("%d", generation),
		"TMPDIR=" + us1IsolatedTempDir(t),
		"TZ=UTC",
	}
}

func us3StartObserver(t *testing.T, runID, epoch, socket string) *harness.Observer {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), us1BarrierBudget)
	defer cancel()
	observer, err := harness.StartObserver(ctx, harness.ObserverConfig{
		Socket:         socket,
		RunID:          runID,
		Epoch:          epoch,
		BarrierTimeout: us1BarrierBudget,
		Version:        testcontrol.Version2,
	})
	if err != nil || observer == nil {
		t.Fatal("US3 v2 observer start failed")
	}
	t.Cleanup(func() { us1CloseObserver(t, observer) })
	return observer
}

func us3StartGeneration(t *testing.T, artifacts *harness.Artifacts, binary, repoRoot, dsn, clientID, apiKey, socket, runID, epoch string, generation uint64) (*harness.Process, string) {
	t.Helper()
	addr := us1FreeLoopbackAddr(t)
	env := us3AppEnv(t, dsn, addr, clientID, apiKey, socket, runID, epoch, generation)
	process := us1StartApplication(t, artifacts, binary, repoRoot, generation, env, 10*time.Second, 60*time.Second)
	us1WaitReady(t, addr)
	return process, addr
}

func us3LockMockTransactions(t *testing.T, pool *pgxpool.Pool) (func(), int) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 2*us1RequestBudget)
	tx, err := pool.Begin(ctx)
	if err != nil {
		cancel()
		t.Fatalf("US3 mock_transactions lock transaction failed (%s)", us1SQLState(err))
	}
	var blockerPID int
	if err := tx.QueryRow(ctx, "SELECT pg_backend_pid()").Scan(&blockerPID); err != nil {
		us2RequireRollback(t, tx.Rollback(ctx), "mock_transactions pid")
		cancel()
		t.Fatalf("US3 mock_transactions blocker pid failed (%s)", us1SQLState(err))
	}
	if _, err := tx.Exec(ctx, "LOCK TABLE mock_transactions IN SHARE MODE"); err != nil {
		us2RequireRollback(t, tx.Rollback(ctx), "mock_transactions lock setup")
		cancel()
		t.Fatalf("US3 mock_transactions lock failed (%s)", us1SQLState(err))
	}
	var once sync.Once
	release := func() {
		once.Do(func() {
			rollbackCtx, rollbackCancel := context.WithTimeout(context.Background(), us1CleanupBudget)
			defer rollbackCancel()
			us2RequireRollback(t, tx.Rollback(rollbackCtx), "mock_transactions lock")
			cancel()
		})
	}
	t.Cleanup(release)
	return release, blockerPID
}

type us3MockInsertWaiter struct {
	PID           int
	BackendStart  time.Time
	XactStart     time.Time
	Query         string
	RelationOID   uint32
	BlockerPID    int
	WaitEventType string
	WaitEvent     string
	ObservedAt    time.Time
}

func us3WaitSingleMockInsertWaiter(t *testing.T, pool *pgxpool.Pool, blockerPID int) us3MockInsertWaiter {
	t.Helper()
	start := time.Now()
	deadline := start.Add(us1BarrierBudget)
	for {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		rows, err := pool.Query(ctx, `
			SELECT DISTINCT a.pid, a.backend_start, a.xact_start, a.query,
			       c.oid::oid, a.wait_event_type, COALESCE(a.wait_event, '')
			FROM pg_locks l
			JOIN pg_stat_activity a ON a.pid = l.pid
			JOIN pg_class c ON c.oid = l.relation
			WHERE l.locktype = 'relation'
			  AND l.mode = 'RowExclusiveLock'
			  AND NOT l.granted
			  AND c.oid = 'mock_transactions'::regclass
			  AND $1 = ANY(pg_blocking_pids(a.pid))
			  AND a.pid <> pg_backend_pid()
		`, blockerPID)
		if err != nil {
			cancel()
			t.Fatalf("US3 mock waiter poll failed (%s)", us1SQLState(err))
		}
		var waiters []us3MockInsertWaiter
		for rows.Next() {
			var waiter us3MockInsertWaiter
			var backendStart, xactStart pgtype.Timestamptz
			var waitEventType, waitEvent pgtype.Text
			if err := rows.Scan(&waiter.PID, &backendStart, &xactStart, &waiter.Query,
				&waiter.RelationOID, &waitEventType, &waitEvent); err != nil {
				rows.Close()
				cancel()
				t.Fatalf("US3 mock waiter scan failed (%s)", us1SQLState(err))
			}
			if !backendStart.Valid || !xactStart.Valid {
				rows.Close()
				cancel()
				t.Fatalf("US3 mock waiter lacks backend_start/xact_start")
			}
			waiter.BackendStart = backendStart.Time.UTC()
			waiter.XactStart = xactStart.Time.UTC()
			waiter.BlockerPID = blockerPID
			waiter.ObservedAt = time.Now().UTC()
			if waitEventType.Valid {
				waiter.WaitEventType = waitEventType.String
			}
			if waitEvent.Valid {
				waiter.WaitEvent = waitEvent.String
			}
			waiters = append(waiters, waiter)
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			cancel()
			t.Fatalf("US3 mock waiter rows failed (%s)", us1SQLState(err))
		}
		rows.Close()
		cancel()
		if len(waiters) == 1 {
			waiter := waiters[0]
			if waiter.RelationOID == 0 || waiter.PID <= 0 || waiter.BlockerPID != blockerPID {
				t.Fatalf("US3 mock waiter identity mismatch")
			}
			if !strings.Contains(strings.ToUpper(waiter.Query), "INSERT INTO MOCK_TRANSACTIONS") {
				t.Fatalf("US3 mock waiter is not the expected Mock INSERT: %q", waiter.Query)
			}
			return waiter
		}
		if len(waiters) > 1 {
			t.Fatalf("US3 mock waiter is not unique: got %d", len(waiters))
		}
		if time.Now().After(deadline) {
			t.Fatalf("US3 mock waiter poll timed out")
		}
		runtime.Gosched()
	}
}

func us3WaiterIdentityGone(waiter us3MockInsertWaiter, exists bool, backendStart time.Time, xactStart *time.Time) bool {
	if !exists {
		return true
	}
	if backendStart.IsZero() || !backendStart.Equal(waiter.BackendStart) {
		return true
	}
	if xactStart == nil || !xactStart.Equal(waiter.XactStart) {
		return true
	}
	return false
}

func us3WaitMockTargetTransactionGone(t *testing.T, pool *pgxpool.Pool, waiter us3MockInsertWaiter) time.Duration {
	t.Helper()
	start := time.Now()
	deadline := start.Add(us1BarrierBudget)
	for {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		var backendStart, xactStart pgtype.Timestamptz
		err := pool.QueryRow(ctx, `
			SELECT backend_start, xact_start FROM pg_stat_activity WHERE pid = $1
		`, waiter.PID).Scan(&backendStart, &xactStart)
		cancel()
		exists := err == nil
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			t.Fatalf("US3 target transaction identity poll failed (%s)", us1SQLState(err))
		}
		if exists && !backendStart.Valid {
			t.Fatalf("US3 target backend identity is unavailable")
		}
		var backend time.Time
		if backendStart.Valid {
			backend = backendStart.Time.UTC()
		}
		var xact *time.Time
		if xactStart.Valid {
			value := xactStart.Time.UTC()
			xact = &value
		}
		if us3WaiterIdentityGone(waiter, exists, backend, xact) {
			return time.Since(start)
		}
		if time.Now().After(deadline) {
			t.Fatalf("US3 target transaction did not terminate by identity")
		}
		runtime.Gosched()
	}
}

func us3RequireLockGone(t *testing.T, pool *pgxpool.Pool, blockerPID int, relationOID uint32) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	var count int
	if err := pool.QueryRow(ctx, `
		SELECT count(*) FROM pg_locks WHERE pid = $1 AND relation = $2::oid
	`, blockerPID, relationOID).Scan(&count); err != nil {
		t.Fatalf("US3 lock release check failed (%s)", us1SQLState(err))
	}
	if count != 0 {
		t.Fatalf("US3 mock_transactions lock remained held after release")
	}
}

func us3RequireNoMockRowForOrder(t *testing.T, pool *pgxpool.Pool, orderID string, window time.Duration) {
	t.Helper()
	deadline := time.Now().Add(window)
	for {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		var count int
		err := pool.QueryRow(ctx, `
			SELECT count(*) FROM mock_transactions WHERE platform_reference_id = $1
		`, orderID).Scan(&count)
		cancel()
		if err != nil {
			t.Fatalf("US3 late Mock row check failed (%s)", us1SQLState(err))
		}
		if count != 0 {
			t.Fatalf("US3 late Mock row appeared after release: %d", count)
		}
		if time.Now().After(deadline) {
			return
		}
		time.Sleep(25 * time.Millisecond)
	}
}

func us3SeedOrder(t *testing.T, pool *pgxpool.Pool, clientID, businessOrderNo string, amount int64, result payment.Result, mockID *string, confirmedAt *time.Time) string {
	t.Helper()
	orderID := payment.NewUUIDv4()
	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	if _, err := pool.Exec(ctx, `
		INSERT INTO payment_orders
			(id, client_id, business_order_no, amount, currency, result, mock_transaction_id, registered_at, confirmed_at)
		VALUES ($1, $2, $3, $4, 'CNY', $5, $6, $7, $8)
	`, orderID, clientID, businessOrderNo, amount, string(result), mockID, time.Now().UTC().Truncate(time.Microsecond), confirmedAt); err != nil {
		t.Fatalf("US3 order seed insert failed (%s)", us1SQLState(err))
	}
	return orderID
}

func us3SeedMock(t *testing.T, pool *pgxpool.Pool, mockID, platformReferenceID string, amount int64, result payment.Result, processedAt time.Time) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	if _, err := pool.Exec(ctx, `
		INSERT INTO mock_transactions
			(id, platform_reference_id, amount, currency, result, processed_at)
		VALUES ($1, $2, $3, 'CNY', $4, $5)
	`, mockID, platformReferenceID, amount, string(result), processedAt.UTC().Truncate(time.Microsecond)); err != nil {
		t.Fatalf("US3 mock seed insert failed (%s)", us1SQLState(err))
	}
}

func us3SeedTrace(t *testing.T, pool *pgxpool.Pool, orderID, requestID string, source payment.Source, decision payment.HandlingDecision, before, after payment.Result, reasonCode *string, evidence map[string]any) string {
	t.Helper()
	recordID := payment.NewUUIDv4()
	encoded, err := json.Marshal(evidence)
	if err != nil {
		t.Fatal("US3 trace evidence encode failed")
	}
	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	if _, err := pool.Exec(ctx, `
		INSERT INTO result_handling_records
			(id, payment_order_id, request_id, source, observed_at, evidence,
			 result_before, result_after, decision, reason_code, reason_message)
		VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, NULL)
	`, recordID, orderID, requestID, string(source), time.Now().UTC().Truncate(time.Microsecond),
		string(encoded), string(before), string(after), string(decision), reasonCode); err != nil {
		t.Fatalf("US3 trace seed insert failed (%s)", us1SQLState(err))
	}
	return recordID
}

func us3OrderFactFromState(state us1OrderState) us3OrderFact {
	fact := us3OrderFact{
		PaymentID:         state.ID,
		ClientRefSHA256:   us3RefSHA256(state.ClientID),
		BusinessRefSHA256: us3RefSHA256(state.BusinessOrderNo),
		Amount:            state.Amount,
		Currency:          state.Currency,
		Result:            state.Result,
		RegisteredAt:      state.RegisteredAt.UTC().Format(time.RFC3339Nano),
	}
	if state.ConfirmedAt != nil {
		value := state.ConfirmedAt.UTC().Format(time.RFC3339Nano)
		fact.ConfirmedAt = &value
	}
	if state.MockTransactionID != "" {
		value := state.MockTransactionID
		fact.MockTransactionID = &value
	}
	return fact
}

func us3ReadOrderFact(t *testing.T, pool *pgxpool.Pool, clientID, businessOrderNo string) us3OrderFact {
	t.Helper()
	return us3OrderFactFromState(us1ReadOrderState(t, pool, clientID, businessOrderNo))
}

func us3ReadRecords(t *testing.T, pool *pgxpool.Pool, paymentOrderID string) []us3Record {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), us1RequestBudget)
	defer cancel()
	rows, err := pool.Query(ctx, `
		SELECT id::text, request_id::text, source, decision, result_before, result_after,
		       COALESCE(reason_code, ''), evidence
		FROM result_handling_records
		WHERE payment_order_id = $1
		ORDER BY observed_at, id
	`, paymentOrderID)
	if err != nil {
		t.Fatalf("US3 record read failed (%s)", us1SQLState(err))
	}
	defer rows.Close()
	var records []us3Record
	for rows.Next() {
		var record us3Record
		var evidence []byte
		record.Evidence.Differences = []string{}
		if err := rows.Scan(&record.RecordID, &record.RequestID, &record.Source, &record.Decision,
			&record.ResultBefore, &record.ResultAfter, &record.ReasonCode, &evidence); err != nil {
			t.Fatalf("US3 record scan failed (%s)", us1SQLState(err))
		}
		var parsed payment.Evidence
		if err := json.Unmarshal(evidence, &parsed); err != nil {
			t.Fatal("US3 record evidence parse failed")
		}
		if parsed.PlatformReferenceID != nil {
			value := string(*parsed.PlatformReferenceID)
			record.Evidence.PlatformReferenceID = &value
		}
		if parsed.MockTransactionID != nil {
			value := string(*parsed.MockTransactionID)
			record.Evidence.MockTransactionID = &value
		}
		record.Evidence.Amount = parsed.Amount
		record.Evidence.Currency = parsed.Currency
		if parsed.Result != nil {
			value := string(*parsed.Result)
			record.Evidence.Result = &value
		}
		for _, difference := range parsed.Differences {
			record.Evidence.Differences = append(record.Evidence.Differences, string(difference))
		}
		records = append(records, record)
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("US3 record rows failed (%s)", us1SQLState(err))
	}
	return records
}

func us3RecordQueryHTTP(t *testing.T, scenario *us3Scenario, generation uint64, requestID, path string, confirmed bool, binding us3QueryBinding, issues ...string) {
	t.Helper()
	if scenario == nil || requestID == "" || !us1LowerHexUUIDv4(requestID) {
		t.Fatalf("US3 query HTTP fact lacks a valid actual request id")
	}
	if binding.Generation != generation || binding.RunID == "" || binding.Epoch == "" ||
		binding.CallSeq == 0 || binding.EventID == "" || binding.OrderID == "" {
		t.Fatalf("US3 query HTTP fact lacks a valid actual call binding")
	}
	if scenario.queryHTTP == nil {
		scenario.queryHTTP = make(map[uint64][]us3QueryHTTPFact)
	}
	scenario.queryHTTP[generation] = append(scenario.queryHTTP[generation], us3QueryHTTPFact{
		RequestID: requestID, Path: path, Confirmed: confirmed,
		Issues: append([]string(nil), issues...), Binding: binding,
	})
}

func us3QueryIdentityKey(binding us3QueryBinding) string {
	return fmt.Sprintf("%s/%s/%d/%d/%s/%s", binding.RunID, binding.Epoch,
		binding.Generation, binding.CallSeq, binding.EventID, binding.OrderID)
}

func us3QueryBindingFromApplication(application harness.QueryApplication) us3QueryBinding {
	return us3QueryBinding{
		RunID: application.RunID, Epoch: application.Epoch,
		Generation: application.Generation, CallSeq: application.CallSeq,
		EventID: application.CallEventID, OrderID: application.OrderID,
	}
}

func us3QueryBindingFromHold(binding harness.QueryBinding) us3QueryBinding {
	return us3QueryBinding{
		RunID: binding.RunID, Epoch: binding.Epoch,
		Generation: binding.Generation, CallSeq: binding.CallSeq,
		EventID: binding.EventID, OrderID: binding.OrderID,
	}
}

func us3QueryApplicationSet(t *testing.T, observer *harness.Observer, generation uint64) map[string]harness.QueryApplication {
	t.Helper()
	snapshot, ok := observer.QueryApplications(generation)
	if !ok {
		t.Fatalf("US3 QueryApplications unavailable for generation %d", generation)
	}
	set := make(map[string]harness.QueryApplication, len(snapshot.Applications))
	for _, application := range snapshot.Applications {
		set[us3QueryIdentityKey(us3QueryBindingFromApplication(application))] = application
	}
	return set
}

func us3NewAppliedQueryBinding(t *testing.T, observer *harness.Observer, generation uint64, before, after map[string]harness.QueryApplication, strict bool) (us3QueryBinding, bool) {
	t.Helper()
	var found []harness.QueryApplication
	for key, application := range after {
		if _, existed := before[key]; existed {
			continue
		}
		if application.AppliedSeq == 0 {
			continue
		}
		found = append(found, application)
	}
	if len(found) == 1 {
		return us3QueryBindingFromApplication(found[0]), true
	}
	if len(found) == 0 && !strict {
		return us3QueryBinding{}, false
	}
	t.Fatalf("US3 query request did not produce exactly one new applied QueryApplication: got %d", len(found))
	return us3QueryBinding{}, false
}

func us3CaptureBeforeCounts(t *testing.T, pool *pgxpool.Pool, clientID string, scenario *us3Scenario, paymentCalls, queryCalls uint64) {
	t.Helper()
	orders, mocks, traces := us1CountBusinessRows(t, pool, clientID)
	scenario.countsBefore = us3Counts{
		PaymentCalls: paymentCalls, QueryCalls: queryCalls,
		OrderRows: uint64(orders), MockRows: uint64(mocks), TraceRows: uint64(traces),
	}
	scenario.countsBeforeSet = true
}

func us3FindRecordByRequest(records []us3Record, source, requestID string) (us3Record, bool) {
	for _, record := range records {
		if record.Source == source && record.RequestID == requestID {
			return record, true
		}
	}
	return us3Record{}, false
}

func us3FindRecordByDecision(records []us3Record, source, decision string) (us3Record, bool) {
	for _, record := range records {
		if record.Source == source && record.Decision == decision {
			return record, true
		}
	}
	return us3Record{}, false
}

func us3ApplyActualQueryFacts(t *testing.T, queries []us3Query, records []us3Record) []us3Query {
	t.Helper()
	for index := range queries {
		query := &queries[index]
		query.ConfirmedReturned = query.confirmed
		matched, found := us3FindRecordByRequest(records, "QUERY", query.RequestID)
		if found {
			query.Decision = matched.Decision
			query.ResultBefore = matched.ResultBefore
			query.ResultAfter = matched.ResultAfter
			query.TraceOutcome = "SAVED"
			if query.ExpectedMockTransaction != nil {
				if matched.Evidence.MockTransactionID == nil ||
					*matched.Evidence.MockTransactionID == *query.ExpectedMockTransaction {
					t.Fatalf("US3 query %d expected Mock transaction was not contradicted by the actual record", index)
				}
				contradicted := false
				for _, difference := range matched.Evidence.Differences {
					if difference == "mock_transaction_id" {
						contradicted = true
					}
				}
				if !contradicted {
					t.Fatalf("US3 query %d actual record lacks the mock_transaction_id difference", index)
				}
			}
			continue
		}
		// A failed trace insert is the only legitimate query without a
		// handling record. It must be explicitly marked by the test that
		// installed the trace fixture and must carry the actual TRACE_SAVE_ERROR
		// HTTP issue; it never falls back to a success-shaped template.
		if query.traceFailure && us3ContainsString(query.httpIssues, "TRACE_SAVE_ERROR") {
			query.Decision = "NOT_RECORDED"
			query.ResultBefore = ""
			query.ResultAfter = ""
			query.TraceOutcome = "FAILED"
			continue
		}
		t.Fatalf("US3 query %d lacks its actual handling record (request %q)", index, query.RequestID)
	}
	return queries
}

func us3ContainsString(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}

func us3WaitQueryApplication(t *testing.T, observer *harness.Observer, generation uint64, binding harness.QueryBinding, start time.Time) time.Duration {
	t.Helper()
	deadline := start.Add(us1BarrierBudget)
	for {
		snapshot, ok := observer.QueryApplications(generation)
		if ok {
			for _, application := range snapshot.Applications {
				if application.CallSeq == binding.CallSeq &&
					application.CallEventID == binding.EventID &&
					application.AppliedSeq != 0 &&
					application.Outcome == testcontrol.QueryOutcomeSubtimeout {
					return time.Since(start)
				}
			}
		}
		if time.Now().After(deadline) {
			t.Fatalf("US3 subtimeout QueryApplication did not reach applied/subtimeout for generation %d", generation)
		}
		runtime.Gosched()
	}
}

func us3FindCall(scenario *us3Scenario, generation uint64) (us3Call, bool) {
	for _, call := range scenario.Calls {
		if call.Generation == generation {
			return call, true
		}
	}
	return us3Call{}, false
}

func us3RequireActualCalls(t *testing.T, scenario *us3Scenario, generation uint64, paymentCalls, queryCalls uint64) {
	t.Helper()
	call, ok := us3FindCall(scenario, generation)
	if !ok || !call.Exact || call.PaymentCalls != paymentCalls || call.QueryCalls != queryCalls {
		t.Fatalf("US3 generation %d actual calls mismatch: %+v want payment=%d query=%d",
			generation, call, paymentCalls, queryCalls)
	}
}

func us3RequirePaymentResponse(t *testing.T, response *http.Response, body []byte, wantStatus int, wantPaymentID, wantBusinessOrderNo string, wantAmount int64, wantCurrency string, wantResult payment.Result, wantIssues ...string) (us1Payment, string) {
	t.Helper()
	if response == nil {
		t.Fatal("US3 payment response is nil")
	}
	if response.StatusCode != wantStatus {
		t.Fatalf("US3 payment response status mismatch: got %d want %d", response.StatusCode, wantStatus)
	}
	if !strings.HasPrefix(response.Header.Get("Content-Type"), "application/json") {
		t.Fatal("US3 payment response content type mismatch")
	}
	if !strings.Contains(response.Header.Get("Cache-Control"), "no-store") {
		t.Fatal("US3 payment response cache policy mismatch")
	}
	envelope := us1ParseEnvelope(t, body)
	if envelope.RequestID == "" || payment.ValidateUUIDv4(envelope.RequestID) != nil {
		t.Fatal("US3 payment response request_id mismatch")
	}
	if envelope.Payment == nil || envelope.Error != nil {
		t.Fatal("US3 payment response envelope mismatch")
	}
	paymentBody := *envelope.Payment
	if paymentBody.PaymentID != wantPaymentID ||
		paymentBody.BusinessOrderNo != wantBusinessOrderNo ||
		paymentBody.Amount != wantAmount ||
		paymentBody.Currency != wantCurrency ||
		paymentBody.Result != string(wantResult) {
		t.Fatal("US3 payment response fact mismatch")
	}
	us1RequireRFC3339Z(t, paymentBody.RegisteredAt)
	if wantResult == payment.ResultSucceeded || wantResult == payment.ResultFailed {
		if paymentBody.ConfirmedAt == nil {
			t.Fatal("US3 confirmed payment lacks confirmed_at")
		}
		us1RequireRFC3339Z(t, *paymentBody.ConfirmedAt)
	} else if paymentBody.ConfirmedAt != nil {
		t.Fatal("US3 unknown payment unexpectedly carried confirmed_at")
	}
	if wantStatus == http.StatusCreated {
		wantLocation := "/api/v1/payments/" + paymentBody.PaymentID
		if response.Header.Get("Location") != wantLocation {
			t.Fatal("US3 created payment Location mismatch")
		}
	} else if response.Header.Get("Location") != "" {
		t.Fatal("US3 non-created payment unexpectedly carried Location")
	}
	wantSet := make(map[string]bool, len(wantIssues))
	for _, code := range wantIssues {
		wantSet[code] = true
	}
	seen := make(map[string]bool, len(envelope.Issues))
	for _, issue := range envelope.Issues {
		if !wantSet[issue.Code] || seen[issue.Code] || issue.Message == "" {
			t.Fatal("US3 payment response carried an unexpected issue")
		}
		seen[issue.Code] = true
	}
	if len(seen) != len(wantSet) {
		t.Fatal("US3 payment response issue set mismatch")
	}
	return paymentBody, envelope.RequestID
}

func us3RecordHTTP(t *testing.T, artifacts *harness.Artifacts, scenario *us3Scenario, generation uint64, status int, requestID, paymentID, result string, issues ...string) {
	t.Helper()
	us3RecordHTTPKind(t, artifacts, scenario, "PRIMARY", generation, status, requestID, paymentID, result, issues...)
}

func us3RecordHTTPKind(t *testing.T, artifacts *harness.Artifacts, scenario *us3Scenario, kind string, generation uint64, status int, requestID, paymentID, result string, issues ...string) {
	t.Helper()
	if issues == nil {
		issues = []string{}
	}
	if err := artifacts.RecordHTTP(harness.HTTPObservation{
		Generation: generation, Status: status, RequestID: requestID,
		PaymentOrderID: paymentID, Result: result,
	}); err != nil {
		t.Fatal("US3 HTTP artifact record failed")
	}
	scenario.HTTP = append(scenario.HTTP, us3HTTP{
		Kind: kind, Generation: generation, Status: status, RequestID: requestID,
		PaymentID: paymentID, Result: result, Issues: issues,
	})
}

func us3RecordSQL(t *testing.T, artifacts *harness.Artifacts, scenario *us3Scenario, generation uint64, state string, rows uint64) {
	t.Helper()
	if err := artifacts.RecordSQL(harness.SQLObservation{Generation: generation, State: "00000", Rows: rows}); err != nil {
		t.Fatal("US3 SQL artifact record failed")
	}
	scenario.PG = append(scenario.PG, us1ReportPG{State: state, Rows: rows})
}

func us3RecordBudget(t *testing.T, artifacts *harness.Artifacts, scenario *us3Scenario, generation uint64, class, kind, source string, passBudget bool, limit, elapsed time.Duration) {
	t.Helper()
	if err := artifacts.RecordBudget(harness.BudgetObservation{
		Generation: generation, Stage: harness.BudgetBarrier, Limit: limit, Elapsed: elapsed,
	}); err != nil {
		t.Fatal("US3 budget artifact record failed")
	}
	scenario.Budgets = append(scenario.Budgets, us3Budget{
		Class: class, Kind: kind, Source: source, PassBudget: passBudget,
		LimitMS: uint64(limit / time.Millisecond), ElapsedMS: uint64(elapsed / time.Millisecond),
	})
}

type us3ScenarioState struct {
	mu        sync.Mutex
	artifacts *harness.Artifacts
	scenario  string
	finished  bool
	calls     int
}

var (
	us3ScenarioStatesMu sync.Mutex
	us3ScenarioStates   = make(map[*harness.Artifacts]*us3ScenarioState)
)

func us3RegisterScenarioState(artifacts *harness.Artifacts, scenario string) *us3ScenarioState {
	state := &us3ScenarioState{artifacts: artifacts, scenario: scenario}
	us3ScenarioStatesMu.Lock()
	us3ScenarioStates[artifacts] = state
	us3ScenarioStatesMu.Unlock()
	return state
}

func us3ScenarioStateFor(artifacts *harness.Artifacts) *us3ScenarioState {
	us3ScenarioStatesMu.Lock()
	defer us3ScenarioStatesMu.Unlock()
	return us3ScenarioStates[artifacts]
}

func (state *us3ScenarioState) finish(t *testing.T, success bool) error {
	t.Helper()
	state.mu.Lock()
	if state.finished {
		state.mu.Unlock()
		return nil
	}
	state.finished = true
	state.calls++
	state.mu.Unlock()
	latched := false
	if !success {
		_ = state.artifacts.RecordHTTP(harness.HTTPObservation{Status: 0})
		latched = true
	}
	_, err := state.artifacts.Finish()
	if err != nil && !latched {
		return err
	}
	return err
}

func us3BeginArtifacts(t *testing.T, repoRoot, runID, scenario string) *harness.Artifacts {
	t.Helper()
	if !us3ScenarioNameSet[scenario] {
		t.Fatal("US3 scenario name is invalid")
	}
	collector := us3ReportInit(t, runID)
	root := filepath.Join(collector.root, runID, "scenarios", "us3", scenario)
	if err := os.MkdirAll(root, 0o700); err != nil {
		t.Fatal("US3 scenario artifact root creation failed")
	}
	if err := us1ValidateArtifactRoot(root); err != nil {
		t.Fatal("US3 scenario artifact root is unsafe")
	}
	artifacts, err := harness.BeginArtifacts(harness.ArtifactConfig{Root: root, SourceRoot: repoRoot, RunID: runID})
	if err != nil {
		t.Fatal("US3 artifacts begin failed")
	}
	state := us3RegisterScenarioState(artifacts, scenario)
	t.Cleanup(func() { _ = state.finish(t, false) })
	return artifacts
}

func us3FinishArtifacts(t *testing.T, artifacts *harness.Artifacts) {
	t.Helper()
	if state := us3ScenarioStateFor(artifacts); state != nil {
		if err := state.finish(t, true); err != nil {
			t.Fatal("US3 artifacts finish failed")
		}
		return
	}
	if _, err := artifacts.Finish(); err != nil {
		t.Fatal("US3 artifacts finish failed")
	}
}

func us3CorrelateQueryFacts(httpFacts []us3QueryHTTPFact, applications []harness.QueryApplication, trusted, strict bool) ([]us3Query, error) {
	httpByIdentity := make(map[string]us3QueryHTTPFact, len(httpFacts))
	for _, fact := range httpFacts {
		if !us1LowerHexUUIDv4(fact.RequestID) || !us3ValidQueryPath(fact.Path) {
			return nil, errors.New("US3 query HTTP fact lacks a valid actual request id/path")
		}
		key := us3QueryIdentityKey(fact.Binding)
		if _, exists := httpByIdentity[key]; exists {
			return nil, errors.New("US3 query HTTP fact identity is duplicated")
		}
		httpByIdentity[key] = fact
	}
	queries := make([]us3Query, 0, len(applications))
	used := make(map[string]bool, len(httpByIdentity))
	for _, application := range applications {
		if application.AppliedSeq == 0 {
			continue
		}
		binding := us3QueryBindingFromApplication(application)
		key := us3QueryIdentityKey(binding)
		fact, found := httpByIdentity[key]
		if !found {
			return nil, errors.New("US3 applied QueryApplication lacks an actual HTTP request identity")
		}
		if used[key] {
			return nil, errors.New("US3 applied QueryApplication identity is duplicated")
		}
		used[key] = true
		directive := "normal"
		if application.Directive != nil {
			directive = string(*application.Directive)
		}
		queries = append(queries, us3Query{
			Generation:   application.Generation,
			RequestID:    fact.RequestID,
			Path:         fact.Path,
			QueryCallSeq: application.CallSeq,
			Directive:    directive,
			Outcome:      string(application.Outcome),
			Applied:      true,
			ApplyCount:   1,
			Binding:      &binding,
			Trusted:      trusted,
			confirmed:    fact.Confirmed,
			httpIssues:   append([]string(nil), fact.Issues...),
			traceFailure: us3ContainsString(fact.Issues, "TRACE_SAVE_ERROR"),
		})
	}
	if strict {
		for key := range httpByIdentity {
			if !used[key] {
				return nil, errors.New("US3 actual HTTP query request lacks an applied QueryApplication")
			}
		}
	}
	return queries, nil
}

func us3MergeQueryApplications(t *testing.T, scenario *us3Scenario, observer *harness.Observer, generation uint64) {
	t.Helper()
	snapshot, ok := observer.QueryApplications(generation)
	if !ok {
		if scenario.strictQueryApplications {
			t.Fatalf("US3 QueryApplications unavailable for generation %d", generation)
		}
		return
	}
	queries, err := us3CorrelateQueryFacts(scenario.queryHTTP[generation], snapshot.Applications, snapshot.Trusted, scenario.strictQueryApplications)
	if err != nil {
		t.Fatal(err)
	}
	for index := range queries {
		if scenario.expectedMockTransaction != "" {
			expected := scenario.expectedMockTransaction
			queries[index].ExpectedMockTransaction = &expected
		}
	}
	merged := make([]us3Query, 0, len(scenario.Queries)+len(queries))
	for _, query := range scenario.Queries {
		if query.Generation != generation {
			merged = append(merged, query)
		}
	}
	scenario.Queries = append(merged, queries...)
}

func us3FinalizeScenario(t *testing.T, artifacts *harness.Artifacts, scenario *us3Scenario, observer *harness.Observer, process *harness.Process, wantGenerations []uint64, controlled *harness.ControlledTerminationResult) {
	t.Helper()
	exit := us1StopProcess(t, process)
	scenario.Commands = append(scenario.Commands, us1ReportCommand{Kind: us1ReportCommandStop, Exit: exit.Code})
	for _, generation := range wantGenerations {
		us3MergeQueryApplications(t, scenario, observer, generation)
	}
	proofs := make([]us3GenerationProof, 0, len(wantGenerations))
	calls := make([]us3Call, 0, len(wantGenerations))
	for _, generation := range wantGenerations {
		if generation == 1 && controlled != nil {
			proof := us3GenerationProof{
				Generation:  generation,
				Mode:        "controlled_truncated",
				Sealed:      false,
				Trusted:     false,
				ExactCounts: false,
				ControlledTermination: &us3ControlledTermination{
					Binding: us3ControlledBinding{
						RunID:       controlled.Binding.RunID,
						Epoch:       controlled.Binding.Epoch,
						Generation:  controlled.Binding.Generation,
						Point:       string(controlled.Binding.Point),
						OrderID:     controlled.Binding.OrderID,
						CallSeq:     controlled.Binding.Seq,
						EventID:     controlled.Binding.EventID,
						PrefixCount: controlled.Binding.PrefixCount,
					},
					Exit: us3ProcessExit{
						Code: controlled.Exit.Code, Canceled: controlled.Exit.Canceled,
						Reaped: controlled.Exit.Reaped, GroupGone: controlled.Exit.GroupGone,
						OutputComplete: controlled.Exit.OutputComplete,
					},
					ControlledTruncated: controlled.ControlledTruncated,
					NoReceipt:           controlled.NoReceipt,
					ExactCounts:         controlled.ExactCounts,
					ObservedPrefix: us3Counts{
						PaymentCalls: controlled.ObservedPrefix.Payment,
						QueryCalls:   controlled.ObservedPrefix.Query,
					},
				},
			}
			proofs = append(proofs, proof)
			calls = append(calls, us3Call{
				Generation: generation, Exact: false,
				PaymentCalls: controlled.ObservedPrefix.Payment,
				QueryCalls:   controlled.ObservedPrefix.Query,
			})
			continue
		}
		receipt, counts, ok := observer.SealedGeneration(generation)
		if !ok {
			t.Fatalf("US3 generation %d is not independently sealed", generation)
		}
		proof := us3GenerationProof{
			Generation: generation, Mode: "complete", Sealed: true, Trusted: true,
			Receipt:     &us3Receipt{RunID: receipt.RunID, Epoch: receipt.Epoch, Generation: receipt.Generation, LastSeq: receipt.LastSeq},
			Counts:      &us3Counts{PaymentCalls: counts.Payment, QueryCalls: counts.Query},
			ExactCounts: true,
		}
		proofs = append(proofs, proof)
		calls = append(calls, us3Call{Generation: generation, Exact: true, PaymentCalls: counts.Payment, QueryCalls: counts.Query})
	}
	scenario.GenerationProofs = proofs
	scenario.Calls = calls
	if scenario.countsBeforeSet {
		scenario.Checks.CountsBefore = scenario.countsBefore
	}
	if len(calls) > 0 {
		last := calls[len(calls)-1]
		scenario.Checks.CountsAfter.PaymentCalls = last.PaymentCalls
		scenario.Checks.CountsAfter.QueryCalls = last.QueryCalls
	}
	if len(scenario.PG) != 3 {
		t.Fatalf("US3 scenario %s must record exactly ORDER/MOCK/TRACE PG counts, got %d", scenario.Name, len(scenario.PG))
	}
	pgSeen := map[string]bool{}
	for _, state := range scenario.PG {
		pgSeen[state.State] = true
		switch state.State {
		case "ORDER":
			scenario.Checks.CountsAfter.OrderRows = state.Rows
		case "MOCK":
			scenario.Checks.CountsAfter.MockRows = state.Rows
		case "TRACE":
			scenario.Checks.CountsAfter.TraceRows = state.Rows
		}
	}
	if !pgSeen["ORDER"] || !pgSeen["MOCK"] || !pgSeen["TRACE"] {
		t.Fatalf("US3 scenario %s PG counts lack ORDER/MOCK/TRACE", scenario.Name)
	}
	exactCounts := true
	for _, proof := range proofs {
		exactCounts = exactCounts && proof.ExactCounts
	}
	scenario.Checks.CountsExact = exactCounts
	scenario.Facts.StableFacts = us3FactsStable(scenario.Facts)
	scenario.Checks.StableFacts = scenario.Facts.StableFacts
	scenario.Queries = us3ApplyActualQueryFacts(t, scenario.Queries, scenario.Records)
	callByGeneration := make(map[uint64]us3Call, len(calls))
	for _, call := range calls {
		callByGeneration[call.Generation] = call
	}
	for index := range scenario.Queries {
		call, ok := callByGeneration[scenario.Queries[index].Generation]
		if !ok {
			t.Fatalf("US3 query generation %d lacks an actual call count", scenario.Queries[index].Generation)
		}
		scenario.Queries[index].NewMockCalls = uint(call.PaymentCalls)
	}
	if scenario.Joint != nil {
		for _, proof := range proofs {
			if proof.Generation == 2 && proof.Receipt != nil {
				scenario.Joint.SecondReceipt = proof.Receipt
			}
		}
		if controlled != nil && len(proofs) > 0 && proofs[0].ControlledTermination != nil {
			truncated := proofs[0].ControlledTermination
			scenario.Joint.HeldBinding = &truncated.Binding
			scenario.Joint.ProcessExit = &truncated.Exit
			scenario.Joint.ObservedPrefix = truncated.ObservedPrefix
			scenario.Joint.HeldPoint = truncated.Binding.Point
		}
		scenario.Joint.FirstMode = proofs[0].Mode
		scenario.Joint.SecondMode = proofs[len(proofs)-1].Mode
		scenario.Joint.FirstExactCounts = proofs[0].ExactCounts
		scenario.Joint.SecondExactCounts = proofs[len(proofs)-1].ExactCounts
		scenario.Joint.NoNewPaymentCallAfterRestart = calls[len(calls)-1].PaymentCalls == 0
		scenario.Joint.OriginalIntentSame = scenario.Facts.Before != nil && scenario.Facts.After != nil &&
			us3ImmutableFactsEqual(*scenario.Facts.Before, *scenario.Facts.After)
		scenario.Joint.NoExtraPaymentEffect = us3JointNoExtraPaymentEffect(
			scenario.Checks.CountsBefore, scenario.Checks.CountsAfter, scenario.Records,
			scenario.Joint.NoNewPaymentCallAfterRestart, scenario.Joint.OriginalIntentSame)
	}
	if scenario.Protection != nil && len(calls) > 0 {
		scenario.Protection.PostConfirmationMockCalls = uint(calls[len(calls)-1].PaymentCalls)
	}
	if scenario.Trace != nil && len(calls) > 0 {
		scenario.Trace.NewLookup = calls[len(calls)-1].PaymentCalls != 0
	}
	if scenario.HTTP == nil {
		scenario.HTTP = []us3HTTP{}
	}
	if scenario.PG == nil {
		scenario.PG = []us1ReportPG{}
	}
	if scenario.Calls == nil {
		scenario.Calls = []us3Call{}
	}
	if scenario.Records == nil {
		scenario.Records = []us3Record{}
	}
	if scenario.Queries == nil {
		scenario.Queries = []us3Query{}
	}
	if scenario.Faults == nil {
		scenario.Faults = []us3Fault{}
	}
	if scenario.Budgets == nil {
		scenario.Budgets = []us3Budget{}
	}
	if scenario.Commands == nil {
		scenario.Commands = []us1ReportCommand{}
	}
	if scenario.GenerationProofs == nil {
		scenario.GenerationProofs = []us3GenerationProof{}
	}
	scenario.Result = us1ReportResultPass
	us1CloseObserver(t, observer)
	us3FinishArtifacts(t, artifacts)
	us3ReportRecord(t, scenario)
}

func us3ValidateReport(report us3Report, runID string) error {
	if !us1LowerHex32(runID) || report.Schema != us3ReportSchema || report.RunID != runID {
		return errors.New("US3 report identity mismatch")
	}
	if len(report.Scenarios) != len(us3ScenarioNames) {
		return errors.New("US3 report scenario count mismatch")
	}
	for index, name := range us3ScenarioNames {
		if report.Scenarios[index].Name != name {
			return errors.New("US3 report scenario order mismatch")
		}
		if err := us3ValidateScenario(report.Scenarios[index], true); err != nil {
			return err
		}
	}
	return nil
}

func us3ValidateScenario(scenario us3Scenario, strict bool) error {
	if !us3ScenarioNameSet[scenario.Name] {
		return errors.New("invalid US3 scenario name")
	}
	if scenario.Generations == nil || scenario.HTTP == nil || scenario.PG == nil ||
		scenario.Calls == nil || scenario.Records == nil || scenario.Queries == nil ||
		scenario.Faults == nil || scenario.Budgets == nil || scenario.Commands == nil ||
		scenario.GenerationProofs == nil {
		return errors.New("US3 scenario required array is nil")
	}
	if len(scenario.HTTP) > us3HTTPMax || len(scenario.PG) > us3PGMax ||
		len(scenario.Calls) > us3CallsMax || len(scenario.Records) > us3RecordsMax ||
		len(scenario.Queries) > us3QueriesMax || len(scenario.Faults) > us3FaultsMax ||
		len(scenario.GenerationProofs) > us3ProofsMax || len(scenario.Budgets) > us3BudgetsMax ||
		len(scenario.Commands) > us3CommandsMax {
		return errors.New("US3 scenario array exceeds limit")
	}
	switch scenario.Result {
	case us1ReportResultPass, us1ReportResultFail, us1ReportResultNotRun:
	default:
		return errors.New("invalid US3 scenario result")
	}
	if !us3ValidGenerations(scenario.Name, scenario.Generations) {
		return errors.New("invalid US3 scenario generations")
	}
	switch scenario.Transport {
	case "RESPONSE", "CLIENT_CANCELED", "NO_RESPONSE":
	default:
		return errors.New("invalid US3 transport")
	}
	if scenario.Name == "lifecycle_cancel_before_mock" || scenario.Name == "lifecycle_cancel_after_mock" {
		if scenario.Transport != "CLIENT_CANCELED" || len(scenario.HTTP) != 0 {
			return errors.New("US3 lifecycle cancel must be CLIENT_CANCELED without HTTP")
		}
	}
	if scenario.Transport == "RESPONSE" {
		primary := false
		for _, observation := range scenario.HTTP {
			if observation.Kind == "PRIMARY" {
				primary = true
			}
		}
		if !primary {
			return errors.New("US3 response transport lacks a PRIMARY observation")
		}
	} else {
		for _, observation := range scenario.HTTP {
			if observation.Kind == "PRIMARY" {
				return errors.New("US3 non-response transport carried a PRIMARY observation")
			}
			if observation.Kind != "SHUTDOWN_PROBE" ||
				scenario.Name != "lifecycle_exit_budget" ||
				observation.Status != 503 ||
				!us3HasGeneration(scenario.Generations, observation.Generation) {
				return errors.New("invalid US3 SHUTDOWN_PROBE observation")
			}
		}
	}
	for _, observation := range scenario.HTTP {
		if observation.Status < 100 || observation.Status > 599 ||
			(observation.RequestID != "" && !us1LowerHexUUIDv4(observation.RequestID)) ||
			(observation.PaymentID != "" && !us1LowerHexUUIDv4(observation.PaymentID)) {
			return errors.New("invalid US3 HTTP observation")
		}
		if strict && !us3ValidHTTPKind(observation.Kind) {
			return errors.New("invalid US3 HTTP kind")
		}
		for _, issue := range observation.Issues {
			if !us3ValidIssue(issue) {
				return errors.New("invalid US3 issue")
			}
		}
	}
	for _, state := range scenario.PG {
		switch state.State {
		case "ORDER", "MOCK", "TRACE":
		default:
			return errors.New("invalid US3 PG state")
		}
	}
	for _, call := range scenario.Calls {
		if call.Generation == 0 {
			return errors.New("invalid US3 call generation")
		}
	}
	for _, record := range scenario.Records {
		if !us1LowerHexUUIDv4(record.RecordID) || !us1LowerHexUUIDv4(record.RequestID) ||
			!us3ValidSource(record.Source) || !us3ValidRecordDecision(record.Decision) ||
			!us3ValidResult(record.ResultBefore) || !us3ValidResult(record.ResultAfter) ||
			(record.ReasonCode != "" && !us3ValidReason(record.ReasonCode)) {
			return errors.New("invalid US3 record")
		}
		if len(record.Evidence.Differences) > us3DifferencesMax {
			return errors.New("US3 record differences exceed limit")
		}
		for _, difference := range record.Evidence.Differences {
			if !us3ValidDifference(difference) {
				return errors.New("invalid US3 difference")
			}
		}
	}
	notRecorded := 0
	requestIDs := make(map[string]bool, len(scenario.Queries))
	for _, query := range scenario.Queries {
		if !us1LowerHexUUIDv4(query.RequestID) ||
			!us3ValidQueryPath(query.Path) || !us3ValidQueryScenario(query.Directive) ||
			!us3ValidQueryOutcome(query.Outcome) || !us3ValidQueryDecision(query.Decision) ||
			!us3ValidTraceOutcome(query.TraceOutcome) ||
			(query.Applied && query.ApplyCount != 1) || (!query.Applied && query.ApplyCount != 0) {
			return errors.New("invalid US3 query proof")
		}
		if requestIDs[query.RequestID] {
			return errors.New("US3 query request id is duplicated")
		}
		requestIDs[query.RequestID] = true
		httpMatched := false
		for _, observation := range scenario.HTTP {
			if observation.Kind == "PRIMARY" && observation.Generation == query.Generation &&
				observation.RequestID == query.RequestID {
				httpMatched = true
			}
		}
		if !httpMatched {
			return errors.New("US3 query lacks its actual PRIMARY HTTP request identity")
		}
		if query.TraceOutcome == "SAVED" {
			record, found := us3FindRecordByRequest(scenario.Records, "QUERY", query.RequestID)
			if !found || record.Decision != query.Decision ||
				record.ResultBefore != query.ResultBefore || record.ResultAfter != query.ResultAfter {
				return errors.New("US3 SAVED query is not bound to the actual record decision/results")
			}
		}
		if query.Decision == "NOT_RECORDED" {
			notRecorded++
			if _, found := us3FindRecordByRequest(scenario.Records, "QUERY", query.RequestID); found {
				return errors.New("US3 NOT_RECORDED query unexpectedly has a handling record")
			}
			if err := us3ValidateNotRecordedQuery(scenario, query); err != nil {
				return err
			}
		} else if query.ResultBefore == "" || query.ResultAfter == "" {
			return errors.New("US3 query results missing without NOT_RECORDED")
		}
		if query.ConfirmedReturned {
			confirmedHTTP := false
			for _, observation := range scenario.HTTP {
				if observation.Generation == query.Generation &&
					(observation.Result == "SUCCEEDED" || observation.Result == "FAILED") {
					confirmedHTTP = true
				}
			}
			if !confirmedHTTP {
				return errors.New("US3 query confirmed_returned lacks a confirmed HTTP response")
			}
		}
		if query.Binding != nil && (!us1LowerHex32(query.Binding.RunID) || !us1LowerHex32(query.Binding.Epoch) ||
			query.Binding.Generation == 0 || query.Binding.CallSeq == 0 ||
			!us1ValidReportEventID(query.Binding.EventID, query.Binding.CallSeq) ||
			!us1LowerHexUUIDv4(query.Binding.OrderID)) {
			return errors.New("invalid US3 query binding")
		}
	}
	if notRecorded > 1 {
		return errors.New("US3 multiple NOT_RECORDED queries")
	}
	for _, fault := range scenario.Faults {
		if !us3ValidFaultKind(fault.Kind) || !us3ValidCleanupKind(fault.Cleanup.Kind) ||
			!us3ValidSQLState(fault.SQLState) || !us3ValidSQLStateSource(fault.SQLStateSource) ||
			!fault.Cleanup.Removed || !fault.Cleanup.Verified {
			return errors.New("invalid US3 fault/cleanup")
		}
	}
	if scenario.Facts.Transition != "" && !us3ValidTransition(scenario.Facts.Transition) {
		return errors.New("invalid US3 facts transition")
	}
	if scenario.Facts.Before != nil && !us3ValidOrderFact(*scenario.Facts.Before) {
		return errors.New("invalid US3 before fact")
	}
	if scenario.Facts.After != nil && !us3ValidOrderFact(*scenario.Facts.After) {
		return errors.New("invalid US3 after fact")
	}
	switch scenario.Facts.Transition {
	case "NONE":
		if scenario.Facts.Before != nil || scenario.Facts.After != nil {
			return errors.New("US3 NONE transition carried facts")
		}
	case "UNCHANGED":
		if scenario.Facts.Before == nil || scenario.Facts.After == nil ||
			!us3FactsEqual(*scenario.Facts.Before, *scenario.Facts.After) {
			return errors.New("US3 unchanged facts differ")
		}
	case "CREATED_UNKNOWN":
		if scenario.Facts.Before != nil || scenario.Facts.After == nil ||
			scenario.Facts.After.Result != "UNKNOWN" {
			return errors.New("US3 created-unknown facts invalid")
		}
	case "UNKNOWN_TO_CONFIRMED":
		if scenario.Facts.Before == nil || scenario.Facts.After == nil ||
			scenario.Facts.Before.Result != "UNKNOWN" ||
			scenario.Facts.After.Result != "SUCCEEDED" ||
			!us3ImmutableFactsEqual(*scenario.Facts.Before, *scenario.Facts.After) {
			return errors.New("US3 confirmed facts invalid")
		}
	}
	if scenario.Checks.StableFacts != scenario.Facts.StableFacts {
		return errors.New("US3 facts/checks stable mismatch")
	}
	if scenario.Save != nil {
		if scenario.Name == "submit_commit_unknown" {
			if scenario.Save.RecordID != "" || len(scenario.Records) != 0 {
				return errors.New("US3 submit-commit save claimed a handling record")
			}
		} else if scenario.Save.RecordID != "" {
			bound := false
			for _, record := range scenario.Records {
				if record.RecordID == scenario.Save.RecordID {
					bound = true
				}
			}
			if !bound {
				return errors.New("US3 save record is not bound to an actual record")
			}
		}
	}
	if scenario.Trace != nil && scenario.Trace.Outcome == "FAILED" {
		if scenario.Trace.SQLState == "" || scenario.Trace.SQLStateSource != "fixture_probe" {
			return errors.New("US3 trace failure lacks a fixture-probe SQLSTATE")
		}
	}
	if scenario.Shutdown != nil {
		if scenario.Shutdown.ExitUpperBoundMS != 20000 ||
			scenario.Shutdown.ObservationToleranceMS != 250 ||
			scenario.Shutdown.ExitElapsedMS > scenario.Shutdown.ExitUpperBoundMS+scenario.Shutdown.ObservationToleranceMS ||
			scenario.Shutdown.HostGraceMS != 25000 || scenario.Shutdown.HostGracePassBudget {
			return errors.New("invalid US3 shutdown budget")
		}
		switch scenario.Shutdown.PoolClosedAfterUsersSource {
		case "APP_UNIT_EVIDENCE", "OBSERVED", "":
		default:
			return errors.New("invalid US3 shutdown pool source")
		}
		if scenario.Shutdown.NoUnmanagedGoroutine &&
			scenario.Shutdown.NoUnmanagedGoroutineSource != "APP_UNIT_PLUS_GROUP_GONE" {
			return errors.New("invalid US3 no-unmanaged-goroutine source")
		}
	}
	for _, budget := range scenario.Budgets {
		if !us3ValidBudget(budget) {
			return errors.New("invalid US3 budget")
		}
	}
	for _, command := range scenario.Commands {
		switch command.Kind {
		case us1ReportCommandBuild, us1ReportCommandStop, us1ReportCommandProbe:
		default:
			return errors.New("invalid US3 command")
		}
		if strict && command.NotRun {
			return errors.New("US3 PASS command not run")
		}
	}
	if !strict {
		return nil
	}
	if us3QueryRequiredScenarios[scenario.Name] {
		if len(scenario.Queries) == 0 {
			return errors.New("US3 query scenario lacks QueryApplications proof")
		}
		for _, query := range scenario.Queries {
			if !query.Applied || query.ApplyCount != 1 || query.Binding == nil || !query.Trusted {
				return errors.New("US3 query scenario lacks applied/trusted binding proof")
			}
		}
	}
	if len(scenario.GenerationProofs) != len(scenario.Generations) {
		return errors.New("US3 generation proof count mismatch")
	}
	controlled := false
	for index, proof := range scenario.GenerationProofs {
		if proof.Generation != scenario.Generations[index] {
			return errors.New("US3 generation proof generation mismatch")
		}
		switch proof.Mode {
		case "complete":
			if !proof.Sealed || !proof.Trusted || proof.Receipt == nil ||
				proof.Counts == nil || !proof.ExactCounts || proof.ControlledTermination != nil {
				return errors.New("invalid US3 complete generation proof")
			}
		case "controlled_truncated":
			if !us3ControlledTruncatedScenarios[scenario.Name] || proof.Generation != 1 ||
				proof.Sealed || proof.Trusted || proof.Receipt != nil || proof.ExactCounts ||
				proof.ControlledTermination == nil ||
				!proof.ControlledTermination.ControlledTruncated ||
				!proof.ControlledTermination.NoReceipt ||
				proof.ControlledTermination.ExactCounts {
				return errors.New("invalid US3 controlled-truncated proof")
			}
			controlled = true
		default:
			return errors.New("invalid US3 generation proof mode")
		}
	}
	exactCounts := true
	for _, proof := range scenario.GenerationProofs {
		exactCounts = exactCounts && proof.ExactCounts
	}
	if scenario.Checks.CountsExact != exactCounts {
		return errors.New("US3 checks counts_exact does not match generation proofs")
	}
	if controlled {
		if scenario.Joint == nil || scenario.Joint.Kind != "CONTROLLED_TRUNCATED_RESTART" ||
			scenario.Joint.FirstGeneration != 1 || scenario.Joint.SecondGeneration != 2 ||
			scenario.Joint.FirstExactCounts || !scenario.Joint.SecondExactCounts ||
			scenario.Joint.SecondReceipt == nil ||
			!scenario.Joint.NoExtraPaymentEffect ||
			!scenario.Joint.NoNewPaymentCallAfterRestart ||
			!scenario.Joint.OriginalIntentSame ||
			scenario.Joint.Limitation != "CROSS_GENERATION_EXACT_COUNTS_NOT_PROVABLE" {
			return errors.New("invalid US3 joint proof")
		}
		if scenario.Name == "restart_registered_unpaid" || scenario.Name == "restart_mock_committed_reply_lost" {
			call2, ok := us3FindCall(&scenario, 2)
			if !ok || !call2.Exact || call2.PaymentCalls != 0 || call2.QueryCalls != 1 {
				return errors.New("US3 restart generation 2 lacks exact Pay0/Query1 calls")
			}
			if len(scenario.Queries) != 1 || scenario.Queries[0].Generation != 2 {
				return errors.New("US3 restart lacks exactly one generation 2 query proof")
			}
			query := scenario.Queries[0]
			record, found := us3FindRecordByRequest(scenario.Records, "QUERY", query.RequestID)
			if !found {
				return errors.New("US3 restart query lacks its actual QUERY record")
			}
			if scenario.Name == "restart_registered_unpaid" {
				if record.Decision != "KEEP_UNKNOWN" || record.ResultBefore != "UNKNOWN" ||
					record.ResultAfter != "UNKNOWN" || record.ReasonCode != "MOCK_RESULT_NOT_FOUND" {
					return errors.New("US3 registered-unpaid restart query record mismatch")
				}
			} else {
				if record.Decision != "ACCEPT_CONFIRMATION" || record.ResultBefore != "UNKNOWN" ||
					record.ResultAfter != "SUCCEEDED" || record.ReasonCode != "" ||
					record.Evidence.PlatformReferenceID == nil || record.Evidence.MockTransactionID == nil ||
					record.Evidence.Amount == nil || record.Evidence.Currency == nil || record.Evidence.Result == nil {
					return errors.New("US3 mock-committed restart query record/evidence mismatch")
				}
			}
			proof2 := scenario.GenerationProofs[len(scenario.GenerationProofs)-1]
			if proof2.Generation != 2 || proof2.Mode != "complete" || !proof2.ExactCounts ||
				proof2.Receipt == nil || proof2.Receipt.LastSeq == 0 ||
				proof2.Counts == nil || proof2.Counts.PaymentCalls != 0 || proof2.Counts.QueryCalls != 1 {
				return errors.New("US3 restart generation 2 seal counts/receipt mismatch")
			}
		}
	} else if scenario.Joint != nil {
		return errors.New("US3 joint proof without controlled truncation")
	}
	return nil
}

func us3ValidateNotRecordedQuery(scenario us3Scenario, query us3Query) error {
	if scenario.Name != "query_confirmed_trace_failure" ||
		query.Path != "BY_ID" || query.Directive != "failure" || query.Outcome != "unavailable" ||
		query.Decision != "NOT_RECORDED" || query.ResultBefore != "" || query.ResultAfter != "" ||
		query.TraceOutcome != "FAILED" || !query.ConfirmedReturned {
		return errors.New("US3 NOT_RECORDED query is outside the trace-failure exception")
	}
	if scenario.Trace == nil || scenario.Trace.Outcome != "FAILED" ||
		scenario.Trace.Issue != "TRACE_SAVE_ERROR" ||
		scenario.Trace.SQLState == "" || scenario.Trace.SQLStateSource != "fixture_probe" {
		return errors.New("US3 NOT_RECORDED query lacks the actual trace-failure proof")
	}
	traceFault := false
	for _, fault := range scenario.Faults {
		if fault.Kind == "TRACE_INSERT_SQL" && fault.Cleanup.Verified && fault.TargetID != nil {
			traceFault = true
		}
	}
	if !traceFault {
		return errors.New("US3 NOT_RECORDED query lacks the verified trace fixture fault")
	}
	traceIssue := false
	confirmedHTTP := false
	for _, observation := range scenario.HTTP {
		if observation.Kind == "PRIMARY" && observation.RequestID == query.RequestID {
			if us3ContainsString(observation.Issues, "TRACE_SAVE_ERROR") {
				traceIssue = true
			}
			if observation.Result == "SUCCEEDED" {
				confirmedHTTP = true
			}
		}
	}
	if !traceIssue || !confirmedHTTP {
		return errors.New("US3 NOT_RECORDED query lacks actual same-request HTTP confirmation/trace issue")
	}
	acceptBound := false
	for _, record := range scenario.Records {
		if record.Decision != "ACCEPT_CONFIRMATION" {
			continue
		}
		for _, candidate := range scenario.Queries {
			if candidate.RequestID == record.RequestID &&
				candidate.Decision == "ACCEPT_CONFIRMATION" &&
				candidate.TraceOutcome == "SAVED" && candidate.Directive == "normal" {
				acceptBound = true
			}
		}
	}
	if !acceptBound {
		return errors.New("US3 NOT_RECORDED query lacks the later actual ACCEPT query/record")
	}
	if scenario.Facts.Transition != "UNKNOWN_TO_CONFIRMED" {
		return errors.New("US3 NOT_RECORDED query lacks stable confirmed order facts")
	}
	return nil
}

func us3ValidGenerations(name string, generations []uint64) bool {
	switch name {
	case "restart_registered_unpaid", "restart_mock_committed_reply_lost",
		"restart_unknown_no_confirm", "restart_unknown_then_confirmed":
		return len(generations) == 2 && generations[0] == 1 && generations[1] == 2
	default:
		return len(generations) == 1 && generations[0] == 1
	}
}

func us3ValidResult(value string) bool {
	return value == "" || value == "UNKNOWN" || value == "SUCCEEDED" || value == "FAILED"
}
func us3ValidSource(value string) bool { return value == "PAYMENT" || value == "QUERY" }
func us3ValidRecordDecision(value string) bool {
	switch value {
	case "ACCEPT_CONFIRMATION", "KEEP_UNKNOWN", "KEEP_CONFIRMED", "REJECT_EVIDENCE":
		return true
	default:
		return false
	}
}
func us3ValidQueryDecision(value string) bool {
	return value == "NOT_RECORDED" || us3ValidRecordDecision(value)
}
func us3ValidReason(value string) bool {
	switch value {
	case "MOCK_EVIDENCE_INVALID", "MOCK_EVIDENCE_MISMATCH", "MOCK_CONFIRMATION_CONFLICT",
		"MOCK_RESULT_UNCONFIRMED", "MOCK_RESULT_NOT_FOUND", "CONFIRMED_RESULT_PRESERVED":
		return true
	default:
		return false
	}
}
func us3ValidIssue(value string) bool {
	switch value {
	case "MOCK_RESULT_UNCONFIRMED", "MOCK_RESULT_NOT_FOUND", "MOCK_EVIDENCE_REJECTED",
		"RESULT_SAVE_ERROR", "TRACE_SAVE_ERROR":
		return true
	default:
		return false
	}
}
func us3ValidDifference(value string) bool {
	switch value {
	case "platform_reference_id", "mock_transaction_id", "amount", "currency", "result":
		return true
	default:
		return false
	}
}
func us3ValidQueryPath(value string) bool { return value == "BY_ID" || value == "BY_BUSINESS_ORDER_NO" }
func us3ValidQueryScenario(value string) bool {
	switch value {
	case "normal", "failure", "subtimeout", "notvisible", "mismatch_reference",
		"mismatch_amount", "mismatch_currency", "mismatch_transaction":
		return true
	default:
		return false
	}
}
func us3ValidQueryOutcome(value string) bool {
	switch value {
	case "found", "not_found", "unavailable", "subtimeout", "mismatch_applied":
		return true
	default:
		return false
	}
}
func us3ValidTraceOutcome(value string) bool {
	switch value {
	case "SAVED", "FAILED", "INDETERMINATE", "NOT_ATTEMPTED":
		return true
	default:
		return false
	}
}
func us3ValidFaultKind(value string) bool {
	switch value {
	case "MOCK_INSERT_WAITER_HOLD", "REGISTRATION_INSERT_HOLD", "RESULT_HANDLING_INSERT_HOLD",
		"REGISTRATION_COMMIT_REPLY_LOST", "RESULT_SAVE_COMMIT_REPLY_LOST", "RESULT_COMMIT_REPLY_HIDDEN",
		"MOCK_QUERY_FAILURE", "MOCK_QUERY_SUBTIMEOUT_250MS", "MOCK_QUERY_NOT_VISIBLE",
		"MISMATCH_REFERENCE", "MISMATCH_AMOUNT", "MISMATCH_CURRENCY", "MISMATCH_TRANSACTION",
		"TRACE_INSERT_SQL", "PROCESS_TERMINATED", "CLIENT_CANCEL", "SHUTDOWN_ACTIVE_REQUEST":
		return true
	default:
		return false
	}
}
func us3ValidHTTPKind(value string) bool {
	return value == "PRIMARY" || value == "SHUTDOWN_PROBE"
}

func us3HasGeneration(generations []uint64, generation uint64) bool {
	for _, value := range generations {
		if value == generation {
			return true
		}
	}
	return false
}

func us3ValidCleanupKind(value string) bool {
	switch value {
	case "NONE", "UDS_SCENARIO", "TRIGGER_FUNCTION", "ROW_LOCK", "TABLE_SHARE_LOCK", "PROCESS_TERMINAL", "TEMP_DB":
		return true
	default:
		return false
	}
}
func us3ValidSQLState(value string) bool {
	switch value {
	case "", "00000", "23505", "40001", "57014", "P0001", "08006", "57P01":
		return true
	default:
		return false
	}
}
func us3ValidSQLStateSource(value string) bool {
	return value == "" || value == "fixture_probe" || value == "application_observed" || value == "none"
}
func us3ValidTransition(value string) bool {
	switch value {
	case "NONE", "CREATED_UNKNOWN", "UNKNOWN_TO_CONFIRMED", "UNCHANGED":
		return true
	default:
		return false
	}
}
func us3ValidSHA256(value string) bool {
	if len(value) != 64 {
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

func us3ValidOrderFact(fact us3OrderFact) bool {
	return us1LowerHexUUIDv4(fact.PaymentID) &&
		us3ValidSHA256(fact.ClientRefSHA256) && us3ValidSHA256(fact.BusinessRefSHA256) &&
		fact.Currency == "CNY" && us3ValidResult(fact.Result) &&
		fact.RegisteredAt != ""
}

func us3StringPointerEqual(left, right *string) bool {
	switch {
	case left == nil && right == nil:
		return true
	case left != nil && right != nil:
		return *left == *right
	default:
		return false
	}
}

func us3FactsEqual(left, right us3OrderFact) bool {
	return left.PaymentID == right.PaymentID &&
		left.ClientRefSHA256 == right.ClientRefSHA256 &&
		left.BusinessRefSHA256 == right.BusinessRefSHA256 &&
		left.Amount == right.Amount &&
		left.Currency == right.Currency &&
		left.Result == right.Result &&
		left.RegisteredAt == right.RegisteredAt &&
		us3StringPointerEqual(left.ConfirmedAt, right.ConfirmedAt) &&
		us3StringPointerEqual(left.MockTransactionID, right.MockTransactionID)
}

func us3ImmutableFactsEqual(left, right us3OrderFact) bool {
	return left.PaymentID == right.PaymentID &&
		left.ClientRefSHA256 == right.ClientRefSHA256 &&
		left.BusinessRefSHA256 == right.BusinessRefSHA256 &&
		left.Amount == right.Amount &&
		left.Currency == right.Currency &&
		left.RegisteredAt == right.RegisteredAt
}

func us3FactsStable(facts us3Facts) bool {
	switch facts.Transition {
	case "UNCHANGED":
		return facts.Before != nil && facts.After != nil &&
			us3FactsEqual(*facts.Before, *facts.After)
	case "UNKNOWN_TO_CONFIRMED":
		return facts.Before != nil && facts.After != nil &&
			us3ImmutableFactsEqual(*facts.Before, *facts.After)
	default:
		return false
	}
}

func us3JointNoExtraPaymentEffect(before, after us3Counts, records []us3Record, noNewPayment, originalIntentSame bool) bool {
	if !noNewPayment || !originalIntentSame ||
		before.OrderRows != after.OrderRows || before.MockRows != after.MockRows {
		return false
	}
	queryRecords := 0
	for _, record := range records {
		switch record.Source {
		case "QUERY":
			queryRecords++
		case "PAYMENT":
			return false
		}
	}
	if after.TraceRows < before.TraceRows {
		return false
	}
	return int(after.TraceRows-before.TraceRows) == queryRecords
}

func us3HeldPostResponseBeforeHold(responseAt, holdAt time.Time) bool {
	return responseAt.Before(holdAt)
}

func us3ActualTransition(t *testing.T, before, after us3OrderFact) string {
	t.Helper()
	if us3FactsEqual(before, after) {
		return "UNCHANGED"
	}
	if before.Result == "UNKNOWN" && after.Result == "SUCCEEDED" &&
		us3ImmutableFactsEqual(before, after) {
		return "UNKNOWN_TO_CONFIRMED"
	}
	t.Fatalf("US3 actual order facts do not form a supported transition: before=%s after=%s",
		before.Result, after.Result)
	return ""
}
func us3ValidBudget(budget us3Budget) bool {
	if budget.PassBudget && budget.ElapsedMS > budget.LimitMS {
		return false
	}
	switch budget.Kind {
	case "QUERY_SUBTIMEOUT":
		if budget.Class != "CONTROLLED" || budget.PassBudget || budget.LimitMS != 250 {
			return false
		}
	case "MOCK_DB_SUBTIMEOUT":
		if budget.Class != "CONTROLLED" || budget.PassBudget || budget.LimitMS != 2000 {
			return false
		}
	case "MOCK":
		if budget.Class != "NORMAL" || budget.Source != "MOCK" || !budget.PassBudget || budget.LimitMS != 3000 {
			return false
		}
	case "EXIT":
		if budget.Class != "NORMAL" || budget.Source != "APP" || !budget.PassBudget || budget.LimitMS != 20000 {
			return false
		}
	case "PROCESS_GRACE":
		if budget.Class != "NORMAL" || budget.Source != "HARNESS" || budget.PassBudget || budget.LimitMS != 25000 {
			return false
		}
	}
	switch budget.Class {
	case "NORMAL", "CONTROLLED":
	default:
		return false
	}
	switch budget.Source {
	case "APP", "HARNESS", "UNIT", "MOCK":
	default:
		return false
	}
	return true
}

type us3ReportCollector struct {
	mu        sync.Mutex
	root      string
	runID     string
	path      string
	scenarios map[string]us3Scenario
}

var (
	us3ProcessReportMu        sync.Mutex
	us3ProcessReportCollector *us3ReportCollector
)

func us3ReportPath(root, runID string) (string, error) {
	if err := us1ValidateArtifactRoot(root); err != nil {
		return "", err
	}
	if !us1LowerHex32(runID) {
		return "", errors.New("US3 report run id is invalid")
	}
	path := filepath.Join(root, runID, "us3.json")
	rel, err := filepath.Rel(root, path)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", errors.New("US3 report path escapes root")
	}
	return path, nil
}

func us3NewReportCollector(root, runID string) (*us3ReportCollector, error) {
	path, err := us3ReportPath(root, runID)
	if err != nil {
		return nil, err
	}
	runDir := filepath.Dir(path)
	if err := os.Mkdir(runDir, 0o700); err != nil && !errors.Is(err, os.ErrExist) {
		return nil, err
	}
	if err := us1ValidateArtifactRoot(runDir); err != nil {
		return nil, err
	}
	if _, err := os.Lstat(path); err == nil {
		return nil, errors.New("US3 report already exists")
	}
	return &us3ReportCollector{root: root, runID: runID, path: path, scenarios: make(map[string]us3Scenario)}, nil
}

func us3WriteScenarioEvidence(t *testing.T, runID, scenario, name string, value any) {
	t.Helper()
	collector := us3ReportInit(t, runID)
	dir := filepath.Join(collector.root, runID, "scenarios", "us3", scenario)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatalf("US3 evidence directory creation failed")
	}
	encoded, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		t.Fatalf("US3 evidence encode failed")
	}
	path := filepath.Join(dir, name+".json")
	if err := os.WriteFile(path, append(encoded, '\n'), 0o600); err != nil {
		t.Fatalf("US3 evidence write failed")
	}
}

func us3ReportInit(t *testing.T, runID string) *us3ReportCollector {
	t.Helper()
	root := os.Getenv(us3ArtifactRootEnv)
	if root == "" {
		t.Fatalf("US3 report artifact root configuration %s is required", us3ArtifactRootEnv)
	}
	us3ProcessReportMu.Lock()
	defer us3ProcessReportMu.Unlock()
	if us3ProcessReportCollector != nil {
		if us3ProcessReportCollector.root != root || us3ProcessReportCollector.runID != runID {
			t.Fatal("US3 report collector identity changed")
		}
		return us3ProcessReportCollector
	}
	collector, err := us3NewReportCollector(root, runID)
	if err != nil {
		t.Fatal("US3 report collector initialization failed")
	}
	us3ProcessReportCollector = collector
	return collector
}

func us3ReportRecord(t *testing.T, scenario *us3Scenario) {
	t.Helper()
	us3ProcessReportMu.Lock()
	collector := us3ProcessReportCollector
	us3ProcessReportMu.Unlock()
	if collector == nil {
		t.Fatal("US3 report collector is not initialized")
	}
	if err := collector.record(*scenario); err != nil {
		t.Fatal("US3 report scenario record failed")
	}
}

func (collector *us3ReportCollector) record(scenario us3Scenario) error {
	collector.mu.Lock()
	defer collector.mu.Unlock()
	if _, exists := collector.scenarios[scenario.Name]; exists {
		return errors.New("duplicate US3 report scenario")
	}
	if err := us3ValidateScenario(scenario, scenario.Result == us1ReportResultPass); err != nil {
		return err
	}
	collector.scenarios[scenario.Name] = scenario
	return collector.writeLocked()
}

func (collector *us3ReportCollector) writeLocked() error {
	report := us3Report{Schema: us3ReportSchema, RunID: collector.runID, Scenarios: make([]us3Scenario, 0, len(collector.scenarios))}
	for _, name := range us3ScenarioNames {
		if scenario, ok := collector.scenarios[name]; ok {
			report.Scenarios = append(report.Scenarios, scenario)
		}
	}
	encoded, err := json.Marshal(report)
	if err != nil {
		return err
	}
	if len(encoded) > us3ReportMaxBytes {
		return errors.New("US3 report exceeds size bound")
	}
	dir := filepath.Dir(collector.path)
	tmp, err := os.CreateTemp(dir, ".us3-*.tmp")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	remove := true
	defer func() {
		if remove {
			_ = os.Remove(tmpName)
		}
	}()
	if err := tmp.Chmod(0o600); err != nil {
		_ = tmp.Close()
		return err
	}
	if _, err := tmp.Write(encoded); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpName, collector.path); err != nil {
		return err
	}
	remove = false
	return nil
}

func us3ReadReport(path string) (us3Report, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return us3Report{}, err
	}
	if len(data) == 0 || len(data) > us3ReportMaxBytes {
		return us3Report{}, errors.New("US3 report size invalid")
	}
	var report us3Report
	if err := json.Unmarshal(data, &report); err != nil {
		return us3Report{}, err
	}
	return report, nil
}
