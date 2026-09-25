package hookd

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/yasyf/captain-hook/internal/snapshots"
)

func pythonSnapshotOwner(t *testing.T) *snapshotOwner {
	t.Helper()
	python := os.Getenv("CAPTAIN_TEST_SNAPSHOT_PYTHON")
	if python == "" {
		t.Skip("requires the exact candidate Python environment in CI")
	}
	pair, err := syscall.Socketpair(syscall.AF_UNIX, syscall.SOCK_STREAM, 0)
	if err != nil {
		t.Fatal(err)
	}
	parent := os.NewFile(uintptr(pair[0]), "snapshot-parent")
	child := os.NewFile(uintptr(pair[1]), "snapshot-child")
	conn, err := net.FileConn(parent)
	_ = parent.Close()
	if err != nil {
		_ = child.Close()
		t.Fatal(err)
	}
	var stderr bytes.Buffer
	command := exec.Command(python, "-P", "-m", "captain_hook.snapshots.worker")
	command.Stdin, command.Stdout, command.Stderr = child, child, &stderr
	if err := command.Start(); err != nil {
		_ = child.Close()
		_ = conn.Close()
		t.Fatal(err)
	}
	_ = child.Close()
	completed := make(chan error, 1)
	go func() { completed <- command.Wait() }()
	t.Cleanup(func() {
		_ = conn.Close()
		select {
		case err := <-completed:
			if err != nil {
				t.Errorf("isolated snapshot owner: %v\n%s", err, stderr.String())
			}
		case <-time.After(10 * time.Second):
			_ = command.Process.Kill()
			<-completed
			t.Errorf("isolated snapshot owner did not exit on pipe EOF\n%s", stderr.String())
		}
	})
	config, err := snapshots.DefaultConfig()
	if err != nil {
		t.Fatal(err)
	}
	var settings map[string]any
	if err := json.Unmarshal(config, &settings); err != nil {
		t.Fatal(err)
	}
	settings["max_read_bytes_per_step"] = 512
	settings["max_events_per_step"] = 1
	config, err = json.Marshal(settings)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	owner, err := handshakeSnapshotOwner(ctx, conn, config)
	if err != nil {
		t.Fatalf("real Python handshake: %v", err)
	}
	return owner
}

