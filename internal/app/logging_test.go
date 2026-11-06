package app

// This file specifies the T008 logging contract before logging.go exists.
//
// Expected implementation API (T010):
//
//	type LogLevel string
//	const (
//		LogLevelInfo  LogLevel = "info"
//		LogLevelWarn  LogLevel = "warn"
//		LogLevelError LogLevel = "error"
//	)
//
//	type LogEvent string
//	const (
//		LogEventRequest    LogEvent = "request"
//		LogEventDependency LogEvent = "dependency"
//		LogEventShutdown   LogEvent = "shutdown"
//		LogEventTrace      LogEvent = "trace"
//		LogEventUnknown    LogEvent = "unknown"
//	)
//
//	type LogStage string
//	const (
//		LogStageEntry       LogStage = "entry"
//		LogStagePayment     LogStage = "payment"
//		LogStageQuery       LogStage = "query"
//		LogStagePersistence LogStage = "persistence"
//		LogStageResponse    LogStage = "response"
//		LogStageShutdown    LogStage = "shutdown"
//		LogStageUnknown     LogStage = "unknown"
//	)
//
//	type LogReason string
//	const (
//		LogReasonInvalidInput LogReason = "invalid_input"
//		LogReasonDependency   LogReason = "dependency"
//		LogReasonTimeout      LogReason = "timeout"
//		LogReasonCanceled     LogReason = "canceled"
//		LogReasonInternal     LogReason = "internal"
//		LogReasonUnknown      LogReason = "unknown"
//	)
//
//	type LogErrorKind string
//	const (
//		LogErrorKindTimeout  LogErrorKind = "timeout"
//		LogErrorKindCanceled LogErrorKind = "canceled"
//		LogErrorKindDatabase LogErrorKind = "database"
//		LogErrorKindHTTP     LogErrorKind = "http"
//		LogErrorKindInternal LogErrorKind = "internal"
//	)
//
//	type LogRecord struct {
//		Level           LogLevel
//		Event           LogEvent
//		Stage           LogStage
//		RequestID       payment.RequestID
//		ClientID        string
//		BusinessOrderNo string
//		PaymentOrderID  payment.PaymentOrderID
//		ResultBefore    *payment.Result
//		ResultAfter     *payment.Result
//		Reason          LogReason
//		Err             error
//	}
//
//	type Logger struct { /* private writer */ }
//	func NewLogger(w io.Writer) *Logger
//	func (l *Logger) Log(record LogRecord) error
//	var ErrLogWriteFailed = errors.New("log write failed")
//
// The implementation must emit exactly one newline-terminated JSON object per
// Log call, with only these keys: time, level, event, stage, request_id,
// client_id, business_order_no, payment_order_id, result_before, result_after,
// reason, error_kind. Time is RFC3339Nano UTC. Empty or invalid associations
// are omitted. An unrecognized level falls back to info; unrecognized
// event/stage/reason values become the fixed "unknown" category. Err is never
// serialized; it is classified with errors.Is/errors.As only. A writer failure
// returns ErrLogWriteFailed exactly, never the underlying writer error text.
// Logger is safe for concurrent use. Log never panics on writer failure and
// never mutates the payment facts in the record.

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5/pgconn"

	"payment-hub/internal/payment"
)

const (
	logTestRequestID       = "3f0c1a2b-4d5e-4f60-8a9b-0c1d2e3f4a5b"
	logTestPaymentOrderID  = "9a7b6c5d-4e3f-4a2b-8c1d-0e2f3a4b5c6d"
	logTestClientID        = "client-0001"
	logTestBusinessOrderNo = "biz-order-0001"

	logTestSecret        = "SIMULATED_SECRET_MARKER_7f3a"
	logTestAuthorization = "Authorization: Bearer " + logTestSecret
	logTestDatabaseURL   = "postgres://app:" + logTestSecret + "@db.example/payment"
	logTestSQL           = "SELECT * FROM payment_orders WHERE api_key = '" + logTestSecret + "'"
	logTestPayload       = `{"authorization":"` + logTestAuthorization + `","card":"` + logTestSecret + `"}`
)

