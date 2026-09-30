package hookd

import (
	"context"
	"sync"
	"time"
)

// startup bounds one worker start by whoever waits on it: readiness after it
// began with nobody waiting, or the latest deadline among its waiters.
type startup struct {
	ctx    context.Context
	cancel context.CancelFunc
	now    func() time.Time

	mu    sync.Mutex
	bound time.Time
	timer *time.Timer
}

func newStartup(parent context.Context, now func() time.Time, readiness time.Duration, deadline time.Time) *startup {
	ctx, cancel := context.WithCancel(parent)
	s := &startup{ctx: ctx, cancel: cancel, now: now, bound: now().Add(readiness)}
	if deadline.After(s.bound) {
		s.bound = deadline
	}
	s.mu.Lock()
	s.timer = time.AfterFunc(s.bound.Sub(now()), s.expire)
	s.mu.Unlock()
	return s
}

func (s *startup) extend(deadline time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !deadline.After(s.bound) {
		return
	}
	s.bound = deadline
	s.timer.Reset(deadline.Sub(s.now()))
}

func (s *startup) expire() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if remaining := s.bound.Sub(s.now()); remaining > 0 {
		s.timer.Reset(remaining)
		return
	}
	s.cancel()
}

func (s *startup) end() {
	s.mu.Lock()
	s.timer.Stop()
	s.mu.Unlock()
	s.cancel()
}
