package hookd

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"net"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/yasyf/captain-hook/internal/snapshots"
	"github.com/yasyf/captain-hook/internal/wireproto"
	"github.com/yasyf/daemonkit"
)

var statsRequest = json.RawMessage(`{"schema":"captain.transcript/1","tool_registry":[],"request":{"schema":"cc-transcript.snapshot/1","operation":"stats","id":"one"}}`)
var warmRequest = json.RawMessage(`{"schema":"captain.transcript/1","tool_registry":[],"request":{"schema":"cc-transcript.snapshot/1","operation":"warm_registered","id":"warm","classifier":{"id":"native","version":"1"},"thread_ids":["thread"],"roots":["/tmp"],"direct_paths":[],"start_index":0,"membership_revision":null,"deadline_unix_ms":9000000000000,"limits":{"max_read_bytes":8388608,"max_events":1000000,"max_items":65536,"max_output_bytes":16777216,"max_discovery_entries":50000,"max_sources":4096}}}`)
var warmRootRequest = json.RawMessage(`{"schema":"captain.transcript/1","tool_registry":[],"request":{"schema":"cc-transcript.snapshot/1","operation":"warm_root","id":"warm-root","path":"/tmp/root.jsonl","classifier":{"id":"native","version":"1"},"deadline_unix_ms":9000000000000,"limits":{"max_read_bytes":8388608,"max_events":1000000,"max_items":65536,"max_output_bytes":16777216,"max_discovery_entries":50000,"max_sources":4096}}}`)

func fakeSnapshotOwner(t *testing.T, frames chan<- wireproto.Frame) (*snapshotOwner, net.Conn) {
	t.Helper()
	client, server := net.Pipe()
	t.Cleanup(func() { _ = server.Close(); _ = client.Close() })
	ready := make(chan error, 1)
	go func() {
		hello, err := wireproto.DecodeFrameLimit(server, snapshots.MaxFrameBytes)
		if err != nil {
			ready <- err
			return
		}
		if err := snapshots.Validate("config", hello.SnapshotConfig); err != nil {
			ready <- err
			return
		}
		err = wireproto.EncodeFrameLimit(server, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpHello, Build: Build}, snapshots.MaxFrameBytes)
		ready <- err
		if err != nil {
			return
		}
		for {
			frame, err := wireproto.DecodeFrameLimit(server, snapshots.MaxFrameBytes)
			if err != nil {
				return
			}
			frames <- frame
		}
	}()
	config, err := snapshots.DefaultConfig()
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	owner, err := handshakeSnapshotOwner(ctx, client, config)
	if err != nil {
		t.Fatal(err)
	}
	if err := <-ready; err != nil {
		t.Fatal(err)
	}
	return owner, server
}

func failSnapshot(t *testing.T, server net.Conn, id uint64) {
	t.Helper()
	if err := wireproto.EncodeFrameLimit(server, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpError, ID: id, Error: "fixture"}, snapshots.MaxFrameBytes); err != nil {
		t.Fatal(err)
	}
}

func receiveSnapshot(t *testing.T, frames <-chan wireproto.Frame) wireproto.Frame {
	t.Helper()
	select {
	case frame := <-frames:
		return frame
	case <-time.After(time.Second):
		t.Fatal("snapshot frame timed out")
		return wireproto.Frame{}
	}
}

