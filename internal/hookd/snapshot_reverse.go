package hookd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"os"
	"strconv"
	"sync/atomic"
	"time"

	"github.com/yasyf/captain-hook/internal/snapshots"
	"github.com/yasyf/captain-hook/internal/wireproto"
)

type snapshotEvent struct {
	ctx    context.Context
	cancel context.CancelFunc
}
type reverseSnapshot struct {
	parent uint64
	cancel context.CancelFunc
}

var workerNamespace atomic.Uint64

func userSnapshotContext(claimant, admission string, uid uint32) snapshots.CallContext {
	return snapshots.CallContext{Claimant: claimant, Admission: admission,
		Authority: snapshots.Authority{Kind: "user", EffectiveUID: strconv.FormatUint(uint64(uid), 10)}, RegistryGeneration: Build}
}

func (w *workerClient) snapshotFrame(frame wireproto.Frame) error {
	if frame.ID == 0 || frame.Build != "" || frame.Request != nil || frame.Response != nil || frame.Adopt != nil || frame.Error != "" || len(frame.SnapshotContext)+len(frame.SnapshotConfig) != 0 {
		return errors.New("captain: invalid reverse snapshot frame")
	}
	if frame.Op == wireproto.OpSnapshotCancel {
		if len(frame.Snapshot) != 0 {
			return errors.New("captain: snapshot cancellation has payload")
		}
		w.mu.Lock()
		pending, ok := w.reverseSnapshots[frame.ID]
		w.mu.Unlock()
		if ok {
			if pending.parent != frame.ParentID {
				return errors.New("captain: snapshot cancellation parent mismatch")
			}
			pending.cancel()
		}
		return nil
	}
	w.mu.Lock()
	event, active := w.snapshotEvents[frame.ParentID]
	service := w.snapshots
	w.mu.Unlock()
	if service == nil {
		return errors.New("captain: snapshot service unavailable")
	}
	if frame.ParentID != 0 && !active {
		ctx, cancel := context.WithTimeout(context.Background(), workerSettlementTimeout)
		defer cancel()
		return w.write(ctx, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpError, ID: frame.ID, ParentID: frame.ParentID, Error: "captain: snapshot parent event is no longer active"})
	}
	if _, err := snapshots.Metadata(frame.Snapshot); err != nil {
		return err
	}
	encoded, err := wireproto.Marshal(frame)
	if err != nil {
		return err
	}
	if len(encoded) > snapshots.MaxFrameBytes {
		return wireproto.ErrPayloadTooLarge
	}
	parentContext, admission := context.Background(), "review"
	if frame.ParentID != 0 {
		parentContext, admission = event.ctx, "hook"
	}
	ctx, cancel := context.WithCancel(parentContext)
	w.mu.Lock()
	if w.closed {
		w.mu.Unlock()
		cancel()
		return net.ErrClosed
	}
	if _, duplicate := w.reverseSnapshots[frame.ID]; duplicate {
		w.mu.Unlock()
		cancel()
		return errors.New("captain: duplicate reverse snapshot id")
	}
	if len(w.reverseSnapshots) >= 64 {
		w.mu.Unlock()
		cancel()
		return errors.New("captain: reverse snapshot capacity exhausted")
	}
	w.reverseSnapshots[frame.ID] = reverseSnapshot{parent: frame.ParentID, cancel: cancel}
	w.mu.Unlock()
	go func() {
		defer cancel()
		callContext := userSnapshotContext(fmt.Sprintf("worker:%d", w.snapshotNamespace), admission, uint32(os.Geteuid()))
		body, err := service.call(ctx, frame.Snapshot, callContext)
		response := wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpSnapshotResult, ID: frame.ID, ParentID: frame.ParentID, Snapshot: body}
		if err != nil {
			response.Op = wireproto.OpError
			response.Error = err.Error()
			response.Snapshot = nil
		}
		writeCtx, stop := context.WithTimeout(context.Background(), workerSettlementTimeout)
		defer stop()
		if err := w.write(writeCtx, response); err != nil {
			w.fail(err)
		}
		w.mu.Lock()
		delete(w.reverseSnapshots, frame.ID)
		w.mu.Unlock()
	}()
	return nil
}

func (w *workerClient) setSnapshots(service *snapshotService) {
	w.mu.Lock()
	w.snapshots = service
	w.mu.Unlock()
}

func (w *workerClient) finishSnapshotEvent(id uint64) {
	w.mu.Lock()
	event, ok := w.snapshotEvents[id]
	delete(w.snapshotEvents, id)
	w.mu.Unlock()
	if ok {
		event.cancel()
	}
}

func snapshotRequestDeadline(body json.RawMessage) (time.Time, error) {
	metadata, err := snapshots.Metadata(body)
	if err != nil {
		return time.Time{}, err
	}
	deadline := time.Now().Add(120 * time.Second)
	if metadata.DeadlineUnixMS != 0 {
		deadline = minTime(deadline, time.UnixMilli(metadata.DeadlineUnixMS))
	}
	return deadline, nil
}
