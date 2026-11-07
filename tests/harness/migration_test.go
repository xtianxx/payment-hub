//go:build integration

package harness

import (
	"context"
	"errors"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

func sqlStateOf(err error) string {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code
	}
	return "none"
}

type orderRow struct {
	result            string
	confirmedAt       *time.Time
	mockTransactionID *string
	registeredAt      *time.Time
}

type mockRow struct {
	platformReferenceID string
	result              string
	processedAt         *time.Time
}

func readOrder(t *testing.T, conn *pgx.Conn, label, id string) orderRow {
	t.Helper()
	var row orderRow
	err := conn.QueryRow(context.Background(), `
		SELECT result, confirmed_at, mock_transaction_id::text, registered_at
		FROM payment_orders
		WHERE id = $1
	`, id).Scan(&row.result, &row.confirmedAt, &row.mockTransactionID, &row.registeredAt)
	if errors.Is(err, pgx.ErrNoRows) {
		t.Fatalf("%s: fixture row not found", label)
	}
	if err != nil {
		t.Fatalf("%s: fixture query failed (SQLSTATE %s)", label, sqlStateOf(err))
	}
	return row
}

func readMock(t *testing.T, conn *pgx.Conn, label, id string) mockRow {
	t.Helper()
	var row mockRow
	err := conn.QueryRow(context.Background(), `
		SELECT platform_reference_id::text, result, processed_at
		FROM mock_transactions
		WHERE id = $1
	`, id).Scan(&row.platformReferenceID, &row.result, &row.processedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		t.Fatalf("%s: fixture row not found", label)
	}
	if err != nil {
		t.Fatalf("%s: fixture query failed (SQLSTATE %s)", label, sqlStateOf(err))
	}
	return row
}

func requireUTC(t *testing.T, label string, at *time.Time) {
	t.Helper()
	if at == nil {
		t.Fatalf("%s: expected non-null timestamp", label)
	}
	if at.IsZero() {
		t.Fatalf("%s: expected non-zero timestamp", label)
	}
	if _, offset := at.Zone(); offset != 0 {
		t.Fatalf("%s: expected UTC timestamp", label)
	}
}

func requireSameInstant(t *testing.T, label string, got *time.Time, want time.Time) {
	t.Helper()
	if got == nil {
		t.Fatalf("%s: expected non-null timestamp", label)
	}
	if diff := got.Sub(want); diff < -time.Microsecond || diff > time.Microsecond {
		t.Fatalf("%s: timestamp does not match seeded fact", label)
	}
}

func requireSameOptionalInstant(t *testing.T, label string, got, want *time.Time) {
	t.Helper()
	if want == nil {
		if got != nil {
			t.Fatalf("%s: expected null timestamp", label)
		}
		return
	}
	if got == nil {
		t.Fatalf("%s: expected non-null timestamp", label)
	}
	if diff := got.Sub(*want); diff < -time.Microsecond || diff > time.Microsecond {
		t.Fatalf("%s: timestamp does not match seeded fact", label)
	}
}

// TestMigrationVersions verifies the applied Goose versions and the UTC
// session in the real test database. Image CLI/version and packaged SQL
// evidence are produced by the migration launcher, not by this test.
func TestMigrationVersions(t *testing.T) {
	conn := OpenPostgres(t)
	ctx := context.Background()

	rows, err := conn.Query(ctx, `SELECT version_id, is_applied FROM goose_db_version ORDER BY version_id`)
	if err != nil {
		t.Fatalf("goose version table query failed (SQLSTATE %s)", sqlStateOf(err))
	}
	var applied []int64
	for rows.Next() {
		var version int64
		var isApplied bool
		if err := rows.Scan(&version, &isApplied); err != nil {
			rows.Close()
			t.Fatalf("goose version row scan failed")
		}
		if isApplied && version > 0 {
			// Goose seeds a version 0 baseline row when it creates its default table.
			applied = append(applied, version)
		}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		t.Fatalf("goose version iteration failed (SQLSTATE %s)", sqlStateOf(err))
	}
	rows.Close()

	if !slices.Equal(applied, []int64{1, 2, 3}) {
		t.Fatalf("goose applied versions: expected 1,2,3")
	}

	var maxVersion int64
	if err := conn.QueryRow(ctx, `SELECT max(version_id) FROM goose_db_version`).Scan(&maxVersion); err != nil {
		t.Fatalf("goose latest version query failed (SQLSTATE %s)", sqlStateOf(err))
	}
	if maxVersion != 3 {
		t.Fatalf("goose latest version: expected 3, got %d", maxVersion)
	}

	var timezone string
	if err := conn.QueryRow(ctx, `SELECT current_setting('TimeZone')`).Scan(&timezone); err != nil {
		t.Fatalf("database timezone query failed (SQLSTATE %s)", sqlStateOf(err))
	}
	if timezone != "UTC" {
		t.Fatalf("database session timezone: expected UTC")
	}

	var offsetSeconds int
	if err := conn.QueryRow(ctx, `SELECT extract(timezone FROM now())::int`).Scan(&offsetSeconds); err != nil {
		t.Fatalf("database timezone offset query failed (SQLSTATE %s)", sqlStateOf(err))
	}
	if offsetSeconds != 0 {
		t.Fatalf("database session timezone offset: expected zero")
	}
}

