package hookd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"net"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
	"github.com/yasyf/daemonkit"
)

const (
	testClaudePID = 100
	testShellPID  = 101
	testHookPID   = 102
	testHogPID    = 200

	testTranscript = "/transcripts/s1.jsonl"
)

var testResourceEnv = map[string]string{
	"HOOKS_PERFORMANCE_MIN_RUNTIME_SECONDS":    "30",
	"HOOKS_PERFORMANCE_SUSTAIN_SECONDS":        "30",
	"HOOKS_PERFORMANCE_GRACE_SECONDS":          "30",
	"HOOKS_PERFORMANCE_ESCALATE_AFTER_SECONDS": "30",
}

type dispatchRecord struct {
	request   wireproto.EventRequest
	payload   map[string]any
	host      bool
	remaining time.Duration
}

func (r dispatchRecord) stage() string {
	stage, _ := r.payload["stage"].(string)
	return stage
}

func (r dispatchRecord) process() map[string]any {
	process, _ := r.payload["process"].(map[string]any)
	return process
}

type hogRate struct {
	cpu  float64
	disk float64
}

type monitorHarness struct {
	t       *testing.T
	clock   *fakeClock
	source  *fakeProcSource
	manager *workerManager
	monitor *resourceMonitor
	root    string

	mu         sync.Mutex
	dispatched []dispatchRecord
	respond    func(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error)
	hogs       map[int]hogRate
	usage      map[int]procUsage
}

func proceedResponse(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error) {
	return wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: resourceProceedAck + "\n"}, nil
}

func newMonitorHarness(t *testing.T, daemonEnv map[string]string) *monitorHarness {
	t.Helper()
	clock := &fakeClock{now: time.Unix(1_700_000_000, 0)}
	manager := mustWorkerManager(t)
	manager.now = clock.Now
	settings, err := wireproto.ParseResourceSettings(daemonEnv)
	if err != nil {
		t.Fatal(err)
	}
	source := newFakeProcSource()
	start := clock.Now().Unix()
	source.add(procRow{PID: testClaudePID, PPID: 1, PGID: testClaudePID, StartUnix: start - 600, Comm: "claude"}, "claude")
	source.add(procRow{PID: testShellPID, PPID: testClaudePID, PGID: testShellPID, StartUnix: start - 1, Comm: "zsh"}, "zsh", "-c", "hook")
	source.add(procRow{PID: testHookPID, PPID: testShellPID, PGID: testShellPID, StartUnix: start - 1, Comm: "hook"}, "hook")
	h := &monitorHarness{
		t: t, clock: clock, source: source, manager: manager, root: t.TempDir(),
		monitor: newResourceMonitor(manager, source, settings),
		respond: proceedResponse, hogs: make(map[int]hogRate), usage: make(map[int]procUsage),
	}
	h.monitor.dispatch = h.record
	t.Cleanup(func() { closeManager(t, manager) })
	return h
}

func (h *monitorHarness) record(ctx context.Context, request wireproto.EventRequest) (wireproto.EventResponse, error) {
	var payload map[string]any
	if err := json.Unmarshal([]byte(request.PayloadRaw), &payload); err != nil {
		h.t.Errorf("payload %q: %v", request.PayloadRaw, err)
	}
	deadline, ok := ctx.Deadline()
	if !ok {
		h.t.Error("a host dispatch carried no deadline")
	}
	h.mu.Lock()
	h.dispatched = append(h.dispatched, dispatchRecord{
		request: request, payload: payload, host: isHostDispatch(ctx), remaining: time.Until(deadline),
	})
	respond := h.respond
	h.mu.Unlock()
	return respond(ctx, request)
}

func (h *monitorHarness) sessionRequest(event, session string, env map[string]string) wireproto.EventRequest {
	request := testEventRequest(event)
	request.Root, request.CWD = h.root, h.root
	request.Env = maps.Clone(testResourceEnv)
	maps.Copy(request.Env, env)
	request.ClientPID, request.ClientPPID = testHookPID, testShellPID
	payload, err := json.Marshal(map[string]string{
		"hook_event_name": event, "session_id": session, "transcript_path": testTranscript,
	})
	if err != nil {
		h.t.Fatal(err)
	}
	request.PayloadRaw = string(payload)
	return request
}

func (h *monitorHarness) register() {
	h.monitor.observe(h.sessionRequest("PreToolUse", "s1", nil), testHookPID)
	if h.sessions() != 1 {
		h.t.Fatal("the session did not register")
	}
}

func (h *monitorHarness) sessions() int {
	h.monitor.mu.Lock()
	defer h.monitor.mu.Unlock()
	return len(h.monitor.sessions)
}

func (h *monitorHarness) spawn(pid, ppid int, rate hogRate, argv ...string) {
	h.source.add(procRow{PID: pid, PPID: ppid, PGID: pid, StartUnix: h.clock.Now().Unix(), Comm: argv[0]}, argv...)
	h.hogs[pid] = rate
	h.usage[pid] = procUsage{CPUKnown: true, DiskKnown: rate.disk != 0}
	h.source.setUsage(pid, h.usage[pid])
}

func (h *monitorHarness) step() {
	h.tickOnly()
	h.settle()
}

func (h *monitorHarness) tickOnly() {
	h.t.Helper()
	interval := h.monitor.settings.SampleInterval
	h.clock.Advance(interval)
	for pid, rate := range h.hogs {
		usage := h.usage[pid]
		usage.CPUSeconds += rate.cpu * interval.Seconds()
		usage.DiskBytes += uint64(rate.disk * interval.Seconds())
		h.usage[pid] = usage
		h.source.setUsage(pid, usage)
	}
	done := make(chan struct{})
	go func() {
		h.monitor.tick(h.clock.Now())
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		h.t.Fatal("tick blocked on a stage in flight")
	}
}

func (h *monitorHarness) inflight() int {
	h.monitor.mu.Lock()
	defer h.monitor.mu.Unlock()
	count := 0
	for _, session := range h.monitor.sessions {
		if session.inflight {
			count++
		}
	}
	return count
}

func (h *monitorHarness) settle() {
	h.t.Helper()
	h.settleTo(0)
}

func (h *monitorHarness) settleTo(n int) {
	h.t.Helper()
	h.waitFor(func() bool { return h.inflight() == n }, "stages in flight never settled")
}

func (h *monitorHarness) awaitRecords(n int) {
	h.t.Helper()
	h.waitFor(func() bool { return len(h.records()) >= n }, "a started stage never dispatched")
}

func (h *monitorHarness) waitFor(condition func() bool, failure string) {
	h.t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !condition() {
		if time.Now().After(deadline) {
			h.t.Fatal(failure)
		}
		time.Sleep(time.Millisecond)
	}
}

func (h *monitorHarness) steps(n int) {
	for range n {
		h.step()
	}
}

func (h *monitorHarness) stages() []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	stages := make([]string, 0, len(h.dispatched))
	for _, record := range h.dispatched {
		stages = append(stages, record.stage())
	}
	return stages
}

func (h *monitorHarness) records() []dispatchRecord {
	h.mu.Lock()
	defer h.mu.Unlock()
	return slices.Clone(h.dispatched)
}

