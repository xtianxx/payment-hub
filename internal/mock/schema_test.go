//go:build integration

package mock

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"payment-hub/tests/harness"
)

const insertMockTransactionSQL = `INSERT INTO mock_transactions
	(id, platform_reference_id, amount, currency, result, processed_at)
	VALUES ($1, $2, $3, $4, $5, $6)`

const insertMockTransactionWithoutResultSQL = `INSERT INTO mock_transactions
	(id, platform_reference_id, amount, currency, processed_at)
	VALUES ($1, $2, $3, $4, $5)`

const (
	sqlStateNotNull = "23502"
	sqlStateUnique  = "23505"
	sqlStateCheck   = "23514"
)

type columnSpec struct {
	name     string
	dataType string
	udtName  string
	nullable bool
}

type indexInfo struct {
	columns []string
}

func testUUID(prefix string, n int) string {
	return fmt.Sprintf("%s000000-0000-4000-8000-%012d", prefix, n)
}

func sqlStateOf(err error) string {
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		return pgErr.Code
	}
	return "none"
}

func mustExec(t *testing.T, conn *pgx.Conn, label, query string, args ...any) {
	t.Helper()
	if _, err := conn.Exec(context.Background(), query, args...); err != nil {
		t.Fatalf("%s: expected database acceptance, statement failed (SQLSTATE %s)", label, sqlStateOf(err))
	}
}

func expectReject(t *testing.T, conn *pgx.Conn, label, wantState, query string, args ...any) {
	t.Helper()
	_, err := conn.Exec(context.Background(), query, args...)
	if err == nil {
		t.Fatalf("%s: expected SQLSTATE %s rejection, statement succeeded", label, wantState)
	}
	if got := sqlStateOf(err); got != wantState {
		t.Fatalf("%s: expected SQLSTATE %s rejection, got SQLSTATE %s", label, wantState, got)
	}
}

func inSavepoint(t *testing.T, conn *pgx.Conn, name string, fn func()) {
	t.Helper()
	ctx := context.Background()
	if _, err := conn.Exec(ctx, "SAVEPOINT "+name); err != nil {
		t.Fatalf("savepoint setup failed (SQLSTATE %s)", sqlStateOf(err))
	}
	defer func() {
		if _, err := conn.Exec(ctx, "ROLLBACK TO SAVEPOINT "+name); err != nil {
			t.Errorf("savepoint rollback failed (SQLSTATE %s)", sqlStateOf(err))
			return
		}
		if _, err := conn.Exec(ctx, "RELEASE SAVEPOINT "+name); err != nil {
			t.Errorf("savepoint release failed (SQLSTATE %s)", sqlStateOf(err))
		}
	}()
	fn()
}

func checkColumns(t *testing.T, conn *pgx.Conn, table string, want []columnSpec) {
	t.Helper()
	rows, err := conn.Query(context.Background(), `
		SELECT column_name, data_type, udt_name, is_nullable
		FROM information_schema.columns
		WHERE table_schema = current_schema() AND table_name = $1
		ORDER BY ordinal_position
	`, table)
	if err != nil {
		t.Fatalf("%s: column metadata query failed (SQLSTATE %s)", table, sqlStateOf(err))
	}
	defer rows.Close()

	var got []columnSpec
	for rows.Next() {
		var spec columnSpec
		var nullable string
		if err := rows.Scan(&spec.name, &spec.dataType, &spec.udtName, &nullable); err != nil {
			t.Fatalf("%s: column metadata scan failed", table)
		}
		spec.nullable = nullable == "YES"
		got = append(got, spec)
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("%s: column metadata iteration failed (SQLSTATE %s)", table, sqlStateOf(err))
	}
	if len(got) != len(want) {
		t.Fatalf("%s: expected %d columns, got %d", table, len(want), len(got))
	}
	for i := range want {
		if got[i].name != want[i].name {
			t.Fatalf("%s: column %d expected name %q, got %q", table, i+1, want[i].name, got[i].name)
		}
		if got[i].dataType != want[i].dataType {
			t.Fatalf("%s.%s: expected data_type %q, got %q", table, want[i].name, want[i].dataType, got[i].dataType)
		}
		if got[i].udtName != want[i].udtName {
			t.Fatalf("%s.%s: expected udt_name %q, got %q", table, want[i].name, want[i].udtName, got[i].udtName)
		}
		if got[i].nullable != want[i].nullable {
			t.Fatalf("%s.%s: expected nullable=%t, got %t", table, want[i].name, want[i].nullable, got[i].nullable)
		}
	}
}

func checkNoCustomEnums(t *testing.T, conn *pgx.Conn) {
	t.Helper()
	var count int
	err := conn.QueryRow(context.Background(), `
		SELECT count(*)
		FROM pg_type t
		JOIN pg_namespace n ON n.oid = t.typnamespace
		WHERE n.nspname = current_schema() AND t.typtype = 'e'
	`).Scan(&count)
	if err != nil {
		t.Fatalf("enum metadata query failed (SQLSTATE %s)", sqlStateOf(err))
	}
	if count != 0 {
		t.Fatalf("expected no custom enum types in current schema, got %d", count)
	}
}

