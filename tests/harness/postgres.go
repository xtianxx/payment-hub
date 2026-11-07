//go:build integration

// Package harness provides integration-test fixtures for the isolated real
// PostgreSQL test database. It does not start containers, create databases, or
// run migrations; the one-shot launcher owns those steps.
package harness

import (
	"context"
	"errors"
	"net"
	"net/url"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"

	"payment-hub/internal/payment"
)

const (
	testDatabaseURLVariable = "PAYMENT_HUB_TEST_DATABASE_URL"
	testRunIDVariable       = "PAYMENT_HUB_TEST_RUN_ID"
	testDatabaseNamePrefix  = "payment_hub_test_"

	expectedServerVersionNum = "160015"

	connectBudget = 5 * time.Second
	verifyBudget  = 5 * time.Second
	beginBudget   = 5 * time.Second
	cleanupBudget = 3 * time.Second
	fixtureBudget = 10 * time.Second
)

const (
	insertFixtureOrderSQL = `INSERT INTO payment_orders
		(id, client_id, business_order_no, amount, currency, result, mock_transaction_id, registered_at, confirmed_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`

	insertFixtureMockSQL = `INSERT INTO mock_transactions
		(id, platform_reference_id, amount, currency, result, processed_at)
		VALUES ($1, $2, $3, $4, $5, $6)`
)

// Fixtures is the fixed set of platform orders and independent Mock facts
// inserted by SeedFixtures. The three Mock IDs are generated separately from
// the three platform order IDs.
type Fixtures struct {
	Unknown, Succeeded, Failed             payment.PaymentOrder
	UnknownMock, SucceededMock, FailedMock payment.MockTransaction
}

// OpenPostgres connects to the already-created, already-migrated database named
// by PAYMENT_HUB_TEST_DATABASE_URL and PAYMENT_HUB_TEST_RUN_ID. It never starts
// Docker, creates the database, or runs migrations. The returned connection is
// inside a transaction that cleanup always rolls back.
func OpenPostgres(t testing.TB) *pgx.Conn {
	t.Helper()

	config := openPostgresConfig(t)

	connectCtx, cancelConnect := context.WithTimeout(context.Background(), connectBudget)
	conn, err := pgx.ConnectConfig(connectCtx, config)
	cancelConnect()
	if err != nil {
		t.Fatalf("test database connection failed (%s)", postgresErrorCategory(err))
	}
	t.Cleanup(func() { cleanupPostgres(t, conn) })
	configureUTCTimestamps(t, conn)

	verifyCtx, cancelVerify := context.WithTimeout(context.Background(), verifyBudget)
	check, err := verifyPostgres(conn, verifyCtx, config.Database)
	cancelVerify()
	if err != nil {
		t.Fatalf("test database verification failed: %s (%s)", check, postgresErrorCategory(err))
	}
	if check != "" {
		t.Fatalf("test database verification failed: %s", check)
	}

	beginCtx, cancelBegin := context.WithTimeout(context.Background(), beginBudget)
	_, err = conn.Exec(beginCtx, "BEGIN")
	cancelBegin()
	if err != nil {
		t.Fatalf("test database transaction start failed (%s)", postgresErrorCategory(err))
	}
	return conn
}

// configureUTCTimestamps makes pgx scan timestamptz values in UTC regardless
// of the host process timezone. It registers a connection-local codec override
// instead of mutating the shared default type map.
func configureUTCTimestamps(t testing.TB, conn *pgx.Conn) {
	t.Helper()
	existing, ok := conn.TypeMap().TypeForName("timestamptz")
	if !ok {
		t.Fatalf("test database configuration: timestamptz codec is unavailable")
	}
	codec, ok := existing.Codec.(*pgtype.TimestamptzCodec)
	if !ok {
		t.Fatalf("test database configuration: timestamptz codec is unavailable")
	}
	codecCopy := *codec
	codecCopy.ScanLocation = time.UTC
	conn.TypeMap().RegisterType(&pgtype.Type{
		Name:  existing.Name,
		OID:   existing.OID,
		Codec: &codecCopy,
	})
}

