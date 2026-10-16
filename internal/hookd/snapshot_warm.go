package hookd

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"strconv"
	"sync"
	"time"

	"github.com/yasyf/captain-hook/internal/snapshots"
	"github.com/yasyf/captain-hook/internal/wireproto"
)

const (
	warmStepBytes      = 8 << 20
	warmStepTimeout    = 3 * time.Second
	warmReplyGrace     = 2 * time.Second
	warmBytesPerSecond = warmStepBytes / 2
	warmDemandWindow   = 5 * time.Minute
	warmDemandHeadroom = 512
	warmStallLimit     = 2
	maxWarmJobs        = 128
)

var (
	warmProgressKinds = map[string]string{"warm_registered": "warmed_registry", "warm_root": "warmed_root"}
	warmStallStatuses = map[string]bool{"incomplete": true, "deadline": true, "retained_limit": true, "lease_limit": true, "cancelled": true}
	warmHostFields    = []string{"id", "deadline_unix_ms", "start_index", "membership_revision"}
)

type warmUsage struct {
	SourceBytesRead        int64 `json:"source_bytes_read"`
	PreparedCacheBytesRead int64 `json:"prepared_cache_bytes_read"`
	EventsParsed           int64 `json:"events_parsed"`
}

func (u *warmUsage) add(step warmUsage) {
	u.SourceBytesRead += step.SourceBytesRead
	u.PreparedCacheBytesRead += step.PreparedCacheBytesRead
	u.EventsParsed += step.EventsParsed
}

func (u warmUsage) String() string {
	return fmt.Sprintf("source_bytes_read=%d prepared_cache_bytes_read=%d events_parsed=%d", u.SourceBytesRead, u.PreparedCacheBytesRead, u.EventsParsed)
}

type warmProgress struct {
	Kind               string `json:"kind"`
	OwnerEpoch         string `json:"owner_epoch"`
	MembershipRevision string `json:"membership_revision"`
	SourceRevision     string `json:"source_revision"`
	NextIndex          int64  `json:"next_index"`
	SourceOffset       int64  `json:"source_offset"`
	Complete           bool   `json:"complete"`
	FactsComplete      bool   `json:"facts_complete"`
}

type warmReply struct {
	Status string        `json:"status"`
	Reason string        `json:"reason"`
	Usage  warmUsage     `json:"usage"`
	Data   *warmProgress `json:"data"`
}

func decodeWarmReply(body json.RawMessage, err error) *warmReply {
	var wrapper struct {
		Response *warmReply `json:"response"`
	}
	if err != nil || json.Unmarshal(body, &wrapper) != nil {
		return nil
	}
	return wrapper.Response
}

func warmPause(read float64) time.Duration {
	return time.Duration(read / warmBytesPerSecond * float64(time.Second))
}

func warmSleep(ctx context.Context, wait time.Duration) error {
	timer := time.NewTimer(wait)
	defer timer.Stop()
	select {
	case <-timer.C:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func warmKey(body json.RawMessage, callContext snapshots.CallContext) (string, error) {
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	var demand any
	if err := decoder.Decode(&demand); err != nil {
		return "", fmt.Errorf("captain: decode snapshot warm demand: %w", err)
	}
	canonical, err := wireproto.Marshal(map[string]any{"admission": callContext.Admission, "authority": callContext.Authority, "demand": demand})
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:]), nil
}

type warmJob struct {
	key         string
	operation   string
	schema      string
	demand      map[string]any
	registry    json.RawMessage
	callContext snapshots.CallContext
	demanded    time.Time
	served      uint64
	steps       int
	stalled     int
	usage       warmUsage

	startIndex         int64
	sourceOffset       int64
	ownerEpoch         string
	membershipRevision string
	sourceRevision     string
	factsComplete      bool
}

