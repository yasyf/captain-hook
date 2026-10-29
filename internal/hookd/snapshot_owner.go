package hookd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/yasyf/captain-hook/internal/snapshots"
	"github.com/yasyf/captain-hook/internal/wireproto"
	"github.com/yasyf/daemonkit"
)

type snapshotResult struct {
	body json.RawMessage
	err  error
}
type snapshotPending struct {
	requestID string
	done      chan struct{}
	result    chan snapshotResult
	settled   func()
}

type snapshotOwnerLog struct {
	writer io.Writer
	pid    int
	start  string
	known  bool
}

func (l snapshotOwnerLog) failure(at time.Time, cause string, pending int) {
	fmt.Fprintf(l.writer, "captain: snapshot_owner_failure time=%s pid=%d process_start=%s metadata_known=%t identity_source=point_probe_only cause=%s pending=%d\n", at.UTC().Format(time.RFC3339Nano), l.pid, l.start, l.known, cause, pending)
}

func (l snapshotOwnerLog) terminal(at time.Time, exit daemonkit.Exit) {
	fmt.Fprintf(l.writer, "captain: snapshot_owner_exit time=%s pid=%d process_start=%s metadata_known=%t identity_source=point_probe_only exit_code=%d signal=%d\n", at.UTC().Format(time.RFC3339Nano), l.pid, l.start, l.known, exit.Code, exit.Signal)
}

type snapshotOwnerFailure struct {
	at      time.Time
	cause   string
	pending int
}

type snapshotOwner struct {
	cancelGrace time.Duration
	conn        net.Conn
	child       *daemonkit.Child
	mu          sync.Mutex
	writeMu     sync.Mutex
	pending     map[uint64]snapshotPending
	nextID      uint64
	closed      bool
	err         error
	failure     snapshotOwnerFailure
}

func handshakeSnapshotOwner(ctx context.Context, conn net.Conn, config json.RawMessage) (*snapshotOwner, error) {
	if err := snapshots.Validate("config", config); err != nil {
		return nil, err
	}
	stop := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer stop()
	hello := wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpHello, Build: Build, SnapshotConfig: config}
	if err := wireproto.EncodeFrameLimit(conn, hello, snapshots.MaxFrameBytes); err != nil {
		return nil, err
	}
	response, err := wireproto.DecodeFrameLimit(conn, snapshots.MaxFrameBytes)
	if err != nil {
		return nil, err
	}
	if response.Op != wireproto.OpHello || response.Build != Build || response.ID != 0 || response.ParentID != 0 || response.Request != nil || response.Response != nil || response.Adopt != nil || response.Error != "" || len(response.Snapshot)+len(response.SnapshotContext)+len(response.SnapshotConfig) != 0 {
		return nil, errors.New("captain: snapshot owner rejected exact build handshake")
	}
	if !stop() {
		return nil, ctx.Err()
	}
	owner := &snapshotOwner{cancelGrace: workerSettlementTimeout, conn: conn, pending: make(map[uint64]snapshotPending)}
	go owner.readLoop()
	return owner, nil
}

func (o *snapshotOwner) write(ctx context.Context, encoded []byte) error {
	o.writeMu.Lock()
	defer o.writeMu.Unlock()
	if err := ctx.Err(); err != nil {
		return err
	}
	deadline, ok := ctx.Deadline()
	if !ok {
		deadline = time.Now().Add(workerSettlementTimeout)
	}
	if err := o.conn.SetWriteDeadline(deadline); err != nil {
		return err
	}
	defer o.conn.SetWriteDeadline(time.Time{})
	_, err := o.conn.Write(encoded)
	return err
}

