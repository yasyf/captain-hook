package hookd

import (
	"errors"
	"maps"
	"sync"
	"testing"

	"github.com/yasyf/captain-hook/internal/wireproto"
)

type fakeProcSource struct {
	mu          sync.Mutex
	rows        map[int]procRow
	usages      map[int]procUsage
	argvs       map[int][]string
	cwds        map[int]string
	snapshots   int
	usageReads  int
	argvReads   int
	usageGate   chan struct{}
	snapshotErr error
}

func newFakeProcSource() *fakeProcSource {
	return &fakeProcSource{
		rows: make(map[int]procRow), usages: make(map[int]procUsage),
		argvs: make(map[int][]string), cwds: make(map[int]string),
	}
}

func (f *fakeProcSource) add(row procRow, argv ...string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.rows[row.PID] = row
	f.argvs[row.PID] = argv
}

func (f *fakeProcSource) remove(pid int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.rows, pid)
	delete(f.usages, pid)
	delete(f.argvs, pid)
	delete(f.cwds, pid)
}

func (f *fakeProcSource) setUsage(pid int, usage procUsage) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.usages[pid] = usage
}

func (f *fakeProcSource) clearUsage(pid int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	delete(f.usages, pid)
}

func (f *fakeProcSource) setCwd(pid int, cwd string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.cwds[pid] = cwd
}

func (f *fakeProcSource) holdUsage(gate chan struct{}) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.usageGate = gate
}

func (f *fakeProcSource) reads() (usage, argv int) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.usageReads, f.argvReads
}

func (f *fakeProcSource) snapshot() (map[int]procRow, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.snapshots++
	if f.snapshotErr != nil {
		return nil, f.snapshotErr
	}
	return maps.Clone(f.rows), nil
}

func (f *fakeProcSource) probe(pid int) (procRow, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	row, ok := f.rows[pid]
	return row, ok
}

func (f *fakeProcSource) usage(pid int) (procUsage, bool) {
	f.mu.Lock()
	f.usageReads++
	gate := f.usageGate
	usage, ok := f.usages[pid]
	f.mu.Unlock()
	if gate != nil {
		<-gate
	}
	return usage, ok
}

func (f *fakeProcSource) argv(pid int) ([]string, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.argvReads++
	argv, ok := f.argvs[pid]
	return argv, ok
}

func (f *fakeProcSource) cwd(pid int) (string, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	cwd, ok := f.cwds[pid]
	return cwd, ok
}

var errFakeSnapshot = errors.New("fake snapshot refused")

func testResourceMonitor(t *testing.T, manager *workerManager) *resourceMonitor {
	t.Helper()
	settings, err := wireproto.ParseResourceSettings(nil)
	if err != nil {
		t.Fatal(err)
	}
	return newResourceMonitor(manager, newFakeProcSource(), settings)
}
