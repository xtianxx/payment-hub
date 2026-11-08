package testcontrol

import (
	"bytes"
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"
)

const (
	protocolTestRunID   = "0123456789abcdef0123456789abcdef"
	protocolTestEpoch   = "fedcba9876543210fedcba9876543210"
	protocolTestBeginID = "00112233445566778899aabbccddeeff"
	protocolTestOrderID = "123e4567-e89b-42d3-a456-426614174000"
)

const (
	protocolTestBeginJSON = `{"v":1,"type":"begin","run_id":"` + protocolTestRunID +
		`","observer_epoch":"` + protocolTestEpoch +
		`","generation":1,"event_id":"` + protocolTestBeginID + `"}`

	protocolTestCallJSON = `{"v":1,"type":"call","run_id":"` + protocolTestRunID +
		`","observer_epoch":"` + protocolTestEpoch +
		`","generation":1,"seq":1,"event_id":"` + protocolTestBeginID +
		`:1","kind":"PAYMENT","order_id":"` + protocolTestOrderID + `"}`

	protocolTestReachJSON = `{"v":1,"type":"reach","run_id":"` + protocolTestRunID +
		`","observer_epoch":"` + protocolTestEpoch +
		`","generation":1,"seq":2,"event_id":"` + protocolTestBeginID +
		`:2","point":"REGISTERED","order_id":"` + protocolTestOrderID + `"}`

	protocolTestSealJSON = `{"v":1,"type":"seal","run_id":"` + protocolTestRunID +
		`","observer_epoch":"` + protocolTestEpoch +
		`","generation":1,"event_id":"` + protocolTestBeginID +
		`:seal","expected_last_seq":0}`

	protocolTestAckBeginJSON = `{"v":1,"type":"ack","run_id":"` + protocolTestRunID +
		`","observer_epoch":"` + protocolTestEpoch +
		`","generation":1,"event_id":"` + protocolTestBeginID + `","for":"begin"}`

	protocolTestAckCallJSON = `{"v":1,"type":"ack","run_id":"` + protocolTestRunID +
		`","observer_epoch":"` + protocolTestEpoch +
		`","generation":1,"seq":2,"event_id":"` + protocolTestBeginID +
		`:2","for":"call"}`

	protocolTestAckReachJSON = `{"v":1,"type":"ack","run_id":"` + protocolTestRunID +
		`","observer_epoch":"` + protocolTestEpoch +
		`","generation":1,"seq":2,"event_id":"` + protocolTestBeginID +
		`:2","for":"reach","action":"continue"}`

	protocolTestAckSealJSON = `{"v":1,"type":"ack","run_id":"` + protocolTestRunID +
		`","observer_epoch":"` + protocolTestEpoch +
		`","generation":1,"event_id":"` + protocolTestBeginID +
		`:seal","for":"seal","expected_last_seq":0}`

	protocolTestFaultJSON = `{"v":1,"type":"fault","code":"protocol"}`
)

type protocolValidCase struct {
	name  string
	frame Frame
	keys  []string
}

type protocolRejectCase struct {
	name  string
	input []byte
}

type protocolEncodeRejectCase struct {
	name  string
	frame Frame
}

func protocolTestLine(body string) []byte {
	return []byte(body + "\n")
}

func protocolTestReplace(body, old, replacement string) []byte {
	return protocolTestLine(strings.Replace(body, old, replacement, 1))
}

func protocolTestAdd(body, field string) []byte {
	return protocolTestLine(strings.TrimSuffix(body, "}") + "," + field + "}")
}

func protocolTestIdentity(typ Type, eventID string) Frame {
	return Frame{
		Version:    Version,
		Type:       typ,
		RunID:      protocolTestRunID,
		Epoch:      protocolTestEpoch,
		Generation: 1,
		EventID:    eventID,
	}
}

func protocolTestMutate(base Frame, mutate func(*Frame)) Frame {
	frame := base
	mutate(&frame)
	return frame
}

func TestProtocolFrozenConstants(t *testing.T) {
	if Version != 1 {
		t.Fatal("frozen protocol version changed")
	}
	if MaxFrameBytes != 4096 {
		t.Fatal("frozen maximum frame size changed")
	}
	if MaxRecords != 4096 {
		t.Fatal("frozen maximum record count changed")
	}
	if MaxGenerations != 64 {
		t.Fatal("frozen maximum generation count changed")
	}

	if TypeBegin != "begin" || TypeCall != "call" || TypeReach != "reach" ||
		TypeSeal != "seal" || TypeAck != "ack" || TypeFault != "fault" {
		t.Fatal("frozen frame type value changed")
	}
	if KindPayment != "PAYMENT" || KindQuery != "QUERY" {
		t.Fatal("frozen kind value changed")
	}
	if PointRegistered != "REGISTERED" || PointMockCommitted != "MOCK_COMMITTED" ||
		PointSavingResult != "SAVING_RESULT" || PointResultCommitted != "RESULT_COMMITTED" {
		t.Fatal("frozen point value changed")
	}
	if ActionContinue != "continue" || ActionReplyHidden != "reply_hidden" {
		t.Fatal("frozen action value changed")
	}
	if FaultProtocol != "protocol" || FaultIncomplete != "incomplete" ||
		FaultTimeout != "timeout" || FaultClosed != "closed" {
		t.Fatal("frozen fault code value changed")
	}
}

