package hookd

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
	"github.com/yasyf/daemonkit"
)

const (
	destructivePayload = `{"cwd":"/r","tool_name":"Bash","tool_input":{"command":"pkill -x sleep"}}`
	benignPayload      = `{"cwd":"/r","tool_name":"Bash","tool_input":{"command":"git status"}}`
)

type scriptedClient struct {
	response wireproto.EventResponse
	err      error
	requests []wireproto.EventRequest
}

func (c *scriptedClient) Event(_ context.Context, request wireproto.EventRequest) (wireproto.EventResponse, error) {
	c.requests = append(c.requests, request)
	return c.response, c.err
}

func (c *scriptedClient) Close() error { return nil }

func scriptClient(t *testing.T, client *scriptedClient, open error) {
	t.Helper()
	previous := openEventClient
	openEventClient = func() (eventClient, error) {
		if open != nil {
			return nil, open
		}
		return client, nil
	}
	t.Cleanup(func() { openEventClient = previous })
}

func runEvent(t *testing.T, event, payload string) (int, string, string) {
	t.Helper()
	t.Setenv("CLAUDE_PROJECT_DIR", "/project")
	t.Setenv("CAPTAIN_HOOK_STATE_DIR", t.TempDir())
	var stdout, stderr bytes.Buffer
	code := Main([]string{"run", event}, strings.NewReader(payload), &stdout, &stderr)
	return code, stdout.String(), stderr.String()
}

func TestRunDeniesAMandatoryEventTheGuardDidNotComplete(t *testing.T) {
	refused := errors.Join(fmt.Errorf("captain: %w", daemonkit.ErrAbsent), context.DeadlineExceeded)
	for _, tc := range []struct {
		name, event, kind string
		open              error
		client            scriptedClient
	}{
		{"host unavailable", "PreToolUse", "host-unavailable",
			errors.New("captain: open signed host: /Users/x/Captain Hook.app is not installed"), scriptedClient{}},
		{"refused until the deadline", "PreToolUse", "transport-refused", nil, scriptedClient{err: refused}},
		{"other transport error", "PreToolUse", "transport-error", nil,
			scriptedClient{err: errors.New("captain: decode event response: boom")}},
		{"worker error", "PreToolUse", "worker-error", nil, scriptedClient{response: wireproto.EventResponse{
			Schema: wireproto.Schema, Status: "error", Stderr: "Traceback /Users/x/secret.py\n", Exit: 1,
		}}},
		{"shed", "PreToolUse", "shed", nil, scriptedClient{response: shedResponse(3, 4*time.Second, time.Second)}},
		{"no verdict", "PreToolUse", "no-verdict", nil,
			scriptedClient{response: wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok"}}},
		{"permission request", "PermissionRequest", "no-verdict", nil,
			scriptedClient{response: wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok"}}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			client := tc.client
			scriptClient(t, &client, tc.open)
			code, stdout, stderr := runEvent(t, tc.event, destructivePayload)
			if want := wireproto.DenyEnvelope(tc.event, tc.kind) + "\n"; code != 0 || stdout != want {
				t.Fatalf("exit=%d stdout=%q, want exit 0 with %q", code, stdout, want)
			}
			if !strings.Contains(stderr, "("+tc.kind+")") || strings.Contains(stderr, "/Users") ||
				strings.Contains(stderr, "boom") || strings.Contains(stderr, "Traceback") {
				t.Fatalf("stderr = %q, want the kind alone", stderr)
			}
			if tc.open == nil && (len(client.requests) != 1 || !client.requests[0].Mandatory) {
				t.Fatalf("requests = %+v, want one mandatory request", client.requests)
			}
		})
	}
}

type flakyClient struct {
	timeouts int
	response wireproto.EventResponse
	requests int
}

func (c *flakyClient) Event(_ context.Context, _ wireproto.EventRequest) (wireproto.EventResponse, error) {
	c.requests++
	if c.requests <= c.timeouts {
		return wireproto.EventResponse{}, context.DeadlineExceeded
	}
	return c.response, nil
}

func (c *flakyClient) Close() error { return nil }

func TestRunRetriesATimedOutGuardOnceThenWarnsInsteadOfDenying(t *testing.T) {
	deny := `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny"}}` + "\n"
	completed := wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: deny, Guard: wireproto.GuardCompleted}
	for _, event := range []string{"PreToolUse", "PermissionRequest"} {
		t.Run(event+" retried", func(t *testing.T) {
			client := &flakyClient{timeouts: 1, response: completed}
			previous := openEventClient
			openEventClient = func() (eventClient, error) { return client, nil }
			t.Cleanup(func() { openEventClient = previous })
			code, stdout, stderr := runEvent(t, event, destructivePayload)
			if code != 0 || stdout != deny || stderr != "" || client.requests != 2 {
				t.Fatalf("exit=%d stdout=%q stderr=%q requests=%d, want the retry's verdict", code, stdout, stderr, client.requests)
			}
		})
		t.Run(event+" timed out twice", func(t *testing.T) {
			client := &flakyClient{timeouts: 2}
			previous := openEventClient
			openEventClient = func() (eventClient, error) { return client, nil }
			t.Cleanup(func() { openEventClient = previous })
			code, stdout, stderr := runEvent(t, event, destructivePayload)
			if code != 0 || client.requests != 2 || strings.Contains(stdout, "deny") ||
				!strings.Contains(stdout, `"systemMessage"`) || !strings.Contains(stdout, "timed out twice") ||
				!strings.Contains(stderr, "(transport-timeout); allowed with a warning") {
				t.Fatalf("exit=%d stdout=%q stderr=%q requests=%d, want a warning and no deny after two timeouts",
					code, stdout, stderr, client.requests)
			}
		})
	}
}

func TestRunPassesACompletedGuardThroughUnchanged(t *testing.T) {
	deny := `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny",` +
		`"permissionDecisionReason":"BLOCKED: pkill signals every process matching a name"}}` + "\n"
	for name, tc := range map[string]struct {
		response       wireproto.EventResponse
		stdout, stderr string
	}{
		"empty allow": {wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Guard: wireproto.GuardCompleted}, "", ""},
		"the guard's own deny": {wireproto.EventResponse{
			Schema: wireproto.Schema, Status: "ok", Stdout: deny, Guard: wireproto.GuardCompleted,
		}, deny, ""},
		"a sibling's stderr": {wireproto.EventResponse{
			Schema: wireproto.Schema, Status: "ok", Stderr: "warned\n", Guard: wireproto.GuardCompleted,
		}, "", "warned\n"},
	} {
		t.Run(name, func(t *testing.T) {
			scriptClient(t, &scriptedClient{response: tc.response}, nil)
			code, stdout, stderr := runEvent(t, "PreToolUse", destructivePayload)
			if code != 0 || stdout != tc.stdout || stderr != tc.stderr {
				t.Fatalf("exit=%d stdout=%q stderr=%q, want 0 %q %q", code, stdout, stderr, tc.stdout, tc.stderr)
			}
		})
	}
}

