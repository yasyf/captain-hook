package hookd

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
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

// scriptedWorker hands the manager a handshaken worker whose far end the test
// serves, so dispatch spawns nothing.
func scriptedWorker(t *testing.T, manager *workerManager, serve func(conn net.Conn)) {
	t.Helper()
	worker, serverConn := silentWorker(t)
	go serve(serverConn)
	manager.start = func(context.Context, workerKey) (*workerClient, error) { return worker, nil }
}

func TestConsecutiveActorEventsShareOneWorker(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	frames := make(chan wireproto.Frame, 3)
	worker, serverConn := silentWorker(t)
	go func() {
		for {
			frame, err := wireproto.DecodeFrame(serverConn)
			if err != nil {
				return
			}
			frames <- frame
			_ = wireproto.EncodeFrame(serverConn, wireproto.Frame{
				Protocol: wireproto.Schema, Op: wireproto.OpResult, ID: frame.ID,
				Response: &wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok"},
			})
		}
	}()
	var starts []workerKey
	manager.start = func(_ context.Context, key workerKey) (*workerClient, error) {
		starts = append(starts, key)
		return worker, nil
	}
	request := testEventRequest("PostToolUse")
	request.Root = t.TempDir()
	env := map[string]string{actorJudgeEnv: "claude", "ANTHROPIC_API_KEY": "synthetic-worker-key"}
	for range 3 {
		if _, err := manager.dispatchActor(t.Context(), request, env); err != nil {
			t.Fatal(err)
		}
	}
	if len(starts) != 1 {
		t.Fatalf("worker starts = %d, want one for every call", len(starts))
	}
	for want := uint64(1); want <= 3; want++ {
		frame := <-frames
		if frame.ID != want {
			t.Fatalf("request id = %d, want %d", frame.ID, want)
		}
		encoded, err := json.Marshal(frame)
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(encoded), "synthetic-worker-key") {
			t.Fatalf("the actor's key crossed the worker protocol: %s", encoded)
		}
	}
	command := workerCmd(starts[0], "/python")
	for _, want := range []string{actorJudgeEnv + "=claude", "ANTHROPIC_API_KEY=synthetic-worker-key"} {
		if !slices.Contains(command.Env, want) {
			t.Fatalf("worker environment lacks %q", want)
		}
	}
	rotated, err := makeActorWorkerKey(request, map[string]string{actorJudgeEnv: "claude", "ANTHROPIC_API_KEY": "rotated"})
	if err != nil {
		t.Fatal(err)
	}
	if rotated.id == starts[0].id {
		t.Fatal("a rotated key reused the worker holding the old one")
	}
	resident, err := makeWorkerKey(request)
	if err != nil {
		t.Fatal(err)
	}
	if resident.id == starts[0].id {
		t.Fatal("an actor shares a worker with a client that holds no key")
	}
}

func TestAnActorWorkerRetiresWhenIdleOrSuperseded(t *testing.T) {
	t.Parallel()
	now := time.Unix(1<<32, 0)
	root := t.TempDir()
	current := workerKey{id: "current", root: root, actor: true}
	for name, tc := range map[string]struct {
		key    workerKey
		idle   time.Duration
		retire bool
	}{
		"just used":                    {current, 0, false},
		"idle past the worker TTL":     {current, workerIdleTTL, true},
		"another root's actor":         {workerKey{id: "elsewhere", root: t.TempDir(), actor: true}, 0, false},
		"a keyless client on the root": {workerKey{id: "keyless", root: root}, 0, false},
		"superseded by a rotated key":  {workerKey{id: "rotated", root: root, actor: true}, 0, true},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			manager := mustWorkerManager(t)
			manager.now = func() time.Time { return now }
			manager.start = func(context.Context, workerKey) (*workerClient, error) {
				worker, _ := silentWorker(t)
				return worker, nil
			}
			entry := readyMember(t, manager, tc.key, 0)
			if tc.key.id != current.id {
				acquired, _, err := manager.acquire(t.Context(), current)
				if err != nil {
					t.Fatal(err)
				}
				manager.settleLoad(acquired)
				manager.release(acquired)
			}
			manager.mu.Lock()
			entry.lastUsed = now.Add(-tc.idle)
			manager.mu.Unlock()
			retired := manager.sweep(now)
			if got := slices.Contains(retired, entry.worker); got != tc.retire {
				t.Fatalf("retired = %v, want %v", got, tc.retire)
			}
		})
	}
}

func TestAnActorEnvironmentStaysOutOfHostOutput(t *testing.T) {
	t.Parallel()
	const planted = "planted-actor-key-5d1f"
	var log bytes.Buffer
	manager := newWorkerManager(daemonkit.Ctx{}, &log)
	manager.memory = func(*workerClient) (uint64, bool) { return 0, false }
	product := &hostProduct{manager: manager, hub: newNotificationHub(), monitor: testResourceMonitor(t, manager)}
	env := map[string]string{actorJudgeEnv: "claude", "ANTHROPIC_API_KEY": planted}
	request := testEventRequest("PostToolUse")
	request.Root = t.TempDir()
	manager.start = func(context.Context, workerKey) (*workerClient, error) { return nil, errors.New("spawn refused") }

	var output []string
	handle := func(op string, body []byte, pid int) {
		reply, err := product.Handle(t.Context(), daemonkit.Request{Op: op, Body: body, Caller: daemonkit.Caller{PID: pid}})
		output = append(output, string(reply.Body), fmt.Sprint(err))
	}
	body, err := json.Marshal(actorEventRequest{Request: request, Env: env})
	if err != nil {
		t.Fatal(err)
	}
	handle(opActorEvent, body, request.ClientPID)
	handle(opActorEvent, body, request.ClientPID+1)
	handle(opActorEvent, []byte(`{"request":{},"env":{"ANTHROPIC_API_KEY":"`+planted+`"},"stray":"`+planted+`"}`), request.ClientPID)
	handle(opActorEvent, []byte(`{"request":{},"env":{"ANTHROPIC_API_KEY":`+planted+`}}`), request.ClientPID)
	invalid := request
	invalid.Schema = 0
	malformed, err := json.Marshal(actorEventRequest{Request: invalid, Env: env})
	if err != nil {
		t.Fatal(err)
	}
	handle(opActorEvent, malformed, request.ClientPID)

	worker, serverConn := silentWorker(t)
	go func() {
		for {
			frame, err := wireproto.DecodeFrame(serverConn)
			if err != nil {
				return
			}
			_ = wireproto.EncodeFrame(serverConn, wireproto.Frame{
				Protocol: wireproto.Schema, Op: wireproto.OpResult, ID: frame.ID,
				Response: &wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok"},
			})
		}
	}()
	manager.start = func(context.Context, workerKey) (*workerClient, error) { return worker, nil }
	handle(opActorEvent, body, request.ClientPID)
	handle(opStatus, nil, 0)
	manager.sweep(time.Now().Add(2 * workerIdleTTL))

	if joined := log.String() + strings.Join(output, "\n"); strings.Contains(joined, planted) {
		t.Fatalf("the host showed an actor's key: %s", joined)
	}
}

