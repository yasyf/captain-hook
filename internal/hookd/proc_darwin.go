//go:build darwin

package hookd

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"runtime"
	"unsafe"

	"github.com/ebitengine/purego"
	"golang.org/x/sys/unix"
)

const (
	darwinZombieState = 5

	rusageInfoV2         = 2
	procPIDVnodePathInfo = 9

	darwinVnodeInfoSize   = 152
	darwinMaxPathLen      = 1024
	darwinVnodePathInfoSz = 2 * (darwinVnodeInfoSize + darwinMaxPathLen)
)

// rusageInfoV2Record mirrors struct rusage_info_v2: 16 uuid bytes followed by
// 18 uint64 counters, with the user and system times in mach absolute units.
type rusageInfoV2Record struct {
	UUID             [16]byte
	UserTime         uint64
	SystemTime       uint64
	PkgIdleWkups     uint64
	InterruptWkups   uint64
	Pageins          uint64
	WiredSize        uint64
	ResidentSize     uint64
	PhysFootprint    uint64
	ProcStartAbstime uint64
	ProcExitAbstime  uint64
	ChildUserTime    uint64
	ChildSystemTime  uint64
	ChildPkgIdle     uint64
	ChildInterrupt   uint64
	ChildPageins     uint64
	ChildElapsed     uint64
	DiskIOBytesRead  uint64
	DiskIOBytesWrite uint64
}

type machTimebase struct {
	Numer uint32
	Denom uint32
}

type darwinProcSource struct {
	pidRusage uintptr
	pidInfo   uintptr
	timebase  machTimebase
}

func newProcSource() procSource {
	lib, err := purego.Dlopen("/usr/lib/libSystem.B.dylib", purego.RTLD_NOW|purego.RTLD_GLOBAL)
	if err != nil {
		panic(fmt.Sprintf("captain: dlopen libSystem: %v", err))
	}
	source := &darwinProcSource{pidRusage: libSystemSymbol(lib, "proc_pid_rusage"), pidInfo: libSystemSymbol(lib, "proc_pidinfo")}
	timebaseInfo := libSystemSymbol(lib, "mach_timebase_info")
	var pinner runtime.Pinner
	pinner.Pin(&source.timebase)
	status, _, _ := purego.SyscallN(timebaseInfo, uintptr(unsafe.Pointer(&source.timebase)))
	pinner.Unpin()
	if status != 0 || source.timebase.Denom == 0 {
		panic(fmt.Sprintf("captain: mach_timebase_info: status %d, %+v", status, source.timebase))
	}
	return source
}

func libSystemSymbol(lib uintptr, name string) uintptr {
	symbol, err := purego.Dlsym(lib, name)
	if err != nil {
		panic(fmt.Sprintf("captain: dlsym %s: %v", name, err))
	}
	return symbol
}

func (s *darwinProcSource) snapshot() (map[int]procRow, error) {
	procs, err := unix.SysctlKinfoProcSlice("kern.proc.all")
	if err != nil {
		return nil, fmt.Errorf("captain: sysctl kern.proc.all: %w", err)
	}
	table := make(map[int]procRow, len(procs))
	for _, kp := range procs {
		if kp.Proc.P_pid <= 0 || kp.Proc.P_stat == darwinZombieState {
			continue
		}
		row := rowFromKinfo(kp)
		table[row.PID] = row
	}
	return table, nil
}

func (s *darwinProcSource) probe(pid int) (procRow, bool) {
	procs, err := unix.SysctlKinfoProcSlice("kern.proc.pid", pid)
	if err != nil || len(procs) == 0 || procs[0].Proc.P_stat == darwinZombieState {
		return procRow{}, false
	}
	return rowFromKinfo(procs[0]), true
}

func rowFromKinfo(kp unix.KinfoProc) procRow {
	comm := kp.Proc.P_comm[:]
	if end := bytes.IndexByte(comm, 0); end >= 0 {
		comm = comm[:end]
	}
	return procRow{
		PID: int(kp.Proc.P_pid), PPID: int(kp.Eproc.Ppid), PGID: int(kp.Eproc.Pgid),
		StartUnix: kp.Proc.P_starttime.Sec, StartUsec: kp.Proc.P_starttime.Usec, Comm: string(comm),
	}
}

func (s *darwinProcSource) usage(pid int) (procUsage, bool) {
	var record rusageInfoV2Record
	var pinner runtime.Pinner
	pinner.Pin(&record)
	defer pinner.Unpin()
	status, _, _ := purego.SyscallN(s.pidRusage, uintptr(pid), rusageInfoV2, uintptr(unsafe.Pointer(&record)))
	if int32(status) != 0 {
		return procUsage{}, false
	}
	ticks := float64(record.UserTime + record.SystemTime)
	nanoseconds := ticks * float64(s.timebase.Numer) / float64(s.timebase.Denom)
	return procUsage{
		CPUSeconds: nanoseconds / 1e9, DiskBytes: record.DiskIOBytesRead + record.DiskIOBytesWrite,
		CPUKnown: true, DiskKnown: true,
	}, true
}

// argv parses KERN_PROCARGS2: a native-endian argc, the exec path, NUL
// padding, then argc NUL-terminated arguments ahead of the environment. The
// kernel answers only for the caller's own processes, so a refusal is unknown.
func (s *darwinProcSource) argv(pid int) ([]string, bool) {
	raw, err := unix.SysctlRaw("kern.procargs2", pid)
	if err != nil || len(raw) < 4 {
		return nil, false
	}
	argc := int(binary.NativeEndian.Uint32(raw[:4]))
	rest := raw[4:]
	execEnd := bytes.IndexByte(rest, 0)
	if argc <= 0 || execEnd < 0 {
		return nil, false
	}
	rest = bytes.TrimLeft(rest[execEnd:], "\x00")
	args := make([]string, 0, argc)
	for len(args) < argc {
		end := bytes.IndexByte(rest, 0)
		if end < 0 {
			return nil, false
		}
		args = append(args, string(rest[:end]))
		rest = rest[end+1:]
	}
	return args, true
}

func (s *darwinProcSource) cwd(pid int) (string, bool) {
	buf := make([]byte, darwinVnodePathInfoSz)
	var pinner runtime.Pinner
	pinner.Pin(&buf[0])
	defer pinner.Unpin()
	size, _, _ := purego.SyscallN(
		s.pidInfo, uintptr(pid), procPIDVnodePathInfo, 0, uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)),
	)
	if int32(size) != darwinVnodePathInfoSz {
		return "", false
	}
	path := buf[darwinVnodeInfoSize : darwinVnodeInfoSize+darwinMaxPathLen]
	end := bytes.IndexByte(path, 0)
	if end <= 0 {
		return "", false
	}
	return string(path[:end]), true
}
