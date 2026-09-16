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
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
	"github.com/yasyf/daemonkit"
	"github.com/yasyf/daemonkit/artifact"
)

const (
	workerReadinessTimeout  = 10 * time.Second
	workerSettlementTimeout = 5 * time.Second

	maxLiveWorkers = 64

	maxWorkersPerRoot = 8

	serviceSmoothing = 4

	// workerIdleTTL retires an interpreter no dispatch has wanted for this
	// long. The cache is keyed on root, so a machine whose roots churn —
	// per-invocation scratch checkouts, short-lived worktrees — mints keys that
	// are never revisited, and without a TTL their interpreters are held until
	// the daemon restarts.
	workerIdleTTL = 30 * time.Minute

	workerSweepInterval = 5 * time.Minute
)

var ErrWorkerCapacity = errors.New("captain: live worker capacity is exhausted")

func workersPerRoot() int {
	if raw := os.Getenv("CAPT_HOOK_WORKERS_PER_ROOT"); raw != "" {
		if configured, err := strconv.Atoi(raw); err == nil && configured > 0 {
			return configured
		}
	}
	return max(1, min(maxWorkersPerRoot, runtime.NumCPU()/4))
}

var errWorkerManagerClosed = errors.New("captain: worker manager is closed")

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
	// the holds on this entry — every caller waiting or dispatching on it, and
	// the startup until it lands — so eviction never pulls an interpreter out
	// from under a call in progress, and the last hold to drop is the one that
	// retires an entry the cache has let go of.
	lastUsed  time.Time
	inflight  int
	ephemeral bool

	service time.Duration
}

func (e *workerEntry) started() bool {
	select {
	case <-e.ready:
		return true
	default:
		return false
	}
}

// idle reports whether the entry can be retired right now: it has finished
// starting and no dispatch holds it. An entry mid-start is never evicted —
// the caller waiting on ready would be left with a worker that is going away.
func (e *workerEntry) idle() bool {
	return e.started() && e.inflight == 0
}

func (e *workerEntry) observe(concurrency int, elapsed time.Duration) {
	sample := elapsed / time.Duration(concurrency)
	if e.service == 0 {
		e.service = sample
		return
	}
	e.service += (sample - e.service) / serviceSmoothing
}

type workerKey struct {
	id    string
	root  string
	env   map[string]string
	shard int
}

func (k workerKey) member() string {
	return k.id + "/" + strconv.Itoa(k.shard)
}

type pool struct {
	ready    *workerEntry
	starting *workerEntry
	size     int
	shard    int
}

func (m *workerManager) poolLocked(id string) pool {
	var view pool
	held := make(map[int]bool)
	for _, entry := range m.entries {
		if entry.key.id != id {
			continue
		}
		view.size++
		held[entry.key.shard] = true
		if !entry.started() {
			view.starting = entry
			continue
		}
		if view.ready == nil || entry.inflight < view.ready.inflight ||
			entry.inflight == view.ready.inflight && entry.lastUsed.After(view.ready.lastUsed) {
			view.ready = entry
		}
	}
	for held[view.shard] {
		view.shard++
	}
	return view
}

type workerManager struct {
	owner     daemonkit.Ctx
	logWriter io.Writer

	now   func() time.Time
	start func(ctx context.Context, key workerKey) (*workerClient, error)

	// lifetime bounds every worker startup and the sweeper; end cancels it at
	// Close, so no startup outlives the manager and none is ever bound to the
	// requester that happened to ask first.
	lifetime context.Context
	end      context.CancelFunc

	mu       sync.Mutex
	closed   bool
	entries  map[string]*workerEntry
	poolSize int
	wg       sync.WaitGroup
}

func newWorkerManager(owner daemonkit.Ctx, logWriter io.Writer) *workerManager {
	lifetime, end := context.WithCancel(context.Background())
	m := &workerManager{
		owner: owner, logWriter: logWriter,
		entries: make(map[string]*workerEntry), poolSize: workersPerRoot(),
		now: time.Now, lifetime: lifetime, end: end,
	}
	m.start = m.startWorker
	return m
}

