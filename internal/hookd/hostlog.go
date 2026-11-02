package hookd

import (
	"os"
	"sync"

	"golang.org/x/sys/unix"
)

const hostLogLimit = 64 << 20

type hostLog struct {
	mu      sync.Mutex
	path    string
	limit   int64
	file    *os.File
	size    int64
	streams []int
}

func openHostLog(path string, limit int64) (*hostLog, error) {
	file, err := openHostLogFile(path)
	if err != nil {
		return nil, err
	}
	info, err := file.Stat()
	if err != nil {
		_ = file.Close()
		return nil, err
	}
	log := &hostLog{path: path, limit: limit, file: file, size: info.Size()}
	for _, stream := range []*os.File{os.Stdout, os.Stderr} {
		if current, err := stream.Stat(); err == nil && os.SameFile(current, info) {
			log.streams = append(log.streams, int(stream.Fd()))
		}
	}
	return log, nil
}

func openHostLogFile(path string) (*os.File, error) {
	return os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
}

func (l *hostLog) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.size > 0 && l.size+int64(len(p)) > l.limit {
		_ = l.rotate()
	}
	n, err := l.file.Write(p)
	l.size += int64(n)
	return n, err
}

func (l *hostLog) rotate() error {
	if err := os.Rename(l.path, l.path+".1"); err != nil {
		return err
	}
	file, err := openHostLogFile(l.path)
	if err != nil {
		return err
	}
	// The service manager opens stdout and stderr on the log path itself, so
	// without the dup they keep appending to the rotated generation.
	for _, fd := range l.streams {
		if err := unix.Dup2(int(file.Fd()), fd); err != nil {
			_ = file.Close()
			return err
		}
	}
	_ = l.file.Close()
	l.file, l.size = file, 0
	return nil
}
