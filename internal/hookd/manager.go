package hookd

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/yasyf/daemonkit"
)

const (
	workerReadinessTimeout  = 10 * time.Second
	workerSettlementTimeout = 5 * time.Second

	maxLiveWorkers = 64
)

// ErrWorkerCapacity refuses a dispatch that would start a worker past the live
// cache bound. One worker is cached per distinct {root, python, build, semantic
// environment} tuple, so the natural population is the number of projects a
// machine runs hooks in at once — reaching maxLiveWorkers means either far more
// than that or a key that is churning, and either way the machine is already
// carrying 64 live Python interpreters. Admission is refused rather than
// queued: a hook that waits behind a full cache stalls the tool call that
// triggered it, and a hook that is told why does not.
var ErrWorkerCapacity = errors.New("captain: live worker capacity is exhausted")

var workerEnvExact = map[string]struct{}{
	"XDG_CACHE_HOME": {}, "CAPTAIN_HOOK_STATE_DIR": {}, "CAPTAIN_HOOK_LOG_DIR": {},
	"CAPTAIN_HOOK_TASKS_DIR": {}, "CAPT_HOOK_DECISIONS_DB": {},
}

type workerEntry struct {
	ready  chan struct{}
	worker *workerClient
	err    error
	key    workerKey
}

type workerKey struct {
	id     string
	root   string
	python string
	build  string
	env    map[string]string
}

type workerManager struct {
	owner     daemonkit.Ctx
	logWriter io.Writer
	scheduler *scheduler

	mu      sync.Mutex
	closed  bool
	entries map[string]*workerEntry
	wg      sync.WaitGroup
}

func newWorkerManager(owner daemonkit.Ctx, logWriter io.Writer) *workerManager {
	return &workerManager{
		owner: owner, logWriter: logWriter, scheduler: newScheduler(16),
		entries: make(map[string]*workerEntry),
	}
}

func (m *workerManager) dispatch(ctx context.Context, request EventRequest) (EventResponse, error) {
	if err := validateEventRequest(request); err != nil {
		return EventResponse{}, err
	}
	key, err := makeWorkerKey(request)
	if err != nil {
		return EventResponse{}, err
	}
	session := sessionID(request.PayloadRaw)
	if session == "" {
		session = fmt.Sprintf("pid:%d", request.ClientPID)
	}
	laneKey := key.id + "\x00" + session
	return m.scheduler.run(ctx, laneKey, func() (EventResponse, error) {
		worker, err := m.worker(ctx, key)
		if err != nil {
			return EventResponse{}, err
		}
		response, err := worker.call(ctx, request)
		if err != nil {
			m.retire(key.id, worker)
			return EventResponse{}, errors.Join(err, m.settle(worker))
		}
		return response, nil
	})
}

