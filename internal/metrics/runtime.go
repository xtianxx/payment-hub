package metrics

import (
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// Runtime bundles the isolated registry, the scrape handler and the HTTP
// middleware so the application can share one registry across both listeners.
type Runtime struct {
	Registry *prometheus.Registry
	Handler  http.Handler
	HTTP     *HTTPMetrics
}

// NewRuntime builds a fresh runtime with Go, process and HTTP metrics. The
// handler serves only GET /metrics; other paths return 404 and other methods
// return 405, so a misconfigured scrape path fails loudly instead of silently
// scraping the same data.
func NewRuntime() *Runtime {
	registry := NewRegistry()
	httpMetrics := NewHTTPMetrics(registry)
	scrape := promhttp.HandlerFor(registry, promhttp.HandlerOpts{
		MaxRequestsInFlight: 2,
		Timeout:             4 * time.Second,
	})
	mux := http.NewServeMux()
	mux.Handle("GET /metrics", scrape)
	return &Runtime{
		Registry: registry,
		Handler:  mux,
		HTTP:     httpMetrics,
	}
}

// Middleware returns the HTTP middleware for this runtime. A nil runtime
// returns a pass-through middleware so callers do not need a separate branch.
func (runtime *Runtime) Middleware() gin.HandlerFunc {
	if runtime == nil || runtime.HTTP == nil {
		return func(c *gin.Context) { c.Next() }
	}
	return runtime.HTTP.Middleware()
}
