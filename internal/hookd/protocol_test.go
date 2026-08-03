package hookd

import (
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"strings"
	"testing"

	"github.com/yasyf/daemonkit"
)

// TestHostSessionCarriesTheWholeEventPayload drives a real escape-heavy event
// sized to land exactly on the ceiling through both framings that must carry
// it. Constants alone cannot prove this: what a session carries depends on how
// JSON escapes the payload, so the test escapes one and measures. It fails
// loudly if daemonkit's envelope reserve or terminal encoding moves, if the
// worker frame stops leaving room for the frame around an admitted body, or if
// HTML escaping is ever turned back on — that alone triples the unit below and
// pushes the same event past the ceiling.
func TestHostSessionCarriesTheWholeEventPayload(t *testing.T) {
	t.Parallel()
	request := boundaryEventRequest(t)
	payload, err := marshalEventRequest(request)
	if err != nil {
		t.Fatalf("marshalEventRequest at the ceiling: %v", err)
	}
	if len(payload) != maxHostPayload {
		t.Fatalf("boundary event serializes to %d bytes, want exactly %d", len(payload), maxHostPayload)
	}
	if detail := daemonkit.MaxDetail(maxHostFrame); detail < daemonkit.Bytes(len(payload)) {
		t.Fatalf("MaxDetail(%d) = %d, want at least the %d-byte body", maxHostFrame, detail, len(payload))
	}

	var framed bytes.Buffer
	if err := encodeWorkerFrame(&framed, workerFrame{
		Protocol: Schema, Op: "event", ID: 1, Request: &request,
	}); err != nil {
		t.Fatalf("encodeWorkerFrame at the ceiling: %v", err)
	}
	decoded, err := decodeWorkerFrame(&framed)
	if err != nil {
		t.Fatalf("decodeWorkerFrame at the ceiling: %v", err)
	}
	if decoded.Request == nil || decoded.Request.PayloadRaw != request.PayloadRaw {
		t.Fatal("worker frame did not round trip the boundary payload intact")
	}
}

// TestOversizeEventIsRefusedByName proves the byte past the ceiling is named
// rather than truncated: a silently shortened payload is a hook deciding on
// half its input.
func TestOversizeEventIsRefusedByName(t *testing.T) {
	t.Parallel()
	request := boundaryEventRequest(t)
	request.PayloadRaw += "<"
	if _, err := marshalEventRequest(request); !errors.Is(err, ErrPayloadTooLarge) {
		t.Fatalf("marshalEventRequest one byte past the ceiling = %v, want %v", err, ErrPayloadTooLarge)
	}
}

// boundaryEventRequest builds the event whose serialized form is exactly
// maxHostPayload bytes. Its payload repeats the five characters whose escaping
// decides the ceiling: `<`, `>`, and `&` cost one byte each only because
// marshalHostJSON leaves them alone, while `"` and `\` cost two — seven
// serialized bytes per five raw ones.
func boundaryEventRequest(t *testing.T) EventRequest {
	t.Helper()
	const unit = `<>&"\`
	const unitCost = 7
	request := EventRequest{
		Schema: Schema, Event: "PreToolUse", Root: "/tmp/repo", CWD: "/tmp/repo",
		Env: map[string]string{}, Python: "/usr/bin/python3", Build: Build,
		ClientPID: 10, ClientPPID: 9,
	}
	empty, err := marshalHostJSON(request)
	if err != nil {
		t.Fatalf("marshalHostJSON: %v", err)
	}
	room := maxHostPayload - len(empty)
	if room <= 0 {
		t.Fatalf("an empty event already spends %d of the %d-byte ceiling", len(empty), maxHostPayload)
	}
	request.PayloadRaw = strings.Repeat(unit, room/unitCost) + strings.Repeat("<", room%unitCost)
	if err := validateEventRequest(request); err != nil {
		t.Fatalf("boundary event is not a valid request: %v", err)
	}
	return request
}

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
