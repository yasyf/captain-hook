package hookd

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

type fakeClock struct {
	mu  sync.Mutex
	now time.Time
}

func (c *fakeClock) Now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.now
}

func (c *fakeClock) Advance(d time.Duration) {
	c.mu.Lock()
	c.now = c.now.Add(d)
	c.mu.Unlock()
}

func TestStartupWithNoWaiterDeadlineEndsAtReadiness(t *testing.T) {
	t.Parallel()
	clock := &fakeClock{now: time.Unix(1_700_000_000, 0)}
	s := newStartup(context.Background(), clock.Now, 0, time.Time{})
	<-s.ctx.Done()
	if !errors.Is(s.ctx.Err(), context.Canceled) {
		t.Fatalf("startup context = %v, want cancelled at the readiness bound", s.ctx.Err())
	}
	s.end()
}

func TestStartupHoldsToItsLatestWaiterDeadline(t *testing.T) {
	t.Parallel()
	clock := &fakeClock{now: time.Unix(1_700_000_000, 0)}
	s := newStartup(context.Background(), clock.Now, 10*time.Second, clock.Now().Add(time.Minute))
	s.extend(clock.Now().Add(30 * time.Second))
	s.extend(clock.Now().Add(2 * time.Minute))

	clock.Advance(90 * time.Second)
	s.expire()
	if s.ctx.Err() != nil {
		t.Fatal("a startup expired while its latest waiter's deadline remained")
	}

	clock.Advance(time.Minute)
	s.expire()
	if !errors.Is(s.ctx.Err(), context.Canceled) {
		t.Fatalf("startup context past every deadline = %v, want cancelled", s.ctx.Err())
	}
}

func TestStartupEndStopsItsBound(t *testing.T) {
	t.Parallel()
	clock := &fakeClock{now: time.Unix(1_700_000_000, 0)}
	s := newStartup(context.Background(), clock.Now, time.Hour, time.Time{})
	s.end()
	if !errors.Is(s.ctx.Err(), context.Canceled) {
		t.Fatalf("ended startup context = %v, want cancelled", s.ctx.Err())
	}
}