func TestMonitorWarnsJudgesThenEscalatesOnce(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	h.register()
	h.spawn(testHogPID, testClaudePID, hogRate{cpu: 1}, "yes")
	h.steps(2)
	if got := h.stages(); len(got) != 0 {
		t.Fatalf("dispatched %v before the sustain window filled", got)
	}
	h.step()
	if got := h.stages(); !slices.Equal(got, []string{"warn"}) {
		t.Fatalf("stages after the sustain window = %v", got)
	}
	h.step()
	if got := h.stages(); len(got) != 1 {
		t.Fatalf("stages inside the grace period = %v", got)
	}
	h.step()
	if got := h.stages(); !slices.Equal(got, []string{"warn", "judge"}) {
		t.Fatalf("stages after the grace period = %v", got)
	}
	h.step()
	if got := h.stages(); len(got) != 2 {
		t.Fatalf("stages before escalate_after = %v", got)
	}
	h.steps(6)
	if got := h.stages(); !slices.Equal(got, []string{"warn", "judge", "escalate"}) {
		t.Fatalf("stages after escalation = %v; want exactly three dispatches ever", got)
	}

	warn := h.records()[0]
	request := warn.request
	if request.Schema != wireproto.Schema || request.Event != resourceEvent || request.Mandatory ||
		request.ClientPID != os.Getpid() || request.ClientPPID != testClaudePID ||
		request.Root != h.root || request.CWD != h.root || request.Env["HOOKS_PERFORMANCE_GRACE_SECONDS"] != "30" {
		t.Fatalf("warn request = %+v", request)
	}
	if err := request.Validate(); err != nil {
		t.Fatalf("synthetic request does not validate: %v", err)
	}
	if !warn.host || warn.remaining <= 0 || warn.remaining > resourceWarnDeadline {
		t.Fatalf("warn dispatch host=%t remaining=%s", warn.host, warn.remaining)
	}
	if judge := h.records()[1]; judge.remaining <= resourceWarnDeadline || judge.remaining > resourceJudgeDeadline {
		t.Fatalf("judge deadline remaining = %s, want up to %s", judge.remaining, resourceJudgeDeadline)
	}

	payload := warn.payload
	if payload["hook_event_name"] != resourceEvent || payload["session_id"] != "s1" ||
		payload["transcript_path"] != testTranscript || payload["cwd"] != h.root || payload["stage"] != "warn" ||
		payload["claude_pid"] != float64(testClaudePID) || payload["claude_start_unix"] != float64(h.source.rows[testClaudePID].StartUnix) {
		t.Fatalf("warn payload = %v", payload)
	}
	if _, present := payload["agent_id"]; present {
		t.Fatal("payload carries agent_id, which would move worker affinity")
	}
	process := warn.process()
	if process["pid"] != float64(testHogPID) || process["ppid"] != float64(testClaudePID) || process["pgid"] != float64(testHogPID) ||
		process["comm"] != "yes" || process["runtime_s"] != float64(45) || process["cpu_fraction"] != float64(1) ||
		process["start_usec"] != float64(0) {
		t.Fatalf("warn process = %v", process)
	}
	if argv, _ := process["argv"].([]any); len(argv) != 1 || argv[0] != "yes" {
		t.Fatalf("warn argv = %v", process["argv"])
	}
	if _, present := process["disk_bps"]; present {
		t.Fatal("disk_bps was emitted for a child whose disk usage is unknown")
	}
	if _, present := process["cwd"]; present {
		t.Fatal("cwd was emitted for a child whose cwd is unknown")
	}
	ancestry, _ := process["ancestry"].([]any)
	if len(ancestry) != 1 || ancestry[0].(map[string]any)["pid"] != float64(testClaudePID) || ancestry[0].(map[string]any)["comm"] != "claude" {
		t.Fatalf("warn ancestry = %v", process["ancestry"])
	}
	metrics, _ := payload["metrics"].(map[string]any)
	if metrics["cpu"] != true || metrics["disk"] != false {
		t.Fatalf("warn metrics = %v", metrics)
	}
}

func TestMonitorDiskRateTripsTheWindowAndReportsBothRates(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	h.register()
	h.spawn(testHogPID, testClaudePID, hogRate{disk: 100 << 20}, "cp")
	h.source.cwds[testHogPID] = "/scratch"
	h.steps(3)
	records := h.records()
	if len(records) != 1 || records[0].stage() != "warn" {
		t.Fatalf("stages = %v", h.stages())
	}
	process := records[0].process()
	metrics := records[0].payload["metrics"].(map[string]any)
	if process["disk_bps"] != float64(100<<20) || process["cpu_fraction"] != float64(0) || process["cwd"] != "/scratch" ||
		metrics["cpu"] != true || metrics["disk"] != true {
		t.Fatalf("disk warn process = %v metrics = %v", process, metrics)
	}
}

func TestMonitorExcludesAChildWithoutAProceedAck(t *testing.T) {
	t.Parallel()
	blockEnvelope := `{"decision":"block","reason":"not owned"}`
	for name, tc := range map[string]struct {
		respond    func(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error)
		dispatches int
	}{
		"empty stdout": {func(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error) {
			return wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok"}, nil
		}, 1},
		"block envelope": {func(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error) {
			return wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: blockEnvelope}, nil
		}, 1},
		"shed": {func(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error) {
			return shedResponse(3, time.Minute, time.Second), nil
		}, 1},
		"worker error": {func(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error) {
			return wireproto.EventResponse{}, errors.New("worker pipe closed")
		}, 1},
		"admission paused": {func(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error) {
			return wireproto.EventResponse{}, errWorkerAdmissionPaused
		}, 1},
		"manager closed": {func(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error) {
			return wireproto.EventResponse{}, errWorkerManagerClosed
		}, 1},
		"capacity": {func(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error) {
			return wireproto.EventResponse{}, ErrWorkerCapacity
		}, 1},
		"timeout": {func(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error) {
			return wireproto.EventResponse{}, context.DeadlineExceeded
		}, 1},
		"deny": {func(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error) {
			return wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: `{"decision":"deny"}`}, nil
		}, 1},
		"extra numeric key": {func(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error) {
			return wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: `{"decision":"proceed","x":1}`}, nil
		}, 1},
		"extra key": {func(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error) {
			return wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: `{"decision":"proceed","systemMessage":"hi"}`}, nil
		}, 1},
		"truncated json": {func(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error) {
			return wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: `{"decision":"proceed"`}, nil
		}, 1},
		"failed exit": {func(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error) {
			return wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Exit: 2, Stdout: resourceProceedAck}, nil
		}, 1},
		"error status": {func(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error) {
			return wireproto.EventResponse{Schema: wireproto.Schema, Status: "error", Stdout: resourceProceedAck}, nil
		}, 1},
		"padded proceed": {func(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error) {
			return wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: "  " + resourceProceedAck + "\n\n"}, nil
		}, 3},
		"worker spacing": {func(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error) {
			return wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: "{\"decision\": \"proceed\"}\n"}, nil
		}, 3},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			h := newMonitorHarness(t, nil)
			h.respond = tc.respond
			h.register()
			h.spawn(testHogPID, testClaudePID, hogRate{cpu: 1}, "yes")
			h.steps(12)
			if got := h.stages(); len(got) != tc.dispatches || got[0] != "warn" {
				t.Fatalf("stages = %v, want %d dispatches starting with warn", got, tc.dispatches)
			}
		})
	}
}

