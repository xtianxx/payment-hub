package app

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"

	"payment-hub/internal/metrics"
)

type metricsPoolStats struct {
	maxConns        int32
	acquireCount    int64
	emptyAcquires   int64
	acquireDuration time.Duration
}

func (stats metricsPoolStats) MaxConns() int32                { return stats.maxConns }
func (metricsPoolStats) TotalConns() int32                    { return 5 }
func (metricsPoolStats) IdleConns() int32                     { return 2 }
func (metricsPoolStats) AcquiredConns() int32                 { return 3 }
func (metricsPoolStats) ConstructingConns() int32             { return 1 }
func (stats metricsPoolStats) AcquireCount() int64            { return stats.acquireCount }
func (stats metricsPoolStats) AcquireDuration() time.Duration { return stats.acquireDuration }
func (stats metricsPoolStats) EmptyAcquireCount() int64       { return stats.emptyAcquires }
func (metricsPoolStats) CanceledAcquireCount() int64          { return 1 }
func (metricsPoolStats) NewConnsCount() int64                 { return 9 }
func (metricsPoolStats) MaxIdleDestroyCount() int64           { return 2 }
func (metricsPoolStats) MaxLifetimeDestroyCount() int64       { return 1 }

type metricsPoolDB struct {
	stats metrics.PoolStats
	calls int
}

func (db *metricsPoolDB) Ping(context.Context) error            { return nil }
func (db *metricsPoolDB) CheckMigrations(context.Context) error { return nil }
func (db *metricsPoolDB) Close()                                {}
func (db *metricsPoolDB) BusinessPoolStats() metrics.PoolStats {
	db.calls++
	return db.stats
}

func TestMetricsRuntimeUsesProvidedPoolStats(t *testing.T) {
	db := &metricsPoolDB{stats: metricsPoolStats{
		maxConns:        41,
		acquireCount:    17,
		emptyAcquires:   4,
		acquireDuration: 1250 * time.Millisecond,
	}}
	runtime, err := realRuntimeDeps().newMetricsRuntime(context.Background(), db)
	if err != nil {
		t.Fatalf("new metrics runtime: %v", err)
	}
	if db.calls != 1 {
		t.Fatalf("BusinessPoolStats calls before scrape = %d, want 1", db.calls)
	}

	values := gatherAppMetrics(t, runtime.Registry)
	for name, want := range map[string]float64{
		"payment_hub_db_pool_connections_max":                41,
		"payment_hub_db_pool_connections":                    5,
		"payment_hub_db_pool_connections_idle":               2,
		"payment_hub_db_pool_connections_in_use":             3,
		"payment_hub_db_pool_connections_constructing":       1,
		"payment_hub_db_pool_acquires_total":                 17,
		"payment_hub_db_pool_acquire_duration_seconds_total": 1.25,
		"payment_hub_db_pool_empty_acquires_total":           4,
	} {
		if got := values[name]; got != want {
			t.Fatalf("%s = %v, want %v", name, got, want)
		}
	}
	if db.calls != 2 {
		t.Fatalf("BusinessPoolStats calls after one scrape = %d, want 2", db.calls)
	}
}

func TestMetricsRuntimeFailsWithoutPoolStatsProvider(t *testing.T) {
	runtime, err := realRuntimeDeps().newMetricsRuntime(context.Background(), newLifecycleTestDB())
	if !errors.Is(err, ErrStartup) {
		t.Fatalf("err = %v, want ErrStartup", err)
	}
	if runtime != nil {
		t.Fatal("metrics runtime must be nil when the business pool is unavailable")
	}
}

func TestProductionDatabasePublishesOnePoolStatsSource(t *testing.T) {
	var missing *pgxpoolRuntimeDatabase
	if missing.BusinessPoolStats() != nil {
		t.Fatal("nil database must not publish pool stats")
	}

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

	database := &pgxpoolRuntimeDatabase{pool: pool}
	stats := database.BusinessPoolStats()
	if stats == nil {
		t.Fatal("production database did not expose pool stats")
	}
	if stats.MaxConns() != 3 {
		t.Fatalf("max connections = %d, want 3", stats.MaxConns())
	}
}

func gatherAppMetrics(t *testing.T, registry *prometheus.Registry) map[string]float64 {
	t.Helper()
	families, err := registry.Gather()
	if err != nil {
		t.Fatalf("gather registry: %v", err)
	}
	values := make(map[string]float64, len(families))
	for _, family := range families {
		if len(family.GetMetric()) != 1 {
			continue
		}
		metric := family.GetMetric()[0]
		switch {
		case metric.GetGauge() != nil:
			values[family.GetName()] = metric.GetGauge().GetValue()
		case metric.GetCounter() != nil:
			values[family.GetName()] = metric.GetCounter().GetValue()
		}
	}
	return values
}
