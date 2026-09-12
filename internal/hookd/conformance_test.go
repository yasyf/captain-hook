package hookd

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
)

const conformanceDirEnv = "CAPT_HOOK_CONFORMANCE_DIR"

type protocolDescriptor struct {
	Protocol int                 `json:"protocol"`
	Limits   map[string]int      `json:"limits"`
	Ops      map[string]string   `json:"ops"`
	Fields   map[string][]string `json:"fields"`
}

type corpusFrame struct {
	Name    string `json:"name"`
	Verdict string `json:"verdict"`
}

func describeProtocol() protocolDescriptor {
	return protocolDescriptor{
		Protocol: Schema,
		Limits: map[string]int{
			"event_input":    maxEventInput,
			"event_envelope": maxEventEnvelope,
			"host_payload":   maxHostPayload,
			"worker_frame":   maxWorkerFrame,
		},
		Ops: map[string]string{
			"hello":  opWorkerHello,
			"event":  opWorkerEvent,
			"result": opWorkerResult,
			"error":  opWorkerError,
		},
		Fields: map[string][]string{
			"worker_frame":   wireFields(workerFrame{}),
			"event_request":  wireFields(EventRequest{}),
			"event_response": wireFields(EventResponse{}),
		},
	}
}

func wireFields(value any) []string {
	structType := reflect.TypeOf(value)
	names := make([]string, 0, structType.NumField())
	for index := range structType.NumField() {
		names = append(names, strings.Split(structType.Field(index).Tag.Get("json"), ",")[0])
	}
	slices.Sort(names)
	return names
}

// admitWorkerFrame is the whole Go-side admission of one decoded frame: the
// payload validators worker.go runs once decodeWorkerFrame has accepted the
// envelope.
func admitWorkerFrame(frame workerFrame) error {
	if frame.Request != nil {
		if err := validateEventRequest(*frame.Request); err != nil {
			return err
		}
	}
	if frame.Response != nil {
		return validateEventResponse(*frame.Response)
	}
	return nil
}

func sampleEventRequest() EventRequest {
	return EventRequest{
		Schema: Schema, Event: "PreToolUse", Root: "/project", CWD: "/project/subdir",
		Env:        map[string]string{"CLAUDE_PROJECT_DIR": "/project"},
		PayloadRaw: `{"session_id":"session-1"}`, Python: "/usr/bin/python3", Build: "12.9.1",
		ClientPID: 100, ClientPPID: 99,
	}
}

func framed(payload []byte) []byte {
	var header [4]byte
	binary.BigEndian.PutUint32(header[:], uint32(len(payload)))
	return append(header[:], payload...)
}

func declaredLength(size uint32, payload []byte) []byte {
	var header [4]byte
	binary.BigEndian.PutUint32(header[:], size)
	return append(header[:], payload...)
}

func encodedFrame(t *testing.T, frame workerFrame) []byte {
	t.Helper()
	var buffer bytes.Buffer
	if err := encodeWorkerFrame(&buffer, frame); err != nil {
		t.Fatalf("encodeWorkerFrame: %v", err)
	}
	return buffer.Bytes()
}

// maxReachableEvent is the largest event the host can actually put on the
// worker pipe: maxEventInput raw bytes of the one character JSON escaping
// doubles, which is what makes the serialized body approach maxHostPayload.
func maxReachableEvent(t *testing.T) []byte {
	t.Helper()
	request := sampleEventRequest()
	request.PayloadRaw = strings.Repeat(`"`, maxEventInput)
	return encodedFrame(t, workerFrame{Protocol: Schema, Op: opWorkerEvent, ID: 3, Request: &request})
}