func TestSnapshotOwnerCancellationIsIsolated(t *testing.T) {
	frames := make(chan wireproto.Frame, 8)
	owner, server := fakeSnapshotOwner(t, frames)
	var settlements atomic.Int64
	settled := make(chan struct{}, 2)
	firstCtx, cancel := context.WithCancel(context.Background())
	first := make(chan error, 1)
	go func() {
		_, err := owner.call(firstCtx, statsRequest, userSnapshotContext("one", "hook", 501), func() { settlements.Add(1); settled <- struct{}{} })
		first <- err
	}()
	a := receiveSnapshot(t, frames)
	second := make(chan error, 1)
	go func() {
		_, err := owner.call(context.Background(), statsRequest, userSnapshotContext("two", "hook", 501), func() { settlements.Add(1); settled <- struct{}{} })
		second <- err
	}()
	b := receiveSnapshot(t, frames)
	if a.ID == b.ID {
		t.Fatal("owner IDs not unique")
	}
	cancel()
	cancelled := receiveSnapshot(t, frames)
	if cancelled.Op != wireproto.OpSnapshotCancel || cancelled.ID != a.ID {
		t.Fatalf("cancel=%+v", cancelled)
	}
	if err := <-first; !errors.Is(err, context.Canceled) {
		t.Fatalf("first=%v", err)
	}
	if settlements.Load() != 0 {
		t.Fatal("cancel released slot before owner settled")
	}
	failSnapshot(t, server, b.ID)
	if err := <-second; err == nil || err.Error() != "fixture" {
		t.Fatalf("second=%v", err)
	}
	failSnapshot(t, server, a.ID)
	<-settled
	<-settled
	owner.mu.Lock()
	pending := len(owner.pending)
	owner.mu.Unlock()
	if pending != 0 {
		t.Fatalf("pending=%d", pending)
	}
}

func TestSnapshotAdmissionReservesHookWhileReviewRuns(t *testing.T) {
	frames := make(chan wireproto.Frame, 8)
	owner, server := fakeSnapshotOwner(t, frames)
	manager := &workerManager{lifetime: context.Background(), logWriter: io.Discard}
	service, err := newSnapshotService(manager)
	if err != nil {
		t.Fatal(err)
	}
	service.owner = owner
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	reviewDone := make(chan error, 1)
	go func() {
		_, err := service.call(ctx, statsRequest, userSnapshotContext("review:1", "review", 501))
		reviewDone <- err
	}()
	review := receiveSnapshot(t, frames)
	secondCtx, stop := context.WithCancel(ctx)
	secondDone := make(chan error, 1)
	go func() {
		_, err := service.call(secondCtx, statsRequest, userSnapshotContext("review:2", "review", 501))
		secondDone <- err
	}()
	hookDone := make(chan error, 1)
	go func() {
		_, err := service.call(ctx, statsRequest, userSnapshotContext("hook:1", "hook", 501))
		hookDone <- err
	}()
	hook := receiveSnapshot(t, frames)
	var authority snapshots.CallContext
	if err := json.Unmarshal(hook.SnapshotContext, &authority); err != nil {
		t.Fatal(err)
	}
	if authority.Admission != "hook" || authority.Authority.Kind != "user" || authority.Authority.EffectiveUID != "501" {
		t.Fatalf("context=%+v", authority)
	}
	stop()
	if err := <-secondDone; !errors.Is(err, context.Canceled) {
		t.Fatalf("queued review=%v", err)
	}
	failSnapshot(t, server, hook.ID)
	<-hookDone
	failSnapshot(t, server, review.ID)
	<-reviewDone
	select {
	case unexpected := <-frames:
		t.Fatalf("queued review reached owner: %+v", unexpected)
	default:
	}
}

func TestSnapshotOwnerStartupSharedAndNotBoundToFirstWaiter(t *testing.T) {
	service, err := newSnapshotService(&workerManager{lifetime: context.Background(), logWriter: io.Discard})
	if err != nil {
		t.Fatal(err)
	}
	entered := make(chan struct{})
	release := make(chan struct{})
	var starts atomic.Int64
	owner := &snapshotOwner{}
	service.start = func(ctx context.Context) (*snapshotOwner, error) {
		starts.Add(1)
		close(entered)
		select {
		case <-release:
			return owner, nil
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	first := make(chan error, 1)
	go func() { _, err := service.get(ctx); first <- err }()
	<-entered
	cancel()
	if err := <-first; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	for range 8 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			got, err := service.get(context.Background())
			if err != nil || got != owner {
				t.Errorf("get=%p,%v", got, err)
			}
		}()
	}
	close(release)
	wg.Wait()
	if starts.Load() != 1 {
		t.Fatalf("starts=%d", starts.Load())
	}
}

