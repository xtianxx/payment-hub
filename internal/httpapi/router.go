package httpapi

import (
	"context"

	"github.com/gin-gonic/gin"
)

// NewRouter returns the minimal HTTP surface for this phase: one
// unauthenticated GET /readyz route and no default middleware.
func NewRouter(check func(context.Context) error) *gin.Engine {
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	router.GET("/readyz", NewReadyz(check))
	return router
}

// NewRouterWithPayments composes the independent unauthenticated GET /readyz
// route with exactly the three authenticated business routes. It is a thin
// composition helper: parsing, authentication, and business ports remain owned
// by RegisterPaymentRoutes. Registration is fail-closed and leaves no partial
// router behind.
func NewRouterWithPayments(check func(context.Context) error, cfg AuthConfig, submit PaymentSubmitter, query PaymentQuerier) (*gin.Engine, error) {
	gin.SetMode(gin.ReleaseMode)
	router := gin.New()
	router.GET("/readyz", NewReadyz(check))
	if err := RegisterPaymentRoutes(router, cfg, submit, query); err != nil {
		return nil, err
	}
	return router, nil
}
