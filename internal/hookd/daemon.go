package hookd

import (
	"time"

	"github.com/yasyf/daemonkit"
)

const (
	hostTeamID                    = "SXKCTF23Q2"
	hostSigningIdentifier         = "capt-hookd"
	helperSigningIdentifier       = "com.yasyf.capt-hook.helper"
	helperClientSigningIdentifier = "com.yasyf.capt-hook.helper.bridge"
	hostShutdownTimeout           = 30 * time.Second
	hostConcurrency               = 64
)

func hostRequirement() daemonkit.Requirement {
	return daemonkit.Requirement{TeamID: hostTeamID, SigningIdentifier: hostSigningIdentifier}
}

func helperRequirement() daemonkit.Requirement {
	return daemonkit.Requirement{TeamID: hostTeamID, SigningIdentifier: helperSigningIdentifier}
}

func helperClientRequirement() daemonkit.Requirement {
	return daemonkit.Requirement{TeamID: hostTeamID, SigningIdentifier: helperClientSigningIdentifier}
}

// hostTrust folds captain-hook's peer classes onto daemonkit's three lanes.
// The control lane admits the signed host alone, so only capt-hookd may drain
// the runtime. The business lane is a disjunction over the three signed
// identities that speak the product protocol — host, helper app, and helper
// bridge — and any one of them may invoke every op the product serves.
func hostTrust() daemonkit.Trust {
	control := hostRequirement()
	return daemonkit.Trust{
		Control:  &control,
		Business: daemonkit.Requirements{control, helperRequirement(), helperClientRequirement()},
		Serving:  daemonkit.ServingSigned(control),
	}
}

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