// openPostgresConfig validates the launcher-provided DSN and returns a config
// with deterministic connection-critical fields and session settings. The DSN
// must be a postgres:// or postgresql:// URL with an explicit loopback host,
// non-zero port, user, password, and the run database name.
func openPostgresConfig(t testing.TB) *pgx.ConnConfig {
	t.Helper()

	dsn := os.Getenv(testDatabaseURLVariable)
	if dsn == "" {
		t.Fatalf("test database configuration: %s is required", testDatabaseURLVariable)
	}
	runID := os.Getenv(testRunIDVariable)
	if !isLowerHex32(runID) {
		t.Fatalf("test database configuration: %s must be 32 lowercase hex characters", testRunIDVariable)
	}
	expectedDatabase := testDatabaseNamePrefix + runID

	parsed, err := url.Parse(dsn)
	if err != nil || (parsed.Scheme != "postgres" && parsed.Scheme != "postgresql") || parsed.Opaque != "" || parsed.Fragment != "" {
		t.Fatalf("test database configuration: database URL is invalid")
	}

	// Only loopback targets are accepted. "localhost" is pinned to the IPv4
	// loopback literal so a resolver cannot redirect the test connection.
	host := parsed.Hostname()
	switch {
	case host == "127.0.0.1" || host == "::1":
	case strings.EqualFold(host, "localhost"):
		host = "127.0.0.1"
	default:
		t.Fatalf("test database configuration: database URL must use a loopback host")
	}
	portText := parsed.Port()
	if portText == "" {
		t.Fatalf("test database configuration: database URL must include an explicit port")
	}
	port, err := strconv.ParseUint(portText, 10, 16)
	if err != nil || port == 0 {
		t.Fatalf("test database configuration: database URL port is invalid")
	}
	if parsed.User == nil {
		t.Fatalf("test database configuration: database URL must include explicit credentials")
	}
	user := parsed.User.Username()
	password, hasPassword := parsed.User.Password()
	if user == "" || !hasPassword || password == "" {
		t.Fatalf("test database configuration: database URL must include explicit credentials")
	}
	if parsed.Path != "/"+expectedDatabase {
		t.Fatalf("test database configuration: database URL must target the run database")
	}

	// Rebuild a minimal URL so query parameters cannot choose another target.
	// pgx may read ambient PG* defaults for absent keys, but every
	// connection-critical field and runtime parameter is overwritten below.
	sanitized := url.URL{
		Scheme: "postgres",
		User:   url.UserPassword(user, password),
		Host:   net.JoinHostPort(host, portText),
		Path:   "/" + expectedDatabase,
	}
	config, err := pgx.ParseConfig(sanitized.String())
	if err != nil {
		t.Fatalf("test database configuration: database URL is invalid")
	}

	config.Host = host
	config.Port = uint16(port)
	config.Database = expectedDatabase
	config.User = user
	config.Password = password
	config.TLSConfig = nil
	config.Fallbacks = nil
	config.ValidateConnect = nil
	config.ConnectTimeout = connectBudget
	config.RequireAuth = ""
	config.ChannelBinding = "prefer"
	config.SSLNegotiation = ""
	config.MinProtocolVersion = "3.0"
	config.MaxProtocolVersion = "3.0"
	config.RuntimeParams = map[string]string{
		"timezone":          "UTC",
		"search_path":       "public",
		"statement_timeout": "2000",
	}
	config.Tracer = nil
	return config
}

