package hookd

import (
	"context"
	"errors"
	"net"
	"testing"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
)

func TestBackgroundTicketMustSurroundACompletedForeground(t *testing.T) {
	worker := &workerClient{pending: map[uint64]chan workerResult{1: make(chan workerResult, 1)}, abandoned: map[uint64]struct{}{}}
	changes := 0
	worker.setOnBackground(func(delta int) { changes += delta })
	frame := wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpBackgroundBegin, ID: 1}
	if err := worker.backgroundFrame(frame); err != nil {
		t.Fatal(err)
	}
	if changes != 1 {
		t.Fatalf("tickets = %d", changes)
	}
	if err := worker.backgroundFrame(frame); err == nil {
		t.Fatal("duplicate ticket accepted")
	}
	frame.Op = wireproto.OpBackgroundEnd
	if err := worker.backgroundFrame(frame); err == nil {
		t.Fatal("end before foreground result accepted")
	}
	delete(worker.pending, 1)
	if err := worker.backgroundFrame(frame); err != nil {
		t.Fatal(err)
	}
	if changes != 0 {
		t.Fatalf("tickets = %d", changes)
	}
	if err := worker.backgroundFrame(frame); err == nil {
		t.Fatal("duplicate end accepted")
	}
	frame.Op = wireproto.OpBackgroundBegin
	if err := worker.backgroundFrame(frame); err == nil {
		t.Fatal("begin after foreground result accepted")
	}
}

func TestBackgroundTicketSurvivesAbandonedForeground(t *testing.T) {
	worker := &workerClient{abandoned: map[uint64]struct{}{7: {}}}
	if err := worker.backgroundFrame(wireproto.Frame{Op: wireproto.OpBackgroundBegin, ID: 7}); err != nil {
		t.Fatal(err)
	}
	if len(worker.background) != 1 {
		t.Fatal("abandoned foreground lost its background ticket")
	}
}

func backgroundEntry(manager *workerManager, worker *workerClient) *workerEntry {
	entry := &workerEntry{ready: make(chan struct{}), worker: worker, key: workerKey{id: "background", root: "/missing-root"}, background: 1}
	close(entry.ready)
	manager.entries[entry.key.member()] = entry
	return entry
}

func TestBackgroundPreventsSweepEvictionAndEphemeralRetirement(t *testing.T) {
	manager := mustWorkerManager(t)
	entry := backgroundEntry(manager, nil)
	entry.ephemeral = true
	entry.inflight = 1
	manager.release(entry)
	if len(manager.entries) != 1 {
		t.Fatal("ephemeral entry retired while background ran")
	}
	if workers := manager.sweep(time.Now()); len(workers) != 0 || len(manager.entries) != 1 {
		t.Fatal("background entry swept")
	}
	manager.mu.Lock()
	victim := manager.evictIdleLocked()
	manager.mu.Unlock()
	if victim != nil {
		t.Fatal("background entry evicted")
	}
	manager.backgroundChanged(entry, -1)
	manager.sweep(time.Now())
	if len(manager.entries) != 0 {
		t.Fatal("completed background kept entry busy")
	}
}

func TestRestartTimeoutLeavesWorkerAndReopensAdmission(t *testing.T) {
	manager := mustWorkerManager(t)
	worker, peer := silentWorker(t)
	defer peer.Close()
	entry := backgroundEntry(manager, worker)
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	ctx, deadlineCancel := context.WithTimeout(ctx, time.Second)
	defer deadlineCancel()
	changed := manager.changed
	done := make(chan error, 1)
	go func() { done <- manager.restart(ctx) }()
	<-changed
	if _, _, err := manager.acquire(t.Context(), workerKey{id: "new"}); !errors.Is(err, errWorkerAdmissionPaused) {
		t.Fatalf("admission = %v", err)
	}
	if err := manager.restart(ctx); !errors.Is(err, errWorkerAdmissionPaused) {
		t.Fatalf("competing restart = %v", err)
	}
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatalf("restart = %v", err)
	}
	if worker.broken() || manager.entries[entry.key.member()] != entry || manager.restarting {
		t.Fatal("timeout changed worker ownership/admission")
	}
	manager.backgroundChanged(entry, -1)
}

