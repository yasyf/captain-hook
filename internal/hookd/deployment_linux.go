package hookd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/yasyf/daemonkit/artifact"
	"github.com/yasyf/daemonkit/durable"
)

const (
	installedHostName    = "capt-hookd"
	installedVersionName = "version.json"
)

// installedHostDir is the fixed home of the Linux host: the plugin's hook shim
// execs its capt-hookd, and the CLI descriptor reads its version.json.
func installedHostDir() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("captain package: resolve user home: %w", err)
	}
	return filepath.Join(home, ".local", "share", "captain-hook", "host"), nil
}

func applyPackagedApplication(ctx context.Context) error {
	store, err := artifact.DefaultStore()
	if err != nil {
		return err
	}
	if _, err := store.Resolve(ctx, productToolDescriptor()); err != nil {
		return fmt.Errorf("captain package: install the capt-hook %s tool env: %w", Build, err)
	}
	source, err := canonicalExecutable()
	if err != nil {
		return err
	}
	dir, err := installedHostDir()
	if err != nil {
		return err
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return fmt.Errorf("captain package: create %q: %w", dir, err)
	}
	target := filepath.Join(dir, installedHostName)
	if source == target {
		return errors.New("captain package: packaged source and installed target must differ")
	}
	if err := installExecutable(source, target); err != nil {
		return err
	}
	version, err := json.Marshal(currentHostVersion())
	if err != nil {
		return err
	}
	if err := durable.WriteFile(filepath.Join(dir, installedVersionName), append(version, '\n'), 0o644); err != nil {
		return fmt.Errorf("captain package: publish installed version: %w", err)
	}
	return stopInstalledHost(ctx)
}

func uninstallPackagedApplication(ctx context.Context) error {
	if err := stopInstalledHost(ctx); err != nil {
		return err
	}
	dir, err := installedHostDir()
	if err != nil {
		return err
	}
	if err := durable.RemoveTree(dir); err != nil {
		return fmt.Errorf("captain package: remove %q: %w", dir, err)
	}
	return nil
}

func installExecutable(source, target string) error {
	in, err := os.Open(source)
	if err != nil {
		return fmt.Errorf("captain package: open %q: %w", source, err)
	}
	defer in.Close()
	out, err := durable.Create(target, 0o755)
	if err != nil {
		return fmt.Errorf("captain package: stage %q: %w", target, err)
	}
	defer out.Close()
	if _, err := io.Copy(out, in); err != nil {
		return fmt.Errorf("captain package: copy %q: %w", source, err)
	}
	if err := out.Commit(); err != nil {
		return fmt.Errorf("captain package: publish %q: %w", target, err)
	}
	return nil
}
