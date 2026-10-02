//go:build darwin

package hookd

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDarwinProcSourceDescribesThisProcess(t *testing.T) {
	t.Parallel()
	source := newProcSource()
	pid := os.Getpid()
	row, ok := source.probe(pid)
	if !ok || row.PID != pid || row.PPID != os.Getppid() || row.StartUnix == 0 || row.Comm == "" {
		t.Fatalf("probe(%d) = %+v, %t", pid, row, ok)
	}
	table, err := source.snapshot()
	if err != nil {
		t.Fatal(err)
	}
	if table[pid].identity() != row.identity() || table[pid].PGID != row.PGID {
		t.Fatalf("snapshot row %+v differs from probe %+v", table[pid], row)
	}
	argv, ok := source.argv(pid)
	if !ok || len(argv) == 0 || filepath.Base(argv[0]) != filepath.Base(os.Args[0]) {
		t.Fatalf("argv(%d) = %q, %t; want this test binary", pid, argv, ok)
	}
	usage, ok := source.usage(pid)
	if !ok || !usage.CPUKnown || !usage.DiskKnown || usage.CPUSeconds <= 0 {
		t.Fatalf("usage(%d) = %+v, %t", pid, usage, ok)
	}
	cwd, ok := source.cwd(pid)
	wd, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	if !ok || mustResolve(t, cwd) != mustResolve(t, wd) {
		t.Fatalf("cwd(%d) = %q, %t; want %q", pid, cwd, ok, wd)
	}
	if _, ok := source.probe(1<<30 - 1); ok {
		t.Fatal("probe of an absent pid reported a row")
	}
	if _, ok := source.usage(1<<30 - 1); ok {
		t.Fatal("usage of an absent pid reported a reading")
	}
}

func mustResolve(t *testing.T, path string) string {
	t.Helper()
	resolved, err := filepath.EvalSymlinks(path)
	if err != nil {
		t.Fatal(err)
	}
	return resolved
}
