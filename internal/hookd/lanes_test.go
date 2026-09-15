package hookd

import (
	"context"
	"encoding/json"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
)

// hookPayload is the shape Claude Code writes to a hook's stdin: the lead's
// session_id beside the fields every event carries, plus agent_id on a
// subagent's events.
func hookPayload(sessionID, agentID string) string {
	payload := map[string]any{
		"session_id": sessionID, "transcript_path": "/tmp/transcripts/" + sessionID + ".jsonl",
		"cwd": "/tmp/repo", "permission_mode": "default", "hook_event_name": "PreToolUse",
		"tool_name": "Bash", "tool_input": map[string]any{"command": "ls"},
	}
	if agentID != "" {
		payload["agent_id"] = agentID
	}
	encoded, err := json.Marshal(payload)
	if err != nil {
		panic(err)
	}
	return string(encoded)
}

// heldWorker reports each request the manager puts on the worker pipe and
// answers it only when the test releases its id.
type heldWorker struct {
	arrived chan wireproto.Frame
	release chan uint64
}

func holdingWorker(t *testing.T, manager *workerManager) *heldWorker {
	t.Helper()
	held := &heldWorker{arrived: make(chan wireproto.Frame, 16), release: make(chan uint64, 16)}
	t.Cleanup(func() { close(held.release) })
	scriptedWorker(t, manager, func(conn net.Conn) {
		go func() {
			for id := range held.release {
				_ = wireproto.EncodeFrame(conn, wireproto.Frame{
					Protocol: wireproto.Schema, Op: wireproto.OpResult, ID: id,
					Response: &wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok"},
				})
			}
		}()
		for {
			frame, err := wireproto.DecodeFrame(conn)
			if err != nil {
				return
			}
			held.arrived <- frame
		}
	})
	return held
}

func (h *heldWorker) next(t *testing.T) wireproto.Frame {
	t.Helper()
	select {
	case frame := <-h.arrived:
		return frame
	case <-time.After(2 * time.Second):
		t.Fatal("no dispatch reached the worker")
		return wireproto.Frame{}
	}
}

func (h *heldWorker) none(t *testing.T) {
	t.Helper()
	select {
	case frame := <-h.arrived:
		t.Fatalf("a dispatch reached the worker while its agent's earlier event was still running: %s", frame.Request.PayloadRaw)
	case <-time.After(25 * time.Millisecond):
	}
}

func agentEvent(pid int, sessionID, agentID string) wireproto.EventRequest {
	request := testEventRequest("PreToolUse")
	request.Root = "/live"
	request.ClientPID = pid
	request.PayloadRaw = hookPayload(sessionID, agentID)
	return request
}

// TestDispatchSerializesOneAgentAndOverlapsItsPeers pins the lane key: two
// events from one agent run one at a time, while the lead and its subagents,
// which share a session_id, run beside each other.
func TestDispatchSerializesOneAgentAndOverlapsItsPeers(t *testing.T) {
	t.Parallel()
	manager := mustWorkerManager(t)
	held := holdingWorker(t, manager)
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	results := make(chan error, 4)
	dispatch := func(request wireproto.EventRequest) {
		go func() {
			_, err := manager.dispatch(ctx, request)
			results <- err
		}()
	}

	dispatch(agentEvent(11, "session-a", ""))
	first := held.next(t)

	dispatch(agentEvent(12, "session-a", "agent-1"))
	dispatch(agentEvent(13, "session-a", "agent-2"))
	dispatch(agentEvent(14, "session-a", ""))
	overlapped := []wireproto.Frame{held.next(t), held.next(t)}
	for _, agent := range []string{"agent-1", "agent-2"} {
		if !strings.Contains(overlapped[0].Request.PayloadRaw, agent) && !strings.Contains(overlapped[1].Request.PayloadRaw, agent) {
			t.Fatalf("%s's event did not overlap with the lead's", agent)
		}
	}
	held.none(t)

	held.release <- first.ID
	second := held.next(t)
	if strings.Contains(second.Request.PayloadRaw, "agent_id") {
		t.Fatalf("the lead's second event did not follow its first: %s", second.Request.PayloadRaw)
	}
	for _, frame := range append(overlapped, second) {
		held.release <- frame.ID
	}
	for range 4 {
		if err := <-results; err != nil {
			t.Fatal(err)
		}
	}
}