func TestProtocolSentinels(t *testing.T) {
	sentinels := []error{ErrProtocol, ErrControlFault, ErrReplyHidden, ErrUncertain}
	for _, sentinel := range sentinels {
		if sentinel == nil {
			t.Fatal("protocol sentinel is nil")
		}
	}
	for i := 0; i < len(sentinels); i++ {
		for j := i + 1; j < len(sentinels); j++ {
			if errors.Is(sentinels[i], sentinels[j]) {
				t.Fatal("protocol sentinels are not distinct")
			}
		}
	}
}

func TestProtocolRoundTripValidFrames(t *testing.T) {
	sealZero := uint64(0)
	sealThree := uint64(3)

	begin := protocolTestIdentity(TypeBegin, protocolTestBeginID)

	callPayment := protocolTestIdentity(TypeCall, protocolTestBeginID+":1")
	callPayment.Seq = 1
	callPayment.Kind = KindPayment
	callPayment.OrderID = protocolTestOrderID

	callQueryMax := protocolTestIdentity(TypeCall, protocolTestBeginID+":4096")
	callQueryMax.Seq = MaxRecords
	callQueryMax.Kind = KindQuery
	callQueryMax.OrderID = protocolTestOrderID

	reach := protocolTestIdentity(TypeReach, protocolTestBeginID+":2")
	reach.Seq = 2
	reach.Point = PointRegistered
	reach.OrderID = protocolTestOrderID

	reachSaving := protocolTestIdentity(TypeReach, protocolTestBeginID+":3")
	reachSaving.Seq = 3
	reachSaving.Point = PointSavingResult
	reachSaving.OrderID = protocolTestOrderID

	sealEmpty := protocolTestIdentity(TypeSeal, protocolTestBeginID+":seal")
	sealEmpty.ExpectedLastSeq = &sealZero

	sealFull := protocolTestIdentity(TypeSeal, protocolTestBeginID+":seal")
	sealFull.ExpectedLastSeq = &sealThree

	ackBegin := protocolTestIdentity(TypeAck, protocolTestBeginID)
	ackBegin.For = TypeBegin

	ackCall := protocolTestIdentity(TypeAck, protocolTestBeginID+":2")
	ackCall.Seq = 2
	ackCall.For = TypeCall

	ackReach := protocolTestIdentity(TypeAck, protocolTestBeginID+":2")
	ackReach.Seq = 2
	ackReach.For = TypeReach
	ackReach.Action = ActionContinue

	ackSeal := protocolTestIdentity(TypeAck, protocolTestBeginID+":seal")
	ackSeal.For = TypeSeal
	ackSeal.ExpectedLastSeq = &sealThree

	cases := []protocolValidCase{
		{name: "begin", frame: begin, keys: []string{"v", "type", "run_id", "observer_epoch", "generation", "event_id"}},
		{name: "call_payment", frame: callPayment, keys: []string{"v", "type", "run_id", "observer_epoch", "generation", "seq", "event_id", "kind", "order_id"}},
		{name: "call_query_max_seq", frame: callQueryMax, keys: []string{"v", "type", "run_id", "observer_epoch", "generation", "seq", "event_id", "kind", "order_id"}},
		{name: "reach_registered", frame: reach, keys: []string{"v", "type", "run_id", "observer_epoch", "generation", "seq", "event_id", "point", "order_id"}},
		{name: "reach_saving_result", frame: reachSaving, keys: []string{"v", "type", "run_id", "observer_epoch", "generation", "seq", "event_id", "point", "order_id"}},
		{name: "seal_empty", frame: sealEmpty, keys: []string{"v", "type", "run_id", "observer_epoch", "generation", "event_id", "expected_last_seq"}},
		{name: "seal_full", frame: sealFull, keys: []string{"v", "type", "run_id", "observer_epoch", "generation", "event_id", "expected_last_seq"}},
		{name: "ack_begin", frame: ackBegin, keys: []string{"v", "type", "run_id", "observer_epoch", "generation", "event_id", "for"}},
		{name: "ack_call", frame: ackCall, keys: []string{"v", "type", "run_id", "observer_epoch", "generation", "seq", "event_id", "for"}},
		{name: "ack_reach", frame: ackReach, keys: []string{"v", "type", "run_id", "observer_epoch", "generation", "seq", "event_id", "for", "action"}},
		{name: "ack_seal", frame: ackSeal, keys: []string{"v", "type", "run_id", "observer_epoch", "generation", "event_id", "for", "expected_last_seq"}},
	}
	for _, code := range []FaultCode{FaultProtocol, FaultIncomplete, FaultTimeout, FaultClosed} {
		cases = append(cases, protocolValidCase{
			name:  "fault_" + string(code),
			frame: Frame{Version: Version, Type: TypeFault, Code: code},
			keys:  []string{"v", "type", "code"},
		})
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			encoded, err := EncodeFrame(tc.frame)
			if err != nil {
				t.Fatal("valid frame failed encoding")
			}
			if len(encoded) == 0 || len(encoded) > MaxFrameBytes {
				t.Fatal("encoded frame size is outside the frozen limit")
			}
			if encoded[len(encoded)-1] != '\n' {
				t.Fatal("encoded frame lacks the unique trailing newline")
			}
			if bytes.Count(encoded, []byte{'\n'}) != 1 {
				t.Fatal("encoded frame contains extra newlines")
			}

			var raw map[string]json.RawMessage
			if err := json.Unmarshal(encoded[:len(encoded)-1], &raw); err != nil {
				t.Fatal("encoded frame is not one JSON object")
			}
			if len(raw) != len(tc.keys) {
				t.Fatal("encoded frame key set mismatch")
			}
			for _, key := range tc.keys {
				if _, ok := raw[key]; !ok {
					t.Fatal("encoded frame is missing a required key")
				}
			}

			decoded, err := DecodeFrame(encoded)
			if err != nil {
				t.Fatal("valid frame failed decoding")
			}
			if !reflect.DeepEqual(decoded, tc.frame) {
				t.Fatal("valid frame did not round trip")
			}

			reencoded, err := EncodeFrame(decoded)
			if err != nil {
				t.Fatal("decoded frame failed re-encoding")
			}
			if !bytes.Equal(reencoded, encoded) {
				t.Fatal("frame encoding is not stable")
			}
		})
	}
}

