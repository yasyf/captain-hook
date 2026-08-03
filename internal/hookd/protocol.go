package hookd

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"

	"github.com/yasyf/daemonkit"
)

const (
	// Schema is the only captain-hook host and worker schema.
	Schema = 1

	// maxEventInput is the raw hook payload capt-hookd will read from stdin. It
	// bounds one read; it does not bound what that payload becomes on the wire.
	maxEventInput = 32 << 20

	// maxEventEnvelope is what an EventRequest spends outside PayloadRaw: the
	// event name, both paths, the interpreter, the build, the client identity,
	// and the semantic environment the worker key is cut from.
	maxEventEnvelope = 1 << 20

	// maxHostPayload is the ceiling on a serialized request or reply body, and
	// the only size that binds: JSON string escaping decides how many bytes a
	// payload becomes and no raw size predicts it. Embedding maxEventInput bytes
	// of well-formed JSON text costs at most two bytes per byte — quotes and
	// backslashes double, and marshalHostJSON keeps `<`, `>`, and `&` at one
	// apiece — so this admits every such payload with room for the envelope
	// beside it. Escaping past 2:1 means bytes JSON cannot carry literally,
	// control codes and invalid UTF-8 at six bytes each, and those are refused
	// by name with ErrPayloadTooLarge rather than sized for.
	maxHostPayload = 2*maxEventInput + maxEventEnvelope

	// maxHostFrame is the frame that carries maxHostPayload. A session base64s
	// its terminal at four bytes per three and reserves 4 KiB for the envelope,
	// so the frame is sized from the payload and never the other way round;
	// TestHostSessionCarriesTheWholeEventPayload proves the pair end to end.
	maxHostFrame daemonkit.Bytes = (maxHostPayload*4+2)/3 + 4<<10

	// maxWorkerFrame carries one maxHostPayload body inside the frame that names
	// it. The frame wraps the request in its protocol, op, and id fields, so it
	// cannot be the payload ceiling itself without refusing a request the host
	// already admitted.
	maxWorkerFrame = maxHostPayload + 4<<10
)

const (
	opEvent          = "captain.event.v1"
	opStatus         = "captain.status.v1"
	opRestartWorkers = "captain.restart-workers.v1"
	opRuntimeHealth  = "captain.host.v1.runtime.health"
	opHelperPing     = "captain.helper.ping.v1"
	opHelperNotify   = "captain.helper.notify.v1"
	opHelperNext     = "captain.helper.next.v1"
)

// hostSchema is the stable v1 application protocol shared across runtime releases.
const hostSchema daemonkit.Schema = "captain-hook.host.v1"

// Build is stamped from the release tag into the wheel and signed helper.
var Build = "0.0.0"

// EventRequest is one exact hook dispatch admitted by the Go host.
type EventRequest struct {
	Schema     int               `json:"schema"`
	Event      string            `json:"event"`
	Async      bool              `json:"async"`
	Root       string            `json:"root"`
	CWD        string            `json:"cwd"`
	Env        map[string]string `json:"env"`
	PayloadRaw string            `json:"payload_raw"`
	Python     string            `json:"python"`
	Build      string            `json:"build"`
	ClientPID  int               `json:"client_pid"`
	ClientPPID int               `json:"client_ppid"`
}

// EventResponse is the byte-shaped product result returned by the Python worker.
type EventResponse struct {
	Schema    int     `json:"schema"`
	Status    string  `json:"status"`
	Stdout    string  `json:"stdout"`
	Stderr    string  `json:"stderr"`
	Exit      int     `json:"exit"`
	ElapsedMS float64 `json:"elapsed_ms"`
}

type workerFrame struct {
	Protocol int            `json:"protocol"`
	Op       string         `json:"op"`
	ID       uint64         `json:"id,omitempty"`
	Build    string         `json:"build,omitempty"`
	Request  *EventRequest  `json:"request,omitempty"`
	Response *EventResponse `json:"response,omitempty"`
	Error    string         `json:"error,omitempty"`
}

type statusResponse struct {
	Schema  int            `json:"schema"`
	Build   string         `json:"build"`
	PID     int            `json:"pid"`
	Workers []workerStatus `json:"workers"`
}

// runtimeHealthResponse is the serving host's own identity. Nothing in it
// restates a phase: the business lane dispatches to a product only once the
// runtime is ready and not draining, so an answer to this op is itself the
// readiness the fields used to carry.
type runtimeHealthResponse struct {
	Schema          int    `json:"schema"`
	RuntimeBuild    string `json:"runtime_build"`
	RuntimeProtocol int    `json:"runtime_protocol"`
	PID             int    `json:"pid"`
}

type restartWorkersRequest struct {
	Schema int    `json:"schema"`
	Build  string `json:"build"`
}

type workerStatus struct {
	Key    string `json:"key"`
	Root   string `json:"root"`
	Build  string `json:"build"`
	Python string `json:"python"`
	PID    int    `json:"pid"`
}

