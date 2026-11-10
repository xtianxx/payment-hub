package testcontrol_test

import (
	"bytes"
	"context"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	"payment-hub/internal/testcontrol"
	"payment-hub/tests/harness"
)

type clientTestPaymentResult struct {
	token testcontrol.PaymentCall
	err   error
}

func clientTestPaymentEntryAsync(t *testing.T, client *testcontrol.Client, orderID string) <-chan clientTestPaymentResult {
	t.Helper()
	ch := make(chan clientTestPaymentResult, 1)
	ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
	go func() {
		defer cancel()
		token, err := client.PaymentEntry(ctx, orderID)
		ch <- clientTestPaymentResult{token: token, err: err}
	}()
	return ch
}

func clientTestAwaitPaymentEntry(t *testing.T, ch <-chan clientTestPaymentResult) testcontrol.PaymentCall {
	t.Helper()
	select {
	case result := <-ch:
		if result.err != nil {
			t.Fatal("payment entry through the real observer failed")
		}
		return result.token
	case <-time.After(clientTestBudget):
		t.Fatal("payment entry did not return within budget")
	}
	return testcontrol.PaymentCall{}
}

func clientTestHoldPaymentEntry(t *testing.T, obs *harness.Observer, client *testcontrol.Client, orderID string) (testcontrol.PaymentCall, *harness.PaymentHold) {
	t.Helper()
	hold, err := obs.HoldPaymentEntry(1)
	if err != nil {
		t.Fatal("hold payment entry failed")
	}
	entry := clientTestPaymentEntryAsync(t, client, orderID)
	ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancel()
	if _, err := hold.Wait(ctx); err != nil {
		t.Fatal("payment hold wait failed")
	}
	if err := hold.Release(testcontrol.DirectiveSucceeded); err != nil {
		t.Fatal("payment hold release failed")
	}
	return clientTestAwaitPaymentEntry(t, entry), hold
}

