package hookd

import (
	"bytes"
	"encoding/binary"
	"io"
	"testing"
)

func TestWorkerFrameRoundTrip(t *testing.T) {
	t.Parallel()
	want := workerFrame{
		Protocol: Schema, Op: "event", ID: 7,
		Request: &EventRequest{
			Schema: Schema, Event: "PreToolUse", Root: "/tmp/repo", CWD: "/tmp/repo",
			Env: map[string]string{}, Python: "/usr/bin/python3", Build: "12.9.1",
			ClientPID: 10, ClientPPID: 9,
		},
	}
	var encoded bytes.Buffer
	if err := encodeWorkerFrame(&encoded, want); err != nil {
		t.Fatalf("encodeWorkerFrame: %v", err)
	}
	got, err := decodeWorkerFrame(&encoded)
	if err != nil {
		t.Fatalf("decodeWorkerFrame: %v", err)
	}
	if got.Protocol != want.Protocol || got.Op != want.Op || got.ID != want.ID || got.Request.Event != want.Request.Event {
		t.Fatalf("round trip = %#v, want %#v", got, want)
	}
}

func TestWorkerFrameRejectsOldLFAndUnknownFields(t *testing.T) {
	t.Parallel()
	for name, payload := range map[string][]byte{
		"old LF":  []byte(`{"v":1,"kind":"event"}` + "\n"),
		"unknown": framedJSON([]byte(`{"protocol":1,"op":"hello","legacy":true}`)),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := decodeWorkerFrame(bytes.NewReader(payload)); err == nil {
				t.Fatal("decodeWorkerFrame accepted an obsolete frame")
			}
		})
	}
}

func TestWriteAllHandlesShortWrites(t *testing.T) {
	t.Parallel()
	writer := &shortWriter{limit: 3}
	payload := []byte("abcdefghij")
	if err := writeAll(writer, payload); err != nil {
		t.Fatalf("writeAll: %v", err)
	}
	if !bytes.Equal(writer.payload, payload) {
		t.Fatalf("payload = %q, want %q", writer.payload, payload)
	}
}

func TestDecodeWorkerFrameRejectsInvalidSize(t *testing.T) {
	t.Parallel()
	var frame [4]byte
	binary.BigEndian.PutUint32(frame[:], maxWorkerFrame+1)
	if _, err := decodeWorkerFrame(bytes.NewReader(frame[:])); err == nil {
		t.Fatal("decodeWorkerFrame accepted oversized frame")
	}
}

func framedJSON(payload []byte) []byte {
	var encoded bytes.Buffer
	var header [4]byte
	binary.BigEndian.PutUint32(header[:], uint32(len(payload)))
	encoded.Write(header[:])
	encoded.Write(payload)
	return encoded.Bytes()
}

type shortWriter struct {
	limit   int
	payload []byte
}

func (w *shortWriter) Write(payload []byte) (int, error) {
	if w.limit == 0 {
		return 0, io.ErrNoProgress
	}
	count := min(w.limit, len(payload))
	w.payload = append(w.payload, payload[:count]...)
	return count, nil
}
