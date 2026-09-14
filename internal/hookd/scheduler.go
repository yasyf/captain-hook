package hookd

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
	"github.com/yasyf/daemonkit"
)

// overloadCode is the ProductError code a shed dispatch crosses the wire with,
// so the client can tell a host shedding load from a host that failed.
const overloadCode = "overloaded"

func overloaded(detail string) error {
	return &daemonkit.ProductError{Code: overloadCode, Message: "captain: host overloaded; hook skipped: " + detail}
}

func isOverloaded(err error) bool {
	var product *daemonkit.ProductError
	return errors.As(err, &product) && product.Code == overloadCode
}

type lane struct {
	gate    chan struct{}
	refs    int
	service time.Duration
}

type pool struct {
	holders int
	waiters []chan struct{}
	service time.Duration
}

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

// run admits one dispatch through its lane and pool and holds both until the
// worker is done with it. An execute that returns abandonedCall has left its
// request on the worker, so the admission is released only when that settles;
// releasing it on return would let the host send more work than the
// interpreter has threads for, which is the collapse the ceiling exists to
// prevent.
func (s *scheduler) run(ctx context.Context, key string, async bool, execute func() (wireproto.EventResponse, error)) (wireproto.EventResponse, error) {
	l, err := s.acquireLane(ctx, key, async)
	if err != nil {
		return wireproto.EventResponse{}, err
	}
	select {
	case l.gate <- struct{}{}:
	case <-ctx.Done():
		s.releaseLane(key, async, l)
		return wireproto.EventResponse{}, ctx.Err()
	}
	if err := s.acquireSlot(ctx, async); err != nil {
		<-l.gate
		s.releaseLane(key, async, l)
		return wireproto.EventResponse{}, err
	}
	started := time.Now()
	response, err := execute()
	release := func() {
		s.finish(l, async, time.Since(started))
		<-l.gate
		s.releaseLane(key, async, l)
	}
	var late *abandonedCall
	if errors.As(err, &late) {
		go func() {
			<-late.settled
			release()
		}()
		return response, err
	}
	release()
	return response, err
}

func (s *scheduler) acquireLane(ctx context.Context, key string, async bool) (*lane, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	l := s.lanes[key]
	if l == nil {
		l = &lane{gate: make(chan struct{}, 1)}
		s.lanes[key] = l
		if !async {
			s.admitSyncLaneLocked()
		}
	} else {
		p, _ := s.poolLocked(async)
		if err := shed(ctx, l.refs, cmp.Or(l.service, p.service), "this session's lane"); err != nil {
			return nil, err
		}
	}
	l.refs++
	return l, nil
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

func (s *scheduler) admitSyncLaneLocked() {
	s.syncLanes++
	s.promoteLocked(&s.sync, s.syncLimitLocked())
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
	if err := shed(ctx, len(p.waiters)/limit+1, p.service, "the host"); err != nil {
		s.mu.Unlock()
		return err
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
		withdrawn := withdrawWaiter(p, granted)
		s.mu.Unlock()
		if !withdrawn {
			s.releaseSlot(async)
		}
		return ctx.Err()
	}
}

// shed refuses a dispatch only once its wait is already lost: ahead turns at
// the last observed service time outlast what is left of the caller's
// deadline. A shed sync hook is a guard skipped, so a dispatch that can still
// make its deadline queues however deep the queue is.
func shed(ctx context.Context, ahead int, service time.Duration, queue string) error {
	deadline, ok := ctx.Deadline()
	if !ok || service == 0 {
		return nil
	}
	remaining := time.Until(deadline)
	if wait := time.Duration(ahead) * service; wait > remaining {
		return overloaded(fmt.Sprintf(
			"%d hooks ahead on %s at %s each outlast the %s left on this one",
			ahead, queue, service.Round(time.Millisecond), remaining.Round(time.Millisecond),
		))
	}
	return nil
}

func (s *scheduler) finish(l *lane, async bool, elapsed time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p, _ := s.poolLocked(async)
	p.service, l.service = elapsed, elapsed
	s.releaseSlotLocked(async)
}

func (s *scheduler) releaseSlot(async bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.releaseSlotLocked(async)
}

func (s *scheduler) releaseSlotLocked(async bool) {
	p, limit := s.poolLocked(async)
	p.holders--
	s.promoteLocked(p, limit)
}

func (s *scheduler) promoteLocked(p *pool, limit int) {
	for len(p.waiters) > 0 && p.holders < limit {
		granted := p.waiters[0]
		p.waiters = p.waiters[1:]
		p.holders++
		close(granted)
	}
}

func withdrawWaiter(p *pool, granted chan struct{}) bool {
	for i, waiter := range p.waiters {
		if waiter == granted {
			p.waiters = append(p.waiters[:i], p.waiters[i+1:]...)
			return true
		}
	}
	return false
}