func TestMonitorANewStartUnderTheSamePIDStartsFresh(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	h.register()
	h.spawn(testHogPID, testClaudePID, hogRate{cpu: 1}, "yes")
	h.steps(3)
	if got := h.stages(); !slices.Equal(got, []string{"warn"}) {
		t.Fatalf("stages = %v", got)
	}
	h.source.remove(testHogPID)
	h.spawn(testHogPID, testClaudePID, hogRate{cpu: 1}, "yes")
	h.steps(2)
	if got := h.stages(); len(got) != 1 {
		t.Fatalf("a reused pid inherited the old identity's state: %v", got)
	}
	h.step()
	records := h.records()
	if len(records) != 2 || records[1].stage() != "warn" || records[1].process()["start_unix"] == records[0].process()["start_unix"] {
		t.Fatalf("records = %v", h.stages())
	}
}

func TestMonitorUnknownUsageNeverAdvancesTheWindow(t *testing.T) {
	t.Parallel()
	for name, usage := range map[string]*procUsage{
		"process unreadable":   nil,
		"both metrics unknown": {CPUSeconds: 1 << 20, DiskBytes: 1 << 40},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			h := newMonitorHarness(t, nil)
			h.register()
			h.spawn(testHogPID, testClaudePID, hogRate{cpu: 1}, "yes")
			delete(h.hogs, testHogPID)
			if usage == nil {
				h.source.clearUsage(testHogPID)
			} else {
				h.source.setUsage(testHogPID, *usage)
			}
			h.steps(12)
			if got := h.stages(); len(got) != 0 {
				t.Fatalf("stages = %v", got)
			}
		})
	}
}

func TestMonitorIgnoresAnIdleLongChildAndACalmedOne(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	h.register()
	h.spawn(testHogPID, testClaudePID, hogRate{cpu: 0.1}, "sleep")
	h.steps(20)
	if got := h.stages(); len(got) != 0 {
		t.Fatalf("an idle child was dispatched: %v", got)
	}
	h.hogs[testHogPID] = hogRate{cpu: 1}
	h.steps(2)
	if got := h.stages(); !slices.Equal(got, []string{"warn"}) {
		t.Fatalf("stages = %v", got)
	}
	h.hogs[testHogPID] = hogRate{cpu: 0}
	h.steps(10)
	if got := h.stages(); len(got) != 1 {
		t.Fatalf("a calmed child was judged: %v", got)
	}
	h.hogs[testHogPID] = hogRate{cpu: 1}
	h.steps(3)
	if got := h.stages(); !slices.Equal(got, []string{"warn", "judge"}) {
		t.Fatalf("a warned child that resumed was warned twice or not judged: %v", got)
	}
}

func TestMonitorUnregistersASession(t *testing.T) {
	t.Parallel()
	for name, retire := range map[string]func(h *monitorHarness){
		"session end": func(h *monitorHarness) { h.monitor.observe(h.sessionRequest("SessionEnd", "s1", nil), testHookPID) },
		"dead anchor": func(h *monitorHarness) { h.source.remove(testClaudePID) },
		"reused claude pid": func(h *monitorHarness) {
			h.source.add(procRow{PID: testClaudePID, PPID: 1, PGID: testClaudePID, StartUnix: h.clock.Now().Unix(), Comm: "claude"}, "claude")
		},
		"missing root": func(h *monitorHarness) {
			if err := os.RemoveAll(h.root); err != nil {
				h.t.Fatal(err)
			}
		},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			h := newMonitorHarness(t, nil)
			h.register()
			h.spawn(testHogPID, testClaudePID, hogRate{cpu: 1}, "yes")
			h.step()
			retire(h)
			h.steps(8)
			if h.sessions() != 0 {
				t.Fatal("the session stayed registered")
			}
			if got := h.stages(); len(got) != 0 {
				t.Fatalf("a retired session dispatched %v", got)
			}
		})
	}
}

func TestMonitorRegistryCapRefusesNewSessions(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, map[string]string{"HOOKS_PERFORMANCE_REGISTRY_CAP": "1"})
	h.register()
	h.monitor.observe(h.sessionRequest("PreToolUse", "s2", nil), testHookPID)
	if h.sessions() != 1 {
		t.Fatalf("sessions = %d past the registry cap", h.sessions())
	}
	h.monitor.observe(h.sessionRequest("SessionEnd", "s1", nil), testHookPID)
	h.monitor.observe(h.sessionRequest("PreToolUse", "s2", nil), testHookPID)
	if h.sessions() != 1 {
		t.Fatal("a freed slot refused the next session")
	}
}

func TestMonitorPerSessionCapSkipsExtraChildrenUntilASlotFrees(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	h.monitor.observe(h.sessionRequest("PreToolUse", "s1", map[string]string{"HOOKS_PERFORMANCE_MAX_TRACKED_PER_SESSION": "1"}), testHookPID)
	h.spawn(testHogPID, testClaudePID, hogRate{cpu: 1}, "yes")
	h.spawn(testHogPID+1, testClaudePID, hogRate{cpu: 1}, "yes")
	h.steps(8)
	records := h.records()
	if len(records) != 3 || records[0].process()["pid"] != float64(testHogPID) || records[2].process()["pid"] != float64(testHogPID) {
		t.Fatalf("stages = %v for pids %v", h.stages(), records)
	}
	h.source.remove(testHogPID)
	delete(h.hogs, testHogPID)
	h.steps(3)
	if records := h.records(); len(records) != 4 || records[3].process()["pid"] != float64(testHogPID+1) || records[3].stage() != "warn" {
		t.Fatalf("the freed slot was not taken by the waiting child: %v", h.stages())
	}
}

func TestMonitorNeverTracksBaselineOrPreRegistrationChildren(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	h.spawn(testHogPID, testClaudePID, hogRate{cpu: 1}, "yes")
	h.register()
	h.source.add(procRow{PID: 300, PPID: testClaudePID, PGID: 300, StartUnix: h.clock.Now().Unix() - 100, Comm: "yes"}, "yes")
	h.hogs[300] = hogRate{cpu: 1}
	h.source.setUsage(300, procUsage{CPUKnown: true})
	h.usage[300] = procUsage{CPUKnown: true}
	h.steps(12)
	if got := h.stages(); len(got) != 0 {
		t.Fatalf("a baseline or pre-registration child was dispatched: %v", got)
	}
}

func TestMonitorPrunesANestedAgentSubtree(t *testing.T) {
	t.Parallel()
	for name, agent := range map[string][]string{
		"codex":       {"codex", "exec"},
		"claude":      {"/opt/homebrew/bin/claude", "-p", "hi"},
		"node cli.js": {"node", "/Users/me/.claude/local/node_modules/@anthropic-ai/claude-code/cli.js"},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			h := newMonitorHarness(t, nil)
			h.register()
			h.source.add(procRow{PID: 300, PPID: testClaudePID, PGID: 300, StartUnix: h.clock.Now().Unix(), Comm: agent[0]}, agent...)
			h.spawn(301, 300, hogRate{cpu: 1}, "yes")
			h.steps(12)
			if got := h.stages(); len(got) != 0 {
				t.Fatalf("a nested agent's child was dispatched: %v", got)
			}
			h.monitor.mu.Lock()
			_, cached := h.monitor.sessions["s1"].argvs[h.source.rows[300].identity()]
			h.monitor.mu.Unlock()
			if !cached {
				t.Fatal("the agent's argv was not cached per identity")
			}
		})
	}
}