func (o *snapshotOwner) call(ctx context.Context, request json.RawMessage, callContext snapshots.CallContext, settled func()) (json.RawMessage, error) {
	encodedContext, err := wireproto.Marshal(callContext)
	if err != nil {
		settled()
		return nil, err
	}
	if err := snapshots.Validate("context", encodedContext); err != nil {
		settled()
		return nil, err
	}
	o.mu.Lock()
	if o.closed {
		err := o.err
		o.mu.Unlock()
		settled()
		return nil, err
	}
	o.nextID++
	id := o.nextID
	o.mu.Unlock()
	frame, err := wireproto.EncodeFrameBytes(wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpSnapshotRequest, ID: id, Snapshot: request, SnapshotContext: encodedContext}, snapshots.MaxFrameBytes)
	if err != nil {
		settled()
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		settled()
		return nil, err
	}
	var requestMetadata struct {
		Request snapshots.RequestMetadata `json:"request"`
	}
	if err := json.Unmarshal(request, &requestMetadata); err != nil {
		settled()
		return nil, err
	}
	o.mu.Lock()
	if o.closed {
		err := o.err
		o.mu.Unlock()
		settled()
		return nil, err
	}
	pending := snapshotPending{done: make(chan struct{}), requestID: requestMetadata.Request.ID, result: make(chan snapshotResult, 1), settled: settled}
	o.pending[id] = pending
	o.mu.Unlock()
	writeCtx, stopWrite := context.WithTimeout(context.Background(), workerSettlementTimeout)
	err = o.write(writeCtx, frame)
	stopWrite()
	if err != nil {
		o.fail(err, "request_write")
		return nil, err
	}
	select {
	case result := <-pending.result:
		return result.body, result.err
	case <-ctx.Done():
		cancelCtx, cancel := context.WithTimeout(context.Background(), workerSettlementTimeout)
		defer cancel()
		cancelFrame, err := wireproto.EncodeFrameBytes(wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpSnapshotCancel, ID: id}, snapshots.MaxFrameBytes)
		if err == nil {
			err = o.write(cancelCtx, cancelFrame)
		}
		if err != nil {
			o.fail(err, "cancellation_write")
		}
		go o.awaitCancellation(id, pending.done)
		return nil, ctx.Err()
	}
}

func (o *snapshotOwner) readLoop() {
	for {
		frame, err := wireproto.DecodeFrameLimit(o.conn, snapshots.MaxFrameBytes)
		if err != nil {
			o.fail(err, "response_read")
			return
		}
		if frame.ID == 0 || frame.ParentID != 0 || frame.Build != "" || frame.Request != nil || frame.Response != nil || frame.Adopt != nil || len(frame.SnapshotContext)+len(frame.SnapshotConfig) != 0 {
			o.fail(errors.New("captain: invalid snapshot owner response frame"), "invalid_response_frame")
			return
		}
		result := snapshotResult{}
		switch frame.Op {
		case wireproto.OpSnapshotResult:
			if frame.Error != "" {
				o.fail(errors.New("captain: snapshot result contains error"), "result_contains_error")
				return
			}
			if err := snapshots.Validate("host-response", frame.Snapshot); err != nil {
				o.fail(err, "invalid_host_response")
				return
			}
			result.body = frame.Snapshot
		case wireproto.OpError:
			if frame.Error == "" || len(frame.Snapshot) != 0 {
				o.fail(errors.New("captain: invalid snapshot owner error"), "invalid_error_frame")
				return
			}
			result.err = errors.New(frame.Error)
		default:
			o.fail(errors.New("captain: invalid snapshot owner operation"), "invalid_response_operation")
			return
		}
		var wrapper struct {
			Response snapshots.RequestMetadata `json:"response"`
		}
		if frame.Op == wireproto.OpSnapshotResult {
			if err := json.Unmarshal(result.body, &wrapper); err != nil {
				o.fail(err, "invalid_result_metadata")
				return
			}
		}
		o.mu.Lock()
		pending, ok := o.pending[frame.ID]
		if ok && frame.Op == wireproto.OpSnapshotResult && wrapper.Response.ID != pending.requestID {
			o.mu.Unlock()
			o.fail(errors.New("captain: snapshot response id mismatch"), "response_id_mismatch")
			return
		}
		if ok {
			delete(o.pending, frame.ID)
		}
		o.mu.Unlock()
		if !ok {
			o.fail(errors.New("captain: unknown snapshot owner request id"), "unknown_response_id")
			return
		}
		close(pending.done)
		pending.settled()
		pending.result <- result
	}
}

func (o *snapshotOwner) awaitCancellation(id uint64, done <-chan struct{}) {
	timer := time.NewTimer(o.cancelGrace)
	defer timer.Stop()
	select {
	case <-done:
		return
	case <-timer.C:
	}
	o.mu.Lock()
	if _, pending := o.pending[id]; !pending {
		o.mu.Unlock()
		return
	}
	o.failLocked(errors.New("captain: snapshot owner failed to acknowledge cancellation"), "cancellation_ack_timeout")
}

func (o *snapshotOwner) fail(err error, cause string) {
	o.mu.Lock()
	o.failLocked(err, cause)
}

func (o *snapshotOwner) failLocked(err error, cause string) {
	if o.closed {
		o.mu.Unlock()
		return
	}
	o.closed, o.err = true, err
	pending := o.pending
	o.failure = snapshotOwnerFailure{at: time.Now(), cause: cause, pending: len(pending)}
	o.pending = make(map[uint64]snapshotPending)
	o.mu.Unlock()
	_ = o.conn.Close()
	for _, request := range pending {
		close(request.done)
		request.settled()
	}
	for _, request := range pending {
		request.result <- snapshotResult{err: err}
	}
}

