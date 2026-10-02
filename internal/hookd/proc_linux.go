//go:build linux

package hookd

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
)

// linuxClockTicks is CLK_TCK, which every Linux ABI x/sys builds for fixes at
// 100; the kernel exposes it only through getauxval(AT_CLKTCK).
const linuxClockTicks = 100

// Indexes into /proc/<pid>/stat counted from the field after the comm, which
// is the third: proc(5) numbers state 3, ppid 4, pgrp 5, utime 14, stime 15
// and starttime 22.
const (
	statState     = 0
	statPPID      = 1
	statPGID      = 2
	statUtime     = 11
	statStime     = 12
	statStart     = 19
	statMinFields = statStart + 1
)

type linuxStat struct {
	comm       string
	state      byte
	ppid       int
	pgid       int
	cpuTicks   uint64
	startTicks uint64
}

type linuxProcSource struct {
	bootUnix int64
}

func newProcSource() procSource {
	boot, err := readBootTime()
	if err != nil {
		panic(fmt.Sprintf("captain: %v", err))
	}
	return &linuxProcSource{bootUnix: boot}
}

func readBootTime() (int64, error) {
	raw, err := os.ReadFile("/proc/stat")
	if err != nil {
		return 0, err
	}
	for line := range strings.SplitSeq(string(raw), "\n") {
		if value, ok := strings.CutPrefix(line, "btime "); ok {
			return strconv.ParseInt(strings.TrimSpace(value), 10, 64)
		}
	}
	return 0, errors.New("/proc/stat carries no btime")
}

func (s *linuxProcSource) snapshot() (map[int]procRow, error) {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return nil, fmt.Errorf("captain: enumerate process table: %w", err)
	}
	table := make(map[int]procRow, len(entries))
	for _, entry := range entries {
		pid, err := strconv.Atoi(entry.Name())
		if err != nil {
			continue
		}
		if row, ok := s.probe(pid); ok {
			table[pid] = row
		}
	}
	return table, nil
}

func (s *linuxProcSource) probe(pid int) (procRow, bool) {
	stat, ok := readLinuxStat(pid)
	if !ok || stat.state == 'Z' || stat.state == 'X' {
		return procRow{}, false
	}
	return s.row(pid, stat), true
}

func (s *linuxProcSource) row(pid int, stat linuxStat) procRow {
	return procRow{
		PID: pid, PPID: stat.ppid, PGID: stat.pgid,
		StartUnix: s.bootUnix + int64(stat.startTicks/linuxClockTicks),
		StartUsec: int32(stat.startTicks%linuxClockTicks) * (1_000_000 / linuxClockTicks),
		Comm:      stat.comm,
	}
}

func (s *linuxProcSource) usage(pid int) (procUsage, bool) {
	stat, ok := readLinuxStat(pid)
	if !ok {
		return procUsage{}, false
	}
	usage := procUsage{CPUSeconds: float64(stat.cpuTicks) / linuxClockTicks, CPUKnown: true}
	if raw, err := os.ReadFile(procPath(pid, "io")); err == nil {
		if disk, ok := parseLinuxIO(raw); ok {
			usage.DiskBytes, usage.DiskKnown = disk, true
		}
	}
	return usage, true
}

func (s *linuxProcSource) argv(pid int) ([]string, bool) {
	raw, err := os.ReadFile(procPath(pid, "cmdline"))
	if err != nil {
		return nil, false
	}
	return parseLinuxCmdline(raw)
}

func (s *linuxProcSource) cwd(pid int) (string, bool) {
	target, err := os.Readlink(procPath(pid, "cwd"))
	if err != nil {
		return "", false
	}
	return target, true
}

func procPath(pid int, leaf string) string {
	return "/proc/" + strconv.Itoa(pid) + "/" + leaf
}

func readLinuxStat(pid int) (linuxStat, bool) {
	raw, err := os.ReadFile(procPath(pid, "stat"))
	if err != nil {
		return linuxStat{}, false
	}
	return parseLinuxStat(raw)
}

// parseLinuxStat reads one /proc/<pid>/stat line. The comm is whatever the
// process set, parentheses and spaces included, so the fixed fields are
// located from the last closing parenthesis and never by splitting the line.
func parseLinuxStat(raw []byte) (linuxStat, bool) {
	opening := bytes.IndexByte(raw, '(')
	closing := bytes.LastIndexByte(raw, ')')
	if opening < 0 || closing < opening {
		return linuxStat{}, false
	}
	fields := strings.Fields(string(raw[closing+1:]))
	if len(fields) < statMinFields || len(fields[statState]) != 1 {
		return linuxStat{}, false
	}
	ppid, ppidErr := strconv.Atoi(fields[statPPID])
	pgid, pgidErr := strconv.Atoi(fields[statPGID])
	utime, utimeErr := strconv.ParseUint(fields[statUtime], 10, 64)
	stime, stimeErr := strconv.ParseUint(fields[statStime], 10, 64)
	start, startErr := strconv.ParseUint(fields[statStart], 10, 64)
	if errors.Join(ppidErr, pgidErr, utimeErr, stimeErr, startErr) != nil {
		return linuxStat{}, false
	}
	return linuxStat{
		comm: string(raw[opening+1 : closing]), state: fields[statState][0],
		ppid: ppid, pgid: pgid, cpuTicks: utime + stime, startTicks: start,
	}, true
}

func parseLinuxIO(raw []byte) (uint64, bool) {
	var total uint64
	found := 0
	for line := range strings.SplitSeq(string(raw), "\n") {
		name, value, ok := strings.Cut(line, ": ")
		if !ok || name != "read_bytes" && name != "write_bytes" {
			continue
		}
		parsed, err := strconv.ParseUint(value, 10, 64)
		if err != nil {
			return 0, false
		}
		total += parsed
		found++
	}
	return total, found == 2
}

func parseLinuxCmdline(raw []byte) ([]string, bool) {
	trimmed := bytes.TrimSuffix(raw, []byte{0})
	if len(trimmed) == 0 {
		return nil, false
	}
	parts := bytes.Split(trimmed, []byte{0})
	args := make([]string, len(parts))
	for i, part := range parts {
		args[i] = string(part)
	}
	return args, true
}
