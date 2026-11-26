package httpapi

import (
	"context"

	"github.com/gin-gonic/gin"
)

// Option configures a router before its routes are registered. It keeps the
// HTTP package independent of any specific metrics or middleware library.
type Option func(*gin.Engine)

// WithMiddleware appends global middleware. Callers pass a Gin handler, not a
// concrete metrics type, so route composition stays decoupled.
func WithMiddleware(middleware ...gin.HandlerFunc) Option {
	return func(router *gin.Engine) {
		for _, handler := range middleware {
			if handler != nil {
				router.Use(handler)
			}
		}
	}
}

// NewRouter returns the minimal HTTP surface for this phase: one
// unauthenticated GET /readyz route and optional global middleware.
func NewRouter(check func(context.Context) error, options ...Option) *gin.Engine {
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	for _, option := range options {
		if option != nil {
			option(router)
		}
	}
	router.GET("/readyz", NewReadyz(check))
	return router
}

// NewRouterWithPayments composes the independent unauthenticated GET /readyz
// route with exactly the three authenticated business routes. It is a thin
// composition helper: parsing, authentication, and business ports remain owned
// by RegisterPaymentRoutes. Registration is fail-closed and leaves no partial
// router behind.
func NewRouterWithPayments(check func(context.Context) error, cfg AuthConfig, submit PaymentSubmitter, query PaymentQuerier, options ...Option) (*gin.Engine, error) {
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	for _, option := range options {
		if option != nil {
			option(router)
		}
	}
	router.GET("/readyz", NewReadyz(check))
	if err := RegisterPaymentRoutes(router, cfg, submit, query); err != nil {
		return nil, err
	}
	return router, nil
}