func TestPaymentDirectiveClient(t *testing.T) {
	t.Run("entry_and_commit_happy_path", func(t *testing.T) {
		obs, socket := clientTestStartObserver(t, 1)
		client := clientTestDial(t, socket, 1)
		hold, err := obs.HoldPaymentEntry(1)
		if err != nil {
			t.Fatal("hold payment entry failed")
		}
		entry := clientTestPaymentEntryAsync(t, client, clientTestOrderA)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		binding, err := hold.Wait(ctx)
		if err != nil {
			t.Fatal("payment hold wait failed")
		}
		if binding.RunID != clientTestRunID || binding.Epoch != clientTestEpoch ||
			binding.Generation != 1 || binding.CallSeq != 1 || binding.OrderID != clientTestOrderA ||
			!strings.HasSuffix(binding.EventID, ":1") {
			t.Fatal("payment binding identity mismatch")
		}
		if err := hold.Release(testcontrol.DirectiveSucceeded); err != nil {
			t.Fatal("payment hold release failed")
		}
		token := clientTestAwaitPaymentEntry(t, entry)
		if directive, ok := token.Directive(); !ok || directive != testcontrol.DirectiveSucceeded {
			t.Fatal("payment token did not carry the selected directive")
		}
		if err := client.MockCommitted(ctx, token, testcontrol.DirectiveSucceeded); err != nil {
			t.Fatal("mock committed proof through the real observer failed")
		}
		receipt := clientTestSealOK(t, client)
		if receipt.LastSeq != 2 {
			t.Fatal("client seal did not close the payment call and proof reach")
		}
		counts, err := obs.Snapshot()
		if err != nil {
			t.Fatal("observer snapshot after payment directive flow failed")
		}
		if counts.Payment != 1 || counts.Query != 0 {
			t.Fatal("observer counted payment directive flow incorrectly")
		}
	})

	t.Run("unconsumed_hold_blocks_seal", func(t *testing.T) {
		obs, socket := clientTestStartObserver(t, 1)
		client := clientTestDial(t, socket, 1)
		hold, err := obs.HoldPaymentEntry(1)
		if err != nil {
			t.Fatal("hold payment entry failed")
		}
		entry := clientTestPaymentEntryAsync(t, client, clientTestOrderA)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		if _, err := hold.Wait(ctx); err != nil {
			t.Fatal("payment hold wait failed")
		}
		if err := hold.Release(testcontrol.DirectiveSucceeded); err != nil {
			t.Fatal("payment hold release failed")
		}
		_ = clientTestAwaitPaymentEntry(t, entry)
		clientTestAssertSealFault(t, client)
		if _, err := obs.Snapshot(); err == nil {
			t.Fatal("unconsumed payment directive left a trusted observer snapshot")
		}
	})

	t.Run("wrong_actual_result_faults", func(t *testing.T) {
		obs, socket := clientTestStartObserver(t, 1)
		client := clientTestDial(t, socket, 1)
		token, _ := clientTestHoldPaymentEntry(t, obs, client, clientTestOrderA)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		clientTestRequireFault(t, client.MockCommitted(ctx, token, testcontrol.DirectiveFailed))
		clientTestAssertSealFault(t, client)
	})

	t.Run("invalid_actual_enum_faults_without_wire", func(t *testing.T) {
		obs, socket := clientTestStartObserver(t, 1)
		client := clientTestDial(t, socket, 1)
		token, _ := clientTestHoldPaymentEntry(t, obs, client, clientTestOrderA)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		clientTestRequireFault(t, client.MockCommitted(ctx, token, testcontrol.Directive(clientTestCanary)))
		clientTestAssertSealFault(t, client)
	})

	t.Run("zero_token_and_nil_client", func(t *testing.T) {
		_, socket := clientTestStartObserver(t, 1)
		client := clientTestDial(t, socket, 1)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		clientTestRequireFault(t, client.MockCommitted(ctx, testcontrol.PaymentCall{}, testcontrol.DirectiveSucceeded))
		clientTestAssertSealFault(t, client)

		var nilClient *testcontrol.Client
		clientTestRequireFault(t, nilClient.MockCommitted(ctx, testcontrol.PaymentCall{}, testcontrol.DirectiveSucceeded))
		if token, err := nilClient.PaymentEntry(ctx, clientTestOrderA); err == nil || token != (testcontrol.PaymentCall{}) {
			t.Fatal("nil client payment entry returned a usable token")
		}
		var zero testcontrol.Client
		clientTestRequireFault(t, zero.MockCommitted(ctx, testcontrol.PaymentCall{}, testcontrol.DirectiveSucceeded))
		if token, err := zero.PaymentEntry(ctx, clientTestOrderA); err == nil || token != (testcontrol.PaymentCall{}) {
			t.Fatal("zero client payment entry returned a usable token")
		}
		if directive, ok := (testcontrol.PaymentCall{}).Directive(); ok || directive != "" {
			t.Fatal("zero payment call reported a directive")
		}
	})

	t.Run("token_owner_is_enforced", func(t *testing.T) {
		obs, socket := clientTestStartObserver(t, 1, 2)
		clientOne := clientTestDial(t, socket, 1)
		clientTwo := clientTestDial(t, socket, 2)
		token, _ := clientTestHoldPaymentEntry(t, obs, clientOne, clientTestOrderA)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		clientTestRequireFault(t, clientTwo.MockCommitted(ctx, token, testcontrol.DirectiveSucceeded))
		clientTestAssertSealFault(t, clientTwo)
	})

	t.Run("token_is_single_use", func(t *testing.T) {
		obs, socket := clientTestStartObserver(t, 1)
		client := clientTestDial(t, socket, 1)
		token, _ := clientTestHoldPaymentEntry(t, obs, client, clientTestOrderA)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		if err := client.MockCommitted(ctx, token, testcontrol.DirectiveSucceeded); err != nil {
			t.Fatal("first mock committed proof failed")
		}
		clientTestRequireFault(t, client.MockCommitted(ctx, token, testcontrol.DirectiveSucceeded))
		clientTestAssertSealFault(t, client)
	})

	t.Run("token_directive_is_a_value_copy", func(t *testing.T) {
		obs, socket := clientTestStartObserver(t, 1)
		client := clientTestDial(t, socket, 1)
		token, _ := clientTestHoldPaymentEntry(t, obs, client, clientTestOrderA)
		directive, ok := token.Directive()
		if !ok || directive != testcontrol.DirectiveSucceeded {
			t.Fatal("payment token directive mismatch")
		}
		directive = testcontrol.DirectiveFailed
		again, ok := token.Directive()
		if !ok || again != testcontrol.DirectiveSucceeded {
			t.Fatal("payment token directive was not a value copy")
		}
	})

	t.Run("actual_result_is_propagated_not_expected", func(t *testing.T) {
		var mu sync.Mutex
		var callSeq uint64
		peer := clientTestStartPeer(t, func(p *clientTestPeer, index int, conn *net.UnixConn) {
			request, _, ok := p.nextRequest(t, conn)
			if !ok {
				return
			}
			switch index {
			case 1:
				if request.Type != testcontrol.TypeBegin {
					t.Errorf("scripted test peer expected a begin frame")
					return
				}
				p.writeFrame(t, conn, clientTestAckFor(request))
			case 2:
				if request.Type != testcontrol.TypeCall || request.Kind != testcontrol.KindPayment {
					t.Errorf("scripted test peer expected a payment call frame")
					return
				}
				mu.Lock()
				callSeq = request.Seq
				mu.Unlock()
				ack := clientTestAckFor(request)
				directive := testcontrol.DirectiveSucceeded
				ack.Directive = &directive
				p.writeFrame(t, conn, ack)
			case 3:
				mu.Lock()
				wantCallSeq := callSeq
				mu.Unlock()
				if request.Type != testcontrol.TypeReach ||
					request.Point != testcontrol.PointMockCommitted ||
					request.PaymentCallSeq == nil || *request.PaymentCallSeq != wantCallSeq ||
					request.AppliedResult == nil || *request.AppliedResult != testcontrol.DirectiveFailed {
					t.Errorf("scripted test peer saw a substituted or incomplete proof")
					return
				}
				p.writeFrame(t, conn, clientTestAckFor(request))
			default:
				t.Errorf("client made an unexpected transport attempt")
			}
		})

		client := clientTestDialPeer(t, peer, 1)
		peer.expectAccepted(t, 1)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		token, err := client.PaymentEntry(ctx, clientTestOrderA)
		if err != nil {
			t.Fatal("payment entry through scripted peer failed")
		}
		peer.expectAccepted(t, 2)
		if err := client.MockCommitted(ctx, token, testcontrol.DirectiveFailed); err != nil {
			t.Fatal("actual result was not accepted by the scripted peer")
		}
		peer.expectAccepted(t, 3)
		peer.requireNoMoreAccepted(t)
		clientTestCloseOK(t, client)
	})

	t.Run("same_payload_transport_retry", func(t *testing.T) {
		var mu sync.Mutex
		var retryFrames [2][]byte
		peer := clientTestStartPeer(t, func(p *clientTestPeer, index int, conn *net.UnixConn) {
			request, raw, ok := p.nextRequest(t, conn)
			if !ok {
				return
			}
			switch index {
			case 1:
				if request.Type != testcontrol.TypeBegin {
					t.Errorf("scripted test peer expected a begin frame")
					return
				}
				p.writeFrame(t, conn, clientTestAckFor(request))
			case 2:
				if request.Type != testcontrol.TypeCall || request.Kind != testcontrol.KindPayment {
					t.Errorf("scripted test peer expected a payment call frame")
					return
				}
				ack := clientTestAckFor(request)
				directive := testcontrol.DirectiveSucceeded
				ack.Directive = &directive
				p.writeFrame(t, conn, ack)
			case 3, 4:
				if request.Type != testcontrol.TypeReach ||
					request.Point != testcontrol.PointMockCommitted ||
					request.PaymentCallSeq == nil ||
					request.AppliedResult == nil || *request.AppliedResult != testcontrol.DirectiveFailed {
					t.Errorf("scripted test peer expected a complete proof retry")
					return
				}
				mu.Lock()
				retryFrames[index-3] = append([]byte(nil), raw...)
				mu.Unlock()
				if index == 4 {
					p.writeFrame(t, conn, clientTestAckFor(request))
				}
			default:
				t.Errorf("client exceeded the transport attempt budget")
			}
		})

		client := clientTestDialPeer(t, peer, 1)
		peer.expectAccepted(t, 1)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		token, err := client.PaymentEntry(ctx, clientTestOrderA)
		if err != nil {
			t.Fatal("payment entry through scripted peer failed")
		}
		peer.expectAccepted(t, 2)
		if err := client.MockCommitted(ctx, token, testcontrol.DirectiveFailed); err != nil {
			t.Fatal("mock committed retry through scripted peer failed")
		}
		peer.expectAccepted(t, 3)
		peer.expectAccepted(t, 4)
		peer.requireNoMoreAccepted(t)
		mu.Lock()
		same := retryFrames[0] != nil && retryFrames[1] != nil &&
			bytes.Equal(retryFrames[0], retryFrames[1])
		mu.Unlock()
		if !same {
			t.Fatal("proof retry did not reuse the original frame")
		}
		clientTestCloseOK(t, client)
	})

	t.Run("legacy_enter_must_not_drop_directive", func(t *testing.T) {
		obs, socket := clientTestStartObserver(t, 1)
		client := clientTestDial(t, socket, 1)
		hold, err := obs.HoldPaymentEntry(1)
		if err != nil {
			t.Fatal("hold payment entry failed")
		}
		enterDone := make(chan error, 1)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		go func() {
			enterDone <- client.Enter(ctx, testcontrol.KindPayment, clientTestOrderA)
		}()
		if _, err := hold.Wait(ctx); err != nil {
			t.Fatal("payment hold wait failed")
		}
		if err := hold.Release(testcontrol.DirectiveSucceeded); err != nil {
			t.Fatal("payment hold release failed")
		}
		select {
		case err := <-enterDone:
			clientTestRequireFault(t, err)
		case <-time.After(clientTestBudget):
			t.Fatal("legacy payment enter did not return")
		}
		clientTestAssertSealFault(t, client)
	})

	t.Run("query_directive_is_rejected_by_client", func(t *testing.T) {
		peer := clientTestStartPeer(t, func(p *clientTestPeer, index int, conn *net.UnixConn) {
			request, _, ok := p.nextRequest(t, conn)
			if !ok {
				return
			}
			switch index {
			case 1:
				if request.Type != testcontrol.TypeBegin {
					t.Errorf("scripted test peer expected a begin frame")
					return
				}
				p.writeFrame(t, conn, clientTestAckFor(request))
			case 2:
				if request.Type != testcontrol.TypeCall || request.Kind != testcontrol.KindQuery {
					t.Errorf("scripted test peer expected a query call frame")
					return
				}
				ack := clientTestAckFor(request)
				directive := testcontrol.DirectiveSucceeded
				ack.Directive = &directive
				p.writeFrame(t, conn, ack)
			default:
				t.Errorf("client retried a query directive rejection")
			}
		})

		client := clientTestDialPeer(t, peer, 1)
		peer.expectAccepted(t, 1)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		clientTestRequireFault(t, client.Enter(ctx, testcontrol.KindQuery, clientTestOrderA))
		peer.expectAccepted(t, 2)
		peer.requireNoMoreAccepted(t)
		clientTestAssertSealFault(t, client)
	})

	t.Run("token_without_directive_uses_bare_reach", func(t *testing.T) {
		obs, socket := clientTestStartObserver(t, 1)
		client := clientTestDial(t, socket, 1)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		token, err := client.PaymentEntry(ctx, clientTestOrderA)
		if err != nil {
			t.Fatal("payment entry without a hold failed")
		}
		if directive, ok := token.Directive(); ok || directive != "" {
			t.Fatal("payment token without a hold reported a directive")
		}
		if err := client.MockCommitted(ctx, token, testcontrol.DirectiveSucceeded); err != nil {
			t.Fatal("legacy bare committed reach failed")
		}
		receipt := clientTestSealOK(t, client)
		if receipt.LastSeq != 2 {
			t.Fatal("legacy payment flow did not seal two records")
		}
		counts, err := obs.Snapshot()
		if err != nil {
			t.Fatal("observer snapshot after legacy bare reach failed")
		}
		if counts.Payment != 1 {
			t.Fatal("legacy bare reach payment count mismatch")
		}
	})

	t.Run("transport_disconnect_latches_seal", func(t *testing.T) {
		peer := clientTestStartPeer(t, func(p *clientTestPeer, index int, conn *net.UnixConn) {
			request, _, ok := p.nextRequest(t, conn)
			if !ok {
				return
			}
			switch index {
			case 1:
				if request.Type != testcontrol.TypeBegin {
					t.Errorf("scripted test peer expected a begin frame")
					return
				}
				p.writeFrame(t, conn, clientTestAckFor(request))
			case 2:
				if request.Type != testcontrol.TypeCall || request.Kind != testcontrol.KindPayment {
					t.Errorf("scripted test peer expected a payment call frame")
					return
				}
				ack := clientTestAckFor(request)
				directive := testcontrol.DirectiveSucceeded
				ack.Directive = &directive
				p.writeFrame(t, conn, ack)
			case 3, 4:
				if request.Type != testcontrol.TypeReach ||
					request.Point != testcontrol.PointMockCommitted ||
					request.PaymentCallSeq == nil || request.AppliedResult == nil {
					t.Errorf("scripted test peer expected a complete proof")
					return
				}
				// Drop both attempts without a response.
			default:
				t.Errorf("client exceeded the transport attempt budget")
			}
		})

		client := clientTestDialPeer(t, peer, 1)
		peer.expectAccepted(t, 1)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		token, err := client.PaymentEntry(ctx, clientTestOrderA)
		if err != nil {
			t.Fatal("payment entry through scripted peer failed")
		}
		peer.expectAccepted(t, 2)
		clientTestRequireFault(t, client.MockCommitted(ctx, token, testcontrol.DirectiveSucceeded))
		peer.expectAccepted(t, 3)
		peer.expectAccepted(t, 4)
		peer.requireNoMoreAccepted(t)
		clientTestAssertSealFault(t, client)
	})

	t.Run("invalid_order_faults", func(t *testing.T) {
		_, socket := clientTestStartObserver(t, 1)
		client := clientTestDial(t, socket, 1)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		if token, err := client.PaymentEntry(ctx, clientTestCanary); err == nil || token != (testcontrol.PaymentCall{}) {
			t.Fatal("payment entry accepted an invalid order")
		}
		clientTestAssertSealFault(t, client)
	})
}