func logTestSensitiveMarkers() map[string]string {
	return map[string]string{
		"simulated secret":     logTestSecret,
		"authorization header": logTestAuthorization,
		"database URL":         logTestDatabaseURL,
		"raw SQL":              logTestSQL,
		"full payload":         logTestPayload,
	}
}

func logTestFullRecord() LogRecord {
	return LogRecord{
		Level:           LogLevelInfo,
		Event:           LogEventRequest,
		Stage:           LogStageResponse,
		RequestID:       payment.RequestID(logTestRequestID),
		ClientID:        logTestClientID,
		BusinessOrderNo: logTestBusinessOrderNo,
		PaymentOrderID:  payment.PaymentOrderID(logTestPaymentOrderID),
		ResultBefore:    logTestResultPtr(payment.ResultUnknown),
		ResultAfter:     logTestResultPtr(payment.ResultSucceeded),
	}
}

func logTestResultPtr(result payment.Result) *payment.Result {
	return &result
}

type logTestErrorWriter struct {
	err error
}

func (w logTestErrorWriter) Write([]byte) (int, error) {
	return 0, w.err
}

// logTestObjects decodes newline-delimited output and rejects anything that is
// not exactly one JSON object per non-empty line. Failure messages never
// include log content because it may contain a leaked secret.
func logTestObjects(t *testing.T, caseName string, data []byte) []map[string]json.RawMessage {
	t.Helper()

	if len(data) == 0 {
		t.Fatalf("case %q: logger produced no output", caseName)
	}
	if data[len(data)-1] != '\n' {
		t.Errorf("case %q: logger output is not newline-terminated", caseName)
	}

	trimmed := bytes.TrimSuffix(data, []byte("\n"))
	if len(trimmed) == 0 {
		t.Fatalf("case %q: logger produced only a newline", caseName)
	}

	lines := bytes.Split(trimmed, []byte("\n"))
	objects := make([]map[string]json.RawMessage, 0, len(lines))
	for i, line := range lines {
		if len(line) == 0 {
			t.Fatalf("case %q: line %d is empty", caseName, i+1)
		}

		var object map[string]json.RawMessage
		if err := json.Unmarshal(line, &object); err != nil {
			t.Fatalf("case %q: line %d is not a single JSON object", caseName, i+1)
		}
		if object == nil {
			t.Fatalf("case %q: line %d is not a JSON object", caseName, i+1)
		}

		objects = append(objects, object)
	}

	return objects
}

func logTestOneObject(t *testing.T, caseName string, data []byte) map[string]json.RawMessage {
	t.Helper()

	objects := logTestObjects(t, caseName, data)
	if len(objects) != 1 {
		t.Fatalf("case %q: got %d log objects, want 1", caseName, len(objects))
	}
	return objects[0]
}

func logTestIsAllowedField(name string) bool {
	switch name {
	case "time", "level", "event", "stage",
		"request_id", "client_id", "business_order_no", "payment_order_id",
		"result_before", "result_after", "reason", "error_kind":
		return true
	default:
		return false
	}
}

func logTestAssertAllowedFields(t *testing.T, caseName string, object map[string]json.RawMessage) {
	t.Helper()

	for name := range object {
		if !logTestIsAllowedField(name) {
			// Do not print the name: an implementation bug could have put
			// attacker-controlled text in a key.
			t.Errorf("case %q: log object contains a field outside the whitelist", caseName)
		}
	}
}

func logTestAssertUTCTime(t *testing.T, caseName string, object map[string]json.RawMessage) {
	t.Helper()

	raw, ok := object["time"]
	if !ok {
		t.Errorf("case %q: missing field %q", caseName, "time")
		return
	}

	var value string
	if err := json.Unmarshal(raw, &value); err != nil {
		t.Errorf("case %q: field %q is not a string", caseName, "time")
		return
	}

	parsed, err := time.Parse(time.RFC3339Nano, value)
	if err != nil {
		t.Errorf("case %q: field %q is not RFC3339Nano", caseName, "time")
		return
	}
	if parsed.IsZero() {
		t.Errorf("case %q: field %q is the zero time", caseName, "time")
	}
	if _, offset := parsed.Zone(); offset != 0 {
		t.Errorf("case %q: field %q is not UTC (non-zero offset)", caseName, "time")
	}
}

