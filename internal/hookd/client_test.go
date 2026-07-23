package hookd

import (
	"testing"

	dkdaemon "github.com/yasyf/daemonkit/daemon"
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
