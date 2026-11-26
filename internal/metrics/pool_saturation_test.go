package metrics

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

// saturatingPool mirrors the pgxpool/puddle counter semantics the collector
// reads: an acquire that finds no idle slot waits, then records one empty
// acquire and its wait time. It lets the saturation path run without opening a
// database connection; the real *pgxpool.Stat mapping is covered separately.
type saturatingPool struct {
	max   int32
	slots chan struct{}

	mu              sync.Mutex
	total           int32
	idle            int32
	acquired        int32
	constructing    int32
	acquires        int64
	empty           int64
	canceled        int64
	created         int64
	destroyedIdle   int64
	destroyedLife   int64
	waiting         int32
	acquireDuration time.Duration
}

func newSaturatingPool(max int32) *saturatingPool {
	return &saturatingPool{max: max, slots: make(chan struct{}, max)}
}

func (pool *saturatingPool) acquire(ctx context.Context) error {
	start := time.Now()
	waited := false
	select {
	case pool.slots <- struct{}{}:
	default:
		waited = true
		pool.mu.Lock()
		pool.waiting++
		pool.mu.Unlock()
		select {
		case pool.slots <- struct{}{}:
		case <-ctx.Done():
			pool.mu.Lock()
			pool.waiting--
			pool.canceled++
			pool.mu.Unlock()
			return ctx.Err()
		}
		pool.mu.Lock()
		pool.waiting--
		pool.mu.Unlock()
	}
	elapsed := time.Since(start)
	pool.mu.Lock()
	if waited {
		pool.empty++
	}
	if pool.idle > 0 {
		pool.idle--
	} else {
		pool.total++
	}
	pool.acquired++
	pool.acquires++
	pool.acquireDuration += elapsed
	pool.mu.Unlock()
	return nil
}

func (pool *saturatingPool) release() {
	<-pool.slots
	pool.mu.Lock()
	pool.acquired--
	pool.idle++
	pool.mu.Unlock()
}

func (pool *saturatingPool) MaxConns() int32 { return pool.max }
func (pool *saturatingPool) TotalConns() int32 {
	pool.mu.Lock()
	defer pool.mu.Unlock()
	return pool.total
}
func (pool *saturatingPool) IdleConns() int32 {
	pool.mu.Lock()
	defer pool.mu.Unlock()
	return pool.idle
}
func (pool *saturatingPool) AcquiredConns() int32 {
	pool.mu.Lock()
	defer pool.mu.Unlock()
	return pool.acquired
}
func (pool *saturatingPool) ConstructingConns() int32 {
	pool.mu.Lock()
	defer pool.mu.Unlock()
	return pool.constructing
}
func (pool *saturatingPool) AcquireCount() int64 {
	pool.mu.Lock()
	defer pool.mu.Unlock()
	return pool.acquires
}
func (pool *saturatingPool) AcquireDuration() time.Duration {
	pool.mu.Lock()
	defer pool.mu.Unlock()
	return pool.acquireDuration
}
func (pool *saturatingPool) EmptyAcquireCount() int64 {
	pool.mu.Lock()
	defer pool.mu.Unlock()
	return pool.empty
}
func (pool *saturatingPool) CanceledAcquireCount() int64 {
	pool.mu.Lock()
	defer pool.mu.Unlock()
	return pool.canceled
}
func (pool *saturatingPool) NewConnsCount() int64 {
	pool.mu.Lock()
	defer pool.mu.Unlock()
	return pool.created
}
func (pool *saturatingPool) MaxIdleDestroyCount() int64 {
	pool.mu.Lock()
	defer pool.mu.Unlock()
	return pool.destroyedIdle
}
func (pool *saturatingPool) MaxLifetimeDestroyCount() int64 {
	pool.mu.Lock()
	defer pool.mu.Unlock()
	return pool.destroyedLife
}
func (pool *saturatingPool) WaitingCount() int32 {
	pool.mu.Lock()
	defer pool.mu.Unlock()
	return pool.waiting
}

func waitForCondition(t *testing.T, description string, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", description)
}

func TestPoolMetricsExposeSaturation(t *testing.T) {
	pool := newSaturatingPool(2)
	ctx := context.Background()
	for index := 0; index < 2; index++ {
		if err := pool.acquire(ctx); err != nil {
			t.Fatalf("acquire %d: %v", index, err)
		}
	}

	registry := prometheus.NewRegistry()
	NewPoolMetrics(registry, func() PoolStats { return pool })

	thirdDone := make(chan error, 1)
	go func() { thirdDone <- pool.acquire(context.Background()) }()

	waitForCondition(t, "third acquire to wait", func() bool { return pool.WaitingCount() == 1 })

	waiting := gatherPoolValues(t, registry)
	if got := waiting["payment_hub_db_pool_connections_in_use"]; got != 2 {
		t.Fatalf("in-use while waiting = %v, want 2", got)
	}
	if got := waiting["payment_hub_db_pool_empty_acquires_total"]; got != 0 {
		t.Fatalf("empty acquires while still waiting = %v, want 0", got)
	}
	if got := waiting["payment_hub_db_pool_acquires_total"]; got != 2 {
		t.Fatalf("acquires while waiting = %v, want 2", got)
	}

	pool.release()
	select {
	case err := <-thirdDone:
		if err != nil {
			t.Fatalf("third acquire: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("third acquire did not finish after release")
	}

	settled := gatherPoolValues(t, registry)
	if got := settled["payment_hub_db_pool_empty_acquires_total"]; got != 1 {
		t.Fatalf("empty acquires = %v, want 1", got)
	}
	if got := settled["payment_hub_db_pool_acquires_total"]; got != 3 {
		t.Fatalf("acquires = %v, want 3", got)
	}
	if got := settled["payment_hub_db_pool_connections_max"]; got != 2 {
		t.Fatalf("max connections = %v, want 2", got)
	}
	if got := settled["payment_hub_db_pool_connections"]; got != 2 {
		t.Fatalf("total connections = %v, want 2", got)
	}
	if got := settled["payment_hub_db_pool_connections_idle"]; got != 0 {
		t.Fatalf("idle connections = %v, want 0", got)
	}

	count := pool.AcquireCount()
	duration := pool.AcquireDuration()
	if count != 3 || duration <= 0 {
		t.Fatalf("acquire count/duration = %d/%v, want 3 and >0", count, duration)
	}
	if average := duration.Seconds() / float64(count); average <= 0 {
		t.Fatalf("average acquire wait = %v, want >0", average)
	}
	if got := settled["payment_hub_db_pool_acquire_duration_seconds_total"]; got <= 0 {
		t.Fatalf("acquire duration metric = %v, want >0", got)
	}
}