func TestMonitorRefusesToRegisterWithoutAnAnchorOrWhenDisabled(t *testing.T) {
	t.Parallel()
	for name, prepare := range map[string]func(h *monitorHarness) wireproto.EventRequest{
		"disabled": func(h *monitorHarness) wireproto.EventRequest {
			return h.sessionRequest("PreToolUse", "s1", map[string]string{"HOOKS_PERFORMANCE_ENABLED": "false"})
		},
		"bad setting": func(h *monitorHarness) wireproto.EventRequest {
			return h.sessionRequest("PreToolUse", "s1", map[string]string{"HOOKS_PERFORMANCE_CPU_FRACTION": "lots"})
		},
		"forged parent": func(h *monitorHarness) wireproto.EventRequest {
			request := h.sessionRequest("PreToolUse", "s1", nil)
			request.ClientPPID = 999
			return request
		},
		"parent below the peer": func(h *monitorHarness) wireproto.EventRequest {
			request := h.sessionRequest("PreToolUse", "s1", nil)
			request.ClientPPID = testHookPID
			return request
		},
		"no claude ancestor": func(h *monitorHarness) wireproto.EventRequest {
			h.source.add(procRow{PID: testShellPID, PPID: 1, PGID: testShellPID, StartUnix: 1, Comm: "zsh"}, "zsh")
			return h.sessionRequest("PreToolUse", "s1", nil)
		},
		"no session id": func(h *monitorHarness) wireproto.EventRequest {
			request := h.sessionRequest("PreToolUse", "s1", nil)
			request.PayloadRaw = `{"hook_event_name":"PreToolUse"}`
			return request
		},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			h := newMonitorHarness(t, nil)
			h.monitor.observe(prepare(h), testHookPID)
			if h.sessions() != 0 {
				t.Fatal("the session registered")
			}
		})
	}
}

func TestMonitorKeepsSessionsWhenTheTableIsUnreadable(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	h.register()
	h.spawn(testHogPID, testClaudePID, hogRate{cpu: 1}, "yes")
	h.source.snapshotErr = errFakeSnapshot
	h.steps(6)
	if h.sessions() != 1 {
		t.Fatal("an unreadable table retired the session")
	}
	if got := h.stages(); len(got) != 0 {
		t.Fatalf("an unreadable table dispatched %v", got)
	}
}

func TestIsClaudeArgvMirrorsThePythonClassifier(t *testing.T) {
	t.Parallel()
	for name, tc := range map[string]struct {
		argv   []string
		claude bool
		agent  bool
	}{
		"bare claude":        {[]string{"claude"}, true, true},
		"absolute claude":    {[]string{"/opt/homebrew/bin/claude", "--resume"}, true, true},
		"node claude cli.js": {[]string{"node", "/Users/me/.claude/local/node_modules/x/cli.js", "-p"}, true, true},
		"bun claude-code":    {[]string{"bun", "run", "/a/claude-code/cli.js"}, true, true},
		"node other cli.js":  {[]string{"node", "/opt/other/cli.js"}, false, false},
		"node claude.js":     {[]string{"node", "/opt/claude/claude.js"}, false, false},
		"codex":              {[]string{"/usr/local/bin/codex", "exec"}, false, true},
		"python":             {[]string{"python3", "-m", "pytest"}, false, false},
		"claude as argument": {[]string{"grep", "claude"}, false, false},
		"empty":              {nil, false, false},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			if got := isClaudeArgv(tc.argv); got != tc.claude {
				t.Fatalf("isClaudeArgv(%q) = %t, want %t", tc.argv, got, tc.claude)
			}
			if got := isAgentArgv(tc.argv); got != tc.agent {
				t.Fatalf("isAgentArgv(%q) = %t, want %t", tc.argv, got, tc.agent)
			}
		})
	}
}

func TestHostDispatchLeavesTheServiceEstimateUnchanged(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	scriptedWorker(t, manager, func(conn net.Conn) {
		for {
			frame, err := wireproto.DecodeFrame(conn)
			if err != nil {
				return
			}
			_ = wireproto.EncodeFrame(conn, wireproto.Frame{
				Protocol: wireproto.Schema, Op: wireproto.OpResult, ID: frame.ID,
				Response: &wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok"},
			})
		}
	})
	request := testEventRequest("PreToolUse")
	request.Root = "/live"
	service := func() time.Duration {
		manager.mu.Lock()
		defer manager.mu.Unlock()
		for _, entry := range manager.entries {
			return entry.service
		}
		t.Fatal("no worker entry")
		return 0
	}
	ctx, cancel := context.WithTimeout(withHostDispatch(t.Context()), 5*time.Second)
	defer cancel()
	for range 3 {
		if _, err := manager.dispatch(ctx, request); err != nil {
			t.Fatal(err)
		}
	}
	if got := service(); got != 0 {
		t.Fatalf("host dispatches moved the service estimate to %s", got)
	}
	for range 2 {
		if _, err := manager.dispatch(t.Context(), request); err != nil {
			t.Fatal(err)
		}
	}
	if got := service(); got <= 0 {
		t.Fatalf("a client dispatch left the service estimate at %s", got)
	}
}

func TestMonitorDispatchesThroughTheWorkerWithSessionAffinity(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	h.monitor.dispatch = h.manager.dispatch
	frames := make(chan wireproto.Frame, 4)
	scriptedWorker(t, h.manager, func(conn net.Conn) {
		for {
			frame, err := wireproto.DecodeFrame(conn)
			if err != nil {
				return
			}
			frames <- frame
			_ = wireproto.EncodeFrame(conn, wireproto.Frame{
				Protocol: wireproto.Schema, Op: wireproto.OpResult, ID: frame.ID,
				Response: &wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: resourceProceedAck},
			})
		}
	})
	h.root = durableRoot(t)
	registering := h.sessionRequest("PreToolUse", "s1", nil)
	h.monitor.observe(registering, testHookPID)
	if h.sessions() != 1 {
		t.Fatal("the session did not register")
	}
	h.spawn(testHogPID, testClaudePID, hogRate{cpu: 1}, "yes")
	h.steps(5)
	for _, stage := range []string{"warn", "judge"} {
		var frame wireproto.Frame
		select {
		case frame = <-frames:
		case <-time.After(10 * time.Second):
			t.Fatalf("no %s frame reached the worker", stage)
		}
		request := frame.Request
		if frame.Op != wireproto.OpEvent || request.Event != resourceEvent || request.ClientPPID != testClaudePID ||
			request.ClientPID != os.Getpid() || request.Mandatory || request.DeadlineUnixMS == 0 {
			t.Fatalf("worker frame = %+v", request)
		}
		synthetic, err := makeWorkerKey(*request)
		if err != nil {
			t.Fatal(err)
		}
		real, err := makeWorkerKey(registering)
		if err != nil {
			t.Fatal(err)
		}
		if synthetic.affinity != testTranscript || synthetic.affinity != real.affinity || synthetic.id != real.id {
			t.Fatalf("synthetic key %+v does not share the session's worker %+v", synthetic, real)
		}
	}
	select {
	case frame := <-frames:
		t.Fatalf("unexpected third frame %+v", frame.Request)
	default:
	}
}

