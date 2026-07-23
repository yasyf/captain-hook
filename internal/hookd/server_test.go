package hookd

import (
	"context"
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/yasyf/daemonkit/wire"
)

func TestHostRuntimeExactStatusHealthAndOldLFRejection(t *testing.T) {
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
		startLock:     filepath.Join(dir, "start.lock"),
		processes:     filepath.Join(dir, "workers.json"),
		stopState:     filepath.Join(dir, "stop-controller.db"),
		stopProcesses: filepath.Join(dir, "stop-processes.db"),
		log:           filepath.Join(dir, "capt-hookd.log"),
	}
	server := &Server{paths: resolved, role: role}
	_, runtime, err := server.runtime()
	if err != nil {
		t.Fatal(err)
	}
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
	healthCtx, cancelHealth := context.WithTimeout(context.Background(), 5*time.Second)
	health, err := client.RuntimeHealth(healthCtx)
	cancelHealth()
	if err != nil {
		t.Fatalf("RuntimeHealth: %v", err)
	}
	if !health.current() || health.PID != os.Getpid() {
		t.Fatalf("runtime health = %#v", health)
	}
	restartCtx, cancelRestart := context.WithTimeout(context.Background(), 5*time.Second)
	if err := client.RestartWorkers(restartCtx); err != nil {
		cancelRestart()
		t.Fatalf("RestartWorkers: %v", err)
	}
	stalePayload, err := json.Marshal(restartWorkersRequest{Schema: Schema, Build: Build + "-stale"})
	if err != nil {
		cancelRestart()
		t.Fatal(err)
	}
	if _, err := client.call(restartCtx, wire.Op(opRestartWorkers), stalePayload); err == nil {
		cancelRestart()
		t.Fatal("stale build restarted workers")
	}
	cancelRestart()

	skewCtx, cancelSkew := context.WithTimeout(context.Background(), 5*time.Second)
	skewed, err := wire.NewClient(skewCtx, wire.ClientConfig{
		Dial: wire.UnixDialer(resolved.socket), WireBuild: Build, MaxFrame: maxHostFrame,
	})
	if err != nil {
		cancelSkew()
		t.Fatalf("open skewed client: %v", err)
	}
	skewResult, err := skewed.Call(skewCtx, wire.Op(opStatus), "", nil)
	cancelSkew()
	_ = skewed.Close()
	if err != nil {
		t.Fatalf("call from skewed client: %v", err)
	}
	if skewResult.Outcome == wire.Delivered {
		t.Fatal("runtime build was accepted as the stable wire build on dispatch")
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
	if err := runtime.Shutdown(shutdownCtx); err != nil {
		cancelShutdown()
		t.Fatalf("runtime Shutdown: %v", err)
	}
	cancelShutdown()
	select {
	case err := <-runResult:
		if err != nil {
			t.Fatalf("runtime Run: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("runtime did not stop")
	}
}
