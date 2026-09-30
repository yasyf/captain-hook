package hookd

import (
	"context"
	"sync"
	"time"
)

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

func newStartup(parent context.Context, now func() time.Time, readiness time.Duration, deadline time.Time) (*startup, func()) {
	ctx, cancel := context.WithCancel(parent)
	s := &startup{ctx: ctx, cancel: cancel, now: now, readiness: now().Add(readiness), waiters: make(map[uint64]time.Time)}
	s.mu.Lock()
	defer s.mu.Unlock()
	leave := s.registerLocked(deadline)
	s.timer = time.AfterFunc(s.boundLocked().Sub(now()), s.expire)
	return s, leave
}

func (s *startup) join(deadline time.Time) (leave func()) {
	s.mu.Lock()
	defer s.mu.Unlock()
	leave = s.registerLocked(deadline)
	s.armLocked()
	return leave
}

func (s *startup) registerLocked(deadline time.Time) (leave func()) {
	if s.ended || deadline.IsZero() {
		return func() {}
	}
	s.joined++
	id := s.joined
	s.waiters[id] = deadline
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