func logTestWantString(t *testing.T, caseName string, object map[string]json.RawMessage, field, want string) {
	t.Helper()

	raw, ok := object[field]
	if !ok {
		t.Errorf("case %q: missing field %q", caseName, field)
		return
	}

	var got string
	if err := json.Unmarshal(raw, &got); err != nil {
		t.Errorf("case %q: field %q is not a string", caseName, field)
		return
	}
	if got != want {
		// Do not print got: a mismatch may be the leak this test is looking
		// for.
		t.Errorf("case %q: field %q does not have the expected value %q", caseName, field, want)
	}
}

func logTestWantAbsent(t *testing.T, caseName string, object map[string]json.RawMessage, field string) {
	t.Helper()

	if _, ok := object[field]; ok {
		t.Errorf("case %q: field %q must be omitted", caseName, field)
	}
}

func logTestAssertNoLeak(t *testing.T, caseName string, data []byte, markers map[string]string) {
	t.Helper()

	for label, marker := range markers {
		if marker == "" {
			continue
		}
		if bytes.Contains(data, []byte(marker)) {
			// Never print the marker or the log content.
			t.Errorf("case %q: sensitive %s content leaked into log output", caseName, label)
		}
	}
}

func TestLogWritesUTCJSONWhitelist(t *testing.T) {
	const caseName = "full record"

	var buffer bytes.Buffer
	logger := NewLogger(&buffer)

	if err := logger.Log(logTestFullRecord()); err != nil {
		t.Fatalf("case %q: Log returned an unexpected error", caseName)
	}

	data := buffer.Bytes()
	object := logTestOneObject(t, caseName, data)

	logTestAssertAllowedFields(t, caseName, object)
	logTestAssertUTCTime(t, caseName, object)
	logTestWantString(t, caseName, object, "level", string(LogLevelInfo))
	logTestWantString(t, caseName, object, "event", string(LogEventRequest))
	logTestWantString(t, caseName, object, "stage", string(LogStageResponse))
	logTestWantString(t, caseName, object, "request_id", logTestRequestID)
	logTestWantString(t, caseName, object, "client_id", logTestClientID)
	logTestWantString(t, caseName, object, "business_order_no", logTestBusinessOrderNo)
	logTestWantString(t, caseName, object, "payment_order_id", logTestPaymentOrderID)
	logTestWantString(t, caseName, object, "result_before", string(payment.ResultUnknown))
	logTestWantString(t, caseName, object, "result_after", string(payment.ResultSucceeded))
	logTestWantAbsent(t, caseName, object, "reason")
	logTestWantAbsent(t, caseName, object, "error_kind")
	logTestAssertNoLeak(t, caseName, data, logTestSensitiveMarkers())
}

