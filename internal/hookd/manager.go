package hookd

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/yasyf/daemonkit/proc"
	"github.com/yasyf/daemonkit/supervise"
)

const workerReadinessTimeout = 10 * time.Second

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
	pool      *supervise.Pool
	reaper    *proc.Reaper
	logWriter io.Writer
	scheduler *scheduler

	mu      sync.Mutex
	closed  bool
	entries map[string]*workerEntry
	wg      sync.WaitGroup
}

func newWorkerManager(pool *supervise.Pool, reaper *proc.Reaper, logWriter io.Writer) *workerManager {
	return &workerManager{
		pool: pool, reaper: reaper, logWriter: logWriter, scheduler: newScheduler(16),
		entries: make(map[string]*workerEntry),
	}
}

func (m *workerManager) recover(ctx context.Context) error {
	if err := m.pool.Recover(ctx); err != nil {
		return err
	}
	_, err := m.reaper.RecoverReapReceipts(
		ctx, proc.RecoveryTask, func(context.Context, proc.ReapReceipt) error { return nil },
	)
	return err
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
			stopErr := worker.stop(context.Background())
			return EventResponse{}, errors.Join(err, stopErr)
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

func (m *workerManager) start(ctx context.Context, key workerKey) (*workerClient, error) {
	ready := make(chan *workerClient, 1)
	process, err := m.pool.StartSession(ctx, supervise.SessionProcessSpec{
		RecoveryClass: proc.RecoveryTask,
		Path:          key.python, Args: []string{"-m", "captain_hook.worker"}, Dir: key.root,
		Env: mergeEnvironment(workerBaseEnvironment(os.Environ()), key.env), Stderr: m.logWriter,
		ReadinessTimeout: workerReadinessTimeout,
		Ready: func(readyCtx context.Context, _ proc.Record, conn net.Conn) error {
			worker, err := handshakeWorker(readyCtx, conn, key.build)
			if err == nil {
				ready <- worker
			}
			return err
		},
	})
	if err != nil {
		return nil, fmt.Errorf("captain: start Python product worker: %w", err)
	}
	worker := <-ready
	worker.process = process
	m.wg.Add(1)
	go m.watch(key.id, worker, process)
	return worker, nil
}

func (m *workerManager) watch(id string, worker *workerClient, process *supervise.SessionProcess) {
	defer m.wg.Done()
	err := process.Wait(context.Background())
	worker.fail(err)
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
		if entry.worker == nil || entry.worker.process == nil {
			continue
		}
		result = append(result, workerStatus{
			Key: entry.key.id, Root: entry.key.root, Build: entry.key.build,
			Python: entry.key.python, PID: entry.worker.process.Record().PID,
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
	var errs []error
	for _, worker := range workers {
		errs = append(errs, worker.stop(ctx))
	}
	return errors.Join(errs...)
}

func (m *workerManager) Close() {
	m.mu.Lock()
	m.closed = true
	m.mu.Unlock()
	m.pool.Close()
}

func (m *workerManager) Cancel() { m.pool.Cancel() }

func (m *workerManager) Wait(ctx context.Context) error {
	poolErr := m.pool.Wait(ctx)
	done := make(chan struct{})
	go func() { m.wg.Wait(); close(done) }()
	select {
	case <-done:
		return poolErr
	case <-ctx.Done():
		<-done
		return errors.Join(poolErr, ctx.Err())
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

func workerBaseEnvironment(environ []string) []string {
	base := make([]string, 0, len(environ))
	for _, item := range environ {
		name, _, ok := strings.Cut(item, "=")
		if !ok {
			continue
		}
		if name == "XDG_CACHE_HOME" || strings.HasPrefix(name, "CAPT_HOOK_") ||
			strings.HasPrefix(name, "CAPTAIN_HOOK_") || strings.HasPrefix(name, "HOOKS_") ||
			strings.HasPrefix(name, "CLAUDE_") || strings.HasPrefix(name, "FACTORY_") {
			continue
		}
		base = append(base, item)
	}
	return base
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
