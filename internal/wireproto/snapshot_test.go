package wireproto

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"testing"
)

type headerOnlyReader struct {
	header      *bytes.Reader
	payloadRead bool
}

func (r *headerOnlyReader) Read(p []byte) (int, error) {
	if r.header.Len() > 0 {
		return r.header.Read(p)
	}
	r.payloadRead = true
	return 0, io.EOF
}

func TestSnapshotDeclaredLengthRejectedBeforePayloadRead(t *testing.T) {
	var header [4]byte
	binary.BigEndian.PutUint32(header[:], (1<<20)+1)
	reader := &headerOnlyReader{header: bytes.NewReader(header[:])}
	if _, err := DecodeFrameLimit(reader, 1<<20); err == nil {
		t.Fatal("oversized snapshot accepted")
	}
	if reader.payloadRead {
		t.Fatal("oversized snapshot payload was read")
	}
}

func TestSnapshotEncodedLimitIncludesEnvelope(t *testing.T) {
	var output bytes.Buffer
	frame := Frame{Protocol: Schema, Op: OpSnapshotRequest, ID: 1, Snapshot: json.RawMessage(`"` + strings.Repeat("x", 1<<20-2) + `"`)}
	if err := EncodeFrameLimit(&output, frame, 1<<20); err == nil {
		t.Fatal("oversized envelope accepted")
	}
	if output.Len() != 0 {
		t.Fatal("oversized snapshot partially written")
	}
}

func TestEncodeFrameBytesMatchesStreamEncoding(t *testing.T) {
	frame := Frame{Protocol: Schema, Op: OpSnapshotRequest, ID: 7, Snapshot: json.RawMessage(`{"request":{"id":"one"}}`)}
	encoded, err := EncodeFrameBytes(frame, 1<<20)
	if err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	if err := EncodeFrameLimit(&output, frame, 1<<20); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(encoded, output.Bytes()) {
		t.Fatalf("bytes=%q stream=%q", encoded, output.Bytes())
	}
	decoded, err := DecodeFrameLimit(bytes.NewReader(encoded), 1<<20)
	if err != nil || decoded.ID != frame.ID || !bytes.Equal(decoded.Snapshot, frame.Snapshot) {
		t.Fatalf("decoded=%+v err=%v", decoded, err)
	}
}

func TestEncodeFrameBytesRefusesOversizedEnvelope(t *testing.T) {
	frame := Frame{Protocol: Schema, Op: OpSnapshotRequest, ID: 1, Snapshot: json.RawMessage(`"` + strings.Repeat("x", 1<<20-2) + `"`)}
	encoded, err := EncodeFrameBytes(frame, 1<<20)
	if !errors.Is(err, ErrPayloadTooLarge) || encoded != nil {
		t.Fatalf("encoded=%d err=%v", len(encoded), err)
	}
}
