package hookd

import (
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

func TestHostAgentPinsSignedBundleAndFailureOnlyRestart(t *testing.T) {
	client := newClientWithPaths(paths{log: "/tmp/captain-hook.log"})
	agent := client.hostAgent("/Applications/Captain Hook.app/Contents/Helpers/capt-hookd")
	if agent.Label != hostServiceLabel || agent.RestartPolicy != service.RestartOnFailure ||
		agent.Program != "/Applications/Captain Hook.app/Contents/Helpers/capt-hookd" ||
		len(agent.Args) != 1 || agent.Args[0] != "serve" ||
		len(agent.AssociatedBundleIdentifiers) != 1 ||
		agent.AssociatedBundleIdentifiers[0] != "com.yasyf.capt-hook.helper" {
		t.Fatalf("host agent = %#v", agent)
	}
}
