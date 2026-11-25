//go:build paymenthub_benchmark

package app

import (
	"fmt"
	"io"

	"github.com/jackc/pgx/v5/pgxpool"
)

// benchmarkRuntimeDatabaseMaxConns is the fixed pool size for the repeatable
// 200 RPS benchmark build. It is deliberately not a general default.
const benchmarkRuntimeDatabaseMaxConns int32 = 16

func init() {
	runtimePoolConfigure = func(config *pgxpool.Config) {
		if config == nil {
			return
		}
		config.MaxConns = benchmarkRuntimeDatabaseMaxConns
	}
	runtimePoolEmit = func(effectiveMaxConns int32, writer io.Writer) {
		if writer == nil {
			return
		}
		_, _ = fmt.Fprintf(
			writer,
			"POOL_CONFIG {\"configured_max_conns\":%d,\"max_conns\":%d}\n",
			benchmarkRuntimeDatabaseMaxConns,
			effectiveMaxConns,
		)
	}
}
