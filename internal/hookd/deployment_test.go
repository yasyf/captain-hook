package hookd

import (
	"bytes"
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/yasyf/daemonkit/durable"
	"github.com/yasyf/daemonkit/launchd"
)

func TestExactAgentsPinSignedBundleFailureRestartsDrainBudgetAndUnrestrictedSession(t *testing.T) {
	t.Parallel()
	root, err := os.MkdirTemp("/private/tmp", "captain-hook-plan-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(root) })
	app := filepath.Join(root, helperApplicationLeaf)
	agents, err := exactAgents(app)
	if err != nil {
		t.Fatal(err)
	}
	if len(agents) != 2 {
		t.Fatalf("agents = %#v", agents)
	}
	var host, helper launchd.Agent
	for _, agent := range agents {
		switch agent.Label {
		case hostServiceLabel:
			host = agent
		case helperServiceLabel:
			helper = agent
		}
	}
	if host.RestartPolicy != launchd.RestartOnFailure || host.Program != hostExecutablePath(app) ||
		len(host.Args) != 1 || host.Args[0] != "serve" || host.ExitTimeOut != hostShutdownTimeout ||
		len(host.AssociatedBundleIdentifiers) != 1 || host.AssociatedBundleIdentifiers[0] != helperBundleID {
		t.Fatalf("host agent = %#v", host)
	}
	if helper.RestartPolicy != launchd.RestartOnFailure || helper.Program != appExecutablePath(app) ||
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

func TestInstallClientPublishesTheBundleHostAtTheStablePath(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	app := filepath.Join(t.TempDir(), helperApplicationLeaf)
	host := hostExecutablePath(app)
	if err := os.MkdirAll(filepath.Dir(host), 0o755); err != nil {
		t.Fatal(err)
	}
	for _, build := range []string{"first", "second"} {
		if err := os.WriteFile(host, []byte(build), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := installClient(testDeadline(t), app); err != nil {
			t.Fatalf("installClient: %v", err)
		}
		client := filepath.Join(home, ".daemonkit", "bin", "capt-hookd")
		body, err := os.ReadFile(client)
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(body, []byte(build)) {
			t.Fatalf("client = %q, want %q", body, build)
		}
		info, err := os.Stat(client)
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm() != 0o755 {
			t.Fatalf("client mode = %v, want 0755", info.Mode().Perm())
		}
		entries, err := os.ReadDir(filepath.Dir(client))
		if err != nil {
			t.Fatal(err)
		}
		for _, entry := range entries {
			if entry.Name() != "capt-hookd" && entry.Name() != ".capt-hookd.lock" {
				t.Fatalf("client directory holds a stray %q", entry.Name())
			}
		}
	}
}

func TestInstallClientLeavesAnIdenticalClientInPlace(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	app := filepath.Join(t.TempDir(), helperApplicationLeaf)
	host := hostExecutablePath(app)
	if err := os.MkdirAll(filepath.Dir(host), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(host, []byte("build"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := installClient(testDeadline(t), app); err != nil {
		t.Fatal(err)
	}
	client := filepath.Join(home, ".daemonkit", "bin", "capt-hookd")
	before, err := os.Stat(client)
	if err != nil {
		t.Fatal(err)
	}
	if err := installClient(testDeadline(t), app); err != nil {
		t.Fatal(err)
	}
	after, err := os.Stat(client)
	if err != nil {
		t.Fatal(err)
	}
	if !os.SameFile(before, after) {
		t.Fatal("an identical client was replaced")
	}
}

func testDeadline(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 30*time.Second)
	t.Cleanup(cancel)
	return ctx
}

func packagedHost(t *testing.T, body string) string {
	t.Helper()
	app := filepath.Join(t.TempDir(), helperApplicationLeaf)
	host := hostExecutablePath(app)
	if err := os.MkdirAll(filepath.Dir(host), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(host, []byte(body), 0o755); err != nil {
		t.Fatal(err)
	}
	return app
}

func TestInstallClientNeverReplacesANewerBuild(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	client := filepath.Join(home, ".daemonkit", "bin", "capt-hookd")
	if err := os.MkdirAll(filepath.Dir(client), 0o755); err != nil {
		t.Fatal(err)
	}
	newer := "#!/bin/sh\necho '{\"schema\":1,\"build\":\"99.0.0\"}'\n"
	if err := os.WriteFile(client, []byte(newer), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := installClient(testDeadline(t), packagedHost(t, "older")); err != nil {
		t.Fatal(err)
	}
	if body, err := os.ReadFile(client); err != nil || string(body) != newer {
		t.Fatalf("client = %q, %v; an older build replaced a newer one", body, err)
	}
}

func TestInstallClientWaitsForAConcurrentPublish(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	dir := filepath.Join(home, ".daemonkit", "bin")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	held, err := durable.AcquireLock(testDeadline(t), filepath.Join(dir, ".capt-hookd.lock"))
	if err != nil {
		t.Fatal(err)
	}
	app := packagedHost(t, "build")
	blocked, cancel := context.WithTimeout(t.Context(), 200*time.Millisecond)
	defer cancel()
	if err := installClient(blocked, app); !errors.Is(err, durable.ErrLockBusy) {
		t.Fatalf("installClient under a held lock = %v, want %v", err, durable.ErrLockBusy)
	}
	if _, err := os.Stat(filepath.Join(dir, "capt-hookd")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("client published while another publish held the lock: %v", err)
	}
	if err := held.Close(); err != nil {
		t.Fatal(err)
	}
	if err := installClient(testDeadline(t), app); err != nil {
		t.Fatalf("installClient after the lock freed = %v", err)
	}
}

func TestPackageInstallAbortsBeforeTheAppWhenTheToolEnvFails(t *testing.T) {
	home, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("HOME", home)
	t.Setenv("DAEMONKIT_HOME", home)
	t.Setenv("PATH", t.TempDir())
	err = applyPackagedApplication(t.Context())
	if err == nil || !strings.Contains(err.Error(), "install the capt-hook "+Build+" tool env") {
		t.Fatalf("applyPackagedApplication without uv = %v, want the tool env failure", err)
	}
	if _, err := os.Lstat(filepath.Join(home, "Applications")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("package-install touched ~/Applications after the tool env failed: %v", err)
	}
}
