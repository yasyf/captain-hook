package hookd

import (
	"context"
	"io"
	"testing"
)

func TestWorkerKeyExcludesSessionAndAccountEnvironment(t *testing.T) {
	t.Parallel()
	base := testEventRequest("PreToolUse")
	base.Root = t.TempDir()
	base.Env = map[string]string{
		"CLAUDE_CODE_SESSION_ID": "session-a", "CLAUDE_CONFIG_DIR": "/account/a",
		"HOOKS_PROFILE": "strict", "CAPTAIN_HOOK_STATE_DIR": "/state",
	}
	changed := base
	changed.Env = map[string]string{
		"CLAUDE_CODE_SESSION_ID": "session-b", "CLAUDE_CONFIG_DIR": "/account/b",
		"HOOKS_PROFILE": "strict", "CAPTAIN_HOOK_STATE_DIR": "/state",
	}
	first, err := makeWorkerKey(base)
	if err != nil {
		t.Fatal(err)
	}
	second, err := makeWorkerKey(changed)
	if err != nil {
		t.Fatal(err)
	}
	if first.id != second.id {
		t.Fatalf("account/session environment partitioned workers: %s != %s", first.id, second.id)
	}
	changed.Env["HOOKS_PROFILE"] = "relaxed"
	third, err := makeWorkerKey(changed)
	if err != nil {
		t.Fatal(err)
	}
	if third.id == first.id {
		t.Fatal("semantic HOOKS environment did not partition workers")
	}
}

func TestSessionIDFallsBackWithoutInventingIdentity(t *testing.T) {
	t.Parallel()
	if got := sessionID(`{"session_id":"abc"}`); got != "abc" {
		t.Fatalf("sessionID = %q", got)
	}
	if got := sessionID(`not-json`); got != "" {
		t.Fatalf("malformed sessionID = %q", got)
	}
}

func TestWorkerBaseEnvironmentDoesNotLeakFirstClientScope(t *testing.T) {
	t.Parallel()
	base := workerBaseEnvironment([]string{
		"PATH=/bin", "HOME=/tmp/home", "CLAUDE_CONFIG_DIR=/account/18",
		"CLAUDE_CODE_SESSION_ID=session-a", "HOOKS_PROFILE=strict", "CAPT_HOOK_RUN_DIR=/old",
	})
	want := map[string]bool{"HOME=/tmp/home": true}
	if len(base) != len(want) {
		t.Fatalf("base environment = %v", base)
	}
	for _, item := range base {
		if !want[item] {
			t.Fatalf("unexpected base environment entry %q", item)
		}
	}
}

func TestWorkerManagerCloseReportsJoinedProductGraph(t *testing.T) {
	manager := newWorkerManager(nil, io.Discard)
	joined, err := manager.Close(context.Background())
	if err != nil || !joined {
		t.Fatalf("Close = joined %t, err %v", joined, err)
	}
}
