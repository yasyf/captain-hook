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
	err := applyPackagedApplication(t.Context())
	if err == nil || !strings.Contains(err.Error(), "install the capt-hook "+Build+" tool env") {
		t.Fatalf("applyPackagedApplication without uv = %v, want the tool env failure", err)
	}
	if _, err := os.Lstat(filepath.Join(home, ".local")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("package-install touched ~/.local after the tool env failed: %v", err)
	}
}
