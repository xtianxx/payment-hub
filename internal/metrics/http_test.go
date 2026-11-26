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

	serveContractRequest(t, router, http.MethodGet, "/server-error")
	family = metricFamily(t, registry, "payment_hub_http_requests_total")
	if !hasStatusPrefix(family, "5") {
		t.Fatal("5xx series missing after a 5xx response")
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