func TestSnapshotInvalidRequestNeverStartsOwner(t *testing.T) {
	service, err := newSnapshotService(&workerManager{lifetime: context.Background(), logWriter: io.Discard})
	if err != nil {
		t.Fatal(err)
	}
	service.start = func(context.Context) (*snapshotOwner, error) {
		t.Error("started for invalid request")
		return nil, errors.New("unexpected")
	}
	_, err = service.call(context.Background(), json.RawMessage(`{"schema":"captain.transcript/1","tool_registry":[],"request":{"operation":"shell"}}`), userSnapshotContext("x", "review", 501))
	if err == nil {
		t.Fatal("invalid request admitted")
	}
}

func TestTranscriptBridgeRejectsOversizeBeforeBusinessCall(t *testing.T) {
	var input, output bytes.Buffer
	if err := wireproto.EncodeFrameLimit(&input, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpHello, Build: Build}, snapshots.MaxFrameBytes); err != nil {
		t.Fatal(err)
	}
	var header [4]byte
	binary.BigEndian.PutUint32(header[:], snapshots.MaxFrameBytes+1)
	input.Write(header[:])
	err := runTranscriptBridge(&input, &output, func(context.Context, string, []byte) ([]byte, error) {
		t.Fatal("oversized request dispatched")
		return nil, nil
	})
	if err == nil {
		t.Fatal("oversized request accepted")
	}
}

func TestReverseSnapshotRequiresActiveEvent(t *testing.T) {
	worker := &workerClient{snapshotEvents: map[uint64]snapshotEvent{}, reverseSnapshots: map[uint64]reverseSnapshot{}}
	frame := wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpSnapshotRequest, ID: 1, ParentID: 42, Snapshot: statsRequest}
	if err := worker.snapshotFrame(frame); err == nil {
		t.Fatal("inactive parent admitted")
	}
}

func TestSnapshotRejectedWaiterDoesNotBreakOwner(t *testing.T) {
	frames := make(chan wireproto.Frame, 8)
	owner, server := fakeSnapshotOwner(t, frames)
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	first := make(chan error, 1)
	go func() {
		_, err := owner.call(ctx, statsRequest, userSnapshotContext("one", "hook", 501), func() {})
		first <- err
	}()
	frame := receiveSnapshot(t, frames)
	cancelled, stop := context.WithCancel(ctx)
	stop()
	if _, err := owner.call(cancelled, statsRequest, userSnapshotContext("two", "hook", 501), func() {}); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancelled=%v", err)
	}
	oversized := json.RawMessage(`"` + string(bytes.Repeat([]byte("x"), snapshots.MaxFrameBytes)) + `"`)
	if _, err := owner.call(ctx, oversized, userSnapshotContext("three", "hook", 501), func() {}); err == nil {
		t.Fatal("oversized waiter accepted")
	}
	failSnapshot(t, server, frame.ID)
	if err := <-first; err == nil || err.Error() != "fixture" {
		t.Fatalf("first=%v", err)
	}
	owner.mu.Lock()
	closed := owner.closed
	owner.mu.Unlock()
	if closed {
		t.Fatal("rejected waiter broke shared owner")
	}
}