func loadIndexes(t *testing.T, conn *pgx.Conn, table string) []indexInfo {
	t.Helper()
	rows, err := conn.Query(context.Background(), `
		SELECT ARRAY(
			SELECT a.attname::text
			FROM unnest(i.indkey::smallint[]) WITH ORDINALITY AS k(attnum, ord)
			JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
			WHERE k.ord <= i.indnkeyatts
			ORDER BY k.ord
		)
		FROM pg_index i
		JOIN pg_class t ON t.oid = i.indrelid
		JOIN pg_namespace n ON n.oid = t.relnamespace
		WHERE n.nspname = current_schema() AND t.relname = $1 AND i.indisvalid
	`, table)
	if err != nil {
		t.Fatalf("%s: index metadata query failed (SQLSTATE %s)", table, sqlStateOf(err))
	}
	defer rows.Close()

	var indexes []indexInfo
	for rows.Next() {
		var columns []string
		if err := rows.Scan(&columns); err != nil {
			t.Fatalf("%s: index metadata scan failed", table)
		}
		indexes = append(indexes, indexInfo{columns: columns})
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("%s: index metadata iteration failed (SQLSTATE %s)", table, sqlStateOf(err))
	}
	return indexes
}

func requireIndexCount(t *testing.T, table string, indexes []indexInfo, want int) {
	t.Helper()
	if len(indexes) != want {
		t.Fatalf("%s: expected %d indexes, got %d", table, want, len(indexes))
	}
}

func requireIndex(t *testing.T, table string, indexes []indexInfo, want ...string) {
	t.Helper()
	for _, index := range indexes {
		if slices.Equal(index.columns, want) {
			return
		}
	}
	t.Fatalf("%s: missing index on columns %s", table, strings.Join(want, ","))
}

func requireNoDuplicateIndexes(t *testing.T, table string, indexes []indexInfo) {
	t.Helper()
	seen := make(map[string]bool, len(indexes))
	for _, index := range indexes {
		key := strings.Join(index.columns, ",")
		if seen[key] {
			t.Fatalf("%s: duplicate index definition for columns %s", table, key)
		}
		seen[key] = true
	}
}

func mockValues(id, platformReferenceID, amount, currency, result, processedAt any) []any {
	return []any{id, platformReferenceID, amount, currency, result, processedAt}
}

func mockValuesWithoutResult(id, platformReferenceID, amount, currency, processedAt any) []any {
	return []any{id, platformReferenceID, amount, currency, processedAt}
}

