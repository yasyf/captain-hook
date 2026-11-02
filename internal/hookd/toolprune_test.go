package hookd

import (
	"os"
	"path/filepath"
	"slices"
	"testing"

	"github.com/yasyf/daemonkit/artifact"
)

func TestPruneProductToolEnvsKeepsCurrentPreviousAndLiveEnvs(t *testing.T) {
	t.Parallel()
	root, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	store := artifact.Store{Root: root}
	env := func(dist, version string, installed bool) string {
		dir := filepath.Join(store.ToolsDir(), dist, version)
		if err := os.MkdirAll(filepath.Join(dir, dist, "bin"), 0o700); err != nil {
			t.Fatal(err)
		}
		if installed {
			if err := os.WriteFile(filepath.Join(dir, ".installed"), nil, 0o600); err != nil {
				t.Fatal(err)
			}
		}
		return dir
	}
	env("capt-hook", "12.111.3", true)
	env("capt-hook", "12.111.2", true)
	env("capt-hook", "12.111.1", true)
	env("capt-hook", "12.111.4", false)
	mcp := env("capt-hook", "12.103.0", true)
	worker := env("capt-hook", "12.105.3", true)
	env("capt-hook", "12.99.0", true)
	env("capt-hook-cli", "12.108.8", false)
	other := env("slop-cop", "1.0.0", true)
	source := newFakeProcSource()
	source.add(procRow{PID: 10}, filepath.Join(mcp, "capt-hook", "bin", "python"), filepath.Join(mcp, "capt-hook", "bin", "capt-hook"), "mcp")
	source.add(procRow{PID: 11}, "python", "-m", "captain_hook.worker")
	source.setCwd(11, filepath.Join(worker, "capt-hook"))
	source.add(procRow{PID: 12}, filepath.Join(store.ToolsDir(), "capt-hook", "12.99.0-sibling", "bin", "python"))

	removed, err := pruneProductToolEnvs(store, source, "12.111.3")
	if err != nil {
		t.Fatal(err)
	}

	var pruned []string
	for _, entry := range removed {
		pruned = append(pruned, entry.Dist+"/"+entry.Version)
	}
	slices.Sort(pruned)
	if want := []string{"capt-hook-cli/12.108.8", "capt-hook/12.111.1", "capt-hook/12.99.0"}; !slices.Equal(pruned, want) {
		t.Fatalf("pruned %v, want %v", pruned, want)
	}
	for _, kept := range []string{"capt-hook/12.111.3", "capt-hook/12.111.2", "capt-hook/12.111.4", "capt-hook/12.103.0", "capt-hook/12.105.3"} {
		if _, err := os.Stat(filepath.Join(store.ToolsDir(), kept)); err != nil {
			t.Errorf("%s was removed: %v", kept, err)
		}
	}
	if _, err := os.Stat(other); err != nil {
		t.Errorf("another dist's env was removed: %v", err)
	}
	for _, gone := range pruned {
		if _, err := os.Stat(filepath.Join(store.ToolsDir(), gone)); !os.IsNotExist(err) {
			t.Errorf("%s survived the prune: %v", gone, err)
		}
	}
}
