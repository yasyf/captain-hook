package hookd

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func launchFixture(t *testing.T) (dir, record, descriptor string) {
	t.Helper()
	root := t.TempDir()
	descriptor = filepath.Join(root, "bin", "hook.binrun")
	if err := os.MkdirAll(filepath.Dir(descriptor), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(descriptor, []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv(binrunPluginRootEnv, root)
	dir = t.TempDir()
	return dir, filepath.Join(dir, "launch", Build), descriptor
}

func TestRecordLaunchNamesTheResolvedExecutable(t *testing.T) {
	dir, record, _ := launchFixture(t)
	if err := recordLaunch(dir); err != nil {
		t.Fatal(err)
	}
	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	if got, err := os.ReadFile(record); err != nil || string(got) != executable+"\n" {
		t.Fatalf("record = %q, %v; want %q", got, err, executable+"\n")
	}
}

func TestRecordLaunchRewritesARecordNoNewerThanTheDescriptor(t *testing.T) {
	dir, record, descriptor := launchFixture(t)
	if err := recordLaunch(dir); err != nil {
		t.Fatal(err)
	}
	stamp := time.Now().Add(time.Hour).Truncate(time.Second)
	if err := os.Chtimes(descriptor, stamp, stamp); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(record, stamp.Add(500*time.Millisecond), stamp.Add(500*time.Millisecond)); err != nil {
		t.Fatal(err)
	}
	if err := recordLaunch(dir); err != nil {
		t.Fatal(err)
	}
	if info, err := os.Stat(record); err != nil || !info.ModTime().Before(stamp) {
		t.Fatalf("record mtime = %v, %v; want the rewrite's own clock, before the descriptor", info.ModTime(), err)
	}
}

func TestRecordLaunchSkipsALaunchBinrunDidNotMake(t *testing.T) {
	dir, record, _ := launchFixture(t)
	t.Setenv(binrunPluginRootEnv, "")
	if err := recordLaunch(dir); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(record); !os.IsNotExist(err) {
		t.Fatalf("stat record = %v; want not exist", err)
	}
}
