package hookd

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestHostLogRotatesToOneGenerationPastItsLimit(t *testing.T) {
	path := filepath.Join(t.TempDir(), "capt-hookd.log")
	log, err := openHostLog(path, 16)
	if err != nil {
		t.Fatal(err)
	}
	for _, line := range []string{"first-line\n", "second-line\n", "third-line\n"} {
		if _, err := log.Write([]byte(line)); err != nil {
			t.Fatal(err)
		}
	}
	assertFile(t, path, "third-line\n")
	assertFile(t, path+".1", "second-line\n")
}

func TestHostLogRotatesAnOversizedLogOnItsFirstWrite(t *testing.T) {
	path := filepath.Join(t.TempDir(), "capt-hookd.log")
	if err := os.WriteFile(path, []byte(strings.Repeat("x", 64)), 0o600); err != nil {
		t.Fatal(err)
	}
	log, err := openHostLog(path, 16)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := log.Write([]byte("fresh\n")); err != nil {
		t.Fatal(err)
	}
	assertFile(t, path, "fresh\n")
	assertFile(t, path+".1", strings.Repeat("x", 64))
}

func TestHostLogFollowsOnlyStreamsAlreadyWritingToIt(t *testing.T) {
	log, err := openHostLog(filepath.Join(t.TempDir(), "capt-hookd.log"), hostLogLimit)
	if err != nil {
		t.Fatal(err)
	}
	if len(log.streams) != 0 {
		t.Fatalf("streams = %v, want none: the test's stdout and stderr are not the log", log.streams)
	}
}

func assertFile(t *testing.T, path, want string) {
	t.Helper()
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != want {
		t.Fatalf("%s = %q, want %q", filepath.Base(path), got, want)
	}
}
