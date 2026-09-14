package hookd

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
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

// TestRunRefusesANonPositiveTimeout covers both inputs that reach the request
// deadline: a zero or negative deadline is a usage error, not the slow, loaded
// machine an immediately-expired context reports it as.
func TestRunRefusesANonPositiveTimeout(t *testing.T) {
	run := func(t *testing.T, extra ...string) (int, string) {
		t.Helper()
		var stdout, stderr bytes.Buffer
		args := append([]string{
			"run", "--event", "PreToolUse", "--root", t.TempDir(), "--cwd", t.TempDir(),
			"--python", "/usr/bin/python3", "--build", Build,
		}, extra...)
		code := Main(args, strings.NewReader(`{"session_id":"abc"}`), &stdout, &stderr)
		return code, stderr.String()
	}
	t.Run("flag", func(t *testing.T) {
		code, stderr := run(t, "--timeout", "0s")
		if code != 2 || !strings.Contains(stderr, "must be positive") {
			t.Fatalf("exit=%d stderr=%q", code, stderr)
		}
	})
	t.Run("environment", func(t *testing.T) {
		t.Setenv("CAPT_HOOK_CLIENT_TIMEOUT", "-1")
		code, stderr := run(t)
		if code != 2 || !strings.Contains(stderr, "must be positive") {
			t.Fatalf("exit=%d stderr=%q", code, stderr)
		}
	})
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

func productToolEnv(t *testing.T) string {
	t.Helper()
	home, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("DAEMONKIT_HOME", home)
	toolDir := filepath.Join(home, ".daemonkit", "tools", "capt-hook", Build)
	venvBin := filepath.Join(toolDir, "capt-hook", "bin")
	for _, dir := range []string{venvBin, filepath.Join(toolDir, "bin")} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	for _, file := range []string{filepath.Join(venvBin, "hook"), filepath.Join(venvBin, "python"), filepath.Join(toolDir, ".installed")} {
		if err := os.WriteFile(file, nil, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink(filepath.Join(venvBin, "hook"), filepath.Join(toolDir, "bin", "hook")); err != nil {
		t.Fatal(err)
	}
	return filepath.Join(venvBin, "python")
}

func spell(t *testing.T, args ...string) (wireproto.EventRequest, int, string) {
	t.Helper()
	var stderr bytes.Buffer
	request, _, code := eventRequest(args, strings.NewReader(`{"session_id":"abc"}`), &stderr)
	return request, code, stderr.String()
}

func withoutProjectEnvironment(t *testing.T) {
	t.Helper()
	t.Setenv("CLAUDE_PROJECT_DIR", "")
	t.Setenv("FACTORY_PROJECT_DIR", "")
}

func TestRunSpellsOneRequestFromEitherGrammar(t *testing.T) {
	python := productToolEnv(t)
	withoutProjectEnvironment(t)
	cwd, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Chdir(cwd)
	root := t.TempDir()
	shim := func(event, root string, extra ...string) []string {
		return append([]string{
			"run", "--event", event, "--root", root, "--cwd", cwd, "--python", python, "--build", Build,
		}, extra...)
	}
	for _, tc := range []struct {
		name       string
		hook, shim []string
	}{
		{"sync", []string{"run", "PreToolUse"}, shim("PreToolUse", cwd)},
		{"async", []string{"run", "PreToolUse", "--async"}, shim("PreToolUse", cwd, "--async")},
		{"spaced root", []string{"--root", root, "run", "Stop"}, shim("Stop", root)},
		{"joined root", []string{"--root=" + root, "run", "Stop", "--async"}, shim("Stop", root, "--async")},
		{"empty joined root", []string{"--root=", "run", "Stop"}, shim("Stop", cwd)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			hook, hookCode, hookStderr := spell(t, tc.hook...)
			flags, flagsCode, flagsStderr := spell(t, tc.shim...)
			if hookCode != 0 || flagsCode != 0 {
				t.Fatalf("hook exit=%d stderr=%q; shim exit=%d stderr=%q", hookCode, hookStderr, flagsCode, flagsStderr)
			}
			if !reflect.DeepEqual(hook, flags) {
				t.Fatalf("hook grammar = %+v\nshim grammar = %+v", hook, flags)
			}
		})
	}
}

func TestRunRootPrecedence(t *testing.T) {
	productToolEnv(t)
	cwd, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Chdir(cwd)
	for _, tc := range []struct {
		name, claude, factory string
		args                  []string
		want                  string
	}{
		{"flag beats both", "/claude", "/factory", []string{"--root", "/flag", "run", "Stop"}, "/flag"},
		{"claude beats factory", "/claude", "/factory", []string{"run", "Stop"}, "/claude"},
		{"factory when claude is empty", "", "/factory", []string{"run", "Stop"}, "/factory"},
		{"cwd when both are empty", "", "", []string{"run", "Stop"}, cwd},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("CLAUDE_PROJECT_DIR", tc.claude)
			t.Setenv("FACTORY_PROJECT_DIR", tc.factory)
			request, code, stderr := spell(t, tc.args...)
			if code != 0 || request.Root != tc.want {
				t.Fatalf("exit=%d stderr=%q root=%q, want %q", code, stderr, request.Root, tc.want)
			}
		})
	}
}