func rawEventFrame(t *testing.T, mutate func(frame map[string]any, request map[string]any)) []byte {
	t.Helper()
	request := sampleEventRequest()
	body, err := marshalHostJSON(request)
	if err != nil {
		t.Fatalf("marshalHostJSON: %v", err)
	}
	var requestMap map[string]any
	if err := json.Unmarshal(body, &requestMap); err != nil {
		t.Fatalf("unmarshal request: %v", err)
	}
	frameMap := map[string]any{"protocol": Schema, "op": opWorkerEvent, "id": 1, "request": requestMap}
	mutate(frameMap, requestMap)
	payload, err := marshalHostJSON(frameMap)
	if err != nil {
		t.Fatalf("marshalHostJSON frame: %v", err)
	}
	return framed(payload)
}

func goCorpus(t *testing.T) []struct {
	frame corpusFrame
	bytes []byte
} {
	t.Helper()
	request := sampleEventRequest()
	return []struct {
		frame corpusFrame
		bytes []byte
	}{
		{corpusFrame{"hello", "accept"},
			encodedFrame(t, workerFrame{Protocol: Schema, Op: opWorkerHello, Build: "12.9.1"})},
		{corpusFrame{"event_minimal", "accept"},
			encodedFrame(t, workerFrame{Protocol: Schema, Op: opWorkerEvent, ID: 1, Request: &request})},
		{corpusFrame{"event_at_max_size", "accept"}, maxReachableEvent(t)},
		{corpusFrame{"frame_unknown_field", "reject"},
			rawEventFrame(t, func(frame map[string]any, _ map[string]any) { frame["legacy"] = true })},
		{corpusFrame{"request_unknown_field", "reject"},
			rawEventFrame(t, func(_ map[string]any, request map[string]any) { request["legacy"] = true })},
		{corpusFrame{"request_missing_cwd", "reject"},
			rawEventFrame(t, func(_ map[string]any, request map[string]any) { delete(request, "cwd") })},
		{corpusFrame{"request_wrong_schema", "reject"},
			rawEventFrame(t, func(_ map[string]any, request map[string]any) { request["schema"] = Schema + 1 })},
		{corpusFrame{"frame_trailing_json", "reject"}, func() []byte {
			payload, err := marshalHostJSON(map[string]any{"protocol": Schema, "op": opWorkerHello, "build": "12.9.1"})
			if err != nil {
				t.Fatalf("marshalHostJSON: %v", err)
			}
			return framed(append(payload, []byte(`{"protocol":1}`)...))
		}()},
		{corpusFrame{"frame_length_zero", "reject"}, declaredLength(0, nil)},
		{corpusFrame{"frame_over_cap", "reject"}, declaredLength(maxWorkerFrame+1, []byte("{}"))},
		{corpusFrame{"event_over_input_cap", "reject"}, func() []byte {
			oversize := sampleEventRequest()
			oversize.PayloadRaw = strings.Repeat("x", maxEventInput+1)
			return encodedFrame(t, workerFrame{Protocol: Schema, Op: opWorkerEvent, ID: 2, Request: &oversize})
		}()},
	}
}

func canonicalDigest(t *testing.T, frame workerFrame) string {
	t.Helper()
	payload, err := marshalHostJSON(frame)
	if err != nil {
		t.Fatalf("marshalHostJSON: %v", err)
	}
	sum := sha256.Sum256(payload)
	return hex.EncodeToString(sum[:])
}

func writeJSON(t *testing.T, path string, value any) {
	t.Helper()
	payload, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		t.Fatalf("marshal %s: %v", path, err)
	}
	if err := os.WriteFile(path, payload, 0o644); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

func readJSON(t *testing.T, path string, target any) {
	t.Helper()
	payload, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	if err := json.Unmarshal(payload, target); err != nil {
		t.Fatalf("unmarshal %s: %v", path, err)
	}
}

func conformanceDir(t *testing.T) string {
	t.Helper()
	dir := os.Getenv(conformanceDirEnv)
	if dir == "" {
		t.Skipf("%s is unset; tests/test_protocol_conformance.py drives this test", conformanceDirEnv)
	}
	return dir
}

