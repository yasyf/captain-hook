package hookd

import (
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sync"
	"time"
)

const (
	ownerRecordEnv   = "CAPT_HOOK_OWNER_RECORD"
	ownerRecordLimit = 4 << 30
)

type recordStream struct {
	mu   sync.Mutex
	file *os.File
	left int64
}

func (s *recordStream) tee(p []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.file == nil {
		return
	}
	if int64(len(p)) > s.left {
		s.closeLocked()
		return
	}
	if _, err := s.file.Write(p); err != nil {
		s.closeLocked()
		return
	}
	s.left -= int64(len(p))
}

func (s *recordStream) close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.closeLocked()
}

func (s *recordStream) closeLocked() error {
	if s.file == nil {
		return nil
	}
	err := s.file.Close()
	s.file = nil
	return err
}

type ownerRecording struct {
	net.Conn
	in  *recordStream
	out *recordStream
}

func recordOwner(conn net.Conn, dir string, pid int) (net.Conn, error) {
	stem := filepath.Join(dir, fmt.Sprintf("owner-%d-%d", time.Now().UnixNano(), pid))
	in, err := os.OpenFile(stem+".in", os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, err
	}
	out, err := os.OpenFile(stem+".out", os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, errors.Join(err, in.Close())
	}
	return &ownerRecording{
		Conn: conn,
		in:   &recordStream{file: in, left: ownerRecordLimit},
		out:  &recordStream{file: out, left: ownerRecordLimit},
	}, nil
}

func (r *ownerRecording) Write(p []byte) (int, error) {
	n, err := r.Conn.Write(p)
	r.in.tee(p[:n])
	return n, err
}

func (r *ownerRecording) Read(p []byte) (int, error) {
	n, err := r.Conn.Read(p)
	r.out.tee(p[:n])
	return n, err
}

func (r *ownerRecording) Close() error {
	return errors.Join(r.Conn.Close(), r.in.close(), r.out.close())
}
