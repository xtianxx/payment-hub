package metrics

import (
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

// The names and types here mirror contracts/metrics.md. A mismatch is a
// contract change and must update the contract, dashboard and docs first.
func TestPoolMetricNamesAndTypesMatchContract(t *testing.T) {
	registry := prometheus.NewRegistry()
	NewPoolMetrics(registry, func() PoolStats { return fakePoolStats{maxConns: 4} })

	families, err := registry.Gather()
	if err != nil {
		t.Fatalf("gather registry: %v", err)
	}

	wantGauges := map[string]bool{
		"payment_hub_db_pool_connections_max":          true,
		"payment_hub_db_pool_connections":              true,
		"payment_hub_db_pool_connections_idle":         true,
		"payment_hub_db_pool_connections_in_use":       true,
		"payment_hub_db_pool_connections_constructing": true,
	}
	wantCounters := map[string]bool{
		"payment_hub_db_pool_acquires_total":                           true,
		"payment_hub_db_pool_acquire_duration_seconds_total":           true,
		"payment_hub_db_pool_empty_acquires_total":                     true,
		"payment_hub_db_pool_canceled_acquires_total":                  true,
		"payment_hub_db_pool_connections_created_total":                true,
		"payment_hub_db_pool_connections_destroyed_max_idle_total":     true,
		"payment_hub_db_pool_connections_destroyed_max_lifetime_total": true,
	}
	if got := len(families); got != len(wantGauges)+len(wantCounters) {
		t.Fatalf("family count = %d, want %d", got, len(wantGauges)+len(wantCounters))
	}
	for _, family := range families {
		name := family.GetName()
		if family.GetHelp() == "" {
			t.Fatalf("%s has empty help text", name)
		}
		if len(family.GetMetric()) != 1 {
			t.Fatalf("%s metric count = %d, want 1", name, len(family.GetMetric()))
		}
		metric := family.GetMetric()[0]
		if len(metric.GetLabel()) != 0 {
			t.Fatalf("%s must not carry labels: %v", name, metric.GetLabel())
		}
		switch {
		case wantGauges[name]:
			if metric.GetGauge() == nil {
				t.Fatalf("%s type = %v, want gauge", name, family.GetType())
			}
			if strings.HasSuffix(name, "_total") {
				t.Fatalf("gauge %s must not use the _total suffix", name)
			}
		case wantCounters[name]:
			if metric.GetCounter() == nil {
				t.Fatalf("%s type = %v, want counter", name, family.GetType())
			}
			if !strings.HasSuffix(name, "_total") {
				t.Fatalf("counter %s must use the _total suffix", name)
			}
		default:
			t.Fatalf("unexpected pool metric %s", name)
		}
	}
}

func TestPoolMetricDurationIsSeconds(t *testing.T) {
	registry := prometheus.NewRegistry()
	NewPoolMetrics(registry, func() PoolStats {
		return fakePoolStats{acquireDuration: 2500 * time.Millisecond}
	})
	values := gatherPoolValues(t, registry)
	if got := values["payment_hub_db_pool_acquire_duration_seconds_total"]; got != 2.5 {
		t.Fatalf("acquire duration = %v, want 2.5 seconds", got)
	}
}
