package testcontrol

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"strconv"
	"strings"
	"unicode/utf8"
)

const (
	Version        = 1
	MaxFrameBytes  = 4096
	MaxRecords     = 4096
	MaxGenerations = 64
)

type Type string

const (
	TypeBegin Type = "begin"
	TypeCall  Type = "call"
	TypeReach Type = "reach"
	TypeSeal  Type = "seal"
	TypeAck   Type = "ack"
	TypeFault Type = "fault"
)

type Kind string

const (
	KindPayment Kind = "PAYMENT"
	KindQuery   Kind = "QUERY"
)

type Point string

const (
	PointRegistered      Point = "REGISTERED"
	PointMockCommitted   Point = "MOCK_COMMITTED"
	PointSavingResult    Point = "SAVING_RESULT"
	PointResultCommitted Point = "RESULT_COMMITTED"
)

type Action string

const (
	ActionContinue    Action = "continue"
	ActionReplyHidden Action = "reply_hidden"
)

type Directive string

const (
	DirectiveSucceeded Directive = "SUCCEEDED"
	DirectiveFailed    Directive = "FAILED"
)

type FaultCode string

const (
	FaultProtocol   FaultCode = "protocol"
	FaultIncomplete FaultCode = "incomplete"
	FaultTimeout    FaultCode = "timeout"
	FaultClosed     FaultCode = "closed"
)

var (
	ErrProtocol     = errors.New("testcontrol: protocol error")
	ErrControlFault = errors.New("testcontrol: control fault")
	ErrReplyHidden  = errors.New("testcontrol: reply hidden")
	ErrUncertain    = errors.New("testcontrol: uncertain")
)

type Session struct {
	Socket     string
	RunID      string
	Epoch      string
	Generation uint64
}

type Receipt struct {
	RunID      string
	Epoch      string
	Generation uint64
	LastSeq    uint64
}

type Frame struct {
	Version         int        `json:"v"`
	Type            Type       `json:"type"`
	RunID           string     `json:"run_id,omitempty"`
	Epoch           string     `json:"observer_epoch,omitempty"`
	Generation      uint64     `json:"generation,omitempty"`
	Seq             uint64     `json:"seq,omitempty"`
	EventID         string     `json:"event_id,omitempty"`
	Kind            Kind       `json:"kind,omitempty"`
	Point           Point      `json:"point,omitempty"`
	OrderID         string     `json:"order_id,omitempty"`
	ExpectedLastSeq *uint64    `json:"expected_last_seq,omitempty"`
	For             Type       `json:"for,omitempty"`
	Action          Action     `json:"action,omitempty"`
	Code            FaultCode  `json:"code,omitempty"`
	Directive       *Directive `json:"directive,omitempty"`
	PaymentCallSeq  *uint64    `json:"payment_call_seq,omitempty"`
	AppliedResult   *Directive `json:"applied_result,omitempty"`
}

var errFrame = errors.New("invalid frame")

var identityKeys = []string{"v", "type", "run_id", "observer_epoch", "generation", "event_id"}

func EncodeFrame(frame Frame) ([]byte, error) {
	if err := validateFrame(frame); err != nil {
		return nil, ErrProtocol
	}
	encoded, err := json.Marshal(frame)
	if err != nil {
		return nil, ErrProtocol
	}
	encoded = append(encoded, '\n')
	if len(encoded) > MaxFrameBytes {
		return nil, ErrProtocol
	}
	return encoded, nil
}

func DecodeFrame(data []byte) (Frame, error) {
	if len(data) == 0 || len(data) > MaxFrameBytes {
		return Frame{}, ErrProtocol
	}
	if data[len(data)-1] != '\n' {
		return Frame{}, ErrProtocol
	}
	if bytes.Count(data, []byte{'\n'}) != 1 {
		return Frame{}, ErrProtocol
	}
	if !utf8.Valid(data) {
		return Frame{}, ErrProtocol
	}
	fields, err := parseObject(data[:len(data)-1])
	if err != nil {
		return Frame{}, ErrProtocol
	}
	frame, err := frameFromFields(fields)
	if err != nil {
		return Frame{}, ErrProtocol
	}
	if err := validateFrame(frame); err != nil {
		return Frame{}, ErrProtocol
	}
	return frame, nil
}

