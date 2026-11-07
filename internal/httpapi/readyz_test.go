package httpapi_test

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"payment-hub/internal/httpapi"
)

const readyzTestCanary = "FAKE_T021_READYZ_CANARY"

func readyzTestCall(t *testing.T, router *gin.Engine, method, path string, ctx context.Context) *httptest.ResponseRecorder {
	t.Helper()
	request := httptest.NewRequest(method, path, nil)
	if ctx != nil {
		request = request.WithContext(ctx)
	}
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, request)
	return recorder
}

func readyzTestAssertExactBody(t *testing.T, recorder *httptest.ResponseRecorder, want string) {
	t.Helper()
	if recorder.Body.String() != want {
		t.Fatal("readyz response body is not the exact fixed object")
	}
	if strings.Contains(recorder.Body.String(), readyzTestCanary) {
		t.Fatal("readyz response echoed a rejected checker value")
	}
}

func readyzTestAssertJSONNoStore(t *testing.T, recorder *httptest.ResponseRecorder) {
	t.Helper()
	if !strings.Contains(recorder.Header().Get("Content-Type"), "application/json") {
		t.Fatal("readyz response is not JSON")
	}
	if recorder.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("readyz response is not marked no-store")
	}
}

func TestReadyzSuccessExactResponseWithoutAuthentication(t *testing.T) {
	router := httpapi.NewRouter(func(context.Context) error { return nil })
	recorder := readyzTestCall(t, router, http.MethodGet, "/readyz", nil)
	if recorder.Code != http.StatusOK {
		t.Fatal("readyz success did not return 200")
	}
	readyzTestAssertExactBody(t, recorder, `{"status":"ready"}`)
	readyzTestAssertJSONNoStore(t, recorder)
	if recorder.Header().Get("Authorization") != "" {
		t.Fatal("readyz response unexpectedly contains authentication material")
	}
}

func TestReadyzFailureIsExact503WithoutRawValues(t *testing.T) {
	cases := []struct {
		name  string
		check func(context.Context) error
	}{
		{
			name:  "nil checker",
			check: nil,
		},
		{
			name: "checker error",
			check: func(context.Context) error {
				return errors.New(readyzTestCanary)
			},
		},
		{
			name: "checker panic",
			check: func(context.Context) error {
				panic(readyzTestCanary)
			},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			router := httpapi.NewRouter(tc.check)
			recorder := readyzTestCall(t, router, http.MethodGet, "/readyz", nil)
			if recorder.Code != http.StatusServiceUnavailable {
				t.Fatal("readyz failure did not return 503")
			}
			readyzTestAssertExactBody(t, recorder, `{"status":"not_ready"}`)
			readyzTestAssertJSONNoStore(t, recorder)
			if strings.Contains(recorder.Body.String(), "error") {
				t.Fatal("readyz failure exposed an error field")
			}
		})
	}
}

func TestReadyzCanceledOrExpiredCannotReturn200(t *testing.T) {
	t.Run("canceled request", func(t *testing.T) {
		router := httpapi.NewRouter(func(context.Context) error { return nil })
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		recorder := readyzTestCall(t, router, http.MethodGet, "/readyz", ctx)
		if recorder.Code != http.StatusServiceUnavailable {
			t.Fatal("readyz returned success for a canceled request")
		}
		readyzTestAssertExactBody(t, recorder, `{"status":"not_ready"}`)
	})

	t.Run("checker returns nil after context expiry", func(t *testing.T) {
		router := httpapi.NewRouter(func(ctx context.Context) error {
			<-ctx.Done()
			return nil
		})
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
		defer cancel()
		recorder := readyzTestCall(t, router, http.MethodGet, "/readyz", ctx)
		if recorder.Code != http.StatusServiceUnavailable {
			t.Fatal("readyz returned success after the checker context expired")
		}
		readyzTestAssertExactBody(t, recorder, `{"status":"not_ready"}`)
	})
}

func TestReadyzDeadlineIsOneSecondOrShorterParent(t *testing.T) {
	t.Run("one second cap", func(t *testing.T) {
		deadlines := make(chan time.Time, 1)
		router := httpapi.NewRouter(func(ctx context.Context) error {
			deadline, ok := ctx.Deadline()
			if !ok {
				t.Error("readyz checker context has no deadline")
				return nil
			}
			deadlines <- deadline
			return nil
		})
		recorder := readyzTestCall(t, router, http.MethodGet, "/readyz", nil)
		if recorder.Code != http.StatusOK {
			t.Fatal("readyz success did not return 200")
		}
		select {
		case deadline := <-deadlines:
			remaining := time.Until(deadline)
			if remaining <= 0 || remaining > time.Second {
				t.Fatal("readyz checker deadline is not the one-second cap")
			}
		case <-time.After(time.Second):
			t.Fatal("readyz checker did not receive a deadline")
		}
	})

	t.Run("shorter parent context", func(t *testing.T) {
		deadlines := make(chan time.Time, 1)
		router := httpapi.NewRouter(func(ctx context.Context) error {
			deadline, ok := ctx.Deadline()
			if !ok {
				t.Error("readyz checker context has no deadline")
				return nil
			}
			deadlines <- deadline
			return nil
		})
		parent, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
		defer cancel()
		recorder := readyzTestCall(t, router, http.MethodGet, "/readyz", parent)
		if recorder.Code != http.StatusOK {
			t.Fatal("readyz success did not return 200 with a live parent context")
		}
		select {
		case deadline := <-deadlines:
			parentDeadline, _ := parent.Deadline()
			if deadline.After(parentDeadline) {
				t.Fatal("readyz checker deadline exceeded the shorter parent deadline")
			}
		case <-time.After(time.Second):
			t.Fatal("readyz checker did not receive a deadline")
		}
	})
}

func TestNewRouterOnlyReadyzAndReleaseMode(t *testing.T) {
	previousMode := gin.Mode()
	defer gin.SetMode(previousMode)

	router := httpapi.NewRouter(nil)
	if gin.Mode() != gin.ReleaseMode {
		t.Fatal("NewRouter did not select Gin release mode")
	}
	routes := router.Routes()
	if len(routes) != 1 || routes[0].Method != http.MethodGet || routes[0].Path != "/readyz" {
		t.Fatal("NewRouter registered routes outside the single GET /readyz contract")
	}

	for _, path := range []string{"/orders", "/payments", "/healthz"} {
		recorder := readyzTestCall(t, router, http.MethodGet, path, nil)
		if recorder.Code != http.StatusNotFound {
			t.Fatal("NewRouter exposed a non-readyz business path")
		}
	}
	recorder := readyzTestCall(t, router, http.MethodPost, "/readyz", nil)
	if recorder.Code != http.StatusNotFound {
		t.Fatal("NewRouter accepted a non-GET readyz method")
	}
	recorder = readyzTestCall(t, router, http.MethodGet, "/readyz", nil)
	if recorder.Code != http.StatusServiceUnavailable {
		t.Fatal("nil readyz checker did not fail closed")
	}
	readyzTestAssertExactBody(t, recorder, `{"status":"not_ready"}`)
}
