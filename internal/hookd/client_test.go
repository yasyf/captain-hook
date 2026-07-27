package hookd

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	dkdaemon "github.com/yasyf/daemonkit/daemon"
	"github.com/yasyf/daemonkit/service"
	"github.com/yasyf/daemonkit/wire"
)

const installRemedy = "run `capt-hook helper install`"

func fieldAckTimeout(socket string) error {
	rawRead := &net.OpError{
		Op:     "raw-read",
		Net:    "unix",
		Source: &net.UnixAddr{Net: "unix"},
		Addr:   &net.UnixAddr{Name: socket, Net: "unix"},
		Err:    os.ErrDeadlineExceeded,
	}
	return fmt.Errorf(
		"%w: read acknowledge: %w", wire.ErrHandshake, fmt.Errorf("wire: wait for frame: %w", rawRead),
	)
}

func TestFieldAckTimeoutChainShape(t *testing.T) {
	socket := "/Users/agent/Library/Application Support/captain-hook/host.sock"
	err := fieldAckTimeout(socket)
	want := "wire: handshake failed: read acknowledge: wire: wait for frame: raw-read unix ->" +
		socket + ": i/o timeout"
	if err.Error() != want {
		t.Fatalf("chain = %q, want %q", err.Error(), want)
	}
	if !errors.Is(err, wire.ErrHandshake) {
		t.Error("chain does not carry wire.ErrHandshake")
	}
	if !errors.Is(err, os.ErrDeadlineExceeded) {
		t.Error("chain does not carry os.ErrDeadlineExceeded")
	}
	var timeout net.Error
	if !errors.As(err, &timeout) || !timeout.Timeout() {
		t.Error("chain does not carry a timing-out net.Error")
	}
}

func TestProbeFailurePrescribesInstallOnlyForAnAbsentHost(t *testing.T) {
	socket := "/Users/agent/Library/Application Support/captain-hook/host.sock"
	tests := []struct {
		name        string
		cause       error
		wantInstall bool
	}{
		{"field acknowledge timeout", fieldAckTimeout(socket), false},
		{
			"dial deadline",
			fmt.Errorf("wire: dial: %w", &net.OpError{
				Op: "dial", Net: "unix", Addr: &net.UnixAddr{Name: socket, Net: "unix"},
				Err: os.ErrDeadlineExceeded,
			}),
			false,
		},
		{"request deadline on a live session", fmt.Errorf("wire: call: %w", context.DeadlineExceeded), false},
		{"kernel connect timeout", fmt.Errorf("wire: dial: %w", syscall.ETIMEDOUT), false},
		{
			"peer dropped mid-handshake",
			fmt.Errorf("%w: read acknowledge: %w", wire.ErrHandshake, io.EOF),
			false,
		},
		{
			// daemonkit's frame decoders substitute this sentinel for a mid-frame
			// EOF, so a truncated acknowledge never arrives as io.ErrUnexpectedEOF.
			"partial acknowledge frame",
			fmt.Errorf("%w: read acknowledge: %w", wire.ErrHandshake, wire.ErrFrameTruncated),
			false,
		},
		{
			"truncated acknowledge payload",
			fmt.Errorf("%w: acknowledge: %w", wire.ErrHandshake, io.ErrUnexpectedEOF),
			false,
		},
		{
			"peer reset mid-handshake",
			fmt.Errorf("%w: read acknowledge: %w", wire.ErrHandshake, syscall.ECONNRESET),
			false,
		},
		{
			"saturated session table",
			&wire.HandshakeRejectionError{
				Code: wire.ResponseCodeSessionCapacity, Reason: "wire: session capacity exhausted",
			},
			false,
		},
		{"unreachable host", ErrDaemonUnavailable, true},
		{
			"socket path missing",
			fmt.Errorf("wire: dial: %w", &net.OpError{
				Op: "dial", Net: "unix", Addr: &net.UnixAddr{Name: socket, Net: "unix"},
				Err: &os.SyscallError{Syscall: "connect", Err: syscall.ENOENT},
			}),
			true,
		},
		{
			"nobody listening",
			fmt.Errorf("wire: dial: %w", &net.OpError{
				Op: "dial", Net: "unix", Addr: &net.UnixAddr{Name: socket, Net: "unix"},
				Err: &os.SyscallError{Syscall: "connect", Err: syscall.ECONNREFUSED},
			}),
			true,
		},
		{"foreign host build", fmt.Errorf("%w: server=%q client=%q", wire.ErrBuildMismatch, "1.0.0", "2.0.0"), true},
		{"incomplete health identity", errors.New("captain: runtime health identity is incomplete"), true},
		{"session closed outside a handshake", fmt.Errorf("wire: read frame: %w", io.EOF), true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := probeFailure(tt.cause)
			if !errors.Is(got, tt.cause) {
				t.Fatalf("probeFailure(%v) dropped its cause: %v", tt.cause, got)
			}
			message := got.Error()
			if strings.Contains(message, installRemedy) != tt.wantInstall {
				t.Fatalf("install remedy present = %t, want %t: %s",
					strings.Contains(message, installRemedy), tt.wantInstall, message)
			}
			if tt.wantInstall {
				if !strings.Contains(message, "captain: signed host is not installed and ready") {
					t.Fatalf("absent host is not named: %s", message)
				}
				return
			}
			for _, want := range []string{"running but slow", "machine load", "retry on the next event"} {
				if !strings.Contains(message, want) {
					t.Fatalf("transient message is missing %q: %s", want, message)
				}
			}
		})
	}
}