func (o *snapshotOwner) exited(exit daemonkit.Exit, log snapshotOwnerLog) {
	o.fail(fmt.Errorf("captain: snapshot owner exited: %v", exit), "child_exit")
	o.mu.Lock()
	failure := o.failure
	o.mu.Unlock()
	log.failure(failure.at, failure.cause, failure.pending)
	log.terminal(time.Now(), exit)
}

func (o *snapshotOwner) stop(ctx context.Context) error {
	o.fail(net.ErrClosed, "service_stop")
	if o.child == nil {
		return nil
	}
	_, err := o.child.Stop(ctx)
	return err
}

type snapshotService struct {
	manager      *workerManager
	mu           sync.Mutex
	owner        *snapshotOwner
	starting     *snapshotStartup
	epoch        uint64
	closed       bool
	hookSlots    chan struct{}
	reviewSlots  chan struct{}
	warmSlots    chan struct{}
	hookQueue    chan struct{}
	reviewQueue  chan struct{}
	warmQueue    chan struct{}
	releaseSlots chan struct{}
	releaseQueue chan struct{}
	config       json.RawMessage
	start        func(context.Context) (*snapshotOwner, error)
	warm         *warmCoordinator
}

func newSnapshotService(manager *workerManager) (*snapshotService, error) {
	config, err := snapshots.HostConfig(hostFactProducer())
	if err != nil {
		return nil, err
	}
	service := &snapshotService{manager: manager, config: config,
		hookSlots: make(chan struct{}, 4), reviewSlots: make(chan struct{}, 1),
		hookQueue: make(chan struct{}, 64), reviewQueue: make(chan struct{}, 16),
		warmSlots: make(chan struct{}, 1), warmQueue: make(chan struct{}, 4),
		releaseSlots: make(chan struct{}, 4), releaseQueue: make(chan struct{}, 64)}
	service.start = service.startOwner
	service.warm = newWarmCoordinator(func(ctx context.Context, request json.RawMessage, callContext snapshots.CallContext) (json.RawMessage, error) {
		return service.admit(ctx, service.warmSlots, service.warmQueue, request, callContext)
	}, manager.now, manager.logWriter)
	return service, nil
}

type snapshotStartup struct {
	ready    chan struct{}
	bound    *startup
	owner    *snapshotOwner
	err      error
	closeErr error
}

func (s *snapshotService) startups() (uint64, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.epoch, s.starting != nil
}

func (s *snapshotService) get(ctx context.Context) (*snapshotOwner, error) {
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return nil, net.ErrClosed
	}
	if s.owner != nil {
		s.owner.mu.Lock()
		closed := s.owner.closed
		s.owner.mu.Unlock()
		if !closed {
			owner := s.owner
			s.mu.Unlock()
			return owner, nil
		}
	}
	deadline, _ := ctx.Deadline()
	var leave func()
	if s.starting == nil {
		startup := &snapshotStartup{ready: make(chan struct{})}
		startup.bound, leave = newStartup(s.manager.lifetime, s.manager.now, s.manager.readiness, deadline)
		s.starting = startup
		s.epoch++
		previous := s.owner
		s.owner = nil
		go s.startGeneration(startup, previous)
	}
	startup := s.starting
	if leave == nil {
		leave = startup.bound.join(deadline)
	}
	s.mu.Unlock()
	defer leave()
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-startup.ready:
		return startup.owner, startup.err
	}
}

func (s *snapshotService) startGeneration(startup *snapshotStartup, previous *snapshotOwner) {
	ctx := startup.bound.ctx
	defer startup.bound.end()
	if previous != nil {
		stopCtx, cancel := context.WithTimeout(ctx, workerSettlementTimeout)
		startup.err = previous.stop(stopCtx)
		cancel()
	}
	if startup.err == nil {
		startup.owner, startup.err = s.start(ctx)
	}
	s.mu.Lock()
	closed := s.closed
	if !closed {
		if startup.err == nil {
			s.owner = startup.owner
		} else if previous != nil {
			s.owner = previous
		}
	}
	s.mu.Unlock()
	if closed {
		startup.closeErr = startup.err
	}
	if closed && startup.owner != nil {
		stopCtx, stop := context.WithTimeout(context.Background(), workerSettlementTimeout)
		startup.closeErr = startup.owner.stop(stopCtx)
		startup.err = errors.Join(net.ErrClosed, startup.err, startup.closeErr)
		startup.owner = nil
		stop()
	}
	if startup.err != nil {
		fmt.Fprintf(s.manager.logWriter, "captain: start snapshot owner: %v\n", startup.err)
	}
	s.mu.Lock()
	s.starting = nil
	s.epoch++
	close(startup.ready)
	s.mu.Unlock()
}

