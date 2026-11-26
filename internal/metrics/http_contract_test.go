package metrics

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/prometheus/client_golang/prometheus"
	dto "github.com/prometheus/client_model/go"
)

func newContractRouter(t *testing.T) (*gin.Engine, *prometheus.Registry) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	registry := prometheus.NewRegistry()
	httpMetrics := NewHTTPMetrics(registry)

	router := gin.New()
	router.Use(httpMetrics.Middleware())
	router.GET("/readyz", func(c *gin.Context) { c.Status(http.StatusOK) })
	router.POST("/api/v1/payments", func(c *gin.Context) { c.Status(http.StatusCreated) })
	router.GET("/api/v1/payments", func(c *gin.Context) { c.Status(http.StatusOK) })
	router.GET("/api/v1/payments/:payment_id", func(c *gin.Context) { c.Status(http.StatusOK) })
	router.GET("/unauthorized", func(c *gin.Context) { c.Status(http.StatusUnauthorized) })
	router.GET("/server-error", func(c *gin.Context) { c.Status(http.StatusInternalServerError) })
	return router, registry
}

func serveContractRequest(t *testing.T, router *gin.Engine, method, path string) int {
	t.Helper()
	request := httptest.NewRequest(method, path, nil)
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	return recorder.Code
}

func metricFamily(t *testing.T, registry *prometheus.Registry, name string) *dto.MetricFamily {
	t.Helper()
	families, err := registry.Gather()
	if err != nil {
		t.Fatalf("gather registry: %v", err)
	}
	for _, family := range families {
		if family.GetName() == name {
			return family
		}
	}
	return nil
}

func metricLabels(metric *dto.Metric) map[string]string {
	labels := make(map[string]string, len(metric.GetLabel()))
	for _, pair := range metric.GetLabel() {
		labels[pair.GetName()] = pair.GetValue()
	}
	return labels
}

func requestSeries(family *dto.MetricFamily) map[string]bool {
	series := make(map[string]bool, len(family.GetMetric()))
	for _, metric := range family.GetMetric() {
		labels := metricLabels(metric)
		key := labels["method"] + "|" + labels["route"] + "|" + labels["status"]
		series[key] = true
	}
	return series
}

func TestHTTPMetricsUsesRouteTemplatesAndSkipsReadyz(t *testing.T) {
	router, registry := newContractRouter(t)

	if code := serveContractRequest(t, router, http.MethodPost, "/api/v1/payments"); code != http.StatusCreated {
		t.Fatalf("POST payment status = %d", code)
	}
	if code := serveContractRequest(t, router, http.MethodGet, "/api/v1/payments/abc"); code != http.StatusOK {
		t.Fatalf("GET payment status = %d", code)
	}
	if code := serveContractRequest(t, router, http.MethodGet, "/missing"); code != http.StatusNotFound {
		t.Fatalf("unmatched status = %d", code)
	}
	if code := serveContractRequest(t, router, http.MethodGet, "/readyz"); code != http.StatusOK {
		t.Fatalf("readyz status = %d", code)
	}
	if code := serveContractRequest(t, router, http.MethodGet, "/metrics"); code != http.StatusNotFound {
		t.Fatalf("business /metrics status = %d", code)
	}
	if code := serveContractRequest(t, router, "FOOBAR", "/api/v1/payments"); code != http.StatusNotFound {
		t.Fatalf("unknown method status = %d", code)
	}
	if code := serveContractRequest(t, router, http.MethodGet, "/unauthorized"); code != http.StatusUnauthorized {
		t.Fatalf("unauthorized status = %d", code)
	}

	family := metricFamily(t, registry, "payment_hub_http_requests_total")
	if family == nil {
		t.Fatal("missing payment_hub_http_requests_total")
	}
	series := requestSeries(family)
	for _, want := range []string{
		"POST|/api/v1/payments|201",
		"GET|/api/v1/payments/:payment_id|200",
		"GET|unmatched|404",
		"GET|/unauthorized|401",
		"OTHER|unmatched|404",
	} {
		if !series[want] {
			t.Fatalf("missing request series %q; got %v", want, series)
		}
	}
	for key := range series {
		if strings.Contains(key, "/readyz") || strings.Contains(key, "/metrics") {
			t.Fatalf("probe or metrics path must not be recorded: %v", series)
		}
		if strings.Contains(key, "123e4567") || strings.Contains(key, "biz-") {
			t.Fatalf("business identifier leaked into labels: %v", series)
		}
	}

	for _, metric := range family.GetMetric() {
		labels := metricLabels(metric)
		if len(labels) != 3 || labels["method"] == "" || labels["route"] == "" || labels["status"] == "" {
			t.Fatalf("unexpected request labels: %v", labels)
		}
	}
}

func TestHTTPMetricsHistogramUsesConfiguredBuckets(t *testing.T) {
	router, registry := newContractRouter(t)
	serveContractRequest(t, router, http.MethodGet, "/api/v1/payments")

	family := metricFamily(t, registry, "payment_hub_http_request_duration_seconds")
	if family == nil {
		t.Fatal("missing payment_hub_http_request_duration_seconds")
	}
	if len(family.GetMetric()) != 1 {
		t.Fatalf("expected one route histogram, got %d", len(family.GetMetric()))
	}
	histogram := family.GetMetric()[0].GetHistogram()
	if histogram == nil {
		t.Fatal("duration metric is not a histogram")
	}
	if got := len(histogram.GetBucket()); got != len(HTTPDurationBuckets) {
		t.Fatalf("bucket count = %d, want %d", got, len(HTTPDurationBuckets))
	}
	if got := family.GetMetric()[0].GetHistogram().GetSampleCount(); got != 1 {
		t.Fatalf("sample count = %d, want 1", got)
	}
}
