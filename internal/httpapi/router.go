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
