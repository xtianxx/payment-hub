package testcontrol

import (
	"errors"
	"reflect"
	"testing"
)

// TestQueryScenarioProtocol freezes the v2 query-control wire boundary. It
// proves that v1 remains byte/field strict, that v2 negotiation uses exactly
// one fixed capability, and that query fields cannot mix with payment fields.

func queryProtocolV2Begin() Frame {
	return Frame{
		Version:    Version2,
		Type:       TypeBegin,
		RunID:      protocolTestRunID,
		Epoch:      protocolTestEpoch,
		Generation: 1,
		EventID:    protocolTestBeginID,
		Capability: CapabilityQueryControl,
	}
}

func queryProtocolV2AckBegin() Frame {
	frame := queryProtocolV2Begin()
	frame.Type = TypeAck
	frame.For = TypeBegin
	return frame
}

func queryProtocolV2Call(seq uint64, kind Kind, orderID string) Frame {
	return Frame{
		Version:    Version2,
		Type:       TypeCall,
		RunID:      protocolTestRunID,
		Epoch:      protocolTestEpoch,
		Generation: 1,
		Seq:        seq,
		EventID:    protocolTestBeginID + ":" + queryProtocolUint(seq),
		Kind:       kind,
		OrderID:    orderID,
	}
}

func queryProtocolV2AckCall(seq uint64, orderID string, scenario *QueryScenario) Frame {
	frame := queryProtocolV2Call(seq, KindQuery, orderID)
	frame.Type = TypeAck
	frame.For = TypeCall
	frame.Kind = ""
	frame.OrderID = ""
	frame.QueryDirective = scenario
	return frame
}

func queryProtocolV2Applied(seq, callSeq uint64, orderID string, outcome QueryOutcome) Frame {
	return Frame{
		Version:      Version2,
		Type:         TypeReach,
		RunID:        protocolTestRunID,
		Epoch:        protocolTestEpoch,
		Generation:   1,
		Seq:          seq,
		EventID:      protocolTestBeginID + ":" + queryProtocolUint(seq),
		Point:        PointQueryApplied,
		OrderID:      orderID,
		QueryCallSeq: &callSeq,
		QueryOutcome: &outcome,
	}
}

func queryProtocolUint(value uint64) string {
	if value == 0 {
		return "0"
	}
	digits := "0123456789"
	var buf [20]byte
	index := len(buf)
	for value > 0 {
		index--
		buf[index] = digits[value%10]
		value /= 10
	}
	return string(buf[index:])
}

func queryProtocolRoundTrip(t *testing.T, frame Frame) Frame {
	t.Helper()
	encoded, err := EncodeFrame(frame)
	if err != nil {
		t.Fatal("v2 query frame failed to encode")
	}
	decoded, err := DecodeFrame(encoded)
	if err != nil {
		t.Fatal("v2 query frame failed to decode")
	}
	if !reflect.DeepEqual(frame, decoded) {
		t.Fatal("v2 query frame did not round-trip exactly")
	}
	return decoded
}

func queryProtocolRequireReject(t *testing.T, frame Frame) {
	t.Helper()
	encoded, err := EncodeFrame(frame)
	if err == nil {
		t.Fatal("invalid query-control frame was accepted by encode")
	}
	if !errors.Is(err, ErrProtocol) {
		t.Fatal("encode rejection was not ErrProtocol")
	}
	if len(encoded) != 0 {
		t.Fatal("encode rejection returned bytes")
	}
}

func queryProtocolRequireRawBytesReject(t *testing.T, raw []byte) {
	t.Helper()
	frame, err := DecodeFrame(raw)
	if err == nil {
		t.Fatal("invalid query-control raw bytes were accepted")
	}
	if !errors.Is(err, ErrProtocol) {
		t.Fatal("raw-bytes decode rejection was not ErrProtocol")
	}
	if frame != (Frame{}) {
		t.Fatal("raw-bytes decode rejection returned a nonzero frame")
	}
}

func queryProtocolRequireRawReject(t *testing.T, raw string) {
	t.Helper()
	frame, err := DecodeFrame(protocolTestLine(raw))
	if err == nil {
		t.Fatal("invalid query-control raw frame was accepted")
	}
	if !errors.Is(err, ErrProtocol) {
		t.Fatal("decode rejection was not ErrProtocol")
	}
	if frame != (Frame{}) {
		t.Fatal("decode rejection returned a nonzero frame")
	}
}

