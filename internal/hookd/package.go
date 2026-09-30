package hookd

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"github.com/yasyf/daemonkit"
)

// hostStopTimeout must clear hostShutdownTimeout: a serving host is drained
// through the grace its supervisor promises it before it comes down, and a
// budget shorter than that grace would end the stop mid-drain.
const hostStopTimeout = hostShutdownTimeout + 15*time.Second

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