func TestEnsureCurrentClassifiesItsOwnProbeFailure(t *testing.T) {
	tests := []struct {
		name        string
		listen      bool
		probe       time.Duration
		wantInstall bool
	}{
		{"host accepts and never acknowledges", true, 250 * time.Millisecond, false},
		{"no host listening", false, 10 * time.Second, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			root, err := os.MkdirTemp("/private/tmp", "captain-hook-probe-")
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = os.RemoveAll(root) })
			socket := filepath.Join(root, "capt-hookd.sock")
			if tt.listen {
				acceptWithoutAcknowledging(t, socket)
			}

			ctx, cancel := context.WithTimeout(context.Background(), tt.probe)
			defer cancel()
			client := newClientWithPaths(paths{dir: root, socket: socket})
			t.Cleanup(func() { _ = client.Close() })

			err = client.EnsureCurrent(ctx, time.Second)
			if err == nil {
				t.Fatal("EnsureCurrent accepted a host that never answered a readiness probe")
			}
			message := err.Error()
			if strings.Contains(message, installRemedy) != tt.wantInstall {
				t.Fatalf("install remedy present = %t, want %t: %s",
					strings.Contains(message, installRemedy), tt.wantInstall, message)
			}
			if tt.wantInstall {
				if !strings.Contains(message, "captain: signed host is not installed and ready") {
					t.Fatalf("absent host is not named: %s", message)
				}
				return
			}
			for _, want := range []string{"running but slow", "machine load", "retry on the next event"} {
				if !strings.Contains(message, want) {
					t.Fatalf("transient message is missing %q: %s", want, message)
				}
			}
		})
	}
}

func acceptWithoutAcknowledging(t *testing.T, socket string) {
	t.Helper()
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	var (
		mu       sync.Mutex
		held     []net.Conn
		accepted sync.WaitGroup
	)
	accepted.Add(1)
	go func() {
		defer accepted.Done()
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			mu.Lock()
			held = append(held, conn)
			mu.Unlock()
		}
	}()
	t.Cleanup(func() {
		_ = listener.Close()
		accepted.Wait()
		mu.Lock()
		defer mu.Unlock()
		for _, conn := range held {
			_ = conn.Close()
		}
	})
}

func TestRuntimeHealthCurrentRequiresExactReadyIdentity(t *testing.T) {
	exact := runtimeHealthResponse{
		Schema: Schema, RuntimeBuild: Build, RuntimeProtocol: Schema,
		ProcessGeneration: "generation", PID: 42, State: string(dkdaemon.StateHealthy), Ready: true,
	}
	if !exact.current() {
		t.Fatal("exact ready health is not current")
	}

	tests := map[string]runtimeHealthResponse{
		"stale build":    func() runtimeHealthResponse { h := exact; h.RuntimeBuild += "-stale"; return h }(),
		"wrong protocol": func() runtimeHealthResponse { h := exact; h.RuntimeProtocol++; return h }(),
		"degraded":       func() runtimeHealthResponse { h := exact; h.State = string(dkdaemon.StateDegraded); return h }(),
		"draining":       func() runtimeHealthResponse { h := exact; h.Draining = true; return h }(),
		"not ready":      func() runtimeHealthResponse { h := exact; h.Ready = false; return h }(),
	}
	for name, health := range tests {
		t.Run(name, func(t *testing.T) {
			if health.current() {
				t.Fatalf("non-current health accepted: %#v", health)
			}
		})
	}
}

func TestServicePlanPinsSignedUserBundleFailureOnlyRestartsAndUnrestrictedSession(t *testing.T) {
	root, err := os.MkdirTemp("/private/tmp", "captain-hook-plan-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(root) })
	app := filepath.Join(root, helperApplicationLeaf)
	for _, executable := range []string{appExecutablePath(app), hostExecutablePath(app)} {
		if err := os.MkdirAll(filepath.Dir(executable), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(executable, []byte("fixture"), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	plan, err := exactServicePlan(app)
	if err != nil {
		t.Fatal(err)
	}
	agents := plan.Agents()
	if len(agents) != 2 {
		t.Fatalf("agents = %#v", agents)
	}
	var host, helper service.Agent
	for _, agent := range agents {
		switch agent.Label {
		case hostServiceLabel:
			host = agent
		case helperServiceLabel:
			helper = agent
		}
	}
	if host.RestartPolicy != service.RestartOnFailure || host.Program != hostExecutablePath(app) ||
		len(host.Args) != 1 || host.Args[0] != "serve" ||
		len(host.AssociatedBundleIdentifiers) != 1 || host.AssociatedBundleIdentifiers[0] != helperBundleID {
		t.Fatalf("host agent = %#v", host)
	}
	if helper.RestartPolicy != service.RestartOnFailure || helper.Program != appExecutablePath(app) ||
		len(helper.Args) != 0 || len(helper.AssociatedBundleIdentifiers) != 1 ||
		helper.AssociatedBundleIdentifiers[0] != helperBundleID {
		t.Fatalf("helper agent = %#v", helper)
	}
	for _, agent := range agents {
		body, err := agent.Plist()
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(body), "LimitLoadToSessionType") {
			t.Fatalf(
				"agent %q pins a launchd session type; launchctl bootstrap refuses it with EIO:\n%s",
				agent.Label, body,
			)
		}
	}
}
