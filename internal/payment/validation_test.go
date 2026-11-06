package payment

import (
	"math"
	"strings"
	"testing"
	"time"
)

type testStringValidationCase struct {
	name    string
	input   string
	wantErr bool
}

func testRunStringValidationCases(t *testing.T, validate func(string) error, cases []testStringValidationCase) {
	t.Helper()
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			err := validate(tc.input)
			if tc.wantErr && err == nil {
				t.Fatal("expected error, got nil")
			}
			if !tc.wantErr && err != nil {
				t.Fatal("expected nil error, got error")
			}
		})
	}
}

func testCheckValidationErr(t *testing.T, wantErr bool, err error) {
	t.Helper()
	if wantErr && err == nil {
		t.Fatal("expected error, got nil")
	}
	if !wantErr && err != nil {
		t.Fatal("expected nil error, got error")
	}
}

func testStringPtr(s string) *string { return &s }

func testTimePtr(at time.Time) *time.Time { return &at }

func TestValidateAmount(t *testing.T) {
	cases := []struct {
		name    string
		amount  int64
		wantErr bool
	}{
		{name: "minimum", amount: 1},
		{name: "typical", amount: 100},
		{name: "just_below_maximum", amount: 999_999},
		{name: "maximum", amount: 1_000_000},
		{name: "zero", amount: 0, wantErr: true},
		{name: "negative_one", amount: -1, wantErr: true},
		{name: "negative_minimum", amount: -1_000_000, wantErr: true},
		{name: "above_maximum", amount: 1_000_001, wantErr: true},
		{name: "min_int64", amount: math.MinInt64, wantErr: true},
		{name: "max_int64", amount: math.MaxInt64, wantErr: true},
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			testCheckValidationErr(t, tc.wantErr, ValidateAmount(tc.amount))
		})
	}
}

func TestValidateCurrency(t *testing.T) {
	cases := []testStringValidationCase{
		{name: "cny_exact", input: "CNY"},
		{name: "empty", input: "", wantErr: true},
		{name: "lowercase", input: "cny", wantErr: true},
		{name: "titlecase", input: "Cny", wantErr: true},
		{name: "leading_space", input: " CNY", wantErr: true},
		{name: "trailing_space", input: "CNY ", wantErr: true},
		{name: "trailing_tab", input: "CNY\t", wantErr: true},
		{name: "newline", input: "CNY\n", wantErr: true},
		{name: "trailing_nul", input: "CNY\x00", wantErr: true},
		{name: "usd", input: "USD", wantErr: true},
		{name: "eur", input: "EUR", wantErr: true},
		{name: "rmb", input: "RMB", wantErr: true},
		{name: "cn", input: "CN", wantErr: true},
		{name: "double_y", input: "CNYY", wantErr: true},
		{name: "cjk", input: "人民币", wantErr: true},
		{name: "fullwidth", input: "ＣＮＹ", wantErr: true},
		{name: "nul", input: "\x00", wantErr: true},
	}

	testRunStringValidationCases(t, ValidateCurrency, cases)
}

func TestValidateReason(t *testing.T) {
	cases := []struct {
		name    string
		reason  *Reason
		wantErr bool
	}{
		{name: "nil", reason: nil},
		{name: "code_one_ascii", reason: &Reason{Code: "X"}},
		{name: "code_one_cjk", reason: &Reason{Code: "中"}},
		{name: "code_64_ascii", reason: &Reason{Code: strings.Repeat("a", 64)}},
		{name: "code_64_cjk", reason: &Reason{Code: strings.Repeat("中", 64)}},
		{name: "message_nil", reason: &Reason{Code: "X", Message: nil}},
		{name: "message_empty", reason: &Reason{Code: "X", Message: testStringPtr("")}},
		{name: "message_short", reason: &Reason{Code: "X", Message: testStringPtr("ok")}},
		{name: "message_512_ascii", reason: &Reason{Code: "X", Message: testStringPtr(strings.Repeat("a", 512))}},
		{name: "message_512_cjk", reason: &Reason{Code: "X", Message: testStringPtr(strings.Repeat("中", 512))}},
		{name: "code_empty", reason: &Reason{Code: ""}, wantErr: true},
		{name: "code_empty_with_message", reason: &Reason{Code: "", Message: testStringPtr("ok")}, wantErr: true},
		{name: "code_65_ascii", reason: &Reason{Code: strings.Repeat("a", 65)}, wantErr: true},
		{name: "code_65_cjk", reason: &Reason{Code: strings.Repeat("中", 65)}, wantErr: true},
		{name: "code_invalid_utf8", reason: &Reason{Code: string([]byte{0xff})}, wantErr: true},
		{name: "message_513_ascii", reason: &Reason{Code: "X", Message: testStringPtr(strings.Repeat("a", 513))}, wantErr: true},
		{name: "message_513_cjk", reason: &Reason{Code: "X", Message: testStringPtr(strings.Repeat("中", 513))}, wantErr: true},
		{name: "message_invalid_utf8", reason: &Reason{Code: "X", Message: testStringPtr(string([]byte{0xff}))}, wantErr: true},
		{name: "code_invalid_utf8_with_message", reason: &Reason{Code: string([]byte{0xff}), Message: testStringPtr("ok")}, wantErr: true},
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			testCheckValidationErr(t, tc.wantErr, ValidateReason(tc.reason))
		})
	}
}

