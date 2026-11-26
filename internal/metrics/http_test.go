package metrics

import (
	"net/http"
	"strings"
	"testing"

	dto "github.com/prometheus/client_model/go"
)

func TestHTTPMetricsDoesNotFabricate5xxSeries(t *testing.T) {
	router, registry := newContractRouter(t)
	serveContractRequest(t, router, http.MethodGet, "/api/v1/payments")

	family := metricFamily(t, registry, "payment_hub_http_requests_total")
	if hasStatusPrefix(family, "5") {
		t.Fatal("5xx series must not exist before a 5xx response")
	}
	if got := requestCounts(family)["GET|/api/v1/payments|200"]; got != 1 {
		t.Fatalf("200 request count = %v, want 1", got)
	}

	serveContractRequest(t, router, http.MethodGet, "/server-error")
	family = metricFamily(t, registry, "payment_hub_http_requests_total")
	if !hasStatusPrefix(family, "5") {
		t.Fatal("5xx series missing after a 5xx response")
	}
	if got := requestCounts(family)["GET|/server-error|500"]; got != 1 {
		t.Fatalf("500 request count = %v, want 1", got)
	}
}

func TestHTTPDurationBucketsAreStableAndCopied(t *testing.T) {
	buckets := HTTPDurationBuckets()
	if len(buckets) != 20 {
		t.Fatalf("bucket count = %d, want 20", len(buckets))
	}
	for index := 1; index < len(buckets); index++ {
		if buckets[index] <= buckets[index-1] {
			t.Fatalf("bucket boundaries are not strictly increasing at %d: %v", index, buckets)
		}
	}

	buckets[0] = 99
	if got := HTTPDurationBuckets()[0]; got != 0.001 {
		t.Fatalf("mutating the returned copy changed the canonical buckets: first = %v", got)
	}
}

func hasStatusPrefix(family *dto.MetricFamily, prefix string) bool {
	for _, metric := range family.GetMetric() {
		if strings.HasPrefix(metricLabels(metric)["status"], prefix) {
			return true
		}
	}
	return false
}