func TestAnActorIdentityShowsNothingWithoutTheHostsSalt(t *testing.T) {
	t.Parallel()
	env := map[string]string{"ANTHROPIC_API_KEY": "synthetic-key"}
	first := actorIdentity("salt-a", env)
	if first == actorIdentity("salt-b", env) {
		t.Fatal("an actor's identity is computable from its keys alone")
	}
	if first != actorIdentity("salt-a", map[string]string{"ANTHROPIC_API_KEY": "synthetic-key"}) {
		t.Fatal("an actor's identity moved between calls")
	}
	if first == actorIdentity("salt-a", map[string]string{"ANTHROPIC_API_KEY": "rotated"}) {
		t.Fatal("a rotated key kept its identity")
	}
	if actorIdentity("salt-a", map[string]string{"A": `1" "B"="2`}) == actorIdentity("salt-a", map[string]string{"A": "1", "B": "2"}) {
		t.Fatal("two environments share an identity")
	}
}

func TestDispatchCarriesTheCallerDeadlineToTheWorker(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	frames := make(chan wireproto.Frame, 1)
	scriptedWorker(t, manager, func(conn net.Conn) {
		frame, err := wireproto.DecodeFrame(conn)
		if err != nil {
			return
		}
		frames <- frame
		_ = wireproto.EncodeFrame(conn, wireproto.Frame{
			Protocol: wireproto.Schema, Op: wireproto.OpResult, ID: frame.ID,
			Response: &wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok"},
		})
	})
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()

	request := testEventRequest("PreToolUse")
	request.Root = "/live"
	if _, err := manager.dispatch(ctx, request); err != nil {
		t.Fatal(err)
	}
	deadline, _ := ctx.Deadline()
	if got := (<-frames).Request.DeadlineUnixMS; got != deadline.UnixMilli() {
		t.Fatalf("frame deadline = %d, want the caller's %d", got, deadline.UnixMilli())
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
		"HOOKS_PROFILE=strict", "CAPT_HOOK_RUN_DIR=/old", "ORCA_TERMINAL_HANDLE=term-a",
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

func TestCerebrasKeyReachesHooksOnlyPerRequest(t *testing.T) {
	t.Parallel()
	base := workerBaseEnvironment([]string{"PATH=/bin", "CEREBRAS_API_KEY=csk-host"})
	if len(base) != 2 || base[0] != "PATH=/bin" || base[1] != "LANG=C" {
		t.Fatalf("base environment = %v", base)
	}
	if semantic := semanticWorkerEnvironment(map[string]string{"CEREBRAS_API_KEY": "csk-test"}); len(semantic) != 0 {
		t.Fatalf("semantic worker environment = %v", semantic)
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
		id: "abc", root: root, env: map[string]string{"HOOKS_PROFILE": "strict"},
	}, "/usr/bin/python3")
	if !cmd.Session {
		t.Fatal("worker spawn does not own its descendants")
	}
	if cmd.Path != "/usr/bin/python3" || cmd.Dir != filepath.Clean(os.TempDir()) ||
		len(cmd.Args) != 3 || cmd.Args[0] != "-P" || cmd.Args[1] != "-m" || cmd.Args[2] != "captain_hook.worker" {
		t.Fatalf("worker spawn = %q %q in %q", cmd.Path, cmd.Args, cmd.Dir)
	}
	seen := map[string]string{}
	for _, item := range cmd.Env {
		name, value, _ := strings.Cut(item, "=")
		seen[name] = value
	}
	// The whole inherited environment carries the user's secrets, so a failure names only what it asserts.
	if seen["LANG"] != "C" || seen["PATH"] == "" || seen["HOOKS_PROFILE"] != "strict" || seen["CAPT_HOOK_WORKER_ROOT"] != root {
		t.Fatalf("worker environment: LANG=%q PATH set=%t HOOKS_PROFILE=%q CAPT_HOOK_WORKER_ROOT=%q",
			seen["LANG"], seen["PATH"] != "", seen["HOOKS_PROFILE"], seen["CAPT_HOOK_WORKER_ROOT"])
	}
}

// TestWorkerDirSurvivesARootDeletedUnderTheSession pins the fallback: a chdir
// into a missing Dir surfaces as posix_spawn reporting the interpreter absent,
// which killed every dispatch from a session whose workspace had been reaped
// before it first needed a worker (observed 2026-09-02).
func TestWorkerDirSurvivesARootDeletedUnderTheSession(t *testing.T) {
	tmp := t.TempDir()
	t.Setenv("TMPDIR", tmp+"/")
	if dir := workerDir(filepath.Join(tmp, "reaped")); dir != tmp {
		t.Fatalf("workerDir = %q, want the clean temp dir %q for a missing root", dir, tmp)
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
		manager.entries[entry.key.member()] = entry
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

	if _, _, err := manager.acquire(t.Context(), workerKey{id: "one-past-the-bound", root: "/fresh"}); errors.Is(err, ErrWorkerCapacity) {
		t.Fatal("a full cache of idle workers refused admission instead of evicting")
	}
	if _, live := manager.entries["live-0/0"]; live {
		t.Fatal("the least recently used worker survived the bound")
	}
	if _, live := manager.entries["live-63/0"]; !live {
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

	_, _, err := manager.acquire(t.Context(), workerKey{id: "one-past-the-bound", root: "/fresh"})
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
		manager.entries[entry.key.member()] = entry
	}
	seed("warm", live, now.Add(-time.Minute), 0)
	seed("cold", live, now.Add(-2*workerIdleTTL), 0)
	seed("dead-root", dead, now, 0)
	seed("busy", live, now.Add(-2*workerIdleTTL), 1)

	manager.sweep(now)

	if _, kept := manager.entries["warm/0"]; !kept {
		t.Error("sweep retired a worker used a minute ago")
	}
	if _, kept := manager.entries["cold/0"]; kept {
		t.Error("sweep kept a worker idle past the TTL")
	}
	if _, kept := manager.entries["dead-root/0"]; kept {
		t.Error("sweep kept a worker whose root no longer exists")
	}
	if _, kept := manager.entries["busy/0"]; !kept {
		t.Error("sweep retired a worker with a dispatch in flight")
	}
}

func TestWorkerManagerSweepRetiresSparePoolMembersSooner(t *testing.T) {
	t.Parallel()
	now := time.Unix(1<<32, 0)
	live := t.TempDir()
	manager := mustWorkerManager(t)
	seed := func(shard int, lastUsed time.Time) {
		entry := &workerEntry{
			ready: make(chan struct{}), key: workerKey{id: "pool", root: live, shard: shard}, lastUsed: lastUsed,
		}
		close(entry.ready)
		manager.entries[entry.key.member()] = entry
	}
	seed(0, now.Add(-spareIdleTTL-2*time.Minute))
	seed(1, now.Add(-spareIdleTTL-time.Minute))
	seed(2, now.Add(-time.Minute))

	manager.sweep(now)

	if _, kept := manager.entries["pool/0"]; kept {
		t.Error("sweep kept a spare member idle past spareIdleTTL")
	}
	if _, kept := manager.entries["pool/1"]; kept {
		t.Error("sweep kept a spare member idle past spareIdleTTL")
	}
	if _, kept := manager.entries["pool/2"]; !kept {
		t.Error("sweep retired a member used a minute ago")
	}

	manager.sweep(now.Add(spareIdleTTL))

	if _, kept := manager.entries["pool/2"]; !kept {
		t.Error("sweep retired the pool's last member before workerIdleTTL")
	}
}

func TestWorkerManagerSweepRetiresAnIdleWorkerOverTheMemoryCeiling(t *testing.T) {
	t.Parallel()
	now := time.Unix(1<<32, 0)
	live := t.TempDir()
	manager := mustWorkerManager(t)
	resident := map[*workerClient]uint64{}
	manager.memory = func(worker *workerClient) (uint64, bool) {
		bytes, known := resident[worker]
		return bytes, known
	}
	seed := func(id string, lastUsed time.Time, inflight int, bytes uint64, known bool) {
		entry := &workerEntry{
			ready: make(chan struct{}), key: workerKey{id: id, root: live}, worker: &workerClient{},
			lastUsed: lastUsed, inflight: inflight,
		}
		close(entry.ready)
		if known {
			resident[entry.worker] = bytes
		}
		manager.entries[entry.key.member()] = entry
	}
	seed("bloated", now.Add(-bloatedIdleTTL), 0, workerMemoryCeiling+1, true)
	seed("at-ceiling", now.Add(-bloatedIdleTTL), 0, workerMemoryCeiling, true)
	seed("just-used", now.Add(-bloatedIdleTTL+time.Second), 0, 2*workerMemoryCeiling, true)
	seed("dispatching", now.Add(-workerIdleTTL/2), 1, 2*workerMemoryCeiling, true)
	seed("unread", now.Add(-workerIdleTTL/2), 0, 0, false)

	retired := manager.sweep(now)

	if len(retired) != 1 {
		t.Fatalf("sweep retired %d workers, want only the bloated one", len(retired))
	}
	for member, kept := range map[string]bool{
		"bloated/0": false, "at-ceiling/0": true, "just-used/0": true, "dispatching/0": true, "unread/0": true,
	} {
		if _, live := manager.entries[member]; live != kept {
			t.Errorf("after the sweep %s cached = %t, want %t", member, live, kept)
		}
	}
}

func TestSweepLogsEachRetirementWithItsTimeAndReason(t *testing.T) {
	now := time.Date(2026, 11, 3, 21, 0, 0, 0, time.UTC)
	live := t.TempDir()
	manager := mustWorkerManager(t)
	var log bytes.Buffer
	manager.logWriter = &log
	manager.memory = func(*workerClient) (uint64, bool) { return workerMemoryCeiling + 1<<20, true }
	seed := func(id string, shard int, root string, lastUsed time.Time) {
		entry := &workerEntry{
			ready: make(chan struct{}), key: workerKey{id: id, root: root, shard: shard}, worker: &workerClient{}, lastUsed: lastUsed,
		}
		close(entry.ready)
		manager.entries[entry.key.member()] = entry
	}
	seed("gone", 0, filepath.Join(live, "deleted"), now)
	seed("cold", 0, live, now.Add(-workerIdleTTL))
	seed("pool", 0, live, now)
	seed("pool", 1, live, now.Add(-spareIdleTTL))
	seed("large", 0, live, now.Add(-bloatedIdleTTL))

	manager.sweep(now)

	for _, want := range []string{
		"captain: 2026-11-03T21:00:00Z retire worker gone/0: root gone, idle 0s\n",
		"captain: 2026-11-03T21:00:00Z retire worker cold/0: idle, idle 30m0s\n",
		"captain: 2026-11-03T21:00:00Z retire worker pool/1: spare, idle 2m0s\n",
		"captain: 2026-11-03T21:00:00Z retire worker large/0: over limit at 513 MiB, idle 1m0s\n",
	} {
		if !strings.Contains(log.String(), want) {
			t.Errorf("sweep log lacks %q:\n%s", want, log.String())
		}
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
	manager.entries[entry.key.member()] = entry

	manager.release(entry)

	if _, cached := manager.entries[entry.key.member()]; cached {
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
	manager := newWorkerManager(daemonkit.Ctx{}, io.Discard)
	manager.memory = func(*workerClient) (uint64, bool) { return 0, false }
	return manager
}

func TestDispatchRunsOneAgentsEventsConcurrently(t *testing.T) {
	t.Parallel()
	const events = 7
	manager := mustWorkerManager(t)
	scriptedWorker(t, manager, func(conn net.Conn) {
		ids := make([]uint64, 0, events)
		for range events {
			frame, err := wireproto.DecodeFrame(conn)
			if err != nil {
				return
			}
			ids = append(ids, frame.ID)
		}
		for _, id := range ids {
			_ = wireproto.EncodeFrame(conn, wireproto.Frame{
				Protocol: wireproto.Schema, Op: wireproto.OpResult, ID: id,
				Response: &wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok"},
			})
		}
	})
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()

	errs := make(chan error, events)
	for range events {
		go func() {
			request := testEventRequest("PostToolUse")
			request.Root = "/live"
			request.PayloadRaw = `{"session_id":"session-a","agent_id":"agent-1"}`
			_, err := manager.dispatch(ctx, request)
			errs <- err
		}()
	}
	for range events {
		if err := <-errs; err != nil {
			t.Fatalf("dispatch = %v; the worker answers only once all %d events are in flight together", err, events)
		}
	}
}

func TestInstalledPythonOnlyLooksUpTheToolEnv(t *testing.T) {
	home, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("DAEMONKIT_HOME", home)
	t.Setenv("PATH", t.TempDir())
	if _, err := installedPython(); err == nil || !strings.Contains(err.Error(), wireproto.Remedy()) {
		t.Fatalf("installedPython without a tool env = %v, want the platform's remedy", err)
	}

	toolDir := filepath.Join(home, ".daemonkit", "tools", "capt-hook", Build)
	venvBin := filepath.Join(toolDir, "capt-hook", "bin")
	for _, dir := range []string{venvBin, filepath.Join(toolDir, "bin")} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	for _, file := range []string{filepath.Join(venvBin, "hook"), filepath.Join(venvBin, "python")} {
		if err := os.WriteFile(file, nil, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink(filepath.Join(venvBin, "hook"), filepath.Join(toolDir, "bin", "hook")); err != nil {
		t.Fatal(err)
	}
	if _, err := installedPython(); err == nil {
		t.Fatal("installedPython accepted a tool env with no install marker")
	}
	if err := os.WriteFile(filepath.Join(toolDir, ".installed"), nil, 0o600); err != nil {
		t.Fatal(err)
	}
	for _, stale := range []string{"capt-hook-cli/12.108.8", "capt-hook/0.0.1"} {
		if err := os.MkdirAll(filepath.Join(home, ".daemonkit", "tools", stale), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	var logged bytes.Buffer
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&logged, nil)))
	t.Cleanup(func() { slog.SetDefault(previous) })
	python, err := installedPython()
	if err != nil || python != filepath.Join(venvBin, "python") {
		t.Fatalf("installedPython = %q, %v; want %q", python, err, filepath.Join(venvBin, "python"))
	}
	if strings.Contains(logged.String(), "no install marker") {
		t.Fatalf("installedPython walked other tool envs and logged:\n%s", logged.String())
	}
}

func readyMember(t *testing.T, manager *workerManager, key workerKey, load int) *workerEntry {
	t.Helper()
	worker, _ := silentWorker(t)
	entry := &workerEntry{ready: make(chan struct{}), key: key, worker: worker, load: load}
	close(entry.ready)
	manager.mu.Lock()
	manager.entries[key.member()] = entry
	manager.mu.Unlock()
	return entry
}

func saturatedMember(t *testing.T, manager *workerManager, key workerKey) *workerEntry {
	t.Helper()
	entry := readyMember(t, manager, key, poolSpillLoad)
	entry.service = poolSpillWait
	return entry
}

func TestAcquirePacksOntoTheRecentMemberBelowTheSpillWait(t *testing.T) {
	t.Parallel()
	now := time.Unix(1<<32, 0)
	manager := mustWorkerManager(t)
	manager.poolSize = 3
	manager.now = func() time.Time { return now }
	manager.start = func(_ context.Context, key workerKey) (*workerClient, error) {
		t.Errorf("load below the spill wait started shard %d", key.shard)
		worker, _ := silentWorker(t)
		return worker, nil
	}
	recent := readyMember(t, manager, workerKey{id: "pool", root: "/live", shard: 0}, 3)
	recent.service = 200 * time.Millisecond
	recent.lastUsed = now.Add(-time.Second)
	spare := readyMember(t, manager, workerKey{id: "pool", root: "/live", shard: 1}, 0)
	spare.lastUsed = now.Add(-time.Minute)

	entry, _, err := manager.acquire(t.Context(), workerKey{id: "pool", root: "/live"})
	if err != nil {
		t.Fatal(err)
	}
	if entry != recent {
		t.Fatalf("acquire routed to shard %d, want the recent member while its queue is %s", entry.key.shard, recent.queueWait())
	}
	manager.wg.Wait()
}

func TestAcquireSpillsPastASaturatedMember(t *testing.T) {
	t.Parallel()
	now := time.Unix(1<<32, 0)
	manager := mustWorkerManager(t)
	manager.poolSize = 2
	manager.now = func() time.Time { return now }
	recent := saturatedMember(t, manager, workerKey{id: "pool", root: "/live", shard: 0})
	recent.lastUsed = now.Add(-time.Second)
	spare := readyMember(t, manager, workerKey{id: "pool", root: "/live", shard: 1}, 0)
	spare.lastUsed = now.Add(-time.Minute)

	entry, _, err := manager.acquire(t.Context(), workerKey{id: "pool", root: "/live"})
	if err != nil {
		t.Fatal(err)
	}
	if entry != spare {
		t.Fatalf("acquire stayed on shard %d behind a %s queue with a spare idle", entry.key.shard, recent.queueWait())
	}
}

func TestAcquireTakesTheShortestQueueWhenEveryMemberIsSaturated(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	manager.poolSize = 2
	longer := saturatedMember(t, manager, workerKey{id: "pool", root: "/live", shard: 0})
	longer.load = 2 * poolSpillLoad
	shorter := saturatedMember(t, manager, workerKey{id: "pool", root: "/live", shard: 1})

	entry, _, err := manager.acquire(t.Context(), workerKey{id: "pool", root: "/live"})
	if err != nil {
		t.Fatal(err)
	}
	if entry != shorter {
		t.Fatalf("acquire took shard %d, want the shorter queue on shard %d", entry.key.shard, shorter.key.shard)
	}
}

func TestALongDispatchAloneNeverSaturatesAMember(t *testing.T) {
	t.Parallel()
	member := &workerEntry{load: poolSpillLoad - 1, service: time.Minute}
	if member.saturated() {
		t.Fatalf("%d calls in flight read as saturated at %s", member.load, member.queueWait())
	}
	member.load = poolSpillLoad
	if !member.saturated() {
		t.Fatalf("%d calls in flight read as unsaturated at %s", member.load, member.queueWait())
	}
}

func TestQueueWaitChargesAnUnseededMemberTheServiceFloor(t *testing.T) {
	t.Parallel()
	unseeded := &workerEntry{load: 10}
	if unseeded.saturated() {
		t.Fatalf("%d calls at the floor read as saturated at %s", unseeded.load, unseeded.queueWait())
	}
	unseeded.load = 11
	if !unseeded.saturated() {
		t.Fatalf("%d calls at the floor read as unsaturated at %s", unseeded.load, unseeded.queueWait())
	}
}

func TestWorkerKeyIgnoresTheSession(t *testing.T) {
	t.Parallel()
	var keys []workerKey
	for _, payload := range []string{
		`{"session_id":"session-a","transcript_path":"/t/a.jsonl"}`,
		`{"session_id":"session-b","transcript_path":"/t/b.jsonl","agent_id":"agent-1"}`,
		`{"session_id":"session-c","agent_transcript_path":"/t/c/agent.jsonl"}`,
	} {
		request := testEventRequest("PreToolUse")
		request.Root = "/live"
		request.PayloadRaw = payload
		key, err := makeWorkerKey(request)
		if err != nil {
			t.Fatal(err)
		}
		keys = append(keys, key)
	}
	for _, key := range keys[1:] {
		if !reflect.DeepEqual(key, keys[0]) {
			t.Fatalf("worker key %+v differs from %+v by session alone", key, keys[0])
		}
	}
}

func TestSequentialSessionsLeaveASpareToRetire(t *testing.T) {
	t.Parallel()
	now := time.Unix(1<<32, 0)
	live := t.TempDir()
	manager := mustWorkerManager(t)
	manager.poolSize = 2
	manager.now = func() time.Time { return now }
	manager.start = func(_ context.Context, key workerKey) (*workerClient, error) {
		t.Errorf("sequential load started shard %d", key.shard)
		worker, _ := silentWorker(t)
		return worker, nil
	}
	spare := readyMember(t, manager, workerKey{id: "pool", root: live, shard: 0}, 0)
	recent := readyMember(t, manager, workerKey{id: "pool", root: live, shard: 1}, 0)
	manager.mu.Lock()
	spare.lastUsed = now.Add(-time.Minute)
	recent.lastUsed = now.Add(-time.Second)
	manager.mu.Unlock()

	for range 4 {
		entry, _, err := manager.acquire(t.Context(), workerKey{id: "pool", root: live})
		if err != nil {
			t.Fatal(err)
		}
		if entry != recent {
			t.Fatalf("sequential load moved to shard %d while shard 1 sat idle", entry.key.shard)
		}
		manager.settleLoad(entry)
		manager.release(entry)
	}
	manager.wg.Wait()
	manager.sweep(now.Add(spareIdleTTL))

	if _, kept := manager.entries[spare.key.member()]; kept {
		t.Error("sequential load kept the spare member warm past spareIdleTTL")
	}
	if _, kept := manager.entries[recent.key.member()]; !kept {
		t.Error("sweep retired the member serving the load")
	}
}

func TestBusyPoolGrowsOneMemberAtATime(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	manager.poolSize = 2
	shards := make(chan int, maxLiveWorkers)
	manager.start = func(_ context.Context, key workerKey) (*workerClient, error) {
		shards <- key.shard
		worker, _ := silentWorker(t)
		return worker, nil
	}
	key := workerKey{id: "pool", root: "/live"}
	first := saturatedMember(t, manager, workerKey{id: "pool", root: "/live", shard: 0})

	entry, _, err := manager.acquire(t.Context(), key)
	if err != nil {
		t.Fatal(err)
	}
	if entry != first {
		t.Fatal("a busy pool made the caller wait on the member it started instead of taking the one that was up")
	}
	if shard := <-shards; shard != 1 {
		t.Fatalf("growth started shard %d, want 1", shard)
	}
	manager.wg.Wait()
	second := cachedEntry(manager, workerKey{id: "pool", shard: 1}.member())
	if second == nil || !second.idle() {
		t.Fatalf("the grown member = %+v, want cached idle", second)
	}

	manager.mu.Lock()
	second.load = poolSpillLoad
	second.service = poolSpillWait
	manager.mu.Unlock()
	if _, _, err := manager.acquire(t.Context(), key); err != nil {
		t.Fatal(err)
	}
	select {
	case shard := <-shards:
		t.Fatalf("a pool at its bound started shard %d", shard)
	default:
	}
}

func TestAPoolMemberStillStartingHoldsGrowth(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	manager.poolSize = 3
	worker, _ := silentWorker(t)
	started, release := stallStart(manager, worker, nil)
	defer close(release)
	saturatedMember(t, manager, workerKey{id: "pool", root: "/live", shard: 0})

	if _, _, err := manager.acquire(t.Context(), workerKey{id: "pool", root: "/live"}); err != nil {
		t.Fatal(err)
	}
	<-started
	if _, _, err := manager.acquire(t.Context(), workerKey{id: "pool", root: "/live"}); err != nil {
		t.Fatal(err)
	}
	manager.mu.Lock()
	size := manager.poolLocked("pool").size
	manager.mu.Unlock()
	if size != 2 {
		t.Fatalf("pool size = %d, want 2: a second start began while the first was still up", size)
	}
}

func TestAnEphemeralPoolNeverGrows(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	manager.poolSize = 2
	manager.start = func(context.Context, workerKey) (*workerClient, error) {
		t.Error("an ephemeral root grew a pool member that release would retire at once")
		worker, _ := silentWorker(t)
		return worker, nil
	}
	key := workerKey{id: "scratch", root: "/tmp/scratch"}
	entry := saturatedMember(t, manager, key)
	entry.ephemeral = true

	if _, _, err := manager.acquire(t.Context(), key); err != nil {
		t.Fatal(err)
	}
	manager.wg.Wait()
}

func TestWorkersPerRootReadsTheHostEnvironment(t *testing.T) {
	t.Setenv("CAPT_HOOK_WORKERS_PER_ROOT", "3")
	if got := workersPerRoot(); got != 3 {
		t.Fatalf("workersPerRoot = %d, want 3", got)
	}
	t.Setenv("CAPT_HOOK_WORKERS_PER_ROOT", "")
	if got := workersPerRoot(); got < 1 || got > maxWorkersPerRoot {
		t.Fatalf("workersPerRoot = %d, want within [1, %d]", got, maxWorkersPerRoot)
	}
}

func TestHostFactProducerReadsTheHostEnvironment(t *testing.T) {
	tests := []struct {
		name, reuse, want string
	}{
		{"unset", "", "capt-hook"},
		{"off", "0", ""},
		{"on", "1", "capt-hook"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			t.Setenv("CAPT_HOOK_PREPARED_FACT_REUSE", test.reuse)
			if got := hostFactProducer(); got != test.want {
				t.Fatalf("hostFactProducer = %q, want %q", got, test.want)
			}
		})
	}
}

func TestHostFactProducerIsStableAcrossBuilds(t *testing.T) {
	t.Setenv("CAPT_HOOK_PREPARED_FACT_REUSE", "")
	previous := Build
	t.Cleanup(func() { Build = previous })
	for _, build := range []string{"0.0.0", "12.106.13", "12.106.14"} {
		Build = build
		if got := hostFactProducer(); got != "capt-hook" {
			t.Fatalf("hostFactProducer for build %q = %q, want capt-hook", build, got)
		}
	}
}

func TestWorkerCmdCarriesTheShard(t *testing.T) {
	t.Parallel()
	cmd := workerCmd(workerKey{id: "abc", root: t.TempDir(), shard: 2}, "/usr/bin/python3")
	for _, item := range cmd.Env {
		if item == "CAPT_HOOK_WORKER_SHARD=2" {
			return
		}
	}
	t.Fatal("worker environment carries no CAPT_HOOK_WORKER_SHARD")
}

func TestObserveSmoothsTheServiceTimePerCallShare(t *testing.T) {
	t.Parallel()
	entry := &workerEntry{}
	entry.observe(1, 800*time.Millisecond)
	if entry.service != 800*time.Millisecond {
		t.Fatalf("first sample seeded service = %s, want 800ms", entry.service)
	}
	entry.observe(4, 4*time.Second)
	if entry.service != 850*time.Millisecond {
		t.Fatalf("service after a 1s share = %s, want 850ms", entry.service)
	}
}

func TestDispatchShedsWhenTheQueueOutlastsTheDeadline(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	frames := make(chan wireproto.Frame, 2)
	worker, serverConn := silentWorker(t)
	go func() {
		for {
			frame, err := wireproto.DecodeFrame(serverConn)
			if err != nil {
				return
			}
			frames <- frame
			_ = wireproto.EncodeFrame(serverConn, wireproto.Frame{
				Protocol: wireproto.Schema, Op: wireproto.OpResult, ID: frame.ID,
				Response: &wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: "served"},
			})
		}
	}()
	request := testEventRequest("PreToolUse")
	request.Root = "/live"
	key, err := makeWorkerKey(request)
	if err != nil {
		t.Fatal(err)
	}
	entry := &workerEntry{ready: make(chan struct{}), key: key, worker: worker, load: 5, service: time.Second}
	worker.setOnSettle(func() { manager.settleLoad(entry) })
	close(entry.ready)
	manager.entries[key.member()] = entry

	short, cancelShort := context.WithTimeout(t.Context(), 2*time.Second)
	defer cancelShort()
	shed, err := manager.dispatch(short, request)
	if err != nil {
		t.Fatal(err)
	}
	if shed.Exit != 0 || shed.Stdout != "" || !strings.Contains(shed.Stderr, "no verdict") {
		t.Fatalf("shed reply = %+v, want an exit-0 no-verdict reply naming the queue", shed)
	}
	select {
	case frame := <-frames:
		t.Fatalf("a shed event reached the worker as frame %d", frame.ID)
	default:
	}
	manager.mu.Lock()
	load := entry.load
	manager.mu.Unlock()
	if load != 5 {
		t.Fatalf("load after a shed = %d, want the 5 the shed left alone", load)
	}

	long, cancelLong := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancelLong()
	served, err := manager.dispatch(long, request)
	if err != nil || served.Stdout != "served" {
		t.Fatalf("dispatch within budget = %+v, %v; want the worker's reply", served, err)
	}
	<-frames
}

func TestDispatchNeverShedsAMandatoryEvent(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	frames := make(chan wireproto.Frame, 1)
	worker, serverConn := silentWorker(t)
	go func() {
		for {
			frame, err := wireproto.DecodeFrame(serverConn)
			if err != nil {
				return
			}
			frames <- frame
			_ = wireproto.EncodeFrame(serverConn, wireproto.Frame{
				Protocol: wireproto.Schema, Op: wireproto.OpResult, ID: frame.ID,
				Response: &wireproto.EventResponse{
					Schema: wireproto.Schema, Status: "ok", Stdout: "served", Guard: wireproto.GuardCompleted,
				},
			})
		}
	}()
	request := testEventRequest("PreToolUse")
	request.Root = "/live"
	request.Mandatory = true
	key, err := makeWorkerKey(request)
	if err != nil {
		t.Fatal(err)
	}
	entry := &workerEntry{ready: make(chan struct{}), key: key, worker: worker, load: 5, service: time.Second}
	worker.setOnSettle(func() { manager.settleLoad(entry) })
	close(entry.ready)
	manager.entries[key.member()] = entry

	short, cancelShort := context.WithTimeout(t.Context(), 2*time.Second)
	defer cancelShort()
	served, err := manager.dispatch(short, request)
	if err != nil || served.Stdout != "served" || served.Guard != wireproto.GuardCompleted {
		t.Fatalf("mandatory dispatch past the queue estimate = %+v, %v; want the worker's reply", served, err)
	}
	frame := <-frames
	if frame.Request == nil || !frame.Request.Mandatory {
		t.Fatalf("the worker received %+v, want the mandatory flag on the wire", frame.Request)
	}
}

func TestShedResponseCarriesNoGuardField(t *testing.T) {
	t.Parallel()
	encoded, err := wireproto.Marshal(shedResponse(3, 4*time.Second, time.Second))
	if err != nil || bytes.Contains(encoded, []byte("guard")) {
		t.Fatalf("shed reply = %s, %v; want no guard field for a pre-guard client to refuse", encoded, err)
	}
}

func replyingWorker(t *testing.T, manager *workerManager, entry *workerEntry, hold <-chan struct{}) *workerClient {
	t.Helper()
	worker, serverConn := silentWorker(t)
	worker.setOnSettle(func() { manager.settleLoad(entry) })
	go func() {
		for {
			frame, err := wireproto.DecodeFrame(serverConn)
			if err != nil {
				return
			}
			if hold != nil {
				<-hold
			}
			_ = wireproto.EncodeFrame(serverConn, wireproto.Frame{
				Protocol: wireproto.Schema, Op: wireproto.OpResult, ID: frame.ID,
				Response: &wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok"},
			})
		}
	}()
	return worker
}

type heldReplyDeadline struct {
	context.Context
	done chan struct{}
	once sync.Once
}

func newHeldReplyDeadline(parent context.Context) *heldReplyDeadline {
	return &heldReplyDeadline{Context: parent, done: make(chan struct{})}
}

func (c *heldReplyDeadline) expire() { c.once.Do(func() { close(c.done) }) }

func (c *heldReplyDeadline) Done() <-chan struct{} { return c.done }

func (c *heldReplyDeadline) Err() error {
	select {
	case <-c.done:
		return context.DeadlineExceeded
	default:
		return nil
	}
}

func drainExpiringOnEvent(conn net.Conn, held *heldReplyDeadline) {
	go func() {
		for {
			frame, err := wireproto.DecodeFrame(conn)
			if err != nil {
				return
			}
			if frame.Op == wireproto.OpEvent {
				held.expire()
			}
		}
	}()
}

func TestTimedOutWorkStaysCountedForAdmission(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	worker, serverConn := silentWorker(t)
	held := newHeldReplyDeadline(t.Context())
	drainExpiringOnEvent(serverConn, held)
	request := testEventRequest("PreToolUse")
	request.Root = "/live"
	key, err := makeWorkerKey(request)
	if err != nil {
		t.Fatal(err)
	}
	entry := &workerEntry{ready: make(chan struct{}), key: key, worker: worker, service: 10 * time.Second}
	worker.setOnSettle(func() { manager.settleLoad(entry) })
	close(entry.ready)
	manager.entries[key.member()] = entry

	if _, err := manager.dispatch(held, request); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("first dispatch = %v, want a timeout while the worker holds the reply", err)
	}

	manager.mu.Lock()
	load, inflight := entry.load, entry.inflight
	manager.mu.Unlock()
	if load != 1 {
		t.Fatalf("load after a timed-out op = %d, want 1: its work still occupies the interpreter", load)
	}
	if inflight != 0 {
		t.Fatalf("inflight after the caller left = %d, want 0", inflight)
	}

	medium, cancelMedium := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancelMedium()
	shed, err := manager.dispatch(medium, request)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(shed.Stderr, "no verdict") {
		t.Fatalf("second dispatch = %+v, want a shed: the timed-out op still occupies the member", shed)
	}
}

func TestConcurrentAdmissionsNeverShedAnEmptyWorker(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	request := testEventRequest("PreToolUse")
	request.Root = "/live"
	key, err := makeWorkerKey(request)
	if err != nil {
		t.Fatal(err)
	}
	for round := range 50 {
		entry := &workerEntry{ready: make(chan struct{}), key: key, service: 10 * time.Second}
		entry.worker = replyingWorker(t, manager, entry, nil)
		close(entry.ready)
		manager.mu.Lock()
		manager.entries[key.member()] = entry
		manager.mu.Unlock()

		const callers = 2
		start := make(chan struct{})
		served := make(chan bool, callers)
		for range callers {
			go func() {
				<-start
				ctx, cancel := context.WithTimeout(t.Context(), 8*time.Second)
				defer cancel()
				resp, err := manager.dispatch(ctx, request)
				served <- err == nil && !strings.Contains(resp.Stderr, "no verdict")
			}()
		}
		close(start)
		count := 0
		for range callers {
			if <-served {
				count++
			}
		}
		if count == 0 {
			t.Fatalf("round %d: both concurrent callers shed an empty worker", round)
		}
		manager.mu.Lock()
		load := entry.load
		delete(manager.entries, key.member())
		manager.mu.Unlock()
		if load != 0 {
			t.Fatalf("round %d: load after every caller settled = %d, want 0", round, load)
		}
	}
}

func TestAGrownMemberSurvivesAnImmediateSweep(t *testing.T) {
	t.Parallel()
	root, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	manager := mustWorkerManager(t)
	worker, _ := silentWorker(t)
	manager.start = func(context.Context, workerKey) (*workerClient, error) { return worker, nil }

	manager.mu.Lock()
	entry, _ := manager.startMemberLocked(workerKey{id: "pool", root: root}, 0, time.Time{})
	manager.mu.Unlock()
	<-entry.ready
	manager.wg.Wait()

	if entry.lastUsed.IsZero() {
		t.Fatal("startMemberLocked left lastUsed zero, so the sweep retires the member before its first request")
	}
	for _, w := range manager.sweep(time.Now()) {
		_ = w
	}
	if cachedEntry(manager, entry.key.member()) == nil {
		t.Fatal("the idle sweep retired a freshly grown member before its first request")
	}
}

type scriptedReply struct {
	advance  time.Duration
	before   func()
	response wireproto.EventResponse
}

func coldMember(t *testing.T, manager *workerManager, clock *fakeClock) (*workerEntry, wireproto.EventRequest, chan scriptedReply) {
	t.Helper()
	request := testEventRequest("PreToolUse")
	request.Root = "/live"
	key, err := makeWorkerKey(request)
	if err != nil {
		t.Fatal(err)
	}
	entry := &workerEntry{ready: make(chan struct{}), key: key}
	worker, serverConn := silentWorker(t)
	worker.setOnSettle(func() { manager.settleLoad(entry) })
	entry.worker = worker
	close(entry.ready)
	manager.entries[key.member()] = entry
	replies := make(chan scriptedReply, 8)
	go func() {
		for {
			frame, err := wireproto.DecodeFrame(serverConn)
			if err != nil {
				return
			}
			reply := <-replies
			if reply.before != nil {
				reply.before()
			}
			clock.Advance(reply.advance)
			_ = wireproto.EncodeFrame(serverConn, wireproto.Frame{
				Protocol: wireproto.Schema, Op: wireproto.OpResult, ID: frame.ID, Response: &reply.response,
			})
		}
	}()
	return entry, request, replies
}

func serviceOf(manager *workerManager, entry *workerEntry) time.Duration {
	manager.mu.Lock()
	defer manager.mu.Unlock()
	return entry.service
}

func TestColdDispatchesDoNotSeedTheServiceTime(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	clock := &fakeClock{now: time.Now()}
	manager.now = clock.Now
	entry, request, replies := coldMember(t, manager, clock)
	served := wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: "served"}
	dispatch := func(reply scriptedReply) wireproto.EventResponse {
		t.Helper()
		ctx, cancel := context.WithDeadline(t.Context(), clock.Now().Add(30*time.Second))
		defer cancel()
		replies <- reply
		response, err := manager.dispatch(ctx, request)
		if err != nil {
			t.Fatal(err)
		}
		return response
	}

	dispatch(scriptedReply{advance: 15 * time.Second, response: served})
	if got := serviceOf(manager, entry); got != 0 {
		t.Fatalf("service after the member's first dispatch = %s, want none: it paid the worker's imports", got)
	}
	warmup := served
	warmup.Warmup = true
	dispatch(scriptedReply{advance: 9 * time.Second, response: warmup})
	if got := serviceOf(manager, entry); got != 0 {
		t.Fatalf("service after a warm-up dispatch = %s, want none: the worker reported a one-time load", got)
	}
	dispatch(scriptedReply{advance: time.Second, response: served})
	if got := serviceOf(manager, entry); got != time.Second {
		t.Fatalf("service after the first warm dispatch = %s, want 1s", got)
	}

	manager.mu.Lock()
	entry.load = 3
	manager.mu.Unlock()
	response := dispatch(scriptedReply{advance: time.Second, response: served})
	if response.Stdout != "served" || strings.Contains(response.Stderr, "no verdict") {
		t.Fatalf("fourth event on a warm member = %+v, want it admitted behind 3 warm events", response)
	}
}

func TestADispatchOverlappingASnapshotOwnerStartupIsNotServiceTime(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	clock := &fakeClock{now: time.Now()}
	manager.now = clock.Now
	entry, request, replies := coldMember(t, manager, clock)
	manager.mu.Lock()
	entry.served = 1
	manager.mu.Unlock()
	served := wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok"}
	dispatch := func(reply scriptedReply) {
		t.Helper()
		ctx, cancel := context.WithDeadline(t.Context(), clock.Now().Add(30*time.Second))
		defer cancel()
		replies <- reply
		if _, err := manager.dispatch(ctx, request); err != nil {
			t.Fatal(err)
		}
	}
	snapshotService := func(start func(context.Context) (*snapshotOwner, error)) *snapshotService {
		service, err := newSnapshotService(manager)
		if err != nil {
			t.Fatal(err)
		}
		service.start = start
		return service
	}

	entered, release := make(chan struct{}), make(chan struct{})
	inFlight := snapshotService(func(ctx context.Context) (*snapshotOwner, error) {
		close(entered)
		<-release
		return &snapshotOwner{}, nil
	})
	manager.snapshots = inFlight
	got := make(chan error, 1)
	go func() { _, err := inFlight.get(context.Background()); got <- err }()
	<-entered
	dispatch(scriptedReply{advance: 12 * time.Second, response: served})
	close(release)
	if err := <-got; err != nil {
		t.Fatal(err)
	}
	if service := serviceOf(manager, entry); service != 0 {
		t.Fatalf("service after a dispatch admitted during an owner startup = %s, want none", service)
	}

	completed := snapshotService(func(context.Context) (*snapshotOwner, error) { return &snapshotOwner{}, nil })
	manager.snapshots = completed
	dispatch(scriptedReply{advance: 8 * time.Second, response: served, before: func() {
		if _, err := completed.get(context.Background()); err != nil {
			t.Error(err)
		}
	}})
	if service := serviceOf(manager, entry); service != 0 {
		t.Fatalf("service after a dispatch that saw an owner start and finish = %s, want none", service)
	}

	dispatch(scriptedReply{advance: time.Second, response: served})
	if service := serviceOf(manager, entry); service != time.Second {
		t.Fatalf("service after a dispatch with the owner settled = %s, want 1s", service)
	}
}
