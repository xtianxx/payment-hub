//go:build integration

package harness

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"
)

// committedPoolMaxConns keeps the helper pool small; each test opens its own
// independent pool and never relies on a shared process-wide pool.
const committedPoolMaxConns = 4

// OpenCommittedPostgresPool opens a pooled connection source for the
// launcher-provided isolated test database. It reuses the existing
// openPostgresConfig and verifyPostgres checks for the DSN, run ID, PG16.15,
// UTC, UTF8, statement timeout, and search path. Unlike OpenPostgres, it does
// not start an outer transaction: the pool is only a connection source, and
// each short transaction is owned and committed by the code under test.
//
// The helper never starts Docker, creates a database, runs migrations, or
// touches a development database. Cleanup closes the pool with a bounded wait.
func OpenCommittedPostgresPool(t testing.TB) *pgxpool.Pool {
	t.Helper()

	config := openPostgresConfig(t)

	// pgxpool.NewWithConfig requires a config created by ParseConfig. The
	// parse only supplies pgxpool's internal defaults and creation flag; the
	// validated connection-critical fields are then taken from config so no
	// ambient or URL query parameter can choose another target.
	poolConfig, err := pgxpool.ParseConfig(config.ConnString())
	if err != nil {
		t.Fatalf("test database pool configuration failed")
	}
	poolConfig.ConnConfig = config
	poolConfig.MaxConns = committedPoolMaxConns
	poolConfig.MinConns = 0
	poolConfig.AfterConnect = func(_ context.Context, conn *pgx.Conn) error {
		return configureCommittedUTCTimestamps(conn)
	}

	connectCtx, cancelConnect := context.WithTimeout(context.Background(), connectBudget)
	pool, err := pgxpool.NewWithConfig(connectCtx, poolConfig)
	cancelConnect()
	if err != nil {
		t.Fatalf("test database pool creation failed (%s)", postgresErrorCategory(err))
	}
	// Register bounded close before verification so a verification Fatal still
	// closes the successfully created pool.
	t.Cleanup(func() { cleanupCommittedPostgresPool(t, pool) })

	verifyCtx, cancelVerify := context.WithTimeout(context.Background(), verifyBudget)
	conn, err := pool.Acquire(verifyCtx)
	if err != nil {
		cancelVerify()
		t.Fatalf("test database pool connection failed (%s)", postgresErrorCategory(err))
	}
	check, err := verifyPostgres(conn.Conn(), verifyCtx, config.Database)
	if err == nil && check == "" && conn.Conn().PgConn().TxStatus() != 'I' {
		check = "outer transaction"
	}
	conn.Release()
	cancelVerify()
	if err != nil {
		t.Fatalf("test database pool verification failed: %s (%s)", check, postgresErrorCategory(err))
	}
	if check != "" {
		t.Fatalf("test database pool verification failed: %s", check)
	}
	return pool
}

// configureCommittedUTCTimestamps installs a per-connection timestamptz scan
// location of UTC. The existing configureUTCTimestamps helper reports through
// testing.TB, which is not appropriate inside pgxpool's AfterConnect callback;
// this callback returns an error instead so pool acquisition fails cleanly.
func configureCommittedUTCTimestamps(conn *pgx.Conn) error {
	existing, ok := conn.TypeMap().TypeForName("timestamptz")
	if !ok {
		return errors.New("timestamptz codec is unavailable")
	}
	codec, ok := existing.Codec.(*pgtype.TimestamptzCodec)
	if !ok {
		return errors.New("timestamptz codec is unavailable")
	}
	codecCopy := *codec
	codecCopy.ScanLocation = time.UTC
	conn.TypeMap().RegisterType(&pgtype.Type{
		Name:  existing.Name,
		OID:   existing.OID,
		Codec: &codecCopy,
	})
	return nil
}

// cleanupCommittedPostgresPool closes the pool with a bounded wait. pgxpool's
// Close has no context parameter and waits for acquired connections, so the
// close runs in a goroutine and the test reports if it exceeds the existing
// cleanup budget.
func cleanupCommittedPostgresPool(t testing.TB, pool *pgxpool.Pool) {
	t.Helper()
	if pool == nil {
		return
	}
	done := make(chan struct{})
	go func() {
		pool.Close()
		close(done)
	}()
	ctx, cancel := context.WithTimeout(context.Background(), cleanupBudget)
	defer cancel()
	select {
	case <-done:
	case <-ctx.Done():
		t.Errorf("test database pool cleanup exceeded the bounded window")
	}
}
