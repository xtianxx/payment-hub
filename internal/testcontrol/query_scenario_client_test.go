package testcontrol_test

import (
	"context"
	"errors"
	"path/filepath"
	"testing"

	"payment-hub/internal/testcontrol"
	"payment-hub/tests/harness"
)

// TestQueryScenarioClient freezes the v2 query call token, exactly-once
// application proof, and seal boundary. It uses the real harness Observer; the
// only fake values are fixed identities.

func queryClientStartObserver(t *testing.T, version int, generations ...uint64) (*harness.Observer, string) {
	t.Helper()
	dir := clientTestNewDir(t)
	socket := filepath.Join(dir, "s")
	ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancel()
	obs, err := harness.StartObserver(ctx, harness.ObserverConfig{
		Socket:         socket,
		RunID:          clientTestRunID,
		Epoch:          clientTestEpoch,
		BarrierTimeout: clientTestBudget,
		Version:        version,
	})
	if err != nil {
		t.Fatal("start real observer for query client test")
	}
	t.Cleanup(func() {
		closeCtx, closeCancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer closeCancel()
		_ = obs.Close(closeCtx)
	})
	for _, generation := range generations {
		if err := obs.Expect(generation); err != nil {
			t.Fatal("expect query client generation")
		}
	}
	return obs, socket
}

func queryClientDial(t *testing.T, socket string, version int, generation uint64) *testcontrol.Client {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
	defer cancel()
	client, err := testcontrol.Dial(ctx, testcontrol.Session{
		Socket:     socket,
		RunID:      clientTestRunID,
		Epoch:      clientTestEpoch,
		Generation: generation,
		Version:    version,
	})
	if err != nil {
		t.Fatal("dial query client failed")
	}
	t.Cleanup(func() {
		_ = client.Close()
	})
	return client
}

func queryClientAwaitEntry(t *testing.T, ch <-chan queryClientEntryResult, ctx context.Context) queryClientEntryResult {
	t.Helper()
	select {
	case result := <-ch:
		return result
	case <-ctx.Done():
		t.Fatal("query entry did not complete")
		return queryClientEntryResult{}
	}
}

type queryClientEntryResult struct {
	token testcontrol.QueryCall
	err   error
}

