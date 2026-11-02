package hookd

import (
	"path/filepath"
	"slices"
	"strings"

	"github.com/yasyf/daemonkit/artifact"
	"github.com/yasyf/daemonkit/version"
)

const productCLIDist = "capt-hook-cli"

// pruneProductToolEnvs removes the tool envs an installed build has left
// behind: every capt-hook version older than current except the newest
// installed one, and every capt-hook-cli env. A newer version is an install
// still landing, and an env a live process was launched from or works in is
// kept, because older sessions keep running the clients and MCP servers they
// started with.
func pruneProductToolEnvs(store artifact.Store, source procSource, current string) ([]artifact.ToolEntry, error) {
	entries, err := store.ToolEntries()
	if err != nil {
		return nil, err
	}
	previous := ""
	for _, entry := range entries {
		if entry.Dist == productDist && !entry.InstalledAt.IsZero() && version.Newer(current, entry.Version) &&
			(previous == "" || version.Newer(entry.Version, previous)) {
			previous = entry.Version
		}
	}
	live, err := liveProcessPaths(source)
	if err != nil {
		return nil, err
	}
	var removed []artifact.ToolEntry
	for _, entry := range entries {
		kept := entry.Dist == productDist && (!version.Newer(current, entry.Version) || entry.Version == previous)
		if kept || entry.Dist != productDist && entry.Dist != productCLIDist ||
			slices.ContainsFunc(live, func(path string) bool { return pathWithin(entry.Dir, path) }) {
			continue
		}
		if err := store.RemoveToolEntry(entry); err != nil {
			return removed, err
		}
		removed = append(removed, entry)
	}
	return removed, nil
}

func liveProcessPaths(source procSource) ([]string, error) {
	table, err := source.snapshot()
	if err != nil {
		return nil, err
	}
	var paths []string
	for pid := range table {
		if argv, ok := source.argv(pid); ok {
			paths = append(paths, argv...)
		}
		if cwd, ok := source.cwd(pid); ok {
			paths = append(paths, cwd)
		}
	}
	return paths, nil
}

func pathWithin(dir, path string) bool {
	return path == dir || strings.HasPrefix(path, dir+string(filepath.Separator))
}
