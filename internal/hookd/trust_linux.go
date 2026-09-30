package hookd

import "github.com/yasyf/daemonkit"

// hostTrust is the same-user floor on every lane: Linux has no code identity to
// pin, so the host is supported only inside a private single-user VM.
func hostTrust() daemonkit.Trust {
	return daemonkit.Trust{Serving: daemonkit.ServingSameUser()}
}
