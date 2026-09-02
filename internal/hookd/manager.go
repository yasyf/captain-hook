package hookd

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/yasyf/daemonkit"
)

const (
	workerReadinessTimeout  = 10 * time.Second
	workerSettlementTimeout = 5 * time.Second

	maxLiveWorkers = 64

	// workerIdleTTL retires an interpreter no dispatch has wanted for this
	// long. The cache is keyed on root, so a machine whose roots churn —
	// per-invocation scratch checkouts, short-lived worktrees — mints keys that
	// are never revisited, and without a TTL their interpreters are held until
	// the daemon restarts.
	workerIdleTTL = 30 * time.Minute

	workerSweepInterval = 5 * time.Minute

	minParallelDispatch = 16

	// No wider budget can reach more interpreters than the worker cache holds.
	maxParallelDispatch = maxLiveWorkers

	// Nothing awaits a background dispatch, so it never competes for the blocking budget.
	asyncParallelDispatch = 4

	parallelCeilingVar = "CAPT_HOOK_MAX_PARALLEL"
)

// ErrWorkerCapacity refuses a dispatch that can neither start a worker nor
// reuse one. One worker is cached per distinct {root, python, build, semantic
// environment} tuple, so the natural population is the number of projects a
// machine runs hooks in at once. A full cache is normally resolved by evicting
// its least recently used idle interpreter — a machine whose roots churn fills
// every slot with keys no dispatch will ask for again — so this is reached only
// when all maxLiveWorkers are in flight at once. Admission is then refused
// rather than queued: a hook that waits behind a full cache stalls the tool
// call that triggered it, and a hook that is told why does not.
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

	// lastUsed and inflight are guarded by workerManager.mu. inflight counts
	// dispatches holding this entry, so eviction never pulls an interpreter out
	// from under a call in progress.
	lastUsed  time.Time
	inflight  int
	ephemeral bool
}

// idle reports whether the entry can be retired right now: it has finished
// starting and no dispatch holds it. An entry mid-start is never evicted —
// the caller waiting on ready would be left with a worker that is going away.
func (e *workerEntry) idle() bool {
	select {
	case <-e.ready:
	default:
		return false
	}
	return e.inflight == 0
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

	now  func() time.Time
	done chan struct{}

	mu      sync.Mutex
	closed  bool
	entries map[string]*workerEntry
	wg      sync.WaitGroup
}

func newWorkerManager(owner daemonkit.Ctx, logWriter io.Writer) (*workerManager, error) {
	ceiling, err := parallelCeiling(os.Getenv(parallelCeilingVar))
	if err != nil {
		return nil, err
	}
	return &workerManager{
		owner: owner, logWriter: logWriter,
		// A ceiling set below the floor is meant, so the floor follows it down.
		scheduler: newScheduler(min(minParallelDispatch, ceiling), ceiling, asyncParallelDispatch),
		entries:   make(map[string]*workerEntry),
		now:       time.Now, done: make(chan struct{}),
	}, nil
}

func parallelCeiling(override string) (int, error) {
	if strings.TrimSpace(override) == "" {
		return maxParallelDispatch, nil
	}
	parsed, err := strconv.Atoi(strings.TrimSpace(override))
	if err != nil {
		return 0, fmt.Errorf("captain: %s must be a positive integer, got %q", parallelCeilingVar, override)
	}
	if parsed <= 0 {
		return 0, fmt.Errorf("captain: %s must be positive, got %d", parallelCeilingVar, parsed)
	}
	return parsed, nil
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
	laneKey := key.id + "\x00" + session + "\x00" + strconv.FormatBool(request.Async)
	return m.scheduler.run(ctx, laneKey, request.Async, func() (EventResponse, error) {
		entry, err := m.acquire(ctx, key)
		if err != nil {
			return EventResponse{}, err
		}
		defer m.release(entry)
		worker := entry.worker
		response, err := worker.call(ctx, request)
		if err != nil {
			m.retire(key.id, worker)
			return EventResponse{}, errors.Join(err, m.settle(worker))
		}
		return response, nil
	})
}

// acquire hands back the cached entry for key, starting its interpreter on
// first use, and marks it in flight for the caller — release drops that hold. A
// full cache evicts its least recently used idle entry rather than refusing
// outright: admission is refused only when every live worker is busy, which is
// real saturation rather than the accumulated residue of keys that never
// repeat.
func (m *workerManager) acquire(ctx context.Context, key workerKey) (*workerEntry, error) {
	m.mu.Lock()
	if m.closed {
		m.mu.Unlock()
		return nil, errors.New("captain: worker manager is closed")
	}
	if entry := m.entries[key.id]; entry != nil {
		entry.lastUsed = m.now()
		entry.inflight++
		m.mu.Unlock()
		select {
		case <-entry.ready:
		case <-ctx.Done():
			m.release(entry)
			return nil, ctx.Err()
		}
		if entry.err != nil {
			m.release(entry)
			return nil, entry.err
		}
		return entry, nil
	}
	var evicted *workerClient
	if len(m.entries) >= maxLiveWorkers {
		victim := m.evictIdleLocked()
		if victim == nil {
			live := len(m.entries)
			m.mu.Unlock()
			return nil, fmt.Errorf("%w: %d live workers, limit is %d", ErrWorkerCapacity, live, maxLiveWorkers)
		}
		evicted = victim.worker
	}
	entry := &workerEntry{
		ready: make(chan struct{}), key: key,
		lastUsed: m.now(), inflight: 1, ephemeral: ephemeralRoot(key.root),
	}
	m.entries[key.id] = entry
	m.mu.Unlock()

	if evicted != nil {
		_ = m.settle(evicted)
	}

	worker, err := m.start(ctx, key)
	m.mu.Lock()
	entry.worker, entry.err = worker, err
	if err != nil {
		delete(m.entries, key.id)
	}
	close(entry.ready)
	m.mu.Unlock()
	if err != nil {
		m.release(entry)
		return nil, err
	}
	return entry, nil
}