func TestProtocolFrameSizeBoundary(t *testing.T) {
	base := protocolTestFaultJSON
	padding := MaxFrameBytes - 1 - len(base)
	if padding <= 0 {
		t.Fatal("frame size boundary fixture is invalid")
	}

	atLimitBody := "{" + strings.Repeat(" ", padding) + strings.TrimPrefix(base, "{")
	atLimit := []byte(atLimitBody + "\n")
	if len(atLimit) != MaxFrameBytes {
		t.Fatal("frame size boundary fixture is not exact")
	}
	frame, err := DecodeFrame(atLimit)
	if err != nil {
		t.Fatal("frame at the size limit was rejected")
	}
	if frame.Type != TypeFault || frame.Code != FaultProtocol {
		t.Fatal("frame at the size limit decoded incorrectly")
	}

	overLimitBody := "{" + strings.Repeat(" ", padding+1) + strings.TrimPrefix(base, "{")
	overLimit := []byte(overLimitBody + "\n")
	if len(overLimit) != MaxFrameBytes+1 {
		t.Fatal("oversized frame fixture is not exact")
	}
	got, err := DecodeFrame(overLimit)
	if err == nil {
		t.Fatal("frame above the size limit was accepted")
	}
	if !errors.Is(err, ErrProtocol) {
		t.Fatal("oversized frame rejection is not ErrProtocol")
	}
	if got != (Frame{}) {
		t.Fatal("oversized frame rejection returned a nonzero frame")
	}
}

