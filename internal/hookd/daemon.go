package hookd

import (
	"time"

	"github.com/yasyf/daemonkit"
)

const (
	hostShutdownTimeout = 30 * time.Second
	// hostConcurrency bounds concurrent wire sessions, not dispatch. Every
	// resident client holds one for its lifetime — an MCP server per Claude
	// Code session, plus each in-flight hook — so it tracks the machine's
	// session count rather than its cores.
	hostConcurrency = 256
)

func hostDaemon() daemonkit.Daemon {
	return daemonkit.Daemon{
		Label:       hostServiceLabel,
		Schemas:     []daemonkit.Schema{hostSchema},
		Restart:     daemonkit.RestartOnFailure,
		Shutdown:    daemonkit.Grace(hostShutdownTimeout),
		MaxFrame:    maxHostFrame,
		Concurrency: hostConcurrency,
	}
}
