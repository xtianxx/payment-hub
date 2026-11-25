//go:build !paymenthub_benchmark

package app

import (
	"bytes"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"
)

func TestBenchmarkPoolSeamIsInertWithoutBuildTag(t *testing.T) {
	config := &pgxpool.Config{MaxConns: 7}
	runtimePoolConfigure(config)
	if config.MaxConns != 7 {
		t.Fatalf("default build changed MaxConns to %d, want 7", config.MaxConns)
	}

	var output bytes.Buffer
	runtimePoolEmit(16, &output)
	if output.Len() != 0 {
		t.Fatalf("default build emitted %q, want no pool-config output", output.String())
	}
}