func TestProtocolDecodeRejections(t *testing.T) {
	oversized := []byte("{" + strings.Repeat(" ", MaxFrameBytes) +
		strings.TrimPrefix(protocolTestBeginJSON, "{") + "\n")

	cases := []protocolRejectCase{
		{name: "empty_input", input: nil},
		{name: "only_newline", input: []byte("\n")},
		{name: "missing_trailing_newline", input: []byte(protocolTestBeginJSON)},
		{name: "two_trailing_newlines", input: []byte(protocolTestBeginJSON + "\n\n")},
		{name: "trailing_space_after_newline", input: []byte(protocolTestBeginJSON + "\n ")},
		{name: "extra_object_line", input: []byte(protocolTestBeginJSON + "\n{}\n")},
		{name: "two_objects_one_line", input: []byte(protocolTestBeginJSON + protocolTestBeginJSON + "\n")},
		{name: "trailing_garbage", input: []byte(protocolTestBeginJSON + "x\n")},
		{name: "invalid_utf8", input: protocolTestReplace(protocolTestBeginJSON, protocolTestRunID, protocolTestRunID[:31]+string([]byte{0xff}))},
		{name: "oversized_frame", input: oversized},
		{name: "unknown_key", input: protocolTestAdd(protocolTestBeginJSON, `"extra":1`)},
		{name: "duplicate_v", input: protocolTestAdd(protocolTestBeginJSON, `"v":1`)},
		{name: "duplicate_run_id", input: protocolTestAdd(protocolTestBeginJSON, `"run_id":"`+protocolTestRunID+`"`)},
		{name: "duplicate_event_id", input: protocolTestAdd(protocolTestBeginJSON, `"event_id":"`+protocolTestBeginID+`"`)},
		{name: "duplicate_expected_last_seq", input: protocolTestAdd(protocolTestSealJSON, `"expected_last_seq":0`)},

		{name: "version_zero", input: protocolTestReplace(protocolTestBeginJSON, `"v":1`, `"v":0`)},
		{name: "version_two", input: protocolTestReplace(protocolTestBeginJSON, `"v":1`, `"v":2`)},
		{name: "version_negative", input: protocolTestReplace(protocolTestBeginJSON, `"v":1`, `"v":-1`)},
		{name: "version_float", input: protocolTestReplace(protocolTestBeginJSON, `"v":1`, `"v":1.0`)},
		{name: "version_string", input: protocolTestReplace(protocolTestBeginJSON, `"v":1`, `"v":"1"`)},
		{name: "version_null", input: protocolTestReplace(protocolTestBeginJSON, `"v":1`, `"v":null`)},
		{name: "type_unknown", input: protocolTestReplace(protocolTestBeginJSON, `"type":"begin"`, `"type":"open"`)},
		{name: "type_null", input: protocolTestReplace(protocolTestBeginJSON, `"type":"begin"`, `"type":null`)},
		{name: "type_missing", input: protocolTestLine(strings.Replace(protocolTestBeginJSON, `"type":"begin",`, "", 1))},

		{name: "generation_zero", input: protocolTestReplace(protocolTestBeginJSON, `"generation":1`, `"generation":0`)},
		{name: "generation_negative", input: protocolTestReplace(protocolTestBeginJSON, `"generation":1`, `"generation":-1`)},
		{name: "generation_float", input: protocolTestReplace(protocolTestBeginJSON, `"generation":1`, `"generation":1.0`)},
		{name: "generation_exponent", input: protocolTestReplace(protocolTestBeginJSON, `"generation":1`, `"generation":1e0`)},
		{name: "generation_string", input: protocolTestReplace(protocolTestBeginJSON, `"generation":1`, `"generation":"1"`)},
		{name: "generation_null", input: protocolTestReplace(protocolTestBeginJSON, `"generation":1`, `"generation":null`)},
		{name: "generation_overflow", input: protocolTestReplace(protocolTestBeginJSON, `"generation":1`, `"generation":18446744073709551616`)},

		{name: "run_id_short", input: protocolTestReplace(protocolTestBeginJSON, protocolTestRunID, protocolTestRunID[:31])},
		{name: "run_id_uppercase", input: protocolTestReplace(protocolTestBeginJSON, protocolTestRunID, strings.ToUpper(protocolTestRunID))},
		{name: "run_id_nonhex", input: protocolTestReplace(protocolTestBeginJSON, protocolTestRunID, "g123456789abcdef0123456789abcdef")},
		{name: "run_id_null", input: protocolTestReplace(protocolTestBeginJSON, `"run_id":"`+protocolTestRunID+`"`, `"run_id":null`)},
		{name: "run_id_missing", input: protocolTestLine(strings.Replace(protocolTestBeginJSON, `"run_id":"`+protocolTestRunID+`",`, "", 1))},
		{name: "epoch_short", input: protocolTestReplace(protocolTestBeginJSON, protocolTestEpoch, protocolTestEpoch[:31])},
		{name: "epoch_uppercase", input: protocolTestReplace(protocolTestBeginJSON, protocolTestEpoch, strings.ToUpper(protocolTestEpoch))},
		{name: "epoch_null", input: protocolTestReplace(protocolTestBeginJSON, `"observer_epoch":"`+protocolTestEpoch+`"`, `"observer_epoch":null`)},
		{name: "epoch_missing", input: protocolTestLine(strings.Replace(protocolTestBeginJSON, `,"observer_epoch":"`+protocolTestEpoch+`"`, "", 1))},

		{name: "begin_event_id_call_form", input: protocolTestReplace(protocolTestBeginJSON, protocolTestBeginID, protocolTestBeginID+":1")},
		{name: "begin_event_id_short", input: protocolTestReplace(protocolTestBeginJSON, protocolTestBeginID, protocolTestBeginID[:31])},
		{name: "begin_event_id_uppercase", input: protocolTestReplace(protocolTestBeginJSON, protocolTestBeginID, strings.ToUpper(protocolTestBeginID))},
		{name: "begin_event_id_missing", input: protocolTestLine(strings.Replace(protocolTestBeginJSON, `,"event_id":"`+protocolTestBeginID+`"`, "", 1))},
		{name: "begin_explicit_seq_zero", input: protocolTestAdd(protocolTestBeginJSON, `"seq":0`)},
		{name: "begin_explicit_kind", input: protocolTestAdd(protocolTestBeginJSON, `"kind":"PAYMENT"`)},
		{name: "begin_explicit_point", input: protocolTestAdd(protocolTestBeginJSON, `"point":"REGISTERED"`)},
		{name: "begin_explicit_expected", input: protocolTestAdd(protocolTestBeginJSON, `"expected_last_seq":0`)},
		{name: "begin_explicit_for", input: protocolTestAdd(protocolTestBeginJSON, `"for":"begin"`)},
		{name: "begin_explicit_action", input: protocolTestAdd(protocolTestBeginJSON, `"action":"continue"`)},
		{name: "begin_explicit_code", input: protocolTestAdd(protocolTestBeginJSON, `"code":"protocol"`)},

		{name: "call_kind_missing", input: protocolTestLine(strings.Replace(protocolTestCallJSON, `"kind":"PAYMENT",`, "", 1))},
		{name: "call_kind_lowercase", input: protocolTestReplace(protocolTestCallJSON, `"kind":"PAYMENT"`, `"kind":"payment"`)},
		{name: "call_kind_unknown", input: protocolTestReplace(protocolTestCallJSON, `"kind":"PAYMENT"`, `"kind":"OTHER"`)},
		{name: "call_kind_null", input: protocolTestReplace(protocolTestCallJSON, `"kind":"PAYMENT"`, `"kind":null`)},
		{name: "call_order_missing", input: protocolTestLine(strings.Replace(protocolTestCallJSON, `,"order_id":"`+protocolTestOrderID+`"`, "", 1))},
		{name: "call_order_null", input: protocolTestReplace(protocolTestCallJSON, `"order_id":"`+protocolTestOrderID+`"`, `"order_id":null`)},
		{name: "call_order_short", input: protocolTestReplace(protocolTestCallJSON, protocolTestOrderID, "not-a-uuid")},
		{name: "call_order_version_one", input: protocolTestReplace(protocolTestCallJSON, "42d3", "12d3")},
		{name: "call_order_variant_invalid", input: protocolTestReplace(protocolTestCallJSON, "a456", "c456")},
		{name: "call_seq_zero", input: protocolTestReplace(protocolTestCallJSON, `"seq":1`, `"seq":0`)},
		{name: "call_seq_negative", input: protocolTestReplace(protocolTestCallJSON, `"seq":1`, `"seq":-1`)},
		{name: "call_seq_float", input: protocolTestReplace(protocolTestCallJSON, `"seq":1`, `"seq":1.0`)},
		{name: "call_seq_string", input: protocolTestReplace(protocolTestCallJSON, `"seq":1`, `"seq":"1"`)},
		{name: "call_seq_null", input: protocolTestReplace(protocolTestCallJSON, `"seq":1`, `"seq":null`)},
		{name: "call_seq_above_max", input: protocolTestReplace(protocolTestCallJSON, `"seq":1`, `"seq":4097`)},
		{name: "call_seq_overflow", input: protocolTestReplace(protocolTestCallJSON, `"seq":1`, `"seq":18446744073709551616`)},
		{name: "call_event_leading_zero", input: protocolTestReplace(protocolTestCallJSON, `:1"`, `:01"`)},
		{name: "call_event_zero", input: protocolTestReplace(protocolTestCallJSON, `:1"`, `:0"`)},
		{name: "call_event_above_max", input: protocolTestReplace(protocolTestCallJSON, `:1"`, `:4097"`)},
		{name: "call_event_seal", input: protocolTestReplace(protocolTestCallJSON, `:1"`, `:seal"`)},
		{name: "call_event_no_seq", input: protocolTestReplace(protocolTestCallJSON, `:1"`, `"`)},
		{name: "call_event_mismatch", input: protocolTestReplace(protocolTestCallJSON, `:1"`, `:2"`)},
		{name: "call_explicit_point", input: protocolTestAdd(protocolTestCallJSON, `"point":"REGISTERED"`)},
		{name: "call_explicit_action", input: protocolTestAdd(protocolTestCallJSON, `"action":"continue"`)},
		{name: "call_explicit_expected", input: protocolTestAdd(protocolTestCallJSON, `"expected_last_seq":0`)},

		{name: "reach_point_missing", input: protocolTestLine(strings.Replace(protocolTestReachJSON, `"point":"REGISTERED",`, "", 1))},
		{name: "reach_point_lowercase", input: protocolTestReplace(protocolTestReachJSON, `"point":"REGISTERED"`, `"point":"registered"`)},
		{name: "reach_point_unknown", input: protocolTestReplace(protocolTestReachJSON, `"point":"REGISTERED"`, `"point":"COMMITTED"`)},
		{name: "reach_point_null", input: protocolTestReplace(protocolTestReachJSON, `"point":"REGISTERED"`, `"point":null`)},
		{name: "reach_order_missing", input: protocolTestLine(strings.Replace(protocolTestReachJSON, `,"order_id":"`+protocolTestOrderID+`"`, "", 1))},
		{name: "reach_explicit_kind", input: protocolTestAdd(protocolTestReachJSON, `"kind":"PAYMENT"`)},
		{name: "reach_seq_mismatch", input: protocolTestReplace(protocolTestReachJSON, `"seq":2`, `"seq":1`)},
		{name: "reach_event_mismatch", input: protocolTestReplace(protocolTestReachJSON, `:2"`, `:3"`)},

		{name: "seal_expected_missing", input: protocolTestLine(strings.Replace(protocolTestSealJSON, `,"expected_last_seq":0`, "", 1))},
		{name: "seal_expected_null", input: protocolTestReplace(protocolTestSealJSON, `"expected_last_seq":0`, `"expected_last_seq":null`)},
		{name: "seal_expected_negative", input: protocolTestReplace(protocolTestSealJSON, `"expected_last_seq":0`, `"expected_last_seq":-1`)},
		{name: "seal_expected_above_max", input: protocolTestReplace(protocolTestSealJSON, `"expected_last_seq":0`, `"expected_last_seq":4097`)},
		{name: "seal_expected_float", input: protocolTestReplace(protocolTestSealJSON, `"expected_last_seq":0`, `"expected_last_seq":0.0`)},
		{name: "seal_expected_string", input: protocolTestReplace(protocolTestSealJSON, `"expected_last_seq":0`, `"expected_last_seq":"0"`)},
		{name: "seal_explicit_seq_zero", input: protocolTestAdd(protocolTestSealJSON, `"seq":0`)},
		{name: "seal_event_call_form", input: protocolTestReplace(protocolTestSealJSON, `:seal"`, `:1"`)},
		{name: "seal_event_short", input: protocolTestReplace(protocolTestSealJSON, `:seal"`, `"`)},

		{name: "ack_for_missing", input: protocolTestLine(strings.Replace(protocolTestAckCallJSON, `,"for":"call"`, "", 1))},
		{name: "ack_for_unknown", input: protocolTestReplace(protocolTestAckCallJSON, `"for":"call"`, `"for":"fault"`)},
		{name: "ack_begin_with_seq", input: protocolTestAdd(protocolTestAckBeginJSON, `"seq":0`)},
		{name: "ack_call_seq_missing", input: protocolTestLine(strings.Replace(protocolTestAckCallJSON, `"seq":2,`, "", 1))},
		{name: "ack_call_seq_mismatch", input: protocolTestReplace(protocolTestAckCallJSON, `"seq":2`, `"seq":1`)},
		{name: "ack_call_with_kind", input: protocolTestAdd(protocolTestAckCallJSON, `"kind":"PAYMENT"`)},
		{name: "ack_call_with_order", input: protocolTestAdd(protocolTestAckCallJSON, `"order_id":"`+protocolTestOrderID+`"`)},
		{name: "ack_reach_action_missing", input: protocolTestLine(strings.Replace(protocolTestAckReachJSON, `,"action":"continue"`, "", 1))},
		{name: "ack_reach_action_unknown", input: protocolTestReplace(protocolTestAckReachJSON, `"action":"continue"`, `"action":"hidden"`)},
		{name: "ack_reach_seq_missing", input: protocolTestLine(strings.Replace(protocolTestAckReachJSON, `"seq":2,`, "", 1))},
		{name: "ack_seal_expected_missing", input: protocolTestLine(strings.Replace(protocolTestAckSealJSON, `,"expected_last_seq":0`, "", 1))},
		{name: "ack_seal_expected_above_max", input: protocolTestReplace(protocolTestAckSealJSON, `"expected_last_seq":0`, `"expected_last_seq":4097`)},
		{name: "ack_seal_with_seq_zero", input: protocolTestAdd(protocolTestAckSealJSON, `"seq":0`)},

		{name: "fault_with_run_id", input: protocolTestAdd(protocolTestFaultJSON, `"run_id":"`+protocolTestRunID+`"`)},
		{name: "fault_code_missing", input: protocolTestLine(strings.Replace(protocolTestFaultJSON, `,"code":"protocol"`, "", 1))},
		{name: "fault_code_unknown", input: protocolTestReplace(protocolTestFaultJSON, `"code":"protocol"`, `"code":"other"`)},
		{name: "fault_with_seq_zero", input: protocolTestAdd(protocolTestFaultJSON, `"seq":0`)},
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			frame, err := DecodeFrame(tc.input)
			if err == nil {
				t.Fatal("malformed frame was accepted")
			}
			if !errors.Is(err, ErrProtocol) {
				t.Fatal("decode rejection is not ErrProtocol")
			}
			if frame != (Frame{}) {
				t.Fatal("decode rejection returned a nonzero frame")
			}
		})
	}
}

