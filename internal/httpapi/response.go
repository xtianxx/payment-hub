package httpapi

import (
	"context"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"payment-hub/internal/payment"
)

const (
	paymentErrorCodeInvalidRequest  = "INVALID_REQUEST"
	paymentErrorCodeUnauthorized    = "UNAUTHORIZED"
	paymentErrorCodePaymentNotFound = "PAYMENT_NOT_FOUND"
	paymentErrorCodePaymentConflict = "PAYMENT_CONFLICT"
	paymentErrorCodeProcessingError = "PROCESSING_ERROR"
)

var paymentErrorMessages = map[string]string{
	paymentErrorCodeInvalidRequest:  "invalid request",
	paymentErrorCodeUnauthorized:    "authentication required",
	paymentErrorCodePaymentNotFound: "payment not found",
	paymentErrorCodePaymentConflict: "payment conflict",
	paymentErrorCodeProcessingError: "processing error",
}

var paymentIssueMessages = map[string]string{
	payment.IssueCodeMockResultUnconfirmed: "mock result unconfirmed",
	payment.IssueCodeMockResultNotFound:    "mock result not found",
	payment.IssueCodeMockEvidenceRejected:  "mock evidence rejected",
	payment.IssueCodeResultSaveError:       "result save error",
	payment.IssueCodeTraceSaveError:        "trace save error",
}

const (
	paymentTraceSaveFailedMessage        = "本次追踪保存失败"
	paymentTraceSaveIndeterminateMessage = "保存状态未确认"
)

var paymentTraceSaveIssueMessages = map[string]string{
	paymentTraceSaveFailedMessage:        "trace save failed",
	paymentTraceSaveIndeterminateMessage: "trace save state unconfirmed",
}

type paymentEnvelope struct {
	RequestID string                 `json:"request_id"`
	Payment   *paymentOrderResponse  `json:"payment,omitempty"`
	Issues    []paymentIssueResponse `json:"issues,omitempty"`
	Error     *paymentErrorResponse  `json:"error,omitempty"`
}

type paymentOrderResponse struct {
	PaymentID       string  `json:"payment_id"`
	BusinessOrderNo string  `json:"business_order_no"`
	Amount          int64   `json:"amount"`
	Currency        string  `json:"currency"`
	Result          string  `json:"result"`
	RegisteredAt    string  `json:"registered_at"`
	ConfirmedAt     *string `json:"confirmed_at"`
}

type paymentIssueResponse struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

type paymentErrorResponse struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func paymentWriteSubmitResult(c *gin.Context, requestID payment.RequestID, clientID string, result payment.SubmitResult) {
	status := http.StatusOK
	if result.Created {
		status = http.StatusCreated
	}
	paymentWritePayment(c, status, requestID, clientID, result.Order, result.Issues)
}

func paymentWriteQueryResult(c *gin.Context, requestID payment.RequestID, clientID string, result payment.QueryResult) {
	paymentWritePayment(c, http.StatusOK, requestID, clientID, result.Order, result.Issues)
}

func paymentWritePayment(c *gin.Context, status int, requestID payment.RequestID, clientID string, order payment.PaymentOrder, issues []payment.Issue) {
	responseOrder, ok := paymentOrderResponseFor(order, clientID)
	if !ok {
		paymentWriteError(c, http.StatusInternalServerError, paymentErrorCodeProcessingError)
		return
	}
	envelope := paymentEnvelope{
		RequestID: string(requestID),
		Payment:   &responseOrder,
		Issues:    paymentIssueResponses(issues),
	}
	location := ""
	if status == http.StatusCreated {
		location = "/api/v1/payments/" + responseOrder.PaymentID
	}
	paymentWriteJSON(c, status, envelope, location)
}

func paymentWriteBusinessError(c *gin.Context, err error) {
	status, code := paymentStatusForError(err)
	paymentWriteError(c, status, code)
}

func paymentWriteError(c *gin.Context, status int, code string) {
	envelope := paymentEnvelope{
		RequestID: string(RequestID(c)),
		Error: &paymentErrorResponse{
			Code:    code,
			Message: paymentErrorMessage(code),
		},
	}
	paymentWriteJSON(c, status, envelope, "")
}

