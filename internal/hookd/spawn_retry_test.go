package hookd

import (
	"context"
	"errors"
	"fmt"
	"syscall"
	"testing"
	"time"

	"github.com/yasyf/daemonkit"
)

type refusingSpawner struct {
	refusals int
	err      error
	calls    int
	child    *daemonkit.Child
}

func (s *refusingSpawner) spawn() (*daemonkit.Child, error) {
	s.calls++
	if s.calls <= s.refusals {
		return nil, fmt.Errorf("posix_spawn python: %w", s.err)
	}
	return s.child, nil
}

func quickBackoff(n int) []time.Duration {
	backoff := make([]time.Duration, n)
	for i := range backoff {
		backoff[i] = time.Millisecond
	}
	return backoff
}

func TestSpawnPastEAGAINRetriesUntilTheKernelAdmitsTheSpawn(t *testing.T) {
	t.Parallel()
	spawner := &refusingSpawner{refusals: 3, err: syscall.EAGAIN, child: &daemonkit.Child{}}

	child, err := spawnPastEAGAIN(t.Context(), quickBackoff(5), spawner.spawn)

	if err != nil || child != spawner.child || spawner.calls != 4 {
		t.Fatalf("spawnPastEAGAIN() = %p, %v after %d calls; want the child after 4", child, err, spawner.calls)
	}
}

func TestSpawnPastEAGAINGivesUpAfterItsBackoff(t *testing.T) {
	t.Parallel()
	spawner := &refusingSpawner{refusals: 100, err: syscall.EAGAIN}

	_, err := spawnPastEAGAIN(t.Context(), quickBackoff(5), spawner.spawn)

	if !errors.Is(err, syscall.EAGAIN) || spawner.calls != 6 {
		t.Fatalf("spawnPastEAGAIN() = %v after %d calls; want EAGAIN after 6", err, spawner.calls)
	}
}

func TestSpawnPastEAGAINReturnsOtherFailuresAtOnce(t *testing.T) {
	t.Parallel()
	spawner := &refusingSpawner{refusals: 100, err: syscall.ENOENT}

	_, err := spawnPastEAGAIN(t.Context(), quickBackoff(5), spawner.spawn)

	if !errors.Is(err, syscall.ENOENT) || spawner.calls != 1 {
		t.Fatalf("spawnPastEAGAIN() = %v after %d calls; want ENOENT after 1", err, spawner.calls)
	}
}

func TestSpawnPastEAGAINStopsAtTheCallersDeadline(t *testing.T) {
	t.Parallel()
	spawner := &refusingSpawner{refusals: 100, err: syscall.EAGAIN}
	ctx, cancel := context.WithCancel(t.Context())
	cancel()

	_, err := spawnPastEAGAIN(ctx, []time.Duration{time.Hour}, spawner.spawn)

	if !errors.Is(err, syscall.EAGAIN) || !errors.Is(err, context.Canceled) || spawner.calls != 1 {
		t.Fatalf("spawnPastEAGAIN() = %v after %d calls; want EAGAIN and the cancellation after 1", err, spawner.calls)
	}
}

func TestSpawnBackoffFitsInsideWorkerReadiness(t *testing.T) {
	t.Parallel()
	var total time.Duration
	for _, delay := range spawnBackoff {
		total += delay
	}
	if total >= workerReadinessTimeout/2 {
		t.Fatalf("spawn backoff totals %s; want it well inside the %s readiness budget", total, workerReadinessTimeout)
	}
}
