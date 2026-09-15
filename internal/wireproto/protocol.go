package wireproto

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

const (
	// Schema is the only captain-hook host and worker schema.
	Schema = 1

	// MaxEventInput is the raw hook payload capt-hookd will read from stdin. It
	// bounds one read; it does not bound what that payload becomes on the wire.
	MaxEventInput = 32 << 20

	// MaxEventEnvelope is what an EventRequest spends outside PayloadRaw: the
	// event name, both paths, the client identity, and the semantic environment
	// the worker key is cut from.
	MaxEventEnvelope = 1 << 20

	// MaxHostPayload is the ceiling on a serialized request or reply body, and
	// the only size that binds: JSON string escaping decides how many bytes a
	// payload becomes and no raw size predicts it. Embedding MaxEventInput bytes
	// of well-formed JSON text costs at most two bytes per byte — quotes and
	// backslashes double, and Marshal keeps `<`, `>`, and `&` at one
	// apiece — so this admits every such payload with room for the envelope
	// beside it. Escaping past 2:1 means bytes JSON cannot carry literally,
	// control codes and invalid UTF-8 at six bytes each, and those are refused
	// by name with ErrPayloadTooLarge rather than sized for.
	MaxHostPayload = 2*MaxEventInput + MaxEventEnvelope

	// MaxWorkerFrame carries one MaxHostPayload body inside the frame that names
	// it. The frame wraps the request in its protocol, op, and id fields, so it
	// cannot be the payload ceiling itself without refusing a request the host
	// already admitted.
	MaxWorkerFrame = MaxHostPayload + 4<<10
)

// OpHello, OpEvent, OpResult, and OpError name the frames the host and a
// Python worker exchange.
const (
	OpHello  = "hello"
	OpEvent  = "event"
	OpResult = "result"
	OpError  = "error"
)

// EventRequest is one exact hook dispatch admitted by the Go host.
// DeadlineUnixMS is the caller's deadline as Unix milliseconds, zero when it
// set none; the worker refuses to start a dispatch whose deadline has passed
// and stops between hooks once it does.
type EventRequest struct {
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

// EventResponse is the byte-shaped product result returned by the Python worker.
type EventResponse struct {
	Schema    int     `json:"schema"`
	Status    string  `json:"status"`
	Stdout    string  `json:"stdout"`
	Stderr    string  `json:"stderr"`
	Exit      int     `json:"exit"`
	ElapsedMS float64 `json:"elapsed_ms"`
}

// Frame is one length-prefixed message on the worker pipe.
type Frame struct {
	Protocol int            `json:"protocol"`
	Op       string         `json:"op"`
	ID       uint64         `json:"id,omitempty"`
	Build    string         `json:"build,omitempty"`
	Request  *EventRequest  `json:"request,omitempty"`
	Response *EventResponse `json:"response,omitempty"`
	Error    string         `json:"error,omitempty"`
}

// Validate refuses a request the worker cannot dispatch exactly.
func (request EventRequest) Validate() error {
	switch {
	case request.Schema != Schema:
		return fmt.Errorf("captain: request schema %d is not exact v%d", request.Schema, Schema)
	case request.Event == "":
		return errors.New("captain: event is required")
	case request.Root == "":
		return errors.New("captain: root is required")
	case request.CWD == "":
		return errors.New("captain: cwd is required")
	case request.ClientPID <= 1 || request.ClientPPID <= 0:
		return errors.New("captain: client process identity is required")
	case request.DeadlineUnixMS < 0:
		return fmt.Errorf("captain: deadline %d is before the epoch", request.DeadlineUnixMS)
	}
	if request.Env == nil {
		return errors.New("captain: request environment is required")
	}
	return nil
}

// Validate refuses a response outside schema v1 or its two statuses.
func (response EventResponse) Validate() error {
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

// Marshal encodes one value the way every captain-hook body travels,
// with HTML escaping off. PayloadRaw carries arbitrary hook JSON, and Go's
// default would render each `<`, `>`, and `&` in it as a six-byte Unicode
// escape — a 6:1 expansion on punctuation hook payloads are full of, against a
// ceiling sized for 2:1.
func Marshal(value any) ([]byte, error) {
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		return nil, err
	}
	return bytes.TrimSuffix(buffer.Bytes(), []byte("\n")), nil
}

// MarshalEventRequest serializes one event and admits it on the size that
// binds. The stdin bound at MaxEventInput still caps what capt-hookd reads, but
// it is no longer the load-bearing check: what a session must carry is this
// serialized body, and only measuring it says whether the session can.
func MarshalEventRequest(request EventRequest) ([]byte, error) {
	payload, err := Marshal(request)
	if err != nil {
		return nil, fmt.Errorf("captain: encode event request: %w", err)
	}
	if len(payload) > MaxHostPayload {
		return nil, fmt.Errorf(
			"%w: event serializes to %d bytes; ceiling is %d", ErrPayloadTooLarge, len(payload), MaxHostPayload,
		)
	}
	return payload, nil
}

func EncodeFrame(writer io.Writer, frame Frame) error {
	payload, err := Marshal(frame)
	if err != nil {
		return fmt.Errorf("captain: encode worker frame: %w", err)
	}
	if len(payload) > MaxWorkerFrame {
		return fmt.Errorf(
			"%w: worker frame is %d bytes; limit is %d", ErrPayloadTooLarge, len(payload), MaxWorkerFrame,
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

func DecodeFrame(reader io.Reader) (Frame, error) {
	var header [4]byte
	if _, err := io.ReadFull(reader, header[:]); err != nil {
		return Frame{}, fmt.Errorf("captain: read worker frame header: %w", err)
	}
	size := binary.BigEndian.Uint32(header[:])
	if size == 0 || size > MaxWorkerFrame {
		return Frame{}, fmt.Errorf("captain: invalid worker frame size %d", size)
	}
	payload := make([]byte, size)
	if _, err := io.ReadFull(reader, payload); err != nil {
		return Frame{}, fmt.Errorf("captain: read worker frame payload: %w", err)
	}
	var frame Frame
	decoder := json.NewDecoder(bytes.NewReader(payload))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&frame); err != nil {
		return Frame{}, fmt.Errorf("captain: decode worker frame: %w", err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return Frame{}, errors.New("captain: worker frame has trailing JSON")
	}
	if frame.Protocol != Schema || frame.Op == "" {
		return Frame{}, errors.New("captain: worker frame has invalid protocol or operation")
	}
	return frame, nil
}
