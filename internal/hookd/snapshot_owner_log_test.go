package hookd

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/yasyf/captain-hook/internal/snapshots"
	"github.com/yasyf/captain-hook/internal/wireproto"
	"github.com/yasyf/daemonkit"
)

func TestSnapshotConcurrentFailureKeepsFirstRecordUntilExit(t *testing.T) {
	client, server := net.Pipe()
	defer server.Close()
	var output bytes.Buffer
	log := snapshotOwnerLog{writer: &output, pid: 142, start: "1700000000.000001", known: true}
	var settled atomic.Int64
	pending := map[uint64]snapshotPending{}
	for id := uint64(1); id <= 2; id++ {
		pending[id] = snapshotPending{requestID: "private-request", done: make(chan struct{}), result: make(chan snapshotResult, 1), settled: func() { settled.Add(1) }}
	}
	owner := &snapshotOwner{conn: client, pending: pending}
	private := errors.New("private-request secret-frame\nsecret-context")
	var group sync.WaitGroup
	for range 16 {
		group.Add(1)
		go func() {
			defer group.Done()
			owner.fail(private, "cancellation_ack_timeout")
		}()
	}
	group.Wait()
	owner.mu.Lock()
	first := owner.failure
	owner.mu.Unlock()
	owner.fail(errors.New("later-private-error"), "response_read")
	if settled.Load() != 2 || first.pending != 2 || first.cause != "cancellation_ack_timeout" || first.at.IsZero() {
		t.Fatalf("unexpected first failure: %+v settled=%d", first, settled.Load())
	}
	for _, request := range pending {
		select {
		case <-request.done:
		default:
			t.Fatal("failure did not settle a pending request")
		}
		select {
		case result := <-request.result:
			if result.err != private {
				t.Fatal("diagnostics replaced the original request error")
			}
		case <-time.After(time.Second):
			t.Fatal("failure did not deliver the pending result")
		}
	}
	if output.Len() != 0 {
		t.Fatal("failure logged before the child exited")
	}
	owner.exited(daemonkit.Exit{Code: 7}, log)
	owner.mu.Lock()
	unchanged := owner.failure == first && owner.err == private
	owner.mu.Unlock()
	if !unchanged {
		t.Fatal("later failure or exit replaced the first record")
	}
	lines := strings.Split(strings.TrimSpace(output.String()), "\n")
	if len(lines) != 2 || len(lines[0]) > 768 || len(lines[1]) > 768 {
		t.Fatalf("unexpected failure/terminal pair: %q", output.String())
	}
	for _, field := range []string{"snapshot_owner_failure", "time=" + first.at.UTC().Format(time.RFC3339Nano), "pid=142", "process_start=1700000000.000001", "metadata_known=true", "identity_source=point_probe_only", "cause=cancellation_ack_timeout", "pending=2"} {
		if !strings.Contains(lines[0], field) {
			t.Fatalf("missing %s in %q", field, lines[0])
		}
	}
	if !strings.Contains(lines[1], "snapshot_owner_exit") || !strings.Contains(lines[1], "exit_code=7") {
		t.Fatalf("missing numeric terminal event: %q", lines[1])
	}
	for _, secret := range []string{"private-request", "secret-frame", "secret-context", "later-private-error"} {
		if strings.Contains(output.String(), secret) {
			t.Fatalf("diagnostic events exposed %s", secret)
		}
	}
}

