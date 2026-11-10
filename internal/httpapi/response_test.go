package httpapi_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"payment-hub/internal/payment"
)

var contractPaymentFieldNames = []string{
	"payment_id",
	"business_order_no",
	"amount",
	"currency",
	"result",
	"registered_at",
	"confirmed_at",
}

var contractIssueCodes = map[string]bool{
	payment.IssueCodeMockResultUnconfirmed: true,
	payment.IssueCodeMockResultNotFound:    true,
	payment.IssueCodeMockEvidenceRejected:  true,
	payment.IssueCodeResultSaveError:       true,
	payment.IssueCodeTraceSaveError:        true,
}

func contractIntegerLiteral(t *testing.T, raw json.RawMessage) int64 {
	t.Helper()
	literal := string(bytes.TrimSpace(raw))
	if literal == "" || strings.ContainsAny(literal, ".eE+") {
		contractFail(t, "amount", "amount is not a plain integer literal")
	}
	value, err := strconv.ParseInt(literal, 10, 64)
	if err != nil {
		contractFail(t, "amount", "amount is not a safe int64 integer literal")
	}
	if strconv.FormatInt(value, 10) != literal {
		contractFail(t, "amount", "amount is not a canonical integer literal")
	}
	return value
}

func contractAssertUTCTime(t *testing.T, category string, raw json.RawMessage, want time.Time) {
	t.Helper()
	var value string
	if err := json.Unmarshal(raw, &value); err != nil {
		contractFail(t, category, "time is not a string")
	}
	if !strings.HasSuffix(value, "Z") {
		contractFail(t, category, "time is not UTC with a Z suffix")
	}
	parsed, err := time.Parse(time.RFC3339Nano, value)
	if err != nil {
		contractFail(t, category, "time is not RFC3339")
	}
	if _, offset := parsed.Zone(); offset != 0 {
		contractFail(t, category, "time is not UTC")
	}
	if !parsed.Truncate(time.Second).Equal(want.Truncate(time.Second)) {
		contractFail(t, category, "time does not match the order fact")
	}
}

func contractAssertPayment(t *testing.T, raw json.RawMessage, expected payment.PaymentOrder) string {
	t.Helper()
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		contractFail(t, "payment_object", "payment is not a JSON object")
	}
	contractAssertExactKeys(t, "payment_fields", fields, contractPaymentFieldNames...)
	for _, name := range contractPaymentFieldNames {
		if name == "confirmed_at" {
			continue
		}
		if string(bytes.TrimSpace(fields[name])) == "null" {
			contractFail(t, "payment_null", "only confirmed_at may be null")
		}
	}

	var paymentID string
	if err := json.Unmarshal(fields["payment_id"], &paymentID); err != nil {
		contractFail(t, "payment_id", "payment_id is not a string")
	}
	if !contractLowerUUIDv4.MatchString(paymentID) {
		contractFail(t, "payment_id", "payment_id is not a canonical lowercase UUIDv4")
	}
	if paymentID != string(expected.ID) {
		contractFail(t, "payment_id", "payment_id does not match the order")
	}

	var businessOrderNo string
	if err := json.Unmarshal(fields["business_order_no"], &businessOrderNo); err != nil {
		contractFail(t, "business_order_no", "business_order_no is not a string")
	}
	if businessOrderNo != expected.BusinessOrderNo {
		contractFail(t, "business_order_no", "business_order_no was not preserved")
	}
	if err := payment.ValidateBusinessOrderNo(businessOrderNo); err != nil {
		contractFail(t, "business_order_no", "business_order_no is not valid")
	}

	amount := contractIntegerLiteral(t, fields["amount"])
	if amount != expected.Amount {
		contractFail(t, "amount", "amount does not match the order")
	}
	if err := payment.ValidateAmount(amount); err != nil {
		contractFail(t, "amount", "amount is outside the contract range")
	}

	var currency string
	if err := json.Unmarshal(fields["currency"], &currency); err != nil {
		contractFail(t, "currency", "currency is not a string")
	}
	if currency != expected.Currency {
		contractFail(t, "currency", "currency does not match the order")
	}

	var result string
	if err := json.Unmarshal(fields["result"], &result); err != nil {
		contractFail(t, "result", "result is not a string")
	}
	if result != string(expected.Result) {
		contractFail(t, "result", "result does not match the order")
	}
	switch payment.Result(result) {
	case payment.ResultUnknown, payment.ResultSucceeded, payment.ResultFailed:
	default:
		contractFail(t, "result", "result is not one of the contract values")
	}

	contractAssertUTCTime(t, "registered_at", fields["registered_at"], expected.RegisteredAt)
	if expected.Result == payment.ResultUnknown {
		if string(bytes.TrimSpace(fields["confirmed_at"])) != "null" {
			contractFail(t, "confirmed_at", "confirmed_at must be null for UNKNOWN")
		}
	} else {
		if string(bytes.TrimSpace(fields["confirmed_at"])) == "null" {
			contractFail(t, "confirmed_at", "confirmed_at must be set for a confirmed result")
		}
		if expected.ConfirmedAt == nil {
			contractFail(t, "fixture_confirmed_at", "fixture confirmed time is missing")
		}
		contractAssertUTCTime(t, "confirmed_at", fields["confirmed_at"], *expected.ConfirmedAt)
	}
	return paymentID
}

