package hookd

import (
	"context"
	"sync/atomic"
	"testing"
	"time"
)

func TestSchedulerSerializesOneSessionWithoutBlockingAnother(t *testing.T) {
	t.Parallel()
	scheduler := newScheduler(2)
	firstEntered := make(chan struct{})
	releaseFirst := make(chan struct{})
	secondSameEntered := make(chan struct{})
	differentEntered := make(chan struct{})
	results := make(chan error, 3)

	go func() {
		_, err := scheduler.run(context.Background(), "session-a", func() (EventResponse, error) {
			close(firstEntered)
			<-releaseFirst
			return EventResponse{}, nil
		})
		results <- err
	}()
	<-firstEntered
	go func() {
		_, err := scheduler.run(context.Background(), "session-a", func() (EventResponse, error) {
			close(secondSameEntered)
			return EventResponse{}, nil
		})
		results <- err
	}()
	go func() {
		_, err := scheduler.run(context.Background(), "session-b", func() (EventResponse, error) {
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
	scheduler := newScheduler(2)
	var active atomic.Int32
	var maximum atomic.Int32
	release := make(chan struct{})
	done := make(chan struct{}, 8)
	for i := range 8 {
		go func() {
			_, _ = scheduler.run(context.Background(), string(rune('a'+i)), func() (EventResponse, error) {
				current := active.Add(1)
				for current > maximum.Load() && !maximum.CompareAndSwap(maximum.Load(), current) {
				}
				<-release
				active.Add(-1)
				return EventResponse{}, nil
			})
			done <- struct{}{}
		}()
	}
	time.Sleep(25 * time.Millisecond)
	if got := maximum.Load(); got != 2 {
		t.Fatalf("maximum parallel executions = %d, want 2", got)
	}
	close(release)
	for range 8 {
		<-done
	}
}