func parseObject(body []byte) (map[string]json.RawMessage, error) {
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	token, err := decoder.Token()
	if err != nil {
		return nil, errFrame
	}
	delim, ok := token.(json.Delim)
	if !ok || delim != '{' {
		return nil, errFrame
	}
	fields := make(map[string]json.RawMessage)
	for decoder.More() {
		keyToken, err := decoder.Token()
		if err != nil {
			return nil, errFrame
		}
		key, ok := keyToken.(string)
		if !ok {
			return nil, errFrame
		}
		if _, exists := fields[key]; exists {
			return nil, errFrame
		}
		var raw json.RawMessage
		if err := decoder.Decode(&raw); err != nil {
			return nil, errFrame
		}
		fields[key] = raw
	}
	token, err = decoder.Token()
	if err != nil {
		return nil, errFrame
	}
	delim, ok = token.(json.Delim)
	if !ok || delim != '}' {
		return nil, errFrame
	}
	if _, err := decoder.Token(); err != io.EOF {
		return nil, errFrame
	}
	return fields, nil
}

func frameFromFields(fields map[string]json.RawMessage) (Frame, error) {
	version, err := decodeIntField(fields, "v")
	if err != nil || version != Version {
		return Frame{}, errFrame
	}
	typeValue, err := decodeStringField(fields, "type")
	if err != nil {
		return Frame{}, errFrame
	}
	frameType := Type(typeValue)

	if frameType == TypeFault {
		if err := requireExactKeys(fields, "v", "type", "code"); err != nil {
			return Frame{}, errFrame
		}
		codeValue, err := decodeStringField(fields, "code")
		if err != nil {
			return Frame{}, errFrame
		}
		frame := Frame{Version: Version, Type: TypeFault, Code: FaultCode(codeValue)}
		if err := validateFrame(frame); err != nil {
			return Frame{}, errFrame
		}
		return frame, nil
	}

	switch frameType {
	case TypeBegin:
		if err := requireExactKeys(fields, identityKeys...); err != nil {
			return Frame{}, errFrame
		}
	case TypeCall:
		if err := requireExactKeys(fields, append(append([]string{}, identityKeys...), "seq", "kind", "order_id")...); err != nil {
			return Frame{}, errFrame
		}
	case TypeReach:
		if err := requireExactOrOptionalKeys(fields, append(append([]string{}, identityKeys...), "seq", "point", "order_id"), "payment_call_seq", "applied_result"); err != nil {
			return Frame{}, errFrame
		}
	case TypeSeal:
		if err := requireExactKeys(fields, append(append([]string{}, identityKeys...), "expected_last_seq")...); err != nil {
			return Frame{}, errFrame
		}
	case TypeAck:
		forValue, err := decodeStringField(fields, "for")
		if err != nil {
			return Frame{}, errFrame
		}
		switch Type(forValue) {
		case TypeBegin:
			if err := requireExactKeys(fields, append(append([]string{}, identityKeys...), "for")...); err != nil {
				return Frame{}, errFrame
			}
		case TypeCall:
			if err := requireExactOrOptionalKeys(fields, append(append([]string{}, identityKeys...), "seq", "for"), "directive"); err != nil {
				return Frame{}, errFrame
			}
		case TypeReach:
			if err := requireExactKeys(fields, append(append([]string{}, identityKeys...), "seq", "for", "action")...); err != nil {
				return Frame{}, errFrame
			}
		case TypeSeal:
			if err := requireExactKeys(fields, append(append([]string{}, identityKeys...), "for", "expected_last_seq")...); err != nil {
				return Frame{}, errFrame
			}
		default:
			return Frame{}, errFrame
		}
	default:
		return Frame{}, errFrame
	}

	runID, err := decodeStringField(fields, "run_id")
	if err != nil {
		return Frame{}, errFrame
	}
	epoch, err := decodeStringField(fields, "observer_epoch")
	if err != nil {
		return Frame{}, errFrame
	}
	generation, err := decodeUintField(fields, "generation")
	if err != nil {
		return Frame{}, errFrame
	}
	eventID, err := decodeStringField(fields, "event_id")
	if err != nil {
		return Frame{}, errFrame
	}
	frame := Frame{
		Version:    Version,
		Type:       frameType,
		RunID:      runID,
		Epoch:      epoch,
		Generation: generation,
		EventID:    eventID,
	}

	switch frameType {
	case TypeBegin:
	case TypeCall:
		seq, err := decodeUintField(fields, "seq")
		if err != nil {
			return Frame{}, errFrame
		}
		kindValue, err := decodeStringField(fields, "kind")
		if err != nil {
			return Frame{}, errFrame
		}
		orderID, err := decodeStringField(fields, "order_id")
		if err != nil {
			return Frame{}, errFrame
		}
		frame.Seq = seq
		frame.Kind = Kind(kindValue)
		frame.OrderID = orderID
	case TypeReach:
		seq, err := decodeUintField(fields, "seq")
		if err != nil {
			return Frame{}, errFrame
		}
		pointValue, err := decodeStringField(fields, "point")
		if err != nil {
			return Frame{}, errFrame
		}
		orderID, err := decodeStringField(fields, "order_id")
		if err != nil {
			return Frame{}, errFrame
		}
		frame.Seq = seq
		frame.Point = Point(pointValue)
		frame.OrderID = orderID
		rawCallSeq, hasCallSeq := fields["payment_call_seq"]
		rawApplied, hasApplied := fields["applied_result"]
		if hasCallSeq != hasApplied {
			return Frame{}, errFrame
		}
		if hasCallSeq {
			callSeq, err := decodeUintRaw(rawCallSeq)
			if err != nil {
				return Frame{}, errFrame
			}
			applied, err := decodeDirectiveRaw(rawApplied)
			if err != nil {
				return Frame{}, errFrame
			}
			frame.PaymentCallSeq = &callSeq
			frame.AppliedResult = &applied
		}
	case TypeSeal:
		expected, err := decodeUintField(fields, "expected_last_seq")
		if err != nil {
			return Frame{}, errFrame
		}
		frame.ExpectedLastSeq = &expected
	case TypeAck:
		forValue, _ := decodeStringField(fields, "for")
		frame.For = Type(forValue)
		switch frame.For {
		case TypeBegin:
		case TypeCall:
			seq, err := decodeUintField(fields, "seq")
			if err != nil {
				return Frame{}, errFrame
			}
			frame.Seq = seq
			if raw, ok := fields["directive"]; ok {
				directive, err := decodeDirectiveRaw(raw)
				if err != nil {
					return Frame{}, errFrame
				}
				frame.Directive = &directive
			}
		case TypeReach:
			seq, err := decodeUintField(fields, "seq")
			if err != nil {
				return Frame{}, errFrame
			}
			actionValue, err := decodeStringField(fields, "action")
			if err != nil {
				return Frame{}, errFrame
			}
			frame.Seq = seq
			frame.Action = Action(actionValue)
		case TypeSeal:
			expected, err := decodeUintField(fields, "expected_last_seq")
			if err != nil {
				return Frame{}, errFrame
			}
			frame.ExpectedLastSeq = &expected
		}
	}

	if err := validateFrame(frame); err != nil {
		return Frame{}, errFrame
	}
	return frame, nil
}

