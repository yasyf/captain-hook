package hookd

import (
	"context"
	"errors"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
)

func silentWorker(t *testing.T) (*workerClient, net.Conn) {
	t.Helper()
	clientConn, serverConn := net.Pipe()
	t.Cleanup(func() { _ = serverConn.Close() })
	go func() {
		hello, err := wireproto.DecodeFrame(serverConn)
		if err != nil {
			return
		}
		_ = wireproto.EncodeFrame(serverConn, wireproto.Frame{Protocol: wireproto.Schema, Op: "hello", Build: hello.Build})
	}()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	worker, err := handshakeWorker(ctx, clientConn, "12.9.1")
	if err != nil {
		t.Fatalf("handshakeWorker: %v", err)
	}
	return worker, serverConn
}

func TestCallerTimeoutLeavesTheWorkerUsable(t *testing.T) {
	t.Parallel()
	worker, serverConn := silentWorker(t)
	go func() {
		for {
			if _, err := wireproto.DecodeFrame(serverConn); err != nil {
				return
			}
		}
	}()

	ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancel()
	if _, err := worker.call(ctx, testEventRequest("PreToolUse")); err == nil {
		t.Fatal("call against a worker that never answers succeeded")
	}

	if worker.broken() {
		t.Fatal("one caller's expired deadline broke the worker every other session shares")
	}
	worker.mu.Lock()
	pending := len(worker.pending)
	worker.mu.Unlock()
	if pending != 0 {
		t.Fatalf("pending after a caller timeout = %d, want 0; the entry leaks until the worker dies", pending)
	}
}

func lateReplyLeavesTheWorkerUsable(t *testing.T, late func(id uint64) wireproto.Frame) {
	t.Helper()
	worker, serverConn := silentWorker(t)
	go func() {
		abandoned, err := wireproto.DecodeFrame(serverConn)
		if err != nil {
			return
		}
		second, err := wireproto.DecodeFrame(serverConn)
		if err != nil {
			return
		}
		_ = wireproto.EncodeFrame(serverConn, late(abandoned.ID))
		_ = wireproto.EncodeFrame(serverConn, wireproto.Frame{
			Protocol: wireproto.Schema, Op: "result", ID: second.ID,
			Response: &wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: "second"},
		})
	}()

	timedOut, cancelTimedOut := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancelTimedOut()
	if _, err := worker.call(timedOut, testEventRequest("PreToolUse")); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("call against a worker that answers late = %v, want DeadlineExceeded", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	response, err := worker.call(ctx, testEventRequest("PostToolUse"))
	if err != nil {
		t.Fatalf("call concurrent with a late reply for an abandoned id = %v, want success", err)
	}
	if response.Stdout != "second" {
		t.Fatalf("Stdout = %q, want %q", response.Stdout, "second")
	}
	if worker.broken() {
		t.Fatal("a late reply for an id one caller abandoned broke the worker every other session shares")
	}
	worker.mu.Lock()
	pending, abandoned := len(worker.pending), len(worker.abandoned)
	worker.mu.Unlock()
	if pending != 0 || abandoned != 0 {
		t.Fatalf("pending, abandoned after the late reply = %d, %d; want 0, 0", pending, abandoned)
	}
}

func TestLateResultForAnAbandonedIDLeavesTheWorkerUsable(t *testing.T) {
	t.Parallel()
	lateReplyLeavesTheWorkerUsable(t, func(id uint64) wireproto.Frame {
		return wireproto.Frame{
			Protocol: wireproto.Schema, Op: "result", ID: id,
			Response: &wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: "late"},
		}
	})
}

func TestLateErrorForAnAbandonedIDLeavesTheWorkerUsable(t *testing.T) {
	t.Parallel()
	lateReplyLeavesTheWorkerUsable(t, func(id uint64) wireproto.Frame {
		return wireproto.Frame{Protocol: wireproto.Schema, Op: "error", ID: id, Error: "a hook raised late"}
	})
}

func TestReplyForANeverIssuedIDMarksTheWorkerBroken(t *testing.T) {
	t.Parallel()
	worker, serverConn := silentWorker(t)
	go func() {
		request, err := wireproto.DecodeFrame(serverConn)
		if err != nil {
			return
		}
		_ = wireproto.EncodeFrame(serverConn, wireproto.Frame{
			Protocol: wireproto.Schema, Op: "result", ID: request.ID + 1,
			Response: &wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok"},
		})
	}()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if _, err := worker.call(ctx, testEventRequest("PreToolUse")); err == nil {
		t.Fatal("call succeeded after a reply for an id this client never issued")
	}
	if !worker.broken() {
		t.Fatal("a reply for a never-issued id left the worker reading as usable")
	}
}

func TestProductErrorLeavesTheWorkerUsable(t *testing.T) {
	t.Parallel()
	clientConn, serverConn := net.Pipe()
	t.Cleanup(func() { _ = serverConn.Close() })
	go func() {
		hello, err := wireproto.DecodeFrame(serverConn)
		if err != nil {
			return
		}
		_ = wireproto.EncodeFrame(serverConn, wireproto.Frame{Protocol: wireproto.Schema, Op: "hello", Build: hello.Build})
		first, err := wireproto.DecodeFrame(serverConn)
		if err != nil {
			return
		}
		_ = wireproto.EncodeFrame(serverConn, wireproto.Frame{
			Protocol: wireproto.Schema, Op: "error", ID: first.ID, Error: "a hook raised",
		})
		second, err := wireproto.DecodeFrame(serverConn)
		if err != nil {
			return
		}
		_ = wireproto.EncodeFrame(serverConn, wireproto.Frame{
			Protocol: wireproto.Schema, Op: "result", ID: second.ID,
			Response: &wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: "second"},
		})
	}()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	worker, err := handshakeWorker(ctx, clientConn, "12.9.1")
	if err != nil {
		t.Fatalf("handshakeWorker: %v", err)
	}

	if _, err := worker.call(ctx, testEventRequest("PreToolUse")); err == nil {
		t.Fatal("a worker error frame returned no error")
	}
	if worker.broken() {
		t.Fatal("one hook's exception broke the interpreter every other session shares")
	}

	response, err := worker.call(ctx, testEventRequest("PostToolUse"))
	if err != nil {
		t.Fatalf("second call after a product error = %v, want success", err)
	}
	if response.Stdout != "second" {
		t.Fatalf("Stdout = %q, want %q", response.Stdout, "second")
	}
}

func TestOversizePayloadLeavesTheWorkerUsable(t *testing.T) {
	t.Parallel()
	worker, serverConn := silentWorker(t)
	go func() {
		for {
			if _, err := wireproto.DecodeFrame(serverConn); err != nil {
				return
			}
		}
	}()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	oversize := testEventRequest("PreToolUse")
	oversize.PayloadRaw = strings.Repeat("x", wireproto.MaxWorkerFrame+1)
	if _, err := worker.call(ctx, oversize); !errors.Is(err, wireproto.ErrPayloadTooLarge) {
		t.Fatalf("call with an oversize payload = %v, want ErrPayloadTooLarge", err)
	}
	if worker.broken() {
		t.Fatal("one oversize payload broke the interpreter every other session shares")
	}
}

func TestTransportFailureMarksTheWorkerBroken(t *testing.T) {
	t.Parallel()
	worker, serverConn := silentWorker(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	_ = serverConn.Close()
	if _, err := worker.call(ctx, testEventRequest("PreToolUse")); err == nil {
		t.Fatal("call over a severed transport succeeded")
	}
	if !worker.broken() {
		t.Fatal("a severed transport left the worker reading as usable")
	}
}
