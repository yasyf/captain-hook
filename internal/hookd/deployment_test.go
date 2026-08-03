package hookd

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/yasyf/daemonkit"
)

// TestHostRecordAbsentPartitionsTheEras pins both arms of the gate that decides
// whether an upgrade stops the incumbent itself. A record present is a v0.21
// host, which deploy's own quiesce drains — stopping it here would take its
// agent down ahead of a Supersede that could still fail. A record absent is a
// pre-v0.21 host, invisible to that quiesce and fatal to the inventory after
// it, so the upgrade has to stop that one itself.
func TestHostRecordAbsentPartitionsTheEras(t *testing.T) {
	t.Parallel()
	present := filepath.Join(t.TempDir(), "daemon.records")
	if err := os.WriteFile(present, []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	absent := filepath.Join(t.TempDir(), "daemon.records")

	for name, tt := range map[string]struct {
		path string
		want bool
	}{
		"record present is the v0.21 era": {present, false},
		"record absent is the legacy era": {absent, true},
	} {
		t.Run(name, func(t *testing.T) {
			got, err := hostRecordAbsent(tt.path)
			if err != nil {
				t.Fatalf("hostRecordAbsent: %v", err)
			}
			if got != tt.want {
				t.Fatalf("hostRecordAbsent(%q) = %t, want %t", tt.path, got, tt.want)
			}
		})
	}
}

// TestHostStopDaemonNamesNoProgram is the whole reason one Stop call serves
// both eras. Stop's inventory gate holds vacuously over a Daemon naming no
// program, so a legacy host with no record and no v0.21 socket is carried to
// the agent removal whose bootout takes it down. Naming a program would invert
// that: the gate would find the live legacy host and refuse with ErrUnsettled.
func TestHostStopDaemonNamesNoProgram(t *testing.T) {
	t.Parallel()
	daemon := hostDaemon()
	if daemon.Program != (daemonkit.Program{}) {
		t.Fatal("host daemon names a program; Stop would refuse a live legacy host instead of removing it")
	}
	if _, err := daemonkit.Open(daemon); err != nil {
		t.Fatalf("host daemon is not openable as a client: %v", err)
	}
}

// TestLaunchctlRunnerReportsExitStatusAsACode holds daemonkit's Runner
// contract: launchctl's own refusal is a status the caller classifies, while an
// error is reserved for a command that never ran and therefore produced none.
func TestLaunchctlRunnerReportsExitStatusAsACode(t *testing.T) {
	t.Parallel()
	output, code, err := launchctlRunner(t.Context(), "/bin/sh", "-c", "echo refused >&2; exit 3")
	if err != nil {
		t.Fatalf("launchctlRunner returned an error for a command that ran: %v", err)
	}
	if code != 3 || output != "refused\n" {
		t.Fatalf("launchctlRunner = %q, code %d, want %q, code 3", output, code, "refused\n")
	}
	if _, code, err := launchctlRunner(t.Context(), filepath.Join(t.TempDir(), "absent")); err == nil {
		t.Fatalf("launchctlRunner accepted an executable that never ran, code %d", code)
	}
}
