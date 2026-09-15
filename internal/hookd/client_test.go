package hookd

import (
	"context"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"testing"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
	"github.com/yasyf/daemonkit"
)

const scriptedHostEnv = "CAPT_HOOK_SCRIPTED_HOST"

func scriptedHostDaemon() daemonkit.Daemon {
	return daemonkit.Daemon{
		Label:    "chk",
		Schemas:  []daemonkit.Schema{hostSchema},
		Trust:    daemonkit.Trust{Serving: daemonkit.ServingSameUser()},
		Shutdown: daemonkit.Grace(5 * time.Second),
		MaxFrame: maxHostFrame,
	}
}

func TestScriptedHostHelper(t *testing.T) {
	if os.Getenv(scriptedHostEnv) == "" {
		t.Skipf("%s is unset; TestEventWaitsForAHostThatStartsListeningWithinItsDeadline drives this test", scriptedHostEnv)
	}
	time.Sleep(500 * time.Millisecond)
	manager := newWorkerManager(daemonkit.Ctx{}, io.Discard)
	scriptedWorker(t, manager, func(conn net.Conn) {
		for {
			frame, err := wireproto.DecodeFrame(conn)
			if err != nil {
				return
			}
			_ = wireproto.EncodeFrame(conn, wireproto.Frame{
				Protocol: wireproto.Schema, Op: wireproto.OpResult, ID: frame.ID,
				Response: &wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: "served"},
			})
		}
	})
	if _, err := daemonkit.Serve(context.Background(), scriptedHostDaemon(), func(daemonkit.Ctx) (daemonkit.Product, error) {
		return &hostProduct{manager: manager, hub: newNotificationHub()}, nil
	}); err != nil {
		t.Fatal(err)
	}
}

func TestEventWaitsForAHostThatStartsListeningWithinItsDeadline(t *testing.T) {
	home, err := os.MkdirTemp("/tmp", fmt.Sprintf("chk-%d-", os.Getpid()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(home) })
	t.Setenv("DAEMONKIT_HOME", home)
	client, err := openClient(scriptedHostDaemon())
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()

	host := exec.Command(os.Args[0], "-test.run=^TestScriptedHostHelper$")
	host.Env = append(os.Environ(), scriptedHostEnv+"=1")
	host.Stderr = os.Stderr
	if err := host.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = host.Process.Kill()
		_ = host.Wait()
	})

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	request := testEventRequest("PreToolUse")
	request.ClientPID, request.ClientPPID = os.Getpid(), os.Getppid()
	response, err := client.Event(ctx, request)
	if err != nil {
		t.Fatalf("Event across an absent host = %v, want it served once the host listens", err)
	}
	if response.Stdout != "served" {
		t.Fatalf("Stdout = %q, want %q", response.Stdout, "served")
	}
}