func isLowerHex32(value string) bool {
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

// verifyPostgres checks that the live connection really targets the expected
// fresh database with the required session settings. A non-empty check name
// means a fixed, sanitized mismatch category.
func verifyPostgres(conn *pgx.Conn, ctx context.Context, expectedDatabase string) (string, error) {
	var database, version, timezone, serverEncoding, clientEncoding, statementTimeout, searchPath string
	err := conn.QueryRow(ctx, `
		SELECT current_database(),
		       current_setting('server_version_num'),
		       current_setting('TimeZone'),
		       current_setting('server_encoding'),
		       current_setting('client_encoding'),
		       (SELECT setting FROM pg_settings WHERE name = 'statement_timeout'),
		       current_setting('search_path')
	`).Scan(&database, &version, &timezone, &serverEncoding, &clientEncoding, &statementTimeout, &searchPath)
	if err != nil {
		return "query", err
	}
	switch {
	case database != expectedDatabase:
		return "database name", nil
	case version != expectedServerVersionNum:
		return "server version", nil
	case timezone != "UTC":
		return "timezone", nil
	case serverEncoding != "UTF8":
		return "server encoding", nil
	case clientEncoding != "UTF8":
		return "client encoding", nil
	case statementTimeout != "2000":
		return "statement timeout", nil
	case searchPath != "public":
		return "search path", nil
	}
	return "", nil
}

// cleanupPostgres uses independent short contexts because the test context may
// already be canceled. It never retries and reports only sanitized categories.
func cleanupPostgres(t testing.TB, conn *pgx.Conn) {
	rollbackCtx, cancelRollback := context.WithTimeout(context.Background(), cleanupBudget)
	_, rollbackErr := conn.Exec(rollbackCtx, "ROLLBACK")
	cancelRollback()
	if rollbackErr != nil {
		t.Errorf("test database cleanup rollback failed (%s)", postgresErrorCategory(rollbackErr))
	}

	closeCtx, cancelClose := context.WithTimeout(context.Background(), cleanupBudget)
	closeErr := conn.Close(closeCtx)
	cancelClose()
	if closeErr != nil {
		t.Errorf("test database cleanup close failed (%s)", postgresErrorCategory(closeErr))
	}
}

// postgresErrorCategory returns a fixed category or the SQLSTATE only. It never
// returns the original error text, which may contain the DSN or SQL.
func postgresErrorCategory(err error) string {
	if err == nil {
		return "none"
	}
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return "SQLSTATE " + pgErr.Code
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return "timeout"
	}
	if errors.Is(err, context.Canceled) {
		return "canceled"
	}
	return "connection error"
}

