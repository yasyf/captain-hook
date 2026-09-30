package hookd

import "github.com/yasyf/daemonkit"

const (
	hostTeamID                    = "SXKCTF23Q2"
	hostSigningIdentifier         = "capt-hookd"
	helperClientSigningIdentifier = "com.yasyf.capt-hook.helper.bridge"
)

func hostRequirement() daemonkit.Requirement {
	return daemonkit.Requirement{TeamID: hostTeamID, SigningIdentifier: hostSigningIdentifier}
}

func helperClientRequirement() daemonkit.Requirement {
	return daemonkit.Requirement{TeamID: hostTeamID, SigningIdentifier: helperClientSigningIdentifier}
}

// hostTrust admits only the signed host to the control lane, so only capt-hookd
// may drain the runtime. The business lane is daemonkit's same-user floor.
func hostTrust() daemonkit.Trust {
	control := hostRequirement()
	return daemonkit.Trust{
		Control: &control,
		Serving: daemonkit.ServingSigned(control),
	}
}
