package app

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5/pgconn"

	"payment-hub/internal/payment"
)

// LogLevel is the closed set of runtime log severities.
type LogLevel string

const (
	LogLevelInfo  LogLevel = "info"
	LogLevelWarn  LogLevel = "warn"
	LogLevelError LogLevel = "error"
)

// LogEvent is the closed set of runtime event categories.
type LogEvent string

const (
	LogEventRequest    LogEvent = "request"
	LogEventDependency LogEvent = "dependency"
	LogEventShutdown   LogEvent = "shutdown"
	LogEventTrace      LogEvent = "trace"
	LogEventUnknown    LogEvent = "unknown"
)

// LogStage is the closed set of operation stages.
type LogStage string

const (
	LogStageEntry       LogStage = "entry"
	LogStagePayment     LogStage = "payment"
	LogStageQuery       LogStage = "query"
	LogStagePersistence LogStage = "persistence"
	LogStageResponse    LogStage = "response"
	LogStageShutdown    LogStage = "shutdown"
	LogStageUnknown     LogStage = "unknown"
)

// LogReason is the closed set of stable, sanitized reason categories.
type LogReason string

const (
	LogReasonInvalidInput LogReason = "invalid_input"
	LogReasonDependency   LogReason = "dependency"
	LogReasonTimeout      LogReason = "timeout"
	LogReasonCanceled     LogReason = "canceled"
	LogReasonInternal     LogReason = "internal"
	LogReasonUnknown      LogReason = "unknown"
)

// LogErrorKind is the closed set of system-error classifications. It is
// derived from the error value, never from the error text.
type LogErrorKind string

const (
	LogErrorKindTimeout  LogErrorKind = "timeout"
	LogErrorKindCanceled LogErrorKind = "canceled"
	LogErrorKindDatabase LogErrorKind = "database"
	LogErrorKindHTTP     LogErrorKind = "http"
	LogErrorKindInternal LogErrorKind = "internal"
)

// LogRecord is the fixed input to one runtime log line. Associations must be
// supplied explicitly by a trusted caller; this type has no generic attribute
// map and no raw message field. Err may be a framework or database error but is
// classified only by type/identity, never serialized.
type LogRecord struct {
	Level           LogLevel
	Event           LogEvent
	Stage           LogStage
	RequestID       payment.RequestID
	ClientID        string
	BusinessOrderNo string
	PaymentOrderID  payment.PaymentOrderID
	ResultBefore    *payment.Result
	ResultAfter     *payment.Result
	Reason          LogReason
	Err             error
}

// ErrLogWriteFailed reports that a log line could not be fully written. It is
// deliberately independent of the underlying writer error so that raw error
// text cannot leak through the returned value.
var ErrLogWriteFailed = errors.New("log write failed")

// Logger writes newline-delimited JSON objects to one injected writer. It is
// safe for concurrent use and serializes access to the writer itself. A nil
// writer fails closed; Logger never falls back to a global writer such as
// os.Stdout.
type Logger struct {
	mu     sync.Mutex
	writer io.Writer
}

// NewLogger returns a Logger that writes to w. The caller owns w and must keep
// it valid for the lifetime of the Logger.
func NewLogger(w io.Writer) *Logger {
	return &Logger{writer: w}
}

// logEntry is the fixed output whitelist. Do not add raw messages, error text,
// SQL, payloads, or connection strings here.
type logEntry struct {
	Time            string `json:"time"`
	Level           string `json:"level"`
	Event           string `json:"event"`
	Stage           string `json:"stage"`
	RequestID       string `json:"request_id,omitempty"`
	ClientID        string `json:"client_id,omitempty"`
	BusinessOrderNo string `json:"business_order_no,omitempty"`
	PaymentOrderID  string `json:"payment_order_id,omitempty"`
	ResultBefore    string `json:"result_before,omitempty"`
	ResultAfter     string `json:"result_after,omitempty"`
	Reason          string `json:"reason,omitempty"`
	ErrorKind       string `json:"error_kind,omitempty"`
}