func contractAssertIssues(t *testing.T, raw json.RawMessage, want []payment.Issue) {
	t.Helper()
	var issues []map[string]json.RawMessage
	if err := json.Unmarshal(raw, &issues); err != nil {
		contractFail(t, "issues", "issues is not an array of objects")
	}
	if len(issues) == 0 {
		contractFail(t, "issues", "issues must be omitted rather than empty")
	}
	if len(issues) != len(want) {
		contractFail(t, "issues", "issues count mismatch")
	}
	seen := make(map[string]bool)
	for _, issue := range issues {
		contractAssertExactKeys(t, "issue_fields", issue, "code", "message")
		var code, message string
		if err := json.Unmarshal(issue["code"], &code); err != nil {
			contractFail(t, "issues", "issue code is not a string")
		}
		if err := json.Unmarshal(issue["message"], &message); err != nil {
			contractFail(t, "issues", "issue message is not a string")
		}
		if !contractIssueCodes[code] {
			contractFail(t, "issues_whitelist", "issue code is not in the contract whitelist")
		}
		if message == "" {
			contractFail(t, "issues", "issue message is empty")
		}
		seen[code] = true
	}
	for _, expected := range want {
		if !seen[expected.Code] {
			contractFail(t, "issues", "expected issue code is missing")
		}
	}
}

func contractAssertPaymentEnvelope(t *testing.T, recorder *httptest.ResponseRecorder, wantStatus int, expected payment.PaymentOrder, wantIssues []payment.Issue, expectLocation bool) string {
	t.Helper()
	contractAssertStatus(t, recorder, wantStatus)
	contractAssertJSONNoStore(t, recorder)
	top := contractDecodeObject(t, recorder)
	wantKeys := []string{"request_id", "payment"}
	if len(wantIssues) > 0 {
		wantKeys = append(wantKeys, "issues")
	}
	contractAssertExactKeys(t, "envelope_keys", top, wantKeys...)
	if _, ok := top["error"]; ok {
		contractFail(t, "envelope_exclusive", "payment response must not contain error")
	}
	contractAssertRequestID(t, top["request_id"])
	paymentID := contractAssertPayment(t, top["payment"], expected)
	if len(wantIssues) > 0 {
		contractAssertIssues(t, top["issues"], wantIssues)
	}
	if expectLocation {
		values := recorder.Header().Values("Location")
		wantLocation := "/api/v1/payments/" + paymentID
		if len(values) != 1 || values[0] != wantLocation {
			contractFail(t, "header_location", "Location does not identify the returned payment")
		}
	}
	contractAssertNoCanary(t, recorder)
	return paymentID
}

func contractRunPOSTResponse(
	t *testing.T,
	result payment.SubmitResult,
	err error,
	wantStatus int,
	wantCode string,
	expected payment.PaymentOrder,
	issues []payment.Issue,
	expectLocation bool,
	wantSubmit int,
) *httptest.ResponseRecorder {
	t.Helper()
	submit := &contractSubmitFake{t: t, result: result, err: err}
	query := &contractQueryFake{t: t, result: contractQueryFound(payment.ResultSucceeded)}
	router := contractNewRouter(t, submit, query)
	req, _ := contractNewRequest(http.MethodPost, contractPostPath, contractValidBody())
	recorder := contractDo(router, req)
	if wantCode != "" {
		contractAssertErrorEnvelope(t, recorder, wantStatus, wantCode)
	} else {
		contractAssertPaymentEnvelope(t, recorder, wantStatus, expected, issues, expectLocation)
	}
	contractAssertCalls(t, submit, query, wantSubmit, 0)
	return recorder
}