func durableRoot(t *testing.T) string {
	t.Helper()
	root, err := filepath.EvalSymlinks(os.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	return root
}

func TestMonitorLoopExitsWithTheManager(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	monitor := testResourceMonitor(t, manager)
	monitor.start()
	closeManager(t, manager)
}

func TestHostProductRefusesClientResourcePressure(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	monitor := testResourceMonitor(t, manager)
	product := &hostProduct{manager: manager, hub: newNotificationHub(), monitor: monitor}
	request := testEventRequest(resourceEvent)
	request.PayloadRaw = `{"session_id":"s1","stage":"judge"}`
	body, err := json.Marshal(request)
	if err != nil {
		t.Fatal(err)
	}
	_, err = product.Handle(t.Context(), daemonkit.Request{Op: opEvent, Body: body, Caller: daemonkit.Caller{PID: request.ClientPID}})
	if err == nil || !strings.Contains(err.Error(), "host event") {
		t.Fatalf("a client-submitted ResourcePressure was not refused: %v", err)
	}
	monitor.mu.Lock()
	defer monitor.mu.Unlock()
	if len(monitor.sessions) != 0 {
		t.Fatal("a refused host event registered a session")
	}
}

func TestMonitorAnUnreadableSampleEmptiesTheWindow(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	h.register()
	h.spawn(testHogPID, testClaudePID, hogRate{cpu: 1}, "yes")
	h.steps(2)
	known := h.usage[testHogPID]
	delete(h.hogs, testHogPID)
	h.source.clearUsage(testHogPID)
	h.steps(3)
	if got := h.stages(); len(got) != 0 {
		t.Fatalf("a stale window dispatched %v", got)
	}
	h.hogs[testHogPID] = hogRate{cpu: 1}
	h.usage[testHogPID] = known
	h.source.setUsage(testHogPID, known)
	h.steps(2)
	if got := h.stages(); len(got) != 0 {
		t.Fatalf("the window advanced before two fresh readable rates: %v", got)
	}
	h.step()
	if got := h.stages(); !slices.Equal(got, []string{"warn"}) {
		t.Fatalf("stages = %v", got)
	}
	delete(h.hogs, testHogPID)
	h.source.clearUsage(testHogPID)
	h.steps(6)
	if got := h.stages(); len(got) != 1 {
		t.Fatalf("an unreadable child in grace was judged: %v", got)
	}
}

func (h *monitorHarness) registerSecondSession() {
	start := h.clock.Now().Unix()
	h.source.add(procRow{PID: 110, PPID: 1, PGID: 110, StartUnix: start - 500, Comm: "claude"}, "claude")
	h.source.add(procRow{PID: 111, PPID: 110, PGID: 111, StartUnix: start - 1, Comm: "zsh"}, "zsh")
	h.source.add(procRow{PID: 112, PPID: 111, PGID: 111, StartUnix: start - 1, Comm: "hook"}, "hook")
	request := h.sessionRequest("PreToolUse", "s2", nil)
	request.ClientPID, request.ClientPPID = 112, 111
	h.monitor.observe(request, 112)
	if h.sessions() != 2 {
		h.t.Fatal("the second session did not register")
	}
}

func holdSession(id string, release <-chan struct{}) func(context.Context, wireproto.EventRequest) (wireproto.EventResponse, error) {
	return func(ctx context.Context, request wireproto.EventRequest) (wireproto.EventResponse, error) {
		if strings.Contains(request.PayloadRaw, `"session_id":"`+id+`"`) {
			select {
			case <-release:
			case <-ctx.Done():
				return wireproto.EventResponse{}, ctx.Err()
			}
		}
		return proceedResponse(ctx, request)
	}
}

func TestMonitorASlowStageNeverBlocksTheTicker(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	release := make(chan struct{})
	h.respond = holdSession("s1", release)
	h.register()
	h.registerSecondSession()
	h.spawn(testHogPID, testClaudePID, hogRate{cpu: 1}, "yes")
	h.spawn(300, 110, hogRate{cpu: 1}, "yes")
	h.steps(2)
	h.tickOnly()
	h.awaitRecords(2)
	h.settleTo(1)
	h.tickOnly()
	h.tickOnly()
	h.settleTo(1)
	if s1, s2 := h.sessionStages("s1"), h.sessionStages("s2"); !slices.Equal(s1, []string{"warn"}) || !slices.Equal(s2, []string{"warn", "judge"}) {
		t.Fatalf("while s1's warn was held: s1 = %v, s2 = %v", s1, s2)
	}
	close(release)
	h.settle()
	h.steps(3)
	if s1, s2 := h.sessionStages("s1"), h.sessionStages("s2"); !slices.Equal(s1, []string{"warn", "judge"}) || !slices.Equal(s2, []string{"warn", "judge", "escalate"}) {
		t.Fatalf("after release: s1 = %v, s2 = %v", s1, s2)
	}
}

func (h *monitorHarness) sessionStages(id string) []string {
	stages := make([]string, 0)
	for _, record := range h.records() {
		if record.payload["session_id"] == id {
			stages = append(stages, record.stage())
		}
	}
	return stages
}

func TestMonitorUnregisterCancelsAStageInFlight(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	ended := make(chan error, 1)
	h.respond = func(ctx context.Context, request wireproto.EventRequest) (wireproto.EventResponse, error) {
		<-ctx.Done()
		ended <- ctx.Err()
		return wireproto.EventResponse{}, ctx.Err()
	}
	h.register()
	h.spawn(testHogPID, testClaudePID, hogRate{cpu: 1}, "yes")
	h.steps(2)
	h.tickOnly()
	h.awaitRecords(1)
	h.monitor.observe(h.sessionRequest("SessionEnd", "s1", nil), testHookPID)
	select {
	case err := <-ended:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("stage ended with %v, want cancellation", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("unregister left the stage running")
	}
	if h.sessions() != 0 {
		t.Fatal("the session stayed registered")
	}
}

func TestMonitorAdmitsOneStageInFlightPerSession(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	release := make(chan struct{})
	h.respond = holdSession("s1", release)
	h.register()
	h.spawn(testHogPID, testClaudePID, hogRate{cpu: 1}, "yes")
	h.spawn(testHogPID+1, testClaudePID, hogRate{cpu: 1}, "yes")
	h.steps(2)
	h.tickOnly()
	h.awaitRecords(1)
	h.tickOnly()
	h.tickOnly()
	if records := h.records(); len(records) != 1 || records[0].process()["pid"] != float64(testHogPID) {
		t.Fatalf("records with one stage held = %v", h.stages())
	}
	close(release)
	h.settle()
	h.step()
	records := h.records()
	if len(records) != 2 || records[1].stage() != "warn" || records[1].process()["pid"] != float64(testHogPID+1) {
		t.Fatalf("records after release = %v", h.stages())
	}
}

func nextFrame(t *testing.T, frames <-chan wireproto.Frame) wireproto.Frame {
	t.Helper()
	select {
	case frame := <-frames:
		return frame
	case <-time.After(10 * time.Second):
		t.Fatal("no frame reached the worker")
		return wireproto.Frame{}
	}
}

func TestMonitorAbandonsAHeldStageOnTheWorkerStream(t *testing.T) {
	t.Parallel()
	for name, retire := range map[string]func(h *monitorHarness){
		"session end": func(h *monitorHarness) { h.monitor.observe(h.sessionRequest("SessionEnd", "s1", nil), testHookPID) },
		"dead anchor": func(h *monitorHarness) {
			h.source.remove(testClaudePID)
			h.tickOnly()
		},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			h := newMonitorHarness(t, nil)
			h.monitor.dispatch = h.manager.dispatch
			frames := make(chan wireproto.Frame, 8)
			scriptedWorker(t, h.manager, func(conn net.Conn) {
				for {
					frame, err := wireproto.DecodeFrame(conn)
					if err != nil {
						return
					}
					frames <- frame
					if frame.Op == wireproto.OpAbandon {
						_ = wireproto.EncodeFrame(conn, wireproto.Frame{Protocol: wireproto.Schema, Op: wireproto.OpError, ID: frame.ID, Error: "abandoned"})
					}
				}
			})
			h.root = durableRoot(t)
			h.monitor.observe(h.sessionRequest("PreToolUse", "s1", nil), testHookPID)
			if h.sessions() != 1 {
				t.Fatal("the session did not register")
			}
			h.spawn(testHogPID, testClaudePID, hogRate{cpu: 1}, "yes")
			h.steps(2)
			h.tickOnly()
			event := nextFrame(t, frames)
			if event.Op != wireproto.OpEvent || event.ID == 0 || event.Request == nil || event.Request.Event != resourceEvent {
				t.Fatalf("first worker frame = %+v", event)
			}
			retire(h)
			abandon := nextFrame(t, frames)
			if abandon.Protocol != wireproto.Schema || abandon.Op != wireproto.OpAbandon || abandon.ID != event.ID ||
				abandon.Build != "" || abandon.Request != nil || abandon.Response != nil || abandon.Error != "" ||
				abandon.Adopt != nil || abandon.ParentID != 0 || len(abandon.Snapshot)+len(abandon.SnapshotContext)+len(abandon.SnapshotConfig) != 0 {
				t.Fatalf("abandon frame = %+v", abandon)
			}
			h.settle()
			if h.sessions() != 0 {
				t.Fatal("the session stayed registered")
			}
			select {
			case frame := <-frames:
				t.Fatalf("unexpected frame after abandonment: %+v", frame)
			default:
			}
		})
	}
}

