// Package cwdguard leaves a removed working directory for "/" before package os
// runs os/executable_darwin.go's `initCwd, initCwdErr = Getwd()`, whose libc
// fallback scans the former parent; importing only syscall puts this init ahead
// of os in Go's import-path initialization order.
package cwdguard

import (
	"syscall"
	"unsafe"
)

// Departed reports that the process started in a removed working directory
// and now runs from "/".
var Departed bool

func init() {
	fd, err := syscall.Open(".", syscall.O_RDONLY|syscall.O_DIRECTORY|syscall.O_CLOEXEC, 0)
	if err != nil {
		return
	}
	defer syscall.Close(fd)
	Departed = !present(fd) && syscall.Chdir("/") == nil
}

func present(fd int) bool {
	var path [1024]byte
	if _, _, errno := syscall.Syscall(syscall.SYS_FCNTL, uintptr(fd), syscall.F_GETPATH, uintptr(unsafe.Pointer(&path[0]))); errno != 0 {
		return false
	}
	end := 0
	for path[end] != 0 {
		end++
	}
	var named, here syscall.Stat_t
	if syscall.Stat(string(path[:end]), &named) != nil || syscall.Fstat(fd, &here) != nil {
		return false
	}
	return named.Dev == here.Dev && named.Ino == here.Ino
}
