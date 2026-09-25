package hookd

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"sync"
	"time"

	"github.com/yasyf/captain-hook/internal/snapshots"
	"github.com/yasyf/captain-hook/internal/wireproto"
	"github.com/yasyf/daemonkit"
)

const workerSlots = 64

type workerResult struct {
	response wireproto.EventResponse
	err      error
}

type workerClient struct {
	conn   net.Conn
	build  string
	python string
	child  *daemonkit.Child

	slots     chan struct{}
	writeMu   sync.Mutex
	mu        sync.Mutex
	nextID    uint64
	pending   map[uint64]chan workerResult
	abandoned map[uint64]struct{}
	closed    bool
	err       error

	stopMu  sync.Mutex
	stopped bool
	stopErr error

	snapshots         *snapshotService
	snapshotNamespace uint64
	snapshotEvents    map[uint64]snapshotEvent
	reverseSnapshots  map[uint64]reverseSnapshot

	background   map[uint64]struct{}
	onBackground func(int)
	onSettle     func()
	onAdopt      func(wireproto.AdoptRequest)
}

func (w *workerClient) setOnSettle(fn func()) {
	w.mu.Lock()
	w.onSettle = fn
	w.mu.Unlock()
}

func (w *workerClient) setOnBackground(fn func(int)) {
	w.mu.Lock()
	w.onBackground = fn
	w.mu.Unlock()
}

func (w *workerClient) backgroundFrame(frame wireproto.Frame) error {
	if frame.ID == 0 || frame.Build != "" || frame.Request != nil || frame.Response != nil || frame.Error != "" || frame.Adopt != nil {
		return errors.New("captain: invalid background ticket frame")
	}
	w.mu.Lock()
	_, exists := w.background[frame.ID]
	_, pending := w.pending[frame.ID]
	_, abandoned := w.abandoned[frame.ID]
	if frame.Op == wireproto.OpBackgroundBegin {
		if exists || !(pending || abandoned) {
			w.mu.Unlock()
			return errors.New("captain: background ticket requires an unfinished foreground request")
		}
		if w.background == nil {
			w.background = make(map[uint64]struct{})
		}
		w.background[frame.ID] = struct{}{}
	} else {
		if !exists || pending || abandoned {
			w.mu.Unlock()
			return errors.New("captain: unknown background ticket")
		}
		delete(w.background, frame.ID)
	}
	changed := w.onBackground
	w.mu.Unlock()
	if changed != nil {
		delta := -1
		if frame.Op == wireproto.OpBackgroundBegin {
			delta = 1
		}
		changed(delta)
	}
	return nil
}

func (w *workerClient) setOnAdopt(fn func(wireproto.AdoptRequest)) {
	w.mu.Lock()
	w.onAdopt = fn
	w.mu.Unlock()
}

// adopt hands a detached process the worker announced to whoever owns the
// host's process scope. It runs on the read loop, so the handler must not
// block: every session's replies queue behind it.
func (w *workerClient) adopt(frame wireproto.Frame) error {
	if frame.ID != 0 || frame.Adopt == nil || frame.Request != nil || frame.Response != nil || frame.Error != "" {
		return errors.New("captain: invalid Python worker adopt frame")
	}
	if err := frame.Adopt.Validate(); err != nil {
		return err
	}
	w.mu.Lock()
	adopt := w.onAdopt
	w.mu.Unlock()
	if adopt != nil {
		adopt(*frame.Adopt)
	}
	return nil
}

func (w *workerClient) notifySettled(n int) {
	if n <= 0 {
		return
	}
	w.mu.Lock()
	settle := w.onSettle
	w.mu.Unlock()
	if settle == nil {
		return
	}
	for range n {
		settle()
	}
}

