package hookd

import (
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

// serviceSampleWeight is the share of one finished dispatch's elapsed time
// that moves its event's running service-time estimate.
const serviceSampleWeight = 0.25

func overloaded(detail string) error {
	return &daemonkit.ProductError{Code: overloadCode, Message: "captain: host overloaded; hook skipped: " + detail}
}

func isOverloaded(err error) bool {
	var product *daemonkit.ProductError
	return errors.As(err, &product) && product.Code == overloadCode
}

type serviceKey struct {
	event string
	async bool
}

type dispatch struct {
	service  serviceKey
	estimate time.Duration
	started  time.Time
}

type lane struct {
	gate    chan struct{}
	refs    int
	pending time.Duration
	holder  *dispatch
}

type waiter struct {
	granted  chan struct{}
	estimate time.Duration
}

type pool struct {
	holders int
	waiters []waiter
	queued  time.Duration
}

type scheduler struct {
	mu        sync.Mutex
	lanes     map[string]*lane
	syncLanes int
	workers   map[string]*pool
	service   map[serviceKey]time.Duration

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
		service: make(map[serviceKey]time.Duration),
		floor:   floor, ceiling: ceiling, asyncCap: asyncCap,
		workerThreads: wireproto.WorkerThreads,
	}
}

// run admits one dispatch through its lane, the host pool, then its worker's
// pool, each weighing it by its event's smoothed service time, and holds all
// three until the worker is done with it. An execute that returns
// abandonedCall has left its request on the worker, so the admission is
// released only when that settles.
func (s *scheduler) run(ctx context.Context, worker, key, event string, async bool, execute func() (wireproto.EventResponse, error)) (wireproto.EventResponse, error) {
	l, d, err := s.acquireLane(ctx, key, event, async)
	if err != nil {
		return wireproto.EventResponse{}, err
	}
	select {
	case l.gate <- struct{}{}:
	case <-ctx.Done():
		s.releaseLane(key, async, l, d)
		return wireproto.EventResponse{}, ctx.Err()
	}
	if err := s.acquireSlot(ctx, async, d); err != nil {
		<-l.gate
		s.releaseLane(key, async, l, d)
		return wireproto.EventResponse{}, err
	}
	if err := s.acquireWorker(ctx, worker, d); err != nil {
		s.releaseSlot(async)
		<-l.gate
		s.releaseLane(key, async, l, d)
		return wireproto.EventResponse{}, err
	}
	s.begin(l, d)
	response, err := execute()
	var late *abandonedCall
	if errors.As(err, &late) {
		go func() {
			<-late.settled
			s.complete(worker, key, async, l, d)
		}()
		return response, err
	}
	s.complete(worker, key, async, l, d)
	return response, err
}

func (s *scheduler) begin(l *lane, d *dispatch) {
	s.mu.Lock()
	defer s.mu.Unlock()
	d.started = time.Now()
	l.holder = d
}

// complete records a finished dispatch's service time against its event and
// releases its worker slot, host slot, gate, and lane hold in one critical
// section, so no arrival counts it as still ahead and sheds on it.
func (s *scheduler) complete(worker, key string, async bool, l *lane, d *dispatch) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.observeLocked(d.service, time.Since(d.started))
	s.releaseWorkerLocked(worker)
	s.releaseSlotLocked(async)
	<-l.gate
	s.releaseLaneLocked(key, async, l, d)
}

func (s *scheduler) observeLocked(k serviceKey, elapsed time.Duration) {
	current, seen := s.service[k]
	if !seen {
		s.service[k] = elapsed
		return
	}
	s.service[k] = current + time.Duration(serviceSampleWeight*float64(elapsed-current))
}

func (s *scheduler) acquireLane(ctx context.Context, key, event string, async bool) (*lane, *dispatch, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	d := &dispatch{service: serviceKey{event: event, async: async}}
	d.estimate = s.service[d.service]
	l := s.lanes[key]
	if l == nil {
		l = &lane{gate: make(chan struct{}, 1)}
		s.lanes[key] = l
		if !async {
			s.admitSyncLaneLocked()
		}
	} else if err := shed(ctx, l.refs, l.waitLocked(), "this session's lane"); err != nil {
		return nil, nil, err
	}
	l.refs++
	l.pending += d.estimate
	return l, d, nil
}

func (l *lane) waitLocked() time.Duration {
	if l.holder == nil {
		return l.pending
	}
	return l.pending - min(time.Since(l.holder.started), l.holder.estimate)
}

func (s *scheduler) releaseLane(key string, async bool, l *lane, d *dispatch) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.releaseLaneLocked(key, async, l, d)
}

func (s *scheduler) releaseLaneLocked(key string, async bool, l *lane, d *dispatch) {
	l.pending -= d.estimate
	if l.holder == d {
		l.holder = nil
	}
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

func (s *scheduler) acquireSlot(ctx context.Context, async bool, d *dispatch) error {
	return s.acquire(ctx, d, "the host",
		func() (*pool, int) { return s.poolLocked(async) },
		func() { s.releaseSlotLocked(async) },
	)
}

func (s *scheduler) acquireWorker(ctx context.Context, worker string, d *dispatch) error {
	return s.acquire(ctx, d, "this project's worker",
		func() (*pool, int) { return s.workerPoolLocked(worker), s.workerThreads },
		func() { s.releaseWorkerLocked(worker) },
	)
}

func (s *scheduler) acquire(ctx context.Context, d *dispatch, queue string, resolve func() (*pool, int), release func()) error {
	s.mu.Lock()
	p, limit := resolve()
	if p.holders < limit {
		p.holders++
		s.mu.Unlock()
		return nil
	}
	if err := shed(ctx, p.holders+len(p.waiters), p.waitLocked(limit, d), queue); err != nil {
		s.mu.Unlock()
		return err
	}
	granted := make(chan struct{})
	p.waiters = append(p.waiters, waiter{granted: granted, estimate: d.estimate})
	p.queued += d.estimate
	s.mu.Unlock()

	select {
	case <-granted:
		return nil
	case <-ctx.Done():
		s.mu.Lock()
		p, _ := resolve()
		if !withdrawWaiter(p, granted) {
			release()
		}
		s.mu.Unlock()
		return ctx.Err()
	}
}

// waitLocked spreads the queued estimates over the pool's slots and adds one
// turn, at the arriving dispatch's own estimate, for the holders to free one.
func (p *pool) waitLocked(limit int, d *dispatch) time.Duration {
	return p.queued/time.Duration(limit) + d.estimate
}

// shed refuses a dispatch only once its wait is already lost: the estimated
// wait outlasts what is left of the caller's deadline. A shed sync hook is a
// guard skipped, so a dispatch that can still make its deadline queues however
// deep the queue is, as does one whose event has no estimate yet.
func shed(ctx context.Context, ahead int, wait time.Duration, queue string) error {
	deadline, ok := ctx.Deadline()
	if !ok || wait <= 0 {
		return nil
	}
	if remaining := time.Until(deadline); wait > remaining {
		return overloaded(fmt.Sprintf(
			"%d hooks ahead on %s need ~%s, outlasting the %s left on this one",
			ahead, queue, wait.Round(time.Millisecond), remaining.Round(time.Millisecond),
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
		next := p.waiters[0]
		p.waiters = p.waiters[1:]
		p.queued -= next.estimate
		p.holders++
		close(next.granted)
	}
}

func withdrawWaiter(p *pool, granted chan struct{}) bool {
	for i, w := range p.waiters {
		if w.granted == granted {
			p.waiters = append(p.waiters[:i], p.waiters[i+1:]...)
			p.queued -= w.estimate
			return true
		}
	}
	return false
}
