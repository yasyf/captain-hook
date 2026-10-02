package hookd

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
)

func runPaused(t *testing.T, event, pauseJSON string) (int, string, *scriptedClient) {
	t.Helper()
	state := t.TempDir()
	if pauseJSON != "" {
		if err := os.WriteFile(filepath.Join(state, pauseFile), []byte(pauseJSON), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("CAPTAIN_HOOK_STATE_DIR", state)
	t.Setenv("CLAUDE_PROJECT_DIR", "/project")
	client := &scriptedClient{response: wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: "dispatched"}}
	scriptClient(t, client, nil)
	var stdout, stderr bytes.Buffer
	code := Main([]string{"run", event}, strings.NewReader(benignPayload), &stdout, &stderr)
	return code, stdout.String(), client
}

func pauseFor(remaining time.Duration, reason string) string {
	return fmt.Sprintf(`{"until": %d, "reason": %q}`, time.Now().Add(remaining).Unix(), reason)
}

func TestRunIsANoOpWhilePaused(t *testing.T) {
	for _, event := range []string{"PreToolUse", "PermissionRequest", "Stop", "PostToolUse"} {
		t.Run(event, func(t *testing.T) {
			code, stdout, client := runPaused(t, event, pauseFor(10*time.Minute, "cpu"))

			if code != 0 || stdout != "" || len(client.requests) != 0 {
				t.Fatalf("paused %s: code %d stdout %q requests %d", event, code, stdout, len(client.requests))
			}
		})
	}
}

func TestSessionStartWhilePausedNamesTheExpiryAndTheWayOut(t *testing.T) {
	code, stdout, client := runPaused(t, "SessionStart", pauseFor(10*time.Minute, "cpu saturation"))

	if code != 0 || len(client.requests) != 0 {
		t.Fatalf("code %d requests %d", code, len(client.requests))
	}
	var envelope struct {
		SystemMessage      string `json:"systemMessage"`
		HookSpecificOutput struct {
			HookEventName     string `json:"hookEventName"`
			AdditionalContext string `json:"additionalContext"`
		} `json:"hookSpecificOutput"`
	}
	if err := json.Unmarshal([]byte(stdout), &envelope); err != nil {
		t.Fatalf("banner %q: %v", stdout, err)
	}
	until := time.Now().Add(10 * time.Minute).Local().Format("15:04")
	for _, want := range []string{"paused until " + until, "(cpu saturation)", "`capt-hook resume`", "never by editing the plugin cache"} {
		if !strings.Contains(envelope.SystemMessage, want) {
			t.Errorf("banner %q lacks %q", envelope.SystemMessage, want)
		}
	}
	if envelope.HookSpecificOutput.HookEventName != "SessionStart" ||
		envelope.HookSpecificOutput.AdditionalContext != envelope.SystemMessage {
		t.Errorf("hookSpecificOutput %+v", envelope.HookSpecificOutput)
	}
}

func TestRunDispatchesWhenNoPauseHolds(t *testing.T) {
	for _, tc := range []struct{ name, pause string }{
		{"no pause", ""},
		{"expired", pauseFor(-time.Second, "")},
		{"beyond the cap", pauseFor(maxPause+time.Minute, "")},
		{"unreadable", "{not json"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			code, stdout, client := runPaused(t, "SessionStart", tc.pause)

			if code != 0 || stdout != "dispatched" || len(client.requests) != 1 {
				t.Fatalf("code %d stdout %q requests %d", code, stdout, len(client.requests))
			}
		})
	}
}
