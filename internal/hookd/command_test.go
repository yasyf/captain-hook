package hookd

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
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

func TestMainAnswersTrustVerifierChild(t *testing.T) {
	t.Parallel()
	var stdout, stderr bytes.Buffer
	request := base64.RawURLEncoding.EncodeToString([]byte(`{"protocol":1}`))
	if code := Main([]string{"--daemonkit-trust-verifier-v1", request}, strings.NewReader(""), &stdout, &stderr); code != 0 {
		t.Fatalf("exit=%d stderr=%q", code, stderr.String())
	}
	var response struct {
		Protocol int    `json:"protocol"`
		Result   string `json:"result"`
	}
	if err := json.Unmarshal(stdout.Bytes(), &response); err != nil {
		t.Fatalf("decode verifier response %q: %v", stdout.String(), err)
	}
	if response.Protocol != 1 || response.Result != "untrusted" {
		t.Fatalf("response = %+v, want protocol 1 result untrusted", response)
	}
}

func TestMainRejectsMalformedTrustVerifierRequest(t *testing.T) {
	t.Parallel()
	var stdout, stderr bytes.Buffer
	if code := Main([]string{"--daemonkit-trust-verifier-v1", "not-base64!"}, strings.NewReader(""), &stdout, &stderr); code != 2 {
		t.Fatalf("exit = %d, want 2", code)
	}
	if !strings.Contains(stderr.String(), "trust:") || strings.Contains(stderr.String(), "unknown command") {
		t.Fatalf("stderr = %q, want a trust error, not unknown-command", stderr.String())
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

func TestRunRejectsBuildSkewBeforeDaemonWork(t *testing.T) {
	t.Parallel()
	var stdout, stderr bytes.Buffer
	code := Main([]string{
		"run", "--event", "PreToolUse", "--root", t.TempDir(), "--cwd", t.TempDir(),
		"--python", "/usr/bin/python3", "--build", "99.0.0",
	}, strings.NewReader(`{"session_id":"abc"}`), &stdout, &stderr)
	if code != 1 || !strings.Contains(stderr.String(), "does not match signed host build") {
		t.Fatalf("exit=%d stderr=%q", code, stderr.String())
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

func TestDurationEnvironmentUsesSeconds(t *testing.T) {
	t.Setenv("CAPT_HOOK_CLIENT_TIMEOUT", "1.25")
	if got := durationFromEnvironment("CAPT_HOOK_CLIENT_TIMEOUT", time.Second); got != 1250*time.Millisecond {
		t.Fatalf("duration = %s", got)
	}
}
