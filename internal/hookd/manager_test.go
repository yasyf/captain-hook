package hookd

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

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
	root := t.TempDir()
	cmd := workerCmd(workerKey{
		id: "abc", root: root, python: "/usr/bin/python3", build: "12.9.1",
		env: map[string]string{"HOOKS_PROFILE": "strict"},
	})
	if !cmd.Session {
		t.Fatal("worker spawn does not own its descendants")
	}
	if cmd.Path != "/usr/bin/python3" || cmd.Dir != root ||
		len(cmd.Args) != 3 || cmd.Args[0] != "-P" || cmd.Args[1] != "-m" || cmd.Args[2] != "captain_hook.worker" {
		t.Fatalf("worker spawn = %q %q in %q", cmd.Path, cmd.Args, cmd.Dir)
	}
	seen := map[string]string{}
	for _, item := range cmd.Env {
		name, value, _ := strings.Cut(item, "=")
		seen[name] = value
	}
	// The whole inherited environment carries the user's secrets, so a failure names only what it asserts.
	if seen["LANG"] != "C" || seen["PATH"] == "" || seen["HOOKS_PROFILE"] != "strict" {
		t.Fatalf("worker environment: LANG=%q PATH set=%t HOOKS_PROFILE=%q",
			seen["LANG"], seen["PATH"] != "", seen["HOOKS_PROFILE"])
	}
}

// TestWorkerCmdSurvivesARootDeletedUnderTheSession pins the fallback: a chdir
// into a missing Dir surfaces as posix_spawn reporting the interpreter absent,
// which killed every dispatch from a session whose workspace had been reaped
// before it first needed a worker (observed 2026-09-02).
func TestWorkerCmdSurvivesARootDeletedUnderTheSession(t *testing.T) {
	t.Parallel()
	root := filepath.Join(t.TempDir(), "reaped")
	cmd := workerCmd(workerKey{id: "abc", root: root, python: "/usr/bin/python3", build: "12.9.1"})
	if cmd.Dir != os.TempDir() {
		t.Fatalf("worker spawn Dir = %q, want the temp dir for a missing root", cmd.Dir)
	}
}

// fillIdleWorkers seeds the cache to the bound with idle entries whose last use
// walks backwards, so live-0 is the least recently used of them.
func fillIdleWorkers(manager *workerManager, base time.Time, cached error) {
	for index := range maxLiveWorkers {
		id := fmt.Sprintf("live-%d", index)
		entry := &workerEntry{
			ready: make(chan struct{}), key: workerKey{id: id, root: "/live"},
			err: cached, lastUsed: base.Add(time.Duration(index) * time.Minute),
		}
		close(entry.ready)
		manager.entries[id] = entry
	}
}

// TestWorkerManagerEvictsLeastRecentlyUsedAtTheBound pins the bound as an
// eviction trigger rather than a wall: a cache full of idle interpreters gives
// up its coldest one so a new root is admitted. Before this, a machine whose
// roots churn — scratch checkouts, short-lived worktrees — filled the cache
// with keys it would never ask for again and then refused every new project.
func TestWorkerManagerEvictsLeastRecentlyUsedAtTheBound(t *testing.T) {
	t.Parallel()
	cached := errors.New("served from the cache")
	manager := mustWorkerManager(t)
	fillIdleWorkers(manager, time.Unix(0, 0), cached)

	if _, err := manager.acquire(t.Context(), workerKey{id: "one-past-the-bound", root: "/fresh"}); errors.Is(err, ErrWorkerCapacity) {
		t.Fatal("a full cache of idle workers refused admission instead of evicting")
	}
	if _, live := manager.entries["live-0"]; live {
		t.Fatal("the least recently used worker survived the bound")
	}
	if _, live := manager.entries["live-63"]; !live {
		t.Fatal("eviction took a warm worker instead of the coldest one")
	}
}

