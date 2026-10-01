package wireproto

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"io"
	"testing"
)

func TestWorkerFrameRoundTrip(t *testing.T) {
	t.Parallel()
	want := Frame{
		Protocol: Schema, Op: "event", ID: 7,
		Request: &EventRequest{
			Schema: Schema, Event: "PreToolUse", Root: "/tmp/repo", CWD: "/tmp/repo",
			Env:       map[string]string{},
			ClientPID: 10, ClientPPID: 9,
		},
	}
	var encoded bytes.Buffer
	if err := EncodeFrame(&encoded, want); err != nil {
		t.Fatalf("EncodeFrame: %v", err)
	}
	got, err := DecodeFrame(&encoded)
	if err != nil {
		t.Fatalf("DecodeFrame: %v", err)
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
			if _, err := DecodeFrame(bytes.NewReader(payload)); err == nil {
				t.Fatal("DecodeFrame accepted an obsolete frame")
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
	binary.BigEndian.PutUint32(frame[:], MaxWorkerFrame+1)
	if _, err := DecodeFrame(bytes.NewReader(frame[:])); err == nil {
		t.Fatal("DecodeFrame accepted oversized frame")
	}
}

func TestGuardFieldsRoundTripAndDefaultWhenAbsent(t *testing.T) {
	t.Parallel()
	request := EventRequest{
		Schema: Schema, Event: "PreToolUse", Root: "/r", CWD: "/r", Env: map[string]string{},
		ClientPID: 10, ClientPPID: 9, Mandatory: true,
	}
	response := EventResponse{Schema: Schema, Status: "ok", Guard: GuardCompleted}
	var encoded bytes.Buffer
	for _, frame := range []Frame{
		{Protocol: Schema, Op: OpEvent, ID: 1, Request: &request},
		{Protocol: Schema, Op: OpResult, ID: 1, Response: &response},
	} {
		if err := EncodeFrame(&encoded, frame); err != nil {
			t.Fatalf("EncodeFrame: %v", err)
		}
	}
	event, err := DecodeFrame(&encoded)
	if err != nil || event.Request == nil || !event.Request.Mandatory {
		t.Fatalf("event round trip = %+v, %v; want the mandatory flag", event.Request, err)
	}
	result, err := DecodeFrame(&encoded)
	if err != nil || result.Response == nil || result.Response.Guard != GuardCompleted {
		t.Fatalf("result round trip = %+v, %v; want the guard completion", result.Response, err)
	}
	for name, payload := range map[string][]byte{
		"request": []byte(`{"protocol":1,"op":"event","id":2,"request":{"schema":1,"event":"PreToolUse","root":"/r",` +
			`"cwd":"/r","env":{},"payload_raw":"","client_pid":10,"client_ppid":9,"deadline_unix_ms":0}}`),
		"response": []byte(`{"protocol":1,"op":"result","id":2,"response":{"schema":1,"status":"ok","stdout":"",` +
			`"stderr":"","exit":0,"elapsed_ms":0}}`),
	} {
		absent, err := DecodeFrame(bytes.NewReader(framedJSON(payload)))
		if err != nil {
			t.Fatalf("%s without the guard fields: %v", name, err)
		}
		switch name {
		case "request":
			if absent.Request.Mandatory || absent.Request.Validate() != nil {
				t.Fatalf("absent mandatory decoded as %+v", absent.Request)
			}
		case "response":
			if absent.Response.Guard != "" || absent.Response.Validate() != nil {
				t.Fatalf("absent guard decoded as %+v", absent.Response)
			}
		}
	}
}

func TestNonMandatoryBodiesKeepThePreGuardEncoding(t *testing.T) {
	t.Parallel()
	request := EventRequest{
		Schema: Schema, Event: "PreToolUse", Root: "/r", CWD: "/r", Env: map[string]string{}, ClientPID: 10, ClientPPID: 9,
	}
	response := EventResponse{Schema: Schema, Status: "ok"}
	type preGuardRequest struct {
		Schema         int               `json:"schema"`
		Event          string            `json:"event"`
		Root           string            `json:"root"`
		CWD            string            `json:"cwd"`
		Env            map[string]string `json:"env"`
		PayloadRaw     string            `json:"payload_raw"`
		ClientPID      int               `json:"client_pid"`
		ClientPPID     int               `json:"client_ppid"`
		DeadlineUnixMS int64             `json:"deadline_unix_ms"`
	}
	type preGuardResponse struct {
		Schema    int     `json:"schema"`
		Status    string  `json:"status"`
		Stdout    string  `json:"stdout"`
		Stderr    string  `json:"stderr"`
		Exit      int     `json:"exit"`
		ElapsedMS float64 `json:"elapsed_ms"`
	}
	for name, tc := range map[string]struct {
		value any
		want  string
		peer  any
	}{
		"request": {request, `{"schema":1,"event":"PreToolUse","root":"/r","cwd":"/r","env":{},"payload_raw":"",` +
			`"client_pid":10,"client_ppid":9,"deadline_unix_ms":0}`, &preGuardRequest{}},
		"reply": {response.Reply(), `{"schema":1,"status":"ok","stdout":"","stderr":"","exit":0,"elapsed_ms":0}`,
			&preGuardResponse{}},
	} {
		encoded, err := Marshal(tc.value)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if string(encoded) != tc.want {
			t.Fatalf("%s encodes as %s, want the pre-guard bytes %s", name, encoded, tc.want)
		}
		decoder := json.NewDecoder(bytes.NewReader(encoded))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(tc.peer); err != nil {
			t.Fatalf("a pre-guard peer refuses the %s: %v", name, err)
		}
	}
	request.Mandatory = true
	response.Guard = GuardCompleted
	for name, tc := range map[string]struct {
		value any
		field string
	}{
		"mandatory request":  {request, `"mandatory":true`},
		"completed response": {response, `"guard":"completed"`},
		"completed reply":    {response.Reply(), `"guard":"completed"`},
	} {
		encoded, err := Marshal(tc.value)
		if err != nil || !bytes.Contains(encoded, []byte(tc.field)) {
			t.Fatalf("%s encodes as %s, %v; want %s", name, encoded, err, tc.field)
		}
	}
}

func TestResponseValidateRefusesAnUnknownGuardCompletion(t *testing.T) {
	t.Parallel()
	response := EventResponse{Schema: Schema, Status: "ok", Guard: "partial"}
	if err := response.Validate(); err == nil {
		t.Fatal("Validate accepted a guard completion the protocol does not name")
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