func TestTranscriptBridgeCancellationUsesRequestContext(t *testing.T) {
	client, server := net.Pipe()
	defer client.Close()
	entered := make(chan struct{})
	cancelled := make(chan struct{})
	done := make(chan error, 1)
	go func() {
		done <- runTranscriptBridge(server, server, func(ctx context.Context, op string, body []byte) ([]byte, error) {
			if op != opTranscript || !bytes.Equal(body, statsRequest) {
				t.Errorf("business call=%s %s", op, body)
			}
			close(entered)
			<-ctx.Done()
			close(cancelled)
			return nil, ctx.Err()
		})
	}()
	if err := wireproto.EncodeFrameLimit(client, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpHello, Build: Build}, snapshots.MaxFrameBytes); err != nil {
		t.Fatal(err)
	}
	if _, err := wireproto.DecodeFrameLimit(client, snapshots.MaxFrameBytes); err != nil {
		t.Fatal(err)
	}
	if err := wireproto.EncodeFrameLimit(client, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpSnapshotRequest, ID: 9, Snapshot: statsRequest}, snapshots.MaxFrameBytes); err != nil {
		t.Fatal(err)
	}
	<-entered
	if err := wireproto.EncodeFrameLimit(client, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpSnapshotCancel, ID: 9}, snapshots.MaxFrameBytes); err != nil {
		t.Fatal(err)
	}
	<-cancelled
	response, err := wireproto.DecodeFrameLimit(client, snapshots.MaxFrameBytes)
	if err != nil {
		t.Fatal(err)
	}
	if response.ID != 9 || response.Op != wireproto.OpError {
		t.Fatalf("response=%+v", response)
	}
	_ = client.Close()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("bridge did not settle")
	}
}

func snapshotFailure(t *testing.T, id string) json.RawMessage {
	t.Helper()
	usage := map[string]int{}
	for _, name := range []string{"source_opens", "source_bytes_read", "bytes_decoded", "events_parsed", "cold_parses", "append_parses", "activity_lifts", "cache_hits", "inflight_joins", "generations_published", "generations_invalidated", "requests_cancelled", "requests_failed", "output_bytes", "transport_bytes", "nonincremental_lowering_calls", "nonincremental_lowering_source_bytes", "discovery_entries_examined"} {
		usage[name] = 0
	}
	body, err := wireproto.Marshal(map[string]any{"schema": "captain.transcript/1", "response": map[string]any{"schema": "cc-transcript.snapshot/1", "id": id, "status": "cancelled", "complete": false, "data": nil, "cursor": nil, "reason": "fixture", "usage": usage}})
	if err != nil {
		t.Fatal(err)
	}
	return body
}

func TestSnapshotOwnerValidatesAndCorrelatesCoreResponse(t *testing.T) {
	for _, id := range []string{"one", "different"} {
		t.Run(id, func(t *testing.T) {
			frames := make(chan wireproto.Frame, 8)
			owner, server := fakeSnapshotOwner(t, frames)
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			done := make(chan snapshotResult, 1)
			go func() {
				body, err := owner.call(ctx, statsRequest, userSnapshotContext("one", "hook", 501), func() {})
				done <- snapshotResult{body: body, err: err}
			}()
			frame := receiveSnapshot(t, frames)
			expected := snapshotFailure(t, id)
			if err := wireproto.EncodeFrameLimit(server, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpSnapshotResult, ID: frame.ID, Snapshot: expected}, snapshots.MaxFrameBytes); err != nil {
				t.Fatal(err)
			}
			result := <-done
			if id == "one" {
				if result.err != nil || !bytes.Equal(result.body, expected) {
					t.Fatalf("result=%s err=%v", result.body, result.err)
				}
			} else if result.err == nil {
				t.Fatal("mismatched core response accepted")
			}
		})
	}
}

