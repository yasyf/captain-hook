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

func awaitHolds(t *testing.T, manager *workerManager, member string, holds int) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		got := -1
		manager.mu.Lock()
		if entry := manager.entries[member]; entry != nil {
			got = entry.inflight
		}
		manager.mu.Unlock()
		if got == holds {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("holds on %s = %d, want %d", member, got, holds)
		}
		time.Sleep(time.Millisecond)
	}
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
	key := workerKey{id: "shared", root: "/live"}

	firstCtx, cancelFirst := context.WithCancel(t.Context())
	defer cancelFirst()
	first := acquireAsync(manager, firstCtx, key)
	<-started
	awaitHolds(t, manager, key.member(), 2)
	second := acquireAsync(manager, t.Context(), key)
	awaitHolds(t, manager, key.member(), 3)

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
	key := workerKey{id: "failing", root: "/live"}

	first := acquireAsync(manager, t.Context(), key)
	<-started
	awaitHolds(t, manager, key.member(), 2)
	second := acquireAsync(manager, t.Context(), key)
	awaitHolds(t, manager, key.member(), 3)
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

func TestAWaitedOnStartupOutlivesTheReadinessBound(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	clock := &fakeClock{now: time.Now()}
	manager.now = clock.Now
	worker, _ := silentWorker(t)
	started, release := stallStart(manager, worker, nil)
	key := workerKey{id: "slow-hello", root: "/live"}

	ctx, cancel := context.WithDeadline(t.Context(), clock.Now().Add(30*time.Second))
	defer cancel()
	result := acquireAsync(manager, ctx, key)
	<-started
	clock.Advance(workerReadinessTimeout + 3*time.Second)
	cachedEntry(manager, key.member()).startup.expire()
	close(release)

	got := <-result
	if got.err != nil {
		t.Fatalf("waiter = %v; the readiness bound tore down a startup its deadline still covered", got.err)
	}
	if got.entry.worker != worker || worker.broken() {
		t.Fatal("waiter did not get the worker whose hello outlasted the readiness bound")
	}
	manager.release(got.entry)
}

func TestAJoinerWithALaterDeadlineHoldsTheStartupOpen(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	clock := &fakeClock{now: time.Now()}
	manager.now, manager.readiness = clock.Now, 20*time.Second
	worker, _ := silentWorker(t)
	started, release := stallStart(manager, worker, nil)
	key := workerKey{id: "joined", root: "/live"}

	first := acquireAsync(manager, t.Context(), key)
	<-started
	awaitHolds(t, manager, key.member(), 2)
	laterCtx, cancel := context.WithDeadline(t.Context(), clock.Now().Add(40*time.Second))
	defer cancel()
	second := acquireAsync(manager, laterCtx, key)
	awaitHolds(t, manager, key.member(), 3)

	clock.Advance(30 * time.Second)
	cachedEntry(manager, key.member()).startup.expire()
	close(release)
	for _, result := range []<-chan acquired{first, second} {
		got := <-result
		if got.err != nil || got.entry.worker != worker {
			t.Fatalf("waiter = %+v, %v; the joiner's deadline did not hold the startup open", got.entry, got.err)
		}
		manager.release(got.entry)
	}
}

func TestAStartupNobodyHoldsADeadlineOnEndsAtTheReadinessBound(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	clock := &fakeClock{now: time.Now()}
	manager.now, manager.readiness = clock.Now, 0
	started, _ := stallStart(manager, nil, errors.New("unreachable"))
	key := workerKey{id: "unbounded", root: "/live"}

	result := acquireAsync(manager, t.Context(), key)
	<-started
	if got := <-result; !errors.Is(got.err, context.Canceled) {
		t.Fatalf("waiter without a deadline = %v, want the readiness bound to end the start", got.err)
	}
	manager.wg.Wait()
	if cachedEntry(manager, key.member()) != nil {
		t.Fatal("a start the readiness bound ended stayed cached")
	}
}

