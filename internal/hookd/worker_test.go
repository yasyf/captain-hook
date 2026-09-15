package hookd

import (
	"context"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
)

func TestWorkerHandshakeAndMultiplexedCalls(t *testing.T) {
	t.Parallel()
	clientConn, serverConn := net.Pipe()
	defer serverConn.Close()
	serverDone := make(chan error, 1)
	go func() {
		hello, err := wireproto.DecodeFrame(serverConn)
		if err != nil {
			serverDone <- err
			return
		}
		if err := wireproto.EncodeFrame(serverConn, wireproto.Frame{
			Protocol: wireproto.Schema, Op: "hello", Build: hello.Build,
		}); err != nil {
			serverDone <- err
			return
		}
		requests := make([]wireproto.Frame, 0, 2)
		for range 2 {
			request, err := wireproto.DecodeFrame(serverConn)
			if err != nil {
				serverDone <- err
				return
			}
			requests = append(requests, request)
		}
		for i := len(requests) - 1; i >= 0; i-- {
			request := requests[i]
			if err := wireproto.EncodeFrame(serverConn, wireproto.Frame{
				Protocol: wireproto.Schema, Op: "result", ID: request.ID,
				Response: &wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: request.Request.Event},
			}); err != nil {
				serverDone <- err
				return
			}
		}
		serverDone <- nil
	}()

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	worker, err := handshakeWorker(ctx, clientConn, "12.9.1")
	if err != nil {
		t.Fatalf("handshakeWorker: %v", err)
	}
	defer worker.fail(net.ErrClosed)
	var wg sync.WaitGroup
	responses := make(chan wireproto.EventResponse, 2)
	for _, event := range []string{"PreToolUse", "PostToolUse"} {
		wg.Add(1)
		go func() {
			defer wg.Done()
			response, callErr := worker.call(ctx, testEventRequest(event))
			if callErr != nil {
				t.Errorf("call %s: %v", event, callErr)
				return
			}
			responses <- response
		}()
	}
	wg.Wait()
	close(responses)
	seen := map[string]bool{}
	for response := range responses {
		seen[response.Stdout] = true
	}
	if !seen["PreToolUse"] || !seen["PostToolUse"] {
		t.Fatalf("responses = %v", seen)
	}
	if err := <-serverDone; err != nil {
		t.Fatal(err)
	}
}

func TestWorkerProtocolViolationFailsEveryPendingCall(t *testing.T) {
	t.Parallel()
	clientConn, serverConn := net.Pipe()
	defer serverConn.Close()
	go func() {
		hello, _ := wireproto.DecodeFrame(serverConn)
		_ = wireproto.EncodeFrame(serverConn, wireproto.Frame{Protocol: wireproto.Schema, Op: "hello", Build: hello.Build})
		request, _ := wireproto.DecodeFrame(serverConn)
		_ = wireproto.EncodeFrame(serverConn, wireproto.Frame{Protocol: wireproto.Schema, Op: "result", ID: request.ID + 1,
			Response: &wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok"}})
	}()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	worker, err := handshakeWorker(ctx, clientConn, "12.9.1")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := worker.call(ctx, testEventRequest("PreToolUse")); err == nil {
		t.Fatal("call succeeded after unknown worker response id")
	}
}

func testEventRequest(event string) wireproto.EventRequest {
	return wireproto.EventRequest{
		Schema: wireproto.Schema, Event: event, Root: "/tmp/repo", CWD: "/tmp/repo",
		Env:       map[string]string{},
		ClientPID: 10, ClientPPID: 9,
	}
}