func TestValidateResult(t *testing.T) {
	cases := []testStringValidationCase{
		{name: "unknown", input: string(ResultUnknown)},
		{name: "succeeded", input: string(ResultSucceeded)},
		{name: "failed", input: string(ResultFailed)},
		{name: "empty", input: "", wantErr: true},
		{name: "lowercase_unknown", input: "unknown", wantErr: true},
		{name: "trailing_space", input: "SUCCEEDED ", wantErr: true},
		{name: "newline", input: "FAILED\n", wantErr: true},
		{name: "pending", input: "PENDING", wantErr: true},
		{name: "success", input: "SUCCESS", wantErr: true},
	}

	testRunStringValidationCases(t, func(s string) error { return ValidateResult(Result(s)) }, cases)
}

func TestValidateSource(t *testing.T) {
	cases := []testStringValidationCase{
		{name: "payment", input: string(SourcePayment)},
		{name: "query", input: string(SourceQuery)},
		{name: "empty", input: "", wantErr: true},
		{name: "lowercase_payment", input: "payment", wantErr: true},
		{name: "trailing_space", input: "PAYMENT ", wantErr: true},
		{name: "newline", input: "QUERY\n", wantErr: true},
		{name: "callback", input: "CALLBACK", wantErr: true},
		{name: "mock", input: "MOCK", wantErr: true},
	}

	testRunStringValidationCases(t, func(s string) error { return ValidateSource(Source(s)) }, cases)
}

func TestValidateHandlingDecision(t *testing.T) {
	cases := []testStringValidationCase{
		{name: "accept_confirmation", input: string(DecisionAcceptConfirmation)},
		{name: "keep_unknown", input: string(DecisionKeepUnknown)},
		{name: "keep_confirmed", input: string(DecisionKeepConfirmed)},
		{name: "reject_evidence", input: string(DecisionRejectEvidence)},
		{name: "empty", input: "", wantErr: true},
		{name: "lowercase", input: "accept_confirmation", wantErr: true},
		{name: "trailing_space", input: "KEEP_UNKNOWN ", wantErr: true},
		{name: "newline", input: "KEEP_CONFIRMED\n", wantErr: true},
		{name: "accept", input: "ACCEPT", wantErr: true},
		{name: "keep", input: "KEEP", wantErr: true},
		{name: "reject", input: "REJECT", wantErr: true},
	}

	testRunStringValidationCases(t, func(s string) error {
		return ValidateHandlingDecision(HandlingDecision(s))
	}, cases)
}

func TestValidateUTC(t *testing.T) {
	utc := time.Date(2026, 11, 6, 9, 0, 0, 123456789, time.UTC)
	fixedZero := time.Date(2026, 11, 6, 9, 0, 0, 0, time.FixedZone("UTC+0", 0))
	positiveOffset := time.Date(2026, 11, 6, 9, 0, 0, 0, time.FixedZone("CST", 8*60*60))
	negativeOffset := time.Date(2026, 11, 6, 9, 0, 0, 0, time.FixedZone("EST", -5*60*60))
	oneSecondOffset := time.Date(2026, 11, 6, 9, 0, 0, 0, time.FixedZone("ODD", 1))

	cases := []struct {
		name    string
		at      time.Time
		wantErr bool
	}{
		{name: "utc", at: utc},
		{name: "fixed_zone_zero", at: fixedZero},
		{name: "zero_value", at: time.Time{}},
		{name: "positive_offset", at: positiveOffset, wantErr: true},
		{name: "negative_offset", at: negativeOffset, wantErr: true},
		{name: "one_second_offset", at: oneSecondOffset, wantErr: true},
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			testCheckValidationErr(t, tc.wantErr, ValidateUTC(tc.at))
		})
	}
}

func TestValidateResultTime(t *testing.T) {
	utc := time.Date(2026, 11, 6, 9, 0, 0, 0, time.UTC)
	fixedZero := time.Date(2026, 11, 6, 9, 0, 0, 0, time.FixedZone("UTC+0", 0))
	positiveOffset := time.Date(2026, 11, 6, 9, 0, 0, 0, time.FixedZone("CST", 8*60*60))
	zero := time.Time{}

	cases := []struct {
		name        string
		result      Result
		confirmedAt *time.Time
		wantErr     bool
	}{
		{name: "unknown_nil", result: ResultUnknown, confirmedAt: nil},
		{name: "unknown_non_nil_utc", result: ResultUnknown, confirmedAt: testTimePtr(utc), wantErr: true},
		{name: "unknown_non_nil_non_utc", result: ResultUnknown, confirmedAt: testTimePtr(positiveOffset), wantErr: true},
		{name: "succeeded_nil", result: ResultSucceeded, confirmedAt: nil, wantErr: true},
		{name: "succeeded_utc", result: ResultSucceeded, confirmedAt: testTimePtr(utc)},
		{name: "succeeded_fixed_zone_zero", result: ResultSucceeded, confirmedAt: testTimePtr(fixedZero)},
		{name: "succeeded_zero_value", result: ResultSucceeded, confirmedAt: testTimePtr(zero)},
		{name: "succeeded_non_utc", result: ResultSucceeded, confirmedAt: testTimePtr(positiveOffset), wantErr: true},
		{name: "failed_nil", result: ResultFailed, confirmedAt: nil, wantErr: true},
		{name: "failed_utc", result: ResultFailed, confirmedAt: testTimePtr(utc)},
		{name: "failed_non_utc", result: ResultFailed, confirmedAt: testTimePtr(positiveOffset), wantErr: true},
		{name: "invalid_result_empty_nil", result: Result(""), confirmedAt: nil, wantErr: true},
		{name: "invalid_result_pending_utc", result: Result("PENDING"), confirmedAt: testTimePtr(utc), wantErr: true},
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			testCheckValidationErr(t, tc.wantErr, ValidateResultTime(tc.result, tc.confirmedAt))
		})
	}
}
