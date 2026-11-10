package testcontrol

import (
	"bytes"
	"encoding/json"
	"errors"
	"reflect"
	"strings"
	"testing"
)

func protocolTestAddPair(body, seq, result string) []byte {
	body = strings.TrimSuffix(body, "}")
	return protocolTestLine(body + `,"payment_call_seq":` + seq + `,"applied_result":` + result + `}`)
}

func protocolTestFrameKeys(t *testing.T, encoded []byte) map[string]json.RawMessage {
	t.Helper()
	if len(encoded) == 0 || encoded[len(encoded)-1] != '\n' {
		t.Fatal("encoded frame has no trailing newline")
	}
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(encoded[:len(encoded)-1], &raw); err != nil {
		t.Fatal("encoded frame is not one JSON object")
	}
	return raw
}

func protocolTestRoundTripDirective(t *testing.T, frame Frame, required ...string) {
	t.Helper()
	encoded, err := EncodeFrame(frame)
	if err != nil {
		t.Fatal("valid directive frame failed encoding")
	}
	raw := protocolTestFrameKeys(t, encoded)
	for _, key := range required {
		if _, ok := raw[key]; !ok {
			t.Fatal("encoded directive frame is missing a required key")
		}
	}
	decoded, err := DecodeFrame(encoded)
	if err != nil {
		t.Fatal("valid directive frame failed decoding")
	}
	if !reflect.DeepEqual(decoded, frame) {
		t.Fatal("valid directive frame did not round trip")
	}
	reencoded, err := EncodeFrame(decoded)
	if err != nil {
		t.Fatal("decoded directive frame failed re-encoding")
	}
	if !bytes.Equal(reencoded, encoded) {
		t.Fatal("directive frame encoding is not stable")
	}
}

