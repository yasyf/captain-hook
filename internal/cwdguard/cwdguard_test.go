package cwdguard_test

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/yasyf/captain-hook/internal/cwdguard"
)

const helperEnv = "CWDGUARD_HELPER"

func TestHelperReportsTheGuardsView(t *testing.T) {
	if os.Getenv(helperEnv) == "" {
		t.Skip("subprocess helper")
	}
	wd, err := os.Getwd()
	if err != nil {
		wd = "error: " + err.Error()
	}
	fmt.Printf("departed=%t wd=%s\n", cwdguard.Departed, wd)
}

func TestInitLeavesARemovedWorkingDirectoryBeforeOS(t *testing.T) {
	for _, tc := range []struct {
		name     string
		remove   bool
		departed bool
	}{
		{"removed", true, true},
		{"live", false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := filepath.Join(t.TempDir(), "gone")
			if err := os.Mkdir(dir, 0o700); err != nil {
				t.Fatal(err)
			}
			t.Chdir(dir)
			if tc.remove {
				if err := os.Remove(dir); err != nil {
					t.Fatal(err)
				}
			}
			cmd := exec.Command(os.Args[0], "-test.run=^TestHelperReportsTheGuardsView$")
			cmd.Env = append(os.Environ(), helperEnv+"=1", "GODEBUG=inittrace=1", "PWD="+dir)
			var stderr strings.Builder
			cmd.Stderr = &stderr
			out, err := cmd.Output()
			if err != nil {
				t.Fatalf("helper: %v\n%s", err, stderr.String())
			}
			guard := strings.Index(stderr.String(), "init github.com/yasyf/captain-hook/internal/cwdguard ")
			osInit := strings.Index(stderr.String(), "init os ")
			if guard < 0 || osInit < 0 || guard > osInit {
				t.Fatalf("init order: guard at %d, os at %d\n%s", guard, osInit, stderr.String())
			}
			wantWD := dir
			if tc.departed {
				wantWD = "/"
			}
			want := fmt.Sprintf("departed=%t wd=%s\n", tc.departed, wantWD)
			if !strings.Contains(string(out), want) {
				t.Fatalf("helper reported %q, want %q", out, want)
			}
		})
	}
}