func TestWorkerLateSnapshotCancelDoesNotFailAnotherEvent(t *testing.T) {
	client, server := net.Pipe()
	defer server.Close()
	go func() {
		hello, _ := wireproto.DecodeFrame(server)
		_ = wireproto.EncodeFrame(server, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpHello, Build: hello.Build})
	}()
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	worker, err := handshakeWorker(ctx, client, Build)
	if err != nil {
		t.Fatal(err)
	}
	defer worker.fail(net.ErrClosed)
	worker.snapshots = &snapshotService{}
	firstCtx, stop := context.WithCancel(ctx)
	first := make(chan error, 1)
	second := make(chan error, 1)
	go func() { _, err := worker.call(firstCtx, testEventRequest("first")); first <- err }()
	a, err := wireproto.DecodeFrame(server)
	if err != nil {
		t.Fatal(err)
	}
	go func() { _, err := worker.call(ctx, testEventRequest("second")); second <- err }()
	b, err := wireproto.DecodeFrame(server)
	if err != nil {
		t.Fatal(err)
	}
	stop()
	if err := <-first; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if err := wireproto.EncodeFrame(server, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpSnapshotCancel, ID: 71, ParentID: a.ID}); err != nil {
		t.Fatal(err)
	}
	if err := wireproto.EncodeFrame(server, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpSnapshotRequest, ID: 72, ParentID: a.ID, Snapshot: statsRequest}); err != nil {
		t.Fatal(err)
	}
	rejected, err := wireproto.DecodeFrame(server)
	if err != nil || rejected.Op != wireproto.OpError || rejected.ID != 72 {
		t.Fatalf("late request=%+v err=%v", rejected, err)
	}
	if err := wireproto.EncodeFrame(server, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpResult, ID: b.ID, Response: &wireproto.EventResponse{Schema: 1, Status: "ok"}}); err != nil {
		t.Fatal(err)
	}
	if err := <-second; err != nil {
		t.Fatalf("unrelated hook failed: %v", err)
	}
}

func TestSnapshotStartupFailureCanRecover(t *testing.T) {
	service, err := newSnapshotService(&workerManager{lifetime: context.Background(), logWriter: io.Discard})
	if err != nil {
		t.Fatal(err)
	}
	var starts atomic.Int64
	recovered := &snapshotOwner{}
	service.start = func(context.Context) (*snapshotOwner, error) {
		if starts.Add(1) == 1 {
			return nil, errors.New("transient startup")
		}
		return recovered, nil
	}
	if _, err := service.get(context.Background()); err == nil {
		t.Fatal("startup failure hidden")
	}
	owner, err := service.get(context.Background())
	if err != nil || owner != recovered || starts.Load() != 2 {
		t.Fatalf("recovery owner=%p err=%v starts=%d", owner, err, starts.Load())
	}
}

func TestUnacknowledgedSnapshotCancellationRecoversOnlyEvidenceOwner(t *testing.T) {
	frames := make(chan wireproto.Frame, 8)
	owner, server := fakeSnapshotOwner(t, frames)
	owner.cancelGrace = 30 * time.Millisecond
	hookWorker := &workerClient{}
	manager := &workerManager{lifetime: context.Background(), logWriter: io.Discard, entries: map[string]*workerEntry{"hook": {worker: hookWorker}}}
	service, err := newSnapshotService(manager)
	if err != nil {
		t.Fatal(err)
	}
	service.owner = owner
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	reviewCtx, stop := context.WithCancel(ctx)
	reviewDone := make(chan error, 1)
	go func() {
		_, err := service.call(reviewCtx, statsRequest, userSnapshotContext("review", "review", 501))
		reviewDone <- err
	}()
	review := receiveSnapshot(t, frames)
	hookDone := make(chan error, 1)
	go func() {
		_, err := service.call(ctx, statsRequest, userSnapshotContext("hook", "hook", 501))
		hookDone <- err
	}()
	receiveSnapshot(t, frames)
	stop()
	frame := receiveSnapshot(t, frames)
	if frame.Op != wireproto.OpSnapshotCancel || frame.ID != review.ID {
		t.Fatalf("cancel=%+v", frame)
	}
	if err := <-reviewDone; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if err := <-hookDone; err == nil || err.Error() != "captain: snapshot owner failed to acknowledge cancellation" {
		t.Fatalf("unhealthy owner result=%v", err)
	}
	if len(service.reviewSlots) != 0 || len(service.hookSlots) != 0 {
		t.Fatal("unhealthy owner retained admission")
	}
	_ = server.Close()
	recoveredFrames := make(chan wireproto.Frame, 8)
	recovered, recoveredServer := fakeSnapshotOwner(t, recoveredFrames)
	var starts atomic.Int64
	service.start = func(context.Context) (*snapshotOwner, error) { starts.Add(1); return recovered, nil }
	next := make(chan error, 1)
	go func() {
		_, err := service.call(ctx, statsRequest, userSnapshotContext("next", "review", 501))
		next <- err
	}()
	request := receiveSnapshot(t, recoveredFrames)
	failSnapshot(t, recoveredServer, request.ID)
	if err := <-next; err == nil || err.Error() != "fixture" {
		t.Fatalf("recovered call=%v", err)
	}
	if starts.Load() != 1 || service.owner != recovered {
		t.Fatal("owner was not replaced once")
	}
	if manager.entries["hook"].worker != hookWorker || hookWorker.closed {
		t.Fatal("recovery changed hook worker")
	}
}

