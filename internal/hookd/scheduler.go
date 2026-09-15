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
	workers   map[string]*pool

	sync  pool
	async pool

	floor         int
	ceiling       int
	asyncCap      int
	workerThreads int
}

func newScheduler(floor, ceiling, asyncCap int) *scheduler {
	return &scheduler{
		lanes:   make(map[string]*lane),
		workers: make(map[string]*pool),
		floor:   floor, ceiling: ceiling, asyncCap: asyncCap,
		workerThreads: wireproto.WorkerThreads,
	}
}

// run admits one dispatch through its lane, the host pool, then its worker's
// pool, and holds all three until the worker is done with it. An execute that
// returns abandonedCall has left its request on the worker, so the admission
// is released only when that settles; releasing on return would let the host
// send more work than the interpreter has threads for.
func (s *scheduler) run(ctx context.Context, worker, key string, async bool, execute func() (wireproto.EventResponse, error)) (wireproto.EventResponse, error) {
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
	if err := s.acquireWorker(ctx, worker, async); err != nil {
		s.releaseSlot(async)
		<-l.gate
		s.releaseLane(key, async, l)
		return wireproto.EventResponse{}, err
	}
	started := time.Now()
	response, err := execute()
	var late *abandonedCall
	if errors.As(err, &late) {
		go func() {
			<-late.settled
			s.complete(worker, key, async, l, time.Since(started))
		}()
		return response, err
	}
	s.complete(worker, key, async, l, time.Since(started))
	return response, err
}

// complete releases a finished dispatch's worker slot, host slot, gate, and
// lane hold in one critical section, so no arrival counts it as still ahead
// and sheds on it.
func (s *scheduler) complete(worker, key string, async bool, l *lane, elapsed time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p, _ := s.poolLocked(async)
	w := s.workers[worker]
	p.service, w.service, l.service = elapsed, elapsed, elapsed
	s.releaseWorkerLocked(worker)
	s.releaseSlotLocked(async)
	<-l.gate
	s.releaseLaneLocked(key, async, l)
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
	s.releaseLaneLocked(key, async, l)
}

func (s *scheduler) releaseLaneLocked(key string, async bool, l *lane) {
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

func (s *scheduler) workerPoolLocked(worker string) *pool {
	w := s.workers[worker]
	if w == nil {
		w = &pool{}
		s.workers[worker] = w
	}
	return w
}

func (s *scheduler) acquireSlot(ctx context.Context, async bool) error {
	return s.acquire(ctx, "the host",
		func() (*pool, int, time.Duration) {
			p, limit := s.poolLocked(async)
			return p, limit, p.service
		},
		func() { s.releaseSlotLocked(async) },
	)
}

func (s *scheduler) acquireWorker(ctx context.Context, worker string, async bool) error {
	return s.acquire(ctx, "this project's worker",
		func() (*pool, int, time.Duration) {
			w := s.workerPoolLocked(worker)
			p, _ := s.poolLocked(async)
			return w, s.workerThreads, cmp.Or(w.service, p.service)
		},
		func() { s.releaseWorkerLocked(worker) },
	)
}

func (s *scheduler) acquire(ctx context.Context, queue string, resolve func() (*pool, int, time.Duration), release func()) error {
	s.mu.Lock()
	p, limit, service := resolve()
	if p.holders < limit {
		p.holders++
		s.mu.Unlock()
		return nil
	}
	if err := shed(ctx, len(p.waiters)/limit+1, service, queue); err != nil {
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
		p, _, _ := resolve()
		if !withdrawWaiter(p, granted) {
			release()
		}
		s.mu.Unlock()
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

func (s *scheduler) releaseWorkerLocked(worker string) {
	w := s.workers[worker]
	w.holders--
	s.promoteLocked(w, s.workerThreads)
	if w.holders == 0 && len(w.waiters) == 0 {
		delete(s.workers, worker)
	}
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
