package hookd

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/yasyf/daemonkit"
	"github.com/yasyf/daemonkit/artifact"
	"github.com/yasyf/daemonkit/durable"
	"github.com/yasyf/daemonkit/supervise"
)

const (
	installedHostName    = "capt-hookd"
	installedVersionName = "version.json"
)

type installedPaths struct {
	dir  string
	host string
	link string
}

// resolveInstalledPaths names the fixed Linux install: the plugin's hook shim
// execs host, the CLI descriptor reads the version.json beside it, and link
// puts it on the user's PATH.
func resolveInstalledPaths() (installedPaths, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return installedPaths{}, fmt.Errorf("captain package: resolve user home: %w", err)
	}
	dir := filepath.Join(home, ".local", "share", "captain-hook", "host")
	return installedPaths{
		dir:  dir,
		host: filepath.Join(dir, installedHostName),
		link: filepath.Join(home, ".local", "bin", installedHostName),
	}, nil
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
	installed, err := resolveInstalledPaths()
	if err != nil {
		return err
	}
	if source == installed.host {
		return errors.New("captain package: packaged source and installed target must differ")
	}
	for _, dir := range []string{installed.dir, filepath.Dir(installed.link)} {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return fmt.Errorf("captain package: create %q: %w", dir, err)
		}
	}
	if err := installExecutable(source, installed.host); err != nil {
		return err
	}
	version, err := json.Marshal(currentHostVersion())
	if err != nil {
		return err
	}
	if err := durable.WriteFile(filepath.Join(installed.dir, installedVersionName), append(version, '\n'), 0o644); err != nil {
		return fmt.Errorf("captain package: publish installed version: %w", err)
	}
	if err := linkExecutable(installed.host, installed.link); err != nil {
		return err
	}
	if err := ensureSupervisedHost(ctx, installed); err != nil && !errors.Is(err, supervise.ErrNoSupervisor) {
		return err
	}
	return nil
}

// supervisedHostDaemon is hostDaemon with the program the workspace's
// supervisor runs: the installed host, served in place.
func supervisedHostDaemon(installed installedPaths) (daemonkit.Daemon, error) {
	resolved, err := resolvePaths()
	if err != nil {
		return daemonkit.Daemon{}, err
	}
	program, err := daemonkit.InBundle(installed.dir, installedHostName)
	if err != nil {
		return daemonkit.Daemon{}, err
	}
	d := hostDaemon()
	d.Program, d.Args, d.Log = program, []string{"serve"}, resolved.log
	return d, nil
}

// ensureSupervisedHost converges the label's supervisor on the installed host,
// draining an incumbent of any other build first.
func ensureSupervisedHost(ctx context.Context, installed installedPaths) error {
	d, err := supervisedHostDaemon(installed)
	if err != nil {
		return err
	}
	client, err := daemonkit.Open(d)
	if err != nil {
		return fmt.Errorf("captain package: open host: %w", err)
	}
	ensureCtx, cancel := context.WithTimeout(ctx, hostStopTimeout)
	defer cancel()
	if _, err := client.Ensure(ensureCtx); err != nil {
		return fmt.Errorf("captain package: ensure the supervised host: %w", err)
	}
	return nil
}

func uninstallPackagedApplication(ctx context.Context) error {
	if err := stopInstalledHost(ctx); err != nil && !errors.Is(err, supervise.ErrNoSupervisor) {
		return err
	}
	installed, err := resolveInstalledPaths()
	if err != nil {
		return err
	}
	if err := durable.Remove(installed.link); err != nil {
		return fmt.Errorf("captain package: remove %q: %w", installed.link, err)
	}
	if err := durable.RemoveTree(installed.dir); err != nil {
		return fmt.Errorf("captain package: remove %q: %w", installed.dir, err)
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

func linkExecutable(host, link string) error {
	staged := link + ".staged"
	if err := durable.Remove(staged); err != nil {
		return fmt.Errorf("captain package: clear %q: %w", staged, err)
	}
	if err := os.Symlink(host, staged); err != nil {
		return fmt.Errorf("captain package: stage %q: %w", link, err)
	}
	if err := durable.Rename(staged, link); err != nil {
		return fmt.Errorf("captain package: publish %q: %w", link, err)
	}
	return nil
}
