package hookd

import (
	"bytes"
	"io"
	"net"
	"os"
	"path/filepath"
	"testing"
)

func TestOwnerRecordingTeesBothDirections(t *testing.T) {
	dir := t.TempDir()
	host, owner := net.Pipe()
	recorded, err := recordOwner(host, dir, 42)
	if err != nil {
		t.Fatal(err)
	}
	go func() {
		buffer := make([]byte, 5)
		if _, err := io.ReadFull(owner, buffer); err != nil {
			t.Error(err)
			return
		}
		if _, err := owner.Write([]byte("reply")); err != nil {
			t.Error(err)
		}
	}()
	if _, err := recorded.Write([]byte("hello")); err != nil {
		t.Fatal(err)
	}
	reply := make([]byte, 5)
	if _, err := io.ReadFull(recorded, reply); err != nil {
		t.Fatal(err)
	}
	if err := recorded.Close(); err != nil {
		t.Fatal(err)
	}
	ins, _ := filepath.Glob(filepath.Join(dir, "owner-*-42.in"))
	outs, _ := filepath.Glob(filepath.Join(dir, "owner-*-42.out"))
	if len(ins) != 1 || len(outs) != 1 {
		t.Fatalf("recordings = %v %v", ins, outs)
	}
	for path, want := range map[string][]byte{ins[0]: []byte("hello"), outs[0]: []byte("reply")} {
		got, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(got, want) {
			t.Fatalf("%s = %q, want %q", path, got, want)
		}
	}
}

func TestOwnerRecordingStopsAtItsByteBound(t *testing.T) {
	path := filepath.Join(t.TempDir(), "bounded")
	file, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	stream := &recordStream{file: file, left: 4}
	stream.tee([]byte("abc"))
	stream.tee([]byte("de"))
	stream.tee([]byte("f"))
	if err := stream.close(); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != "abc" {
		t.Fatalf("recorded %q, want %q", got, "abc")
	}
}