// SeedFixtures inserts three platform orders and three independent Mock facts
// into the caller's already-open transaction. It never commits, never updates
// the platform result from Mock data, and never reuses a unique key. Every call
// uses fresh business keys so concurrent package test processes cannot collide.
func SeedFixtures(t testing.TB, conn *pgx.Conn) Fixtures {
	t.Helper()
	if conn == nil {
		t.Fatalf("fixture seeding: database connection is required")
	}
	if status := conn.PgConn().TxStatus(); status != 'T' {
		t.Fatalf("fixture seeding: connection is not in a transaction")
	}

	callID := payment.NewUUIDv4()
	clientID := "fixture-client-" + callID
	unknownBusinessOrderNo := "fixture-unknown-" + callID
	succeededBusinessOrderNo := "fixture-succeeded-" + callID
	failedBusinessOrderNo := "fixture-failed-" + callID
	base := time.Now().UTC().Truncate(time.Microsecond)

	unknownID := payment.NewUUIDv4()
	succeededID := payment.NewUUIDv4()
	failedID := payment.NewUUIDv4()
	unknownMockID := payment.NewUUIDv4()
	succeededMockID := payment.NewUUIDv4()
	failedMockID := payment.NewUUIDv4()

	unknownRegisteredAt := base
	succeededRegisteredAt := base.Add(time.Second)
	failedRegisteredAt := base.Add(2 * time.Second)
	succeededConfirmedAt := base.Add(time.Minute)
	failedConfirmedAt := base.Add(2 * time.Minute)
	unknownMockProcessedAt := base.Add(10 * time.Second)
	succeededMockProcessedAt := base.Add(11 * time.Second)
	failedMockProcessedAt := base.Add(12 * time.Second)

	ctx, cancel := context.WithTimeout(context.Background(), fixtureBudget)
	defer cancel()

	// UNKNOWN has no confirmed_at and no Mock association.
	if _, err := conn.Exec(ctx, insertFixtureOrderSQL,
		unknownID, clientID, unknownBusinessOrderNo, int64(1000), "CNY",
		string(payment.ResultUnknown), nil, unknownRegisteredAt, nil,
	); err != nil {
		t.Fatalf("fixture seeding: unknown order insert failed (%s)", postgresErrorCategory(err))
	}
	if _, err := conn.Exec(ctx, insertFixtureOrderSQL,
		succeededID, clientID, succeededBusinessOrderNo, int64(2500), "CNY",
		string(payment.ResultSucceeded), succeededMockID, succeededRegisteredAt, succeededConfirmedAt,
	); err != nil {
		t.Fatalf("fixture seeding: succeeded order insert failed (%s)", postgresErrorCategory(err))
	}
	if _, err := conn.Exec(ctx, insertFixtureOrderSQL,
		failedID, clientID, failedBusinessOrderNo, int64(750), "CNY",
		string(payment.ResultFailed), failedMockID, failedRegisteredAt, failedConfirmedAt,
	); err != nil {
		t.Fatalf("fixture seeding: failed order insert failed (%s)", postgresErrorCategory(err))
	}

	// Mock facts carry their own explicitly supplied amount, currency, result,
	// and processed_at. They are never selected from the platform order table.
	if _, err := conn.Exec(ctx, insertFixtureMockSQL,
		unknownMockID, unknownID, int64(1000), "CNY",
		string(payment.ResultSucceeded), unknownMockProcessedAt,
	); err != nil {
		t.Fatalf("fixture seeding: unknown mock insert failed (%s)", postgresErrorCategory(err))
	}
	if _, err := conn.Exec(ctx, insertFixtureMockSQL,
		succeededMockID, succeededID, int64(2500), "CNY",
		string(payment.ResultSucceeded), succeededMockProcessedAt,
	); err != nil {
		t.Fatalf("fixture seeding: succeeded mock insert failed (%s)", postgresErrorCategory(err))
	}
	if _, err := conn.Exec(ctx, insertFixtureMockSQL,
		failedMockID, failedID, int64(750), "CNY",
		string(payment.ResultFailed), failedMockProcessedAt,
	); err != nil {
		t.Fatalf("fixture seeding: failed mock insert failed (%s)", postgresErrorCategory(err))
	}

	succeededMockRef := payment.MockTransactionID(succeededMockID)
	failedMockRef := payment.MockTransactionID(failedMockID)
	return Fixtures{
		Unknown: payment.PaymentOrder{
			ID:                payment.PaymentOrderID(unknownID),
			ClientID:          clientID,
			BusinessOrderNo:   unknownBusinessOrderNo,
			Amount:            1000,
			Currency:          "CNY",
			Result:            payment.ResultUnknown,
			MockTransactionID: nil,
			RegisteredAt:      unknownRegisteredAt,
			ConfirmedAt:       nil,
		},
		Succeeded: payment.PaymentOrder{
			ID:                payment.PaymentOrderID(succeededID),
			ClientID:          clientID,
			BusinessOrderNo:   succeededBusinessOrderNo,
			Amount:            2500,
			Currency:          "CNY",
			Result:            payment.ResultSucceeded,
			MockTransactionID: &succeededMockRef,
			RegisteredAt:      succeededRegisteredAt,
			ConfirmedAt:       &succeededConfirmedAt,
		},
		Failed: payment.PaymentOrder{
			ID:                payment.PaymentOrderID(failedID),
			ClientID:          clientID,
			BusinessOrderNo:   failedBusinessOrderNo,
			Amount:            750,
			Currency:          "CNY",
			Result:            payment.ResultFailed,
			MockTransactionID: &failedMockRef,
			RegisteredAt:      failedRegisteredAt,
			ConfirmedAt:       &failedConfirmedAt,
		},
		UnknownMock: payment.MockTransaction{
			ID:                  payment.MockTransactionID(unknownMockID),
			PlatformReferenceID: payment.PaymentOrderID(unknownID),
			Amount:              1000,
			Currency:            "CNY",
			Result:              payment.ResultSucceeded,
			ProcessedAt:         unknownMockProcessedAt,
		},
		SucceededMock: payment.MockTransaction{
			ID:                  payment.MockTransactionID(succeededMockID),
			PlatformReferenceID: payment.PaymentOrderID(succeededID),
			Amount:              2500,
			Currency:            "CNY",
			Result:              payment.ResultSucceeded,
			ProcessedAt:         succeededMockProcessedAt,
		},
		FailedMock: payment.MockTransaction{
			ID:                  payment.MockTransactionID(failedMockID),
			PlatformReferenceID: payment.PaymentOrderID(failedID),
			Amount:              750,
			Currency:            "CNY",
			Result:              payment.ResultFailed,
			ProcessedAt:         failedMockProcessedAt,
		},
	}
}