func contractRunGETResponse(
	t *testing.T,
	target string,
	result payment.QueryResult,
	err error,
	wantStatus int,
	wantCode string,
	expected payment.PaymentOrder,
	issues []payment.Issue,
	prepare func(*http.Request),
) *httptest.ResponseRecorder {
	t.Helper()
	submit := &contractSubmitFake{t: t, result: contractSubmitCreated()}
	query := &contractQueryFake{t: t, result: result, err: err}
	router := contractNewRouter(t, submit, query)
	req, _ := contractNewRequest(http.MethodGet, target, nil)
	if prepare != nil {
		prepare(req)
	}
	recorder := contractDo(router, req)
	if wantCode != "" {
		contractAssertErrorEnvelope(t, recorder, wantStatus, wantCode)
	} else {
		contractAssertPaymentEnvelope(t, recorder, wantStatus, expected, issues, false)
	}
	contractAssertCalls(t, submit, query, 0, 1)
	return recorder
}

func TestPaymentResponseContract(t *testing.T) {
	t.Run("POST created UNKNOWN 201", func(t *testing.T) {
		expected := contractOrder(payment.ResultUnknown)
		contractRunPOSTResponse(t, payment.SubmitResult{Order: expected, Created: true}, nil, http.StatusCreated, "", expected, nil, true, 1)
	})

	t.Run("POST reused SUCCEEDED 200", func(t *testing.T) {
		expected := contractOrder(payment.ResultSucceeded)
		contractRunPOSTResponse(t, payment.SubmitResult{Order: expected, Created: false}, nil, http.StatusOK, "", expected, nil, false, 1)
	})

	t.Run("POST FAILED is a normal fact", func(t *testing.T) {
		expected := contractOrder(payment.ResultFailed)
		contractRunPOSTResponse(t, payment.SubmitResult{Order: expected, Created: true}, nil, http.StatusCreated, "", expected, nil, true, 1)
	})

	t.Run("POST UNKNOWN is not 202", func(t *testing.T) {
		t.Run("created", func(t *testing.T) {
			expected := contractOrder(payment.ResultUnknown)
			contractRunPOSTResponse(t, payment.SubmitResult{Order: expected, Created: true}, nil, http.StatusCreated, "", expected, nil, true, 1)
		})
		t.Run("reused", func(t *testing.T) {
			expected := contractOrder(payment.ResultUnknown)
			contractRunPOSTResponse(t, payment.SubmitResult{Order: expected, Created: false}, nil, http.StatusOK, "", expected, nil, false, 1)
		})
	})

	t.Run("POST issues keep the payment", func(t *testing.T) {
		expected := contractOrder(payment.ResultUnknown)
		issues := []payment.Issue{
			{Code: payment.IssueCodeMockResultUnconfirmed, Message: "mock result unconfirmed"},
			{Code: payment.IssueCodeTraceSaveError, Message: "trace save failed"},
		}
		contractRunPOSTResponse(t, payment.SubmitResult{Order: expected, Created: true, Issues: issues}, nil, http.StatusCreated, "", expected, issues, true, 1)
	})

	t.Run("POST empty issues are omitted", func(t *testing.T) {
		expected := contractOrder(payment.ResultSucceeded)
		contractRunPOSTResponse(t, payment.SubmitResult{Order: expected, Created: true}, nil, http.StatusCreated, "", expected, nil, true, 1)
		contractRunPOSTResponse(t, payment.SubmitResult{Order: expected, Created: true, Issues: []payment.Issue{}}, nil, http.StatusCreated, "", expected, nil, true, 1)
	})

	t.Run("POST unknown issue code is dropped", func(t *testing.T) {
		expected := contractOrder(payment.ResultSucceeded)
		validIssue := payment.Issue{Code: payment.IssueCodeTraceSaveError, Message: "trace save failed"}
		issues := []payment.Issue{
			validIssue,
			{Code: "FAKE_UNKNOWN_ISSUE", Message: contractCanary},
		}
		contractRunPOSTResponse(t, payment.SubmitResult{Order: expected, Created: true, Issues: issues}, nil, http.StatusCreated, "", expected, []payment.Issue{validIssue}, true, 1)
	})

	t.Run("POST non-nil error drops attached order", func(t *testing.T) {
		attached := contractOrder(payment.ResultSucceeded)
		contractRunPOSTResponse(t, payment.SubmitResult{Order: attached, Created: true}, payment.ErrConflict, http.StatusConflict, "PAYMENT_CONFLICT", payment.PaymentOrder{}, nil, false, 1)
	})

	t.Run("POST error mappings", func(t *testing.T) {
		attached := contractOrder(payment.ResultSucceeded)
		cases := []struct {
			name       string
			err        error
			wantStatus int
			wantCode   string
		}{
			{"invalid_input", payment.ErrInvalidInput, http.StatusBadRequest, "INVALID_REQUEST"},
			{"conflict", payment.ErrConflict, http.StatusConflict, "PAYMENT_CONFLICT"},
			{"unavailable", payment.ErrUnavailable, http.StatusServiceUnavailable, "PROCESSING_ERROR"},
			{"wrapped_unavailable", fmt.Errorf("FAKE_LEAK_CANARY_HTTP_CONTRACT: %w", payment.ErrUnavailable), http.StatusServiceUnavailable, "PROCESSING_ERROR"},
			{"unexpected", errors.New("FAKE_LEAK_CANARY_HTTP_CONTRACT: raw internal failure"), http.StatusInternalServerError, "PROCESSING_ERROR"},
			{"timeout", fmt.Errorf("FAKE_LEAK_CANARY_HTTP_CONTRACT: %w", context.DeadlineExceeded), http.StatusServiceUnavailable, "PROCESSING_ERROR"},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				contractRunPOSTResponse(t, payment.SubmitResult{Order: attached, Created: true}, tc.err, tc.wantStatus, tc.wantCode, payment.PaymentOrder{}, nil, false, 1)
			})
		}
	})

	t.Run("POST 413 uses INVALID_REQUEST", func(t *testing.T) {
		submit := &contractSubmitFake{t: t, result: contractSubmitCreated()}
		query := &contractQueryFake{t: t, result: contractQueryFound(payment.ResultSucceeded)}
		router := contractNewRouter(t, submit, query)
		req, _ := contractNewRequest(http.MethodPost, contractPostPath, contractPaddedBody(t, 4097))
		recorder := contractDo(router, req)
		contractAssertErrorEnvelope(t, recorder, http.StatusRequestEntityTooLarge, "INVALID_REQUEST")
		contractAssertCalls(t, submit, query, 0, 0)
	})

	t.Run("POST 415 uses INVALID_REQUEST", func(t *testing.T) {
		submit := &contractSubmitFake{t: t, result: contractSubmitCreated()}
		query := &contractQueryFake{t: t, result: contractQueryFound(payment.ResultSucceeded)}
		router := contractNewRouter(t, submit, query)
		req, _ := contractNewRequest(http.MethodPost, contractPostPath, contractValidBody())
		req.Header.Set("Content-Type", "text/plain")
		recorder := contractDo(router, req)
		contractAssertErrorEnvelope(t, recorder, http.StatusUnsupportedMediaType, "INVALID_REQUEST")
		contractAssertCalls(t, submit, query, 0, 0)
	})

	t.Run("GET by id returns one payment", func(t *testing.T) {
		expected := contractOrder(payment.ResultSucceeded)
		contractRunGETResponse(t, contractGetByIDPath, contractQueryFound(payment.ResultSucceeded), nil, http.StatusOK, "", expected, nil, nil)
	})

	t.Run("GET by business order no returns one object", func(t *testing.T) {
		expected := contractOrder(payment.ResultUnknown)
		contractRunGETResponse(t, contractPostPath+"?business_order_no=M100", contractQueryFound(payment.ResultUnknown), nil, http.StatusOK, "", expected, nil, nil)
	})

	t.Run("GET FAILED is a normal fact", func(t *testing.T) {
		expected := contractOrder(payment.ResultFailed)
		contractRunGETResponse(t, contractGetByIDPath, contractQueryFound(payment.ResultFailed), nil, http.StatusOK, "", expected, nil, nil)
	})

	t.Run("GET UNKNOWN is not 202", func(t *testing.T) {
		expected := contractOrder(payment.ResultUnknown)
		contractRunGETResponse(t, contractGetByIDPath, contractQueryFound(payment.ResultUnknown), nil, http.StatusOK, "", expected, nil, nil)
	})

	t.Run("GET issues keep the payment", func(t *testing.T) {
		expected := contractOrder(payment.ResultUnknown)
		issues := []payment.Issue{{Code: payment.IssueCodeMockResultNotFound, Message: "mock result not found"}}
		contractRunGETResponse(t, contractGetByIDPath, payment.QueryResult{Order: expected, Issues: issues}, nil, http.StatusOK, "", expected, issues, nil)
	})

	t.Run("GET unknown issue code is dropped", func(t *testing.T) {
		expected := contractOrder(payment.ResultUnknown)
		validIssue := payment.Issue{Code: payment.IssueCodeMockResultNotFound, Message: "mock result not found"}
		issues := []payment.Issue{
			validIssue,
			{Code: "FAKE_UNKNOWN_ISSUE", Message: contractCanary},
		}
		contractRunGETResponse(t, contractGetByIDPath, payment.QueryResult{Order: expected, Issues: issues}, nil, http.StatusOK, "", expected, []payment.Issue{validIssue}, nil)
	})

	t.Run("GET missing order is 404", func(t *testing.T) {
		contractRunGETResponse(t, contractGetByIDPath, payment.QueryResult{}, payment.ErrNotFound, http.StatusNotFound, "PAYMENT_NOT_FOUND", payment.PaymentOrder{}, nil, nil)
	})

	t.Run("GET non-nil error drops attached order", func(t *testing.T) {
		attached := contractOrder(payment.ResultSucceeded)
		contractRunGETResponse(t, contractGetByIDPath, payment.QueryResult{Order: attached}, payment.ErrUnavailable, http.StatusServiceUnavailable, "PROCESSING_ERROR", payment.PaymentOrder{}, nil, nil)
	})

	t.Run("GET error mappings", func(t *testing.T) {
		attached := contractOrder(payment.ResultSucceeded)
		cases := []struct {
			name       string
			err        error
			wantStatus int
			wantCode   string
		}{
			{"invalid_input", payment.ErrInvalidInput, http.StatusBadRequest, "INVALID_REQUEST"},
			{"not_found", payment.ErrNotFound, http.StatusNotFound, "PAYMENT_NOT_FOUND"},
			{"unavailable", payment.ErrUnavailable, http.StatusServiceUnavailable, "PROCESSING_ERROR"},
			{"wrapped_unavailable", fmt.Errorf("FAKE_LEAK_CANARY_HTTP_CONTRACT: %w", payment.ErrUnavailable), http.StatusServiceUnavailable, "PROCESSING_ERROR"},
			{"unexpected", errors.New("FAKE_LEAK_CANARY_HTTP_CONTRACT: raw internal failure"), http.StatusInternalServerError, "PROCESSING_ERROR"},
			{"timeout", fmt.Errorf("FAKE_LEAK_CANARY_HTTP_CONTRACT: %w", context.DeadlineExceeded), http.StatusServiceUnavailable, "PROCESSING_ERROR"},
		}
		for _, tc := range cases {
			tc := tc
			t.Run(tc.name, func(t *testing.T) {
				contractRunGETResponse(t, contractGetByIDPath, payment.QueryResult{Order: attached}, tc.err, tc.wantStatus, tc.wantCode, payment.PaymentOrder{}, nil, nil)
			})
		}
	})

	t.Run("conditional GET still queries", func(t *testing.T) {
		expected := contractOrder(payment.ResultSucceeded)
		prepare := func(req *http.Request) {
			req.Header.Set("If-None-Match", `"fake-etag"`)
			req.Header.Set("If-Modified-Since", "Wed, 21 Oct 2015 07:28:00 GMT")
		}
		contractRunGETResponse(t, contractGetByIDPath, contractQueryFound(payment.ResultSucceeded), nil, http.StatusOK, "", expected, nil, prepare)
	})

	t.Run("request ids are per-request UUIDv4", func(t *testing.T) {
		submit := &contractSubmitFake{t: t, result: contractSubmitCreated()}
		query := &contractQueryFake{t: t, result: contractQueryFound(payment.ResultSucceeded)}
		router := contractNewRouter(t, submit, query)
		expected := contractOrder(payment.ResultSucceeded)

		firstRequest, _ := contractNewRequest(http.MethodGet, contractGetByIDPath, nil)
		first := contractDo(router, firstRequest)
		contractAssertPaymentEnvelope(t, first, http.StatusOK, expected, nil, false)

		secondRequest, _ := contractNewRequest(http.MethodGet, contractGetByIDPath, nil)
		second := contractDo(router, secondRequest)
		contractAssertPaymentEnvelope(t, second, http.StatusOK, expected, nil, false)

		firstID := contractAssertRequestID(t, contractDecodeObject(t, first)["request_id"])
		secondID := contractAssertRequestID(t, contractDecodeObject(t, second)["request_id"])
		if firstID == secondID {
			contractFail(t, "request_id", "separate requests reused the same request_id")
		}
		if query.calls != 2 {
			contractFail(t, "query_calls", "Query calls = %d, want 2", query.calls)
		}
	})
}