func TestLogOmitsUnknownAndInvalidAssociations(t *testing.T) {
	testCases := []struct {
		name   string
		mutate func(*LogRecord)
		field  string
	}{
		{
			name:   "unknown request id",
			mutate: func(record *LogRecord) { record.RequestID = "" },
			field:  "request_id",
		},
		{
			name:   "invalid request id",
			mutate: func(record *LogRecord) { record.RequestID = payment.RequestID("not-a-uuid-" + logTestSecret) },
			field:  "request_id",
		},
		{
			name:   "unknown client id",
			mutate: func(record *LogRecord) { record.ClientID = "" },
			field:  "client_id",
		},
		{
			name:   "invalid client id length",
			mutate: func(record *LogRecord) { record.ClientID = strings.Repeat("a", 65) },
			field:  "client_id",
		},
		{
			name:   "invalid client id non ascii",
			mutate: func(record *LogRecord) { record.ClientID = "客户-" + logTestSecret },
			field:  "client_id",
		},
		{
			name:   "unknown business order number",
			mutate: func(record *LogRecord) { record.BusinessOrderNo = "" },
			field:  "business_order_no",
		},
		{
			name:   "invalid business order number length",
			mutate: func(record *LogRecord) { record.BusinessOrderNo = strings.Repeat("b", 65) },
			field:  "business_order_no",
		},
		{
			name:   "invalid business order number non ascii",
			mutate: func(record *LogRecord) { record.BusinessOrderNo = "订单-" + logTestSecret },
			field:  "business_order_no",
		},
		{
			name:   "unknown payment order id",
			mutate: func(record *LogRecord) { record.PaymentOrderID = "" },
			field:  "payment_order_id",
		},
		{
			name:   "invalid payment order id",
			mutate: func(record *LogRecord) { record.PaymentOrderID = payment.PaymentOrderID("not-a-uuid-" + logTestSecret) },
			field:  "payment_order_id",
		},
		{
			name:   "unknown result before",
			mutate: func(record *LogRecord) { record.ResultBefore = nil },
			field:  "result_before",
		},
		{
			name:   "unknown result after",
			mutate: func(record *LogRecord) { record.ResultAfter = nil },
			field:  "result_after",
		},
		{
			name: "invalid result before",
			mutate: func(record *LogRecord) {
				record.ResultBefore = logTestResultPtr(payment.Result("BROKEN_" + logTestSecret))
			},
			field: "result_before",
		},
		{
			name: "invalid result after",
			mutate: func(record *LogRecord) {
				record.ResultAfter = logTestResultPtr(payment.Result("BROKEN_" + logTestSecret))
			},
			field: "result_after",
		},
	}

	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			var buffer bytes.Buffer
			logger := NewLogger(&buffer)

			record := logTestFullRecord()
			testCase.mutate(&record)

			if err := logger.Log(record); err != nil {
				t.Fatalf("case %q: Log returned an unexpected error", testCase.name)
			}

			data := buffer.Bytes()
			object := logTestOneObject(t, testCase.name, data)

			logTestAssertAllowedFields(t, testCase.name, object)
			logTestAssertUTCTime(t, testCase.name, object)
			logTestWantAbsent(t, testCase.name, object, testCase.field)
			logTestAssertNoLeak(t, testCase.name, data, logTestSensitiveMarkers())
		})
	}
}

func TestLogSanitizesControlledFields(t *testing.T) {
	testCases := []struct {
		name       string
		mutate     func(*LogRecord)
		field      string
		want       string
		wantAbsent bool
	}{
		{
			name:   "invalid level",
			mutate: func(record *LogRecord) { record.Level = LogLevel("Bearer " + logTestSecret) },
			field:  "level",
			want:   string(LogLevelInfo),
		},
		{
			name:   "invalid event",
			mutate: func(record *LogRecord) { record.Event = LogEvent("payload " + logTestPayload) },
			field:  "event",
			want:   string(LogEventUnknown),
		},
		{
			name:   "invalid stage",
			mutate: func(record *LogRecord) { record.Stage = LogStage("sql " + logTestSQL) },
			field:  "stage",
			want:   string(LogStageUnknown),
		},
		{
			name:   "invalid reason",
			mutate: func(record *LogRecord) { record.Reason = LogReason("Bearer " + logTestSecret) },
			field:  "reason",
			want:   string(LogReasonUnknown),
		},
		{
			name:   "valid reason",
			mutate: func(record *LogRecord) { record.Reason = LogReasonInvalidInput },
			field:  "reason",
			want:   string(LogReasonInvalidInput),
		},
		{
			name:       "empty reason",
			mutate:     func(record *LogRecord) { record.Reason = "" },
			field:      "reason",
			wantAbsent: true,
		},
	}

	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			var buffer bytes.Buffer
			logger := NewLogger(&buffer)

			record := logTestFullRecord()
			testCase.mutate(&record)

			if err := logger.Log(record); err != nil {
				t.Fatalf("case %q: Log returned an unexpected error", testCase.name)
			}

			data := buffer.Bytes()
			object := logTestOneObject(t, testCase.name, data)

			logTestAssertAllowedFields(t, testCase.name, object)
			logTestAssertUTCTime(t, testCase.name, object)
			if testCase.wantAbsent {
				logTestWantAbsent(t, testCase.name, object, testCase.field)
			} else {
				logTestWantString(t, testCase.name, object, testCase.field, testCase.want)
			}
			logTestWantAbsent(t, testCase.name, object, "error_kind")
			logTestAssertNoLeak(t, testCase.name, data, logTestSensitiveMarkers())
		})
	}
}