func TestQueryScenarioProtocol(t *testing.T) {
	if Version != 1 || Version2 != 2 {
		t.Fatal("protocol version constants changed")
	}
	if CapabilityQueryControl != "query-control-v1" {
		t.Fatal("query-control capability literal changed")
	}
	if KindQuery != "QUERY" || PointQueryApplied != "QUERY_APPLIED" {
		t.Fatal("query wire literals changed")
	}

	t.Run("closed_scenario_and_outcome_sets", func(t *testing.T) {
		scenarios := []QueryScenario{
			QueryScenarioNormal, QueryScenarioFailure, QueryScenarioSubtimeout,
			QueryScenarioNotVisible, QueryScenarioMismatchReference,
			QueryScenarioMismatchAmount, QueryScenarioMismatchCurrency,
			QueryScenarioMismatchTransaction,
		}
		wantScenarios := []string{
			"normal", "failure", "subtimeout", "notvisible", "mismatch_reference",
			"mismatch_amount", "mismatch_currency", "mismatch_transaction",
		}
		for i, scenario := range scenarios {
			if string(scenario) != wantScenarios[i] {
				t.Fatal("query scenario literal mismatch")
			}
		}
		outcomes := []QueryOutcome{
			QueryOutcomeFound, QueryOutcomeNotFound, QueryOutcomeUnavailable,
			QueryOutcomeSubtimeout, QueryOutcomeMismatchApplied,
		}
		wantOutcomes := []string{"found", "not_found", "unavailable", "subtimeout", "mismatch_applied"}
		for i, outcome := range outcomes {
			if string(outcome) != wantOutcomes[i] {
				t.Fatal("query outcome literal mismatch")
			}
		}
	})

	t.Run("v2_begin_and_ack_round_trip", func(t *testing.T) {
		queryProtocolRoundTrip(t, queryProtocolV2Begin())
		queryProtocolRoundTrip(t, queryProtocolV2AckBegin())
	})

	t.Run("v2_query_call_and_directive_ack_round_trip", func(t *testing.T) {
		queryProtocolRoundTrip(t, queryProtocolV2Call(1, KindQuery, protocolTestOrderID))
		for _, scenario := range []QueryScenario{
			QueryScenarioNormal, QueryScenarioFailure, QueryScenarioSubtimeout,
			QueryScenarioNotVisible, QueryScenarioMismatchReference,
			QueryScenarioMismatchAmount, QueryScenarioMismatchCurrency,
			QueryScenarioMismatchTransaction,
		} {
			value := scenario
			queryProtocolRoundTrip(t, queryProtocolV2AckCall(1, protocolTestOrderID, &value))
		}
	})

	t.Run("v2_query_applied_round_trip", func(t *testing.T) {
		for _, outcome := range []QueryOutcome{
			QueryOutcomeFound, QueryOutcomeNotFound, QueryOutcomeUnavailable,
			QueryOutcomeSubtimeout, QueryOutcomeMismatchApplied,
		} {
			queryProtocolRoundTrip(t, queryProtocolV2Applied(2, 1, protocolTestOrderID, outcome))
		}
	})

	t.Run("v2_legacy_payment_directive_still_round_trips", func(t *testing.T) {
		call := queryProtocolV2Call(1, KindPayment, protocolTestOrderID)
		queryProtocolRoundTrip(t, call)
		ack := call
		ack.Type = TypeAck
		ack.For = TypeCall
		ack.Kind = ""
		ack.OrderID = ""
		value := DirectiveSucceeded
		ack.Directive = &value
		queryProtocolRoundTrip(t, ack)
	})

	t.Run("v1_rejects_every_new_field_and_point", func(t *testing.T) {
		begin := queryProtocolV2Begin()
		begin.Version = Version
		queryProtocolRequireReject(t, begin)

		ackBegin := queryProtocolV2AckBegin()
		ackBegin.Version = Version
		queryProtocolRequireReject(t, ackBegin)

		scenario := QueryScenarioNormal
		ackCall := queryProtocolV2AckCall(1, protocolTestOrderID, &scenario)
		ackCall.Version = Version
		queryProtocolRequireReject(t, ackCall)

		applied := queryProtocolV2Applied(2, 1, protocolTestOrderID, QueryOutcomeFound)
		applied.Version = Version
		queryProtocolRequireReject(t, applied)

		callSeq := uint64(1)
		outcome := QueryOutcomeFound
		reach := Frame{
			Version: Version, Type: TypeReach, RunID: protocolTestRunID,
			Epoch: protocolTestEpoch, Generation: 1, Seq: 2,
			EventID: protocolTestBeginID + ":2", Point: PointRegistered,
			OrderID: protocolTestOrderID, QueryCallSeq: &callSeq,
			QueryOutcome: &outcome,
		}
		queryProtocolRequireReject(t, reach)
	})

	t.Run("v2_begin_requires_exact_capability", func(t *testing.T) {
		for _, capability := range []string{"", "query-control-v2", "query-control-v1 ", "QUERY-CONTROL-V1"} {
			frame := queryProtocolV2Begin()
			frame.Capability = capability
			queryProtocolRequireReject(t, frame)
		}
	})

	t.Run("v2_ack_begin_requires_exact_capability", func(t *testing.T) {
		frame := queryProtocolV2AckBegin()
		frame.Capability = ""
		queryProtocolRequireReject(t, frame)
		frame.Capability = "other"
		queryProtocolRequireReject(t, frame)
	})

	t.Run("v2_rejects_mixed_payment_and_query_fields", func(t *testing.T) {
		scenario := QueryScenarioFailure
		directive := DirectiveFailed
		ack := queryProtocolV2AckCall(1, protocolTestOrderID, &scenario)
		ack.Directive = &directive
		queryProtocolRequireReject(t, ack)

		applied := queryProtocolV2Applied(2, 1, protocolTestOrderID, QueryOutcomeFound)
		callSeq := uint64(1)
		applied.PaymentCallSeq = &callSeq
		applied.AppliedResult = &directive
		queryProtocolRequireReject(t, applied)

		outcome := QueryOutcomeFound
		reach := Frame{
			Version: Version2, Type: TypeReach, RunID: protocolTestRunID,
			Epoch: protocolTestEpoch, Generation: 1, Seq: 2,
			EventID: protocolTestBeginID + ":2", Point: PointRegistered,
			OrderID: protocolTestOrderID, QueryCallSeq: &callSeq,
			QueryOutcome: &outcome,
		}
		queryProtocolRequireReject(t, reach)

		payment := queryProtocolV2Call(1, KindPayment, protocolTestOrderID)
		payment.QueryDirective = &scenario
		queryProtocolRequireReject(t, payment)

		proof := queryProtocolV2Call(1, KindPayment, protocolTestOrderID)
		proof.Type = TypeReach
		proof.Kind = ""
		proof.Point = PointMockCommitted
		proof.PaymentCallSeq = &callSeq
		proof.AppliedResult = &directive
		proof.QueryOutcome = &outcome
		queryProtocolRequireReject(t, proof)
	})

	t.Run("v2_query_applied_requires_exact_fields", func(t *testing.T) {
		frame := queryProtocolV2Applied(2, 1, protocolTestOrderID, QueryOutcomeFound)
		frame.QueryCallSeq = nil
		queryProtocolRequireReject(t, frame)
		frame = queryProtocolV2Applied(2, 1, protocolTestOrderID, QueryOutcomeFound)
		frame.QueryOutcome = nil
		queryProtocolRequireReject(t, frame)
		frame = queryProtocolV2Applied(2, 1, protocolTestOrderID, QueryOutcomeFound)
		zero := uint64(0)
		frame.QueryCallSeq = &zero
		queryProtocolRequireReject(t, frame)
		frame = queryProtocolV2Applied(2, 1, protocolTestOrderID, QueryOutcomeFound)
		equal := uint64(2)
		frame.QueryCallSeq = &equal
		queryProtocolRequireReject(t, frame)
		frame = queryProtocolV2Applied(2, 1, protocolTestOrderID, QueryOutcome("other"))
		queryProtocolRequireReject(t, frame)
	})

	t.Run("v2_unknown_fields_rejected_on_every_frame", func(t *testing.T) {
		queryProtocolRequireRawReject(t, `{"v":2,"type":"begin","run_id":"`+protocolTestRunID+
			`","observer_epoch":"`+protocolTestEpoch+`","generation":1,"event_id":"`+protocolTestBeginID+
			`","capability":"`+CapabilityQueryControl+`","extra":1}`)
		queryProtocolRequireRawReject(t, `{"v":2,"type":"call","run_id":"`+protocolTestRunID+
			`","observer_epoch":"`+protocolTestEpoch+`","generation":1,"seq":1,"event_id":"`+protocolTestBeginID+
			`:1","kind":"QUERY","order_id":"`+protocolTestOrderID+`","query_outcome":"found"}`)
	})

	t.Run("v1_raw_new_fields_rejected", func(t *testing.T) {
		queryProtocolRequireRawReject(t, `{"v":1,"type":"begin","run_id":"`+protocolTestRunID+
			`","observer_epoch":"`+protocolTestEpoch+`","generation":1,"event_id":"`+protocolTestBeginID+
			`","capability":"`+CapabilityQueryControl+`"}`)
		queryProtocolRequireRawReject(t, `{"v":1,"type":"reach","run_id":"`+protocolTestRunID+
			`","observer_epoch":"`+protocolTestEpoch+`","generation":1,"seq":2,"event_id":"`+protocolTestBeginID+
			`:2","point":"QUERY_APPLIED","order_id":"`+protocolTestOrderID+
			`","query_call_seq":1,"query_outcome":"found"}`)
	})

	t.Run("v2_raw_bad_capability_rejected", func(t *testing.T) {
		queryProtocolRequireRawReject(t, `{"v":2,"type":"begin","run_id":"`+protocolTestRunID+
			`","observer_epoch":"`+protocolTestEpoch+`","generation":1,"event_id":"`+protocolTestBeginID+
			`","capability":"other"}`)
	})

	t.Run("v1_ack_call_query_directive_raw_bytes_rejected", func(t *testing.T) {
		// These cases go directly through DecodeFrame. EncodeFrame must not be
		// used first because it would filter the very field under test.
		fields := []string{
			`"query_directive":"failure"`,
			`"query_directive":"normal"`,
			`"query_directive":""`,
			`"query_directive":123`,
			`"query_directive":null`,
			`"query_directive":true`,
			`"query_directive":{}`,
			`"query_directive":[]`,
			`"directive":"SUCCEEDED","query_directive":"failure"`,
		}
		for _, field := range fields {
			queryProtocolRequireRawBytesReject(t, protocolTestAdd(protocolTestAckCallJSON, field))
		}
		if _, err := DecodeFrame(protocolTestLine(protocolTestAckCallJSON)); err != nil {
			t.Fatal("legacy v1 ack call without query_directive was rejected")
		}
	})

	t.Run("v2_ack_call_query_directive_raw_bytes", func(t *testing.T) {
		v2AckCall := `{"v":2,"type":"ack","run_id":"` + protocolTestRunID +
			`","observer_epoch":"` + protocolTestEpoch +
			`","generation":1,"seq":1,"event_id":"` + protocolTestBeginID + `:1","for":"call"}`
		accepted, err := DecodeFrame(protocolTestAdd(v2AckCall, `"query_directive":"failure"`))
		if err != nil {
			t.Fatal("v2 fixed query_directive raw ack was rejected")
		}
		if accepted.QueryDirective == nil || *accepted.QueryDirective != QueryScenarioFailure {
			t.Fatal("v2 fixed query_directive raw ack was not preserved")
		}
		if _, err := DecodeFrame(protocolTestAdd(v2AckCall, `"query_directive":"normal"`)); err != nil {
			t.Fatal("v2 normal query_directive raw ack was rejected")
		}
		if _, err := DecodeFrame(protocolTestLine(v2AckCall)); err != nil {
			t.Fatal("v2 ack call without query_directive was rejected")
		}
		for _, field := range []string{
			`"query_directive":""`,
			`"query_directive":"other"`,
			`"query_directive":123`,
			`"query_directive":null`,
			`"query_directive":true`,
			`"query_directive":{}`,
			`"query_directive":[]`,
			`"directive":"SUCCEEDED","query_directive":"failure"`,
		} {
			queryProtocolRequireRawBytesReject(t, protocolTestAdd(v2AckCall, field))
		}
	})

	t.Run("v1_legacy_bytes_unchanged", func(t *testing.T) {
		for _, raw := range []string{
			protocolTestBeginJSON, protocolTestCallJSON, protocolTestReachJSON,
			protocolTestSealJSON, protocolTestAckBeginJSON, protocolTestAckCallJSON,
			protocolTestAckReachJSON, protocolTestAckSealJSON, protocolTestFaultJSON,
		} {
			if _, err := DecodeFrame(protocolTestLine(raw)); err != nil {
				t.Fatal("legacy v1 bytes were rejected")
			}
		}
	})
}
