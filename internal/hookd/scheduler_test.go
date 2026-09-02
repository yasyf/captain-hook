package hookd

import (
	"context"
	"errors"
	"fmt"
	"sync/atomic"
	"testing"
	"time"
)

// blockingRun holds every admitted dispatch inside execute until release closes,
// recording how many ran at once so a test can assert the admitted width.
type blockingRun struct {
	scheduler *scheduler
	active    atomic.Int32
	maximum   atomic.Int32
	entered   atomic.Int32
	release   chan struct{}
	done      chan struct{}
}

func newBlockingRun(s *scheduler) *blockingRun {
	return &blockingRun{scheduler: s, release: make(chan struct{}), done: make(chan struct{}, 64)}
}

func (b *blockingRun) start(key string, async bool) {
	go func() {
		_, _ = b.scheduler.run(context.Background(), key, async, func() (EventResponse, error) {
			b.entered.Add(1)
			current := b.active.Add(1)
			for {
				peak := b.maximum.Load()
				if current <= peak || b.maximum.CompareAndSwap(peak, current) {
					break
				}
			}
			<-b.release
			b.active.Add(-1)
			return EventResponse{}, nil
		})
		b.done <- struct{}{}
	}()
}

func (b *blockingRun) awaitEntered(t *testing.T, want int32) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for b.entered.Load() < want {
		if time.Now().After(deadline) {
			t.Fatalf("entered dispatches = %d, want %d", b.entered.Load(), want)
		}
		time.Sleep(time.Millisecond)
	}
}

func (b *blockingRun) finish(t *testing.T, count int) {
	t.Helper()
	close(b.release)
	for range count {
		select {
		case <-b.done:
		case <-time.After(2 * time.Second):
			t.Fatal("dispatch did not finish")
		}
	}
}

func TestSchedulerSerializesOneSessionWithoutBlockingAnother(t *testing.T) {
	t.Parallel()
	scheduler := newScheduler(2, 2, 2)
	firstEntered := make(chan struct{})
	releaseFirst := make(chan struct{})
	secondSameEntered := make(chan struct{})
	differentEntered := make(chan struct{})
	results := make(chan error, 3)

	go func() {
		_, err := scheduler.run(context.Background(), "session-a", false, func() (EventResponse, error) {
			close(firstEntered)
			<-releaseFirst
			return EventResponse{}, nil
		})
		results <- err
	}()
	<-firstEntered
	go func() {
		_, err := scheduler.run(context.Background(), "session-a", false, func() (EventResponse, error) {
			close(secondSameEntered)
			return EventResponse{}, nil
		})
		results <- err
	}()
	go func() {
		_, err := scheduler.run(context.Background(), "session-b", false, func() (EventResponse, error) {
			close(differentEntered)
			return EventResponse{}, nil
		})
		results <- err
	}()

	select {
	case <-differentEntered:
	case <-time.After(time.Second):
		t.Fatal("different session did not overlap")
	}
	select {
	case <-secondSameEntered:
		t.Fatal("same session overlapped")
	case <-time.After(25 * time.Millisecond):
	}
	close(releaseFirst)
	select {
	case <-secondSameEntered:
	case <-time.After(time.Second):
		t.Fatal("same session did not advance")
	}
	for range 3 {
		if err := <-results; err != nil {
			t.Fatal(err)
		}
	}
}

func TestSchedulerHonorsGlobalParallelism(t *testing.T) {
	t.Parallel()
	run := newBlockingRun(newScheduler(2, 2, 2))
	for i := range 8 {
		run.start(fmt.Sprintf("session-%d", i), false)
	}
	run.awaitEntered(t, 2)
	time.Sleep(25 * time.Millisecond)
	if got := run.maximum.Load(); got != 2 {
		t.Fatalf("maximum parallel executions = %d, want 2", got)
	}
	run.finish(t, 8)
}

// The regression this fix exists for: a fixed budget narrower than the number of
// live sessions forced unrelated sessions to queue behind each other, because a
// lane gate already limits each of them to one executing dispatch.
func TestSchedulerWidensWithSessionDemand(t *testing.T) {
	t.Parallel()
	run := newBlockingRun(newScheduler(1, 16, 2))
	for i := range 6 {
		run.start(fmt.Sprintf("session-%d", i), false)
	}
	run.awaitEntered(t, 6)
	if got := run.maximum.Load(); got != 6 {
		t.Fatalf("maximum parallel executions = %d, want 6", got)
	}
	run.finish(t, 6)
}

func TestSchedulerClampsDemandToCeiling(t *testing.T) {
	t.Parallel()
	run := newBlockingRun(newScheduler(1, 3, 2))
	for i := range 9 {
		run.start(fmt.Sprintf("session-%d", i), false)
	}
	run.awaitEntered(t, 3)
	time.Sleep(25 * time.Millisecond)
	if got := run.maximum.Load(); got != 3 {
		t.Fatalf("maximum parallel executions = %d, want 3", got)
	}
	run.finish(t, 9)
}