func TestLogClassifiesErrorsWithoutRawContent(t *testing.T) {
	ginContext, _ := gin.CreateTestContext(httptest.NewRecorder())
	ginError := ginContext.Error(errors.New("Authorization: Bearer " + logTestSecret))
	ginError.Meta = map[string]any{
		"authorization": logTestAuthorization,
		"payload":       logTestPayload,
		"database_url":  logTestDatabaseURL,
	}

	pgError := &pgconn.PgError{
		Severity:      "ERROR",
		Code:          "23505",
		Message:       "duplicate key " + logTestSecret,
		Detail:        "Key (api_key)=(" + logTestSecret + ")",
		Hint:          logTestSQL,
		InternalQuery: logTestSQL,
		Where:         logTestPayload,
	}

	testCases := []struct {
		name string
		err  error
		want string
	}{
		{
			name: "deadline exceeded",
			err:  context.DeadlineExceeded,
			want: string(LogErrorKindTimeout),
		},
		{
			name: "wrapped deadline exceeded",
			err:  fmt.Errorf("authorization=%s: %w", logTestAuthorization, context.DeadlineExceeded),
			want: string(LogErrorKindTimeout),
		},
		{
			name: "canceled",
			err:  context.Canceled,
			want: string(LogErrorKindCanceled),
		},
		{
			name: "pg error",
			err:  pgError,
			want: string(LogErrorKindDatabase),
		},
		{
			name: "wrapped pg error",
			err:  fmt.Errorf("database_url=%s: %w", logTestDatabaseURL, pgError),
			want: string(LogErrorKindDatabase),
		},
		{
			name: "gin error",
			err:  ginError,
			want: string(LogErrorKindHTTP),
		},
		{
			name: "generic error mentioning timeout",
			err:  errors.New("timeout while handling " + logTestAuthorization),
			want: string(LogErrorKindInternal),
		},
		{
			name: "generic error with sensitive context",
			err:  fmt.Errorf("payload=%s sql=%s database_url=%s", logTestPayload, logTestSQL, logTestDatabaseURL),
			want: string(LogErrorKindInternal),
		},
	}

	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			var buffer bytes.Buffer
			logger := NewLogger(&buffer)

			record := logTestFullRecord()
			record.Level = LogLevelError
			record.Event = LogEventDependency
			record.Stage = LogStagePersistence
			record.Err = testCase.err

			if err := logger.Log(record); err != nil {
				t.Fatalf("case %q: Log returned an unexpected error", testCase.name)
			}

			data := buffer.Bytes()
			object := logTestOneObject(t, testCase.name, data)

			logTestAssertAllowedFields(t, testCase.name, object)
			logTestAssertUTCTime(t, testCase.name, object)
			logTestWantString(t, testCase.name, object, "level", string(LogLevelError))
			logTestWantString(t, testCase.name, object, "event", string(LogEventDependency))
			logTestWantString(t, testCase.name, object, "stage", string(LogStagePersistence))
			logTestWantString(t, testCase.name, object, "error_kind", testCase.want)
			logTestWantAbsent(t, testCase.name, object, "reason")
			logTestAssertNoLeak(t, testCase.name, data, logTestSensitiveMarkers())
		})
	}
}

func TestLogBusinessFailedIsNotAnErrorEvent(t *testing.T) {
	const caseName = "business failed"

	var buffer bytes.Buffer
	logger := NewLogger(&buffer)

	record := logTestFullRecord()
	record.Level = LogLevelInfo
	record.Event = LogEventRequest
	record.Stage = LogStageResponse
	record.ResultBefore = logTestResultPtr(payment.ResultUnknown)
	record.ResultAfter = logTestResultPtr(payment.ResultFailed)
	record.Reason = ""
	record.Err = nil

	if err := logger.Log(record); err != nil {
		t.Fatalf("case %q: Log returned an unexpected error", caseName)
	}

	data := buffer.Bytes()
	object := logTestOneObject(t, caseName, data)

	logTestAssertAllowedFields(t, caseName, object)
	logTestAssertUTCTime(t, caseName, object)
	logTestWantString(t, caseName, object, "level", string(LogLevelInfo))
	logTestWantString(t, caseName, object, "result_before", string(payment.ResultUnknown))
	logTestWantString(t, caseName, object, "result_after", string(payment.ResultFailed))
	logTestWantAbsent(t, caseName, object, "error_kind")
	logTestWantAbsent(t, caseName, object, "reason")
	logTestAssertNoLeak(t, caseName, data, logTestSensitiveMarkers())
}

