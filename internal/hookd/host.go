package hookd

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"

	"github.com/yasyf/captain-hook/internal/wireproto"
	"github.com/yasyf/daemonkit"
)

// maxHostFrame is the frame that carries wireproto.MaxHostPayload. A session
// base64s its terminal at four bytes per three and reserves 4 KiB for the
// envelope, so the frame is sized from the payload and never the other way
// round; TestHostSessionCarriesTheWholeEventPayload proves the pair end to end.
const maxHostFrame daemonkit.Bytes = (wireproto.MaxHostPayload*4+2)/3 + 4<<10

const (
	opTranscript     = "transcript"
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
	Key             string `json:"key"`
	Shard           int    `json:"shard"`
	Root            string `json:"root"`
	Build           string `json:"build"`
	Python          string `json:"python"`
	PID             int    `json:"pid"`
	PendingRequests int    `json:"pending_requests"`
	Background      int    `json:"background"`
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
