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

func TestSnapshotFirstFailureReportsOnceAfterCleanup(t *testing.T) {
	client, server := net.Pipe()
	defer server.Close()
	var output lockedBuffer
	log := snapshotOwnerLog{writer: &output, pid: 142, start: "1700000000.000001", known: true}
	var settled atomic.Int64
	pending := map[uint64]snapshotPending{}
	for id := uint64(1); id <= 2; id++ {
		pending[id] = snapshotPending{requestID: "private-request", done: make(chan struct{}), result: make(chan snapshotResult, 1), settled: func() { settled.Add(1) }}
	}
	owner := &snapshotOwner{conn: client, pending: pending}
	var reports atomic.Int64
	var observed atomic.Bool
	owner.failure = func(at time.Time, cause string, count int) {
		unlocked := owner.mu.TryLock()
		if unlocked {
			owner.mu.Unlock()
		}
		reports.Add(1)
		observed.Store(unlocked && settled.Load() == 2)
		log.failure(at, cause, count)
	}
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
	owner.fail(errors.New("later-private-error"), "response_read")
	if reports.Load() != 1 || !observed.Load() {
		t.Fatal("failure logging preceded cleanup or retained the owner lock")
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
	line := output.String()
	if strings.Count(line, "snapshot_owner_failure") != 1 || len(line) > 768 {
		t.Fatalf("unexpected failure event count or size: %q", line)
	}
	for _, field := range []string{"pid=142", "process_start=1700000000.000001", "metadata_known=true", "identity_source=point_probe_only", "cause=cancellation_ack_timeout", "pending=2"} {
		if !strings.Contains(line, field) {
			t.Fatalf("missing %s in %q", field, line)
		}
	}
	for _, secret := range []string{"private-request", "secret-frame", "secret-context", "later-private-error"} {
		if strings.Contains(line, secret) {
			t.Fatalf("failure event exposed %s", secret)
		}
	}
	timestamp := false
	for _, field := range strings.Fields(line) {
		if strings.HasPrefix(field, "time=") {
			timestamp = true
			if _, err := time.Parse(time.RFC3339Nano, strings.TrimPrefix(field, "time=")); err != nil {
				t.Fatal(err)
			}
		}
	}
	if !timestamp {
		t.Fatal("failure event has no timestamp")
	}
}

func TestSnapshotBlockedTerminalLogDoesNotDelayFailure(t *testing.T) {
	client, server := net.Pipe()
	defer server.Close()
	reader, writer := io.Pipe()
	defer reader.Close()
	defer writer.Close()
	request := snapshotPending{done: make(chan struct{}), result: make(chan snapshotResult, 1), settled: func() {}}
	owner := &snapshotOwner{conn: client, pending: map[uint64]snapshotPending{1: request}}
	owner.failure = snapshotOwnerLog{writer: io.Discard, pid: 143, start: "unknown"}.failure
	finished := make(chan struct{})
	go func() {
		owner.exited(daemonkit.Exit{Code: -1, Signal: 9}, snapshotOwnerLog{writer: writer, pid: 143, start: "unknown"})
		close(finished)
	}()
	select {
	case result := <-request.result:
		if result.err == nil {
			t.Fatal("child exit did not fail the pending request")
		}
	case <-time.After(time.Second):
		t.Fatal("blocked terminal logging delayed existing failure cleanup")
	}
	if !owner.mu.TryLock() {
		t.Fatal("terminal logging retained the owner lock")
	}
	owner.mu.Unlock()
	buffer := make([]byte, 768)
	n, err := reader.Read(buffer)
	if err != nil {
		t.Fatal(err)
	}
	line := string(buffer[:n])
	for _, field := range []string{"snapshot_owner_exit", "pid=143", "process_start=unknown", "metadata_known=false", "exit_code=-1", "signal=9"} {
		if !strings.Contains(line, field) {
			t.Fatalf("missing %s in %q", field, line)
		}
	}
	select {
	case <-finished:
	case <-time.After(time.Second):
		t.Fatal("terminal logger did not finish after its writer drained")
	}
}

func TestSnapshotImmediateEOFUsesConfiguredFailureObserver(t *testing.T) {
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
	failure := make(chan string, 1)
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if _, err := handshakeSnapshotOwner(ctx, client, config, func(_ time.Time, cause string, pending int) {
		if pending == 0 {
			failure <- cause
		}
	}); err != nil {
		t.Fatal(err)
	}
	if err := <-completed; err != nil {
		t.Fatal(err)
	}
	select {
	case cause := <-failure:
		if cause != "response_read" {
			t.Fatalf("immediate EOF cause %q", cause)
		}
	case <-time.After(time.Second):
		t.Fatal("immediate EOF lost its configured failure observer")
	}
}

func TestSnapshotTerminalEventPreservesFirstFailureCause(t *testing.T) {
	client, server := net.Pipe()
	defer server.Close()
	var output bytes.Buffer
	log := snapshotOwnerLog{writer: &output, pid: 144, start: "1700000001.000002", known: true}
	owner := &snapshotOwner{conn: client, pending: map[uint64]snapshotPending{}, failure: log.failure}
	first := errors.New("private-first-error")
	owner.fail(first, "response_read")
	owner.exited(daemonkit.Exit{Code: 7}, log)
	lines := strings.Split(strings.TrimSpace(output.String()), "\n")
	if len(lines) != 2 || !strings.Contains(lines[0], "cause=response_read") || !strings.Contains(lines[1], "snapshot_owner_exit") || !strings.Contains(lines[1], "exit_code=7") {
		t.Fatalf("unexpected first failure/terminal pair: %q", output.String())
	}
	if owner.err != first || strings.Contains(output.String(), "private-first-error") {
		t.Fatal("terminal reporting replaced or exposed the first error")
	}
}