func validateFrame(frame Frame) error {
	if frame.Version != Version {
		return errFrame
	}
	if frame.Type == TypeFault {
		if !validFaultCode(frame.Code) {
			return errFrame
		}
		if frame.RunID != "" || frame.Epoch != "" || frame.Generation != 0 ||
			frame.Seq != 0 || frame.EventID != "" || frame.Kind != "" ||
			frame.Point != "" || frame.OrderID != "" || frame.ExpectedLastSeq != nil ||
			frame.For != "" || frame.Action != "" || frame.Directive != nil ||
			frame.PaymentCallSeq != nil || frame.AppliedResult != nil {
			return errFrame
		}
		return nil
	}

	switch frame.Type {
	case TypeBegin, TypeCall, TypeReach, TypeSeal, TypeAck:
	default:
		return errFrame
	}
	if !validLowerHex32(frame.RunID) || !validLowerHex32(frame.Epoch) || frame.Generation == 0 {
		return errFrame
	}
	if !utf8.ValidString(frame.RunID) || !utf8.ValidString(frame.Epoch) ||
		!utf8.ValidString(frame.EventID) || !utf8.ValidString(frame.OrderID) {
		return errFrame
	}

	switch frame.Type {
	case TypeBegin:
		if !validLowerHex32(frame.EventID) {
			return errFrame
		}
		if frame.Seq != 0 || frame.Kind != "" || frame.Point != "" || frame.OrderID != "" ||
			frame.ExpectedLastSeq != nil || frame.For != "" || frame.Action != "" || frame.Code != "" ||
			frame.Directive != nil || frame.PaymentCallSeq != nil || frame.AppliedResult != nil {
			return errFrame
		}
	case TypeCall:
		if frame.Seq < 1 || frame.Seq > MaxRecords || !validKind(frame.Kind) ||
			!validUUIDv4(frame.OrderID) || !validCallEventID(frame.EventID, frame.Seq) {
			return errFrame
		}
		if frame.Point != "" || frame.ExpectedLastSeq != nil || frame.For != "" ||
			frame.Action != "" || frame.Code != "" || frame.Directive != nil ||
			frame.PaymentCallSeq != nil || frame.AppliedResult != nil {
			return errFrame
		}
	case TypeReach:
		if frame.Seq < 1 || frame.Seq > MaxRecords || !validPoint(frame.Point) ||
			!validUUIDv4(frame.OrderID) || !validCallEventID(frame.EventID, frame.Seq) {
			return errFrame
		}
		if frame.Kind != "" || frame.ExpectedLastSeq != nil || frame.For != "" ||
			frame.Action != "" || frame.Code != "" || frame.Directive != nil {
			return errFrame
		}
		if (frame.PaymentCallSeq == nil) != (frame.AppliedResult == nil) {
			return errFrame
		}
		if frame.PaymentCallSeq != nil {
			if frame.Point != PointMockCommitted || *frame.PaymentCallSeq < 1 ||
				*frame.PaymentCallSeq >= frame.Seq || frame.Seq > MaxRecords ||
				!validDirective(*frame.AppliedResult) {
				return errFrame
			}
		}
	case TypeSeal:
		if frame.ExpectedLastSeq == nil || *frame.ExpectedLastSeq > MaxRecords ||
			!validSealEventID(frame.EventID) {
			return errFrame
		}
		if frame.Seq != 0 || frame.Kind != "" || frame.Point != "" || frame.OrderID != "" ||
			frame.For != "" || frame.Action != "" || frame.Code != "" ||
			frame.Directive != nil || frame.PaymentCallSeq != nil || frame.AppliedResult != nil {
			return errFrame
		}
	case TypeAck:
		if !validAckTarget(frame.For) {
			return errFrame
		}
		switch frame.For {
		case TypeBegin:
			if !validLowerHex32(frame.EventID) {
				return errFrame
			}
			if frame.Seq != 0 || frame.Action != "" || frame.ExpectedLastSeq != nil ||
				frame.Kind != "" || frame.Point != "" || frame.OrderID != "" || frame.Code != "" ||
				frame.Directive != nil || frame.PaymentCallSeq != nil || frame.AppliedResult != nil {
				return errFrame
			}
		case TypeCall:
			if frame.Seq < 1 || frame.Seq > MaxRecords || !validCallEventID(frame.EventID, frame.Seq) {
				return errFrame
			}
			if frame.Action != "" || frame.ExpectedLastSeq != nil || frame.Kind != "" ||
				frame.Point != "" || frame.OrderID != "" || frame.Code != "" ||
				frame.PaymentCallSeq != nil || frame.AppliedResult != nil {
				return errFrame
			}
			if frame.Directive != nil && !validDirective(*frame.Directive) {
				return errFrame
			}
		case TypeReach:
			if frame.Seq < 1 || frame.Seq > MaxRecords || !validCallEventID(frame.EventID, frame.Seq) ||
				!validAction(frame.Action) {
				return errFrame
			}
			if frame.ExpectedLastSeq != nil || frame.Kind != "" || frame.Point != "" ||
				frame.OrderID != "" || frame.Code != "" || frame.Directive != nil ||
				frame.PaymentCallSeq != nil || frame.AppliedResult != nil {
				return errFrame
			}
		case TypeSeal:
			if frame.ExpectedLastSeq == nil || *frame.ExpectedLastSeq > MaxRecords ||
				!validSealEventID(frame.EventID) {
				return errFrame
			}
			if frame.Seq != 0 || frame.Action != "" || frame.Kind != "" || frame.Point != "" ||
				frame.OrderID != "" || frame.Code != "" || frame.Directive != nil ||
				frame.PaymentCallSeq != nil || frame.AppliedResult != nil {
				return errFrame
			}
		}
	}
	return nil
}

