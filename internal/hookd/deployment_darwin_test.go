package hookd

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/yasyf/daemonkit/launchd"
)

func TestExactAgentsPinSignedBundleFailureRestartsDrainBudgetInteractivePolicyAndUnrestrictedSession(t *testing.T) {
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
		host.ProcessType != launchd.ProcessTypeInteractive ||
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

func TestPackageInstallAbortsBeforeTheAppWhenTheToolEnvFails(t *testing.T) {
	home, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("HOME", home)
	t.Setenv("DAEMONKIT_HOME", home)
	t.Setenv("PATH", t.TempDir())
	err = installPackage(installProductToolEnv, applyPackagedApplication, packageToolEnvTimeout, packageLifecycleTimeout)
	if err == nil || !strings.Contains(err.Error(), "install the capt-hook "+Build+" tool env") {
		t.Fatalf("installPackage without uv = %v, want the tool env failure", err)
	}
	if _, err := os.Lstat(filepath.Join(home, "Applications")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("package-install touched ~/Applications after the tool env failed: %v", err)
	}
}

func TestIncumbentProbesRefuseAnInstalledMachineFromTheFilesystemFirst(t *testing.T) {
	for _, tc := range []struct {
		name  string
		leaf  []string
		probe int
	}{
		{name: "installed app", leaf: []string{"Applications", helperApplicationLeaf}, probe: 0},
		{name: "deploy records", leaf: []string{"Applications", deployMetadataDir, helperApplicationName}, probe: 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			home := t.TempDir()
			t.Setenv("HOME", home)
			t.Setenv("DAEMONKIT_HOME", home)
			evidence := filepath.Join(append([]string{home}, tc.leaf...)...)
			if err := os.MkdirAll(evidence, 0o755); err != nil {
				t.Fatal(err)
			}
			probes, err := incumbentProbes()
			if err != nil {
				t.Fatal(err)
			}
			if got, err := probes[tc.probe](context.Background()); got != evidence+" exists" || err != nil {
				t.Fatalf("probe %d = %q, %v; want %q", tc.probe, got, err, evidence+" exists")
			}
			err = refuseIncumbent(context.Background(), probes)
			if !errors.Is(err, errIncumbentHost) || !strings.Contains(err.Error(), evidence) {
				t.Fatalf("refuseIncumbent = %v, want a refusal naming %q", err, evidence)
			}
		})
	}
}