func handshakeWorker(ctx context.Context, conn net.Conn, build string) (*workerClient, error) {
	if deadline, ok := ctx.Deadline(); ok {
		if err := conn.SetDeadline(deadline); err != nil {
			return nil, err
		}
	}
	if err := wireproto.EncodeFrame(conn, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpHello, Build: build}); err != nil {
		return nil, err
	}
	response, err := wireproto.DecodeFrame(conn)
	if err != nil {
		return nil, err
	}
	if response.Op != wireproto.OpHello || response.ID != 0 || response.Build != build || response.Request != nil ||
		response.Response != nil || response.Error != "" || response.Adopt != nil || response.ParentID != 0 || len(response.Snapshot)+len(response.SnapshotContext)+len(response.SnapshotConfig) != 0 {
		return nil, errors.New("captain: Python worker rejected the exact build handshake")
	}
	if err := conn.SetDeadline(time.Time{}); err != nil {
		return nil, err
	}
	w := &workerClient{snapshotNamespace: workerNamespace.Add(1), snapshotEvents: make(map[uint64]snapshotEvent), reverseSnapshots: make(map[uint64]reverseSnapshot), conn: conn, build: build, slots: make(chan struct{}, workerSlots), pending: make(map[uint64]chan workerResult), abandoned: make(map[uint64]struct{})}
	go w.readLoop()
	return w, nil
}

func (w *workerClient) call(ctx context.Context, request wireproto.EventRequest) (wireproto.EventResponse, error) {
	select {
	case w.slots <- struct{}{}:
	case <-ctx.Done():
		w.notifySettled(1)
		return wireproto.EventResponse{}, ctx.Err()
	}
	if err := ctx.Err(); err != nil {
		w.release(1)
		return wireproto.EventResponse{}, err
	}
	w.mu.Lock()
	if w.closed {
		err := w.err
		w.mu.Unlock()
		w.release(1)
		if err == nil {
			err = net.ErrClosed
		}
		return wireproto.EventResponse{}, err
	}
	w.nextID++
	id := w.nextID
	result := make(chan workerResult, 1)
	w.pending[id] = result
	eventCtx, eventCancel := context.WithCancel(ctx)
	w.snapshotEvents[id] = snapshotEvent{ctx: eventCtx, cancel: eventCancel}
	w.mu.Unlock()
	defer w.finishSnapshotEvent(id)

	if err := w.write(ctx, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpEvent, ID: id, Request: &request}); err != nil {
		w.removePending(id)
		if !errors.Is(err, wireproto.ErrPayloadTooLarge) {
			w.fail(err)
		}
		return wireproto.EventResponse{}, err
	}
	select {
	case received := <-result:
		return received.response, received.err
	case <-ctx.Done():
		w.abandon(id)
		return wireproto.EventResponse{}, ctx.Err()
	}
}

// broken reports whether the transport itself failed, which a caller's own
// expired deadline and a hook's own error both produce a call error without.
// Workers are shared across sessions, so retiring one on either of those takes
// down every other session mid-call.
func (w *workerClient) broken() bool {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.closed
}

func (w *workerClient) write(ctx context.Context, frame wireproto.Frame) error {
	w.writeMu.Lock()
	defer w.writeMu.Unlock()
	if deadline, ok := ctx.Deadline(); ok {
		if err := w.conn.SetWriteDeadline(deadline); err != nil {
			return err
		}
		defer w.conn.SetWriteDeadline(time.Time{}) //nolint:errcheck
	}
	if frame.Op == wireproto.OpSnapshotResult || frame.Op == wireproto.OpSnapshotRequest || frame.Op == wireproto.OpSnapshotCancel || frame.ParentID != 0 {
		return wireproto.EncodeFrameLimit(w.conn, frame, snapshots.MaxFrameBytes)
	}
	return wireproto.EncodeFrame(w.conn, frame)
}