func TestRunCWDIsTheKernelsNotPWDs(t *testing.T) {
	productToolEnv(t)
	withoutProjectEnvironment(t)
	dir, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(dir, "target")
	link := filepath.Join(dir, "link")
	if err := os.Mkdir(target, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
	t.Chdir(link)
	request, code, stderr := spell(t, "run", "Stop")
	if code != 0 || request.CWD != target || request.Root != target {
		t.Fatalf("exit=%d stderr=%q cwd=%q root=%q, want %q", code, stderr, request.CWD, request.Root, target)
	}
}

func TestRunDeletedCWDStillSpellsADispatch(t *testing.T) {
	productToolEnv(t)
	withoutProjectEnvironment(t)
	for _, tc := range []struct {
		name, pwd, want string
		recreate        bool
	}{
		{"with PWD", "/deleted/workspace", "/deleted/workspace", false},
		{"without PWD", "", "/", false},
		{"recreated at its path", "/deleted/workspace", "/deleted/workspace", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			gone := filepath.Join(t.TempDir(), "gone")
			if err := os.Mkdir(gone, 0o700); err != nil {
				t.Fatal(err)
			}
			t.Chdir(gone)
			if err := os.Remove(gone); err != nil {
				t.Fatal(err)
			}
			if tc.recreate {
				if err := os.Mkdir(gone, 0o700); err != nil {
					t.Fatal(err)
				}
			}
			t.Setenv("PWD", tc.pwd)
			request, code, stderr := spell(t, "run", "Stop")
			if code != 0 || request.CWD != tc.want || request.Root != tc.want {
				t.Fatalf("exit=%d stderr=%q cwd=%q root=%q, want %q", code, stderr, request.CWD, request.Root, tc.want)
			}
		})
	}
}

func TestRunDerivesTheToolEnvOfItsOwnBuild(t *testing.T) {
	python := productToolEnv(t)
	request, code, stderr := spell(t, "run", "Stop")
	if code != 0 || request.Python != python || request.Build != Build {
		t.Fatalf("exit=%d stderr=%q python=%q build=%q, want %q %q", code, stderr, request.Python, request.Build, python, Build)
	}
}

func TestRunFailsClosedWhenTheToolEnvCannotMaterialize(t *testing.T) {
	t.Setenv("DAEMONKIT_HOME", t.TempDir())
	t.Setenv("PATH", t.TempDir())
	_, code, stderr := spell(t, "run", "Stop")
	if code != 1 || !strings.Contains(stderr, "capt-hookd: resolve Python product "+Build) {
		t.Fatalf("exit=%d stderr=%q", code, stderr)
	}
}

var shimRejections = [][]string{
	{"run"},
	{"run", ""},
	{"run", "-x"},
	{"run", "Stop", "--sync"},
	{"run", "Stop", "--async", "extra"},
	{"--root"},
	{"--root", "/r"},
	{"--root", "/r", "run"},
	{"--root=/r", "run", "--async"},
	{"--root", "/r", "--root", "/s", "run", "Stop"},
}

func TestRunRejectsExactlyWhatTheShimRejects(t *testing.T) {
	t.Parallel()
	for _, args := range shimRejections {
		var stdout, stderr bytes.Buffer
		if code := Main(args, strings.NewReader(""), &stdout, &stderr); code != 1 ||
			stdout.Len() != 0 || stderr.String() != "usage: hook [--root ROOT] run EVENT [--async]\n" {
			t.Errorf("%q: exit=%d stdout=%q stderr=%q", args, code, stdout.String(), stderr.String())
		}
	}
}