func TestProtocolEncodeRejections(t *testing.T) {
	sealZero := uint64(0)
	sealAboveMax := uint64(MaxRecords + 1)

	baseBegin := protocolTestIdentity(TypeBegin, protocolTestBeginID)

	baseCall := protocolTestIdentity(TypeCall, protocolTestBeginID+":1")
	baseCall.Seq = 1
	baseCall.Kind = KindPayment
	baseCall.OrderID = protocolTestOrderID

	baseReach := protocolTestIdentity(TypeReach, protocolTestBeginID+":2")
	baseReach.Seq = 2
	baseReach.Point = PointRegistered
	baseReach.OrderID = protocolTestOrderID

	baseSeal := protocolTestIdentity(TypeSeal, protocolTestBeginID+":seal")
	baseSeal.ExpectedLastSeq = &sealZero

	baseAckBegin := protocolTestIdentity(TypeAck, protocolTestBeginID)
	baseAckBegin.For = TypeBegin

	baseAckCall := protocolTestIdentity(TypeAck, protocolTestBeginID+":1")
	baseAckCall.Seq = 1
	baseAckCall.For = TypeCall

	baseAckReach := protocolTestIdentity(TypeAck, protocolTestBeginID+":2")
	baseAckReach.Seq = 2
	baseAckReach.For = TypeReach
	baseAckReach.Action = ActionContinue

	baseAckSeal := protocolTestIdentity(TypeAck, protocolTestBeginID+":seal")
	baseAckSeal.For = TypeSeal
	baseAckSeal.ExpectedLastSeq = &sealZero

	baseFault := Frame{Version: Version, Type: TypeFault, Code: FaultProtocol}

	badRunID := protocolTestRunID[:31] + string([]byte{0xff})

	cases := []protocolEncodeRejectCase{
		{name: "zero_frame", frame: Frame{}},
		{name: "version_zero", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.Version = 0 })},
		{name: "version_two", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.Version = 2 })},
		{name: "type_unknown", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.Type = Type("open") })},
		{name: "type_empty", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.Type = Type("") })},
		{name: "run_id_missing", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.RunID = "" })},
		{name: "run_id_short", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.RunID = protocolTestRunID[:31] })},
		{name: "run_id_uppercase", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.RunID = strings.ToUpper(protocolTestRunID) })},
		{name: "run_id_invalid_utf8", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.RunID = badRunID })},
		{name: "epoch_missing", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.Epoch = "" })},
		{name: "epoch_short", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.Epoch = protocolTestEpoch[:31] })},
		{name: "generation_zero", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.Generation = 0 })},
		{name: "begin_event_call_form", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.EventID = protocolTestBeginID + ":1" })},
		{name: "begin_event_short", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.EventID = protocolTestBeginID[:31] })},
		{name: "begin_with_seq", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.Seq = 1 })},
		{name: "begin_with_kind", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.Kind = KindPayment })},
		{name: "begin_with_point", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.Point = PointRegistered })},
		{name: "begin_with_order", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.OrderID = protocolTestOrderID })},
		{name: "begin_with_expected", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.ExpectedLastSeq = &sealZero })},
		{name: "begin_with_for", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.For = TypeBegin })},
		{name: "begin_with_action", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.Action = ActionContinue })},
		{name: "begin_with_code", frame: protocolTestMutate(baseBegin, func(f *Frame) { f.Code = FaultProtocol })},

		{name: "call_kind_missing", frame: protocolTestMutate(baseCall, func(f *Frame) { f.Kind = "" })},
		{name: "call_kind_unknown", frame: protocolTestMutate(baseCall, func(f *Frame) { f.Kind = Kind("OTHER") })},
		{name: "call_seq_zero", frame: protocolTestMutate(baseCall, func(f *Frame) { f.Seq = 0 })},
		{name: "call_seq_above_max", frame: protocolTestMutate(baseCall, func(f *Frame) { f.Seq = MaxRecords + 1 })},
		{name: "call_event_mismatch", frame: protocolTestMutate(baseCall, func(f *Frame) { f.EventID = protocolTestBeginID + ":2" })},
		{name: "call_order_missing", frame: protocolTestMutate(baseCall, func(f *Frame) { f.OrderID = "" })},
		{name: "call_order_invalid", frame: protocolTestMutate(baseCall, func(f *Frame) { f.OrderID = "not-a-uuid" })},
		{name: "call_order_version_one", frame: protocolTestMutate(baseCall, func(f *Frame) { f.OrderID = "123e4567-e89b-12d3-a456-426614174000" })},
		{name: "call_order_variant_invalid", frame: protocolTestMutate(baseCall, func(f *Frame) { f.OrderID = "123e4567-e89b-42d3-c456-426614174000" })},
		{name: "call_with_point", frame: protocolTestMutate(baseCall, func(f *Frame) { f.Point = PointRegistered })},
		{name: "call_with_expected", frame: protocolTestMutate(baseCall, func(f *Frame) { f.ExpectedLastSeq = &sealZero })},

		{name: "reach_point_missing", frame: protocolTestMutate(baseReach, func(f *Frame) { f.Point = "" })},
		{name: "reach_point_unknown", frame: protocolTestMutate(baseReach, func(f *Frame) { f.Point = Point("COMMITTED") })},
		{name: "reach_seq_zero", frame: protocolTestMutate(baseReach, func(f *Frame) { f.Seq = 0 })},
		{name: "reach_event_mismatch", frame: protocolTestMutate(baseReach, func(f *Frame) { f.EventID = protocolTestBeginID + ":3" })},
		{name: "reach_order_missing", frame: protocolTestMutate(baseReach, func(f *Frame) { f.OrderID = "" })},
		{name: "reach_with_kind", frame: protocolTestMutate(baseReach, func(f *Frame) { f.Kind = KindPayment })},

		{name: "seal_expected_missing", frame: protocolTestMutate(baseSeal, func(f *Frame) { f.ExpectedLastSeq = nil })},
		{name: "seal_expected_above_max", frame: protocolTestMutate(baseSeal, func(f *Frame) { f.ExpectedLastSeq = &sealAboveMax })},
		{name: "seal_seq_nonzero", frame: protocolTestMutate(baseSeal, func(f *Frame) { f.Seq = 1 })},
		{name: "seal_event_call_form", frame: protocolTestMutate(baseSeal, func(f *Frame) { f.EventID = protocolTestBeginID + ":1" })},

		{name: "ack_for_missing", frame: protocolTestMutate(baseAckBegin, func(f *Frame) { f.For = "" })},
		{name: "ack_for_unknown", frame: protocolTestMutate(baseAckBegin, func(f *Frame) { f.For = Type("fault") })},
		{name: "ack_begin_with_seq", frame: protocolTestMutate(baseAckBegin, func(f *Frame) { f.Seq = 1 })},
		{name: "ack_call_seq_missing", frame: protocolTestMutate(baseAckCall, func(f *Frame) { f.Seq = 0 })},
		{name: "ack_call_event_mismatch", frame: protocolTestMutate(baseAckCall, func(f *Frame) { f.EventID = protocolTestBeginID + ":2" })},
		{name: "ack_call_with_kind", frame: protocolTestMutate(baseAckCall, func(f *Frame) { f.Kind = KindPayment })},
		{name: "ack_call_with_expected", frame: protocolTestMutate(baseAckCall, func(f *Frame) { f.ExpectedLastSeq = &sealZero })},
		{name: "ack_reach_action_missing", frame: protocolTestMutate(baseAckReach, func(f *Frame) { f.Action = "" })},
		{name: "ack_reach_action_unknown", frame: protocolTestMutate(baseAckReach, func(f *Frame) { f.Action = Action("hidden") })},
		{name: "ack_seal_expected_missing", frame: protocolTestMutate(baseAckSeal, func(f *Frame) { f.ExpectedLastSeq = nil })},
		{name: "ack_seal_expected_above_max", frame: protocolTestMutate(baseAckSeal, func(f *Frame) { f.ExpectedLastSeq = &sealAboveMax })},
		{name: "ack_with_point", frame: protocolTestMutate(baseAckBegin, func(f *Frame) { f.Point = PointRegistered })},
		{name: "ack_with_order", frame: protocolTestMutate(baseAckBegin, func(f *Frame) { f.OrderID = protocolTestOrderID })},

		{name: "fault_with_identity", frame: protocolTestMutate(baseFault, func(f *Frame) {
			f.RunID = protocolTestRunID
			f.Epoch = protocolTestEpoch
			f.Generation = 1
			f.EventID = protocolTestBeginID
		})},
		{name: "fault_code_missing", frame: protocolTestMutate(baseFault, func(f *Frame) { f.Code = "" })},
		{name: "fault_code_unknown", frame: protocolTestMutate(baseFault, func(f *Frame) { f.Code = FaultCode("other") })},
		{name: "fault_with_seq", frame: protocolTestMutate(baseFault, func(f *Frame) { f.Seq = 1 })},
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			encoded, err := EncodeFrame(tc.frame)
			if err == nil {
				t.Fatal("invalid frame was accepted for encoding")
			}
			if !errors.Is(err, ErrProtocol) {
				t.Fatal("encode rejection is not ErrProtocol")
			}
			if len(encoded) != 0 {
				t.Fatal("encode rejection returned bytes")
			}
		})
	}
}

func TestProtocolErrorsDoNotEchoInput(t *testing.T) {
	const canary = "FAKE_T018_PROTOCOL_CANARY"

	decodeInput := protocolTestAdd(protocolTestBeginJSON, `"extra":"`+canary+`"`)
	if _, err := DecodeFrame(decodeInput); err == nil {
		t.Fatal("expected decode rejection")
	} else {
		if !errors.Is(err, ErrProtocol) {
			t.Fatal("decode rejection is not ErrProtocol")
		}
		if strings.Contains(err.Error(), canary) {
			t.Fatal("decode error echoed raw input")
		}
	}

	encodeFrame := protocolTestIdentity(TypeBegin, canary)
	if _, err := EncodeFrame(encodeFrame); err == nil {
		t.Fatal("expected encode rejection")
	} else {
		if !errors.Is(err, ErrProtocol) {
			t.Fatal("encode rejection is not ErrProtocol")
		}
		if strings.Contains(err.Error(), canary) {
			t.Fatal("encode error echoed raw input")
		}
	}
}
