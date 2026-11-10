package httpapi

import (
	"bytes"
	"encoding/json"
	"io"
	"mime"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/gin-gonic/gin"

	"payment-hub/internal/payment"
)

const (
	paymentBodyLimitBytes = 4096
	paymentBodyReadLimit  = paymentBodyLimitBytes + 1
)

type paymentHandlers struct {
	submit PaymentSubmitter
	query  PaymentQuerier
}

type paymentSubmitRequest struct {
	businessOrderNo string
	amount          int64
	currency        string
}

func (h paymentHandlers) submitPayment(c *gin.Context) {
	clientID, ok := ClientID(c)
	if !ok {
		authAbortUnauthorized(c)
		return
	}
	input, status := parsePaymentSubmitRequest(c)
	if status != 0 {
		paymentWriteError(c, status, paymentErrorCodeInvalidRequest)
		return
	}
	requestID := RequestID(c)
	result, err := h.submit.Submit(c.Request.Context(), requestID, payment.RegisterOrderInput{
		ClientID:        clientID,
		BusinessOrderNo: input.businessOrderNo,
		Amount:          input.amount,
		Currency:        input.currency,
	})
	if err != nil {
		paymentWriteBusinessError(c, err)
		return
	}
	paymentWriteSubmitResult(c, requestID, clientID, result)
}

func (h paymentHandlers) queryByPaymentID(c *gin.Context) {
	clientID, ok := ClientID(c)
	if !ok {
		authAbortUnauthorized(c)
		return
	}
	paymentID, ok := parsePaymentIDRequest(c)
	if !ok {
		paymentWriteError(c, http.StatusBadRequest, paymentErrorCodeInvalidRequest)
		return
	}
	requestID := RequestID(c)
	result, err := h.query.Query(c.Request.Context(), requestID, payment.QueryInput{
		ClientID:       clientID,
		PaymentOrderID: payment.PaymentOrderID(paymentID),
	})
	if err != nil {
		paymentWriteBusinessError(c, err)
		return
	}
	paymentWriteQueryResult(c, requestID, clientID, result)
}

func (h paymentHandlers) queryByBusinessOrderNo(c *gin.Context) {
	clientID, ok := ClientID(c)
	if !ok {
		authAbortUnauthorized(c)
		return
	}
	businessOrderNo, ok := parseBusinessOrderNoRequest(c)
	if !ok {
		paymentWriteError(c, http.StatusBadRequest, paymentErrorCodeInvalidRequest)
		return
	}
	requestID := RequestID(c)
	result, err := h.query.Query(c.Request.Context(), requestID, payment.QueryInput{
		ClientID:        clientID,
		BusinessOrderNo: businessOrderNo,
	})
	if err != nil {
		paymentWriteBusinessError(c, err)
		return
	}
	paymentWriteQueryResult(c, requestID, clientID, result)
}

func parsePaymentSubmitRequest(c *gin.Context) (paymentSubmitRequest, int) {
	var zero paymentSubmitRequest
	if c == nil || c.Request == nil {
		return zero, http.StatusBadRequest
	}
	if !paymentRequestHasJSONMediaType(c.Request) {
		return zero, http.StatusUnsupportedMediaType
	}
	if !paymentRequestHasIdentityEncoding(c.Request) {
		return zero, http.StatusUnsupportedMediaType
	}
	body, status := readPaymentBody(c.Request)
	if status != 0 {
		return zero, status
	}
	if !paymentRequestQueryEmpty(c.Request) {
		return zero, http.StatusBadRequest
	}
	if !utf8.Valid(body) {
		return zero, http.StatusBadRequest
	}
	return parsePaymentSubmitJSON(body)
}

func parsePaymentIDRequest(c *gin.Context) (string, bool) {
	if c == nil || c.Request == nil {
		return "", false
	}
	if c.Request.URL.RawQuery != "" {
		return "", false
	}
	paymentID := c.Param("payment_id")
	if payment.ValidateUUIDv4(paymentID) != nil {
		return "", false
	}
	empty, ok := paymentRequestBodyEmpty(c.Request)
	if !ok || !empty {
		return "", false
	}
	return strings.ToLower(paymentID), true
}

func parseBusinessOrderNoRequest(c *gin.Context) (string, bool) {
	if c == nil || c.Request == nil {
		return "", false
	}
	values, err := url.ParseQuery(c.Request.URL.RawQuery)
	if err != nil || len(values) != 1 {
		return "", false
	}
	raw, ok := values["business_order_no"]
	if !ok || len(raw) != 1 {
		return "", false
	}
	businessOrderNo := raw[0]
	if payment.ValidateBusinessOrderNo(businessOrderNo) != nil {
		return "", false
	}
	empty, ok := paymentRequestBodyEmpty(c.Request)
	if !ok || !empty {
		return "", false
	}
	return businessOrderNo, true
}

func paymentRequestQueryEmpty(req *http.Request) bool {
	if req == nil || req.URL == nil {
		return false
	}
	values, err := url.ParseQuery(req.URL.RawQuery)
	if err != nil {
		return false
	}
	return len(values) == 0
}

func paymentRequestBodyEmpty(req *http.Request) (bool, bool) {
	if req == nil {
		return true, false
	}
	if req.Body == nil || req.Body == http.NoBody {
		return true, true
	}
	if req.ContentLength > 0 {
		return false, true
	}
	var one [1]byte
	n, err := io.ReadFull(req.Body, one[:])
	if n > 0 {
		return false, true
	}
	if err == io.EOF {
		return true, true
	}
	if err != nil {
		return false, false
	}
	return true, true
}