func (m *workerManager) dispatch(ctx context.Context, request wireproto.EventRequest) (wireproto.EventResponse, error) {
	if err := request.Validate(); err != nil {
		return wireproto.EventResponse{}, err
	}
	key, err := makeWorkerKey(request)
	if err != nil {
		return wireproto.EventResponse{}, err
	}
	if deadline, ok := ctx.Deadline(); ok {
		request.DeadlineUnixMS = deadline.UnixMilli()
	}
	entry, err := m.acquire(ctx, key)
	if err != nil {
		return wireproto.EventResponse{}, err
	}
	defer m.release(entry)
	admitted := m.now()
	ahead, wait := m.queued(entry)
	if deadline, ok := ctx.Deadline(); ok && wait > deadline.Sub(admitted) {
		return shedResponse(ahead, wait, deadline.Sub(admitted)), nil
	}
	worker := entry.worker
	response, err := worker.call(ctx, request)
	if err != nil && worker.broken() {
		m.forget(worker)
		return wireproto.EventResponse{}, errors.Join(err, m.settle(worker))
	}
	m.mu.Lock()
	entry.observe(ahead+1, m.now().Sub(admitted))
	m.mu.Unlock()
	return response, err
}

func (m *workerManager) queued(entry *workerEntry) (int, time.Duration) {
	m.mu.Lock()
	defer m.mu.Unlock()
	ahead := entry.inflight - 1
	return ahead, time.Duration(ahead) * entry.service
}

func shedResponse(ahead int, wait, remaining time.Duration) wireproto.EventResponse {
	return wireproto.EventResponse{
		Schema: wireproto.Schema, Status: "ok",
		Stderr: fmt.Sprintf("capt-hook: %d events ahead on this worker take %s, past the %s left; no verdict\n",
			ahead, wait.Round(time.Millisecond), remaining.Round(time.Millisecond)),
	}
}

