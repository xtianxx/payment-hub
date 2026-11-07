//go:build integration

package postgres

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

const (
	insertPaymentOrderSQL = `INSERT INTO payment_orders
		(id, client_id, business_order_no, amount, currency, result, mock_transaction_id, registered_at, confirmed_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`
	insertPaymentOrderWithoutResultSQL = `INSERT INTO payment_orders
		(id, client_id, business_order_no, amount, currency, mock_transaction_id, registered_at, confirmed_at)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`
	deletePaymentOrderSQL = `DELETE FROM payment_orders WHERE id = $1`

	insertResultHandlingRecordSQL = `INSERT INTO result_handling_records
		(id, payment_order_id, request_id, source, observed_at, evidence, result_before, result_after, decision, reason_code, reason_message)
		VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)`
)

const (
	sqlStateNotNull     = "23502"
	sqlStateForeignKey  = "23503"
	sqlStateUnique      = "23505"
	sqlStateCheck       = "23514"
	sqlStateInvalidText = "22P02"
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

func expectRejectAndRecover(t *testing.T, conn *pgx.Conn, name, label, wantState, query string, args ...any) {
	t.Helper()
	ctx := context.Background()
	if _, err := conn.Exec(ctx, "SAVEPOINT "+name); err != nil {
		t.Fatalf("%s: savepoint setup failed (SQLSTATE %s)", label, sqlStateOf(err))
	}
	_, err := conn.Exec(ctx, query, args...)
	if err == nil {
		t.Fatalf("%s: expected SQLSTATE %s rejection, statement succeeded", label, wantState)
	}
	if got := sqlStateOf(err); got != wantState {
		t.Fatalf("%s: expected SQLSTATE %s rejection, got SQLSTATE %s", label, wantState, got)
	}
	if _, err := conn.Exec(ctx, "ROLLBACK TO SAVEPOINT "+name); err != nil {
		t.Fatalf("%s: savepoint recovery failed (SQLSTATE %s)", label, sqlStateOf(err))
	}
	if _, err := conn.Exec(ctx, "RELEASE SAVEPOINT "+name); err != nil {
		t.Fatalf("%s: savepoint release failed (SQLSTATE %s)", label, sqlStateOf(err))
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

func requireIndexAny(t *testing.T, table string, indexes []indexInfo, alternatives ...[]string) {
	t.Helper()
	for _, want := range alternatives {
		for _, index := range indexes {
			if slices.Equal(index.columns, want) {
				return
			}
		}
	}
	t.Fatalf("%s: missing expected unique index on business key columns", table)
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

func orderValues(id, clientID, businessOrderNo, amount, currency, result, mockTransactionID, registeredAt, confirmedAt any) []any {
	return []any{id, clientID, businessOrderNo, amount, currency, result, mockTransactionID, registeredAt, confirmedAt}
}

func orderValuesWithoutResult(id, clientID, businessOrderNo, amount, currency, mockTransactionID, registeredAt, confirmedAt any) []any {
	return []any{id, clientID, businessOrderNo, amount, currency, mockTransactionID, registeredAt, confirmedAt}
}

func recordValues(id, paymentOrderID, requestID, source, observedAt, evidence, resultBefore, resultAfter, decision, reasonCode, reasonMessage any) []any {
	return []any{id, paymentOrderID, requestID, source, observedAt, evidence, resultBefore, resultAfter, decision, reasonCode, reasonMessage}
}

func TestPaymentOrdersSchema(t *testing.T) {
	conn := harness.OpenPostgres(t)
	fixtures := harness.SeedFixtures(t, conn)
	now := time.Date(2026, time.November, 7, 12, 0, 0, 0, time.UTC)
	confirmed := now.Add(time.Minute)

	t.Run("columns", func(t *testing.T) {
		checkColumns(t, conn, "payment_orders", []columnSpec{
			{name: "id", dataType: "uuid", udtName: "uuid", nullable: false},
			{name: "client_id", dataType: "text", udtName: "text", nullable: false},
			{name: "business_order_no", dataType: "text", udtName: "text", nullable: false},
			{name: "amount", dataType: "bigint", udtName: "int8", nullable: false},
			{name: "currency", dataType: "text", udtName: "text", nullable: false},
			{name: "result", dataType: "text", udtName: "text", nullable: false},
			{name: "mock_transaction_id", dataType: "uuid", udtName: "uuid", nullable: true},
			{name: "registered_at", dataType: "timestamp with time zone", udtName: "timestamptz", nullable: false},
			{name: "confirmed_at", dataType: "timestamp with time zone", udtName: "timestamptz", nullable: true},
		})
		checkNoCustomEnums(t, conn)
	})

	t.Run("uuid type accepts non version four values", func(t *testing.T) {
		inSavepoint(t, conn, "uuid_non_v4_order", func() {
			mustExec(t, conn, "non version four order id", insertPaymentOrderSQL, orderValues("a8098c1a-f86e-11da-bd1a-00112444be1e", "uuid-v1", "uuid-v1-1", int64(100), "CNY", "UNKNOWN", nil, now, nil)...)
		})
	})

	t.Run("amount bounds", func(t *testing.T) {
		inSavepoint(t, conn, "amount_bounds_accept", func() {
			mustExec(t, conn, "amount minimum", insertPaymentOrderSQL, orderValues(testUUID("10", 1), "amount-min", "amount-min-1", int64(1), "CNY", "UNKNOWN", nil, now, nil)...)
			mustExec(t, conn, "amount maximum", insertPaymentOrderSQL, orderValues(testUUID("10", 2), "amount-max", "amount-max-1", int64(1_000_000), "CNY", "UNKNOWN", nil, now, nil)...)
		})
		for _, tc := range []struct {
			name   string
			amount any
		}{
			{name: "zero", amount: int64(0)},
			{name: "negative", amount: int64(-1)},
			{name: "above maximum", amount: int64(1_000_001)},
		} {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				inSavepoint(t, conn, "amount_reject", func() {
					expectReject(t, conn, "amount "+tc.name, sqlStateCheck, insertPaymentOrderSQL, orderValues(testUUID("10", 3), "amount-reject", "amount-reject-1", tc.amount, "CNY", "UNKNOWN", nil, now, nil)...)
				})
			})
		}
		t.Run("null", func(t *testing.T) {
			inSavepoint(t, conn, "amount_null", func() {
				expectReject(t, conn, "amount null", sqlStateNotNull, insertPaymentOrderSQL, orderValues(testUUID("10", 4), "amount-null", "amount-null-1", nil, "CNY", "UNKNOWN", nil, now, nil)...)
			})
		})
	})

	t.Run("currency", func(t *testing.T) {
		inSavepoint(t, conn, "currency_accept", func() {
			mustExec(t, conn, "currency CNY", insertPaymentOrderSQL, orderValues(testUUID("10", 10), "currency-cny", "currency-cny-1", int64(100), "CNY", "UNKNOWN", nil, now, nil)...)
		})
		for _, tc := range []struct {
			name     string
			currency string
		}{
			{name: "USD", currency: "USD"},
			{name: "lowercase", currency: "cny"},
			{name: "empty", currency: ""},
		} {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				inSavepoint(t, conn, "currency_reject", func() {
					expectReject(t, conn, "currency "+tc.name, sqlStateCheck, insertPaymentOrderSQL, orderValues(testUUID("10", 11), "currency-reject", "currency-reject-1", int64(100), tc.currency, "UNKNOWN", nil, now, nil)...)
				})
			})
		}
		t.Run("null", func(t *testing.T) {
			inSavepoint(t, conn, "currency_null", func() {
				expectReject(t, conn, "currency null", sqlStateNotNull, insertPaymentOrderSQL, orderValues(testUUID("10", 12), "currency-null", "currency-null-1", int64(100), nil, "UNKNOWN", nil, now, nil)...)
			})
		})
	})

	t.Run("identifiers", func(t *testing.T) {
		inSavepoint(t, conn, "identifier_accept", func() {
			mustExec(t, conn, "identifier minimum length", insertPaymentOrderSQL, orderValues(testUUID("10", 20), "A", "b", int64(100), "CNY", "UNKNOWN", nil, now, nil)...)
			mustExec(t, conn, "identifier maximum length", insertPaymentOrderSQL, orderValues(testUUID("10", 21), strings.Repeat("a", 64), strings.Repeat("Z", 64), int64(100), "CNY", "UNKNOWN", nil, now, nil)...)
			mustExec(t, conn, "identifier allowed punctuation", insertPaymentOrderSQL, orderValues(testUUID("10", 22), "Abc-123_xyz", "Abc-123_xyz", int64(100), "CNY", "UNKNOWN", nil, now, nil)...)
		})

		invalid65 := strings.Repeat("a", 65)
		reject := func(t *testing.T, name, wantState string, clientID, businessOrderNo any) {
			t.Helper()
			inSavepoint(t, conn, "identifier_reject", func() {
				expectReject(t, conn, name, wantState, insertPaymentOrderSQL, orderValues(testUUID("10", 23), clientID, businessOrderNo, int64(100), "CNY", "UNKNOWN", nil, now, nil)...)
			})
		}
		t.Run("client empty", func(t *testing.T) { reject(t, "client empty", sqlStateCheck, "", "valid-business") })
		t.Run("client too long", func(t *testing.T) { reject(t, "client too long", sqlStateCheck, invalid65, "valid-business") })
		t.Run("client space", func(t *testing.T) { reject(t, "client space", sqlStateCheck, "bad id", "valid-business") })
		t.Run("client slash", func(t *testing.T) { reject(t, "client slash", sqlStateCheck, "bad/id", "valid-business") })
		t.Run("client dot", func(t *testing.T) { reject(t, "client dot", sqlStateCheck, "bad.id", "valid-business") })
		t.Run("client at sign", func(t *testing.T) { reject(t, "client at sign", sqlStateCheck, "bad@id", "valid-business") })
		t.Run("client non ascii", func(t *testing.T) { reject(t, "client non ascii", sqlStateCheck, "中文", "valid-business") })
		t.Run("business empty", func(t *testing.T) { reject(t, "business empty", sqlStateCheck, "valid-client", "") })
		t.Run("business too long", func(t *testing.T) { reject(t, "business too long", sqlStateCheck, "valid-client", invalid65) })
		t.Run("business space", func(t *testing.T) { reject(t, "business space", sqlStateCheck, "valid-client", "bad id") })
		t.Run("business slash", func(t *testing.T) { reject(t, "business slash", sqlStateCheck, "valid-client", "bad/id") })
		t.Run("business dot", func(t *testing.T) { reject(t, "business dot", sqlStateCheck, "valid-client", "bad.id") })
		t.Run("business at sign", func(t *testing.T) { reject(t, "business at sign", sqlStateCheck, "valid-client", "bad@id") })
		t.Run("business non ascii", func(t *testing.T) { reject(t, "business non ascii", sqlStateCheck, "valid-client", "中文") })
		t.Run("client null", func(t *testing.T) { reject(t, "client null", sqlStateNotNull, nil, "valid-business") })
		t.Run("business null", func(t *testing.T) { reject(t, "business null", sqlStateNotNull, "valid-client", nil) })

		t.Run("case sensitive uniqueness", func(t *testing.T) {
			inSavepoint(t, conn, "identifier_case", func() {
				id := testUUID("10", 24)
				mustExec(t, conn, "case original", insertPaymentOrderSQL, orderValues(id, "CaseClient", "CaseOrder", int64(100), "CNY", "UNKNOWN", nil, now, nil)...)
				var gotClient, gotBusiness string
				if err := conn.QueryRow(context.Background(), `SELECT client_id, business_order_no FROM payment_orders WHERE id = $1`, id).Scan(&gotClient, &gotBusiness); err != nil {
					t.Fatalf("identifier readback failed (SQLSTATE %s)", sqlStateOf(err))
				}
				if gotClient != "CaseClient" || gotBusiness != "CaseOrder" {
					t.Fatalf("identifier readback: expected original case preserved")
				}
				mustExec(t, conn, "different client case", insertPaymentOrderSQL, orderValues(testUUID("10", 25), "caseclient", "CaseOrder", int64(100), "CNY", "UNKNOWN", nil, now, nil)...)
				mustExec(t, conn, "different business case", insertPaymentOrderSQL, orderValues(testUUID("10", 26), "CaseClient", "caseorder", int64(100), "CNY", "UNKNOWN", nil, now, nil)...)
				expectReject(t, conn, "duplicate business key", sqlStateUnique, insertPaymentOrderSQL, orderValues(testUUID("10", 27), "CaseClient", "CaseOrder", int64(100), "CNY", "UNKNOWN", nil, now, nil)...)
			})
		})
	})

	t.Run("primary key collision", func(t *testing.T) {
		inSavepoint(t, conn, "pk_collision", func() {
			expectReject(t, conn, "primary key collision", sqlStateUnique, insertPaymentOrderSQL, orderValues(string(fixtures.Unknown.ID), "pk-collision", "pk-collision-1", int64(100), "CNY", "UNKNOWN", nil, now, nil)...)
		})
	})

	t.Run("business unique key", func(t *testing.T) {
		inSavepoint(t, conn, "business_unique", func() {
			mustExec(t, conn, "business key first", insertPaymentOrderSQL, orderValues(testUUID("10", 30), "business-client", "business-order", int64(100), "CNY", "UNKNOWN", nil, now, nil)...)
			mustExec(t, conn, "same client different business order", insertPaymentOrderSQL, orderValues(testUUID("10", 31), "business-client", "business-order-2", int64(100), "CNY", "UNKNOWN", nil, now, nil)...)
			mustExec(t, conn, "different client same business order", insertPaymentOrderSQL, orderValues(testUUID("10", 32), "business-client-2", "business-order", int64(100), "CNY", "UNKNOWN", nil, now, nil)...)
			expectReject(t, conn, "duplicate business key", sqlStateUnique, insertPaymentOrderSQL, orderValues(testUUID("10", 33), "business-client", "business-order", int64(100), "CNY", "UNKNOWN", nil, now, nil)...)
		})
	})

	t.Run("mock transaction association", func(t *testing.T) {
		inSavepoint(t, conn, "mock_null_association", func() {
			mustExec(t, conn, "first null mock association", insertPaymentOrderSQL, orderValues(testUUID("10", 40), "mock-null-1", "mock-null-1", int64(100), "CNY", "UNKNOWN", nil, now, nil)...)
			mustExec(t, conn, "second null mock association", insertPaymentOrderSQL, orderValues(testUUID("10", 41), "mock-null-2", "mock-null-2", int64(100), "CNY", "UNKNOWN", nil, now, nil)...)
		})
		mockID := testUUID("50", 1)
		inSavepoint(t, conn, "mock_unique_association", func() {
			mustExec(t, conn, "first mock association", insertPaymentOrderSQL, orderValues(testUUID("10", 42), "mock-unique-1", "mock-unique-1", int64(100), "CNY", "UNKNOWN", mockID, now, nil)...)
			expectReject(t, conn, "duplicate mock association", sqlStateUnique, insertPaymentOrderSQL, orderValues(testUUID("10", 43), "mock-unique-2", "mock-unique-2", int64(100), "CNY", "UNKNOWN", mockID, now, nil)...)
		})
	})

	t.Run("result values", func(t *testing.T) {
		inSavepoint(t, conn, "result_accept", func() {
			mustExec(t, conn, "result unknown", insertPaymentOrderSQL, orderValues(testUUID("10", 50), "result-unknown", "result-unknown-1", int64(100), "CNY", "UNKNOWN", nil, now, nil)...)
			mustExec(t, conn, "result succeeded", insertPaymentOrderSQL, orderValues(testUUID("10", 51), "result-succeeded", "result-succeeded-1", int64(100), "CNY", "SUCCEEDED", nil, now, confirmed)...)
			mustExec(t, conn, "result failed", insertPaymentOrderSQL, orderValues(testUUID("10", 52), "result-failed", "result-failed-1", int64(100), "CNY", "FAILED", nil, now, confirmed)...)
		})
		t.Run("omitted result", func(t *testing.T) {
			inSavepoint(t, conn, "result_omitted", func() {
				expectReject(t, conn, "omitted result", sqlStateNotNull, insertPaymentOrderWithoutResultSQL, orderValuesWithoutResult(testUUID("10", 56), "result-omitted", "result-omitted-1", int64(100), "CNY", nil, now, nil)...)
			})
		})
		for _, tc := range []struct {
			name   string
			result string
		}{
			{name: "pending", result: "PENDING"},
			{name: "lowercase", result: "succeeded"},
			{name: "empty", result: ""},
		} {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				inSavepoint(t, conn, "result_reject", func() {
					expectReject(t, conn, "result "+tc.name, sqlStateCheck, insertPaymentOrderSQL, orderValues(testUUID("10", 53), "result-reject", "result-reject-1", int64(100), "CNY", tc.result, nil, now, nil)...)
				})
			})
		}
		t.Run("null", func(t *testing.T) {
			inSavepoint(t, conn, "result_null", func() {
				expectReject(t, conn, "result null", sqlStateNotNull, insertPaymentOrderSQL, orderValues(testUUID("10", 54), "result-null", "result-null-1", int64(100), "CNY", nil, nil, now, nil)...)
			})
		})
	})

	t.Run("result time combinations", func(t *testing.T) {
		for _, tc := range []struct {
			name        string
			result      any
			confirmedAt any
		}{
			{name: "unknown with confirmation time", result: "UNKNOWN", confirmedAt: confirmed},
			{name: "succeeded without confirmation time", result: "SUCCEEDED", confirmedAt: nil},
			{name: "failed without confirmation time", result: "FAILED", confirmedAt: nil},
		} {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				inSavepoint(t, conn, "result_time", func() {
					expectReject(t, conn, tc.name, sqlStateCheck, insertPaymentOrderSQL, orderValues(testUUID("10", 55), "result-time", "result-time-1", int64(100), "CNY", tc.result, nil, now, tc.confirmedAt)...)
				})
			})
		}
	})

	t.Run("required columns", func(t *testing.T) {
		for _, tc := range []struct {
			name   string
			values []any
		}{
			{name: "id null", values: orderValues(nil, "required-client", "required-id", int64(100), "CNY", "UNKNOWN", nil, now, nil)},
			{name: "registered_at null", values: orderValues(testUUID("10", 60), "required-client", "required-registered", int64(100), "CNY", "UNKNOWN", nil, nil, nil)},
		} {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				inSavepoint(t, conn, "required_reject", func() {
					expectReject(t, conn, tc.name, sqlStateNotNull, insertPaymentOrderSQL, tc.values...)
				})
			})
		}
	})

	t.Run("indexes", func(t *testing.T) {
		indexes := loadIndexes(t, conn, "payment_orders")
		requireIndexCount(t, "payment_orders", indexes, 3)
		requireIndex(t, "payment_orders", indexes, "id")
		requireIndexAny(t, "payment_orders", indexes, []string{"client_id", "business_order_no"}, []string{"business_order_no", "client_id"})
		requireIndex(t, "payment_orders", indexes, "mock_transaction_id")
		requireNoDuplicateIndexes(t, "payment_orders", indexes)
	})
}

