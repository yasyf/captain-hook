package hookd

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

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
		if err := installClient(app); err != nil {
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
		if len(entries) != 1 {
			t.Fatalf("client directory holds %d entries, want only the client", len(entries))
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
	if err := installClient(app); err != nil {
		t.Fatal(err)
	}
	client := filepath.Join(home, ".daemonkit", "bin", "capt-hookd")
	before, err := os.Stat(client)
	if err != nil {
		t.Fatal(err)
	}
	if err := installClient(app); err != nil {
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