func readPaymentBody(req *http.Request) ([]byte, int) {
	if req == nil || req.Body == nil {
		return nil, http.StatusBadRequest
	}
	body, err := io.ReadAll(io.LimitReader(req.Body, paymentBodyReadLimit))
	if err != nil {
		return nil, http.StatusBadRequest
	}
	if len(body) > paymentBodyLimitBytes {
		return nil, http.StatusRequestEntityTooLarge
	}
	return body, 0
}

func paymentRequestHasJSONMediaType(req *http.Request) bool {
	if req == nil {
		return false
	}
	value, ok := paymentSingleHeaderValue(req.Header, "Content-Type")
	if !ok {
		return false
	}
	mediaType, params, err := mime.ParseMediaType(value)
	if err != nil || mediaType != "application/json" {
		return false
	}
	names, ok := paymentMediaTypeParameterNames(value)
	if !ok {
		return false
	}
	if len(names) == 0 {
		return len(params) == 0
	}
	if len(names) != 1 || names[0] != "charset" || len(params) != 1 {
		return false
	}
	charset, ok := params["charset"]
	return ok && strings.EqualFold(charset, "utf-8")
}

func paymentRequestHasIdentityEncoding(req *http.Request) bool {
	if req == nil {
		return false
	}
	count := 0
	var values []string
	for name, headerValues := range req.Header {
		if strings.EqualFold(name, "Content-Encoding") {
			count++
			values = headerValues
		}
	}
	if count == 0 {
		return true
	}
	if count != 1 || len(values) != 1 {
		return false
	}
	return strings.EqualFold(strings.TrimSpace(values[0]), "identity")
}

func paymentSingleHeaderValue(header http.Header, name string) (string, bool) {
	count := 0
	var values []string
	for key, headerValues := range header {
		if strings.EqualFold(key, name) {
			count++
			values = headerValues
		}
	}
	if count != 1 || len(values) != 1 {
		return "", false
	}
	return values[0], true
}

func paymentMediaTypeParameterNames(value string) ([]string, bool) {
	parts := make([]string, 0, 2)
	start := 0
	inQuote := false
	escaped := false
	for i := 0; i < len(value); i++ {
		switch {
		case escaped:
			escaped = false
		case inQuote:
			switch value[i] {
			case '\\':
				escaped = true
			case '"':
				inQuote = false
			}
		case value[i] == '"':
			inQuote = true
		case value[i] == ';':
			parts = append(parts, value[start:i])
			start = i + 1
		}
	}
	if inQuote {
		return nil, false
	}
	parts = append(parts, value[start:])
	names := make([]string, 0, len(parts)-1)
	for _, part := range parts[1:] {
		part = strings.TrimSpace(part)
		if part == "" {
			return nil, false
		}
		equals := strings.IndexByte(part, '=')
		if equals <= 0 {
			return nil, false
		}
		name := strings.TrimSpace(part[:equals])
		if name == "" {
			return nil, false
		}
		names = append(names, strings.ToLower(name))
	}
	return names, true
}

func parsePaymentSubmitJSON(body []byte) (paymentSubmitRequest, int) {
	var zero paymentSubmitRequest
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	token, err := decoder.Token()
	if err != nil {
		return zero, http.StatusBadRequest
	}
	opening, ok := token.(json.Delim)
	if !ok || opening != '{' {
		return zero, http.StatusBadRequest
	}
	seen := make(map[string]bool, 3)
	var input paymentSubmitRequest
	for decoder.More() {
		keyToken, err := decoder.Token()
		if err != nil {
			return zero, http.StatusBadRequest
		}
		key, ok := keyToken.(string)
		if !ok || seen[key] {
			return zero, http.StatusBadRequest
		}
		seen[key] = true
		valueToken, err := decoder.Token()
		if err != nil {
			return zero, http.StatusBadRequest
		}
		switch key {
		case "business_order_no":
			value, ok := valueToken.(string)
			if !ok {
				return zero, http.StatusBadRequest
			}
			input.businessOrderNo = value
		case "amount":
			number, ok := valueToken.(json.Number)
			if !ok {
				return zero, http.StatusBadRequest
			}
			amount, ok := parsePaymentAmountLiteral(string(number))
			if !ok {
				return zero, http.StatusBadRequest
			}
			input.amount = amount
		case "currency":
			value, ok := valueToken.(string)
			if !ok {
				return zero, http.StatusBadRequest
			}
			input.currency = value
		default:
			return zero, http.StatusBadRequest
		}
	}
	closing, err := decoder.Token()
	if err != nil {
		return zero, http.StatusBadRequest
	}
	closingDelim, ok := closing.(json.Delim)
	if !ok || closingDelim != '}' {
		return zero, http.StatusBadRequest
	}
	if len(seen) != 3 || !seen["business_order_no"] || !seen["amount"] || !seen["currency"] {
		return zero, http.StatusBadRequest
	}
	if _, err := decoder.Token(); err != io.EOF {
		return zero, http.StatusBadRequest
	}
	if payment.ValidateBusinessOrderNo(input.businessOrderNo) != nil {
		return zero, http.StatusBadRequest
	}
	// Amount range and currency value are payment business decisions. The HTTP
	// layer has already enforced the strict JSON syntax and integer
	// representation; valid business values follow the normal registration
	// path, while invalid ones are delegated to the limited conflict-priority
	// read in payment.
	return input, 0
}

func parsePaymentAmountLiteral(literal string) (int64, bool) {
	if literal == "" || strings.ContainsAny(literal, ".eE+") {
		return 0, false
	}
	amount, err := strconv.ParseInt(literal, 10, 64)
	if err != nil {
		return 0, false
	}
	if strconv.FormatInt(amount, 10) != literal {
		return 0, false
	}
	return amount, true
}
