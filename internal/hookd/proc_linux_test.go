//go:build linux

package hookd

import (
	"slices"
	"testing"
)

func TestParseLinuxStatLocatesFieldsPastAnAwkwardComm(t *testing.T) {
	t.Parallel()
	raw := []byte("4242 (my (odd) name) S 100 4242 4242 0 -1 4194560 11 0 0 0 150 50 0 0 20 0 1 0 987654 1024 100 18446744073709551615 0 0 0 0 0 0 0 0 0 0 0 0 17 3 0 0 0 0 0 0 0 0 0 0 0 0 0\n")
	stat, ok := parseLinuxStat(raw)
	if !ok {
		t.Fatal("parseLinuxStat refused a well-formed line")
	}
	want := linuxStat{comm: "my (odd) name", state: 'S', ppid: 100, pgid: 4242, cpuTicks: 200, startTicks: 987654}
	if stat != want {
		t.Fatalf("parseLinuxStat = %+v, want %+v", stat, want)
	}
	source := &linuxProcSource{bootUnix: 1_700_000_000}
	row := source.row(4242, stat)
	if row.StartUnix != 1_700_009_876 || row.StartUsec != 540_000 || row.PPID != 100 || row.Comm != "my (odd) name" {
		t.Fatalf("row = %+v", row)
	}
	if _, ok := parseLinuxStat([]byte("4242 (short) S 1 2")); ok {
		t.Fatal("parseLinuxStat accepted a truncated line")
	}
}

func TestParseLinuxIOSumsReadAndWriteBytes(t *testing.T) {
	t.Parallel()
	raw := []byte("rchar: 10\nwchar: 20\nsyscr: 1\nsyscw: 1\nread_bytes: 4096\nwrite_bytes: 8192\ncancelled_write_bytes: 0\n")
	total, ok := parseLinuxIO(raw)
	if !ok || total != 12288 {
		t.Fatalf("parseLinuxIO = %d, %t", total, ok)
	}
	if _, ok := parseLinuxIO([]byte("rchar: 10\n")); ok {
		t.Fatal("parseLinuxIO reported a total without both counters")
	}
}

func TestParseLinuxCmdlineSplitsOnNUL(t *testing.T) {
	t.Parallel()
	argv, ok := parseLinuxCmdline([]byte("node\x00/opt/claude/cli.js\x00--resume\x00"))
	if !ok || !slices.Equal(argv, []string{"node", "/opt/claude/cli.js", "--resume"}) {
		t.Fatalf("parseLinuxCmdline = %q, %t", argv, ok)
	}
	if !isClaudeArgv(argv) {
		t.Fatal("a node process running claude's cli.js is not claude")
	}
	if _, ok := parseLinuxCmdline(nil); ok {
		t.Fatal("an empty cmdline (kernel thread) reported argv")
	}
}
