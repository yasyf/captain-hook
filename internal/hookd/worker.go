package hookd

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"sync"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
	"github.com/yasyf/daemonkit"
)

type workerResult struct {
	response wireproto.EventResponse
	err      error
}

// abandonedCall is the error a call returns once its caller's context ended
// with the request still on the worker. settled closes when the worker is
// done with it — the late reply arrives, or the worker fails — and only then
// is the interpreter free of the work the caller walked away from.
type abandonedCall struct {
	cause   error
	settled <-chan struct{}
}

func (e *abandonedCall) Error() string { return e.cause.Error() }

func (e *abandonedCall) Unwrap() error { return e.cause }

type workerClient struct {
	conn  net.Conn
	build string
	child *daemonkit.Child

	writeMu   sync.Mutex
	mu        sync.Mutex
	nextID    uint64
	pending   map[uint64]chan workerResult
	abandoned map[uint64]chan struct{}
	closed    bool
	err       error

	stopMu  sync.Mutex
	stopped bool
	stopErr error
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
		response.Response != nil || response.Error != "" {
		return nil, errors.New("captain: Python worker rejected the exact build handshake")
	}
	if err := conn.SetDeadline(time.Time{}); err != nil {
		return nil, err
	}
	w := &workerClient{conn: conn, build: build, pending: make(map[uint64]chan workerResult), abandoned: make(map[uint64]chan struct{})}
	go w.readLoop()
	return w, nil
}

func (w *workerClient) call(ctx context.Context, request wireproto.EventRequest) (wireproto.EventResponse, error) {
	w.mu.Lock()
	if w.closed {
		err := w.err
		w.mu.Unlock()
		if err == nil {
			err = net.ErrClosed
		}
		return wireproto.EventResponse{}, err
	}
	w.nextID++
	id := w.nextID
	result := make(chan workerResult, 1)
	w.pending[id] = result
	w.mu.Unlock()

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
		return wireproto.EventResponse{}, &abandonedCall{cause: ctx.Err(), settled: w.abandon(id)}
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
	return wireproto.EncodeFrame(w.conn, frame)
}

func (w *workerClient) readLoop() {
	for {
		frame, err := wireproto.DecodeFrame(w.conn)
		if err != nil {
			w.fail(err)
			return
		}
		if frame.ID == 0 || (frame.Op != wireproto.OpResult && frame.Op != wireproto.OpError) || frame.Request != nil {
			w.fail(errors.New("captain: invalid Python worker response frame"))
			return
		}
		w.mu.Lock()
		pending := w.pending[frame.ID]
		delete(w.pending, frame.ID)
		settled, abandoned := w.abandoned[frame.ID]
		delete(w.abandoned, frame.ID)
		w.mu.Unlock()
		if pending == nil {
			if abandoned {
				close(settled)
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
	delete(w.pending, id)
	w.mu.Unlock()
}

// abandon stops waiting on id and returns the channel that closes once the
// worker is done with it. An id the worker already answered, or that a failure
// already swept, is settled at once.
func (w *workerClient) abandon(id uint64) <-chan struct{} {
	settled := make(chan struct{})
	w.mu.Lock()
	defer w.mu.Unlock()
	if _, ok := w.pending[id]; !ok {
		close(settled)
		return settled
	}
	delete(w.pending, id)
	w.abandoned[id] = settled
	return settled
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
	w.err = err
	pending, abandoned := w.pending, w.abandoned
	w.pending = make(map[uint64]chan workerResult)
	w.abandoned = make(map[uint64]chan struct{})
	w.mu.Unlock()
	_ = w.conn.Close()
	for _, waiter := range pending {
		waiter <- workerResult{err: err}
	}
	for _, settled := range abandoned {
		close(settled)
	}
}

// stop closes the session and terminates the child, latching only a proven
// exit. An unsettled stop stays retryable: the next caller — restart, Close, or
// the manager's own settlement — asks again instead of reading a cached refusal
// for a process that is still running.
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
