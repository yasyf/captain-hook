package hookd

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestPackageInstallAbortsBeforeTheHostWhenTheToolEnvFails(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("DAEMONKIT_HOME", home)
	t.Setenv("PATH", t.TempDir())
	err := installPackage(installProductToolEnv, applyPackagedApplication, packageToolEnvTimeout, packageLifecycleTimeout)
	if err == nil || !strings.Contains(err.Error(), "install the capt-hook "+Build+" tool env") {
		t.Fatalf("installPackage without uv = %v, want the tool env failure", err)
	}
	if _, err := os.Lstat(filepath.Join(home, ".local")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("package-install touched ~/.local after the tool env failed: %v", err)
	}
}

func TestPackageInstallLinksTheToolEnvCLI(t *testing.T) {
	home, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("HOME", home)
	t.Setenv("DAEMONKIT_HOME", home)
	t.Setenv("PATH", t.TempDir())
	installed, err := resolveInstalledPaths()
	if err != nil {
		t.Fatal(err)
	}
	if want := filepath.Join(home, ".local", "bin", "capt-hook"); installed.cli != want {
		t.Fatalf("the CLI link is %q, want %q beside capt-hookd", installed.cli, want)
	}
	if err := os.MkdirAll(filepath.Dir(installed.cli), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := linkInstalledCLI(installed.cli); err == nil {
		t.Fatal("linkInstalledCLI linked a CLI with no tool env installed")
	}
	if _, err := os.Lstat(installed.cli); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("linkInstalledCLI left %q behind with no tool env installed: %v", installed.cli, err)
	}

	toolDir := filepath.Join(home, ".daemonkit", "tools", "capt-hook", Build)
	venvBin := filepath.Join(toolDir, "capt-hook", "bin")
	for _, dir := range []string{venvBin, filepath.Join(toolDir, "bin")} {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	cli := filepath.Join(venvBin, "capt-hook")
	for _, file := range []string{filepath.Join(venvBin, "hook"), cli, filepath.Join(toolDir, ".installed")} {
		if err := os.WriteFile(file, nil, 0o700); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink(filepath.Join(venvBin, "hook"), filepath.Join(toolDir, "bin", "hook")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(home, "an-earlier-build", "capt-hook"), installed.cli); err != nil {
		t.Fatal(err)
	}
	if err := linkInstalledCLI(installed.cli); err != nil {
		t.Fatal(err)
	}
	if target, err := filepath.EvalSymlinks(installed.cli); err != nil || target != cli {
		t.Fatalf("%q resolves to %q, %v; want this build's %q", installed.cli, target, err, cli)
	}
}