func TestSchedulerHoldsDemandAtFloor(t *testing.T) {
	t.Parallel()
	run := newBlockingRun(newScheduler(4, 16, 2))
	for i := range 3 {
		run.start(fmt.Sprintf("session-%d", i), false)
	}
	run.awaitEntered(t, 3)
	if got := run.maximum.Load(); got != 3 {
		t.Fatalf("maximum parallel executions = %d, want 3", got)
	}
	run.finish(t, 3)
}

func TestSchedulerReleasesSlotWhenQueuedCallerCancels(t *testing.T) {
	t.Parallel()
	scheduler := newScheduler(1, 1, 2)
	run := newBlockingRun(scheduler)
	run.start("session-a", false)
	run.awaitEntered(t, 1)

	ctx, cancel := context.WithCancel(context.Background())
	queued := make(chan error, 1)
	go func() {
		_, err := scheduler.run(ctx, "session-b", false, func() (EventResponse, error) {
			return EventResponse{}, nil
		})
		queued <- err
	}()
	time.Sleep(25 * time.Millisecond)
	cancel()
	select {
	case err := <-queued:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("queued dispatch error = %v, want context.Canceled", err)
		}
	case <-time.After(time.Second):
		t.Fatal("cancelled dispatch did not return")
	}
	run.finish(t, 1)

	admitted := make(chan struct{})
	go func() {
		_, _ = scheduler.run(context.Background(), "session-c", false, func() (EventResponse, error) {
			close(admitted)
			return EventResponse{}, nil
		})
	}()
	select {
	case <-admitted:
	case <-time.After(time.Second):
		t.Fatal("cancelled dispatch leaked its slot")
	}
}

func TestSchedulerCapsBackgroundLaneSeparately(t *testing.T) {
	t.Parallel()
	run := newBlockingRun(newScheduler(1, 16, 2))
	for i := range 6 {
		run.start(fmt.Sprintf("session-%d", i), true)
	}
	run.awaitEntered(t, 2)
	time.Sleep(25 * time.Millisecond)
	if got := run.maximum.Load(); got != 2 {
		t.Fatalf("maximum background executions = %d, want 2", got)
	}
	run.finish(t, 6)
}

// Claude Code awaits the blocking dispatch and nothing awaits the background one.
func TestSchedulerBackgroundSaturationLeavesBlockingLaneFree(t *testing.T) {
	t.Parallel()
	scheduler := newScheduler(1, 16, 2)
	background := newBlockingRun(scheduler)
	for i := range 5 {
		background.start(fmt.Sprintf("background-%d", i), true)
	}
	background.awaitEntered(t, 2)

	blocking := make(chan struct{})
	go func() {
		_, _ = scheduler.run(context.Background(), "session-a", false, func() (EventResponse, error) {
			close(blocking)
			return EventResponse{}, nil
		})
	}()
	select {
	case <-blocking:
	case <-time.After(time.Second):
		t.Fatal("blocking dispatch queued behind the background lane")
	}
	background.finish(t, 5)
}

func TestSchedulerSeparatesOneSessionsBlockingAndBackgroundLanes(t *testing.T) {
	t.Parallel()
	scheduler := newScheduler(1, 16, 2)
	held := make(chan struct{})
	release := make(chan struct{})
	go func() {
		_, _ = scheduler.run(context.Background(), "session-a\x00true", true, func() (EventResponse, error) {
			close(held)
			<-release
			return EventResponse{}, nil
		})
	}()
	<-held

	ran := make(chan struct{})
	go func() {
		_, _ = scheduler.run(context.Background(), "session-a\x00false", false, func() (EventResponse, error) {
			close(ran)
			return EventResponse{}, nil
		})
	}()
	select {
	case <-ran:
	case <-time.After(time.Second):
		t.Fatal("a session's blocking dispatch serialized behind its own background dispatch")
	}
	close(release)
}

func TestParallelCeilingReadsOverride(t *testing.T) {
	t.Parallel()
	for _, testCase := range []struct {
		name     string
		override string
		want     int
	}{
		{name: "unset", override: "", want: maxParallelDispatch},
		{name: "blank", override: "   ", want: maxParallelDispatch},
		{name: "explicit", override: "24", want: 24},
		{name: "padded", override: "  24  ", want: 24},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			t.Parallel()
			got, err := parallelCeiling(testCase.override)
			if err != nil {
				t.Fatal(err)
			}
			if got != testCase.want {
				t.Fatalf("parallelCeiling(%q) = %d, want %d", testCase.override, got, testCase.want)
			}
		})
	}
}

func TestParallelCeilingRefusesAnUnusableOverride(t *testing.T) {
	t.Parallel()
	for _, override := range []string{"wide", "0", "-3", "3.5"} {
		t.Run(override, func(t *testing.T) {
			t.Parallel()
			if _, err := parallelCeiling(override); err == nil {
				t.Fatalf("parallelCeiling(%q) accepted an unusable override", override)
			}
		})
	}
}