func TestCancellingTheLatestWaiterWithdrawsItsDeadline(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	clock := &fakeClock{now: time.Now()}
	manager.now = clock.Now
	started, _ := stallStart(manager, nil, errors.New("unreachable"))
	key := workerKey{id: "withdrawn", root: "/live"}

	firstCtx, cancelFirst := context.WithDeadline(t.Context(), clock.Now().Add(20*time.Second))
	defer cancelFirst()
	first := acquireAsync(manager, firstCtx, key)
	<-started
	awaitHolds(t, manager, key.member(), 2)
	laterCtx, cancelLater := context.WithDeadline(t.Context(), clock.Now().Add(40*time.Second))
	later := acquireAsync(manager, laterCtx, key)
	awaitHolds(t, manager, key.member(), 3)
	cancelLater()
	if got := <-later; !errors.Is(got.err, context.Canceled) {
		t.Fatalf("cancelled joiner = %v, want %v", got.err, context.Canceled)
	}

	clock.Advance(30 * time.Second)
	cachedEntry(manager, key.member()).startup.expire()
	if got := <-first; !errors.Is(got.err, context.Canceled) {
		t.Fatalf("waiter past its own deadline = %v, want the startup ended: the cancelled joiner's 40 s no longer holds it", got.err)
	}
	manager.wg.Wait()
	if cachedEntry(manager, key.member()) != nil {
		t.Fatal("a startup held only by a cancelled waiter's deadline stayed cached")
	}
}

func TestCancellingEveryWaiterEndsTheStartupAtReadiness(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	clock := &fakeClock{now: time.Now()}
	manager.now = clock.Now
	started, _ := stallStart(manager, nil, errors.New("unreachable"))
	key := workerKey{id: "abandoned-deadlines", root: "/live"}

	ctx, cancel := context.WithDeadline(t.Context(), clock.Now().Add(time.Hour))
	first := acquireAsync(manager, ctx, key)
	<-started
	awaitHolds(t, manager, key.member(), 2)
	second := acquireAsync(manager, ctx, key)
	awaitHolds(t, manager, key.member(), 3)
	cancel()
	for _, result := range []<-chan acquired{first, second} {
		if got := <-result; !errors.Is(got.err, context.Canceled) {
			t.Fatalf("cancelled waiter = %v, want %v", got.err, context.Canceled)
		}
	}
	awaitHolds(t, manager, key.member(), 1)

	clock.Advance(workerReadinessTimeout + time.Second)
	cachedEntry(manager, key.member()).startup.expire()
	manager.wg.Wait()
	if cachedEntry(manager, key.member()) != nil {
		t.Fatal("a startup every waiter abandoned outlived the readiness bound")
	}
}

type holdingClock struct {
	fakeClock
	calls    int
	holdCall int
	held     chan struct{}
	release  chan struct{}
}

func (c *holdingClock) Now() time.Time {
	c.mu.Lock()
	c.calls++
	hold := c.calls == c.holdCall
	c.mu.Unlock()
	if hold {
		close(c.held)
		<-c.release
	}
	return c.fakeClock.Now()
}

func TestTheFirstWaiterHoldsTheMemberStartupFromItsConstruction(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	clock := &holdingClock{fakeClock: fakeClock{now: time.Now()}, holdCall: 3, held: make(chan struct{}), release: make(chan struct{})}
	manager.now, manager.readiness = clock.Now, 0
	worker, _ := silentWorker(t)
	started, releaseStart := stallStart(manager, worker, nil)
	key := workerKey{id: "held-construction", root: "/live"}

	ctx, cancel := context.WithDeadline(t.Context(), clock.Now().Add(30*time.Second))
	defer cancel()
	result := acquireAsync(manager, ctx, key)
	<-clock.held
	clock.Advance(workerReadinessTimeout + 3*time.Second)
	close(clock.release)
	<-started
	cachedEntry(manager, key.member()).startup.expire()
	close(releaseStart)

	got := <-result
	if got.err != nil {
		t.Fatalf("waiter = %v; the readiness timer fired before its deadline was registered", got.err)
	}
	if got.entry.worker != worker || worker.broken() {
		t.Fatal("waiter did not get the worker whose startup it held from construction")
	}
	manager.release(got.entry)
}
