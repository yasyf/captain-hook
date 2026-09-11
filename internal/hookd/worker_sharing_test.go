package hookd

import (
	"context"
	"errors"
	"net"
	"strings"
	"testing"
	"time"
)

func silentWorker(t *testing.T) (*workerClient, net.Conn) {
	t.Helper()
	clientConn, serverConn := net.Pipe()
	t.Cleanup(func() { _ = serverConn.Close() })
	go func() {
		hello, err := decodeWorkerFrame(serverConn)
		if err != nil {
			return
		}
		_ = encodeWorkerFrame(serverConn, workerFrame{Protocol: Schema, Op: "hello", Build: hello.Build})
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
			if _, err := decodeWorkerFrame(serverConn); err != nil {
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

func TestProductErrorLeavesTheWorkerUsable(t *testing.T) {
	t.Parallel()
	clientConn, serverConn := net.Pipe()
	t.Cleanup(func() { _ = serverConn.Close() })
	go func() {
		hello, err := decodeWorkerFrame(serverConn)
		if err != nil {
			return
		}
		_ = encodeWorkerFrame(serverConn, workerFrame{Protocol: Schema, Op: "hello", Build: hello.Build})
		first, err := decodeWorkerFrame(serverConn)
		if err != nil {
			return
		}
		_ = encodeWorkerFrame(serverConn, workerFrame{
			Protocol: Schema, Op: "error", ID: first.ID, Error: "a hook raised",
		})
		second, err := decodeWorkerFrame(serverConn)
		if err != nil {
			return
		}
		_ = encodeWorkerFrame(serverConn, workerFrame{
			Protocol: Schema, Op: "result", ID: second.ID,
			Response: &EventResponse{Schema: Schema, Status: "ok", Stdout: "second"},
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
			if _, err := decodeWorkerFrame(serverConn); err != nil {
				return
			}
		}
	}()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	oversize := testEventRequest("PreToolUse")
	oversize.PayloadRaw = strings.Repeat("x", maxWorkerFrame+1)
	if _, err := worker.call(ctx, oversize); !errors.Is(err, ErrPayloadTooLarge) {
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
