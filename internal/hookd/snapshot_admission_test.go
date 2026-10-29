package hookd

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"runtime"
	"sync/atomic"
	"testing"
	"time"

	"github.com/yasyf/captain-hook/internal/snapshots"
	"github.com/yasyf/captain-hook/internal/wireproto"
)

var reviewPreparations = []string{"prepare_review", "prepare_corrections"}

func domainSnapshotRequest(t *testing.T, operation, id string) json.RawMessage {
	t.Helper()
	request := map[string]any{
		"schema": "captain.transcript/1", "id": id, "operation": operation, "deadline_unix_ms": int64(9_000_000_000_000),
		"limits": map[string]int{"max_read_bytes": 1024, "max_source_read_bytes": 1024, "max_events": 10, "max_items": 10, "max_output_bytes": 1024, "max_discovery_entries": 10, "max_sources": 10},
		"view":   map[string]any{"handle": map[string]string{"owner_epoch": "e", "snapshot_id": "s", "generation": "g", "lease_id": "l"}, "classifier": map[string]string{"id": "captain", "version": "1"}, "selectors": []any{}, "attachments": []string{}},
	}
	policy := map[string]string{"id": "captain-review", "version": "1"}
	switch operation {
	case "prepare_review":
		request["policy"], request["min_confidence"], request["min_confidence_fix"] = policy, 0.5, 0.25
	case "prepare_corrections":
		request["policy"], request["anchors"], request["feedback"], request["repo"] = policy, []any{}, []string{}, nil
	case "prepare_hook_view":
		request["cwd"], request["droid"] = nil, false
	default:
		t.Fatalf("no domain request fixture for %s", operation)
	}
	body, err := wireproto.Marshal(map[string]any{"schema": "captain.transcript/1", "tool_registry": []any{}, "request": request})
	if err != nil {
		t.Fatal(err)
	}
	return body
}

type reviewGateHarness struct {
	service *snapshotService
	frames  chan wireproto.Frame
	server  net.Conn
	starts  *atomic.Int64
	review  wireproto.Frame
	done    chan error
}

func occupiedReviewGate(t *testing.T) *reviewGateHarness {
	t.Helper()
	frames := make(chan wireproto.Frame, 8)
	owner, server := fakeSnapshotOwner(t, frames)
	service, err := newSnapshotService(&workerManager{lifetime: context.Background(), logWriter: io.Discard, now: time.Now, readiness: workerReadinessTimeout})
	if err != nil {
		t.Fatal(err)
	}
	service.owner = owner
	harness := &reviewGateHarness{service: service, frames: frames, server: server, starts: &atomic.Int64{}, done: make(chan error, 1)}
	service.start = func(context.Context) (*snapshotOwner, error) {
		harness.starts.Add(1)
		return nil, errors.New("unexpected owner startup")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	t.Cleanup(cancel)
	go func() {
		_, err := service.call(ctx, statsRequest, userSnapshotContext("review:1", "review", 501))
		harness.done <- err
	}()
	harness.review = receiveSnapshot(t, frames)
	if len(service.reviewSlots) != cap(service.reviewSlots) || len(service.hookSlots) != 0 {
		t.Fatalf("in-flight review holds review slots=%d hook slots=%d", len(service.reviewSlots), len(service.hookSlots))
	}
	return harness
}

func (h *reviewGateHarness) finishReview(t *testing.T) {
	t.Helper()
	failSnapshot(t, h.server, h.review.ID)
	if err := <-h.done; err == nil || err.Error() != "fixture" {
		t.Fatalf("in-flight review=%v", err)
	}
}

func awaitSnapshotAdmissions(t *testing.T, service *snapshotService, entered int) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for len(service.reviewQueue)+len(service.hookQueue) != entered {
		if time.Now().After(deadline) {
			t.Fatalf("admission queues review=%d hook=%d, want %d entered", len(service.reviewQueue), len(service.hookQueue), entered)
		}
		runtime.Gosched()
	}
}

