package hookd

import (
	"context"
	"sync"
	"time"
)

// startup bounds one worker start by whoever waits on it: readiness after it
// began, or the latest deadline among the waiters still holding it.
type startup struct {
	ctx    context.Context
	cancel context.CancelFunc
	now    func() time.Time

	mu        sync.Mutex
	readiness time.Time
	waiters   map[uint64]time.Time
	joined    uint64
	timer     *time.Timer
	ended     bool
}

func newStartup(parent context.Context, now func() time.Time, readiness time.Duration) *startup {
	ctx, cancel := context.WithCancel(parent)
	s := &startup{ctx: ctx, cancel: cancel, now: now, readiness: now().Add(readiness), waiters: make(map[uint64]time.Time)}
	s.mu.Lock()
	s.timer = time.AfterFunc(readiness, s.expire)
	s.mu.Unlock()
	return s
}

// join holds the startup open to deadline until leave is called; a waiter
// without a deadline holds nothing.
func (s *startup) join(deadline time.Time) (leave func()) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.ended || deadline.IsZero() {
		return func() {}
	}
	s.joined++
	id := s.joined
	s.waiters[id] = deadline
	s.armLocked()
	return func() {
		s.mu.Lock()
		defer s.mu.Unlock()
		if _, live := s.waiters[id]; !live || s.ended {
			return
		}
		delete(s.waiters, id)
		s.armLocked()
	}
}

func (s *startup) boundLocked() time.Time {
	bound := s.readiness
	for _, deadline := range s.waiters {
		if deadline.After(bound) {
			bound = deadline
		}
	}
	return bound
}

func (s *startup) armLocked() {
	s.timer.Reset(s.boundLocked().Sub(s.now()))
}

func (s *startup) expire() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.ended {
		return
	}
	if remaining := s.boundLocked().Sub(s.now()); remaining > 0 {
		s.timer.Reset(remaining)
		return
	}
	s.cancel()
}

func (s *startup) end() {
	s.mu.Lock()
	s.ended = true
	s.timer.Stop()
	s.mu.Unlock()
	s.cancel()
}
