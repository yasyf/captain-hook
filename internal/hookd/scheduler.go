package hookd

import (
	"context"
	"sync"
)

type lane struct {
	gate chan struct{}
	refs int
}

type scheduler struct {
	parallel chan struct{}

	mu    sync.Mutex
	lanes map[string]*lane
}

func newScheduler(parallel int) *scheduler {
	return &scheduler{parallel: make(chan struct{}, parallel), lanes: make(map[string]*lane)}
}

func (s *scheduler) run(ctx context.Context, key string, execute func() (EventResponse, error)) (EventResponse, error) {
	l := s.acquireLane(key)
	defer s.releaseLane(key, l)
	select {
	case l.gate <- struct{}{}:
		defer func() { <-l.gate }()
	case <-ctx.Done():
		return EventResponse{}, ctx.Err()
	}
	select {
	case s.parallel <- struct{}{}:
		defer func() { <-s.parallel }()
	case <-ctx.Done():
		return EventResponse{}, ctx.Err()
	}
	return execute()
}

func (s *scheduler) acquireLane(key string) *lane {
	s.mu.Lock()
	defer s.mu.Unlock()
	l := s.lanes[key]
	if l == nil {
		l = &lane{gate: make(chan struct{}, 1)}
		s.lanes[key] = l
	}
	l.refs++
	return l
}

func (s *scheduler) releaseLane(key string, l *lane) {
	s.mu.Lock()
	defer s.mu.Unlock()
	l.refs--
	if l.refs == 0 {
		delete(s.lanes, key)
	}
}
