package metrics

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestRuntimeHandlerOnlyServesMetricsPath(t *testing.T) {
	runtime := NewRuntime()

	tests := []struct {
		method string
		path   string
		want   int
	}{
		{method: http.MethodGet, path: "/metrics", want: http.StatusOK},
		{method: http.MethodGet, path: "/", want: http.StatusNotFound},
		{method: http.MethodGet, path: "/metrics/", want: http.StatusNotFound},
		{method: http.MethodGet, path: "/readyz", want: http.StatusNotFound},
		{method: http.MethodPost, path: "/metrics", want: http.StatusMethodNotAllowed},
	}
	for _, test := range tests {
		t.Run(test.method+" "+test.path, func(t *testing.T) {
			recorder := httptest.NewRecorder()
			runtime.Handler.ServeHTTP(recorder, httptest.NewRequest(test.method, test.path, nil))
			if recorder.Code != test.want {
				t.Fatalf("status = %d, want %d", recorder.Code, test.want)
			}
		})
	}
}