func TestBackgroundReverseSnapshotsUseReviewAdmission(t *testing.T) {
	frames := make(chan wireproto.Frame, 8)
	owner, ownerServer := fakeSnapshotOwner(t, frames)
	service, err := newSnapshotService(&workerManager{lifetime: context.Background(), logWriter: io.Discard})
	if err != nil {
		t.Fatal(err)
	}
	service.owner = owner
	workerConn, pythonConn := net.Pipe()
	defer pythonConn.Close()
	worker := &workerClient{conn: workerConn, snapshots: service, snapshotNamespace: 17, snapshotEvents: map[uint64]snapshotEvent{}, reverseSnapshots: map[uint64]reverseSnapshot{}}
	defer workerConn.Close()
	if err := worker.snapshotFrame(wireproto.Frame{Protocol: 1, Op: wireproto.OpSnapshotRequest, ID: 10, Snapshot: statsRequest}); err != nil {
		t.Fatal(err)
	}
	request := receiveSnapshot(t, frames)
	var callContext snapshots.CallContext
	if err := json.Unmarshal(request.SnapshotContext, &callContext); err != nil {
		t.Fatal(err)
	}
	if callContext.Admission != "review" || callContext.Claimant != "worker:17" {
		t.Fatalf("context=%+v", callContext)
	}
	failSnapshot(t, ownerServer, request.ID)
	reply, err := wireproto.DecodeFrame(pythonConn)
	if err != nil || reply.ID != 10 || reply.ParentID != 0 || reply.Op != wireproto.OpError {
		t.Fatalf("reply=%+v err=%v", reply, err)
	}
}

func TestBackgroundWarmUsesHookIdentityWithoutHookAdmission(t *testing.T) {
	frames := make(chan wireproto.Frame, 8)
	owner, ownerServer := fakeSnapshotOwner(t, frames)
	service, err := newSnapshotService(&workerManager{lifetime: context.Background(), logWriter: io.Discard})
	if err != nil {
		t.Fatal(err)
	}
	service.owner = owner
	for range cap(service.hookSlots) {
		service.hookSlots <- struct{}{}
	}
	for range cap(service.hookQueue) {
		service.hookQueue <- struct{}{}
	}
	for range cap(service.reviewSlots) {
		service.reviewSlots <- struct{}{}
	}
	workerConn, pythonConn := net.Pipe()
	defer pythonConn.Close()
	worker := &workerClient{conn: workerConn, snapshots: service, snapshotNamespace: 17, snapshotEvents: map[uint64]snapshotEvent{}, reverseSnapshots: map[uint64]reverseSnapshot{}}
	defer workerConn.Close()
	for index, body := range []json.RawMessage{warmRequest, warmRootRequest} {
		id := uint64(10 + index)
		if err := worker.snapshotFrame(wireproto.Frame{Protocol: 1, Op: wireproto.OpSnapshotRequest, ID: id, Snapshot: body}); err != nil {
			t.Fatal(err)
		}
		request := receiveSnapshot(t, frames)
		var callContext snapshots.CallContext
		if err := json.Unmarshal(request.SnapshotContext, &callContext); err != nil {
			t.Fatal(err)
		}
		if callContext.Admission != "hook" || callContext.WorkClass != "background" || callContext.Claimant != "worker:17" || callContext.Authority.Kind != "user" {
			t.Fatalf("warm context=%+v", callContext)
		}
		failSnapshot(t, ownerServer, request.ID)
		reply, err := wireproto.DecodeFrame(pythonConn)
		if err != nil || reply.ID != id || reply.ParentID != 0 || reply.Op != wireproto.OpError {
			t.Fatalf("warm reply=%+v err=%v", reply, err)
		}
	}
	if len(service.hookSlots) != cap(service.hookSlots) || len(service.hookQueue) != cap(service.hookQueue) {
		t.Fatal("warming consumed hook admission")
	}
	if len(service.reviewSlots) != cap(service.reviewSlots) || len(service.warmSlots) != 0 {
		t.Fatal("warming consumed review admission or retained a warm slot")
	}
}

