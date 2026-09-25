package hookd

import (
	"bytes"
	"os"
	"strings"
	"testing"
	"time"
)

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