func TestResultHandlingRecordsSchema(t *testing.T) {
	conn := harness.OpenPostgres(t)
	fixtures := harness.SeedFixtures(t, conn)
	now := time.Date(2026, time.November, 7, 12, 0, 0, 0, time.UTC)
	orderID := string(fixtures.Unknown.ID)
	requestID := testUUID("40", 1)

	t.Run("columns", func(t *testing.T) {
		checkColumns(t, conn, "result_handling_records", []columnSpec{
			{name: "id", dataType: "uuid", udtName: "uuid", nullable: false},
			{name: "payment_order_id", dataType: "uuid", udtName: "uuid", nullable: false},
			{name: "request_id", dataType: "uuid", udtName: "uuid", nullable: false},
			{name: "source", dataType: "text", udtName: "text", nullable: false},
			{name: "observed_at", dataType: "timestamp with time zone", udtName: "timestamptz", nullable: false},
			{name: "evidence", dataType: "jsonb", udtName: "jsonb", nullable: false},
			{name: "result_before", dataType: "text", udtName: "text", nullable: false},
			{name: "result_after", dataType: "text", udtName: "text", nullable: false},
			{name: "decision", dataType: "text", udtName: "text", nullable: false},
			{name: "reason_code", dataType: "text", udtName: "text", nullable: true},
			{name: "reason_message", dataType: "text", udtName: "text", nullable: true},
		})
		checkNoCustomEnums(t, conn)
	})

	t.Run("uuid type accepts non version four request id", func(t *testing.T) {
		inSavepoint(t, conn, "uuid_non_v4_request", func() {
			mustExec(t, conn, "non version four request id", insertResultHandlingRecordSQL, recordValues(testUUID("30", 35), orderID, "a8098c1a-f86e-11da-bd1a-00112444be1e", "QUERY", now, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", "NO_MATCH", nil)...)
		})
	})

	t.Run("required columns and references", func(t *testing.T) {
		for _, tc := range []struct {
			name     string
			id       any
			orderID  any
			request  any
			observed any
		}{
			{name: "id null", id: nil, orderID: orderID, request: requestID, observed: now},
			{name: "payment order null", id: testUUID("30", 5), orderID: nil, request: requestID, observed: now},
			{name: "request id null", id: testUUID("30", 6), orderID: orderID, request: nil, observed: now},
			{name: "observed at null", id: testUUID("30", 7), orderID: orderID, request: requestID, observed: nil},
		} {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				inSavepoint(t, conn, "record_required_null", func() {
					expectReject(t, conn, tc.name, sqlStateNotNull, insertResultHandlingRecordSQL, recordValues(tc.id, tc.orderID, tc.request, "QUERY", tc.observed, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", "NO_MATCH", nil)...)
				})
			})
		}
		t.Run("unknown payment order", func(t *testing.T) {
			inSavepoint(t, conn, "record_unknown_order", func() {
				expectReject(t, conn, "unknown payment order", sqlStateForeignKey, insertResultHandlingRecordSQL, recordValues(testUUID("30", 8), testUUID("10", 999), requestID, "QUERY", now, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", "NO_MATCH", nil)...)
			})
		})
		t.Run("invalid request uuid", func(t *testing.T) {
			inSavepoint(t, conn, "record_invalid_request", func() {
				expectReject(t, conn, "invalid request uuid", sqlStateInvalidText, insertResultHandlingRecordSQL, recordValues(testUUID("30", 9), orderID, "not-a-uuid", "QUERY", now, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", "NO_MATCH", nil)...)
			})
		})
	})

	t.Run("source values", func(t *testing.T) {
		inSavepoint(t, conn, "source_accept", func() {
			mustExec(t, conn, "source PAYMENT", insertResultHandlingRecordSQL, recordValues(testUUID("30", 1), orderID, requestID, "PAYMENT", now, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", "NO_MATCH", nil)...)
			mustExec(t, conn, "source QUERY", insertResultHandlingRecordSQL, recordValues(testUUID("30", 2), orderID, testUUID("40", 2), "QUERY", now, "{}", "UNKNOWN", "SUCCEEDED", "ACCEPT_CONFIRMATION", nil, nil)...)
		})
		for _, tc := range []struct {
			name   string
			source string
		}{
			{name: "lowercase", source: "payment"},
			{name: "unknown", source: "OTHER"},
			{name: "empty", source: ""},
		} {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				inSavepoint(t, conn, "source_reject", func() {
					expectReject(t, conn, "source "+tc.name, sqlStateCheck, insertResultHandlingRecordSQL, recordValues(testUUID("30", 3), orderID, requestID, tc.source, now, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", "NO_MATCH", nil)...)
				})
			})
		}
		t.Run("null", func(t *testing.T) {
			inSavepoint(t, conn, "source_null", func() {
				expectReject(t, conn, "source null", sqlStateNotNull, insertResultHandlingRecordSQL, recordValues(testUUID("30", 4), orderID, requestID, nil, now, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", "NO_MATCH", nil)...)
			})
		})
	})

	t.Run("decision values", func(t *testing.T) {
		inSavepoint(t, conn, "decision_accept", func() {
			mustExec(t, conn, "decision accept confirmation", insertResultHandlingRecordSQL, recordValues(testUUID("30", 10), orderID, requestID, "PAYMENT", now, `{"amount":100,"currency":"CNY","result":"SUCCEEDED"}`, "UNKNOWN", "SUCCEEDED", "ACCEPT_CONFIRMATION", nil, nil)...)
			mustExec(t, conn, "decision keep unknown", insertResultHandlingRecordSQL, recordValues(testUUID("30", 11), orderID, requestID, "QUERY", now, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", "NO_MATCH", nil)...)
			mustExec(t, conn, "decision keep confirmed", insertResultHandlingRecordSQL, recordValues(testUUID("30", 12), orderID, requestID, "QUERY", now, "{}", "SUCCEEDED", "SUCCEEDED", "KEEP_CONFIRMED", nil, nil)...)
			mustExec(t, conn, "decision reject evidence", insertResultHandlingRecordSQL, recordValues(testUUID("30", 13), orderID, requestID, "QUERY", now, "{}", "UNKNOWN", "UNKNOWN", "REJECT_EVIDENCE", "MISMATCH", "mismatched evidence")...)
		})
		for _, tc := range []struct {
			name     string
			decision string
		}{
			{name: "accept", decision: "ACCEPT"},
			{name: "lowercase", decision: "keep_unknown"},
			{name: "empty", decision: ""},
		} {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				inSavepoint(t, conn, "decision_reject", func() {
					expectReject(t, conn, "decision "+tc.name, sqlStateCheck, insertResultHandlingRecordSQL, recordValues(testUUID("30", 14), orderID, requestID, "QUERY", now, "{}", "UNKNOWN", "UNKNOWN", tc.decision, "NO_MATCH", nil)...)
				})
			})
		}
		t.Run("null", func(t *testing.T) {
			inSavepoint(t, conn, "decision_null", func() {
				expectReject(t, conn, "decision null", sqlStateNotNull, insertResultHandlingRecordSQL, recordValues(testUUID("30", 15), orderID, requestID, "QUERY", now, "{}", "UNKNOWN", "UNKNOWN", nil, "NO_MATCH", nil)...)
			})
		})
	})

	t.Run("result values", func(t *testing.T) {
		inSavepoint(t, conn, "record_result_accept", func() {
			mustExec(t, conn, "result unknown to succeeded", insertResultHandlingRecordSQL, recordValues(testUUID("30", 20), orderID, requestID, "QUERY", now, "{}", "UNKNOWN", "SUCCEEDED", "ACCEPT_CONFIRMATION", nil, nil)...)
			mustExec(t, conn, "result succeeded to succeeded", insertResultHandlingRecordSQL, recordValues(testUUID("30", 21), orderID, requestID, "QUERY", now, "{}", "SUCCEEDED", "SUCCEEDED", "KEEP_CONFIRMED", nil, nil)...)
			mustExec(t, conn, "result failed to failed", insertResultHandlingRecordSQL, recordValues(testUUID("30", 22), orderID, requestID, "QUERY", now, "{}", "FAILED", "FAILED", "KEEP_CONFIRMED", nil, nil)...)
			mustExec(t, conn, "result unknown to failed", insertResultHandlingRecordSQL, recordValues(testUUID("30", 23), orderID, requestID, "QUERY", now, "{}", "UNKNOWN", "FAILED", "ACCEPT_CONFIRMATION", nil, nil)...)
		})
		for _, tc := range []struct {
			name   string
			before any
			after  any
		}{
			{name: "before pending", before: "PENDING", after: "UNKNOWN"},
			{name: "after pending", before: "UNKNOWN", after: "PENDING"},
			{name: "before lowercase", before: "succeeded", after: "UNKNOWN"},
			{name: "after lowercase", before: "UNKNOWN", after: "failed"},
			{name: "before empty", before: "", after: "UNKNOWN"},
			{name: "after empty", before: "UNKNOWN", after: ""},
		} {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				inSavepoint(t, conn, "record_result_reject", func() {
					expectReject(t, conn, tc.name, sqlStateCheck, insertResultHandlingRecordSQL, recordValues(testUUID("30", 24), orderID, requestID, "QUERY", now, "{}", tc.before, tc.after, "KEEP_UNKNOWN", "NO_MATCH", nil)...)
				})
			})
		}
		for _, tc := range []struct {
			name   string
			before any
			after  any
		}{
			{name: "before null", before: nil, after: "UNKNOWN"},
			{name: "after null", before: "UNKNOWN", after: nil},
		} {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				inSavepoint(t, conn, "record_result_null", func() {
					expectReject(t, conn, tc.name, sqlStateNotNull, insertResultHandlingRecordSQL, recordValues(testUUID("30", 25), orderID, requestID, "QUERY", now, "{}", tc.before, tc.after, "KEEP_UNKNOWN", "NO_MATCH", nil)...)
				})
			})
		}
	})

	t.Run("evidence object", func(t *testing.T) {
		inSavepoint(t, conn, "evidence_accept", func() {
			mustExec(t, conn, "empty evidence object", insertResultHandlingRecordSQL, recordValues(testUUID("30", 30), orderID, requestID, "QUERY", now, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", "NO_MATCH", nil)...)
			mustExec(t, conn, "allowed evidence fields", insertResultHandlingRecordSQL, recordValues(testUUID("30", 31), orderID, requestID, "QUERY", now, `{"platform_reference_id":"11111111-1111-4111-8111-111111111111","amount":100,"currency":"CNY","result":"SUCCEEDED"}`, "UNKNOWN", "SUCCEEDED", "ACCEPT_CONFIRMATION", nil, nil)...)
			mustExec(t, conn, "object without application whitelist", insertResultHandlingRecordSQL, recordValues(testUUID("30", 32), orderID, requestID, "QUERY", now, `{"unexpected":"value"}`, "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", "NO_MATCH", nil)...)
		})
		for _, tc := range []struct {
			name     string
			evidence any
		}{
			{name: "json null", evidence: "null"},
			{name: "array", evidence: "[]"},
			{name: "number", evidence: "1"},
			{name: "string", evidence: `"text"`},
			{name: "boolean", evidence: "true"},
		} {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				inSavepoint(t, conn, "evidence_reject", func() {
					expectReject(t, conn, "evidence "+tc.name, sqlStateCheck, insertResultHandlingRecordSQL, recordValues(testUUID("30", 33), orderID, requestID, "QUERY", now, tc.evidence, "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", "NO_MATCH", nil)...)
				})
			})
		}
		t.Run("sql null", func(t *testing.T) {
			inSavepoint(t, conn, "evidence_null", func() {
				expectReject(t, conn, "evidence null", sqlStateNotNull, insertResultHandlingRecordSQL, recordValues(testUUID("30", 34), orderID, requestID, "QUERY", now, nil, "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", "NO_MATCH", nil)...)
			})
		})
	})

	t.Run("reason lengths", func(t *testing.T) {
		inSavepoint(t, conn, "reason_accept", func() {
			mustExec(t, conn, "reason both null", insertResultHandlingRecordSQL, recordValues(testUUID("30", 40), orderID, requestID, "QUERY", now, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", nil, nil)...)
			mustExec(t, conn, "reason code one", insertResultHandlingRecordSQL, recordValues(testUUID("30", 41), orderID, requestID, "QUERY", now, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", "E", nil)...)
			mustExec(t, conn, "reason code 64 ascii", insertResultHandlingRecordSQL, recordValues(testUUID("30", 42), orderID, requestID, "QUERY", now, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", strings.Repeat("a", 64), nil)...)
			mustExec(t, conn, "reason code 64 chinese", insertResultHandlingRecordSQL, recordValues(testUUID("30", 43), orderID, requestID, "QUERY", now, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", strings.Repeat("中", 64), nil)...)
			mustExec(t, conn, "reason message 512 ascii", insertResultHandlingRecordSQL, recordValues(testUUID("30", 44), orderID, requestID, "QUERY", now, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", "E", strings.Repeat("a", 512))...)
			mustExec(t, conn, "reason message 512 chinese", insertResultHandlingRecordSQL, recordValues(testUUID("30", 45), orderID, requestID, "QUERY", now, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", "E", strings.Repeat("中", 512))...)
		})
		for _, tc := range []struct {
			name    string
			code    any
			message any
		}{
			{name: "code empty", code: "", message: nil},
			{name: "code 65 ascii", code: strings.Repeat("a", 65), message: nil},
			{name: "code 65 chinese", code: strings.Repeat("中", 65), message: nil},
			{name: "message 513 ascii", code: "E", message: strings.Repeat("a", 513)},
			{name: "message 513 chinese", code: "E", message: strings.Repeat("中", 513)},
		} {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				inSavepoint(t, conn, "reason_reject", func() {
					expectReject(t, conn, tc.name, sqlStateCheck, insertResultHandlingRecordSQL, recordValues(testUUID("30", 46), orderID, requestID, "QUERY", now, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", tc.code, tc.message)...)
				})
			})
		}
	})

	t.Run("foreign key restrict", func(t *testing.T) {
		inSavepoint(t, conn, "fk_restrict_outer", func() {
			recordID := testUUID("30", 50)
			mustExec(t, conn, "record for delete restriction", insertResultHandlingRecordSQL, recordValues(recordID, orderID, requestID, "QUERY", now, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", "NO_MATCH", nil)...)
			expectRejectAndRecover(t, conn, "fk_restrict_delete", "delete referenced order", sqlStateForeignKey, deletePaymentOrderSQL, orderID)

			var orderExists bool
			if err := conn.QueryRow(context.Background(), `SELECT EXISTS(SELECT 1 FROM payment_orders WHERE id = $1)`, orderID).Scan(&orderExists); err != nil {
				t.Fatalf("order existence query failed (SQLSTATE %s)", sqlStateOf(err))
			}
			if !orderExists {
				t.Fatalf("order was deleted despite RESTRICT foreign key")
			}
			var recordExists bool
			if err := conn.QueryRow(context.Background(), `SELECT EXISTS(SELECT 1 FROM result_handling_records WHERE id = $1)`, recordID).Scan(&recordExists); err != nil {
				t.Fatalf("record existence query failed (SQLSTATE %s)", sqlStateOf(err))
			}
			if !recordExists {
				t.Fatalf("handling record disappeared after rejected delete")
			}
		})
	})

	t.Run("foreign key definition", func(t *testing.T) {
		var total int
		if err := conn.QueryRow(context.Background(), `
			SELECT count(*)
			FROM pg_constraint c
			JOIN pg_class t ON t.oid = c.conrelid
			JOIN pg_namespace n ON n.oid = t.relnamespace
			WHERE n.nspname = current_schema() AND t.relname = 'result_handling_records' AND c.contype = 'f'
		`).Scan(&total); err != nil {
			t.Fatalf("foreign key metadata query failed (SQLSTATE %s)", sqlStateOf(err))
		}
		if total != 1 {
			t.Fatalf("expected exactly one foreign key on result_handling_records, got %d", total)
		}
		var restrictToOrders int
		if err := conn.QueryRow(context.Background(), `
			SELECT count(*)
			FROM pg_constraint c
			JOIN pg_class t ON t.oid = c.conrelid
			JOIN pg_namespace n ON n.oid = t.relnamespace
			JOIN pg_class rt ON rt.oid = c.confrelid
			WHERE n.nspname = current_schema()
			  AND t.relname = 'result_handling_records'
			  AND c.contype = 'f'
			  AND c.confdeltype = 'r'
			  AND rt.relname = 'payment_orders'
		`).Scan(&restrictToOrders); err != nil {
			t.Fatalf("foreign key action query failed (SQLSTATE %s)", sqlStateOf(err))
		}
		if restrictToOrders != 1 {
			t.Fatalf("expected one RESTRICT foreign key to payment_orders, got %d", restrictToOrders)
		}
	})

	t.Run("multiple records per request and order", func(t *testing.T) {
		inSavepoint(t, conn, "multiple_records", func() {
			firstID := testUUID("30", 60)
			secondID := testUUID("30", 61)
			mustExec(t, conn, "first record for same request and order", insertResultHandlingRecordSQL, recordValues(firstID, orderID, requestID, "QUERY", now, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", "NO_MATCH", nil)...)
			mustExec(t, conn, "second record for same request and order", insertResultHandlingRecordSQL, recordValues(secondID, orderID, requestID, "QUERY", now, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", "NO_MATCH", nil)...)
			var count int
			if err := conn.QueryRow(context.Background(), `SELECT count(*) FROM result_handling_records WHERE id IN ($1, $2)`, firstID, secondID).Scan(&count); err != nil {
				t.Fatalf("record count query failed (SQLSTATE %s)", sqlStateOf(err))
			}
			if count != 2 {
				t.Fatalf("expected two independent handling records, got %d", count)
			}
		})
	})

	t.Run("primary key collision", func(t *testing.T) {
		inSavepoint(t, conn, "record_pk_collision", func() {
			id := testUUID("30", 70)
			mustExec(t, conn, "record primary key first", insertResultHandlingRecordSQL, recordValues(id, orderID, requestID, "QUERY", now, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", "NO_MATCH", nil)...)
			expectReject(t, conn, "record primary key collision", sqlStateUnique, insertResultHandlingRecordSQL, recordValues(id, orderID, testUUID("40", 70), "QUERY", now, "{}", "UNKNOWN", "UNKNOWN", "KEEP_UNKNOWN", "NO_MATCH", nil)...)
		})
	})

	t.Run("indexes", func(t *testing.T) {
		indexes := loadIndexes(t, conn, "result_handling_records")
		requireIndexCount(t, "result_handling_records", indexes, 2)
		requireIndex(t, "result_handling_records", indexes, "id")
		requireIndex(t, "result_handling_records", indexes, "payment_order_id", "observed_at", "id")
		requireNoDuplicateIndexes(t, "result_handling_records", indexes)
	})
}