func validateEventRequest(request EventRequest) error {
	switch {
	case request.Schema != Schema:
		return fmt.Errorf("captain: request schema %d is not exact v%d", request.Schema, Schema)
	case request.Event == "":
		return errors.New("captain: event is required")
	case request.Root == "":
		return errors.New("captain: root is required")
	case request.CWD == "":
		return errors.New("captain: cwd is required")
	case request.Python == "":
		return errors.New("captain: python executable is required")
	case request.Build == "":
		return errors.New("captain: product build is required")
	case request.ClientPID <= 1 || request.ClientPPID <= 0:
		return errors.New("captain: client process identity is required")
	}
	if request.Env == nil {
		return errors.New("captain: request environment is required")
	}
	return nil
}

func validateEventResponse(response EventResponse) error {
	if response.Schema != Schema {
		return fmt.Errorf("captain: response schema %d is not exact v%d", response.Schema, Schema)
	}
	switch response.Status {
	case "ok", "error":
		return nil
	default:
		return fmt.Errorf("captain: invalid worker status %q", response.Status)
	}
}

// ErrPayloadTooLarge refuses a body whose serialized size clears the host
// payload ceiling. It names the one failure a raw byte count cannot predict:
// JSON escaping expands control codes and invalid UTF-8 six bytes to one, so a
// payload that read small off the wire can still exceed what a session carries.
// A refusal here is the loud alternative to a truncated hook decision.
var ErrPayloadTooLarge = errors.New("captain: payload exceeds the host ceiling")

// marshalHostJSON encodes one value the way every captain-hook body travels,
// with HTML escaping off. PayloadRaw carries arbitrary hook JSON, and Go's
// default would render each `<`, `>`, and `&` in it as a six-byte Unicode
// escape — a 6:1 expansion on punctuation hook payloads are full of, against a
// ceiling sized for 2:1.
func marshalHostJSON(value any) ([]byte, error) {
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(buffer.Bytes(), []byte("\n")), nil
}

// marshalEventRequest serializes one event and admits it on the size that
// binds. The stdin bound at maxEventInput still caps what capt-hookd reads, but
// it is no longer the load-bearing check: what a session must carry is this
// serialized body, and only measuring it says whether the session can.
func marshalEventRequest(request EventRequest) ([]byte, error) {
	payload, err := marshalHostJSON(request)
	if err != nil {
		return nil, fmt.Errorf("captain: encode event request: %w", err)
	}
	if len(payload) > maxHostPayload {
		return nil, fmt.Errorf(
			"%w: event serializes to %d bytes; ceiling is %d", ErrPayloadTooLarge, len(payload), maxHostPayload,
		)
	}
	return payload, nil
}

func encodeWorkerFrame(writer io.Writer, frame workerFrame) error {
	payload, err := marshalHostJSON(frame)
	if err != nil {
		return fmt.Errorf("captain: encode worker frame: %w", err)
	}
	if len(payload) > maxWorkerFrame {
		return fmt.Errorf(
			"%w: worker frame is %d bytes; limit is %d", ErrPayloadTooLarge, len(payload), maxWorkerFrame,
		)
	}
	var header [4]byte
	binary.BigEndian.PutUint32(header[:], uint32(len(payload)))
	if err := writeAll(writer, header[:]); err != nil {
		return fmt.Errorf("captain: write worker frame header: %w", err)
	}
	if err := writeAll(writer, payload); err != nil {
		return fmt.Errorf("captain: write worker frame payload: %w", err)
	}
	return nil
}

func writeAll(writer io.Writer, payload []byte) error {
	for len(payload) != 0 {
		written, err := writer.Write(payload)
		if err != nil {
			return err
		}
		if written <= 0 || written > len(payload) {
			return io.ErrShortWrite
		}
		payload = payload[written:]
	}
	return nil
}

func decodeWorkerFrame(reader io.Reader) (workerFrame, error) {
	var header [4]byte
	if _, err := io.ReadFull(reader, header[:]); err != nil {
		return workerFrame{}, fmt.Errorf("captain: read worker frame header: %w", err)
	}
	size := binary.BigEndian.Uint32(header[:])
	if size == 0 || size > maxWorkerFrame {
		return workerFrame{}, fmt.Errorf("captain: invalid worker frame size %d", size)
	}
	payload := make([]byte, size)
	if _, err := io.ReadFull(reader, payload); err != nil {
		return workerFrame{}, fmt.Errorf("captain: read worker frame payload: %w", err)
	}
	var frame workerFrame
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&frame); err != nil {
		return workerFrame{}, fmt.Errorf("captain: decode worker frame: %w", err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return workerFrame{}, errors.New("captain: worker frame has trailing JSON")
	}
	if frame.Protocol != Schema || frame.Op == "" {
		return workerFrame{}, errors.New("captain: worker frame has invalid protocol or operation")
	}
	return frame, nil
}

func decodeStrict(payload []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return errors.New("trailing JSON")
	}
	return nil
}