func TestTranscriptClientWarmUsesHookAdmission(t *testing.T) {
	frames := make(chan wireproto.Frame, 8)
	owner, ownerServer := fakeSnapshotOwner(t, frames)
	service, err := newSnapshotService(&workerManager{lifetime: context.Background(), logWriter: io.Discard})
	if err != nil {
		t.Fatal(err)
	}
	service.owner = owner
	product := &hostProduct{snapshots: service}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	for _, body := range []json.RawMessage{warmRequest, warmRootRequest} {
		done := make(chan error, 1)
		go func() {
			_, err := product.transcript(ctx, daemonkit.Request{Op: opTranscript, Body: body, Caller: daemonkit.Caller{UID: 501}})
			done <- err
		}()
		frame := receiveSnapshot(t, frames)
		var callContext snapshots.CallContext
		if err := json.Unmarshal(frame.SnapshotContext, &callContext); err != nil {
			t.Fatal(err)
		}
		if callContext.Admission != "hook" || callContext.WorkClass != "background" || callContext.Authority.EffectiveUID != "501" {
			t.Fatalf("operator warmer context=%+v", callContext)
		}
		failSnapshot(t, ownerServer, frame.ID)
		if err := <-done; err == nil || err.Error() != "fixture" {
			t.Fatalf("operator warmer response=%v", err)
		}
	}
}

func TestCaptainDomainRequestUsesGeneratedIngressSchema(t *testing.T) {
	request := map[string]any{"schema": "captain.transcript/1", "tool_registry": []any{}, "request": map[string]any{
		"schema": "captain.transcript/1", "id": "one", "operation": "prepare_review", "deadline_unix_ms": time.Now().Add(time.Second).UnixMilli(),
		"limits": map[string]int{"max_read_bytes": 1024, "max_events": 10, "max_items": 10, "max_output_bytes": 1024, "max_discovery_entries": 10, "max_sources": 10},
		"view":   map[string]any{"handle": map[string]string{"owner_epoch": "e", "snapshot_id": "s", "generation": "g", "lease_id": "l"}, "classifier": map[string]string{"id": "captain", "version": "1"}, "selectors": []any{}, "attachments": []string{}},
		"policy": map[string]string{"id": "captain-review", "version": "1"}, "min_confidence": 0.5, "min_confidence_fix": 0.25,
	}}
	body, err := wireproto.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	frames := make(chan wireproto.Frame, 8)
	owner, server := fakeSnapshotOwner(t, frames)
	service, err := newSnapshotService(&workerManager{lifetime: context.Background(), logWriter: io.Discard})
	if err != nil {
		t.Fatal(err)
	}
	service.owner = owner
	done := make(chan error, 1)
	go func() {
		_, err := service.call(context.Background(), body, userSnapshotContext("domain", "review", 501))
		done <- err
	}()
	frame := receiveSnapshot(t, frames)
	if !bytes.Equal(frame.Snapshot, body) {
		t.Fatal("domain request changed")
	}
	failSnapshot(t, server, frame.ID)
	if err := <-done; err == nil || err.Error() != "fixture" {
		t.Fatalf("domain dispatch=%v", err)
	}
	invalid := bytes.Replace(body, []byte(`"min_confidence":0.5`), []byte(`"min_confidence":-1`), 1)
	if _, err := service.call(context.Background(), invalid, userSnapshotContext("invalid-domain", "review", 501)); err == nil {
		t.Fatal("negative confidence admitted")
	}
	select {
	case frame := <-frames:
		t.Fatalf("invalid domain request reached owner: %+v", frame)
	default:
	}
}