func TestMonitorEscalatesOnlyOnCurrentPressure(t *testing.T) {
	t.Parallel()
	for name, tc := range map[string]struct {
		quiet      func(h *monitorHarness)
		resume     func(h *monitorHarness)
		transition string
	}{
		"unknown metrics": {
			quiet: func(h *monitorHarness) {
				delete(h.hogs, testHogPID)
				h.source.clearUsage(testHogPID)
			},
			resume: func(h *monitorHarness) {
				known := procUsage{CPUKnown: true}
				h.hogs[testHogPID] = hogRate{cpu: 1}
				h.usage[testHogPID] = known
				h.source.setUsage(testHogPID, known)
			},
			transition: "no sustained pressure at escalation; observing",
		},
		"calmed": {
			quiet:      func(h *monitorHarness) { h.hogs[testHogPID] = hogRate{cpu: 0} },
			resume:     func(h *monitorHarness) { h.hogs[testHogPID] = hogRate{cpu: 1} },
			transition: "no sustained pressure at escalation; observing",
		},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			h := newMonitorHarness(t, nil)
			log := &lockedBuffer{}
			h.monitor.logWriter = log
			h.register()
			h.spawn(testHogPID, testClaudePID, hogRate{cpu: 1}, "yes")
			h.steps(5)
			if got := h.stages(); !slices.Equal(got, []string{"warn", "judge"}) {
				t.Fatalf("stages before the escalation delay = %v", got)
			}
			tc.quiet(h)
			h.steps(8)
			if got := h.stages(); !slices.Equal(got, []string{"warn", "judge"}) {
				t.Fatalf("a judged child without current pressure was escalated: %v", got)
			}
			if strings.Count(log.String(), tc.transition) != 1 {
				t.Fatalf("log = %q, want one %q transition", log.String(), tc.transition)
			}
			tc.resume(h)
			h.steps(4)
			if got := h.stages(); !slices.Equal(got, []string{"warn", "judge", "judge"}) {
				t.Fatalf("stages after pressure resumed = %v, want a fresh judge before any escalation", got)
			}
		})
	}
}

func (h *monitorHarness) addForeignTree(claude, shell, hook int, claudeArgv ...string) {
	start := h.clock.Now().Unix()
	if len(claudeArgv) != 0 {
		h.source.add(procRow{PID: claude, PPID: 1, PGID: claude, StartUnix: start - 500, Comm: claudeArgv[0]}, claudeArgv...)
	}
	h.source.add(procRow{PID: shell, PPID: claude, PGID: shell, StartUnix: start - 1, Comm: "zsh"}, "zsh")
	h.source.add(procRow{PID: hook, PPID: shell, PGID: shell, StartUnix: start - 1, Comm: "hook"}, "hook")
}

func TestMonitorAForeignPeerCannotUnregisterOrRefresh(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	log := &lockedBuffer{}
	h.monitor.logWriter = log
	h.register()
	h.addForeignTree(110, 111, 112, "claude")
	h.addForeignTree(1, 121, 122)
	registered := h.lastSeen("s1")
	h.clock.Advance(time.Minute)
	for _, peer := range []struct{ pid, ppid int }{{112, 111}, {122, 121}} {
		for _, event := range []string{"SessionEnd", "PreToolUse"} {
			request := h.sessionRequest(event, "s1", nil)
			request.ClientPID, request.ClientPPID = peer.pid, peer.ppid
			h.monitor.observe(request, peer.pid)
		}
	}
	if h.sessions() != 1 {
		t.Fatal("a foreign peer unregistered the session")
	}
	if got := h.lastSeen("s1"); !got.Equal(registered) {
		t.Fatalf("a foreign peer refreshed the session: last seen moved from %s to %s", registered, got)
	}
	if got := strings.Count(log.String(), "outside its claude anchor"); got != 1 {
		t.Fatalf("foreign peers were logged %d times, want once: %q", got, log.String())
	}
	h.monitor.observe(h.sessionRequest("PreToolUse", "s1", nil), testHookPID)
	if got := h.lastSeen("s1"); !got.After(registered) {
		t.Fatal("the session's own peer did not refresh it")
	}
	h.monitor.observe(h.sessionRequest("SessionEnd", "s1", nil), testHookPID)
	if h.sessions() != 0 {
		t.Fatal("the session's own peer could not unregister it")
	}
}

