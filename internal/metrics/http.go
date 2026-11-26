package metrics

import (
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/prometheus/client_golang/prometheus"
)

// httpDurationBuckets covers the current 10-25 ms P95 with extra resolution
// between 5 and 30 ms. Each route template adds one exported bucket series per
// boundary plus the implicit +Inf series. The slice is private so a caller
// cannot change the boundaries of already-created or future registries.
var httpDurationBuckets = []float64{
	0.001, 0.0025, 0.005, 0.0075, 0.01, 0.0125,
	0.015, 0.0175, 0.02, 0.025, 0.03, 0.04,
	0.05, 0.075, 0.1, 0.25, 0.5, 1, 2.5, 5,
}

// HTTPDurationBuckets returns a copy of the canonical finite bucket
// boundaries. Callers may inspect or format the copy without affecting
// registries built by NewHTTPMetrics.
func HTTPDurationBuckets() []float64 {
	return append([]float64(nil), httpDurationBuckets...)
}

// HTTPMetrics owns the request counter and duration histogram.
type HTTPMetrics struct {
	requests *prometheus.CounterVec
	duration *prometheus.HistogramVec
}

// NewHTTPMetrics creates and registers the HTTP series on registerer.
func NewHTTPMetrics(registerer prometheus.Registerer) *HTTPMetrics {
	metrics := &HTTPMetrics{
		requests: prometheus.NewCounterVec(prometheus.CounterOpts{
			Namespace: "payment_hub",
			Name:      "http_requests_total",
			Help:      "Total number of completed HTTP requests.",
		}, []string{"method", "route", "status"}),
		duration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Namespace: "payment_hub",
			Name:      "http_request_duration_seconds",
			Help:      "HTTP request duration from entering the API route group to handler return.",
			Buckets:   httpDurationBuckets,
		}, []string{"method", "route"}),
	}
	registerer.MustRegister(metrics.requests, metrics.duration)
	return metrics
}

// Middleware records one counter sample and one histogram observation per
// completed business request. /readyz and the business-port /metrics path are
// excluded so probes and misplaced scrapes do not pollute business metrics.
// The route label uses the Gin template, never the raw path. Methods other
// than GET and POST collapse to OTHER to keep the label set bounded.
func (metrics *HTTPMetrics) Middleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		switch c.Request.URL.Path {
		case "/readyz", "/metrics":
			c.Next()
			return
		}

		start := time.Now()
		c.Next()

		route := c.FullPath()
		if route == "" {
			route = "unmatched"
		}
		metrics.requests.WithLabelValues(
			requestMethod(c.Request.Method),
			route,
			strconv.Itoa(c.Writer.Status()),
		).Inc()
		metrics.duration.WithLabelValues(
			requestMethod(c.Request.Method),
			route,
		).Observe(time.Since(start).Seconds())
	}
}

func requestMethod(method string) string {
	switch method {
	case http.MethodGet, http.MethodPost:
		return method
	default:
		return "OTHER"
	}
}
