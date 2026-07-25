package hookd

import (
	"os"
	"path/filepath"
	"testing"

	dkdaemon "github.com/yasyf/daemonkit/daemon"
	"github.com/yasyf/daemonkit/service"
)

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

func TestServicePlanPinsSignedUserBundleAndFailureOnlyRestarts(t *testing.T) {
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
}
