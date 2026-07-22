package hookd

import (
	"context"
	"errors"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/yasyf/daemonkit/wire"
)

func TestHostRuntimeExactStatusLifecycleAndOldLFRejection(t *testing.T) {
	role, err := DaemonRole()
	if err != nil {
		t.Skipf("exact executable identity unavailable: %v", err)
	}
	dir, err := os.MkdirTemp("/tmp", "capt-hookd-test-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	resolved := paths{
		dir: dir, socket: filepath.Join(dir, "capt-hookd.sock"),
		startLock: filepath.Join(dir, "start.lock"),
		processes: filepath.Join(dir, "workers.json"),
		log:       filepath.Join(dir, "capt-hookd.log"),
	}
	server := &Server{paths: resolved, role: role}
	wireServer, runtime, err := server.runtime()
	if err != nil {
		t.Fatal(err)
	}
	wireServer.RegisterLifecycle(runtime)
	runResult := make(chan error, 1)
	go func() { runResult <- runtime.Run(context.Background()) }()
	readyCtx, cancelReady := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancelReady()
	if err := runtime.WaitReady(readyCtx); err != nil {
		t.Fatalf("WaitReady: %v", err)
	}

	client := newClientWithPaths(resolved)
	defer client.Close()
	statusCtx, cancelStatus := context.WithTimeout(context.Background(), 5*time.Second)
	status, err := client.Status(statusCtx)
	cancelStatus()
	if err != nil {
		t.Fatalf("Status: %v", err)
	}
	if status.Schema != Schema || status.Build != Build || status.PID != os.Getpid() || len(status.Workers) != 0 {
		t.Fatalf("status = %#v", status)
	}

	conn, err := net.DialTimeout("unix", resolved.socket, time.Second)
	if err != nil {
		t.Fatalf("dial old client: %v", err)
	}
	if _, err := conn.Write([]byte(`{"v":1,"kind":"ping"}` + "\n")); err != nil {
		t.Fatalf("write old LF: %v", err)
	}
	_ = conn.SetReadDeadline(time.Now().Add(time.Second))
	var response [1]byte
	if _, err := conn.Read(response[:]); err == nil {
		t.Fatal("old LF client received a response")
	}
	_ = conn.Close()

	shutdownCtx, cancelShutdown := context.WithTimeout(context.Background(), 5*time.Second)
	if err := client.Shutdown(shutdownCtx); err != nil {
		cancelShutdown()
		t.Fatalf("Shutdown: %v", err)
	}
	cancelShutdown()
	select {
	case err := <-runResult:
		if err != nil && !errors.Is(err, wire.ErrDraining) {
			t.Fatalf("runtime Run: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("runtime did not stop")
	}
}
