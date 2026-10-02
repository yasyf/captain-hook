package hookd

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
)

const (
	eventPythonEnv = "CAPTAIN_TEST_EVENT_PYTHON"

	fakeShellPID = 5_000_001
	fakeHookPID  = 5_000_002

	fakeJudgeDelay  = 3 * time.Second
	childLifetime   = 120 * time.Second
	integrationWait = 30 * time.Second
)

const fakeClaudeScript = `#!/bin/sh
printf 'start %s\n' "$*" >> "CALL_LOG"
case "$1" in
auth) exit 0 ;;
esac
sleep DELAY
printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"result":"ok","structured_output":{"disposable":true,"reasoning":"fake"}}'
printf 'done %s\n' "$*" >> "CALL_LOG"
`

const transcriptFixture = `{"type":"user","uuid":"event-1","sessionId":"s1","timestamp":"2026-10-01T00:00:00Z","message":{"role":"user","content":"run the tests"}}
{"type":"user","uuid":"event-2","sessionId":"s1","timestamp":"2026-10-01T00:00:01Z","message":{"role":"user","content":"keep going"}}
`

type pythonWorkerProcess struct {
	command *exec.Cmd
	stderr  *bytes.Buffer
	done    chan error
}

type stageOutcome struct {
	stage    string
	response wireproto.EventResponse
	err      error
}

type pythonMonitorHarness struct {
	t         *testing.T
	python    string
	build     string
	home      string
	root      string
	signalLog string
	callLog   string
	env       map[string]string

	clock   *fakeClock
	source  *fakeProcSource
	real    procSource
	manager *workerManager
	monitor *resourceMonitor

	anchor    *exec.Cmd
	anchorRow procRow
	child     procRow

	mu      sync.Mutex
	worker  *workerClient
	workers []*pythonWorkerProcess
	usage   procUsage
	stages  []stageOutcome
}

func pythonProbe(t *testing.T, python string) (build, shell string) {
	t.Helper()
	out, err := exec.Command(python, "-P", "-c",
		"import importlib.metadata as m, os, pwd; print(m.version('capt-hook')); print(pwd.getpwuid(os.getuid()).pw_shell)").Output()
	if err != nil {
		t.Fatalf("probe %s: %v", python, err)
	}
	lines := strings.Split(strings.TrimSpace(string(out)), "\n")
	if len(lines) != 2 {
		t.Fatalf("probe output %q", out)
	}
	return lines[0], lines[1]
}

