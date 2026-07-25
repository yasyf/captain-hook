package hookd

import (
	"context"
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/yasyf/daemonkit/trust"
	"github.com/yasyf/daemonkit/wire"
)

func TestHostRuntimeExactStatusHealthAndOldLFRejection(t *testing.T) {
	policy, err := trust.NewTrustPolicy(trust.TrustPolicyConfig{
		ExpectedUID: os.Geteuid(), AllowUnprotected: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	dir, err := os.MkdirTemp("/tmp", "capt-hookd-test-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	resolved := paths{
		dir: dir, socket: filepath.Join(dir, "capt-hookd.sock"),
		processes:           filepath.Join(dir, "workers.json"),
		stopProcesses:       filepath.Join(dir, "stop-processes.db"),
		deploymentProcesses: filepath.Join(dir, "deployment-processes.db"),
		log:                 filepath.Join(dir, "capt-hookd.log"),
	}
	server := &Server{paths: resolved, trust: policy}
	_, runtime, err := server.runtime()
	if err != nil {
		t.Fatal(err)
	}
	runResult := make(chan error, 1)
	go func() { runResult <- runtime.run(context.Background()) }()
	readyCtx, cancelReady := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancelReady()
	readyResult := make(chan error, 1)
	go func() { readyResult <- runtime.daemon.WaitReady(readyCtx) }()
	select {
	case err := <-runResult:
		t.Fatalf("runtime exited before readiness: %v", err)
	case err := <-readyResult:
		if err != nil {
			t.Fatalf("WaitReady: %v", err)
		}
	}

	client := newClientWithPaths(resolved)
	client.role = trust.UnprotectedRole
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
		Dial: wire.UnixDialer(resolved.socket), WireBuild: Build, Role: trust.UnprotectedRole,
		MaxFrame: maxHostFrame,
	})
	cancelSkew()
	if err == nil {
		_ = skewed.Close()
		t.Fatal("runtime build was accepted as the stable wire build during handshake")
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
	if count, err := conn.Read(response[:]); err == nil && count != 0 && response[0] == '{' {
		t.Fatal("old LF client received a legacy response")
	}
	_ = conn.Close()

	shutdownCtx, cancelShutdown := context.WithTimeout(context.Background(), 5*time.Second)
	if err := runtime.daemon.Shutdown(shutdownCtx); err != nil {
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

func TestHostTrustPolicySeparatesEveryAuthorityRole(t *testing.T) {
	policy, err := hostTrustPolicy()
	if err != nil {
		t.Fatal(err)
	}
	for _, role := range []trust.PeerRole{businessRoleID, lifecycleRoleID, stopControlRoleID} {
		requirement, ok := policy.Requirement(role)
		if !ok || requirement.TeamID != hostTeamID || requirement.SigningIdentifier != hostSigningIdentifier {
			t.Fatalf("role %q requirement = %#v, %t", role, requirement, ok)
		}
	}
	if policy.AllowsStop(businessRoleID) || policy.AllowsReceipt(businessRoleID) ||
		policy.AllowsReadiness(businessRoleID) {
		t.Fatal("business role received lifecycle authority")
	}
	if !policy.AllowsStop(stopControlRoleID) || policy.AllowsReceipt(stopControlRoleID) ||
		policy.AllowsReadiness(stopControlRoleID) {
		t.Fatal("stop role authority is not exact")
	}
	if policy.AllowsStop(lifecycleRoleID) || !policy.AllowsReceipt(lifecycleRoleID) ||
		!policy.AllowsReadiness(lifecycleRoleID) || policy.AllowsHandoff(lifecycleRoleID) {
		t.Fatal("lifecycle role authority is not exact")
	}
	for _, role := range []trust.PeerRole{helperConsumerRoleID, helperBrokerLifecycleRoleID} {
		requirement, ok := policy.Requirement(role)
		if !ok || requirement.TeamID != hostTeamID || requirement.SigningIdentifier != helperSigningIdentifier {
			t.Fatalf("helper role %q requirement = %#v, %t", role, requirement, ok)
		}
		if policy.AllowsStop(role) || !policy.AllowsReceipt(role) || !policy.AllowsReadiness(role) ||
			policy.AllowsHandoff(role) {
			t.Fatalf("helper lifecycle role %q authority is not exact", role)
		}
	}
	handoffRequirement, ok := policy.Requirement(helperBrokerHandoffRoleID)
	if !ok || handoffRequirement.SigningIdentifier != helperSigningIdentifier ||
		policy.AllowsStop(helperBrokerHandoffRoleID) || policy.AllowsReceipt(helperBrokerHandoffRoleID) ||
		policy.AllowsReadiness(helperBrokerHandoffRoleID) || !policy.AllowsHandoff(helperBrokerHandoffRoleID) {
		t.Fatal("helper broker handoff authority is not exact")
	}
	clientRequirement, ok := policy.Requirement(helperClientRoleID)
	if !ok || clientRequirement.SigningIdentifier != helperClientSigningIdentifier ||
		policy.AllowsStop(helperClientRoleID) || policy.AllowsReceipt(helperClientRoleID) ||
		policy.AllowsReadiness(helperClientRoleID) || policy.AllowsHandoff(helperClientRoleID) {
		t.Fatal("helper client authority is not exact")
	}
}