func (h *monitorHarness) lastSeen(id string) time.Time {
	h.monitor.mu.Lock()
	defer h.monitor.mu.Unlock()
	return h.monitor.sessions[id].lastSeen
}

func TestMonitorCapsSessionsPerAnchor(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	for i := range resourceSessionsPerAnchor + 1 {
		h.monitor.observe(h.sessionRequest("PreToolUse", fmt.Sprintf("s%d", i), nil), testHookPID)
	}
	if h.sessions() != resourceSessionsPerAnchor {
		t.Fatalf("sessions = %d, want the per-anchor cap of %d", h.sessions(), resourceSessionsPerAnchor)
	}
	h.addForeignTree(110, 111, 112, "claude")
	other := h.sessionRequest("PreToolUse", "other", nil)
	other.ClientPID, other.ClientPPID = 112, 111
	h.monitor.observe(other, 112)
	if h.sessions() != resourceSessionsPerAnchor+1 {
		t.Fatal("another anchor was refused by the first anchor's cap")
	}
	h.monitor.observe(h.sessionRequest("SessionEnd", "s0", nil), testHookPID)
	h.monitor.observe(h.sessionRequest("PreToolUse", fmt.Sprintf("s%d", resourceSessionsPerAnchor), nil), testHookPID)
	if h.sessions() != resourceSessionsPerAnchor+1 {
		t.Fatal("a freed anchor slot refused the next session")
	}
}

func (h *monitorHarness) addChain(parent, first, n int) {
	start := h.clock.Now().Unix() - 100
	for pid := first; pid < first+n; pid++ {
		h.source.add(procRow{PID: pid, PPID: parent, PGID: first, StartUnix: start, Comm: "sh"}, "sh")
		parent = pid
	}
}

type walkState struct {
	walking bool
	visited int
	cached  int
	tracked []int
}

func (h *monitorHarness) walkState(id string) walkState {
	h.monitor.mu.Lock()
	defer h.monitor.mu.Unlock()
	session := h.monitor.sessions[id]
	state := walkState{walking: session.walk != nil, cached: len(session.argvs), tracked: sortedTracked(session.tracked)}
	if session.walk != nil {
		state.visited = len(session.walk.visited)
	}
	return state
}

func (h *monitorHarness) argvReadsPerTick(ticks int) []int {
	reads := make([]int, 0, ticks)
	for range ticks {
		_, before := h.source.reads()
		h.step()
		_, after := h.source.reads()
		reads = append(reads, after-before)
	}
	return reads
}

func TestMonitorABurstOfRegistrationsTakesOneCensus(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	for _, id := range []string{"s1", "s2", "s3"} {
		h.monitor.observe(h.sessionRequest("PreToolUse", id, nil), testHookPID)
	}
	if h.sessions() != 3 || h.source.censuses() != 1 {
		t.Fatalf("three registrations in one interval: sessions = %d, censuses = %d, want 3 and 1", h.sessions(), h.source.censuses())
	}
	h.clock.Advance(h.monitor.settings.SampleInterval)
	h.monitor.observe(h.sessionRequest("PreToolUse", "s4", nil), testHookPID)
	if h.source.censuses() != 2 {
		t.Fatalf("a registration one interval later took %d censuses in total, want a fresh one (2)", h.source.censuses())
	}
	h.tickOnly()
	h.monitor.observe(h.sessionRequest("PreToolUse", "s5", nil), testHookPID)
	if h.sessions() != 5 || h.source.censuses() != 3 {
		t.Fatalf("a registration right after a tick: sessions = %d, censuses = %d, want 5 and the tick's census (3)", h.sessions(), h.source.censuses())
	}
}

func TestMonitorRegistrationsDuringATickJoinItsCensus(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	gate := make(chan struct{})
	h.source.holdSnapshots(gate)
	var joined sync.WaitGroup
	joined.Add(1)
	go func() {
		defer joined.Done()
		h.monitor.tick(h.clock.Now())
	}()
	h.waitFor(func() bool { return h.source.censuses() == 1 }, "the tick never started its census")
	for _, id := range []string{"s1", "s2"} {
		joined.Add(1)
		go func() {
			defer joined.Done()
			h.monitor.observe(h.sessionRequest("PreToolUse", id, nil), testHookPID)
		}()
	}
	h.waitFor(func() bool { return h.source.probeReads() == 6 }, "the registrations never resolved their anchors")
	close(gate)
	joined.Wait()
	if h.sessions() != 2 || h.source.censuses() != 1 {
		t.Fatalf("two registrations during the tick's census: sessions = %d, censuses = %d, want 2 and 1", h.sessions(), h.source.censuses())
	}
}

func TestMonitorNeverReusesAFailedCensus(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	h.source.snapshotErr = errFakeSnapshot
	h.monitor.observe(h.sessionRequest("PreToolUse", "s1", nil), testHookPID)
	if h.sessions() != 0 {
		t.Fatal("a registration whose census failed was admitted")
	}
	h.source.snapshotErr = nil
	h.register()
	if h.source.censuses() != 2 {
		t.Fatalf("the registration after a failed census took %d censuses in total, want a fresh one (2)", h.source.censuses())
	}
}

func TestMonitorResumesAnOversizedWalkAcrossTicks(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	log := &lockedBuffer{}
	h.monitor.logWriter = log
	h.register()
	h.source.remove(testShellPID)
	h.source.remove(testHookPID)
	const descendants = 10_000
	h.addChain(testClaudePID, 1000, descendants-1)
	leaf := 1000 + descendants - 1
	h.spawn(leaf, leaf-1, hogRate{cpu: 1}, "yes")
	reads := h.argvReadsPerTick(3)
	if !slices.Equal(reads[:2], []int{resourceTickBudget, resourceTickBudget}) {
		t.Fatalf("argv reads on the first two ticks = %v, want one per visited parent (%d)", reads[:2], resourceTickBudget)
	}
	if total := reads[0] + reads[1] + reads[2]; total != descendants || slices.Max(reads) > resourceTickBudget {
		t.Fatalf("argv reads per tick = %v, want at most %d per tick and %d in total with no restart", reads, resourceTickBudget, descendants)
	}
	if state := h.walkState("s1"); state.walking || state.cached != descendants || !slices.Equal(state.tracked, []int{leaf}) {
		t.Fatalf("after %d ticks: %+v, want a finished walk that tracks the deep leaf", len(reads), state)
	}
	more := h.argvReadsPerTick(6)
	if slices.Max(more) > 1 || h.source.censuses() != 10 {
		t.Fatalf("argv reads per tick on later walks = %v with %d censuses, want only the warn payload's read", more, h.source.censuses())
	}
	records := h.records()
	if len(records) != 1 || records[0].stage() != "warn" || records[0].process()["pid"] != float64(leaf) {
		t.Fatalf("records = %v, want one warn for the deep leaf", h.stages())
	}
	if got := strings.Count(log.String(), "exceeded"); got != 1 {
		t.Fatalf("the spanning walk was logged %d times, want once: %q", got, log.String())
	}
}