func TestRunKeepsNonMandatoryOutcomesAsTheyWere(t *testing.T) {
	shed := shedResponse(3, 4*time.Second, time.Second)
	for _, tc := range []struct {
		name, event, payload string
		open                 error
		client               scriptedClient
		code                 int
		stdout, stderr       string
	}{
		{"host unavailable", "PreToolUse", benignPayload, errors.New("captain: open signed host: absent"),
			scriptedClient{}, 1, "", "captain: open signed host: absent\n"},
		{"transport error", "PreToolUse", benignPayload, nil,
			scriptedClient{err: errors.New("captain: decode event response: boom")}, 1, "",
			"captain: decode event response: boom\n"},
		{"worker error", "PreToolUse", benignPayload, nil, scriptedClient{response: wireproto.EventResponse{
			Schema: wireproto.Schema, Status: "error", Stderr: "Traceback\n", Exit: 1,
		}}, 1, "", "Traceback\n"},
		{"shed", "PreToolUse", benignPayload, nil, scriptedClient{response: shed}, 0, "", shed.Stderr},
		{"no guard", "PreToolUse", benignPayload, nil,
			scriptedClient{response: wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: "{}\n"}},
			0, "{}\n", ""},
		{"destructive payload outside the guarded events", "PostToolUse", destructivePayload, nil,
			scriptedClient{response: wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok"}}, 0, "", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			client := tc.client
			scriptClient(t, &client, tc.open)
			code, stdout, stderr := runEvent(t, tc.event, tc.payload)
			if code != tc.code || stdout != tc.stdout || stderr != tc.stderr {
				t.Fatalf("exit=%d stdout=%q stderr=%q, want %d %q %q", code, stdout, stderr, tc.code, tc.stdout, tc.stderr)
			}
			if tc.open == nil && (len(client.requests) != 1 || client.requests[0].Mandatory) {
				t.Fatalf("requests = %+v, want one non-mandatory request", client.requests)
			}
		})
	}
}

func TestMainRejectsUnknownCommandsWithoutPassThrough(t *testing.T) {
	t.Parallel()
	var stdout, stderr bytes.Buffer
	if code := Main([]string{"review", "run"}, strings.NewReader(""), &stdout, &stderr); code != 2 {
		t.Fatalf("exit = %d, want 2", code)
	}
	if stdout.Len() != 0 || !strings.Contains(stderr.String(), "unknown command") {
		t.Fatalf("stdout=%q stderr=%q", stdout.String(), stderr.String())
	}
}

func TestVersionReportsExactSchemaAndBuild(t *testing.T) {
	t.Parallel()
	var stdout, stderr bytes.Buffer
	if code := Main([]string{"version"}, strings.NewReader(""), &stdout, &stderr); code != 0 {
		t.Fatalf("exit=%d stderr=%q", code, stderr.String())
	}
	want := `{"schema":1,"build":"` + Build + `"}` + "\n"
	if stdout.String() != want {
		t.Fatalf("version = %q, want %q", stdout.String(), want)
	}
}