func newWarmJob(key string, body json.RawMessage, callContext snapshots.CallContext, now time.Time) (*warmJob, error) {
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	var demand struct {
		Schema       string                     `json:"schema"`
		Request      map[string]json.RawMessage `json:"request"`
		ToolRegistry json.RawMessage            `json:"tool_registry"`
	}
	if err := decoder.Decode(&demand); err != nil {
		return nil, fmt.Errorf("captain: decode snapshot warm demand: %w", err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return nil, errors.New("captain: snapshot warm demand has trailing JSON")
	}
	var operation string
	if err := json.Unmarshal(demand.Request["operation"], &operation); err != nil || warmProgressKinds[operation] == "" {
		return nil, errors.New("captain: snapshot warm demand names no warm operation")
	}
	for _, field := range warmHostFields {
		if _, owned := demand.Request[field]; owned {
			return nil, fmt.Errorf("captain: snapshot warm demand carries host-owned %s", field)
		}
	}
	var limits map[string]json.RawMessage
	if err := json.Unmarshal(demand.Request["limits"], &limits); err != nil || limits == nil {
		return nil, errors.New("captain: snapshot warm demand has no limits")
	}
	limits["max_source_read_bytes"] = json.RawMessage(strconv.Itoa(warmStepBytes))
	callContext.Claimant, callContext.WorkClass = "warm:"+key[:32], "background"
	job := &warmJob{key: key, operation: operation, schema: demand.Schema, registry: demand.ToolRegistry, callContext: callContext, demand: make(map[string]any, len(demand.Request)+len(warmHostFields))}
	for name, value := range demand.Request {
		job.demand[name] = value
	}
	job.demand["limits"] = limits
	request, err := job.request(0, now.Add(warmStepTimeout))
	if err != nil {
		return nil, err
	}
	if _, err := snapshots.Metadata(request); err != nil {
		return nil, err
	}
	return job, nil
}

func (j *warmJob) request(sequence uint64, deadline time.Time) (json.RawMessage, error) {
	request := maps.Clone(j.demand)
	request["id"] = fmt.Sprintf("warm-%s-%d", j.key[:16], sequence)
	request["deadline_unix_ms"] = deadline.UnixMilli()
	if j.operation == "warm_registered" {
		request["start_index"], request["membership_revision"] = j.startIndex, nil
		if j.membershipRevision != "" {
			request["membership_revision"] = j.membershipRevision
		}
	}
	return wireproto.Marshal(map[string]any{"schema": j.schema, "request": request, "tool_registry": j.registry})
}

func (j *warmJob) advance(reply *warmReply) (complete bool, stopped string) {
	switch {
	case reply == nil || warmStallStatuses[reply.Status]:
		return false, j.stall(false, false)
	case reply.Status == "changed" && j.operation == "warm_registered":
		j.startIndex, j.sourceOffset, j.membershipRevision, j.stalled = 0, 0, "", 0
		return false, ""
	case reply.Status != "ok":
		return false, fmt.Sprintf("%s: %s", reply.Status, reply.Reason)
	case reply.Data == nil || reply.Data.Kind != warmProgressKinds[j.operation]:
		return false, "invalid progress"
	}
	data, read := reply.Data, reply.Usage.SourceBytesRead > 0 || reply.Usage.EventsParsed > 0
	changed := j.ownerEpoch != "" && data.OwnerEpoch != j.ownerEpoch
	if j.operation == "warm_root" {
		changed = changed || (j.sourceRevision != "" && data.SourceRevision != j.sourceRevision)
		progress := changed || read || data.SourceOffset > j.sourceOffset || (data.FactsComplete && !j.factsComplete)
		j.ownerEpoch, j.sourceRevision, j.sourceOffset, j.factsComplete = data.OwnerEpoch, data.SourceRevision, data.SourceOffset, data.FactsComplete
		complete = data.Complete && data.FactsComplete
		return complete, j.stall(progress, complete)
	}
	changed = changed || (j.membershipRevision != "" && data.MembershipRevision != j.membershipRevision)
	progress := changed || read || data.NextIndex > j.startIndex || data.SourceOffset > j.sourceOffset
	j.ownerEpoch, j.membershipRevision, j.startIndex, j.sourceOffset = data.OwnerEpoch, data.MembershipRevision, data.NextIndex, data.SourceOffset
	if changed {
		j.startIndex, j.sourceOffset = 0, 0
	}
	return data.Complete && !changed, j.stall(progress, data.Complete)
}

func (j *warmJob) stall(progress, finished bool) string {
	j.stalled++
	if progress {
		j.stalled = 0
	}
	if j.stalled >= warmStallLimit && !finished {
		return "no progress"
	}
	return ""
}

type warmCoordinator struct {
	step  func(context.Context, json.RawMessage, snapshots.CallContext) (json.RawMessage, error)
	now   func() time.Time
	pause func(context.Context, time.Duration) error
	log   io.Writer

	ctx    context.Context
	cancel context.CancelFunc
	wg     sync.WaitGroup

	mu      sync.Mutex
	jobs    map[string]*warmJob
	order   uint64
	ready   time.Time
	running bool
	closed  bool
}

func newWarmCoordinator(step func(context.Context, json.RawMessage, snapshots.CallContext) (json.RawMessage, error), now func() time.Time, log io.Writer) *warmCoordinator {
	ctx, cancel := context.WithCancel(context.Background())
	return &warmCoordinator{step: step, now: now, pause: warmSleep, log: log, ctx: ctx, cancel: cancel, jobs: make(map[string]*warmJob)}
}

func (c *warmCoordinator) demand(body json.RawMessage, callContext snapshots.CallContext) error {
	if c.ctx.Err() != nil {
		return nil
	}
	if len(body) == 0 || len(body) > snapshots.MaxFrameBytes-warmDemandHeadroom {
		return wireproto.ErrPayloadTooLarge
	}
	key, err := warmKey(body, callContext)
	if err != nil {
		return err
	}
	if c.join(key, nil) {
		return nil
	}
	job, err := newWarmJob(key, body, callContext, c.now())
	if err != nil {
		return err
	}
	c.join(key, job)
	return nil
}

func (c *warmCoordinator) join(key string, created *warmJob) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.closed {
		return true
	}
	job, ok := c.jobs[key]
	if !ok && created == nil {
		return false
	}
	if !ok {
		if len(c.jobs) >= maxWarmJobs {
			c.evict()
		}
		c.order++
		created.served = c.order
		c.jobs[key], job = created, created
	}
	job.demanded = c.now()
	if !c.running {
		c.running = true
		c.wg.Add(1)
		go c.drive()
	}
	return true
}