func TestQueryScenarioClient(t *testing.T) {
	t.Run("v2_hold_directive_apply_and_seal", func(t *testing.T) {
		obs, socket := queryClientStartObserver(t, testcontrol.Version2, 1)
		client := queryClientDial(t, socket, testcontrol.Version2, 1)
		hold, err := obs.HoldQueryEntry(1)
		if err != nil {
			t.Fatal("hold query entry failed")
		}

		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		entry := make(chan queryClientEntryResult, 1)
		go func() {
			token, err := client.QueryEntry(ctx, clientTestOrderA)
			entry <- queryClientEntryResult{token: token, err: err}
		}()
		binding, err := hold.Wait(ctx)
		if err != nil {
			t.Fatal("query hold wait failed")
		}
		if binding.RunID != clientTestRunID || binding.Epoch != clientTestEpoch ||
			binding.Generation != 1 || binding.CallSeq != 1 ||
			binding.OrderID != clientTestOrderA {
			t.Fatal("query binding identity mismatch")
		}
		if err := hold.Release(testcontrol.QueryScenarioFailure); err != nil {
			t.Fatal("query hold release failed")
		}
		result := queryClientAwaitEntry(t, entry, ctx)
		if result.err != nil {
			t.Fatal("query entry failed")
		}
		scenario, ok := result.token.Directive()
		if !ok || scenario != testcontrol.QueryScenarioFailure {
			t.Fatal("query entry directive mismatch")
		}
		if err := client.QueryApplied(ctx, result.token, testcontrol.QueryOutcomeUnavailable); err != nil {
			t.Fatal("query applied proof failed")
		}
		receipt, err := client.Seal(ctx)
		if err != nil {
			t.Fatal("seal after query proof failed")
		}
		if receipt.LastSeq != 2 {
			t.Fatal("query receipt sequence mismatch")
		}
		sealedReceipt, counts, ok := obs.SealedGeneration(1)
		if !ok || sealedReceipt != receipt || counts != (harness.Counts{Query: 1}) {
			t.Fatal("sealed generation proof mismatch")
		}
	})

	t.Run("normal_query_still_requires_applied_proof", func(t *testing.T) {
		obs, socket := queryClientStartObserver(t, testcontrol.Version2, 1)
		client := queryClientDial(t, socket, testcontrol.Version2, 1)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		token, err := client.QueryEntry(ctx, clientTestOrderA)
		if err != nil {
			t.Fatal("normal query entry failed")
		}
		if _, ok := token.Directive(); ok {
			t.Fatal("normal query entry unexpectedly carried a directive")
		}
		if _, err := client.Seal(ctx); !errors.Is(err, testcontrol.ErrControlFault) {
			t.Fatal("seal accepted an unconsumed query token")
		}
		if _, _, ok := obs.SealedGeneration(1); ok {
			t.Fatal("unconsumed query generation reported sealed")
		}
	})

	t.Run("v1_client_has_no_query_control", func(t *testing.T) {
		_, socket := queryClientStartObserver(t, testcontrol.Version, 1)
		client := queryClientDial(t, socket, testcontrol.Version, 1)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		if _, err := client.QueryEntry(ctx, clientTestOrderA); !errors.Is(err, testcontrol.ErrControlFault) {
			t.Fatal("v1 client accepted a query-control entry")
		}
	})

	t.Run("v2_client_requires_v2_observer_negotiation", func(t *testing.T) {
		_, socket := queryClientStartObserver(t, testcontrol.Version, 1)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		client, err := testcontrol.Dial(ctx, testcontrol.Session{
			Socket:     socket,
			RunID:      clientTestRunID,
			Epoch:      clientTestEpoch,
			Generation: 1,
			Version:    testcontrol.Version2,
		})
		if client != nil {
			_ = client.Close()
		}
		if !errors.Is(err, testcontrol.ErrControlFault) {
			t.Fatal("v2 client dialed a v1 observer")
		}
	})

	t.Run("duplicate_apply_fails_and_blocks_seal", func(t *testing.T) {
		_, socket := queryClientStartObserver(t, testcontrol.Version2, 1)
		client := queryClientDial(t, socket, testcontrol.Version2, 1)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		token, err := client.QueryEntry(ctx, clientTestOrderA)
		if err != nil {
			t.Fatal("query entry failed")
		}
		if err := client.QueryApplied(ctx, token, testcontrol.QueryOutcomeFound); err != nil {
			t.Fatal("first query applied failed")
		}
		if err := client.QueryApplied(ctx, token, testcontrol.QueryOutcomeFound); !errors.Is(err, testcontrol.ErrControlFault) {
			t.Fatal("duplicate query applied was accepted")
		}
		if _, err := client.Seal(ctx); !errors.Is(err, testcontrol.ErrControlFault) {
			t.Fatal("seal recovered after duplicate query apply")
		}
	})

	t.Run("cross_client_and_cross_order_token_rejected", func(t *testing.T) {
		obs, socket := queryClientStartObserver(t, testcontrol.Version2, 1, 2, 3)
		first := queryClientDial(t, socket, testcontrol.Version2, 1)
		second := queryClientDial(t, socket, testcontrol.Version2, 2)
		third := queryClientDial(t, socket, testcontrol.Version2, 3)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		firstToken, err := first.QueryEntry(ctx, clientTestOrderA)
		if err != nil {
			t.Fatal("first query entry failed")
		}
		secondToken, err := second.QueryEntry(ctx, clientTestOrderB)
		if err != nil {
			t.Fatal("second query entry failed")
		}
		thirdToken, err := third.QueryEntry(ctx, clientTestOrderA)
		if err != nil {
			t.Fatal("third query entry failed")
		}
		if err := third.QueryApplied(ctx, secondToken, testcontrol.QueryOutcomeFound); !errors.Is(err, testcontrol.ErrControlFault) {
			t.Fatal("cross-client query token was accepted")
		}
		if err := third.QueryApplied(ctx, thirdToken, testcontrol.QueryOutcomeFound); !errors.Is(err, testcontrol.ErrControlFault) {
			t.Fatal("poisoned cross-token client recovered")
		}
		if _, err := third.Seal(ctx); !errors.Is(err, testcontrol.ErrControlFault) {
			t.Fatal("poisoned cross-token client sealed")
		}
		if err := first.QueryApplied(ctx, firstToken, testcontrol.QueryOutcomeFound); err != nil {
			t.Fatal("first own query token apply failed")
		}
		if err := second.QueryApplied(ctx, secondToken, testcontrol.QueryOutcomeFound); err != nil {
			t.Fatal("second own query token apply failed")
		}
		if _, err := first.Seal(ctx); err != nil {
			t.Fatal("first query seal failed")
		}
		if _, err := second.Seal(ctx); err != nil {
			t.Fatal("second query seal failed")
		}
		if _, counts, ok := obs.SealedGeneration(1); !ok || counts != (harness.Counts{Query: 1}) {
			t.Fatal("first sealed generation counts mismatch")
		}
		if _, counts, ok := obs.SealedGeneration(2); !ok || counts != (harness.Counts{Query: 1}) {
			t.Fatal("second sealed generation counts mismatch")
		}
	})

	t.Run("invalid_or_zero_token_rejected", func(t *testing.T) {
		_, socket := queryClientStartObserver(t, testcontrol.Version2, 1)
		client := queryClientDial(t, socket, testcontrol.Version2, 1)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		if err := client.QueryApplied(ctx, testcontrol.QueryCall{}, testcontrol.QueryOutcomeFound); !errors.Is(err, testcontrol.ErrControlFault) {
			t.Fatal("zero query token was accepted")
		}
		if _, err := client.Seal(ctx); !errors.Is(err, testcontrol.ErrControlFault) {
			t.Fatal("client recovered after zero query token")
		}

		_, socket = queryClientStartObserver(t, testcontrol.Version2, 1)
		client = queryClientDial(t, socket, testcontrol.Version2, 1)
		token, err := client.QueryEntry(ctx, clientTestOrderA)
		if err != nil {
			t.Fatal("query entry failed")
		}
		if err := client.QueryApplied(ctx, token, testcontrol.QueryOutcome("other")); !errors.Is(err, testcontrol.ErrControlFault) {
			t.Fatal("invalid query outcome was accepted")
		}
		if err := client.QueryApplied(ctx, token, testcontrol.QueryOutcomeFound); !errors.Is(err, testcontrol.ErrControlFault) {
			t.Fatal("client recovered after invalid query outcome")
		}
		if _, err := client.Seal(ctx); !errors.Is(err, testcontrol.ErrControlFault) {
			t.Fatal("client sealed after invalid query outcome")
		}
	})

	t.Run("query_entry_transport_retry_keeps_one_record", func(t *testing.T) {
		obs, socket := queryClientStartObserver(t, testcontrol.Version2, 1)
		client := queryClientDial(t, socket, testcontrol.Version2, 1)
		ctx, cancel := context.WithTimeout(context.Background(), clientTestBudget)
		defer cancel()
		token, err := client.QueryEntry(ctx, clientTestOrderA)
		if err != nil {
			t.Fatal("query entry failed")
		}
		// The client's transport retry reuses the same seq/event ID; the real
		// observer deduplicates rather than counting a second business query.
		if err := client.QueryApplied(ctx, token, testcontrol.QueryOutcomeFound); err != nil {
			t.Fatal("query applied failed")
		}
		if _, err := client.Seal(ctx); err != nil {
			t.Fatal("query seal failed")
		}
		if _, counts, ok := obs.SealedGeneration(1); !ok || counts != (harness.Counts{Query: 1}) {
			t.Fatal("query retry was counted more than once")
		}
	})
}
