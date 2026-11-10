package httpapi

import (
	"context"
	"errors"

	"github.com/gin-gonic/gin"

	"payment-hub/internal/payment"
)

// PaymentSubmitter is the narrow business port used by POST /api/v1/payments.
// The HTTP layer must not depend on a concrete submission implementation.
type PaymentSubmitter interface {
	Submit(context.Context, payment.RequestID, payment.RegisterOrderInput) (payment.SubmitResult, error)
}

// PaymentQuerier is the narrow business port used by both GET payment
// endpoints. The HTTP layer must not depend on a concrete query implementation.
type PaymentQuerier interface {
	Query(context.Context, payment.RequestID, payment.QueryInput) (payment.QueryResult, error)
}

var (
	errPaymentRoutesNilRouter    = errors.New("httpapi: nil router")
	errPaymentRoutesNilSubmitter = errors.New("httpapi: nil payment submitter")
	errPaymentRoutesNilQuerier   = errors.New("httpapi: nil payment querier")
)

// RegisterPaymentRoutes mounts exactly the three contract business endpoints.
// It fails closed on a nil router, invalid authentication configuration, or a
// nil business dependency. Request parsing and response projection live in
// request.go and response.go; the HTTP layer calls only the supplied business
// interfaces and never accesses storage or the Mock channel directly.
func RegisterPaymentRoutes(router *gin.Engine, cfg AuthConfig, submit PaymentSubmitter, query PaymentQuerier) error {
	if router == nil {
		return errPaymentRoutesNilRouter
	}
	if submit == nil {
		return errPaymentRoutesNilSubmitter
	}
	if query == nil {
		return errPaymentRoutesNilQuerier
	}
	middleware, err := NewAuthMiddleware(cfg)
	if err != nil {
		return err
	}

	handlers := paymentHandlers{submit: submit, query: query}
	group := router.Group("/api/v1")
	group.Use(middleware)
	group.POST("/payments", handlers.submitPayment)
	group.GET("/payments/:payment_id", handlers.queryByPaymentID)
	group.GET("/payments", handlers.queryByBusinessOrderNo)
	return nil
}