func (s *snapshotService) call(ctx context.Context, request json.RawMessage, callContext snapshots.CallContext) (json.RawMessage, error) {
	metadata, err := snapshots.Metadata(request)
	if err != nil {
		return nil, err
	}
	deadline := time.Now().Add(120 * time.Second)
	if metadata.Operation == "release" {
		deadline = time.Now().Add(workerSettlementTimeout)
	}
	if metadata.DeadlineUnixMS != 0 {
		deadline = minTime(deadline, time.UnixMilli(metadata.DeadlineUnixMS))
	}
	ctx, cancel := context.WithDeadline(ctx, deadline)
	defer cancel()
	if metadata.Operation == "warm_registered" || metadata.Operation == "warm_root" {
		body, err := s.admit(ctx, s.warmSlots, s.warmQueue, request, callContext)
		s.warm.charge(decodeWarmReply(body, err))
		return body, err
	}
	slots, queue := s.hookSlots, s.hookQueue
	switch {
	case metadata.Operation == "release":
		slots, queue = s.releaseSlots, s.releaseQueue
	case metadata.Operation == "prepare_review", metadata.Operation == "prepare_corrections", callContext.Admission != "hook":
		slots, queue = s.reviewSlots, s.reviewQueue
	}
	return s.admit(ctx, slots, queue, request, callContext)
}

func (s *snapshotService) admit(ctx context.Context, slots, queue chan struct{}, request json.RawMessage, callContext snapshots.CallContext) (json.RawMessage, error) {
	select {
	case queue <- struct{}{}:
	default:
		return nil, errors.New("captain: snapshot admission queue exhausted")
	}
	defer func() { <-queue }()
	select {
	case slots <- struct{}{}:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	var once sync.Once
	settled := func() { once.Do(func() { <-slots }) }
	owner, err := s.get(ctx)
	if err != nil {
		settled()
		return nil, err
	}
	return owner.call(ctx, request, callContext, settled)
}

func minTime(a, b time.Time) time.Time {
	if a.Before(b) {
		return a
	}
	return b
}

func maxTime(a, b time.Time) time.Time {
	if a.After(b) {
		return a
	}
	return b
}

func (s *snapshotService) startOwner(ctx context.Context) (*snapshotOwner, error) {
	python, err := installedPython()
	if err != nil {
		return nil, err
	}
	cmd := daemonkit.Cmd{Path: python, Args: []string{"-P", "-m", "captain_hook.snapshots.worker"},
		Dir: filepath.Clean(os.TempDir()), Env: workerBaseEnvironment(os.Environ()), Session: true}
	spawnCtx, cancel := context.WithTimeout(ctx, workerReadinessTimeout)
	defer cancel()
	child, err := spawnPastEAGAIN(spawnCtx, spawnBackoff, func() (*daemonkit.Child, error) {
		return s.manager.owner.Spawn(spawnCtx, cmd, daemonkit.ChannelStdio, s.manager.logWriter)
	})
	if err != nil {
		return nil, err
	}
	conn, err := child.Conn()
	if err != nil {
		return nil, s.manager.stopChild(child, errors.New("captain: snapshot owner pipe"), err)
	}
	row, known := newProcSource().probe(child.PID())
	start := "unknown"
	if known {
		start = fmt.Sprintf("%d.%06d", row.StartUnix, row.StartUsec)
	}
	log := snapshotOwnerLog{writer: s.manager.logWriter, pid: child.PID(), start: start, known: known}
	owner, err := handshakeSnapshotOwner(ctx, conn, s.config)
	if err != nil {
		_ = conn.Close()
		return nil, s.manager.stopChild(child, errors.New("captain: snapshot owner handshake"), err)
	}
	owner.child = child
	go func() { owner.exited(<-child.Done(), log) }()
	return owner, nil
}

func (s *snapshotService) Close(ctx context.Context) error {
	s.warm.close()
	s.mu.Lock()
	s.closed = true
	owner, starting := s.owner, s.starting
	s.mu.Unlock()
	if starting != nil {
		select {
		case <-starting.ready:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	var err error
	if owner != nil {
		err = owner.stop(ctx)
	} else if starting != nil {
		err = starting.closeErr
	}
	if waitErr := s.warm.wait(ctx); waitErr != nil {
		return errors.Join(err, waitErr)
	}
	return err
}
