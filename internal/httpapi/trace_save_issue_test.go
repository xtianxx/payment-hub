package httpapi_test

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"payment-hub/internal/payment"
)

type traceSaveIssueJSON struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func traceSaveIssueDecode(t *testing.T, recorder *httptest.ResponseRecorder) []traceSaveIssueJSON {
	t.Helper()
	top := contractDecodeObject(t, recorder)
	raw, ok := top["issues"]
	if !ok {
		contractFail(t, "issues", "issues are missing from the response")
	}
	var issues []traceSaveIssueJSON
	if err := json.Unmarshal(raw, &issues); err != nil {
		contractFail(t, "issues", "issues are not an array of objects")
	}
	return issues
}

func traceSaveIssueAssertExact(t *testing.T, got, want []traceSaveIssueJSON) {
	t.Helper()
	if len(got) != len(want) {
		contractFail(t, "issues", "issues count mismatch")
	}
	for i := range want {
		if got[i].Code != want[i].Code || got[i].Message != want[i].Message {
			contractFail(t, "issues", "issue %d did not use the controlled code/message", i)
		}
	}
}

func TestTraceSaveIssueVariants(t *testing.T) {
	variants := []struct {
		name     string
		input    string
		external string
	}{
		{name: "failed", input: "本次追踪保存失败", external: "trace save failed"},
		{name: "indeterminate", input: "保存状态未确认", external: "trace save state unconfirmed"},
	}

	t.Run("POST new registration 201 keeps each controlled trace variant separate", func(t *testing.T) {
		for _, variant := range variants {
			variant := variant
			t.Run(variant.name, func(t *testing.T) {
				order := contractOrder(payment.ResultSucceeded)
				issues := []payment.Issue{
					{Code: payment.IssueCodeMockResultUnconfirmed, Message: "ignored-mock-input"},
					{Code: payment.IssueCodeTraceSaveError, Message: variant.input},
				}
				result := payment.SubmitResult{Order: order, Created: true, Issues: issues}
				submit := &contractSubmitFake{t: t, result: result}
				query := &contractQueryFake{t: t, result: contractQueryFound(payment.ResultSucceeded)}
				router := contractNewRouter(t, submit, query)
				req, _ := contractNewRequest(http.MethodPost, contractPostPath, contractValidBody())
				recorder := contractDo(router, req)

				contractAssertPaymentEnvelope(t, recorder, http.StatusCreated, order, issues, true)
				want := []traceSaveIssueJSON{
					{Code: payment.IssueCodeMockResultUnconfirmed, Message: "mock result unconfirmed"},
					{Code: payment.IssueCodeTraceSaveError, Message: variant.external},
				}
				traceSaveIssueAssertExact(t, traceSaveIssueDecode(t, recorder), want)
				if strings.Contains(recorder.Body.String(), variant.input) {
					contractFail(t, "message_leak", "the controlled input message leaked into the HTTP body")
				}
				contractAssertCalls(t, submit, query, 1, 0)
			})
		}
	})

	t.Run("arbitrary trace message falls back and never leaks", func(t *testing.T) {
		order := contractOrder(payment.ResultSucceeded)
		issues := []payment.Issue{{Code: payment.IssueCodeTraceSaveError, Message: contractCanary}}
		result := payment.SubmitResult{Order: order, Created: true, Issues: issues}
		submit := &contractSubmitFake{t: t, result: result}
		query := &contractQueryFake{t: t, result: contractQueryFound(payment.ResultSucceeded)}
		router := contractNewRouter(t, submit, query)
		req, _ := contractNewRequest(http.MethodPost, contractPostPath, contractValidBody())
		recorder := contractDo(router, req)

		contractAssertPaymentEnvelope(t, recorder, http.StatusCreated, order, issues, true)
		want := []traceSaveIssueJSON{{Code: payment.IssueCodeTraceSaveError, Message: "trace save error"}}
		traceSaveIssueAssertExact(t, traceSaveIssueDecode(t, recorder), want)
		if strings.Contains(recorder.Body.String(), contractCanary) {
			contractFail(t, "message_leak", "arbitrary issue message leaked into the HTTP body")
		}
	})

	t.Run("other issue codes ignore arbitrary messages", func(t *testing.T) {
		order := contractOrder(payment.ResultSucceeded)
		issues := []payment.Issue{{Code: payment.IssueCodeMockResultUnconfirmed, Message: contractCanary}}
		result := payment.SubmitResult{Order: order, Created: true, Issues: issues}
		submit := &contractSubmitFake{t: t, result: result}
		query := &contractQueryFake{t: t, result: contractQueryFound(payment.ResultSucceeded)}
		router := contractNewRouter(t, submit, query)
		req, _ := contractNewRequest(http.MethodPost, contractPostPath, contractValidBody())
		recorder := contractDo(router, req)

		contractAssertPaymentEnvelope(t, recorder, http.StatusCreated, order, issues, true)
		want := []traceSaveIssueJSON{{Code: payment.IssueCodeMockResultUnconfirmed, Message: "mock result unconfirmed"}}
		traceSaveIssueAssertExact(t, traceSaveIssueDecode(t, recorder), want)
		if strings.Contains(recorder.Body.String(), contractCanary) {
			contractFail(t, "message_leak", "arbitrary issue message leaked into the HTTP body")
		}
	})
}
