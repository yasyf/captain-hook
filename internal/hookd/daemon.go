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

// hostDaemon is the one declaration the serving host, every launcher, and the
// deployment read. Program, Args, and Log stay unset because Client.Ensure
// never runs here: launchd's job for this daemon is declared exactly once, by
// exactAgents, and converged by the deployment. A Program built from the
// installed bundle would also refuse construction on a machine where the app
// is not installed yet, which is exactly where a launcher must still be able
// to report that it is not installed.
func hostDaemon() daemonkit.Daemon {
	return daemonkit.Daemon{
		Label:       hostServiceLabel,
		Schemas:     []daemonkit.Schema{hostSchema},
		Trust:       hostTrust(),
		Restart:     daemonkit.RestartOnFailure,
		Shutdown:    daemonkit.Grace(hostShutdownTimeout),
		MaxFrame:    maxHostFrame,
		Concurrency: hostConcurrency,
	}
}