func (w *workerClient) readLoop() {
	for {
		frame, err := wireproto.DecodeFrame(w.conn)
		if err != nil {
			w.fail(err)
			return
		}
		if frame.Op == wireproto.OpSnapshotRequest || frame.Op == wireproto.OpSnapshotCancel {
			if err := w.snapshotFrame(frame); err != nil {
				w.fail(err)
				return
			}
			continue
		}
		if len(frame.Snapshot)+len(frame.SnapshotContext)+len(frame.SnapshotConfig) != 0 || frame.ParentID != 0 {
			w.fail(errors.New("captain: snapshot fields on event response"))
			return
		}
		if frame.Op == wireproto.OpBackgroundBegin || frame.Op == wireproto.OpBackgroundEnd {
			if err := w.backgroundFrame(frame); err != nil {
				w.fail(err)
				return
			}
			continue
		}
		if frame.Op == wireproto.OpAdopt {
			if err := w.adopt(frame); err != nil {
				w.fail(err)
				return
			}
			continue
		}
		if frame.ID == 0 || (frame.Op != wireproto.OpResult && frame.Op != wireproto.OpError) || frame.Request != nil ||
			frame.Adopt != nil {
			w.fail(errors.New("captain: invalid Python worker response frame"))
			return
		}
		w.mu.Lock()
		pending := w.pending[frame.ID]
		delete(w.pending, frame.ID)
		_, abandoned := w.abandoned[frame.ID]
		delete(w.abandoned, frame.ID)
		w.mu.Unlock()
		if pending != nil || abandoned {
			w.release(1)
		}
		if pending == nil {
			if abandoned {
				continue
			}
			w.fail(fmt.Errorf("captain: Python worker returned unknown request id %d", frame.ID))
			return
		}
		if frame.Op == wireproto.OpError {
			if frame.Error == "" || frame.Response != nil {
				w.fail(errors.New("captain: invalid Python worker error frame"))
				return
			}
			pending <- workerResult{err: errors.New(frame.Error)}
			continue
		}
		if frame.Response == nil || frame.Error != "" {
			w.fail(errors.New("captain: invalid Python worker result frame"))
			return
		}
		if err := frame.Response.Validate(); err != nil {
			w.fail(err)
			return
		}
		pending <- workerResult{response: *frame.Response}
	}
}

func (w *workerClient) removePending(id uint64) {
	w.mu.Lock()
	_, ok := w.pending[id]
	delete(w.pending, id)
	w.mu.Unlock()
	if ok {
		w.release(1)
	}
}

func (w *workerClient) release(n int) {
	for range n {
		<-w.slots
	}
	w.notifySettled(n)
}

func (w *workerClient) abandon(id uint64) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if _, ok := w.pending[id]; ok {
		delete(w.pending, id)
		w.abandoned[id] = struct{}{}
	}
}

func (w *workerClient) fail(err error) {
	if err == nil {
		err = io.EOF
	}
	w.mu.Lock()
	if w.closed {
		w.mu.Unlock()
		return
	}
	w.closed = true
	for _, event := range w.snapshotEvents {
		event.cancel()
	}
	w.snapshotEvents = make(map[uint64]snapshotEvent)
	for _, pending := range w.reverseSnapshots {
		pending.cancel()
	}
	w.err = err
	pending, abandoned := w.pending, len(w.abandoned)
	w.pending = make(map[uint64]chan workerResult)
	w.abandoned = make(map[uint64]struct{})
	w.mu.Unlock()
	_ = w.conn.Close()
	w.release(len(pending) + abandoned)
	for _, waiter := range pending {
		waiter <- workerResult{err: err}
	}
}

func (w *workerClient) settled() bool {
	w.stopMu.Lock()
	defer w.stopMu.Unlock()
	return w.stopped && w.stopErr == nil
}

func (w *workerClient) stop(ctx context.Context) error {
	w.stopMu.Lock()
	defer w.stopMu.Unlock()
	if w.stopped {
		return w.stopErr
	}
	w.fail(net.ErrClosed)
	if w.child == nil {
		w.stopped = true
		return nil
	}
	if _, err := w.child.Stop(ctx); err != nil {
		w.stopErr = err
		return err
	}
	w.stopped, w.stopErr = true, nil
	return nil
}