func TestMonitorRecordsWalkProgressBetweenTicks(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	h.register()
	h.source.remove(testShellPID)
	h.source.remove(testHookPID)
	h.addChain(testClaudePID, 1000, 2*resourceTickBudget+1)
	h.step()
	if state := h.walkState("s1"); !state.walking || state.visited != resourceTickBudget {
		t.Fatalf("after one tick: %+v, want %d rows walked and the walk pending", state, resourceTickBudget)
	}
	h.step()
	if state := h.walkState("s1"); !state.walking || state.visited != 2*resourceTickBudget {
		t.Fatalf("after two ticks: %+v, want the walk resumed to %d rows", state, 2*resourceTickBudget)
	}
	h.step()
	if state := h.walkState("s1"); state.walking || state.cached != 2*resourceTickBudget {
		t.Fatalf("after three ticks: %+v, want the walk finished and every parent cached", state)
	}
}

func TestMonitorSessionsShareOneTickBudget(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	h.register()
	h.registerSecondSession()
	for _, pid := range []int{testShellPID, testHookPID, 111, 112} {
		h.source.remove(pid)
	}
	const chain = 5000
	h.addChain(testClaudePID, 1000, chain)
	h.addChain(110, 20_000, chain)
	_, before := h.source.reads()
	h.step()
	_, after := h.source.reads()
	s1, s2 := h.walkState("s1"), h.walkState("s2")
	if after-before > resourceTickBudget || s1.visited+s2.visited > resourceTickBudget || s1.visited == 0 || s2.visited == 0 {
		t.Fatalf("one tick read %d argvs and walked s1 = %d, s2 = %d rows; want both progressing within %d", after-before, s1.visited, s2.visited, resourceTickBudget)
	}
	reads := h.argvReadsPerTick(2)
	if slices.Max(reads) > resourceTickBudget || after-before+reads[0]+reads[1] != 2*(chain-1) {
		t.Fatalf("argv reads per tick = %v after %d, want at most %d each and %d in total", reads, after-before, resourceTickBudget, 2*(chain-1))
	}
	for _, id := range []string{"s1", "s2"} {
		if state := h.walkState(id); state.walking || state.cached != chain-1 {
			t.Fatalf("%s after three ticks: %+v, want a finished walk", id, state)
		}
	}
}

func TestMonitorDropsAFrontierRowWhoseIdentityChanged(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	h.register()
	h.source.remove(testShellPID)
	h.source.remove(testHookPID)
	const chain = 5000
	h.addChain(testClaudePID, 1000, chain-1)
	leaf := 1000 + chain - 1
	h.spawn(leaf, leaf-1, hogRate{}, "yes")
	frontier := 1000 + resourceTickBudget
	h.step()
	old := h.source.rows[frontier]
	h.source.add(procRow{PID: frontier, PPID: frontier - 1, PGID: frontier, StartUnix: h.clock.Now().Unix(), Comm: "yes"}, "yes")
	if reads := h.argvReadsPerTick(1); reads[0] != 0 {
		t.Fatalf("the stale frontier row cost %d argv reads", reads[0])
	}
	h.monitor.mu.Lock()
	_, trusted := h.monitor.sessions["s1"].argvs[old.identity()]
	h.monitor.mu.Unlock()
	if state := h.walkState("s1"); state.walking || len(state.tracked) != 0 || trusted {
		t.Fatalf("after the identity change: %+v, old identity cached = %t; want the frontier row dropped with its subtree", state, trusted)
	}
	h.steps(2)
	h.monitor.mu.Lock()
	_, rewalked := h.monitor.sessions["s1"].argvs[h.source.rows[frontier].identity()]
	h.monitor.mu.Unlock()
	if state := h.walkState("s1"); state.walking || !rewalked || !slices.Equal(state.tracked, []int{frontier, leaf}) {
		t.Fatalf("after the next walk: %+v, new identity cached = %t; want its subtree re-walked and tracked", state, rewalked)
	}
}

func (h *monitorHarness) addBornNow(pid int) {
	born := h.clock.Now()
	h.source.add(procRow{
		PID: pid, PPID: testClaudePID, PGID: pid, StartUnix: born.Unix(), StartUsec: int32(born.Nanosecond() / 1000), Comm: "yes",
	}, "yes")
}

func TestMonitorAWarmTableNeverAdmitsAChildBornBeforeRegistration(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	h.tickOnly()
	h.clock.Advance(100 * time.Millisecond)
	h.addBornNow(testHogPID)
	h.hogs[testHogPID] = hogRate{cpu: 1}
	h.usage[testHogPID] = procUsage{CPUKnown: true}
	h.source.setUsage(testHogPID, h.usage[testHogPID])
	h.clock.Advance(100 * time.Millisecond)
	censuses := h.source.censuses()
	h.register()
	if h.source.censuses() != censuses {
		t.Fatal("the registration took a fresh census instead of reusing the warm table")
	}
	h.clock.Advance(100 * time.Millisecond)
	h.addBornNow(testHogPID + 1)
	h.steps(6)
	if got := h.stages(); len(got) != 0 {
		t.Fatalf("a child born before registration in the same second was dispatched: %v", got)
	}
	if state := h.walkState("s1"); !slices.Equal(state.tracked, []int{testHogPID + 1}) {
		t.Fatalf("tracked = %v, want only the child born after registration", state.tracked)
	}
}

func TestMonitorChargesLeafArgvReadsToTheWalk(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	h.register()
	h.source.remove(testShellPID)
	h.source.remove(testHookPID)
	for pid := 1000; pid < 1000+resourceTickBudget+1; pid++ {
		h.spawn(pid, testClaudePID, hogRate{cpu: 1}, "codex", "exec")
	}
	if reads := h.argvReadsPerTick(3); !slices.Equal(reads, []int{resourceTickBudget, 1, 0}) {
		t.Fatalf("argv reads per tick = %v, want each leaf read once inside its tick's share", reads)
	}
	if state := h.walkState("s1"); len(state.tracked) != 0 {
		t.Fatalf("agent leaves were tracked: %v", state.tracked)
	}
	if got := h.stages(); len(got) != 0 {
		t.Fatalf("agent leaves were dispatched: %v", got)
	}
}

func TestMonitorASlowSamplerNeverBlocksObserve(t *testing.T) {
	t.Parallel()
	h := newMonitorHarness(t, nil)
	h.register()
	h.spawn(testHogPID, testClaudePID, hogRate{cpu: 1}, "yes")
	gate := make(chan struct{})
	h.source.holdUsage(gate)
	ticked := make(chan struct{})
	go func() {
		h.monitor.tick(h.clock.Now())
		close(ticked)
	}()
	h.waitFor(func() bool { usage, _ := h.source.reads(); return usage > 0 }, "the tick never reached the sampler")
	observed := make(chan struct{})
	go func() {
		h.monitor.observe(h.sessionRequest("PreToolUse", "s1", nil), testHookPID)
		h.monitor.observe(h.sessionRequest("SessionEnd", "s1", nil), testHookPID)
		close(observed)
	}()
	select {
	case <-observed:
	case <-time.After(5 * time.Second):
		t.Fatal("observe waited on the sampler's native reads")
	}
	if h.sessions() != 0 {
		t.Fatal("the session stayed registered")
	}
	close(gate)
	<-ticked
	if got := h.stages(); len(got) != 0 {
		t.Fatalf("a sample applied after unregister dispatched %v", got)
	}
}
