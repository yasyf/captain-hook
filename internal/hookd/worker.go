package hookd

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"sync"
	"time"

	"github.com/yasyf/daemonkit/proc"
)

type workerResult struct {
	response EventResponse
	err      error
}

type workerClient struct {
	conn    net.Conn
	build   string
	child   *proc.PreparedChild
	receipt proc.ProcessReceipt

	writeMu sync.Mutex
	mu      sync.Mutex
	nextID  uint64
	pending map[uint64]chan workerResult
	closed  bool
	err     error

	stopOnce sync.Once
	stopErr  error
}

type workerPipeConn struct {
	reader *os.File
	writer *os.File
	once   sync.Once
	err    error
}

type workerPipeAddr struct{}

func (workerPipeAddr) Network() string { return "pipe" }
func (workerPipeAddr) String() string  { return "captain-hook-worker" }

func newWorkerPipeConn(reader, writer *os.File) net.Conn {
	return &workerPipeConn{reader: reader, writer: writer}
}

func (c *workerPipeConn) Read(payload []byte) (int, error)  { return c.reader.Read(payload) }
func (c *workerPipeConn) Write(payload []byte) (int, error) { return c.writer.Write(payload) }
func (c *workerPipeConn) LocalAddr() net.Addr               { return workerPipeAddr{} }
func (c *workerPipeConn) RemoteAddr() net.Addr              { return workerPipeAddr{} }

func (c *workerPipeConn) Close() error {
	c.once.Do(func() { c.err = errors.Join(c.reader.Close(), c.writer.Close()) })
	return c.err
}

func (c *workerPipeConn) SetDeadline(deadline time.Time) error {
	return errors.Join(c.reader.SetReadDeadline(deadline), c.writer.SetWriteDeadline(deadline))
}

func (c *workerPipeConn) SetReadDeadline(deadline time.Time) error {
	return c.reader.SetReadDeadline(deadline)
}

func (c *workerPipeConn) SetWriteDeadline(deadline time.Time) error {
	return c.writer.SetWriteDeadline(deadline)
}

func handshakeWorker(ctx context.Context, conn net.Conn, build string) (*workerClient, error) {
	if deadline, ok := ctx.Deadline(); ok {
		if err := conn.SetDeadline(deadline); err != nil {
			return nil, err
		}
	}
	if err := encodeWorkerFrame(conn, workerFrame{Protocol: Schema, Op: "hello", Build: build}); err != nil {
		return nil, err
	}
	response, err := decodeWorkerFrame(conn)
	if err != nil {
		return nil, err
	}
	if response.Op != "hello" || response.ID != 0 || response.Build != build || response.Request != nil ||
		response.Response != nil || response.Error != "" {
		return nil, errors.New("captain: Python worker rejected the exact build handshake")
	}
	if err := conn.SetDeadline(time.Time{}); err != nil {
		return nil, err
	}
	w := &workerClient{conn: conn, build: build, pending: make(map[uint64]chan workerResult)}
	go w.readLoop()
	return w, nil
}

func (w *workerClient) call(ctx context.Context, request EventRequest) (EventResponse, error) {
	w.mu.Lock()
	if w.closed {
		err := w.err
		w.mu.Unlock()
		if err == nil {
			err = net.ErrClosed
		}
		return EventResponse{}, err
	}
	w.nextID++
	id := w.nextID
	result := make(chan workerResult, 1)
	w.pending[id] = result
	w.mu.Unlock()

	if err := w.write(ctx, workerFrame{Protocol: Schema, Op: "event", ID: id, Request: &request}); err != nil {
		w.removePending(id)
		return EventResponse{}, err
	}
	select {
	case received := <-result:
		return received.response, received.err
	case <-ctx.Done():
		return EventResponse{}, ctx.Err()
	}
}

func (w *workerClient) write(ctx context.Context, frame workerFrame) error {
	w.writeMu.Lock()
	defer w.writeMu.Unlock()
	if deadline, ok := ctx.Deadline(); ok {
		if err := w.conn.SetWriteDeadline(deadline); err != nil {
			return err
		}
		defer w.conn.SetWriteDeadline(time.Time{}) //nolint:errcheck
	}
	return encodeWorkerFrame(w.conn, frame)
}

func (w *workerClient) readLoop() {
	for {
		frame, err := decodeWorkerFrame(w.conn)
		if err != nil {
			w.fail(err)
			return
		}
		if frame.ID == 0 || (frame.Op != "result" && frame.Op != "error") || frame.Request != nil {
			w.fail(errors.New("captain: invalid Python worker response frame"))
			return
		}
		w.mu.Lock()
		pending := w.pending[frame.ID]
		delete(w.pending, frame.ID)
		w.mu.Unlock()
		if pending == nil {
			w.fail(fmt.Errorf("captain: Python worker returned unknown request id %d", frame.ID))
			return
		}
		if frame.Op == "error" {
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
		if err := validateEventResponse(*frame.Response); err != nil {
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
	pending := w.pending
	w.pending = make(map[uint64]chan workerResult)
	w.mu.Unlock()
	_ = w.conn.Close()
	for _, waiter := range pending {
		waiter <- workerResult{err: err}
	}
}

func (w *workerClient) stop(ctx context.Context) error {
	w.stopOnce.Do(func() {
		w.fail(net.ErrClosed)
		if w.child != nil {
			w.stopErr = w.child.Stop(ctx)
		}
	})
	return w.stopErr
}
