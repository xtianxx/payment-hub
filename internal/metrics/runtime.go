package metrics

import (
	"net"
	"net/http"
	"strings"
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
// handler serves only GET /metrics. Other paths return 404, other methods
// return 405 with an Allow header, and requests whose Host is not a loopback
// literal are rejected with 403 so a DNS-rebinding page cannot read the
// unauthenticated scrape through a victim browser.
func NewRuntime() *Runtime {
	return NewRuntimeWithPool(nil)
}

// NewRuntimeWithPool also collects the single business pool from the given
// snapshot function. A nil function keeps the pool collector unregistered, so
// the HTTP-only runtime stays usable in tests; production always passes the
// existing pool's Stat method and never opens a second pool.
func NewRuntimeWithPool(pool func() PoolStats) *Runtime {
	registry := NewRegistry()
	httpMetrics := NewHTTPMetrics(registry)
	if pool != nil {
		NewPoolMetrics(registry, pool)
	}
	scrape := promhttp.HandlerFor(registry, promhttp.HandlerOpts{
		MaxRequestsInFlight: 2,
		Timeout:             4 * time.Second,
	})
	return &Runtime{
		Registry: registry,
		Handler:  newScrapeHandler(scrape),
		HTTP:     httpMetrics,
	}
}

func newScrapeHandler(scrape http.Handler) http.Handler {
	return http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/metrics" {
			http.NotFound(writer, request)
			return
		}
		if request.Method != http.MethodGet {
			writer.Header().Set("Allow", http.MethodGet)
			http.Error(writer, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		if !isLoopbackHost(request.Host) {
			http.Error(writer, "forbidden", http.StatusForbidden)
			return
		}
		scrape.ServeHTTP(writer, request)
	})
}

// isLoopbackHost reports whether the Host header names the local machine by a
// loopback literal or the conventional localhost name. Anything else, including
// an attacker-controlled rebinding domain, is rejected.
func isLoopbackHost(hostport string) bool {
	if hostport == "" {
		return false
	}
	host := hostport
	if splitHost, _, err := net.SplitHostPort(hostport); err == nil {
		host = splitHost
	} else if strings.HasPrefix(host, "[") && strings.HasSuffix(host, "]") {
		host = strings.TrimSuffix(strings.TrimPrefix(host, "["), "]")
	}
	if strings.EqualFold(host, "localhost") {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

// Middleware returns the HTTP middleware for this runtime. A nil runtime
// returns a pass-through middleware so callers do not need a separate branch.
func (runtime *Runtime) Middleware() gin.HandlerFunc {
	if runtime == nil || runtime.HTTP == nil {
		return func(c *gin.Context) { c.Next() }
	}
	return runtime.HTTP.Middleware()
}