func (c *warmCoordinator) evict() {
	var victim *warmJob
	for _, job := range c.jobs {
		if victim == nil || job.demanded.Before(victim.demanded) || (job.demanded.Equal(victim.demanded) && job.served < victim.served) {
			victim = job
		}
	}
	c.stop(victim, "evicted")
}

func (c *warmCoordinator) stop(job *warmJob, why string) {
	delete(c.jobs, job.key)
	fmt.Fprintf(c.log, "captain: snapshot warm %s %.16s stopped after %d steps: %s %s\n", job.operation, job.key, job.steps, why, job.usage)
}

func (c *warmCoordinator) runnable(now time.Time) *warmJob {
	var next *warmJob
	for _, job := range c.jobs {
		if now.Sub(job.demanded) <= warmDemandWindow && (next == nil || job.served < next.served) {
			next = job
		}
	}
	return next
}

func (c *warmCoordinator) drive() {
	defer c.wg.Done()
	for {
		c.mu.Lock()
		now := c.now()
		job := c.runnable(now)
		if c.closed || job == nil {
			c.running = false
			c.mu.Unlock()
			return
		}
		if wait := c.ready.Sub(now); wait > 0 {
			c.mu.Unlock()
			_ = c.pause(c.ctx, wait)
			continue
		}
		c.order++
		job.served = c.order
		job.steps++
		request, err := job.request(job.served, now.Add(warmStepTimeout))
		callContext := job.callContext
		c.mu.Unlock()
		var body json.RawMessage
		if err == nil {
			ctx, cancel := context.WithTimeout(c.ctx, warmStepTimeout+warmReplyGrace)
			body, err = c.step(ctx, request, callContext)
			cancel()
		}
		if c.ctx.Err() != nil {
			continue
		}
		reply := decodeWarmReply(body, err)
		c.charge(reply)
		c.mu.Lock()
		if reply != nil {
			job.usage.add(reply.Usage)
		}
		if c.jobs[job.key] == job {
			c.settle(job, reply)
		}
		c.mu.Unlock()
	}
}

func (c *warmCoordinator) charge(reply *warmReply) {
	c.mu.Lock()
	defer c.mu.Unlock()
	now := c.now()
	if reply == nil {
		c.ready = maxTime(c.ready, now.Add(warmPause(warmStepBytes)))
		return
	}
	c.ready = maxTime(c.ready, now).Add(warmPause(float64(reply.Usage.SourceBytesRead) + float64(reply.Usage.PreparedCacheBytesRead)))
}

func (c *warmCoordinator) settle(job *warmJob, reply *warmReply) {
	complete, stopped := job.advance(reply)
	switch {
	case stopped != "":
		c.stop(job, stopped)
	case complete:
		delete(c.jobs, job.key)
		fmt.Fprintf(c.log, "captain: snapshot warm %s %.16s complete after %d steps %s\n", job.operation, job.key, job.steps, job.usage)
	}
}

func (c *warmCoordinator) close() {
	c.mu.Lock()
	c.closed = true
	c.mu.Unlock()
	c.cancel()
}

func (c *warmCoordinator) wait(ctx context.Context) error {
	done := make(chan struct{})
	go func() { c.wg.Wait(); close(done) }()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