func TestMockTransactionsSchema(t *testing.T) {
	conn := harness.OpenPostgres(t)
	fixtures := harness.SeedFixtures(t, conn)
	now := time.Date(2026, time.November, 7, 12, 0, 0, 0, time.UTC)

	t.Run("columns", func(t *testing.T) {
		checkColumns(t, conn, "mock_transactions", []columnSpec{
			{name: "id", dataType: "uuid", udtName: "uuid", nullable: false},
			{name: "platform_reference_id", dataType: "uuid", udtName: "uuid", nullable: false},
			{name: "amount", dataType: "bigint", udtName: "int8", nullable: false},
			{name: "currency", dataType: "text", udtName: "text", nullable: false},
			{name: "result", dataType: "text", udtName: "text", nullable: false},
			{name: "processed_at", dataType: "timestamp with time zone", udtName: "timestamptz", nullable: false},
		})
		checkNoCustomEnums(t, conn)
	})

	t.Run("uuid type accepts non version four values", func(t *testing.T) {
		inSavepoint(t, conn, "uuid_non_v4_mock", func() {
			mustExec(t, conn, "non version four mock id", insertMockTransactionSQL, mockValues("a8098c1a-f86e-11da-bd1a-00112444be1e", testUUID("10", 130), int64(100), "CNY", "SUCCEEDED", now)...)
		})
	})

	t.Run("result values", func(t *testing.T) {
		inSavepoint(t, conn, "mock_result_accept", func() {
			mustExec(t, conn, "mock result succeeded", insertMockTransactionSQL, mockValues(testUUID("20", 1), testUUID("10", 100), int64(100), "CNY", "SUCCEEDED", now)...)
			mustExec(t, conn, "mock result failed", insertMockTransactionSQL, mockValues(testUUID("20", 2), testUUID("10", 101), int64(100), "CNY", "FAILED", now)...)
		})
		t.Run("omitted result", func(t *testing.T) {
			inSavepoint(t, conn, "mock_result_omitted", func() {
				expectReject(t, conn, "mock omitted result", sqlStateNotNull, insertMockTransactionWithoutResultSQL, mockValuesWithoutResult(testUUID("20", 5), testUUID("10", 105), int64(100), "CNY", now)...)
			})
		})
		for _, tc := range []struct {
			name   string
			result string
		}{
			{name: "unknown", result: "UNKNOWN"},
			{name: "pending", result: "PENDING"},
			{name: "lowercase", result: "succeeded"},
			{name: "empty", result: ""},
		} {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				inSavepoint(t, conn, "mock_result_reject", func() {
					expectReject(t, conn, "mock result "+tc.name, sqlStateCheck, insertMockTransactionSQL, mockValues(testUUID("20", 3), testUUID("10", 102), int64(100), "CNY", tc.result, now)...)
				})
			})
		}
		t.Run("null", func(t *testing.T) {
			inSavepoint(t, conn, "mock_result_null", func() {
				expectReject(t, conn, "mock result null", sqlStateNotNull, insertMockTransactionSQL, mockValues(testUUID("20", 4), testUUID("10", 103), int64(100), "CNY", nil, now)...)
			})
		})
	})

	t.Run("platform reference uniqueness", func(t *testing.T) {
		inSavepoint(t, conn, "mock_reference_accept", func() {
			mustExec(t, conn, "mock reference first", insertMockTransactionSQL, mockValues(testUUID("20", 10), testUUID("10", 110), int64(100), "CNY", "SUCCEEDED", now)...)
			mustExec(t, conn, "mock reference second", insertMockTransactionSQL, mockValues(testUUID("20", 11), testUUID("10", 111), int64(100), "CNY", "FAILED", now)...)
		})
		inSavepoint(t, conn, "mock_reference_duplicate", func() {
			expectReject(t, conn, "duplicate platform reference", sqlStateUnique, insertMockTransactionSQL, mockValues(testUUID("20", 12), string(fixtures.SucceededMock.PlatformReferenceID), int64(100), "CNY", "SUCCEEDED", now)...)
		})
		t.Run("null", func(t *testing.T) {
			inSavepoint(t, conn, "mock_reference_null", func() {
				expectReject(t, conn, "platform reference null", sqlStateNotNull, insertMockTransactionSQL, mockValues(testUUID("20", 13), nil, int64(100), "CNY", "SUCCEEDED", now)...)
			})
		})
		inSavepoint(t, conn, "mock_primary_key_collision", func() {
			expectReject(t, conn, "mock primary key collision", sqlStateUnique, insertMockTransactionSQL, mockValues(string(fixtures.UnknownMock.ID), testUUID("10", 112), int64(100), "CNY", "SUCCEEDED", now)...)
		})
		t.Run("id null", func(t *testing.T) {
			inSavepoint(t, conn, "mock_id_null", func() {
				expectReject(t, conn, "mock id null", sqlStateNotNull, insertMockTransactionSQL, mockValues(nil, testUUID("10", 113), int64(100), "CNY", "SUCCEEDED", now)...)
			})
		})
	})

	t.Run("independent amount and currency", func(t *testing.T) {
		inSavepoint(t, conn, "mock_independent_facts", func() {
			mustExec(t, conn, "negative amount and non CNY", insertMockTransactionSQL, mockValues(testUUID("20", 20), testUUID("10", 120), int64(-1), "USD", "FAILED", now)...)
			mustExec(t, conn, "zero amount", insertMockTransactionSQL, mockValues(testUUID("20", 24), testUUID("10", 124), int64(0), "CNY", "SUCCEEDED", now)...)
			mustExec(t, conn, "above platform maximum", insertMockTransactionSQL, mockValues(testUUID("20", 21), testUUID("10", 121), int64(1_000_001), "CNY", "SUCCEEDED", now)...)
			mustExec(t, conn, "large negative amount", insertMockTransactionSQL, mockValues(testUUID("20", 22), testUUID("10", 122), int64(-1_000_000), "usd", "FAILED", now)...)
		})
		for _, tc := range []struct {
			name      string
			amount    any
			currency  any
			processed any
		}{
			{name: "amount null", amount: nil, currency: "CNY", processed: now},
			{name: "currency null", amount: int64(100), currency: nil, processed: now},
			{name: "processed_at null", amount: int64(100), currency: "CNY", processed: nil},
		} {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				inSavepoint(t, conn, "mock_fact_null", func() {
					expectReject(t, conn, tc.name, sqlStateNotNull, insertMockTransactionSQL, mockValues(testUUID("20", 23), testUUID("10", 123), tc.amount, tc.currency, "SUCCEEDED", tc.processed)...)
				})
			})
		}
	})

	t.Run("indexes", func(t *testing.T) {
		indexes := loadIndexes(t, conn, "mock_transactions")
		requireIndexCount(t, "mock_transactions", indexes, 2)
		requireIndex(t, "mock_transactions", indexes, "id")
		requireIndex(t, "mock_transactions", indexes, "platform_reference_id")
		requireNoDuplicateIndexes(t, "mock_transactions", indexes)
	})

	t.Run("no cross module foreign keys", func(t *testing.T) {
		var count int
		if err := conn.QueryRow(context.Background(), `
			SELECT count(*)
			FROM pg_constraint c
			JOIN pg_class t ON t.oid = c.conrelid
			JOIN pg_namespace n ON n.oid = t.relnamespace
			WHERE n.nspname = current_schema() AND t.relname = 'mock_transactions' AND c.contype = 'f'
		`).Scan(&count); err != nil {
			t.Fatalf("foreign key metadata query failed (SQLSTATE %s)", sqlStateOf(err))
		}
		if count != 0 {
			t.Fatalf("expected no foreign keys on mock_transactions, got %d", count)
		}
	})
}