func TestSnapshotBlockedFirstExitLogDoesNotRetainCallQueueOrStop(t *testing.T) {
	client, server := net.Pipe()
	_ = server.Close()
	owner := &snapshotOwner{conn: client, pending: make(map[uint64]snapshotPending)}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	service, err := newSnapshotService(&workerManager{lifetime: ctx, logWriter: io.Discard, now: time.Now, readiness: workerReadinessTimeout})
	if err != nil {
		t.Fatal(err)
	}
	service.owner = owner
	called := make(chan error, 1)
	go func() {
		_, err := service.call(ctx, statsRequest, userSnapshotContext("hook", "hook", 501))
		called <- err
	}()
	select {
	case err := <-called:
		if err == nil {
			t.Fatal("closed pipe did not fail the request write")
		}
	case <-ctx.Done():
		t.Fatal("request failure did not return before child-exit observation")
	}
	owner.mu.Lock()
	cause := owner.failure.cause
	owner.mu.Unlock()
	if cause != "request_write" {
		t.Fatalf("first failure cause %q", cause)
	}
	reader, writer := io.Pipe()
	defer reader.Close()
	defer writer.Close()
	finished := make(chan struct{})
	go func() {
		defer writer.Close()
		owner.exited(daemonkit.Exit{Code: -1, Signal: 9}, snapshotOwnerLog{writer: writer, pid: 143, start: "unknown"})
		close(finished)
	}()
	var prefix [1]byte
	started := make(chan error, 1)
	go func() {
		_, err := io.ReadFull(reader, prefix[:])
		started <- err
	}()
	select {
	case err := <-started:
		if err != nil {
			t.Fatal(err)
		}
	case <-ctx.Done():
		t.Fatal("first exit log did not start")
	}
	stopped := make(chan error, 1)
	go func() { stopped <- owner.stop(ctx) }()
	select {
	case err := <-stopped:
		if err != nil {
			t.Fatal(err)
		}
	case <-ctx.Done():
		t.Fatal("blocked first log retained owner stop")
	}
	if len(service.hookQueue) != 0 || len(service.hookSlots) != 0 {
		t.Fatal("blocked first log retained call admission")
	}
	if !owner.mu.TryLock() {
		t.Fatal("logging retained the owner lock")
	}
	owner.mu.Unlock()
	select {
	case <-finished:
		t.Fatal("exit logger finished without its first write being drained")
	default:
	}
	type logRead struct {
		data []byte
		err  error
	}
	read := make(chan logRead, 1)
	go func() {
		data, err := io.ReadAll(io.LimitReader(reader, 1536))
		read <- logRead{append(prefix[:], data...), err}
	}()
	var events []string
	select {
	case result := <-read:
		if result.err != nil {
			t.Fatal(result.err)
		}
		if len(result.data) > 1536 {
			t.Fatal("exit event pair exceeded its byte bound")
		}
		events = strings.Split(strings.TrimSpace(string(result.data)), "\n")
	case <-ctx.Done():
		t.Fatal("exit event pair was not emitted")
	}
	if len(events) != 2 {
		t.Fatalf("unexpected event count: %q", events)
	}
	for index, marker := range []string{"snapshot_owner_failure", "snapshot_owner_exit"} {
		line := events[index]
		if !strings.Contains(line, marker) || !strings.Contains(line, "process_start=unknown") || !strings.Contains(line, "metadata_known=false") {
			t.Fatalf("unexpected event: %q", line)
		}
	}
	select {
	case <-finished:
	case <-ctx.Done():
		t.Fatal("exit logger did not finish after its writer drained")
	}
}

func TestSnapshotImmediateEOFStoresFirstFailureBeforeObservation(t *testing.T) {
	client, server := net.Pipe()
	defer client.Close()
	completed := make(chan error, 1)
	go func() {
		defer server.Close()
		if _, err := wireproto.DecodeFrameLimit(server, snapshots.MaxFrameBytes); err != nil {
			completed <- err
			return
		}
		completed <- wireproto.EncodeFrameLimit(server, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpHello, Build: Build}, snapshots.MaxFrameBytes)
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
	if err := <-completed; err != nil {
		t.Fatal(err)
	}
	ticker := time.NewTicker(time.Millisecond)
	defer ticker.Stop()
	for {
		owner.mu.Lock()
		closed, failure := owner.closed, owner.failure
		owner.mu.Unlock()
		if closed {
			if failure.cause != "response_read" || failure.pending != 0 || failure.at.IsZero() {
				t.Fatalf("unexpected immediate EOF record: %+v", failure)
			}
			return
		}
		select {
		case <-ticker.C:
		case <-ctx.Done():
			t.Fatal("immediate EOF lost its first failure record")
		}
	}
}

func TestSnapshotChildExitCanBeTheFirstFailure(t *testing.T) {
	client, server := net.Pipe()
	defer server.Close()
	var output bytes.Buffer
	owner := &snapshotOwner{conn: client, pending: map[uint64]snapshotPending{}}
	owner.exited(daemonkit.Exit{Code: 0}, snapshotOwnerLog{writer: &output, pid: 144, start: "1700000001.000002", known: true})
	lines := strings.Split(strings.TrimSpace(output.String()), "\n")
	if len(lines) != 2 || !strings.Contains(lines[0], "cause=child_exit") || !strings.Contains(lines[0], "pending=0") || !strings.Contains(lines[1], "exit_code=0") || !strings.Contains(lines[1], "signal=0") {
		t.Fatalf("unexpected child-first failure/terminal pair: %q", output.String())
	}
}