// release drops the caller's hold on entry. An ephemeral entry — one keyed on a
// scratch root that will never be revisited — retires the moment its last
// dispatch finishes rather than waiting for the sweep to notice it.
func (m *workerManager) release(entry *workerEntry) {
	m.mu.Lock()
	entry.inflight--
	retire := entry.ephemeral && entry.inflight == 0
	if retire {
		if cached := m.entries[entry.key.id]; cached == entry {
			delete(m.entries, entry.key.id)
		}
	}
	worker := entry.worker
	m.mu.Unlock()
	if retire && worker != nil {
		_ = m.settle(worker)
	}
}

// evictIdleLocked removes the least recently used idle entry and returns it for
// the caller to stop off the lock. Returns nil only when every entry is still
// starting or in flight, which is the one state that still refuses admission —
// an entry whose interpreter never started is nil-worker but still a freed
// slot, so the entry rather than its worker is what reports the eviction.
func (m *workerManager) evictIdleLocked() *workerEntry {
	var victim *workerEntry
	for _, entry := range m.entries {
		if !entry.idle() {
			continue
		}
		if victim == nil || entry.lastUsed.Before(victim.lastUsed) {
			victim = entry
		}
	}
	if victim == nil {
		return nil
	}
	delete(m.entries, victim.key.id)
	return victim
}

// sweep retires every cached entry that no longer earns its interpreter: one
// idle past workerIdleTTL, and one keyed on a root that has since been deleted
// — a reaped scratch directory or a removed worktree. Returns the workers it
// removed so the caller stops them off the lock.
func (m *workerManager) sweep(now time.Time) []*workerClient {
	m.mu.Lock()
	defer m.mu.Unlock()
	var retired []*workerClient
	for id, entry := range m.entries {
		if !entry.idle() {
			continue
		}
		if rootExists(entry.key.root) && now.Sub(entry.lastUsed) < workerIdleTTL {
			continue
		}
		delete(m.entries, id)
		if entry.worker != nil {
			retired = append(retired, entry.worker)
		}
	}
	return retired
}

// startSweeper runs the idle sweep until Close. The daemon owns this loop; unit
// tests drive sweep directly so the cache stays deterministic under -race.
func (m *workerManager) startSweeper(interval time.Duration) {
	m.wg.Add(1)
	go func() {
		defer m.wg.Done()
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-m.done:
				return
			case <-ticker.C:
				for _, worker := range m.sweep(m.now()) {
					_ = m.settle(worker)
				}
			}
		}
	}()
}

// rootExists treats anything but a definite absence as present: a permissions
// or I/O blip must not retire a live project's interpreter.
func rootExists(root string) bool {
	_, err := os.Stat(root)
	return !errors.Is(err, fs.ErrNotExist)
}

// ephemeralRoot reports whether root lives inside the system temp directory. A
// scratch root there is a tool's per-invocation working copy — it is never
// revisited, so an interpreter cached against one holds a slot that only
// eviction can ever reclaim.
func ephemeralRoot(root string) bool {
	tmp := os.TempDir()
	if resolved, err := filepath.EvalSymlinks(tmp); err == nil {
		tmp = resolved
	}
	rel, err := filepath.Rel(tmp, root)
	if err != nil || rel == "." {
		return false
	}
	return rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
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

// workerCmd runs the worker with -P, so the session repo the worker's Dir names
// stays off sys.path: a directory there sharing an installed dependency's name
// otherwise shadows it, failing the import inside the worker thread where no
// hook response can carry it.
func workerCmd(key workerKey) daemonkit.Cmd {
	return daemonkit.Cmd{
		Path: key.python, Args: []string{"-P", "-m", "captain_hook.worker"}, Dir: workerDir(key.root),
		Env:     mergeEnvironment(workerBaseEnvironment(os.Environ()), key.env),
		Session: true,
		Exec:    daemonkit.ServingSameUser(),
	}
}

// workerDir is the root, or the temp directory once that root is gone. A session
// outlives the workspace it was started in — a deleted worktree, a reaped Orca
// workspace — and spawning into a missing Dir fails the chdir, which posix_spawn
// reports as a missing interpreter, so the dispatch dies before a worker exists.
// An empty directory detects the same languages a deleted one would.
func workerDir(root string) string {
	if info, err := os.Stat(root); err == nil && info.IsDir() {
		return root
	}
	return os.TempDir()
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
	if !m.closed {
		m.closed = true
		close(m.done)
	}
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