func (m *workerManager) acquire(ctx context.Context, key workerKey) (*workerEntry, error) {
	m.mu.Lock()
	if m.closed {
		m.mu.Unlock()
		return nil, errWorkerManagerClosed
	}
	var evicted *workerClient
	view := m.poolLocked(key.id)
	entry := view.ready
	switch {
	case entry == nil && view.starting != nil:
		entry = view.starting
	case entry == nil:
		if len(m.entries) >= maxLiveWorkers {
			victim := m.evictIdleLocked()
			if victim == nil {
				live := len(m.entries)
				m.mu.Unlock()
				return nil, fmt.Errorf("%w: %d live workers, limit is %d", ErrWorkerCapacity, live, maxLiveWorkers)
			}
			evicted = victim.worker
		}
		entry = m.startMemberLocked(key, view.shard)
	case entry.inflight > 0 && !entry.ephemeral && view.starting == nil &&
		view.size < m.poolSize && len(m.entries) < maxLiveWorkers:
		m.startMemberLocked(key, view.shard)
	}
	entry.lastUsed = m.now()
	entry.inflight++
	m.mu.Unlock()

	if evicted != nil {
		_ = m.settle(evicted)
	}

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

func (m *workerManager) startMemberLocked(key workerKey, shard int) *workerEntry {
	key.shard = shard
	entry := &workerEntry{
		ready: make(chan struct{}), key: key, inflight: 1, ephemeral: ephemeralRoot(key.root),
	}
	m.entries[key.member()] = entry
	m.wg.Add(1)
	go m.startEntry(entry)
	return entry
}

func (m *workerManager) startEntry(entry *workerEntry) {
	defer m.wg.Done()
	worker, err := m.start(m.lifetime, entry.key)
	m.mu.Lock()
	stranded := err == nil && m.closed
	if stranded {
		err = errWorkerManagerClosed
	} else {
		entry.worker = worker
	}
	entry.err = err
	if (err != nil || worker.broken()) && m.entries[entry.key.member()] == entry {
		delete(m.entries, entry.key.member())
	}
	close(entry.ready)
	m.mu.Unlock()
	if stranded {
		_ = m.settle(worker)
	}
	m.release(entry)
}

// release drops the caller's hold on entry. The last hold to drop retires an
// ephemeral entry — a scratch root never revisited — at once rather than at the
// sweep, and settles an entry the cache has already let go of (restart, Close,
// a dead child), so a worker still in use at that moment never leaks.
func (m *workerManager) release(entry *workerEntry) {
	m.mu.Lock()
	entry.inflight--
	cached := m.entries[entry.key.member()] == entry
	retire := entry.inflight == 0 && (entry.ephemeral || !cached)
	if retire && cached {
		delete(m.entries, entry.key.member())
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
	delete(m.entries, victim.key.member())
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
			case <-m.lifetime.Done():
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

// startWorker spawns one Python worker on ChannelStdio: daemonkit joins the child's
// stdin and stdout into one deadline-aware conn, drains its stderr into the
// host log for the child's whole life, and records the process durably under
// the daemon's own ownership scope before the child runs an instruction.
// Session gives the worker its own session, so settlement covers the hook
// subprocesses it spawns and not just the interpreter.
func (m *workerManager) startWorker(ctx context.Context, key workerKey) (*workerClient, error) {
	python, err := installedPython()
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(ctx, workerReadinessTimeout)
	defer cancel()
	child, err := m.owner.Spawn(ctx, workerCmd(key, python), daemonkit.ChannelStdio, m.logWriter)
	if err != nil {
		return nil, fmt.Errorf("captain: spawn Python product worker: %w", err)
	}
	conn, err := child.Conn()
	if err != nil {
		return nil, m.stopChild(child, errors.New("captain: take Python worker channel"), err)
	}
	worker, err := handshakeWorker(ctx, conn, Build)
	if err != nil {
		_ = conn.Close()
		return nil, m.stopChild(child, errors.New("captain: handshake Python product worker"), err)
	}
	worker.child, worker.python = child, python
	m.wg.Add(1)
	go m.watch(worker, child)
	return worker, nil
}

// workerCmd runs the worker with -P, so the session repo the worker's Dir names
// stays off sys.path: a directory there sharing an installed dependency's name
// otherwise shadows it, failing the import inside the worker thread where no
// hook response can carry it. CAPT_HOOK_WORKER_SHARD keys the worker's daemon
// log apart from its pool peers', since loguru rotates per process.
func workerCmd(key workerKey, python string) daemonkit.Cmd {
	env := mergeEnvironment(workerBaseEnvironment(os.Environ()), key.env)
	return daemonkit.Cmd{
		Path: python, Args: []string{"-P", "-m", "captain_hook.worker"}, Dir: workerDir(key.root),
		Env:     append(env, "CAPT_HOOK_WORKER_SHARD="+strconv.Itoa(key.shard)),
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
	return filepath.Clean(os.TempDir())
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

// stopAll terminates every worker at once on one shared budget. Stopped one
// after another, each settlement spent what the next one had left, and the
// tail of a long list was demanded on a deadline already gone.
func (m *workerManager) stopAll(ctx context.Context, workers []*workerClient) error {
	errs := make([]error, len(workers))
	var wg sync.WaitGroup
	for i, worker := range workers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			errs[i] = worker.stop(ctx)
		}()
	}
	wg.Wait()
	return errors.Join(errs...)
}

func (m *workerManager) stopChild(child *daemonkit.Child, message, cause error) error {
	stopCtx, cancel := context.WithTimeout(context.Background(), workerSettlementTimeout)
	defer cancel()
	_, stopErr := child.Stop(stopCtx)
	return errors.Join(message, cause, stopErr, child.StderrErr())
}

func (m *workerManager) watch(worker *workerClient, child *daemonkit.Child) {
	defer m.wg.Done()
	exit := <-child.Done()
	var exitErr error
	if exit.Signal != 0 {
		exitErr = fmt.Errorf("captain: Python worker died on signal %s", exit.Signal)
	} else if exit.Code != 0 {
		exitErr = fmt.Errorf("captain: Python worker exited with status %d", exit.Code)
	}
	worker.fail(errors.Join(exitErr, child.StderrErr()))
	m.forget(worker)
}

func (m *workerManager) forget(worker *workerClient) {
	m.mu.Lock()
	for id, entry := range m.entries {
		if entry.worker == worker {
			delete(m.entries, id)
		}
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
			Key: entry.key.id, Shard: entry.key.shard, Root: entry.key.root, Build: Build,
			Python: entry.worker.python, PID: entry.worker.child.PID(),
		})
	}
	sort.Slice(result, func(i, j int) bool {
		if result[i].Key != result[j].Key {
			return result[i].Key < result[j].Key
		}
		return result[i].Shard < result[j].Shard
	})
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
	return m.stopAll(stopCtx, workers)
}

func (m *workerManager) Close(ctx context.Context) (bool, error) {
	m.mu.Lock()
	if !m.closed {
		m.closed = true
		m.end()
	}
	workers := make([]*workerClient, 0, len(m.entries))
	for _, entry := range m.entries {
		if entry.worker != nil {
			workers = append(workers, entry.worker)
		}
	}
	m.entries = make(map[string]*workerEntry)
	m.mu.Unlock()
	stopErr := m.stopAll(ctx, workers)
	done := make(chan struct{})
	go func() { m.wg.Wait(); close(done) }()
	select {
	case <-done:
		return true, stopErr
	case <-ctx.Done():
		return false, errors.Join(stopErr, ctx.Err())
	}
}

func makeWorkerKey(request wireproto.EventRequest) (workerKey, error) {
	root, err := filepath.Abs(request.Root)
	if err != nil {
		return workerKey{}, fmt.Errorf("captain: resolve root: %w", err)
	}
	if resolved, err := filepath.EvalSymlinks(root); err == nil {
		root = resolved
	}
	env := semanticWorkerEnvironment(request.Env)
	keys := make([]string, 0, len(env))
	for name := range env {
		keys = append(keys, name)
	}
	sort.Strings(keys)
	parts := []string{root}
	for _, name := range keys {
		parts = append(parts, name+"="+env[name])
	}
	digest := sha256.Sum256([]byte(strings.Join(parts, "\x00")))
	return workerKey{id: hex.EncodeToString(digest[:8]), root: root, env: env}, nil
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
		if name == "PATH" || name == "LANG" || name == "XDG_CACHE_HOME" || name == "CEREBRAS_API_KEY" ||
			strings.HasPrefix(name, "CAPT_HOOK_") ||
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

func productToolDescriptor() *artifact.Descriptor {
	return &artifact.Descriptor{
		Schema: 1, Name: "capt-hook", Kind: artifact.PythonTool,
		Version: artifact.VersionSource{Static: Build},
		Tool:    &artifact.ToolSpec{Dist: "capt-hook", Entrypoint: "hook"},
	}
}

func installedPython() (string, error) {
	store, err := artifact.DefaultStore()
	if err != nil {
		return "", err
	}
	entries, err := store.ToolEntries()
	if err != nil {
		return "", err
	}
	for _, entry := range entries {
		if entry.Dist != "capt-hook" || entry.Version != Build || entry.InstalledAt.IsZero() {
			continue
		}
		entrypoint, err := filepath.EvalSymlinks(filepath.Join(entry.Dir, "bin", "hook"))
		if err != nil {
			return "", err
		}
		return filepath.Join(filepath.Dir(entrypoint), "python"), nil
	}
	return "", fmt.Errorf("captain: the capt-hook %s tool env is not installed; run `capt-hook helper install`", Build)
}