func requireNoOwnerWork(t *testing.T, frames <-chan wireproto.Frame) {
	t.Helper()
	select {
	case frame := <-frames:
		t.Fatalf("owner received op=%s id=%d snapshot=%s", frame.Op, frame.ID, frame.Snapshot)
	default:
	}
}

func ownerCallContext(t *testing.T, frame wireproto.Frame) snapshots.CallContext {
	t.Helper()
	var callContext snapshots.CallContext
	if err := json.Unmarshal(frame.SnapshotContext, &callContext); err != nil {
		t.Fatal(err)
	}
	return callContext
}

func TestSnapshotHookReviewPreparationsQueueOnTheReviewGate(t *testing.T) {
	gate := occupiedReviewGate(t)
	service := gate.service
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	done := make(chan error, len(reviewPreparations))
	for index, operation := range reviewPreparations {
		body := domainSnapshotRequest(t, operation, operation)
		go func() {
			_, err := service.call(ctx, body, userSnapshotContext("worker:17", "hook", 501))
			done <- err
		}()
		awaitSnapshotAdmissions(t, service, index+2)
	}
	if len(service.reviewQueue) != 3 || len(service.hookQueue) != 0 || len(service.hookSlots) != 0 {
		t.Fatalf("hook-context review preparations entered review queue=%d hook queue=%d hook slots=%d, want 3, 0, 0", len(service.reviewQueue), len(service.hookQueue), len(service.hookSlots))
	}
	requireNoOwnerWork(t, gate.frames)
	gate.finishReview(t)
	drained := map[string]bool{}
	for range reviewPreparations {
		frame := receiveSnapshot(t, gate.frames)
		metadata, err := snapshots.Metadata(frame.Snapshot)
		if err != nil {
			t.Fatal(err)
		}
		if callContext := ownerCallContext(t, frame); callContext.Admission != "hook" || callContext.Claimant != "worker:17" || callContext.Authority.Kind != "user" || callContext.Authority.EffectiveUID != "501" {
			t.Fatalf("%s reached the owner with context=%+v", metadata.Operation, callContext)
		}
		if len(service.reviewSlots) != 1 || len(service.hookSlots) != 0 {
			t.Fatalf("%s runs with review slots=%d hook slots=%d, want 1, 0", metadata.Operation, len(service.reviewSlots), len(service.hookSlots))
		}
		drained[metadata.Operation] = true
		failSnapshot(t, gate.server, frame.ID)
		if err := <-done; err == nil || err.Error() != "fixture" {
			t.Fatalf("%s=%v", metadata.Operation, err)
		}
	}
	if !drained["prepare_review"] || !drained["prepare_corrections"] {
		t.Fatalf("drained=%v", drained)
	}
	if len(service.reviewSlots) != 0 || len(service.reviewQueue) != 0 || gate.starts.Load() != 0 {
		t.Fatalf("review slots=%d queue=%d owner starts=%d", len(service.reviewSlots), len(service.reviewQueue), gate.starts.Load())
	}
	requireNoOwnerWork(t, gate.frames)
}

func TestSnapshotCancelledQueuedHookReviewPreparationNeverReachesTheOwner(t *testing.T) {
	for _, operation := range reviewPreparations {
		t.Run(operation, func(t *testing.T) {
			gate := occupiedReviewGate(t)
			service := gate.service
			body := domainSnapshotRequest(t, operation, operation)
			ctx, stop := context.WithCancel(context.Background())
			defer stop()
			queued := make(chan error, 1)
			go func() {
				_, err := service.call(ctx, body, userSnapshotContext("worker:17", "hook", 501))
				queued <- err
			}()
			awaitSnapshotAdmissions(t, service, 2)
			if len(service.reviewQueue) != 2 || len(service.hookQueue) != 0 {
				t.Fatalf("hook-context %s entered review queue=%d hook queue=%d, want 2, 0", operation, len(service.reviewQueue), len(service.hookQueue))
			}
			stop()
			if err := <-queued; !errors.Is(err, context.Canceled) {
				t.Fatalf("cancelled queued %s=%v", operation, err)
			}
			requireNoOwnerWork(t, gate.frames)
			owner := service.owner
			owner.mu.Lock()
			pending, closed := len(owner.pending), owner.closed
			owner.mu.Unlock()
			if pending != 1 || closed || len(service.reviewQueue) != 1 || len(service.reviewSlots) != 1 || gate.starts.Load() != 0 {
				t.Fatalf("after cancelling %s owner pending=%d closed=%t review queue=%d slots=%d owner starts=%d", operation, pending, closed, len(service.reviewQueue), len(service.reviewSlots), gate.starts.Load())
			}
			gate.finishReview(t)
			requireNoOwnerWork(t, gate.frames)
		})
	}
}