func requireExactKeys(fields map[string]json.RawMessage, keys ...string) error {
	if len(fields) != len(keys) {
		return errFrame
	}
	for _, key := range keys {
		if _, ok := fields[key]; !ok {
			return errFrame
		}
	}
	return nil
}

func requireExactOrOptionalKeys(fields map[string]json.RawMessage, required []string, optional ...string) error {
	allowed := make(map[string]struct{}, len(required)+len(optional))
	for _, key := range required {
		allowed[key] = struct{}{}
	}
	for _, key := range optional {
		allowed[key] = struct{}{}
	}
	for key := range fields {
		if _, ok := allowed[key]; !ok {
			return errFrame
		}
	}
	for _, key := range required {
		if _, ok := fields[key]; !ok {
			return errFrame
		}
	}
	return nil
}

func decodeStringField(fields map[string]json.RawMessage, key string) (string, error) {
	raw, ok := fields[key]
	if !ok {
		return "", errFrame
	}
	var value *string
	if err := json.Unmarshal(raw, &value); err != nil || value == nil {
		return "", errFrame
	}
	if !utf8.ValidString(*value) {
		return "", errFrame
	}
	return *value, nil
}

func decodeDirectiveRaw(raw json.RawMessage) (Directive, error) {
	var value *string
	if err := json.Unmarshal(raw, &value); err != nil || value == nil {
		return "", errFrame
	}
	if !utf8.ValidString(*value) {
		return "", errFrame
	}
	directive := Directive(*value)
	if !validDirective(directive) {
		return "", errFrame
	}
	return directive, nil
}