const parityCaseEnv = "CAPT_HOOK_PARITY_CASE"

const parityShim = `
import json, os, sys
from capt_hook_client import client

emitted = sys.argv[1]

def execv(_path, argv):
    with open(emitted, "w") as out:
        json.dump(argv[1:], out)
    os._exit(0)

os.execv = execv
sys.argv = ["hook", *sys.argv[2:]]
client.main()
`

const parityScene = `
cd "$1" || exit 97
case "$2" in
  deleted) rmdir "$1" ;;
  recreated) rmdir "$1" && mkdir "$1" ;;
esac
[ "$3" = keep ] || unset PWD
shift 3
exec "$@"
`

type parityOutcome struct {
	Code    int                    `json:"code"`
	Stderr  string                 `json:"stderr"`
	Request wireproto.EventRequest `json:"request"`
}

type parityCase struct {
	argv       []string
	scene, pwd string
	env        []string
}

// TestDispatchParityHelper runs as a child of TestRunSpellsTheRequestTheShimDoes,
// under the cwd and environment of one case, and spells a request for each argv
// the case names.
func TestDispatchParityHelper(t *testing.T) {
	path := os.Getenv(parityCaseEnv)
	if path == "" {
		t.Skipf("%s is unset; TestRunSpellsTheRequestTheShimDoes drives this test", parityCaseEnv)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var argvs map[string][]string
	if err := json.Unmarshal(data, &argvs); err != nil {
		t.Fatal(err)
	}
	outcomes := make(map[string]parityOutcome, len(argvs))
	for name, argv := range argvs {
		request, code, stderr := spell(t, argv...)
		outcomes[name] = parityOutcome{Code: code, Stderr: stderr, Request: request}
	}
	encoded, err := json.Marshal(outcomes)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path+".out", encoded, 0o600); err != nil {
		t.Fatal(err)
	}
}

