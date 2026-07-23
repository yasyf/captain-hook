package hookd

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
	Schema         = 1
	maxEventInput  = 32 << 20
	maxHostFrame   = 64 << 20
	maxWorkerFrame = maxHostFrame
)

const (
	opEvent          = "captain.event.v1"
	opStatus         = "captain.status.v1"
	opRestartWorkers = "captain.restart-workers.v1"
	opRuntimeHealth  = "captain.host.v1.runtime.health"
)

// WireBuild is the stable v1 transport identity shared across runtime releases.
const WireBuild = "captain-hook.host.v1"

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

type runtimeHealthResponse struct {
	Schema            int    `json:"schema"`
	RuntimeBuild      string `json:"runtime_build"`
	RuntimeProtocol   int    `json:"runtime_protocol"`
	ProcessGeneration string `json:"process_generation"`
	PID               int    `json:"pid"`
	State             string `json:"state"`
	Draining          bool   `json:"draining"`
	Busy              bool   `json:"busy"`
	Ready             bool   `json:"ready"`
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

func encodeWorkerFrame(writer io.Writer, frame workerFrame) error {
	payload, err := json.Marshal(frame)
	if err != nil {
		return fmt.Errorf("captain: encode worker frame: %w", err)
	}
	if len(payload) > maxWorkerFrame {
		return fmt.Errorf("captain: worker frame is %d bytes; limit is %d", len(payload), maxWorkerFrame)
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