func newPythonMonitorHarness(t *testing.T) *pythonMonitorHarness {
	t.Helper()
	python := os.Getenv(eventPythonEnv)
	if python == "" {
		t.Skip("requires the exact candidate Python environment in CI")
	}
	build, shell := pythonProbe(t, python)
	tmp, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	h := &pythonMonitorHarness{
		t: t, python: python, build: build, real: newProcSource(),
		home: filepath.Join(tmp, "home"), root: filepath.Join(tmp, "repo"),
		signalLog: filepath.Join(tmp, "signals.log"), callLog: filepath.Join(tmp, "claude-calls.log"),
		source: newFakeProcSource(), usage: procUsage{CPUKnown: true},
	}
	bin := filepath.Join(tmp, "bin")
	cache := filepath.Join(tmp, "cache")
	for _, dir := range []string{h.home, h.root, bin, filepath.Join(cache, "captain-hook")} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	script := strings.NewReplacer("DELAY", fmt.Sprintf("%d", int(fakeJudgeDelay.Seconds())), "CALL_LOG", h.callLog).Replace(fakeClaudeScript)
	if err := os.WriteFile(filepath.Join(bin, "claude"), []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	loginPath, err := json.Marshal(map[string]string{
		"shell": shell, "path": bin + ":/usr/bin:/bin", "at": time.Now().UTC().Format("2006-01-02T15:04:05.000000-07:00"),
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(cache, "captain-hook", "login-path.json"), loginPath, 0o600); err != nil {
		t.Fatal(err)
	}
	h.env = maps.Clone(testResourceEnv)
	maps.Copy(h.env, map[string]string{
		"XDG_CACHE_HOME":            cache,
		"CAPTAIN_HOOK_STATE_DIR":    filepath.Join(tmp, "state"),
		"CAPTAIN_HOOK_LOG_DIR":      filepath.Join(tmp, "logs"),
		"CAPT_HOOK_TEST_SIGNAL_LOG": h.signalLog,
		"CAPT_HOOK_HELPER_CLIENT":   filepath.Join(tmp, "no-helper-client"),
	})
	if err := os.WriteFile(filepath.Join(h.root, "transcript.jsonl"), []byte(transcriptFixture), 0o600); err != nil {
		t.Fatal(err)
	}
	h.spawnAnchor()
	t.Cleanup(h.reapWorkers)
	h.manager = mustWorkerManager(t)
	snapshotService, err := newSnapshotService(h.manager)
	if err != nil {
		t.Fatal(err)
	}
	snapshotService.start = func(context.Context) (*snapshotOwner, error) {
		return spawnPythonSnapshotOwner(t, python, snapshotService.config), nil
	}
	h.manager.snapshots = snapshotService
	warmCtx, stopWarm := context.WithTimeout(context.Background(), integrationWait)
	defer stopWarm()
	if _, err := snapshotService.call(warmCtx, statsRequest, userSnapshotContext("warm", "hook", uint32(os.Getuid()))); err != nil {
		t.Fatalf("warm the snapshot owner: %v", err)
	}
	t.Cleanup(func() {
		h.drainBackground()
		closeManager(t, h.manager)
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := snapshotService.Close(ctx); err != nil {
			t.Errorf("snapshot service close: %v", err)
		}
	})
	h.clock = &fakeClock{now: time.Unix(h.child.StartUnix, 0)}
	h.manager.start = h.startWorker
	settings, err := wireproto.ParseResourceSettings(nil)
	if err != nil {
		t.Fatal(err)
	}
	h.monitor = newResourceMonitor(h.manager, h.source, settings)
	h.monitor.now, h.monitor.dispatch = h.clock.Now, h.dispatch
	h.source.add(h.anchorRow, h.realArgv(h.anchorRow.PID)...)
	h.source.add(procRow{PID: fakeShellPID, PPID: h.anchorRow.PID, PGID: fakeShellPID, StartUnix: h.anchorRow.StartUnix, Comm: "zsh"}, "zsh", "-c", "hook")
	h.source.add(procRow{PID: fakeHookPID, PPID: fakeShellPID, PGID: fakeShellPID, StartUnix: h.anchorRow.StartUnix, Comm: "hook"}, "hook")
	h.warmWorker()
	return h
}

func (h *pythonMonitorHarness) warmWorker() {
	h.t.Helper()
	payload, err := json.Marshal(resourcePayload{
		HookEventName: resourceEvent, SessionID: "warm", TranscriptPath: filepath.Join(h.root, "transcript.jsonl"), CWD: h.root,
		Stage: stageWarn, ClaudePID: h.anchorRow.PID, ClaudeStartUnix: h.anchorRow.StartUnix,
		Process: resourceProcess{
			PID: fakeHookPID, PPID: h.anchorRow.PID, PGID: fakeHookPID, StartUnix: h.anchorRow.StartUnix, Comm: "sleep",
			Argv: []string{"sleep", "1"}, CWD: h.root, RuntimeS: 180, Ancestry: []resourceAncestor{},
		},
		Metrics: resourceMetrics{CPU: true},
	})
	if err != nil {
		h.t.Fatal(err)
	}
	request := h.sessionRequest(resourceEvent)
	request.PayloadRaw = string(payload)
	request.ClientPID, request.ClientPPID = os.Getpid(), h.anchorRow.PID
	ctx, cancel := context.WithTimeout(withHostDispatch(context.Background()), integrationWait)
	defer cancel()
	response, err := h.manager.dispatch(ctx, request)
	if err != nil || response.Status != "ok" || response.Exit != 0 {
		h.fail("warm-up dispatch = %+v, %v", response, err)
	}
	h.drainBackground()
}

func (h *pythonMonitorHarness) drainBackground() {
	deadline := time.Now().Add(integrationWait)
	for h.backgroundTickets() != 0 && time.Now().Before(deadline) {
		time.Sleep(5 * time.Millisecond)
	}
}

func (h *pythonMonitorHarness) realArgv(pid int) []string {
	h.t.Helper()
	argv, ok := h.real.argv(pid)
	if !ok {
		h.t.Fatalf("argv of pid %d is unreadable", pid)
	}
	return argv
}

func (h *pythonMonitorHarness) spawnAnchor() {
	h.t.Helper()
	anchor := exec.Command("/bin/sh", "-c", fmt.Sprintf("sleep %d & wait", int(childLifetime.Seconds())))
	anchor.Args[0] = "claude"
	anchor.Dir = h.root
	anchor.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := anchor.Start(); err != nil {
		h.t.Fatal(err)
	}
	h.anchor = anchor
	h.t.Cleanup(h.reapAnchor)
	row, ok := h.real.probe(anchor.Process.Pid)
	if !ok {
		h.t.Fatalf("anchor pid %d vanished", anchor.Process.Pid)
	}
	h.anchorRow = row
	deadline := time.Now().Add(10 * time.Second)
	for {
		table, err := h.real.snapshot()
		if err != nil {
			h.t.Fatal(err)
		}
		for _, candidate := range table {
			if candidate.PPID == row.PID && candidate.Comm == "sleep" {
				h.child = candidate
				return
			}
		}
		if time.Now().After(deadline) {
			h.t.Fatal("the anchor never started its sleep child")
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func (h *pythonMonitorHarness) reapAnchor() {
	if current, ok := h.real.probe(h.child.PID); ok && current.identity() == h.child.identity() {
		_ = syscall.Kill(h.child.PID, syscall.SIGKILL)
	}
	done := make(chan error, 1)
	go func() { done <- h.anchor.Wait() }()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		_ = h.anchor.Process.Kill()
		<-done
	}
}

func (h *pythonMonitorHarness) startWorker(ctx context.Context, key workerKey) (*workerClient, error) {
	pair, err := syscall.Socketpair(syscall.AF_UNIX, syscall.SOCK_STREAM, 0)
	if err != nil {
		return nil, err
	}
	parent := os.NewFile(uintptr(pair[0]), "event-parent")
	child := os.NewFile(uintptr(pair[1]), "event-child")
	conn, err := net.FileConn(parent)
	_ = parent.Close()
	if err != nil {
		_ = child.Close()
		return nil, err
	}
	spec := workerCmd(key, h.python)
	command := exec.Command(spec.Path, spec.Args...)
	command.Dir = spec.Dir
	for _, item := range spec.Env {
		if !strings.HasPrefix(item, "HOME=") {
			command.Env = append(command.Env, item)
		}
	}
	command.Env = append(command.Env, "HOME="+h.home)
	process := &pythonWorkerProcess{command: command, stderr: &bytes.Buffer{}, done: make(chan error, 1)}
	command.Stdin, command.Stdout, command.Stderr = child, child, process.stderr
	if err := command.Start(); err != nil {
		_ = child.Close()
		_ = conn.Close()
		return nil, err
	}
	_ = child.Close()
	go func() { process.done <- command.Wait() }()
	h.mu.Lock()
	h.workers = append(h.workers, process)
	h.mu.Unlock()
	worker, err := handshakeWorker(ctx, conn, h.build)
	if err != nil {
		_ = conn.Close()
		return nil, fmt.Errorf("real Python handshake: %w", err)
	}
	worker.python = h.python
	h.mu.Lock()
	h.worker = worker
	h.mu.Unlock()
	return worker, nil
}

func (h *pythonMonitorHarness) reapWorkers() {
	h.mu.Lock()
	workers := h.workers
	h.mu.Unlock()
	for _, process := range workers {
		select {
		case err := <-process.done:
			if err != nil {
				h.t.Errorf("real Python worker: %v\n%s", err, process.stderr.String())
			}
		case <-time.After(10 * time.Second):
			_ = process.command.Process.Kill()
			<-process.done
			h.t.Errorf("real Python worker did not exit on pipe EOF\n%s", process.stderr.String())
		}
	}
}

func (h *pythonMonitorHarness) dispatch(ctx context.Context, request wireproto.EventRequest) (wireproto.EventResponse, error) {
	var payload struct {
		Stage string `json:"stage"`
	}
	if err := json.Unmarshal([]byte(request.PayloadRaw), &payload); err != nil {
		h.t.Errorf("payload %q: %v", request.PayloadRaw, err)
	}
	response, err := h.manager.dispatch(ctx, request)
	h.mu.Lock()
	h.stages = append(h.stages, stageOutcome{stage: payload.Stage, response: response, err: err})
	h.mu.Unlock()
	return response, err
}

func (h *pythonMonitorHarness) outcomes() []stageOutcome {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]stageOutcome(nil), h.stages...)
}

func (h *pythonMonitorHarness) sessionRequest(event string) wireproto.EventRequest {
	request := testEventRequest(event)
	request.Root, request.CWD = h.root, h.root
	request.Env = maps.Clone(h.env)
	request.ClientPID, request.ClientPPID = fakeHookPID, fakeShellPID
	payload, err := json.Marshal(map[string]string{
		"hook_event_name": event, "session_id": "s1", "transcript_path": filepath.Join(h.root, "transcript.jsonl"),
	})
	if err != nil {
		h.t.Fatal(err)
	}
	request.PayloadRaw = string(payload)
	return request
}

func (h *pythonMonitorHarness) sessions() int {
	h.monitor.mu.Lock()
	defer h.monitor.mu.Unlock()
	return len(h.monitor.sessions)
}

func (h *pythonMonitorHarness) childState() childState {
	h.monitor.mu.Lock()
	defer h.monitor.mu.Unlock()
	session, ok := h.monitor.sessions["s1"]
	if !ok {
		return childDone
	}
	child, ok := session.tracked[h.child.PID]
	if !ok {
		return childObserving
	}
	return child.state
}

func (h *pythonMonitorHarness) register() {
	h.t.Helper()
	h.monitor.observe(h.sessionRequest("PreToolUse"), fakeHookPID)
	if h.sessions() != 1 {
		h.t.Fatal("the session did not register")
	}
	cwd, ok := h.real.cwd(h.child.PID)
	if !ok {
		h.t.Fatalf("cwd of pid %d is unreadable", h.child.PID)
	}
	h.source.add(h.child, h.realArgv(h.child.PID)...)
	h.source.setCwd(h.child.PID, cwd)
	h.source.setUsage(h.child.PID, h.usage)
}

func (h *pythonMonitorHarness) tick() {
	h.t.Helper()
	interval := h.monitor.settings.SampleInterval
	h.clock.Advance(interval)
	h.usage.CPUSeconds += interval.Seconds()
	h.source.setUsage(h.child.PID, h.usage)
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

func (h *pythonMonitorHarness) waitFor(condition func() bool, failure string) {
	h.t.Helper()
	deadline := time.Now().Add(integrationWait)
	for !condition() {
		if time.Now().After(deadline) {
			h.fail("%s", failure)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func (h *pythonMonitorHarness) fail(format string, args ...any) {
	h.t.Helper()
	h.t.Fatalf("%s\nstages: %s\ncalls: %s\nworker logs: %s", fmt.Sprintf(format, args...), h.describeStages(), h.calls(), h.workerLogs())
}

func (h *pythonMonitorHarness) describeStages() string {
	var described []string
	for _, outcome := range h.outcomes() {
		described = append(described, fmt.Sprintf("%s: err=%v status=%q exit=%d stdout=%q stderr=%q",
			outcome.stage, outcome.err, outcome.response.Status, outcome.response.Exit, outcome.response.Stdout, outcome.response.Stderr))
	}
	return strings.Join(described, "\n")
}

func (h *pythonMonitorHarness) workerLogs() string {
	var logs []string
	for _, dir := range []string{h.env["CAPTAIN_HOOK_LOG_DIR"], h.env["CAPTAIN_HOOK_STATE_DIR"]} {
		logs = append(logs, h.tails(dir)...)
	}
	return strings.Join(logs, "\n")
}

func (h *pythonMonitorHarness) tails(dir string) []string {
	var logs []string
	_ = filepath.WalkDir(dir, func(path string, entry os.DirEntry, err error) error {
		if err != nil || entry.IsDir() {
			return nil
		}
		content, _ := os.ReadFile(path)
		lines := strings.Split(strings.TrimSpace(string(content)), "\n")
		if len(lines) > 60 {
			lines = lines[len(lines)-60:]
		}
		logs = append(logs, path+":\n"+strings.Join(lines, "\n"))
		return nil
	})
	return logs
}

func (h *pythonMonitorHarness) calls() string {
	calls, _ := os.ReadFile(h.callLog)
	return string(calls)
}

func (h *pythonMonitorHarness) stage(index int) (stageOutcome, bool) {
	outcomes := h.outcomes()
	if len(outcomes) <= index {
		return stageOutcome{}, false
	}
	return outcomes[index], true
}

func (h *pythonMonitorHarness) abandonedIDs() int {
	h.mu.Lock()
	worker := h.worker
	h.mu.Unlock()
	if worker == nil {
		return -1
	}
	worker.mu.Lock()
	defer worker.mu.Unlock()
	return len(worker.abandoned)
}

func (h *pythonMonitorHarness) backgroundTickets() int {
	h.mu.Lock()
	worker := h.worker
	h.mu.Unlock()
	if worker == nil {
		return -1
	}
	worker.mu.Lock()
	defer worker.mu.Unlock()
	return len(worker.background)
}

func (h *pythonMonitorHarness) warnThenHoldTheJudge() {
	h.t.Helper()
	h.register()
	h.tick()
	h.tick()
	h.tick()
	h.waitFor(func() bool { _, ok := h.stage(0); return ok }, "the warn stage never returned")
	if warn, _ := h.stage(0); warn.stage != "warn" || warn.err != nil || !isProceedAck(warn.response) {
		h.fail("warn stage = %+v", warn)
	}
	h.waitFor(func() bool { return h.childState() == childGrace }, "the warn ack never started the grace period")
	h.tick()
	h.tick()
	h.waitFor(func() bool { return strings.Contains(h.calls(), "start -p") }, "the judge never reached the fake claude")
	if h.childState() != childJudgePending || strings.Contains(h.calls(), "done -p") {
		h.fail("judge state = %v", h.childState())
	}
}

func (h *pythonMonitorHarness) signals() (string, bool) {
	content, err := os.ReadFile(h.signalLog)
	if errors.Is(err, os.ErrNotExist) {
		return "", false
	}
	if err != nil {
		h.t.Fatal(err)
	}
	return string(content), true
}

func TestMonitorRealPythonWorkerAbandonsTheJudgeBeforeItsSignal(t *testing.T) {
	t.Run("abandoned", func(t *testing.T) {
		h := newPythonMonitorHarness(t)
		h.warnThenHoldTheJudge()
		h.monitor.observe(h.sessionRequest("SessionEnd"), fakeHookPID)
		if h.sessions() != 0 {
			t.Fatal("the session stayed registered")
		}
		h.waitFor(func() bool { _, ok := h.stage(1); return ok }, "the judge dispatch never returned")
		if judge, _ := h.stage(1); judge.stage != "judge" || !errors.Is(judge.err, context.Canceled) {
			h.fail("judge stage = %+v", judge)
		}
		h.waitFor(func() bool { return strings.Contains(h.calls(), "done -p") }, "the fake claude never finished")
		h.waitFor(func() bool { return h.abandonedIDs() == 0 }, "the worker's reply for the abandoned id never arrived")
		if content, exists := h.signals(); exists {
			h.fail("the abandoned judge signalled: %q", content)
		}
	})
	t.Run("control", func(t *testing.T) {
		h := newPythonMonitorHarness(t)
		h.warnThenHoldTheJudge()
		h.waitFor(func() bool { _, ok := h.stage(1); return ok }, "the judge dispatch never returned")
		if judge, _ := h.stage(1); judge.stage != "judge" || judge.err != nil || judge.response.Status != "ok" {
			h.fail("judge stage = %+v", judge)
		}
		h.waitFor(func() bool { return h.childState() == childJudged }, "the judge reply was never applied")
		content, exists := h.signals()
		if want := fmt.Sprintf("%d %d\n", h.child.PID, syscall.SIGTERM); !exists || content != want {
			h.fail("signal log = %q (exists %t), want %q", content, exists, want)
		}
	})
}
