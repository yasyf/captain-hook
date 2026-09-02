package hookd

import (
	"context"
	"sync"
)

type lane struct {
	gate chan struct{}
	refs int
}

// pool is one admission budget: dispatches executing against it, and the FIFO
// queue waiting for a slot.
type pool struct {
	holders int
	waiters []chan struct{}
}

// scheduler runs one dispatch per lane at a time, under two budgets. The
// blocking one tracks the live sync lane count clamped to [floor, ceiling] —
// below that count, unrelated sessions merely queue behind each other.
// Background dispatches, which nothing awaits, get a fixed budget of their own.
type scheduler struct {
	mu        sync.Mutex
	lanes     map[string]*lane
	syncLanes int

	sync  pool
	async pool

	floor    int
	ceiling  int
	asyncCap int
}

func newScheduler(floor, ceiling, asyncCap int) *scheduler {
	return &scheduler{
		lanes: make(map[string]*lane),
		floor: floor, ceiling: ceiling, asyncCap: asyncCap,
	}
}

func (s *scheduler) run(ctx context.Context, key string, async bool, execute func() (EventResponse, error)) (EventResponse, error) {
	l := s.acquireLane(key, async)
	defer s.releaseLane(key, async, l)
	select {
	case l.gate <- struct{}{}:
		defer func() { <-l.gate }()
	case <-ctx.Done():
		return EventResponse{}, ctx.Err()
	}
	if err := s.acquireSlot(ctx, async); err != nil {
		return EventResponse{}, err
	}
	defer s.releaseSlot(async)
	return execute()
}

func (s *scheduler) acquireLane(key string, async bool) *lane {
	s.mu.Lock()
	defer s.mu.Unlock()
	l := s.lanes[key]
	if l == nil {
		l = &lane{gate: make(chan struct{}, 1)}
		s.lanes[key] = l
		if !async {
			// A new sync lane widens the limit, so queued callers may now fit.
			s.syncLanes++
			s.promoteLocked(&s.sync, s.syncLimitLocked())
		}
	}
	l.refs++
	return l
}

func (s *scheduler) releaseLane(key string, async bool, l *lane) {
	s.mu.Lock()
	defer s.mu.Unlock()
	l.refs--
	if l.refs == 0 {
		delete(s.lanes, key)
		if !async {
			s.syncLanes--
		}
	}
}

func (s *scheduler) syncLimitLocked() int {
	switch {
	case s.syncLanes < s.floor:
		return s.floor
	case s.syncLanes > s.ceiling:
		return s.ceiling
	default:
		return s.syncLanes
	}
}

func (s *scheduler) poolLocked(async bool) (*pool, int) {
	if async {
		return &s.async, s.asyncCap
	}
	return &s.sync, s.syncLimitLocked()
}

func (s *scheduler) acquireSlot(ctx context.Context, async bool) error {
	s.mu.Lock()
	p, limit := s.poolLocked(async)
	if p.holders < limit {
		p.holders++
		s.mu.Unlock()
		return nil
	}
	granted := make(chan struct{})
	p.waiters = append(p.waiters, granted)
	s.mu.Unlock()

	select {
	case <-granted:
		return nil
	case <-ctx.Done():
		s.mu.Lock()
		p, _ := s.poolLocked(async)
		dropped := dropWaiter(p, granted)
		s.mu.Unlock()
		// A concurrent release already granted the slot; hand it back.
		if !dropped {
			s.releaseSlot(async)
		}
		return ctx.Err()
	}
}

func (s *scheduler) releaseSlot(async bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p, limit := s.poolLocked(async)
	p.holders--
	s.promoteLocked(p, limit)
}

// promoteLocked admits queued callers in arrival order. holders may exceed the
// limit when a closing lane narrowed it under callers already executing.
func (s *scheduler) promoteLocked(p *pool, limit int) {
	for len(p.waiters) > 0 && p.holders < limit {
		granted := p.waiters[0]
		p.waiters = p.waiters[1:]
		p.holders++
		close(granted)
	}
}

func dropWaiter(p *pool, granted chan struct{}) bool {
	for i, waiter := range p.waiters {
		if waiter == granted {
			p.waiters = append(p.waiters[:i], p.waiters[i+1:]...)
			return true
		}
	}
	return false
}