func TestRestartWaitsForNaturalBackgroundCompletion(t *testing.T) {
	manager := mustWorkerManager(t)
	worker, peer := silentWorker(t)
	defer peer.Close()
	entry := backgroundEntry(manager, worker)
	ctx, cancel := context.WithTimeout(t.Context(), time.Second)
	defer cancel()
	changed := manager.changed
	done := make(chan error, 1)
	go func() { done <- manager.restart(ctx) }()
	<-changed
	if worker.broken() {
		t.Fatal("restart stopped running background")
	}
	manager.backgroundChanged(entry, -1)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if !worker.broken() || manager.restarting || len(manager.entries) != 0 {
		t.Fatal("restart did not settle completed cohort")
	}
}

func TestBackgroundTicketProcessedBeforeForegroundResult(t *testing.T) {
	manager := mustWorkerManager(t)
	release := make(chan struct{})
	end := make(chan struct{})
	scriptedWorker(t, manager, func(conn net.Conn) {
		frame, err := wireproto.DecodeFrame(conn)
		if err != nil {
			return
		}
		_ = wireproto.EncodeFrame(conn, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpBackgroundBegin, ID: frame.ID})
		_ = wireproto.EncodeFrame(conn, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpResult, ID: frame.ID, Response: &wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok"}})
		<-release
		_ = wireproto.EncodeFrame(conn, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpBackgroundEnd, ID: frame.ID})
		close(end)
	})
	request := testEventRequest("Stop")
	request.Root = "/live"
	ctx, cancel := context.WithTimeout(t.Context(), time.Second)
	defer cancel()
	if _, err := manager.dispatch(ctx, request); err != nil {
		t.Fatal(err)
	}
	manager.mu.Lock()
	for _, entry := range manager.entries {
		if entry.background != 1 || entry.idle() {
			t.Fatal("foreground settled before background registration")
		}
	}
	manager.mu.Unlock()
	close(release)
	<-end
}

func TestRestartObservesLastBusyWorkerForgotten(t *testing.T) {
	manager := mustWorkerManager(t)
	worker, peer := silentWorker(t)
	defer peer.Close()
	backgroundEntry(manager, worker)
	ctx, cancel := context.WithTimeout(t.Context(), time.Second)
	defer cancel()
	changed := manager.changed
	done := make(chan error, 1)
	go func() { done <- manager.restart(ctx) }()
	<-changed
	manager.forget(worker)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

func TestRestartRetainsCohortOnSettlementError(t *testing.T) {
	manager := mustWorkerManager(t)
	worker, peer := silentWorker(t)
	defer peer.Close()
	entry := backgroundEntry(manager, worker)
	entry.background = 0
	worker.stopped = true
	worker.stopErr = errors.New("unsettled owned process")
	ctx, cancel := context.WithTimeout(t.Context(), time.Second)
	defer cancel()
	if err := manager.restart(ctx); !errors.Is(err, worker.stopErr) {
		t.Fatalf("restart = %v", err)
	}
	if manager.entries[entry.key.member()] != entry || manager.restarting {
		t.Fatal("failed settlement lost cohort/admission")
	}
	if retired := manager.sweep(time.Now()); len(retired) != 0 || manager.entries[entry.key.member()] != entry {
		t.Fatal("sweep retired an unsettled restart cohort")
	}
	if _, _, err := manager.acquire(ctx, workerKey{id: "replacement"}); !errors.Is(err, errWorkerAdmissionPaused) {
		t.Fatalf("unsettled cohort admitted new work: %v", err)
	}
	worker.stopErr = nil
	if err := manager.restart(ctx); err != nil {
		t.Fatal(err)
	}
	if len(manager.entries) != 0 {
		t.Fatal("successful settlement retained cohort")
	}
}
