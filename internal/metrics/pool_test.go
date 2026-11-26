package metrics

import (
	"context"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"
)

type fakePoolStats struct {
	maxConns          int32
	totalConns        int32
	idleConns         int32
	acquiredConns     int32
	constructingConns int32
	acquireCount      int64
	acquireDuration   time.Duration
	emptyAcquireCount int64
	canceledAcquires  int64
	newConnsCount     int64
	maxIdleDestroy    int64
	maxLifetimeDest   int64
}

func (stats fakePoolStats) MaxConns() int32          { return stats.maxConns }
func (stats fakePoolStats) TotalConns() int32        { return stats.totalConns }
func (stats fakePoolStats) IdleConns() int32         { return stats.idleConns }
func (stats fakePoolStats) AcquiredConns() int32     { return stats.acquiredConns }
func (stats fakePoolStats) ConstructingConns() int32 { return stats.constructingConns }
func (stats fakePoolStats) AcquireCount() int64      { return stats.acquireCount }
func (stats fakePoolStats) AcquireDuration() time.Duration {
	return stats.acquireDuration
}
func (stats fakePoolStats) EmptyAcquireCount() int64       { return stats.emptyAcquireCount }
func (stats fakePoolStats) CanceledAcquireCount() int64    { return stats.canceledAcquires }
func (stats fakePoolStats) NewConnsCount() int64           { return stats.newConnsCount }
func (stats fakePoolStats) MaxIdleDestroyCount() int64     { return stats.maxIdleDestroy }
func (stats fakePoolStats) MaxLifetimeDestroyCount() int64 { return stats.maxLifetimeDest }

func gatherPoolValues(t *testing.T, registry *prometheus.Registry) map[string]float64 {
	t.Helper()
	families, err := registry.Gather()
	if err != nil {
		t.Fatalf("gather registry: %v", err)
	}
	values := make(map[string]float64, len(families))
	for _, family := range families {
		if len(family.GetMetric()) != 1 {
			t.Fatalf("%s metric count = %d, want 1", family.GetName(), len(family.GetMetric()))
		}
		metric := family.GetMetric()[0]
		if len(metric.GetLabel()) != 0 {
			t.Fatalf("%s must not carry labels: %v", family.GetName(), metric.GetLabel())
		}
		switch {
		case metric.GetGauge() != nil:
			values[family.GetName()] = metric.GetGauge().GetValue()
		case metric.GetCounter() != nil:
			values[family.GetName()] = metric.GetCounter().GetValue()
		default:
			t.Fatalf("%s is neither gauge nor counter", family.GetName())
		}
	}
	return values
}

func TestPoolMetricsMapSnapshotValues(t *testing.T) {
	stats := fakePoolStats{
		maxConns:          8,
		totalConns:        5,
		idleConns:         3,
		acquiredConns:     2,
		constructingConns: 1,
		acquireCount:      11,
		acquireDuration:   1500 * time.Millisecond,
		emptyAcquireCount: 4,
		canceledAcquires:  2,
		newConnsCount:     6,
		maxIdleDestroy:    1,
		maxLifetimeDest:   3,
	}
	registry := prometheus.NewRegistry()
	NewPoolMetrics(registry, func() PoolStats { return stats })

	want := map[string]float64{
		"payment_hub_db_pool_connections_max":                          8,
		"payment_hub_db_pool_connections":                              5,
		"payment_hub_db_pool_connections_idle":                         3,
		"payment_hub_db_pool_connections_in_use":                       2,
		"payment_hub_db_pool_connections_constructing":                 1,
		"payment_hub_db_pool_acquires_total":                           11,
		"payment_hub_db_pool_acquire_duration_seconds_total":           1.5,
		"payment_hub_db_pool_empty_acquires_total":                     4,
		"payment_hub_db_pool_canceled_acquires_total":                  2,
		"payment_hub_db_pool_connections_created_total":                6,
		"payment_hub_db_pool_connections_destroyed_max_idle_total":     1,
		"payment_hub_db_pool_connections_destroyed_max_lifetime_total": 3,
	}
	got := gatherPoolValues(t, registry)
	if len(got) != len(want) {
		t.Fatalf("metric count = %d, want %d: %v", len(got), len(want), got)
	}
	for name, value := range want {
		if got[name] != value {
			t.Fatalf("%s = %v, want %v", name, got[name], value)
		}
	}
}

func TestPoolMetricsReadsRealPGXPoolSnapshot(t *testing.T) {
	config, err := pgxpool.ParseConfig("postgres://metrics:metrics@127.0.0.1:1/metrics?sslmode=disable")
	if err != nil {
		t.Fatalf("parse config: %v", err)
	}
	config.MaxConns = 2
	config.MinConns = 0
	pool, err := pgxpool.NewWithConfig(context.Background(), config)
	if err != nil {
		t.Fatalf("new pool: %v", err)
	}
	t.Cleanup(pool.Close)

	registry := prometheus.NewRegistry()
	NewPoolMetrics(registry, func() PoolStats { return pool.Stat() })
	got := gatherPoolValues(t, registry)

	if got["payment_hub_db_pool_connections_max"] != 2 {
		t.Fatalf("max connections = %v, want 2", got["payment_hub_db_pool_connections_max"])
	}
	for _, name := range []string{
		"payment_hub_db_pool_connections",
		"payment_hub_db_pool_connections_idle",
		"payment_hub_db_pool_connections_in_use",
		"payment_hub_db_pool_connections_constructing",
		"payment_hub_db_pool_acquires_total",
		"payment_hub_db_pool_acquire_duration_seconds_total",
	} {
		if got[name] != 0 {
			t.Fatalf("%s = %v, want 0 before any acquire", name, got[name])
		}
	}
}

func TestPoolMetricsNilInputsRegisterNothing(t *testing.T) {
	registry := prometheus.NewRegistry()
	if NewPoolMetrics(registry, nil) != nil {
		t.Fatal("nil stats must not create a collector")
	}
	if NewPoolMetrics(nil, func() PoolStats { return fakePoolStats{} }) != nil {
		t.Fatal("nil registerer must not create a collector")
	}
	families, err := registry.Gather()
	if err != nil {
		t.Fatalf("gather registry: %v", err)
	}
	if len(families) != 0 {
		t.Fatalf("registry has %d families, want 0", len(families))
	}
}

var _ PoolStats = (*pgxpool.Stat)(nil)