func decodeUintField(fields map[string]json.RawMessage, key string) (uint64, error) {
	raw, ok := fields[key]
	if !ok {
		return 0, errFrame
	}
	return decodeUintRaw(raw)
}

func decodeUintRaw(raw json.RawMessage) (uint64, error) {
	number, err := decodeNumberRaw(raw)
	if err != nil {
		return 0, errFrame
	}
	value, err := strconv.ParseUint(number.String(), 10, 64)
	if err != nil {
		return 0, errFrame
	}
	return value, nil
}

func decodeIntField(fields map[string]json.RawMessage, key string) (int, error) {
	raw, ok := fields[key]
	if !ok {
		return 0, errFrame
	}
	number, err := decodeNumberRaw(raw)
	if err != nil {
		return 0, errFrame
	}
	value, err := strconv.ParseInt(number.String(), 10, 64)
	if err != nil {
		return 0, errFrame
	}
	return int(value), nil
}

func decodeNumberRaw(raw json.RawMessage) (json.Number, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return "", errFrame
	}
	number, ok := value.(json.Number)
	if !ok {
		return "", errFrame
	}
	return number, nil
}

func validKind(value Kind) bool {
	return value == KindPayment || value == KindQuery
}

func validPoint(value Point) bool {
	switch value {
	case PointRegistered, PointMockCommitted, PointSavingResult, PointResultCommitted:
		return true
	default:
		return false
	}
}

func validAction(value Action) bool {
	return value == ActionContinue || value == ActionReplyHidden
}

func validDirective(value Directive) bool {
	return value == DirectiveSucceeded || value == DirectiveFailed
}

func validFaultCode(value FaultCode) bool {
	switch value {
	case FaultProtocol, FaultIncomplete, FaultTimeout, FaultClosed:
		return true
	default:
		return false
	}
}

func validAckTarget(value Type) bool {
	switch value {
	case TypeBegin, TypeCall, TypeReach, TypeSeal:
		return true
	default:
		return false
	}
}

func validLowerHex32(value string) bool {
	if len(value) != 32 {
		return false
	}
	for i := 0; i < len(value); i++ {
		c := value[i]
		if (c < '0' || c > '9') && (c < 'a' || c > 'f') {
			return false
		}
	}
	return true
}

func validCallEventID(eventID string, seq uint64) bool {
	index := strings.LastIndexByte(eventID, ':')
	if index != 32 || seq < 1 || seq > MaxRecords {
		return false
	}
	return validLowerHex32(eventID[:index]) && eventID[index+1:] == strconv.FormatUint(seq, 10)
}

func validSealEventID(eventID string) bool {
	index := strings.LastIndexByte(eventID, ':')
	if index != 32 {
		return false
	}
	return validLowerHex32(eventID[:index]) && eventID[index+1:] == "seal"
}

func validUUIDv4(value string) bool {
	if len(value) != 36 {
		return false
	}
	for i := 0; i < len(value); i++ {
		switch i {
		case 8, 13, 18, 23:
			if value[i] != '-' {
				return false
			}
		default:
			if !isHexDigit(value[i]) {
				return false
			}
		}
	}
	if value[14] != '4' {
		return false
	}
	switch value[19] {
	case '8', '9', 'a', 'b', 'A', 'B':
		return true
	default:
		return false
	}
}

func isHexDigit(c byte) bool {
	return c >= '0' && c <= '9' || c >= 'a' && c <= 'f' || c >= 'A' && c <= 'F'
}
