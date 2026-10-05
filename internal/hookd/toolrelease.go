package hookd

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"
	"time"

	captainhook "github.com/yasyf/captain-hook"
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
	python, err := pythonFloor(captainhook.Pyproject)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), releaseWaitTimeout)
	defer cancel()
	if err := awaitResolvable(ctx, "uv", productDist, Build, python); err != nil {
		return fmt.Errorf("captain package: wait for capt-hook %s to resolve: %w", Build, err)
	}
	return nil
}

func pythonFloor(pyproject string) (string, error) {
	for line := range strings.Lines(pyproject) {
		spec, ok := strings.CutPrefix(strings.TrimSpace(line), "requires-python = ")
		if !ok {
			continue
		}
		for clause := range strings.SplitSeq(strings.Trim(spec, `"`), ",") {
			if floor, ok := strings.CutPrefix(strings.TrimSpace(clause), ">="); ok {
				return floor, nil
			}
		}
		return "", fmt.Errorf("captain package: requires-python %s names no lower bound", spec)
	}
	return "", errors.New("captain package: pyproject.toml declares no requires-python")
}

func awaitResolvable(ctx context.Context, uv, dist, version, python string) error {
	poll := time.NewTicker(releasePollEvery)
	defer poll.Stop()
	reason := "no probe finished"
	for ctx.Err() == nil {
		cmd := exec.CommandContext(
			ctx, uv, "pip", "compile", "--no-deps", "--refresh-package", dist, "--python-version", python,
			"--quiet", "--no-header", "-",
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
