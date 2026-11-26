package metrics

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func serveScrape(runtime *Runtime, method, path, host string) *httptest.ResponseRecorder {
	request := httptest.NewRequest(method, path, nil)
	if host != "" {
		request.Host = host
	}
	recorder := httptest.NewRecorder()
	runtime.Handler.ServeHTTP(recorder, request)
	return recorder
}

func TestRuntimeHandlerOnlyServesMetricsPath(t *testing.T) {
	runtime := NewRuntime()

	tests := []struct {
		method string
		path   string
		host   string
		want   int
	}{
		{method: http.MethodGet, path: "/metrics", host: "127.0.0.1:9101", want: http.StatusOK},
		{method: http.MethodGet, path: "/metrics", host: "[::1]:9101", want: http.StatusOK},
		{method: http.MethodGet, path: "/metrics", host: "localhost:9101", want: http.StatusOK},
		{method: http.MethodGet, path: "/", host: "127.0.0.1:9101", want: http.StatusNotFound},
		{method: http.MethodGet, path: "/metrics/", host: "127.0.0.1:9101", want: http.StatusNotFound},
		{method: http.MethodGet, path: "/readyz", host: "127.0.0.1:9101", want: http.StatusNotFound},
		{method: http.MethodPost, path: "/metrics", host: "127.0.0.1:9101", want: http.StatusMethodNotAllowed},
		{method: http.MethodHead, path: "/metrics", host: "127.0.0.1:9101", want: http.StatusMethodNotAllowed},
	}
	for _, test := range tests {
		t.Run(test.method+" "+test.path+" host="+test.host, func(t *testing.T) {
			recorder := serveScrape(runtime, test.method, test.path, test.host)
			if recorder.Code != test.want {
				t.Fatalf("status = %d, want %d", recorder.Code, test.want)
			}
			if test.want == http.StatusMethodNotAllowed && recorder.Header().Get("Allow") != http.MethodGet {
				t.Fatalf("Allow = %q, want GET", recorder.Header().Get("Allow"))
			}
		})
	}
}

func TestRuntimeHandlerRejectsNonLoopbackHost(t *testing.T) {
	runtime := NewRuntime()
	for _, host := range []string{
		"attacker.example:9101",
		"localhost.attacker.example:9101",
		"192.0.2.10:9101",
		"",
	} {
		t.Run("host="+host, func(t *testing.T) {
			recorder := serveScrape(runtime, http.MethodGet, "/metrics", host)
			if recorder.Code != http.StatusForbidden {
				t.Fatalf("status = %d, want %d", recorder.Code, http.StatusForbidden)
			}
		})
	}
}