func TestSnapshotReleaseBypassesSaturatedReviewAdmission(t *testing.T) {
	frames := make(chan wireproto.Frame, 8)
	owner, server := fakeSnapshotOwner(t, frames)
	service, err := newSnapshotService(&workerManager{lifetime: context.Background(), logWriter: io.Discard})
	if err != nil {
		t.Fatal(err)
	}
	service.owner = owner
	service.start = func(context.Context) (*snapshotOwner, error) {
		t.Error("release started another owner")
		return nil, errors.New("unexpected startup")
	}
	service.reviewSlots <- struct{}{}
	for range cap(service.reviewQueue) {
		service.reviewQueue <- struct{}{}
	}
	request := json.RawMessage(`{"schema":"captain.transcript/1","tool_registry":[],"request":{"schema":"cc-transcript.snapshot/1","operation":"release","id":"release-one","owner_epoch":"epoch","token":"lease","kind":"lease"}}`)
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() {
		_, err := service.call(ctx, request, userSnapshotContext("worker:17", "review", 501))
		done <- err
	}()
	var frame wireproto.Frame
	select {
	case err := <-done:
		t.Fatalf("release refused by review admission: %v", err)
	case frame = <-frames:
	case <-ctx.Done():
		t.Fatal("release blocked behind review")
	}
	if frame.Op != wireproto.OpSnapshotRequest || !bytes.Equal(frame.Snapshot, request) {
		t.Fatalf("unexpected owner work: %+v", frame)
	}
	var context snapshots.CallContext
	if err := json.Unmarshal(frame.SnapshotContext, &context); err != nil {
		t.Fatal(err)
	}
	if context.Claimant != "worker:17" || context.Admission != "review" || context.Authority.Kind != "user" {
		t.Fatalf("cleanup changed authority: %+v", context)
	}
	var response map[string]any
	if err := json.Unmarshal(snapshotFailure(t, "release-one"), &response); err != nil {
		t.Fatal(err)
	}
	core := response["response"].(map[string]any)
	core["status"], core["complete"], core["reason"] = "ok", true, nil
	core["data"] = map[string]any{"kind": "released", "released": true}
	reply, err := wireproto.Marshal(response)
	if err != nil {
		t.Fatal(err)
	}
	if err := wireproto.EncodeFrameLimit(server, wireproto.Frame{Protocol: 1, Op: wireproto.OpSnapshotResult, ID: frame.ID, Snapshot: reply}, snapshots.MaxFrameBytes); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if len(service.reviewSlots) != 1 || len(service.reviewQueue) != cap(service.reviewQueue) {
		t.Fatal("release changed review admission")
	}
	if _, err := service.call(ctx, statsRequest, userSnapshotContext("worker:17", "review", 501)); err == nil {
		t.Fatal("non-release bypassed review admission")
	}
	for range cap(service.releaseQueue) {
		service.releaseQueue <- struct{}{}
	}
	if _, err := service.call(ctx, request, userSnapshotContext("worker:17", "review", 501)); err == nil {
		t.Fatal("release metadata queue is unbounded")
	}

	select {
	case extra := <-frames:
		t.Fatalf("release requested additional owner work: %+v", extra)
	default:
	}
}