func TestSnapshotHookReviewPreparationsLeaveHookCapacityForHookWork(t *testing.T) {
	for _, ordinary := range []struct {
		name string
		body json.RawMessage
	}{
		{"stats", statsRequest},
		{"prepare_hook_view", domainSnapshotRequest(t, "prepare_hook_view", "hook-view")},
	} {
		t.Run(ordinary.name, func(t *testing.T) {
			gate := occupiedReviewGate(t)
			service := gate.service
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			preparationsCtx, stopPreparations := context.WithCancel(ctx)
			defer stopPreparations()
			preparations := make(chan error, cap(service.hookSlots))
			for index := range cap(service.hookSlots) {
				body := domainSnapshotRequest(t, reviewPreparations[index%len(reviewPreparations)], fmt.Sprintf("preparation-%d", index))
				go func() {
					_, err := service.call(preparationsCtx, body, userSnapshotContext("worker:17", "hook", 501))
					preparations <- err
				}()
			}
			awaitSnapshotAdmissions(t, service, 1+cap(service.hookSlots))
			if len(service.hookQueue) != 0 || len(service.hookSlots) != 0 {
				t.Fatalf("hook-context review preparations hold hook queue=%d and %d of %d hook slots", len(service.hookQueue), len(service.hookSlots), cap(service.hookSlots))
			}
			hookDone := make(chan error, 1)
			go func() {
				_, err := service.call(ctx, ordinary.body, userSnapshotContext("worker:17", "hook", 501))
				hookDone <- err
			}()
			hook := receiveSnapshot(t, gate.frames)
			if !bytes.Equal(hook.Snapshot, ordinary.body) {
				t.Fatalf("owner received %s, want the hook %s", hook.Snapshot, ordinary.name)
			}
			if callContext := ownerCallContext(t, hook); callContext.Admission != "hook" {
				t.Fatalf("hook %s context=%+v", ordinary.name, callContext)
			}
			if len(service.hookSlots) != 1 || len(service.hookQueue) != 1 || len(service.reviewQueue) != 1+cap(service.hookSlots) {
				t.Fatalf("hook %s runs with hook slots=%d queue=%d review queue=%d, want 1, 1, %d", ordinary.name, len(service.hookSlots), len(service.hookQueue), len(service.reviewQueue), 1+cap(service.hookSlots))
			}
			failSnapshot(t, gate.server, hook.ID)
			if err := <-hookDone; err == nil || err.Error() != "fixture" {
				t.Fatalf("hook %s=%v", ordinary.name, err)
			}
			stopPreparations()
			for range cap(service.hookSlots) {
				if err := <-preparations; !errors.Is(err, context.Canceled) {
					t.Fatalf("queued preparation=%v", err)
				}
			}
			requireNoOwnerWork(t, gate.frames)
			gate.finishReview(t)
			if len(service.hookSlots) != 0 || len(service.hookQueue) != 0 || len(service.reviewSlots) != 0 || len(service.reviewQueue) != 0 || gate.starts.Load() != 0 {
				t.Fatalf("hook slots=%d queue=%d review slots=%d queue=%d owner starts=%d", len(service.hookSlots), len(service.hookQueue), len(service.reviewSlots), len(service.reviewQueue), gate.starts.Load())
			}
			requireNoOwnerWork(t, gate.frames)
		})
	}
}