// TestConformanceEmit publishes the Go half of the wire contract — the
// descriptor reflected off the structs encoding/json actually uses, and a
// corpus of frames the Python half must judge the same way.
func TestConformanceEmit(t *testing.T) {
	dir := conformanceDir(t)
	writeJSON(t, filepath.Join(dir, "go.json"), describeProtocol())

	outbound := filepath.Join(dir, "go_to_python")
	if err := os.MkdirAll(outbound, 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", outbound, err)
	}
	manifest := make([]corpusFrame, 0)
	digests := make(map[string]string)
	for _, entry := range goCorpus(t) {
		if err := os.WriteFile(filepath.Join(outbound, entry.frame.Name+".bin"), entry.bytes, 0o644); err != nil {
			t.Fatalf("write %s: %v", entry.frame.Name, err)
		}
		manifest = append(manifest, entry.frame)
		if entry.frame.Verdict != "accept" {
			continue
		}
		frame, err := decodeWorkerFrame(bytes.NewReader(entry.bytes))
		if err != nil {
			t.Fatalf("%s: Go refused its own accepted frame: %v", entry.frame.Name, err)
		}
		if err := admitWorkerFrame(frame); err != nil {
			t.Fatalf("%s: Go refused its own admitted frame: %v", entry.frame.Name, err)
		}
		digests[entry.frame.Name] = canonicalDigest(t, frame)
	}
	writeJSON(t, filepath.Join(outbound, "manifest.json"), manifest)
	writeJSON(t, filepath.Join(outbound, "digests.json"), digests)
}

// TestConformanceVerify closes both legs of the round trip: Go decodes what
// Python re-encoded from Go's corpus, and judges Python's own corpus against
// the verdicts Python recorded for it.
func TestConformanceVerify(t *testing.T) {
	dir := conformanceDir(t)

	var digests map[string]string
	readJSON(t, filepath.Join(dir, "go_to_python", "digests.json"), &digests)
	for name, want := range digests {
		payload, err := os.ReadFile(filepath.Join(dir, "go_to_python_back", name+".bin"))
		if err != nil {
			t.Fatalf("read %s: %v", name, err)
		}
		frame, err := decodeWorkerFrame(bytes.NewReader(payload))
		if err != nil {
			t.Fatalf("%s: Go refused Python's re-encoding: %v", name, err)
		}
		if err := admitWorkerFrame(frame); err != nil {
			t.Fatalf("%s: Go refused Python's re-admitted frame: %v", name, err)
		}
		if got := canonicalDigest(t, frame); got != want {
			t.Fatalf("%s: round trip changed the frame\n go  %s\n back %s", name, want, got)
		}
	}

	inbound := filepath.Join(dir, "python_to_go")
	var manifest []corpusFrame
	readJSON(t, filepath.Join(inbound, "manifest.json"), &manifest)
	back := filepath.Join(dir, "python_to_go_back")
	if err := os.MkdirAll(back, 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", back, err)
	}
	for _, entry := range manifest {
		payload, err := os.ReadFile(filepath.Join(inbound, entry.Name+".bin"))
		if err != nil {
			t.Fatalf("read %s: %v", entry.Name, err)
		}
		frame, err := decodeWorkerFrame(bytes.NewReader(payload))
		if err == nil {
			err = admitWorkerFrame(frame)
		}
		verdict := "accept"
		if err != nil {
			verdict = "reject"
		}
		if verdict != entry.Verdict {
			t.Fatalf("%s: Go verdict %q, Python recorded %q (%v)", entry.Name, verdict, entry.Verdict, err)
		}
		if verdict != "accept" {
			continue
		}
		var reencoded bytes.Buffer
		if err := encodeWorkerFrame(&reencoded, frame); err != nil {
			t.Fatalf("%s: re-encode: %v", entry.Name, err)
		}
		if err := os.WriteFile(filepath.Join(back, entry.Name+".bin"), reencoded.Bytes(), 0o644); err != nil {
			t.Fatalf("write %s: %v", entry.Name, err)
		}
	}
}