// Log writes one UTC JSON object followed by a newline. Unknown or invalid
// associations are omitted. Unknown controlled values are replaced by fixed
// fallback categories; their original text is never written. A nil Err produces
// no error_kind, and a business FAILED result is not treated as a system error.
func (l *Logger) Log(record LogRecord) error {
	if l == nil || l.writer == nil {
		return ErrLogWriteFailed
	}

	entry := logEntry{
		Time:  time.Now().UTC().Format(time.RFC3339Nano),
		Level: normalizeLogLevel(record.Level),
		Event: normalizeLogEvent(record.Event),
		Stage: normalizeLogStage(record.Stage),
	}

	if record.RequestID != "" && payment.ValidateUUIDv4(string(record.RequestID)) == nil {
		entry.RequestID = string(record.RequestID)
	}
	if record.ClientID != "" && payment.ValidateClientID(record.ClientID) == nil {
		entry.ClientID = record.ClientID
	}
	if record.BusinessOrderNo != "" && payment.ValidateBusinessOrderNo(record.BusinessOrderNo) == nil {
		entry.BusinessOrderNo = record.BusinessOrderNo
	}
	if record.PaymentOrderID != "" && payment.ValidateUUIDv4(string(record.PaymentOrderID)) == nil {
		entry.PaymentOrderID = string(record.PaymentOrderID)
	}
	if record.ResultBefore != nil && payment.ValidateResult(*record.ResultBefore) == nil {
		entry.ResultBefore = string(*record.ResultBefore)
	}
	if record.ResultAfter != nil && payment.ValidateResult(*record.ResultAfter) == nil {
		entry.ResultAfter = string(*record.ResultAfter)
	}
	if record.Reason != "" {
		entry.Reason = normalizeLogReason(record.Reason)
	}
	if record.Err != nil {
		entry.ErrorKind = classifyLogError(record.Err)
	}

	data, err := json.Marshal(entry)
	if err != nil {
		return ErrLogWriteFailed
	}
	data = append(data, '\n')

	l.mu.Lock()
	defer l.mu.Unlock()

	n, err := l.writer.Write(data)
	if err != nil || n != len(data) {
		return ErrLogWriteFailed
	}
	return nil
}

func normalizeLogLevel(level LogLevel) string {
	switch level {
	case LogLevelInfo, LogLevelWarn, LogLevelError:
		return string(level)
	default:
		return string(LogLevelInfo)
	}
}

func normalizeLogEvent(event LogEvent) string {
	switch event {
	case LogEventRequest, LogEventDependency, LogEventShutdown, LogEventTrace:
		return string(event)
	default:
		return string(LogEventUnknown)
	}
}

func normalizeLogStage(stage LogStage) string {
	switch stage {
	case LogStageEntry, LogStagePayment, LogStageQuery, LogStagePersistence, LogStageResponse, LogStageShutdown:
		return string(stage)
	default:
		return string(LogStageUnknown)
	}
}

func normalizeLogReason(reason LogReason) string {
	switch reason {
	case LogReasonInvalidInput, LogReasonDependency, LogReasonTimeout, LogReasonCanceled, LogReasonInternal:
		return string(reason)
	default:
		return string(LogReasonUnknown)
	}
}

func classifyLogError(err error) string {
	switch {
	case errors.Is(err, context.DeadlineExceeded):
		return string(LogErrorKindTimeout)
	case errors.Is(err, context.Canceled):
		return string(LogErrorKindCanceled)
	}

	var pgError *pgconn.PgError
	if errors.As(err, &pgError) {
		return string(LogErrorKindDatabase)
	}

	var ginError *gin.Error
	if errors.As(err, &ginError) {
		return string(LogErrorKindHTTP)
	}

	return string(LogErrorKindInternal)
}
