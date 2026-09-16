package hookd

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"
)

// stallStart replaces the manager's spawn with one that blocks until the test
// releases it, then hands back worker and err. A second entry panics on the
// closed started channel: one key spawns once.
func stallStart(manager *workerManager, worker *workerClient, err error) (started, release chan struct{}) {
	started, release = make(chan struct{}), make(chan struct{})
	manager.start = func(ctx context.Context, key workerKey) (*workerClient, error) {
		close(started)
		select {
		case <-release:
			return worker, err
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	return started, release
}

// admissions ticks once per requester acquire admits, through the clock read
// every admission makes under the lock.
func admissions(manager *workerManager) <-chan struct{} {
	ticks := make(chan struct{}, maxLiveWorkers)
	manager.now = func() time.Time {
		ticks <- struct{}{}
		return time.Now()
	}
	return ticks
}

func cachedEntry(manager *workerManager, id string) *workerEntry {
	manager.mu.Lock()
	defer manager.mu.Unlock()
	return manager.entries[id]
}

type acquired struct {
	entry *workerEntry
	err   error
}

func acquireAsync(manager *workerManager, ctx context.Context, key workerKey) <-chan acquired {
	result := make(chan acquired, 1)
	go func() {
		entry, _, err := manager.acquire(ctx, key)
		result <- acquired{entry: entry, err: err}
	}()
	return result
}

// TestFirstRequesterLeavingDoesNotFailTheStartOthersWaitOn pins the live
// failure: a hook with a 200ms timeout started a fresh root's worker, its
// deadline tore the half-started child down, and every hook that had queued
// behind it failed with the handshake error. Startup belongs to the manager;
// a requester that leaves drops only its own hold.
func TestFirstRequesterLeavingDoesNotFailTheStartOthersWaitOn(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	worker, _ := silentWorker(t)
	started, release := stallStart(manager, worker, nil)
	admitted := admissions(manager)
	key := workerKey{id: "shared", root: "/live"}

	firstCtx, cancelFirst := context.WithCancel(t.Context())
	defer cancelFirst()
	first := acquireAsync(manager, firstCtx, key)
	<-started
	<-admitted
	second := acquireAsync(manager, t.Context(), key)
	<-admitted

	cancelFirst()
	if got := <-first; !errors.Is(got.err, context.Canceled) {
		t.Fatalf("first requester after cancelling = %v, want %v", got.err, context.Canceled)
	}
	close(release)

	got := <-second
	if got.err != nil {
		t.Fatalf("second requester = %v; the first requester's cancellation failed the start it was waiting on", got.err)
	}
	if got.entry.worker != worker || worker.broken() {
		t.Fatal("second requester did not get the worker the shared start produced")
	}
	manager.wg.Wait()
	entry := cachedEntry(manager, key.member())
	manager.mu.Lock()
	holds := entry.inflight
	manager.mu.Unlock()
	if entry != got.entry || holds != 1 {
		t.Fatalf("cached entry holds = %d, want 1 for the second requester alone", holds)
	}
}

// TestAbandonedStartStillLandsItsWorker pins what happens when every requester
// leaves before the interpreter is up: the start finishes on the manager's own
// lifetime and the result is cached idle for the next caller, or retired at
// once when the root is ephemeral — never left half-owned.
func TestAbandonedStartStillLandsItsWorker(t *testing.T) {
	t.Parallel()
	scratch, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name string
		root string
	}{
		{name: "cached", root: "/live"},
		{name: "ephemeral", root: scratch},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			manager := mustWorkerManager(t)
			worker, _ := silentWorker(t)
			started, release := stallStart(manager, worker, nil)
			key := workerKey{id: "abandoned", root: tc.root}

			ctx, cancel := context.WithCancel(t.Context())
			result := acquireAsync(manager, ctx, key)
			<-started
			cancel()
			if got := <-result; !errors.Is(got.err, context.Canceled) {
				t.Fatalf("abandoning requester = %v, want %v", got.err, context.Canceled)
			}
			close(release)
			manager.wg.Wait()

			entry := cachedEntry(manager, key.member())
			if tc.name == "ephemeral" {
				if entry != nil {
					t.Fatal("an ephemeral worker nobody waited for stayed cached")
				}
				if !worker.broken() {
					t.Fatal("an ephemeral worker nobody waited for was never retired")
				}
				return
			}
			if entry == nil || entry.worker != worker || !entry.idle() {
				t.Fatalf("abandoned start left entry %+v, want the worker cached idle", entry)
			}
			if worker.broken() {
				t.Fatal("a cached worker was retired with its entry still in the cache")
			}
		})
	}
}

// TestStartFailureReachesEveryWaiterAndFreesTheKey pins the retry path: a
// spawn that fails answers every requester queued on it and leaves no entry
// behind, so the next request for that key starts over.
func TestStartFailureReachesEveryWaiterAndFreesTheKey(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	spawnErr := errors.New("captain: spawn Python product worker: interpreter missing")
	started, release := stallStart(manager, nil, spawnErr)
	admitted := admissions(manager)
	key := workerKey{id: "failing", root: "/live"}

	first := acquireAsync(manager, t.Context(), key)
	<-started
	<-admitted
	second := acquireAsync(manager, t.Context(), key)
	<-admitted
	close(release)

	for _, result := range []<-chan acquired{first, second} {
		if got := <-result; !errors.Is(got.err, spawnErr) {
			t.Fatalf("waiter on a failed start = %v, want %v", got.err, spawnErr)
		}
	}
	manager.wg.Wait()
	if cachedEntry(manager, key.member()) != nil {
		t.Fatal("a failed start left its entry cached, so no later request can retry")
	}
}

// TestStartThatLandsBrokenIsNotCached pins the gap between startWorker
// returning and startEntry publishing: a child dying there is invisible to
// watch, so the landing itself must refuse to cache a dead worker.
func TestStartThatLandsBrokenIsNotCached(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	worker, serverConn := silentWorker(t)
	started, release := stallStart(manager, worker, nil)
	key := workerKey{id: "dead-on-arrival", root: "/live"}

	ctx, cancel := context.WithCancel(t.Context())
	result := acquireAsync(manager, ctx, key)
	<-started
	cancel()
	<-result
	_ = serverConn.Close()
	for !worker.broken() {
		time.Sleep(time.Millisecond)
	}
	close(release)
	manager.wg.Wait()

	if cachedEntry(manager, key.member()) != nil {
		t.Fatal("a worker whose child died before it was published stayed cached for the next requester")
	}
}

// TestCloseSettlesAWorkerThatFinishedStartingUnderIt pins the shutdown edge: a
// spawn that lands after Close snapshotted the cache is still settled before
// Close reports the product joined, and the requester waiting on it is told
// the manager closed rather than handed a worker on its way out.
func TestCloseSettlesAWorkerThatFinishedStartingUnderIt(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	worker, _ := silentWorker(t)
	started := make(chan struct{})
	manager.start = func(ctx context.Context, key workerKey) (*workerClient, error) {
		close(started)
		<-ctx.Done()
		return worker, nil
	}
	key := workerKey{id: "closing", root: "/live"}

	result := acquireAsync(manager, t.Context(), key)
	<-started

	closeCtx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	joined, err := manager.Close(closeCtx)
	if err != nil || !joined {
		t.Fatalf("Close = joined %t, err %v", joined, err)
	}
	if !worker.broken() {
		t.Fatal("a worker that finished starting under Close escaped settlement")
	}
	if got := <-result; !errors.Is(got.err, errWorkerManagerClosed) {
		t.Fatalf("requester waiting through Close = %v, want %v", got.err, errWorkerManagerClosed)
	}
}
