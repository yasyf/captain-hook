package hookd

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"

	"github.com/yasyf/daemonkit"
	"github.com/yasyf/daemonkit/launchd"
)

const (
	installRemedy = "run `capt-hook helper install`"
	absentMessage = "captain: signed host is not installed and ready"
)

// TestProbeFailureNamesTheOutcomeItMet pins the five distinct next steps: a
// transition and a deadline both resolve themselves, a trust failure and a
// missing verifier never do, and only an unclassified failure is a host that
// was never installed.
func TestProbeFailureNamesTheOutcomeItMet(t *testing.T) {
	t.Parallel()
	transition := []string{"between generations", "retry on the next event"}
	slow := []string{"running but slow", "machine load", "retry on the next event"}
	absent := []string{absentMessage, installRemedy}
	tests := []struct {
		name  string
		cause error
		want  []string
		deny  []string
	}{
		{
			"request deadline on a live session", fmt.Errorf("captain: call: %w", context.DeadlineExceeded),
			slow, []string{absentMessage, installRemedy},
		},
		{
			"transport deadline", fmt.Errorf("captain: call: %w", os.ErrDeadlineExceeded),
			slow, []string{absentMessage, installRemedy},
		},
		{
			"runtime still starting", fmt.Errorf("captain: call: %w", daemonkit.ErrNotReady),
			transition, []string{absentMessage, installRemedy, "machine load"},
		},
		{
			"incumbent leaving", fmt.Errorf("captain: call: %w", daemonkit.ErrDraining),
			transition, []string{absentMessage, installRemedy, "machine load"},
		},
		{
			"peer exited mid-attach", fmt.Errorf("captain: call: %w", daemonkit.ErrPeerGone),
			transition, []string{absentMessage, installRemedy, "machine load"},
		},
		{
			"untrusted server", fmt.Errorf("captain: attach: %w", daemonkit.ErrUntrusted),
			[]string{"is not the signed capt-hookd", "reinstall the helper"},
			[]string{absentMessage, "retry on the next event", "machine load"},
		},
		{
			"no codesign verifier", fmt.Errorf("captain: attach: %w", daemonkit.ErrNoVerifier),
			[]string{"no code-signing verifier"},
			[]string{absentMessage, installRemedy, "retry on the next event"},
		},
		{"unreachable host", ErrDaemonUnavailable, absent, []string{"retry on the next event"}},
		{"nobody listening", fmt.Errorf("captain: dial: %w", daemonkit.ErrAbsent), absent, []string{"machine load"}},
		{"connect refused", fmt.Errorf("captain: dial: %w", syscall.ECONNREFUSED), absent, []string{"machine load"}},
		{
			"incomplete health identity", errors.New("captain: runtime health identity is incomplete"),
			absent, []string{"machine load"},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := probeFailure(tt.cause)
			if !errors.Is(got, tt.cause) {
				t.Fatalf("probeFailure(%v) dropped its cause: %v", tt.cause, got)
			}
			message := got.Error()
			for _, want := range tt.want {
				if !strings.Contains(message, want) {
					t.Fatalf("message is missing %q: %s", want, message)
				}
			}
			for _, deny := range tt.deny {
				if strings.Contains(message, deny) {
					t.Fatalf("message wrongly claims %q: %s", deny, message)
				}
			}
		})
	}
}

func TestRuntimeHealthRequiresExactIdentity(t *testing.T) {
	t.Parallel()
	current := runtimeHealthResponse{
		Schema: Schema, RuntimeBuild: Build, RuntimeProtocol: Schema, PID: 42,
	}
	if err := current.exact(); err != nil {
		t.Fatalf("exact health refused: %v", err)
	}
	tests := map[string]runtimeHealthResponse{
		"stale build":    func() runtimeHealthResponse { h := current; h.RuntimeBuild += "-stale"; return h }(),
		"wrong protocol": func() runtimeHealthResponse { h := current; h.RuntimeProtocol++; return h }(),
	}
	for name, health := range tests {
		t.Run(name, func(t *testing.T) {
			if err := health.exact(); err == nil {
				t.Fatalf("non-current health accepted: %#v", health)
			}
		})
	}
}

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