func TestRunSpellsTheRequestFromThePayload(t *testing.T) {
	cwd := "/payload/cwd"
	payload := `{"session_id":"abc","cwd":"` + cwd + `"}`
	for _, tc := range []struct {
		name, claude, factory, want string
	}{
		{"claude beats factory", "/claude", "/factory", "/claude"},
		{"factory when claude is empty", "", "/factory", "/factory"},
		{"payload cwd when both are empty", "", "", cwd},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("CLAUDE_PROJECT_DIR", tc.claude)
			t.Setenv("FACTORY_PROJECT_DIR", tc.factory)
			request, err := eventRequest("Stop", strings.NewReader(payload))
			if err != nil {
				t.Fatal(err)
			}
			if request.Event != "Stop" || request.Root != tc.want || request.CWD != cwd ||
				request.PayloadRaw != payload || request.ClientPID != os.Getpid() || request.ClientPPID != os.Getppid() {
				t.Fatalf("request = %+v", request)
			}
		})
	}
}

func TestRunRefusesWhatItCannotDispatchWithoutBlocking(t *testing.T) {
	for _, tc := range []struct {
		name, input, want string
		args              []string
		timeout           string
	}{
		{"no event", `{"cwd":"/r"}`, "usage: capt-hookd run EVENT", nil, ""},
		{"flag for an event", `{"cwd":"/r"}`, "usage: capt-hookd run EVENT", []string{"--event"}, ""},
		{"extra argument", `{"cwd":"/r"}`, "usage: capt-hookd run EVENT", []string{"Stop", "--sync"}, ""},
		{"non-positive timeout", `{"cwd":"/r"}`, "must be positive", []string{"Stop"}, "0"},
		{"payload is not JSON", `not-json`, "decode event", []string{"Stop"}, ""},
		{"payload names no cwd", `{"session_id":"abc"}`, "cwd is required", []string{"Stop"}, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("CAPT_HOOK_CLIENT_TIMEOUT", tc.timeout)
			t.Setenv("CLAUDE_PROJECT_DIR", "/project")
			var stdout, stderr bytes.Buffer
			code := Main(append([]string{"run"}, tc.args...), strings.NewReader(tc.input), &stdout, &stderr)
			if code != 1 || stdout.Len() != 0 || !strings.Contains(stderr.String(), tc.want) {
				t.Fatalf("exit=%d stdout=%q stderr=%q, want exit 1 naming %q", code, stdout.String(), stderr.String(), tc.want)
			}
		})
	}
}

func TestRunAsyncExitsWithoutDispatch(t *testing.T) {
	t.Parallel()
	var stdout, stderr bytes.Buffer
	if code := Main([]string{"run", "PreToolUse", "--async"}, strings.NewReader("not-json"), &stdout, &stderr); code != 0 ||
		stdout.Len() != 0 || stderr.Len() != 0 {
		t.Fatalf("exit=%d stdout=%q stderr=%q", code, stdout.String(), stderr.String())
	}
}

func TestRequestEnvironmentHasExactScope(t *testing.T) {
	t.Parallel()
	got := requestEnvironment([]string{
		"HOME=/tmp/home", "PATH=/bin", "CLAUDE_CONFIG_DIR=/account/18",
		"FACTORY_PROJECT_DIR=/repo", "HOOKS_PROFILE=strict", "XDG_CACHE_HOME=/cache",
	})
	if len(got) != 4 || got["CLAUDE_CONFIG_DIR"] != "/account/18" ||
		got["FACTORY_PROJECT_DIR"] != "/repo" || got["HOOKS_PROFILE"] != "strict" ||
		got["XDG_CACHE_HOME"] != "/cache" {
		t.Fatalf("request environment = %v", got)
	}
}

func TestRequestEnvironmentCarriesTheCerebrasKey(t *testing.T) {
	t.Parallel()
	got := requestEnvironment([]string{"CEREBRAS_API_KEY=csk-test", "OPENAI_API_KEY=sk-test"})
	if len(got) != 1 || got["CEREBRAS_API_KEY"] != "csk-test" {
		t.Fatalf("request environment = %v", got)
	}
}

func TestRequestEnvironmentCarriesTheOrcaTerminal(t *testing.T) {
	t.Parallel()
	got := requestEnvironment([]string{
		"ORCA_TERMINAL_HANDLE=term-7", "ORCA_USER_DATA_PATH=/orca", "TERM_PROGRAM=Orca",
	})
	if len(got) != 2 || got["ORCA_TERMINAL_HANDLE"] != "term-7" || got["ORCA_USER_DATA_PATH"] != "/orca" {
		t.Fatalf("request environment = %v", got)
	}
}

func TestDurationEnvironmentUsesSeconds(t *testing.T) {
	t.Setenv("CAPT_HOOK_CLIENT_TIMEOUT", "1.25")
	if got := durationFromEnvironment("CAPT_HOOK_CLIENT_TIMEOUT", time.Second); got != 1250*time.Millisecond {
		t.Fatalf("duration = %s", got)
	}
}
