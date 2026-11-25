//go:build paymenthub_benchmark

package app

import (
	"bytes"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"
)

func TestBenchmarkPoolSeamPinsConnectionsWithBuildTag(t *testing.T) {
	config := &pgxpool.Config{MaxConns: 7}
	runtimePoolConfigure(config)
	if config.MaxConns != benchmarkRuntimeDatabaseMaxConns {
		t.Fatalf(
			"benchmark build MaxConns = %d, want %d",
			config.MaxConns,
			benchmarkRuntimeDatabaseMaxConns,
		)
	}

	var output bytes.Buffer
	runtimePoolEmit(16, &output)
	want := "POOL_CONFIG {\"configured_max_conns\":16,\"max_conns\":16}\n"
	if output.String() != want {
		t.Fatalf("benchmark pool-config output = %q, want %q", output.String(), want)
	}
}

func TestBenchmarkPoolSeamToleratesNilConfig(t *testing.T) {
	runtimePoolConfigure(nil)
}
