package app

import (
	"context"
	"errors"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"
)

type metricsPoolDB struct {
	pool              *pgxpool.Pool
	businessPoolCalls int
}

func (db *metricsPoolDB) Ping(context.Context) error            { return nil }
func (db *metricsPoolDB) CheckMigrations(context.Context) error { return nil }
func (db *metricsPoolDB) Close()                                {}
func (db *metricsPoolDB) BusinessPool() *pgxpool.Pool {
	db.businessPoolCalls++
	return db.pool
}

func TestMetricsRuntimeUsesExistingBusinessPool(t *testing.T) {
	config, err := pgxpool.ParseConfig("postgres://metrics:metrics@127.0.0.1:1/metrics?sslmode=disable")
	if err != nil {
		t.Fatalf("parse config: %v", err)
	}
	config.MaxConns = 3
	config.MinConns = 0
	pool, err := pgxpool.NewWithConfig(context.Background(), config)
	if err != nil {
		t.Fatalf("new pool: %v", err)
	}
	t.Cleanup(pool.Close)

	db := &metricsPoolDB{pool: pool}
	runtime, err := realRuntimeDeps().newMetricsRuntime(context.Background(), db)
	if err != nil {
		t.Fatalf("new metrics runtime: %v", err)
	}
	if db.businessPoolCalls != 1 {
		t.Fatalf("BusinessPool calls = %d, want 1 (no second pool)", db.businessPoolCalls)
	}

	families, err := runtime.Registry.Gather()
	if err != nil {
		t.Fatalf("gather registry: %v", err)
	}
	found := false
	for _, family := range families {
		if family.GetName() != "payment_hub_db_pool_connections_max" {
			continue
		}
		found = true
		metric := family.GetMetric()[0]
		if metric.GetGauge() == nil || metric.GetGauge().GetValue() != 3 {
			t.Fatalf("pool max metric = %v, want gauge 3", metric)
		}
		if len(metric.GetLabel()) != 0 {
			t.Fatalf("pool max metric must not carry labels: %v", metric.GetLabel())
		}
	}
	if !found {
		t.Fatal("metrics runtime did not register the business pool collector")
	}
}

func TestMetricsRuntimeFailsWithoutBusinessPool(t *testing.T) {
	runtime, err := realRuntimeDeps().newMetricsRuntime(context.Background(), newLifecycleTestDB())
	if !errors.Is(err, ErrStartup) {
		t.Fatalf("err = %v, want ErrStartup", err)
	}
	if runtime != nil {
		t.Fatal("metrics runtime must be nil when the business pool is unavailable")
	}
}