func TestSeedFixtures(t *testing.T) {
	conn := OpenPostgres(t)
	fixtures := SeedFixtures(t, conn)

	unknown := readOrder(t, conn, "unknown order", string(fixtures.Unknown.ID))
	if unknown.result != "UNKNOWN" {
		t.Fatalf("unknown order: expected result UNKNOWN")
	}
	if unknown.confirmedAt != nil {
		t.Fatalf("unknown order: expected null confirmed_at")
	}
	if unknown.mockTransactionID != nil {
		t.Fatalf("unknown order: expected null mock_transaction_id")
	}
	requireUTC(t, "unknown order registered_at fixture", &fixtures.Unknown.RegisteredAt)
	requireSameInstant(t, "unknown order registered_at", unknown.registeredAt, fixtures.Unknown.RegisteredAt)

	succeeded := readOrder(t, conn, "succeeded order", string(fixtures.Succeeded.ID))
	if succeeded.result != "SUCCEEDED" {
		t.Fatalf("succeeded order: expected result SUCCEEDED")
	}
	requireUTC(t, "succeeded order registered_at fixture", &fixtures.Succeeded.RegisteredAt)
	requireUTC(t, "succeeded order confirmed_at fixture", fixtures.Succeeded.ConfirmedAt)
	requireSameInstant(t, "succeeded order registered_at", succeeded.registeredAt, fixtures.Succeeded.RegisteredAt)
	requireSameOptionalInstant(t, "succeeded order confirmed_at", succeeded.confirmedAt, fixtures.Succeeded.ConfirmedAt)
	if succeeded.mockTransactionID == nil {
		t.Fatalf("succeeded order: expected non-null mock_transaction_id")
	}
	if !strings.EqualFold(*succeeded.mockTransactionID, string(fixtures.SucceededMock.ID)) {
		t.Fatalf("succeeded order: mock_transaction_id does not match seeded mock fact")
	}
	failed := readOrder(t, conn, "failed order", string(fixtures.Failed.ID))
	if failed.result != "FAILED" {
		t.Fatalf("failed order: expected result FAILED")
	}
	requireUTC(t, "failed order registered_at fixture", &fixtures.Failed.RegisteredAt)
	requireUTC(t, "failed order confirmed_at fixture", fixtures.Failed.ConfirmedAt)
	requireSameInstant(t, "failed order registered_at", failed.registeredAt, fixtures.Failed.RegisteredAt)
	requireSameOptionalInstant(t, "failed order confirmed_at", failed.confirmedAt, fixtures.Failed.ConfirmedAt)
	if failed.mockTransactionID == nil {
		t.Fatalf("failed order: expected non-null mock_transaction_id")
	}
	if !strings.EqualFold(*failed.mockTransactionID, string(fixtures.FailedMock.ID)) {
		t.Fatalf("failed order: mock_transaction_id does not match seeded mock fact")
	}

	unknownMock := readMock(t, conn, "unknown mock", string(fixtures.UnknownMock.ID))
	if !strings.EqualFold(unknownMock.platformReferenceID, string(fixtures.Unknown.ID)) {
		t.Fatalf("unknown mock: platform_reference_id does not match unknown order")
	}
	if unknownMock.result != string(fixtures.UnknownMock.Result) {
		t.Fatalf("unknown mock: result does not match seeded mock fact")
	}
	if unknownMock.result != "SUCCEEDED" && unknownMock.result != "FAILED" {
		t.Fatalf("unknown mock: expected one of the two Mock result values")
	}
	requireUTC(t, "unknown mock processed_at fixture", &fixtures.UnknownMock.ProcessedAt)
	requireSameInstant(t, "unknown mock processed_at", unknownMock.processedAt, fixtures.UnknownMock.ProcessedAt)

	succeededMock := readMock(t, conn, "succeeded mock", string(fixtures.SucceededMock.ID))
	if !strings.EqualFold(succeededMock.platformReferenceID, string(fixtures.Succeeded.ID)) {
		t.Fatalf("succeeded mock: platform_reference_id does not match succeeded order")
	}
	if succeededMock.result != "SUCCEEDED" {
		t.Fatalf("succeeded mock: expected result SUCCEEDED")
	}
	if succeededMock.result != string(fixtures.SucceededMock.Result) {
		t.Fatalf("succeeded mock: result does not match seeded mock fact")
	}
	requireUTC(t, "succeeded mock processed_at fixture", &fixtures.SucceededMock.ProcessedAt)
	requireSameInstant(t, "succeeded mock processed_at", succeededMock.processedAt, fixtures.SucceededMock.ProcessedAt)

	failedMock := readMock(t, conn, "failed mock", string(fixtures.FailedMock.ID))
	if !strings.EqualFold(failedMock.platformReferenceID, string(fixtures.Failed.ID)) {
		t.Fatalf("failed mock: platform_reference_id does not match failed order")
	}
	if failedMock.result != "FAILED" {
		t.Fatalf("failed mock: expected result FAILED")
	}
	if failedMock.result != string(fixtures.FailedMock.Result) {
		t.Fatalf("failed mock: result does not match seeded mock fact")
	}
	requireUTC(t, "failed mock processed_at fixture", &fixtures.FailedMock.ProcessedAt)
	requireSameInstant(t, "failed mock processed_at", failedMock.processedAt, fixtures.FailedMock.ProcessedAt)

	if fixtures.Unknown.MockTransactionID != nil {
		t.Fatalf("unknown fixture: expected null mock_transaction_id")
	}
	if fixtures.Succeeded.MockTransactionID == nil || !strings.EqualFold(string(*fixtures.Succeeded.MockTransactionID), string(fixtures.SucceededMock.ID)) {
		t.Fatalf("succeeded fixture: mock_transaction_id does not match seeded mock fact")
	}
	if fixtures.Failed.MockTransactionID == nil || !strings.EqualFold(string(*fixtures.Failed.MockTransactionID), string(fixtures.FailedMock.ID)) {
		t.Fatalf("failed fixture: mock_transaction_id does not match seeded mock fact")
	}
	if !strings.EqualFold(string(fixtures.UnknownMock.PlatformReferenceID), string(fixtures.Unknown.ID)) {
		t.Fatalf("unknown fixture: mock platform reference does not match order")
	}
	if !strings.EqualFold(string(fixtures.SucceededMock.PlatformReferenceID), string(fixtures.Succeeded.ID)) {
		t.Fatalf("succeeded fixture: mock platform reference does not match order")
	}
	if !strings.EqualFold(string(fixtures.FailedMock.PlatformReferenceID), string(fixtures.Failed.ID)) {
		t.Fatalf("failed fixture: mock platform reference does not match order")
	}

	ids := []string{
		string(fixtures.Unknown.ID),
		string(fixtures.Succeeded.ID),
		string(fixtures.Failed.ID),
		string(fixtures.UnknownMock.ID),
		string(fixtures.SucceededMock.ID),
		string(fixtures.FailedMock.ID),
	}
	seen := make(map[string]bool, len(ids))
	for _, id := range ids {
		if id == "" {
			t.Fatalf("fixture IDs: expected non-empty IDs")
		}
		key := strings.ToLower(id)
		if seen[key] {
			t.Fatalf("fixture IDs: expected independent platform and Mock IDs")
		}
		seen[key] = true
	}
}
