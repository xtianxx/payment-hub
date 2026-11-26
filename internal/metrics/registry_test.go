package metrics

import (
	"net/http"
	"slices"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
)

func gatherNames(t *testing.T, registry *prometheus.Registry) []string {
	t.Helper()
	families, err := registry.Gather()
	if err != nil {
		t.Fatalf("gather registry: %v", err)
	}
	names := make([]string, 0, len(families))
	for _, family := range families {
		names = append(names, family.GetName())
	}
	return names
}

func TestNewRegistryIncludesRuntimeAndProcessCollectors(t *testing.T) {
	registry := NewRegistry()
	names := gatherNames(t, registry)

	for _, want := range []string{
		"go_goroutines",
		"go_memstats_heap_inuse_bytes",
		"go_gc_duration_seconds",
		"process_cpu_seconds_total",
		"process_resident_memory_bytes",
	} {
		if !slices.Contains(names, want) {
			t.Fatalf("registry missing %q; got %v", want, names)
		}
	}
}

func TestNewRegistryIsIsolated(t *testing.T) {
	first := NewRegistry()
	second := NewRegistry()
	firstHTTP := NewHTTPMetrics(first)
	firstHTTP.requests.WithLabelValues(http.MethodGet, "/readyz", "200").Inc()

	firstNames := gatherNames(t, first)
	secondNames := gatherNames(t, second)
	if slices.Contains(secondNames, "payment_hub_http_requests_total") {
		t.Fatalf("second registry unexpectedly contains HTTP metrics: %v", secondNames)
	}
	if !slices.Contains(firstNames, "payment_hub_http_requests_total") {
		t.Fatalf("first registry missing HTTP metrics: %v", firstNames)
	}
}