func (m *workerManager) worker(ctx context.Context, key workerKey) (*workerClient, error) {
	m.mu.Lock()
	if m.closed {
		m.mu.Unlock()
		return nil, errors.New("captain: worker manager is closed")
	}
	if entry := m.entries[key.id]; entry != nil {
		m.mu.Unlock()
		select {
		case <-entry.ready:
			return entry.worker, entry.err
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	if len(m.entries) >= maxLiveWorkers {
		live := len(m.entries)
		m.mu.Unlock()
		return nil, fmt.Errorf("%w: %d live workers, limit is %d", ErrWorkerCapacity, live, maxLiveWorkers)
	}
	entry := &workerEntry{ready: make(chan struct{}), key: key}
	m.entries[key.id] = entry
	m.mu.Unlock()

	worker, err := m.start(ctx, key)
	m.mu.Lock()
	entry.worker, entry.err = worker, err
	if err != nil {
		delete(m.entries, key.id)
	}
	close(entry.ready)
	m.mu.Unlock()
	return worker, err
}

// start spawns one Python worker on ChannelStdio: daemonkit joins the child's
// stdin and stdout into one deadline-aware conn, drains its stderr into the
// host log for the child's whole life, and records the process durably under
// the daemon's own ownership scope before the child runs an instruction. The
// exec posture is the named waiver — the executable is whatever interpreter
// the requesting project points at. Session gives the worker its own session,
// so settlement covers the hook subprocesses it spawns and not just the
// interpreter.
func (m *workerManager) start(ctx context.Context, key workerKey) (*workerClient, error) {
	readyCtx, cancel := context.WithTimeout(ctx, workerReadinessTimeout)
	defer cancel()
	child, err := m.owner.Spawn(readyCtx, workerCmd(key), daemonkit.ChannelStdio, m.logWriter)
	if err != nil {
		return nil, fmt.Errorf("captain: spawn Python product worker: %w", err)
	}
	conn, err := child.Conn()
	if err != nil {
		return nil, m.stopChild(child, errors.New("captain: take Python worker channel"), err)
	}
	worker, err := handshakeWorker(readyCtx, conn, key.build)
	if err != nil {
		_ = conn.Close()
		return nil, m.stopChild(child, errors.New("captain: handshake Python product worker"), err)
	}
	worker.child = child
	m.wg.Add(1)
	go m.watch(key.id, worker, child)
	return worker, nil
}

func workerCmd(key workerKey) daemonkit.Cmd {
	return daemonkit.Cmd{
		Path: key.python, Args: []string{"-m", "captain_hook.worker"}, Dir: key.root,
		Env:     mergeEnvironment(workerBaseEnvironment(os.Environ()), key.env),
		Session: true,
		Exec:    daemonkit.ServingSameUser(),
	}
}

// settle terminates one retired worker on a budget of its own: the request
// context that just failed carries a spent deadline, and Child.Stop refuses a
// context without one — which would retire the child from the map without ever
// signalling it.
func (m *workerManager) settle(worker *workerClient) error {
	stopCtx, cancel := context.WithTimeout(context.Background(), workerSettlementTimeout)
	defer cancel()
	return worker.stop(stopCtx)
}

func (m *workerManager) stopChild(child *daemonkit.Child, message, cause error) error {
	stopCtx, cancel := context.WithTimeout(context.Background(), workerSettlementTimeout)
	defer cancel()
	_, stopErr := child.Stop(stopCtx)
	return errors.Join(message, cause, stopErr, child.StderrErr())
}

func (m *workerManager) watch(id string, worker *workerClient, child *daemonkit.Child) {
	defer m.wg.Done()
	exit := <-child.Done()
	var exitErr error
	if exit.Signal != 0 {
		exitErr = fmt.Errorf("captain: Python worker died on signal %s", exit.Signal)
	} else if exit.Code != 0 {
		exitErr = fmt.Errorf("captain: Python worker exited with status %d", exit.Code)
	}
	worker.fail(errors.Join(exitErr, child.StderrErr()))
	m.mu.Lock()
	if entry := m.entries[id]; entry != nil && entry.worker == worker {
		delete(m.entries, id)
	}
	m.mu.Unlock()
}

func (m *workerManager) retire(id string, worker *workerClient) {
	m.mu.Lock()
	if entry := m.entries[id]; entry != nil && entry.worker == worker {
		delete(m.entries, id)
	}
	m.mu.Unlock()
}

func (m *workerManager) status() []workerStatus {
	m.mu.Lock()
	defer m.mu.Unlock()
	result := make([]workerStatus, 0, len(m.entries))
	for _, entry := range m.entries {
		if entry.worker == nil || entry.worker.child == nil {
			continue
		}
		result = append(result, workerStatus{
			Key: entry.key.id, Root: entry.key.root, Build: entry.key.build,
			Python: entry.key.python, PID: entry.worker.child.PID(),
		})
	}
	sort.Slice(result, func(i, j int) bool { return result[i].Key < result[j].Key })
	return result
}

func (m *workerManager) restart(ctx context.Context) error {
	m.mu.Lock()
	workers := make([]*workerClient, 0, len(m.entries))
	for _, entry := range m.entries {
		if entry.worker != nil {
			workers = append(workers, entry.worker)
		}
	}
	m.entries = make(map[string]*workerEntry)
	m.mu.Unlock()
	stopCtx, cancel := context.WithTimeout(ctx, workerSettlementTimeout)
	defer cancel()
	var errs []error
	for _, worker := range workers {
		errs = append(errs, worker.stop(stopCtx))
	}
	return errors.Join(errs...)
}

func (m *workerManager) Close(ctx context.Context) (bool, error) {
	m.mu.Lock()
	m.closed = true
	workers := make([]*workerClient, 0, len(m.entries))
	for _, entry := range m.entries {
		if entry.worker != nil {
			workers = append(workers, entry.worker)
		}
	}
	m.entries = make(map[string]*workerEntry)
	m.mu.Unlock()
	var errs []error
	for _, worker := range workers {
		errs = append(errs, worker.stop(ctx))
	}
	done := make(chan struct{})
	go func() { m.wg.Wait(); close(done) }()
	select {
	case <-done:
		return true, errors.Join(errs...)
	case <-ctx.Done():
		return false, errors.Join(append(errs, ctx.Err())...)
	}
}

func makeWorkerKey(request EventRequest) (workerKey, error) {
	root, err := filepath.Abs(request.Root)
	if err != nil {
		return workerKey{}, fmt.Errorf("captain: resolve root: %w", err)
	}
	if resolved, err := filepath.EvalSymlinks(root); err == nil {
		root = resolved
	}
	python, err := filepath.Abs(request.Python)
	if err != nil {
		return workerKey{}, fmt.Errorf("captain: resolve Python: %w", err)
	}
	env := semanticWorkerEnvironment(request.Env)
	keys := make([]string, 0, len(env))
	for name := range env {
		keys = append(keys, name)
	}
	sort.Strings(keys)
	parts := []string{root, python, request.Build}
	for _, name := range keys {
		parts = append(parts, name+"="+env[name])
	}
	digest := sha256.Sum256([]byte(strings.Join(parts, "\x00")))
	return workerKey{
		id: hex.EncodeToString(digest[:8]), root: root, python: python,
		build: request.Build, env: env,
	}, nil
}

func semanticWorkerEnvironment(env map[string]string) map[string]string {
	result := make(map[string]string)
	for name, value := range env {
		_, exact := workerEnvExact[name]
		if exact || strings.HasPrefix(name, "HOOKS_") {
			result[name] = value
		}
	}
	return result
}

func mergeEnvironment(base []string, overrides map[string]string) []string {
	merged := make(map[string]string, len(base)+len(overrides))
	for _, item := range base {
		name, value, ok := strings.Cut(item, "=")
		if ok {
			merged[name] = value
		}
	}
	for name, value := range overrides {
		merged[name] = value
	}
	keys := make([]string, 0, len(merged))
	for name := range merged {
		keys = append(keys, name)
	}
	sort.Strings(keys)
	result := make([]string, 0, len(keys))
	for _, name := range keys {
		result = append(result, name+"="+merged[name])
	}
	return result
}

// workerBaseEnvironment is the host environment a worker inherits, with every
// name a request may set semantically stripped so only the worker key decides
// it. PATH and LANG are seeded here rather than inherited: a non-nil Cmd.Env is
// the child's exact environment and v0.21 injects nothing into it, so without
// this the worker would lose user-installed command discovery and a stable
// locale. They are stripped from the inherited set too, so no request overrides
// them and neither reaches the key.
func workerBaseEnvironment(environ []string) []string {
	base := make([]string, 0, len(environ)+2)
	base = append(base, "PATH="+parentPath(environ), "LANG=C")
	for _, item := range environ {
		name, _, ok := strings.Cut(item, "=")
		if !ok {
			continue
		}
		if name == "PATH" || name == "LANG" || name == "XDG_CACHE_HOME" || strings.HasPrefix(name, "CAPT_HOOK_") ||
			strings.HasPrefix(name, "CAPTAIN_HOOK_") || strings.HasPrefix(name, "HOOKS_") ||
			strings.HasPrefix(name, "CLAUDE_") || strings.HasPrefix(name, "FACTORY_") {
			continue
		}
		base = append(base, item)
	}
	return base
}

func parentPath(environ []string) string {
	for _, item := range environ {
		if name, value, ok := strings.Cut(item, "="); ok && name == "PATH" && value != "" {
			return value
		}
	}
	return "/usr/bin:/bin:/usr/sbin:/sbin"
}

func sessionID(payload string) string {
	var value struct {
		SessionID string `json:"session_id"`
	}
	if jsonErr := decodeStrict([]byte(payload), &value); jsonErr == nil && value.SessionID != "" {
		return value.SessionID
	}
	return ""
}