func TestSnapshotRealPythonOwnerSharesLoadsAcrossClients(t *testing.T) {
	owner := pythonSnapshotOwner(t)
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	service, err := newSnapshotService(&workerManager{lifetime: ctx, logWriter: io.Discard})
	if err != nil {
		t.Fatal(err)
	}
	var starts atomic.Int64
	service.start = func(context.Context) (*snapshotOwner, error) {
		starts.Add(1)
		return owner, nil
	}
	var ids atomic.Int64
	call := func(client string, admission string, operation string, arguments map[string]any) (map[string]any, error) {
		request := map[string]any{"schema": "cc-transcript.snapshot/1", "id": fmt.Sprintf("request-%d", ids.Add(1)), "operation": operation}
		for key, value := range arguments {
			request[key] = value
		}
		encoded, err := json.Marshal(map[string]any{"schema": "captain.transcript/1", "tool_registry": []any{}, "request": request})
		if err != nil {
			return nil, err
		}
		payload, err := service.call(ctx, encoded, userSnapshotContext(client, admission, uint32(os.Getuid())))
		if err != nil {
			return nil, err
		}
		var wrapper struct {
			Response map[string]any `json:"response"`
		}
		err = json.Unmarshal(payload, &wrapper)
		return wrapper.Response, err
	}
	source := filepath.Join(t.TempDir(), "fixture.jsonl")
	var input bytes.Buffer
	for index := range 40 {
		fmt.Fprintf(&input, `{"type":"user","uuid":"event-%d","sessionId":"fixture","timestamp":"2026-09-25T00:00:00Z","message":{"role":"user","content":"source event %d"}}`+"\n", index, index)
	}
	if err := os.WriteFile(source, input.Bytes(), 0o600); err != nil {
		t.Fatal(err)
	}
	acquire := map[string]any{
		"path": source, "classifier": map[string]string{"id": "native", "version": "1"}, "deadline_unix_ms": time.Now().Add(40 * time.Second).UnixMilli(),
		"limits": map[string]int{"max_read_bytes": 1 << 20, "max_events": 1000, "max_items": 1000, "max_output_bytes": 1 << 20, "max_discovery_entries": 100, "max_sources": 10},
	}
	abandoned, err := call("worker:cancelled", "hook", "acquire", acquire)
	if err != nil || abandoned["status"] != "incomplete" {
		t.Fatalf("staged acquisition: %v, %v", abandoned, err)
	}
	reservation := abandoned["data"].(map[string]any)["reservation"].(map[string]any)
	type started struct {
		client   string
		response map[string]any
		err      error
	}
	ready := make(chan started, 2)
	for index := range 2 {
		go func(index int) {
			client := fmt.Sprintf("worker:%d", index)
			response, err := call(client, "hook", "acquire", acquire)
			ready <- started{client, response, err}
		}(index)
	}
	clients := []started{<-ready, <-ready}
	for _, client := range clients {
		if client.err != nil {
			t.Fatal(client.err)
		}
	}
	released, err := call("worker:cancelled", "hook", "release", map[string]any{"owner_epoch": reservation["owner_epoch"], "kind": "reservation", "token": reservation["reservation_id"]})
	if err != nil || released["status"] != "ok" {
		t.Fatalf("cancel one reservation: %v, %v", released, err)
	}
	var waiting sync.WaitGroup
	failures := make(chan error, len(clients))
	for _, client := range clients {
		waiting.Add(1)
		go func(client started) {
			defer waiting.Done()
			response := client.response
			for response["status"] == "incomplete" {
				var err error
				response, err = call(client.client, "hook", "resume", map[string]any{"cursor": response["cursor"]})
				if err != nil {
					failures <- err
					return
				}
			}
			if response["status"] != "ok" {
				failures <- fmt.Errorf("remaining client lost shared load: %v", response)
				return
			}
			handle := response["data"].(map[string]any)["description"].(map[string]any)["handle"].(map[string]any)
			_, err := call(client.client, "hook", "release", map[string]any{"owner_epoch": handle["owner_epoch"], "kind": "lease", "token": handle["lease_id"]})
			if err != nil {
				failures <- err
			}
		}(client)
	}
	waiting.Wait()
	close(failures)
	for err := range failures {
		t.Error(err)
	}
	service.reviewSlots <- struct{}{}
	reviewContext, stopReview := context.WithCancel(ctx)
	reviewDone := make(chan error, 1)
	go func() {
		_, err := service.call(reviewContext, statsRequest, userSnapshotContext("review:queued", "review", uint32(os.Getuid())))
		reviewDone <- err
	}()
	until := time.Now().Add(time.Second)
	for len(service.reviewQueue) == 0 && time.Now().Before(until) {
		time.Sleep(time.Millisecond)
	}
	if len(service.reviewQueue) != 1 {
		t.Fatal("review was not queued")
	}
	stats, err := call("worker:reserved", "hook", "stats", nil)
	stopReview()
	<-service.reviewSlots
	if err != nil || stats["status"] != "ok" {
		t.Fatalf("reserved hook admission: %v, %v", stats, err)
	}
	if err := <-reviewDone; !errors.Is(err, context.Canceled) {
		t.Fatalf("queued review cancellation: %v", err)
	}
	counters := stats["data"].(map[string]any)["counters"].(map[string]any)
	if counters["cold_parses"] != float64(1) || counters["source_opens"] != float64(1) || starts.Load() != 1 {
		t.Fatalf("shared owner accounting: %v; owner starts=%d", counters, starts.Load())
	}
}
