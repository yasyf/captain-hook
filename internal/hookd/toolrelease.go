package hookd

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"
	"time"
)

const (
	productDist        = "capt-hook"
	releaseWaitTimeout = 10 * time.Minute
	releasePollEvery   = 5 * time.Second
)

func awaitProductRelease() error {
	if _, err := installedPython(); err == nil {
		return nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), releaseWaitTimeout)
	defer cancel()
	if err := awaitResolvable(ctx, "uv", productDist, Build); err != nil {
		return fmt.Errorf("captain package: wait for capt-hook %s to resolve: %w", Build, err)
	}
	return nil
}

// awaitResolvable holds until uv resolves dist==version from the sources it
// installs from: PyPI lists a fresh upload minutes after publish succeeds, and
// until then uv tool install fails with "No solution found".
func awaitResolvable(ctx context.Context, uv, dist, version string) error {
	poll := time.NewTicker(releasePollEvery)
	defer poll.Stop()
	reason := "no probe finished"
	for ctx.Err() == nil {
		cmd := exec.CommandContext(
			ctx, uv, "pip", "compile", "--no-deps", "--refresh-package", dist, "--quiet", "--no-header", "-",
		)
		cmd.Stdin = strings.NewReader(dist + "==" + version + "\n")
		var stderr bytes.Buffer
		cmd.Stderr = &stderr
		err := cmd.Run()
		if err == nil {
			return nil
		}
		if ctx.Err() != nil {
			break
		}
		if exit := (*exec.ExitError)(nil); !errors.As(err, &exit) {
			return err
		}
		reason = strings.TrimSpace(stderr.String())
		select {
		case <-ctx.Done():
		case <-poll.C:
		}
	}
	return fmt.Errorf("uv cannot resolve %s==%s: %s: %w", dist, version, reason, ctx.Err())
}
