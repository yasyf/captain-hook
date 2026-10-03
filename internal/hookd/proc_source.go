package hookd

import (
	"path/filepath"
	"strings"
)

type procRow struct {
	PID       int
	PPID      int
	PGID      int
	StartUnix int64
	StartUsec int32
	Comm      string
}

type procIdentity struct {
	PID       int
	StartUnix int64
	StartUsec int32
}

func (r procRow) identity() procIdentity {
	return procIdentity{PID: r.PID, StartUnix: r.StartUnix, StartUsec: r.StartUsec}
}

type procUsage struct {
	CPUSeconds float64
	DiskBytes  uint64
	CPUKnown   bool
	DiskKnown  bool
}

type procSource interface {
	snapshot() (map[int]procRow, error)
	probe(pid int) (procRow, bool)
	usage(pid int) (procUsage, bool)
	argv(pid int) ([]string, bool)
	cwd(pid int) (string, bool)
}

var scriptRuntimes = map[string]bool{"node": true, "bun": true, "deno": true}

func isClaudeCLIJS(token string) bool {
	return filepath.Base(token) == "cli.js" && strings.Contains(filepath.Dir(token), "claude")
}

func isClaudeArgv(argv []string) bool {
	if len(argv) == 0 {
		return false
	}
	switch base := filepath.Base(argv[0]); {
	case base == "claude":
		return true
	case scriptRuntimes[base]:
		for _, arg := range argv[1:] {
			if isClaudeCLIJS(arg) {
				return true
			}
		}
	}
	return false
}

func isAgentArgv(argv []string) bool {
	return isClaudeArgv(argv) || len(argv) != 0 && filepath.Base(argv[0]) == "codex"
}

func childIndex(table map[int]procRow) map[int][]int {
	children := make(map[int][]int, len(table))
	for pid, row := range table {
		children[row.PPID] = append(children[row.PPID], pid)
	}
	return children
}