func TestPaymentDirectiveProtocol(t *testing.T) {
	if DirectiveSucceeded != "SUCCEEDED" || DirectiveFailed != "FAILED" {
		t.Fatal("frozen directive value changed")
	}

	succeeded := DirectiveSucceeded
	failed := DirectiveFailed
	callSeqOne := uint64(1)
	callSeqMax := uint64(MaxRecords - 1)

	baseAckCall := protocolTestIdentity(TypeAck, protocolTestBeginID+":1")
	baseAckCall.Seq = 1
	baseAckCall.For = TypeCall

	baseProofReach := protocolTestIdentity(TypeReach, protocolTestBeginID+":2")
	baseProofReach.Seq = 2
	baseProofReach.Point = PointMockCommitted
	baseProofReach.OrderID = protocolTestOrderID
	baseProofReach.PaymentCallSeq = &callSeqOne
	baseProofReach.AppliedResult = &succeeded

	t.Run("valid_ack_call_directive", func(t *testing.T) {
		for _, directive := range []Directive{DirectiveSucceeded, DirectiveFailed} {
			frame := baseAckCall
			value := directive
			frame.Directive = &value
			protocolTestRoundTripDirective(t, frame, "directive")
		}
	})

	t.Run("valid_reach_proof", func(t *testing.T) {
		for _, directive := range []Directive{DirectiveSucceeded, DirectiveFailed} {
			frame := baseProofReach
			value := directive
			frame.AppliedResult = &value
			protocolTestRoundTripDirective(t, frame, "payment_call_seq", "applied_result")
		}
	})

	t.Run("valid_reach_proof_at_max_seq", func(t *testing.T) {
		frame := protocolTestIdentity(TypeReach, protocolTestBeginID+":4096")
		frame.Seq = MaxRecords
		frame.Point = PointMockCommitted
		frame.OrderID = protocolTestOrderID
		frame.PaymentCallSeq = &callSeqMax
		frame.AppliedResult = &failed
		protocolTestRoundTripDirective(t, frame, "payment_call_seq", "applied_result")
	})

	t.Run("legacy_frames_remain_compatible", func(t *testing.T) {
		legacyAck := baseAckCall
		protocolTestRoundTripDirective(t, legacyAck)
		legacyReach := baseProofReach
		legacyReach.PaymentCallSeq = nil
		legacyReach.AppliedResult = nil
		protocolTestRoundTripDirective(t, legacyReach)

		decoded, err := DecodeFrame(protocolTestLine(protocolTestCallJSON))
		if err != nil {
			t.Fatal("legacy call frame without directive fields was rejected")
		}
		if decoded.Directive != nil || decoded.PaymentCallSeq != nil || decoded.AppliedResult != nil {
			t.Fatal("legacy call frame gained directive fields")
		}
	})

	ackCallRaw := protocolTestAckCallJSON
	ackBeginRaw := protocolTestAckBeginJSON
	ackReachRaw := protocolTestAckReachJSON
	ackSealRaw := protocolTestAckSealJSON
	callRaw := protocolTestCallJSON
	reachRaw := protocolTestReachJSON
	mockReachRaw := strings.Replace(protocolTestReachJSON, `"point":"REGISTERED"`, `"point":"MOCK_COMMITTED"`, 1)
	sealRaw := protocolTestSealJSON
	beginRaw := protocolTestBeginJSON
	faultRaw := protocolTestFaultJSON

	decodeCases := []protocolRejectCase{
		{name: "ack_call_directive_null", input: protocolTestAdd(ackCallRaw, `"directive":null`)},
		{name: "ack_call_directive_empty", input: protocolTestAdd(ackCallRaw, `"directive":""`)},
		{name: "ack_call_directive_unknown", input: protocolTestAdd(ackCallRaw, `"directive":"OTHER"`)},
		{name: "ack_call_directive_number", input: protocolTestAdd(ackCallRaw, `"directive":1`)},
		{name: "ack_call_directive_bool", input: protocolTestAdd(ackCallRaw, `"directive":true`)},
		{name: "ack_call_directive_object", input: protocolTestAdd(ackCallRaw, `"directive":{}`)},
		{name: "ack_begin_directive", input: protocolTestAdd(ackBeginRaw, `"directive":"SUCCEEDED"`)},
		{name: "ack_reach_directive", input: protocolTestAdd(ackReachRaw, `"directive":"SUCCEEDED"`)},
		{name: "ack_seal_directive", input: protocolTestAdd(ackSealRaw, `"directive":"SUCCEEDED"`)},
		{name: "begin_directive", input: protocolTestAdd(beginRaw, `"directive":"SUCCEEDED"`)},
		{name: "call_directive", input: protocolTestAdd(callRaw, `"directive":"SUCCEEDED"`)},
		{name: "reach_directive", input: protocolTestAdd(mockReachRaw, `"directive":"SUCCEEDED"`)},
		{name: "seal_directive", input: protocolTestAdd(sealRaw, `"directive":"SUCCEEDED"`)},
		{name: "fault_directive", input: protocolTestAdd(faultRaw, `"directive":"SUCCEEDED"`)},

		{name: "reach_pair_missing_result", input: protocolTestAdd(mockReachRaw, `"payment_call_seq":1`)},
		{name: "reach_pair_missing_call_seq", input: protocolTestAdd(mockReachRaw, `"applied_result":"SUCCEEDED"`)},
		{name: "reach_pair_null_call_seq", input: protocolTestAddPair(mockReachRaw, `null`, `"SUCCEEDED"`)},
		{name: "reach_pair_null_result", input: protocolTestAddPair(mockReachRaw, `1`, `null`)},
		{name: "reach_pair_empty_result", input: protocolTestAddPair(mockReachRaw, `1`, `""`)},
		{name: "reach_pair_unknown_result", input: protocolTestAddPair(mockReachRaw, `1`, `"OTHER"`)},
		{name: "reach_pair_number_result", input: protocolTestAddPair(mockReachRaw, `1`, `1`)},
		{name: "reach_pair_zero_call_seq", input: protocolTestAddPair(mockReachRaw, `0`, `"SUCCEEDED"`)},
		{name: "reach_pair_equal_call_seq", input: protocolTestAddPair(mockReachRaw, `2`, `"SUCCEEDED"`)},
		{name: "reach_pair_above_reach_seq", input: protocolTestAddPair(mockReachRaw, `3`, `"SUCCEEDED"`)},
		{name: "reach_pair_negative_call_seq", input: protocolTestAddPair(mockReachRaw, `-1`, `"SUCCEEDED"`)},
		{name: "reach_pair_float_call_seq", input: protocolTestAddPair(mockReachRaw, `1.0`, `"SUCCEEDED"`)},
		{name: "reach_pair_string_call_seq", input: protocolTestAddPair(mockReachRaw, `"1"`, `"SUCCEEDED"`)},
		{name: "reach_pair_overflow_call_seq", input: protocolTestAddPair(mockReachRaw, `18446744073709551616`, `"SUCCEEDED"`)},
		{name: "reach_pair_on_registered", input: protocolTestAddPair(reachRaw, `1`, `"SUCCEEDED"`)},
		{name: "reach_pair_on_saving_result", input: protocolTestAddPair(strings.Replace(protocolTestReachJSON, `"point":"REGISTERED"`, `"point":"SAVING_RESULT"`, 1), `1`, `"SUCCEEDED"`)},
		{name: "reach_pair_on_result_committed", input: protocolTestAddPair(strings.Replace(protocolTestReachJSON, `"point":"REGISTERED"`, `"point":"RESULT_COMMITTED"`, 1), `1`, `"SUCCEEDED"`)},

		{name: "ack_call_pair_call_seq", input: protocolTestAdd(ackCallRaw, `"payment_call_seq":1`)},
		{name: "ack_call_pair_result", input: protocolTestAdd(ackCallRaw, `"applied_result":"SUCCEEDED"`)},
		{name: "ack_begin_pair", input: protocolTestAddPair(ackBeginRaw, `1`, `"SUCCEEDED"`)},
		{name: "ack_reach_pair", input: protocolTestAddPair(ackReachRaw, `1`, `"SUCCEEDED"`)},
		{name: "ack_seal_pair", input: protocolTestAddPair(ackSealRaw, `1`, `"SUCCEEDED"`)},
		{name: "begin_pair", input: protocolTestAddPair(beginRaw, `1`, `"SUCCEEDED"`)},
		{name: "call_pair", input: protocolTestAddPair(callRaw, `1`, `"SUCCEEDED"`)},
		{name: "seal_pair", input: protocolTestAddPair(sealRaw, `1`, `"SUCCEEDED"`)},
		{name: "fault_pair", input: protocolTestAddPair(faultRaw, `1`, `"SUCCEEDED"`)},
	}

	for _, tc := range decodeCases {
		tc := tc
		t.Run("decode_"+tc.name, func(t *testing.T) {
			frame, err := DecodeFrame(tc.input)
			if err == nil {
				t.Fatal("malformed directive frame was accepted")
			}
			if !errors.Is(err, ErrProtocol) {
				t.Fatal("directive decode rejection is not ErrProtocol")
			}
			if frame != (Frame{}) {
				t.Fatal("directive decode rejection returned a nonzero frame")
			}
		})
	}

	encodeBaseProof := baseProofReach
	encodeBaseProofOnlySeq := baseProofReach
	encodeBaseProofOnlyResult := baseProofReach
	encodeBaseProofOnlySeq.AppliedResult = nil
	encodeBaseProofOnlyResult.PaymentCallSeq = nil

	badDirective := Directive("OTHER")
	emptyDirective := Directive("")
	zeroSeq := uint64(0)
	equalSeq := uint64(2)
	aboveSeq := uint64(3)
	aboveMaxSeq := uint64(MaxRecords + 1)
	oneSeq := uint64(1)

	encodeBaseAckDirective := baseAckCall
	encodeBaseAckDirective.Directive = &succeeded

	encodeCases := []protocolEncodeRejectCase{
		{name: "ack_call_directive_empty", frame: protocolTestMutate(baseAckCall, func(f *Frame) { f.Directive = &emptyDirective })},
		{name: "ack_call_directive_unknown", frame: protocolTestMutate(baseAckCall, func(f *Frame) { f.Directive = &badDirective })},
		{name: "ack_call_pair_seq", frame: protocolTestMutate(encodeBaseAckDirective, func(f *Frame) { f.PaymentCallSeq = &oneSeq })},
		{name: "ack_call_pair_result", frame: protocolTestMutate(encodeBaseAckDirective, func(f *Frame) { f.AppliedResult = &succeeded })},
		{name: "ack_begin_directive", frame: protocolTestMutate(protocolTestIdentity(TypeAck, protocolTestBeginID), func(f *Frame) {
			f.For = TypeBegin
			f.Directive = &succeeded
		})},
		{name: "ack_reach_directive", frame: protocolTestMutate(protocolTestIdentity(TypeAck, protocolTestBeginID+":2"), func(f *Frame) {
			f.Seq = 2
			f.For = TypeReach
			f.Action = ActionContinue
			f.Directive = &succeeded
		})},
		{name: "ack_seal_directive", frame: protocolTestMutate(protocolTestIdentity(TypeAck, protocolTestBeginID+":seal"), func(f *Frame) {
			f.For = TypeSeal
			f.ExpectedLastSeq = &zeroSeq
			f.Directive = &succeeded
		})},
		{name: "begin_directive", frame: protocolTestMutate(protocolTestIdentity(TypeBegin, protocolTestBeginID), func(f *Frame) { f.Directive = &succeeded })},
		{name: "call_directive", frame: protocolTestMutate(protocolTestIdentity(TypeCall, protocolTestBeginID+":1"), func(f *Frame) {
			f.Seq = 1
			f.Kind = KindPayment
			f.OrderID = protocolTestOrderID
			f.Directive = &succeeded
		})},
		{name: "reach_directive", frame: protocolTestMutate(encodeBaseProof, func(f *Frame) { f.Directive = &succeeded })},
		{name: "seal_directive", frame: protocolTestMutate(protocolTestIdentity(TypeSeal, protocolTestBeginID+":seal"), func(f *Frame) {
			f.ExpectedLastSeq = &zeroSeq
			f.Directive = &succeeded
		})},
		{name: "fault_directive", frame: protocolTestMutate(Frame{Version: Version, Type: TypeFault, Code: FaultProtocol}, func(f *Frame) { f.Directive = &succeeded })},

		{name: "reach_pair_only_seq", frame: encodeBaseProofOnlySeq},
		{name: "reach_pair_only_result", frame: encodeBaseProofOnlyResult},
		{name: "reach_pair_zero_seq", frame: protocolTestMutate(encodeBaseProof, func(f *Frame) { f.PaymentCallSeq = &zeroSeq })},
		{name: "reach_pair_equal_seq", frame: protocolTestMutate(encodeBaseProof, func(f *Frame) { f.PaymentCallSeq = &equalSeq })},
		{name: "reach_pair_above_seq", frame: protocolTestMutate(encodeBaseProof, func(f *Frame) { f.PaymentCallSeq = &aboveSeq })},
		{name: "reach_pair_above_max", frame: protocolTestMutate(encodeBaseProof, func(f *Frame) {
			f.Seq = MaxRecords
			f.EventID = protocolTestBeginID + ":4096"
			f.PaymentCallSeq = &aboveMaxSeq
		})},
		{name: "reach_pair_empty_result", frame: protocolTestMutate(encodeBaseProof, func(f *Frame) { f.AppliedResult = &emptyDirective })},
		{name: "reach_pair_unknown_result", frame: protocolTestMutate(encodeBaseProof, func(f *Frame) { f.AppliedResult = &badDirective })},
		{name: "reach_pair_on_registered", frame: protocolTestMutate(encodeBaseProof, func(f *Frame) { f.Point = PointRegistered })},
		{name: "reach_pair_on_saving_result", frame: protocolTestMutate(encodeBaseProof, func(f *Frame) { f.Point = PointSavingResult })},
		{name: "reach_pair_on_result_committed", frame: protocolTestMutate(encodeBaseProof, func(f *Frame) { f.Point = PointResultCommitted })},
		{name: "call_pair", frame: protocolTestMutate(protocolTestIdentity(TypeCall, protocolTestBeginID+":1"), func(f *Frame) {
			f.Seq = 1
			f.Kind = KindPayment
			f.OrderID = protocolTestOrderID
			f.PaymentCallSeq = &oneSeq
			f.AppliedResult = &succeeded
		})},
		{name: "seal_pair", frame: protocolTestMutate(protocolTestIdentity(TypeSeal, protocolTestBeginID+":seal"), func(f *Frame) {
			f.ExpectedLastSeq = &zeroSeq
			f.PaymentCallSeq = &oneSeq
			f.AppliedResult = &succeeded
		})},
		{name: "fault_pair", frame: protocolTestMutate(Frame{Version: Version, Type: TypeFault, Code: FaultProtocol}, func(f *Frame) {
			f.PaymentCallSeq = &oneSeq
			f.AppliedResult = &succeeded
		})},
	}

	for _, tc := range encodeCases {
		tc := tc
		t.Run("encode_"+tc.name, func(t *testing.T) {
			encoded, err := EncodeFrame(tc.frame)
			if err == nil {
				t.Fatal("invalid directive frame was accepted for encoding")
			}
			if !errors.Is(err, ErrProtocol) {
				t.Fatal("directive encode rejection is not ErrProtocol")
			}
			if len(encoded) != 0 {
				t.Fatal("directive encode rejection returned bytes")
			}
		})
	}
}
