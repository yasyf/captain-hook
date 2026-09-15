package hookd

import (
	"bytes"
	"errors"
	"strings"
	"testing"

	"github.com/yasyf/captain-hook/internal/wireproto"
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
	payload, err := wireproto.MarshalEventRequest(request)
	if err != nil {
		t.Fatalf("MarshalEventRequest at the ceiling: %v", err)
	}
	if len(payload) != wireproto.MaxHostPayload {
		t.Fatalf("boundary event serializes to %d bytes, want exactly %d", len(payload), wireproto.MaxHostPayload)
	}
	if detail := daemonkit.MaxDetail(maxHostFrame); detail < daemonkit.Bytes(len(payload)) {
		t.Fatalf("MaxDetail(%d) = %d, want at least the %d-byte body", maxHostFrame, detail, len(payload))
	}

	var framed bytes.Buffer
	if err := wireproto.EncodeFrame(&framed, wireproto.Frame{
		Protocol: wireproto.Schema, Op: "event", ID: 1, Request: &request,
	}); err != nil {
		t.Fatalf("EncodeFrame at the ceiling: %v", err)
	}
	decoded, err := wireproto.DecodeFrame(&framed)
	if err != nil {
		t.Fatalf("DecodeFrame at the ceiling: %v", err)
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
	if _, err := wireproto.MarshalEventRequest(request); !errors.Is(err, wireproto.ErrPayloadTooLarge) {
		t.Fatalf("MarshalEventRequest one byte past the ceiling = %v, want %v", err, wireproto.ErrPayloadTooLarge)
	}
}

// boundaryEventRequest builds the event whose serialized form is exactly
// wireproto.MaxHostPayload bytes. Its payload repeats the five characters whose
// escaping decides the ceiling: `<`, `>`, and `&` cost one byte each only
// because wireproto.Marshal leaves them alone, while `"` and `\` cost two —
// seven serialized bytes per five raw ones.
func boundaryEventRequest(t *testing.T) wireproto.EventRequest {
	t.Helper()
	const unit = `<>&"\`
	const unitCost = 7
	request := wireproto.EventRequest{
		Schema: wireproto.Schema, Event: "PreToolUse", Root: "/tmp/repo", CWD: "/tmp/repo",
		Env:       map[string]string{},
		ClientPID: 10, ClientPPID: 9,
	}
	empty, err := wireproto.Marshal(request)
	if err != nil {
		t.Fatalf("Marshal: %v", err)
	}
	room := wireproto.MaxHostPayload - len(empty)
	if room <= 0 {
		t.Fatalf("an empty event already spends %d of the %d-byte ceiling", len(empty), wireproto.MaxHostPayload)
	}
	request.PayloadRaw = strings.Repeat(unit, room/unitCost) + strings.Repeat("<", room%unitCost)
	if err := request.Validate(); err != nil {
		t.Fatalf("boundary event is not a valid request: %v", err)
	}
	return request
}