func paymentWriteJSON(c *gin.Context, status int, payload any, location string) {
	header := c.Writer.Header()
	header.Del("ETag")
	header.Del("Last-Modified")
	header.Set("Content-Type", "application/json")
	header.Set("Cache-Control", "no-store")
	if location != "" {
		header.Set("Location", location)
	} else {
		header.Del("Location")
	}
	c.JSON(status, payload)
}

func paymentStatusForError(err error) (int, string) {
	switch {
	case errors.Is(err, payment.ErrInvalidInput):
		return http.StatusBadRequest, paymentErrorCodeInvalidRequest
	case errors.Is(err, payment.ErrConflict):
		return http.StatusConflict, paymentErrorCodePaymentConflict
	case errors.Is(err, payment.ErrNotFound):
		return http.StatusNotFound, paymentErrorCodePaymentNotFound
	case errors.Is(err, payment.ErrUnavailable):
		return http.StatusServiceUnavailable, paymentErrorCodeProcessingError
	case errors.Is(err, context.DeadlineExceeded), errors.Is(err, context.Canceled):
		return http.StatusServiceUnavailable, paymentErrorCodeProcessingError
	default:
		return http.StatusInternalServerError, paymentErrorCodeProcessingError
	}
}

func paymentOrderResponseFor(order payment.PaymentOrder, clientID string) (paymentOrderResponse, bool) {
	if order.ClientID != clientID {
		return paymentOrderResponse{}, false
	}
	if payment.ValidateUUIDv4(string(order.ID)) != nil {
		return paymentOrderResponse{}, false
	}
	if payment.ValidateBusinessOrderNo(order.BusinessOrderNo) != nil {
		return paymentOrderResponse{}, false
	}
	if payment.ValidateAmount(order.Amount) != nil {
		return paymentOrderResponse{}, false
	}
	if payment.ValidateCurrency(order.Currency) != nil {
		return paymentOrderResponse{}, false
	}
	if payment.ValidateResultTime(order.Result, order.ConfirmedAt) != nil {
		return paymentOrderResponse{}, false
	}
	if payment.ValidateUTC(order.RegisteredAt) != nil {
		return paymentOrderResponse{}, false
	}

	var confirmedAt *string
	if order.ConfirmedAt != nil {
		value := order.ConfirmedAt.UTC().Format(time.RFC3339Nano)
		confirmedAt = &value
	}
	return paymentOrderResponse{
		PaymentID:       strings.ToLower(string(order.ID)),
		BusinessOrderNo: order.BusinessOrderNo,
		Amount:          order.Amount,
		Currency:        order.Currency,
		Result:          string(order.Result),
		RegisteredAt:    order.RegisteredAt.UTC().Format(time.RFC3339Nano),
		ConfirmedAt:     confirmedAt,
	}, true
}

func paymentIssueResponses(issues []payment.Issue) []paymentIssueResponse {
	if len(issues) == 0 {
		return nil
	}
	responses := make([]paymentIssueResponse, 0, len(issues))
	seen := make(map[string]bool, len(issues))
	for _, issue := range issues {
		message, ok := paymentIssueMessage(issue)
		if !ok || seen[issue.Code] {
			continue
		}
		seen[issue.Code] = true
		responses = append(responses, paymentIssueResponse{Code: issue.Code, Message: message})
	}
	if len(responses) == 0 {
		return nil
	}
	return responses
}

// paymentIssueMessage maps only the two controlled trace-save variants to
// distinct external messages. Every other Issue.Message is ignored so raw
// error text can never pass through the HTTP boundary.
func paymentIssueMessage(issue payment.Issue) (string, bool) {
	if issue.Code == payment.IssueCodeTraceSaveError {
		if message, ok := paymentTraceSaveIssueMessages[issue.Message]; ok {
			return message, true
		}
	}
	message, ok := paymentIssueMessages[issue.Code]
	return message, ok
}

func paymentErrorMessage(code string) string {
	if message, ok := paymentErrorMessages[code]; ok {
		return message
	}
	return paymentErrorMessages[paymentErrorCodeProcessingError]
}
