package metrics

import (
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

// PoolStats is the narrow read-only view the pool collector needs. It is
// satisfied directly by *pgxpool.Stat, so this package never imports pgx and a
// single production pool can be shared without opening a second one.
type PoolStats interface {
	MaxConns() int32
	TotalConns() int32
	IdleConns() int32
	AcquiredConns() int32
	ConstructingConns() int32
	AcquireCount() int64
	AcquireDuration() time.Duration
	EmptyAcquireCount() int64
	CanceledAcquireCount() int64
	NewConnsCount() int64
	MaxIdleDestroyCount() int64
	MaxLifetimeDestroyCount() int64
}

// PoolMetrics exposes the single business pool through one prometheus
// collector. It reads a snapshot per scrape, adds no pool label, and never
// keeps values between scrapes.
type PoolMetrics struct {
	stats func() PoolStats

	maxConns          *prometheus.Desc
	totalConns        *prometheus.Desc
	idleConns         *prometheus.Desc
	inUseConns        *prometheus.Desc
	constructingConns *prometheus.Desc
	acquires          *prometheus.Desc
	acquireDuration   *prometheus.Desc
	emptyAcquires     *prometheus.Desc
	canceledAcquires  *prometheus.Desc
	createdConns      *prometheus.Desc
	destroyedMaxIdle  *prometheus.Desc
	destroyedLife     *prometheus.Desc
}

// NewPoolMetrics registers the pool collector on registerer. A nil registerer
// or nil stats function returns nil and registers nothing, so callers can keep
// the metrics listener optional without branching here.
func NewPoolMetrics(registerer prometheus.Registerer, stats func() PoolStats) *PoolMetrics {
	if registerer == nil || stats == nil {
		return nil
	}
	metrics := &PoolMetrics{
		stats: stats,
		maxConns: poolDesc(
			"db_pool_connections_max",
			"Maximum number of connections in the business pool.",
		),
		totalConns: poolDesc(
			"db_pool_connections",
			"Total number of connections in the business pool.",
		),
		idleConns: poolDesc(
			"db_pool_connections_idle",
			"Idle connections in the business pool.",
		),
		inUseConns: poolDesc(
			"db_pool_connections_in_use",
			"Connections currently acquired from the business pool.",
		),
		constructingConns: poolDesc(
			"db_pool_connections_constructing",
			"Connections currently being established in the business pool.",
		),
		acquires: poolDesc(
			"db_pool_acquires_total",
			"Total successful acquires from the business pool.",
		),
		acquireDuration: poolDesc(
			"db_pool_acquire_duration_seconds_total",
			"Cumulative time spent acquiring from the business pool, in seconds.",
		),
		emptyAcquires: poolDesc(
			"db_pool_empty_acquires_total",
			"Acquires that found no idle connection and had to wait.",
		),
		canceledAcquires: poolDesc(
			"db_pool_canceled_acquires_total",
			"Acquires canceled while waiting for the business pool.",
		),
		createdConns: poolDesc(
			"db_pool_connections_created_total",
			"Connections created in the business pool.",
		),
		destroyedMaxIdle: poolDesc(
			"db_pool_connections_destroyed_max_idle_total",
			"Connections destroyed after exceeding the idle time.",
		),
		destroyedLife: poolDesc(
			"db_pool_connections_destroyed_max_lifetime_total",
			"Connections destroyed after exceeding the maximum lifetime.",
		),
	}
	registerer.MustRegister(metrics)
	return metrics
}

func poolDesc(name, help string) *prometheus.Desc {
	return prometheus.NewDesc("payment_hub_"+name, help, nil, nil)
}

// Describe implements prometheus.Collector.
func (metrics *PoolMetrics) Describe(ch chan<- *prometheus.Desc) {
	ch <- metrics.maxConns
	ch <- metrics.totalConns
	ch <- metrics.idleConns
	ch <- metrics.inUseConns
	ch <- metrics.constructingConns
	ch <- metrics.acquires
	ch <- metrics.acquireDuration
	ch <- metrics.emptyAcquires
	ch <- metrics.canceledAcquires
	ch <- metrics.createdConns
	ch <- metrics.destroyedMaxIdle
	ch <- metrics.destroyedLife
}

// Collect implements prometheus.Collector. A nil snapshot emits nothing rather
// than fabricating zeros, so a missing pool stays visible as No data.
func (metrics *PoolMetrics) Collect(ch chan<- prometheus.Metric) {
	if metrics == nil || metrics.stats == nil {
		return
	}
	stats := metrics.stats()
	if stats == nil {
		return
	}
	ch <- mustGauge(metrics.maxConns, float64(stats.MaxConns()))
	ch <- mustGauge(metrics.totalConns, float64(stats.TotalConns()))
	ch <- mustGauge(metrics.idleConns, float64(stats.IdleConns()))
	ch <- mustGauge(metrics.inUseConns, float64(stats.AcquiredConns()))
	ch <- mustGauge(metrics.constructingConns, float64(stats.ConstructingConns()))
	ch <- mustCounter(metrics.acquires, float64(stats.AcquireCount()))
	ch <- mustCounter(metrics.acquireDuration, stats.AcquireDuration().Seconds())
	ch <- mustCounter(metrics.emptyAcquires, float64(stats.EmptyAcquireCount()))
	ch <- mustCounter(metrics.canceledAcquires, float64(stats.CanceledAcquireCount()))
	ch <- mustCounter(metrics.createdConns, float64(stats.NewConnsCount()))
	ch <- mustCounter(metrics.destroyedMaxIdle, float64(stats.MaxIdleDestroyCount()))
	ch <- mustCounter(metrics.destroyedLife, float64(stats.MaxLifetimeDestroyCount()))
}

func mustGauge(desc *prometheus.Desc, value float64) prometheus.Metric {
	return prometheus.MustNewConstMetric(desc, prometheus.GaugeValue, value)
}

func mustCounter(desc *prometheus.Desc, value float64) prometheus.Metric {
	return prometheus.MustNewConstMetric(desc, prometheus.CounterValue, value)
}