func shimToolEnv(t *testing.T) (home, python string) {
	t.Helper()
	home, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	toolDir := filepath.Join(home, ".daemonkit", "tools", "capt-hook", Build)
	venv := filepath.Join(toolDir, "capt-hook")
	if out, err := exec.Command("python3", "-m", "venv", "--without-pip", venv).CombinedOutput(); err != nil {
		t.Fatalf("create tool env: %v: %s", err, out)
	}
	sites, err := filepath.Glob(filepath.Join(venv, "lib", "python*", "site-packages"))
	if err != nil || len(sites) != 1 {
		t.Fatalf("site-packages = %v, %v", sites, err)
	}
	dist := filepath.Join(sites[0], "capt_hook-"+Build+".dist-info")
	if err := os.Mkdir(dist, 0o700); err != nil {
		t.Fatal(err)
	}
	metadata := "Metadata-Version: 2.1\nName: capt-hook\nVersion: " + Build + "\n"
	if err := os.WriteFile(filepath.Join(dist, "METADATA"), []byte(metadata), 0o600); err != nil {
		t.Fatal(err)
	}
	client, err := filepath.Abs(filepath.Join("..", "..", "capt_hook_client"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(client, filepath.Join(sites[0], "capt_hook_client")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(venv, "bin", "hook"), nil, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(toolDir, "bin"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(venv, "bin", "hook"), filepath.Join(toolDir, "bin", "hook")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(toolDir, ".installed"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	return home, filepath.Join(venv, "bin", "python")
}

// TestRunSpellsTheRequestTheShimDoes is the differential against the Python
// shim: each case runs capt_hook_client inside a tool env laid out the way
// binrun materializes it, capturing the argv it would exec, then spells both
// that argv and the raw one in a child under the same cwd and environment.
func TestRunSpellsTheRequestTheShimDoes(t *testing.T) {
	t.Parallel()
	home, python := shimToolEnv(t)
	grammars := [][]string{
		{"run", "PreToolUse"},
		{"run", "PostToolUse", "--async"},
		{"--root", "/spelled/root", "run", "Stop"},
		{"--root=/joined/root", "run", "Stop", "--async"},
		{"--root=", "run", "Stop"},
	}
	var cases []parityCase
	for _, argv := range grammars {
		for _, scene := range [][2]string{
			{"plain", "keep"}, {"symlink", "keep"}, {"deleted", "keep"}, {"deleted", "unset"}, {"recreated", "keep"},
		} {
			cases = append(cases, parityCase{argv: argv, scene: scene[0], pwd: scene[1]})
		}
	}
	for _, argv := range [][]string{grammars[0], grammars[4]} {
		for _, scene := range []string{"plain", "deleted"} {
			for _, env := range [][]string{
				{"CLAUDE_PROJECT_DIR=/claude"},
				{"FACTORY_PROJECT_DIR=/factory"},
				{"CLAUDE_PROJECT_DIR=", "FACTORY_PROJECT_DIR=/factory"},
				{"CLAUDE_PROJECT_DIR=/claude", "FACTORY_PROJECT_DIR=/factory"},
			} {
				cases = append(cases, parityCase{argv: argv, scene: scene, pwd: "keep", env: env})
			}
		}
	}
	for _, argv := range shimRejections {
		cases = append(cases, parityCase{argv: argv, scene: "plain", pwd: "keep"})
	}
	for _, tc := range cases {
		t.Run(fmt.Sprintf("%q/%s/%s/%q", tc.argv, tc.scene, tc.pwd, tc.env), func(t *testing.T) {
			t.Parallel()
			scratch, err := filepath.EvalSymlinks(t.TempDir())
			if err != nil {
				t.Fatal(err)
			}
			cwd := filepath.Join(scratch, "cwd")
			scene := tc.scene
			if scene == "symlink" {
				cwd, scene = filepath.Join(scratch, "link"), "plain"
				if err := os.Symlink(filepath.Join(scratch, "cwd"), cwd); err != nil {
					t.Fatal(err)
				}
			}
			run := func(extra []string, command ...string) (int, string) {
				t.Helper()
				if err := os.MkdirAll(filepath.Join(scratch, "cwd"), 0o700); err != nil {
					t.Fatal(err)
				}
				cmd := exec.Command("/bin/sh", append([]string{"-c", parityScene, "sh", cwd, scene, tc.pwd}, command...)...)
				cmd.Env = append(append([]string{
					"PATH=" + os.Getenv("PATH"), "HOME=" + home, "DAEMONKIT_HOME=" + home,
				}, tc.env...), extra...)
				cmd.Stdin = strings.NewReader(`{"session_id":"parity"}`)
				var stderr bytes.Buffer
				cmd.Stderr = &stderr
				err := cmd.Run()
				var exitErr *exec.ExitError
				if err != nil && !errors.As(err, &exitErr) {
					t.Fatal(err)
				}
				return cmd.ProcessState.ExitCode(), stderr.String()
			}

			emitted := filepath.Join(scratch, "shim.json")
			shimCode, shimStderr := run(nil, append([]string{python, "-c", parityShim, emitted}, tc.argv...)...)
			argvs := map[string][]string{"raw": tc.argv}
			if shimCode == 0 {
				data, err := os.ReadFile(emitted)
				if err != nil {
					t.Fatal(err)
				}
				var shim []string
				if err := json.Unmarshal(data, &shim); err != nil {
					t.Fatal(err)
				}
				argvs["shim"] = shim
			}
			spec := filepath.Join(scratch, "case.json")
			data, err := json.Marshal(argvs)
			if err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(spec, data, 0o600); err != nil {
				t.Fatal(err)
			}
			if code, stderr := run([]string{parityCaseEnv + "=" + spec}, os.Args[0], "-test.run=^TestDispatchParityHelper$"); code != 0 {
				t.Fatalf("helper exit=%d stderr=%s", code, stderr)
			}
			data, err = os.ReadFile(spec + ".out")
			if err != nil {
				t.Fatal(err)
			}
			var outcomes map[string]parityOutcome
			if err := json.Unmarshal(data, &outcomes); err != nil {
				t.Fatal(err)
			}

			raw := outcomes["raw"]
			if raw.Code != shimCode {
				t.Fatalf("exit = %d (stderr %q), shim exit = %d (stderr %q)", raw.Code, raw.Stderr, shimCode, shimStderr)
			}
			if shimCode != 0 {
				if raw.Stderr != shimStderr {
					t.Fatalf("stderr = %q, shim stderr = %q", raw.Stderr, shimStderr)
				}
				return
			}
			if shim := outcomes["shim"]; shim.Code != 0 || !reflect.DeepEqual(raw.Request, shim.Request) {
				t.Fatalf("raw argv spells %+v\nshim argv spells %+v (exit %d, stderr %q)", raw.Request, shim.Request, shim.Code, shim.Stderr)
			}
		})
	}
}
