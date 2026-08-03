package hookd

import (
	"context"
	"encoding/json"
	"io"
	"os"
	"strings"
	"testing"

	"github.com/yasyf/daemonkit"
)

// TestHostTrustFoldsSevenPeerClassesOntoThreeLanes pins the collapse: the
// control lane admits the signed host alone, and the business lane is the
// disjunction over every identity that speaks the product protocol. Nothing
// below the lane distinguishes peers any more.
func TestHostTrustFoldsSevenPeerClassesOntoThreeLanes(t *testing.T) {
	t.Parallel()
	trust := hostTrust()
	if trust.Control == nil || trust.Control.Digest() != hostRequirement().Digest() {
		t.Fatalf("control lane requirement = %#v", trust.Control)
	}
	want := daemonkit.Requirements{hostRequirement(), helperRequirement(), helperClientRequirement()}
	if len(trust.Business) != len(want) {
		t.Fatalf("business lane = %#v", trust.Business)
	}
	for i, requirement := range want {
		if trust.Business[i].Digest() != requirement.Digest() {
			t.Fatalf("business lane[%d] = %#v, want %#v", i, trust.Business[i], requirement)
		}
	}
	if trust.Business.Digest() == (daemonkit.Requirements{*trust.Control}).Digest() {
		t.Fatal("business disjunction digests as the single control requirement")
	}
}

func TestHostDaemonValidatesOnBothHalves(t *testing.T) {
	t.Parallel()
	daemon := hostDaemon()
	if err := daemon.ValidateForServe(); err != nil {
		t.Fatalf("ValidateForServe: %v", err)
	}
	if err := daemon.ValidateForClient(); err != nil {
		t.Fatalf("ValidateForClient: %v", err)
	}
	if daemon.Schemas[0] != hostSchema || daemon.MaxFrame != maxHostFrame {
		t.Fatalf("daemon = %#v", daemon)
	}
}

func TestHostProductHandleDispatchesEveryOpAndRefusesTheRest(t *testing.T) {
	t.Parallel()
	product := &hostProduct{
		manager: newWorkerManager(daemonkit.Ctx{}, io.Discard),
		hub:     newNotificationHub(),
	}
	ctx := context.Background()

	health, err := product.Handle(ctx, daemonkit.Request{Op: opRuntimeHealth})
	if err != nil {
		t.Fatalf("runtime health: %v", err)
	}
	var identity runtimeHealthResponse
	if err := decodeStrict(health.Body, &identity); err != nil {
		t.Fatal(err)
	}
	if identity.Schema != Schema || identity.RuntimeBuild != Build ||
		identity.RuntimeProtocol != Schema || identity.PID != os.Getpid() {
		t.Fatalf("runtime health = %#v", identity)
	}

	status, err := product.Handle(ctx, daemonkit.Request{Op: opStatus})
	if err != nil {
		t.Fatalf("status: %v", err)
	}
	var reported statusResponse
	if err := decodeStrict(status.Body, &reported); err != nil {
		t.Fatal(err)
	}
	if reported.Schema != Schema || reported.Build != Build || reported.PID != os.Getpid() ||
		len(reported.Workers) != 0 {
		t.Fatalf("status = %#v", reported)
	}

	stale, err := json.Marshal(restartWorkersRequest{Schema: Schema, Build: Build + "-stale"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := product.Handle(ctx, daemonkit.Request{Op: opRestartWorkers, Body: stale}); err == nil {
		t.Fatal("stale build restarted workers")
	}

	request := testEventRequest("PreToolUse")
	request.Build = Build
	event, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	_, err = product.Handle(ctx, daemonkit.Request{
		Op: opEvent, Body: event, Caller: daemonkit.Caller{PID: request.ClientPID + 1},
	})
	if err == nil || !strings.Contains(err.Error(), "does not match authenticated peer") {
		t.Fatalf("event accepted a forged client pid: %v", err)
	}

	if _, err := product.Handle(ctx, daemonkit.Request{Op: opStatus, Body: []byte("{}")}); err == nil {
		t.Fatal("status accepted a body")
	}
	if _, err := product.Handle(ctx, daemonkit.Request{Op: "captain.unknown.v1"}); err == nil {
		t.Fatal("unknown op dispatched")
	}
}