func TestLogWriterFailureIsControlledAndRedacted(t *testing.T) {
	const caseName = "writer failure"

	writerError := fmt.Errorf(
		"write failed: authorization=%s database_url=%s sql=%s payload=%s",
		logTestAuthorization,
		logTestDatabaseURL,
		logTestSQL,
		logTestPayload,
	)
	logger := NewLogger(logTestErrorWriter{err: writerError})

	record := logTestFullRecord()
	record.Level = LogLevelError
	record.Event = LogEventDependency
	record.Stage = LogStagePersistence
	record.ResultBefore = logTestResultPtr(payment.ResultUnknown)
	record.ResultAfter = logTestResultPtr(payment.ResultFailed)

	err := logger.Log(record)
	if err == nil {
		t.Fatalf("case %q: Log returned nil error for a failing writer", caseName)
	}
	if !errors.Is(err, ErrLogWriteFailed) {
		t.Errorf("case %q: Log did not return the controlled write error", caseName)
	}
	if err.Error() != ErrLogWriteFailed.Error() {
		t.Errorf("case %q: returned error text is not the controlled sentinel text", caseName)
	}
	logTestAssertNoLeak(t, caseName, []byte(err.Error()), logTestSensitiveMarkers())

	if record.ResultBefore == nil || *record.ResultBefore != payment.ResultUnknown {
		t.Errorf("case %q: Log changed the before payment fact in the record", caseName)
	}
	if record.ResultAfter == nil || *record.ResultAfter != payment.ResultFailed {
		t.Errorf("case %q: Log changed the after payment fact in the record", caseName)
	}
}

func TestLogConcurrentWritesRemainOneJSONObjectPerLine(t *testing.T) {
	const (
		goroutines   = 16
		perGoroutine = 25
		total        = goroutines * perGoroutine
	)

	// Deliberately use an unsynchronized bytes.Buffer: Logger is the component
	// under test and must serialize access to the underlying io.Writer. The
	// buffer is read only after WaitGroup.Wait, so the test itself does not
	// introduce a concurrent write/read.
	var buffer bytes.Buffer
	logger := NewLogger(&buffer)

	errorsChannel := make(chan error, total)
	var waitGroup sync.WaitGroup

	for i := 0; i < goroutines; i++ {
		waitGroup.Add(1)
		go func() {
			defer waitGroup.Done()
			for j := 0; j < perGoroutine; j++ {
				record := logTestFullRecord()
				record.Stage = LogStagePayment
				if err := logger.Log(record); err != nil {
					errorsChannel <- err
				}
			}
		}()
	}

	waitGroup.Wait()
	close(errorsChannel)

	for range errorsChannel {
		// Do not print the error: it may contain writer or payload data.
		t.Errorf("case %q: concurrent Log returned an unexpected error", "concurrent writes")
	}

	data := buffer.Bytes()
	objects := logTestObjects(t, "concurrent writes", data)
	if len(objects) != total {
		t.Fatalf("case %q: got %d log objects, want %d", "concurrent writes", len(objects), total)
	}

	for i, object := range objects {
		caseName := fmt.Sprintf("concurrent write line %d", i+1)
		logTestAssertAllowedFields(t, caseName, object)
		logTestAssertUTCTime(t, caseName, object)
		logTestWantString(t, caseName, object, "level", string(LogLevelInfo))
		logTestWantString(t, caseName, object, "event", string(LogEventRequest))
		logTestWantString(t, caseName, object, "stage", string(LogStagePayment))
		logTestWantString(t, caseName, object, "request_id", logTestRequestID)
		logTestWantString(t, caseName, object, "client_id", logTestClientID)
		logTestWantString(t, caseName, object, "business_order_no", logTestBusinessOrderNo)
		logTestWantString(t, caseName, object, "payment_order_id", logTestPaymentOrderID)
	}

	logTestAssertNoLeak(t, "concurrent writes", data, logTestSensitiveMarkers())
}