// TestWorkerManagerRefusesWhenEveryWorkerIsBusy keeps the admission refusal for
// the one state it describes — real saturation. An in-flight entry is never
// evicted: the dispatch holding it would lose its interpreter mid-call.
func TestWorkerManagerRefusesWhenEveryWorkerIsBusy(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	fillIdleWorkers(manager, time.Unix(0, 0), errors.New("served from the cache"))
	for _, entry := range manager.entries {
		entry.inflight = 1
	}

	_, err := manager.acquire(t.Context(), workerKey{id: "one-past-the-bound", root: "/fresh"})
	if !errors.Is(err, ErrWorkerCapacity) {
		t.Fatalf("acquire past a fully busy bound = %v, want %v", err, ErrWorkerCapacity)
	}
}

// TestWorkerManagerSweepRetiresIdleAndDeadRoots pins the two reasons a cached
// interpreter stops earning its slot — nothing has wanted it for workerIdleTTL,
// or its root has been deleted — and the two that keep it: recent use, and a
// dispatch still holding it.
func TestWorkerManagerSweepRetiresIdleAndDeadRoots(t *testing.T) {
	t.Parallel()
	now := time.Unix(1<<32, 0)
	live := t.TempDir()
	dead := filepath.Join(t.TempDir(), "reaped")
	manager := mustWorkerManager(t)
	seed := func(id, root string, lastUsed time.Time, inflight int) {
		entry := &workerEntry{
			ready: make(chan struct{}), key: workerKey{id: id, root: root},
			lastUsed: lastUsed, inflight: inflight,
		}
		close(entry.ready)
		manager.entries[id] = entry
	}
	seed("warm", live, now.Add(-time.Minute), 0)
	seed("cold", live, now.Add(-2*workerIdleTTL), 0)
	seed("dead-root", dead, now, 0)
	seed("busy", live, now.Add(-2*workerIdleTTL), 1)

	manager.sweep(now)

	if _, kept := manager.entries["warm"]; !kept {
		t.Error("sweep retired a worker used a minute ago")
	}
	if _, kept := manager.entries["cold"]; kept {
		t.Error("sweep kept a worker idle past the TTL")
	}
	if _, kept := manager.entries["dead-root"]; kept {
		t.Error("sweep kept a worker whose root no longer exists")
	}
	if _, kept := manager.entries["busy"]; !kept {
		t.Error("sweep retired a worker with a dispatch in flight")
	}
}

// TestEphemeralRootIsScoped keeps the ephemeral test narrow: a scratch root
// under the system temp directory, and nothing else. Misreading a real project
// as ephemeral would stop it ever caching an interpreter.
func TestEphemeralRootIsScoped(t *testing.T) {
	t.Parallel()
	tmp, err := filepath.EvalSymlinks(os.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if !ephemeralRoot(filepath.Join(tmp, "slop-cop-llm-906868983")) {
		t.Error("a scratch root under TMPDIR was not treated as ephemeral")
	}
	if ephemeralRoot(tmp) {
		t.Error("the temp directory itself was treated as an ephemeral root")
	}
	for _, root := range []string{"/Users/someone/Code/project", "/live"} {
		if ephemeralRoot(root) {
			t.Errorf("project root %q was treated as ephemeral", root)
		}
	}
}

// TestReleaseRetiresEphemeralEntryImmediately pins the scratch-root path: the
// entry leaves the cache as soon as its last dispatch finishes, rather than
// occupying a slot until the sweep notices it.
func TestReleaseRetiresEphemeralEntryImmediately(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	entry := &workerEntry{
		ready: make(chan struct{}), key: workerKey{id: "scratch", root: "/tmp/scratch"},
		inflight: 1, ephemeral: true,
	}
	close(entry.ready)
	manager.entries["scratch"] = entry

	manager.release(entry)

	if _, cached := manager.entries["scratch"]; cached {
		t.Fatal("an ephemeral entry stayed cached after its last dispatch")
	}
}

func TestWorkerManagerCloseReportsJoinedProductGraph(t *testing.T) {
	manager := mustWorkerManager(t)
	joined, err := manager.Close(context.Background())
	if err != nil || !joined {
		t.Fatalf("Close = joined %t, err %v", joined, err)
	}
}

func mustWorkerManager(t *testing.T) *workerManager {
	t.Helper()
	manager, err := newWorkerManager(daemonkit.Ctx{}, io.Discard)
	if err != nil {
		t.Fatal(err)
	}
	return manager
}
