package hookd

import (
	"context"
	"errors"
	"fmt"
	"io"
	"strings"
	"testing"

	"github.com/yasyf/daemonkit"
)

func TestWorkerKeyExcludesSessionAndAccountEnvironment(t *testing.T) {
	t.Parallel()
	base := testEventRequest("PreToolUse")
	base.Root = t.TempDir()
	base.Env = map[string]string{
		"CLAUDE_CODE_SESSION_ID": "session-a", "CLAUDE_CONFIG_DIR": "/account/a",
		"HOOKS_PROFILE": "strict", "CAPTAIN_HOOK_STATE_DIR": "/state",
	}
	changed := base
	changed.Env = map[string]string{
		"CLAUDE_CODE_SESSION_ID": "session-b", "CLAUDE_CONFIG_DIR": "/account/b",
		"HOOKS_PROFILE": "strict", "CAPTAIN_HOOK_STATE_DIR": "/state",
	}
	first, err := makeWorkerKey(base)
	if err != nil {
		t.Fatal(err)
	}
	second, err := makeWorkerKey(changed)
	if err != nil {
		t.Fatal(err)
	}
	if first.id != second.id {
		t.Fatalf("account/session environment partitioned workers: %s != %s", first.id, second.id)
	}
	changed.Env["HOOKS_PROFILE"] = "relaxed"
	third, err := makeWorkerKey(changed)
	if err != nil {
		t.Fatal(err)
	}
	if third.id == first.id {
		t.Fatal("semantic HOOKS environment did not partition workers")
	}
}

func TestSessionIDFallsBackWithoutInventingIdentity(t *testing.T) {
	t.Parallel()
	if got := sessionID(`{"session_id":"abc"}`); got != "abc" {
		t.Fatalf("sessionID = %q", got)
	}
	if got := sessionID(`not-json`); got != "" {
		t.Fatalf("malformed sessionID = %q", got)
	}
}

// TestWorkerBaseEnvironmentSeedsDiscoveryWithoutLeakingFirstClientScope pins
// both halves: the host's PATH and a stable locale reach the worker — a
// non-nil Cmd.Env is the child's exact environment and daemonkit injects
// nothing into it — while every name a request may set semantically is
// stripped so only the worker key decides it.
func TestWorkerBaseEnvironmentSeedsDiscoveryWithoutLeakingFirstClientScope(t *testing.T) {
	t.Parallel()
	base := workerBaseEnvironment([]string{
		"PATH=/opt/homebrew/bin:/bin", "LANG=en_US.UTF-8", "HOME=/tmp/home",
		"CLAUDE_CONFIG_DIR=/account/18", "CLAUDE_CODE_SESSION_ID=session-a",
		"HOOKS_PROFILE=strict", "CAPT_HOOK_RUN_DIR=/old",
	})
	want := map[string]bool{"PATH=/opt/homebrew/bin:/bin": true, "LANG=C": true, "HOME=/tmp/home": true}
	if len(base) != len(want) {
		t.Fatalf("base environment = %v", base)
	}
	for _, item := range base {
		if !want[item] {
			t.Fatalf("unexpected base environment entry %q", item)
		}
	}
	pathless := workerBaseEnvironment([]string{"HOME=/tmp/home"})
	if len(pathless) != 3 || pathless[0] != "PATH=/usr/bin:/bin:/usr/sbin:/sbin" ||
		pathless[1] != "LANG=C" || pathless[2] != "HOME=/tmp/home" {
		t.Fatalf("pathless host environment = %v", pathless)
	}
}

// TestWorkerCmdOwnsTheWholeWorkerSession pins the posture a worker is spawned
// under: hook subprocesses live inside the worker's own session, so settlement
// covers them at restart, timeout, crash, and upgrade instead of leaving them
// running under a retired generation.
func TestWorkerCmdOwnsTheWholeWorkerSession(t *testing.T) {
	t.Parallel()
	cmd := workerCmd(workerKey{
		id: "abc", root: "/tmp/repo", python: "/usr/bin/python3", build: "12.9.1",
		env: map[string]string{"HOOKS_PROFILE": "strict"},
	})
	if !cmd.Session {
		t.Fatal("worker spawn does not own its descendants")
	}
	if cmd.Path != "/usr/bin/python3" || cmd.Dir != "/tmp/repo" ||
		len(cmd.Args) != 2 || cmd.Args[0] != "-m" || cmd.Args[1] != "captain_hook.worker" {
		t.Fatalf("worker cmd = %#v", cmd)
	}
	seen := map[string]string{}
	for _, item := range cmd.Env {
		name, value, _ := strings.Cut(item, "=")
		seen[name] = value
	}
	if seen["LANG"] != "C" || seen["PATH"] == "" || seen["HOOKS_PROFILE"] != "strict" {
		t.Fatalf("worker environment = %v", cmd.Env)
	}
}

// TestWorkerManagerRefusesPastTheLiveBound pins the admission bound at
// maxLiveWorkers: the 65th distinct key is refused by name rather than queued,
// while a key already in the cache is still served from it — the bound gates
// starting an interpreter, not reusing one.
func TestWorkerManagerRefusesPastTheLiveBound(t *testing.T) {
	t.Parallel()
	cached := errors.New("served from the cache")
	manager := newWorkerManager(daemonkit.Ctx{}, io.Discard)
	for index := range maxLiveWorkers {
		id := fmt.Sprintf("live-%d", index)
		entry := &workerEntry{ready: make(chan struct{}), key: workerKey{id: id}, err: cached}
		close(entry.ready)
		manager.entries[id] = entry
	}

	_, err := manager.worker(t.Context(), workerKey{id: "one-past-the-bound"})
	if !errors.Is(err, ErrWorkerCapacity) {
		t.Fatalf("worker past the bound = %v, want %v", err, ErrWorkerCapacity)
	}
	if _, err := manager.worker(t.Context(), workerKey{id: "live-0"}); !errors.Is(err, cached) {
		t.Fatalf("a cached key at the bound = %v, want the cache's own answer", err)
	}

	delete(manager.entries, "live-0")
	if _, err := manager.worker(t.Context(), workerKey{id: "one-past-the-bound"}); errors.Is(err, ErrWorkerCapacity) {
		t.Fatal("admission still refused with room in the cache")
	}
}

func TestWorkerManagerCloseReportsJoinedProductGraph(t *testing.T) {
	manager := newWorkerManager(daemonkit.Ctx{}, io.Discard)
	joined, err := manager.Close(context.Background())
	if err != nil || !joined {
		t.Fatalf("Close = joined %t, err %v", joined, err)
	}
}
