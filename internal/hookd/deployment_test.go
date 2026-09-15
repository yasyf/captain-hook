package hookd

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/yasyf/daemonkit"
	"github.com/yasyf/daemonkit/deploy"
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

func TestRetryRestoredAbort(t *testing.T) {
	t.Parallel()
	restoredAbort := errors.Join(daemonkit.ErrUnsettled, deploy.ErrRestored)
	unrestoredAbort := daemonkit.ErrUnsettled
	other := errors.New("captain package: activate installed app")

	for name, tt := range map[string]struct {
		results   []error
		wantErr   error
		wantCalls int
	}{
		"restored abort once then success retries": {[]error{restoredAbort, nil}, nil, 2},
		"restored abort twice fails":               {[]error{restoredAbort, restoredAbort}, deploy.ErrRestored, 2},
		"unrestored abort is not retried":          {[]error{unrestoredAbort}, daemonkit.ErrUnsettled, 1},
		"other failure is not retried":             {[]error{other}, other, 1},
	} {
		t.Run(name, func(t *testing.T) {
			calls := 0
			err := retryRestoredAbort(t.Context(), func(context.Context) error {
				calls++
				return tt.results[calls-1]
			})
			if calls != tt.wantCalls {
				t.Fatalf("apply ran %d times, want %d", calls, tt.wantCalls)
			}
			if !errors.Is(err, tt.wantErr) {
				t.Fatalf("retryRestoredAbort = %v, want %v", err, tt.wantErr)
			}
		})
	}
}
