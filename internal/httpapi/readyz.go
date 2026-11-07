package httpapi

import (
	"context"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
)

const readyzCheckTimeout = time.Second

var errReadyzPanic = errReadyzFailure("readiness check panicked")

type errReadyzFailure string

func (e errReadyzFailure) Error() string { return string(e) }

// NewReadyz returns the unauthenticated readiness handler. It gives the checker
// the smaller of the request deadline and one second, and fails closed on nil,
// error, cancellation, expiry, or panic.
func NewReadyz(check func(context.Context) error) gin.HandlerFunc {
	return func(c *gin.Context) {
		if c == nil {
			return
		}
		status := http.StatusServiceUnavailable
		body := `{"status":"not_ready"}`
		if c.Request != nil && c.Request.Context().Err() == nil {
			ctx, cancel := context.WithTimeout(c.Request.Context(), readyzCheckTimeout)
			err := callReadyzCheck(ctx, check)
			checkErr := ctx.Err()
			cancel()
			if err == nil && checkErr == nil {
				status = http.StatusOK
				body = `{"status":"ready"}`
			}
		}
		c.Header("Cache-Control", "no-store")
		c.Data(status, "application/json; charset=utf-8", []byte(body))
	}
}

func callReadyzCheck(ctx context.Context, check func(context.Context) error) (err error) {
	if check == nil || ctx.Err() != nil {
		return errReadyzFailure("readiness unavailable")
	}
	defer func() {
		if recover() != nil {
			err = errReadyzPanic
		}
	}()
	return check(ctx)
}
