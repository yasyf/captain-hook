package hookd

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/yasyf/daemonkit"
)

const (
	// hostStopTimeout must clear hostShutdownTimeout: a serving host is drained
	// through the grace its supervisor promises it before it comes down, and a
	// budget shorter than that grace would end the stop mid-drain.
	hostStopTimeout = hostShutdownTimeout + 15*time.Second

	incumbentProbeTimeout = 10 * time.Second
)

var errIncumbentHost = errors.New("captain package: package-install refused")

// incumbentProbe reports evidence that a host may be installed, starting, or
// serving. An empty report is a proven absence; an error proves nothing.
type incumbentProbe func(context.Context) (string, error)

// canonicalExecutable is this process's own image in the form the kernel
// reports one: absolute and symlink-free.
func canonicalExecutable() (string, error) {
	executable, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("captain package: resolve current executable: %w", err)
	}
	resolved, err := filepath.EvalSymlinks(executable)
	if err != nil {
		return "", fmt.Errorf("captain package: resolve %q: %w", executable, err)
	}
	return resolved, nil
}

// stopInstalledHost makes nothing serve the host label and takes its agent
// down, draining the incumbent through the control lane.
func stopInstalledHost(ctx context.Context) error {
	client, err := daemonkit.Open(hostDaemon())
	if err != nil {
		return fmt.Errorf("captain package: open signed host: %w", err)
	}
	stopCtx, cancel := context.WithTimeout(ctx, hostStopTimeout)
	defer cancel()
	if err := client.Stop(stopCtx); err != nil {
		return fmt.Errorf("captain package: stop installed host: %w", err)
	}
	return nil
}

// refuseIncumbent runs before package-install touches anything. Replacing an
// incumbent drains, cancels, and terminates the work it serves, so the install
// proceeds only where every probe proves there is no host to replace.
func refuseIncumbent(ctx context.Context, probes []incumbentProbe) error {
	for _, probe := range probes {
		switch evidence, err := probe(ctx); {
		case err != nil:
			return fmt.Errorf("%w: cannot prove that no Captain Hook host is installed or serving: %w", errIncumbentHost, err)
		case evidence != "":
			return fmt.Errorf("%w: %s, and replacing that host would end work in flight", errIncumbentHost, evidence)
		}
	}
	return nil
}

func pathProbe(path string) incumbentProbe {
	return func(context.Context) (string, error) {
		switch _, err := os.Lstat(path); {
		case err == nil:
			return fmt.Sprintf("%s exists", path), nil
		case errors.Is(err, os.ErrNotExist):
			return "", nil
		default:
			return "", err
		}
	}
}

// controlProbe attaches the host's control lane and lets go at once: the same
// classifying attach daemonkit's readiness proof makes.
func controlProbe(ctx context.Context) (string, error) {
	client, err := daemonkit.Open(hostDaemon())
	if err != nil {
		return "", err
	}
	control, err := client.Control(ctx)
	switch {
	case err == nil:
		_ = control.Close(ctx)
		return "a Captain Hook host is serving", nil
	case errors.Is(err, daemonkit.ErrAbsent):
		return "", nil
	default:
		return "", err
	}
}
