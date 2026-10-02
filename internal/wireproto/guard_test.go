package wireproto

import (
	"bytes"
	"encoding/json"
	"os"
	"strings"
	"testing"
	"unicode"
)

type guardFixture struct {
	Name      string          `json:"name"`
	Event     string          `json:"event"`
	Payload   json.RawMessage `json:"payload"`
	Mandatory bool            `json:"mandatory"`
}

func loadGuardFixtures(t *testing.T) []guardFixture {
	t.Helper()
	raw, err := os.ReadFile("guard_fixtures.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixtures []guardFixture
	if err := json.Unmarshal(raw, &fixtures); err != nil {
		t.Fatal(err)
	}
	if len(fixtures) == 0 {
		t.Fatal("guard_fixtures.json names no fixtures")
	}
	return fixtures
}

func TestGuardFixturesDecideMandatory(t *testing.T) {
	t.Parallel()
	for _, fixture := range loadGuardFixtures(t) {
		t.Run(fixture.Name, func(t *testing.T) {
			t.Parallel()
			if got := Mandatory(fixture.Event, fixture.Payload); got != fixture.Mandatory {
				t.Fatalf("Mandatory(%s, %s) = %t, want %t", fixture.Event, fixture.Payload, got, fixture.Mandatory)
			}
		})
	}
}

func TestGuardDecodesThePayloadRatherThanScanningIt(t *testing.T) {
	t.Parallel()
	for name, tc := range map[string]struct {
		payload   string
		mandatory bool
	}{
		"unicode escape spells the name":    {`{"tool_name":"Bash","tool_input":{"command":"\u006bill 14575"}}`, true},
		"kelvin sign spells the name":       {`{"tool_name":"Bash","tool_input":{"command":"\u212aill -9 -1"}}`, true},
		"long s spells the name":            {`{"tool_name":"Bash","tool_input":{"command":"\u017fhutdown -h now"}}`, true},
		"dotted capital i spells the name":  {`{"tool_name":"Bash","tool_input":{"command":"\u0130onice -c 3 x"}}`, true},
		"dotless i spells the name":         {`{"tool_name":"Bash","tool_input":{"command":"\u0131onice -c 3 x"}}`, true},
		"escaped quote splits nothing":      {`{"tool_name":"Bash","tool_input":{"command":"\"pkill\" -x sleep"}}`, true},
		"name only in a key":                {`{"tool_name":"Bash","tool_input":{"kill":"nothing here"}}`, false},
		"name only in the tool name":        {`{"tool_name":"orca","tool_input":{"verb":"list"}}`, true},
		"deep list of dicts":                {`{"tool_name":"mcp__x__y","tool_input":{"steps":[{"run":["renice","-n","5"]}]}}`, true},
		"split across list items":           {`{"tool_name":"mcp__x__y","tool_input":{"argv":["pk","ill"]}}`, false},
		"numbers and booleans are inert":    {`{"tool_name":"mcp__x__y","tool_input":{"count":9,"flag":true,"none":null}}`, false},
		"malformed payload":                 {`{"tool_name":"Bash","tool_input":{"command":"pkill`, false},
		"payload is not an object":          {`["pkill"]`, false},
		"tool input is a bare string":       {`{"tool_name":"Bash","tool_input":"tmux kill-server"}`, true},
		"serialized json alone is inert":    {`{"tool_name":"Bash","tool_input":{"command":"echo ok"},"transcript_path":"/t/killall.jsonl"}`, false},
		"unquoted name in a lone surrogate": {`{"tool_name":"mcp__x__exec","tool_input":{"command":"kill 14575 \udc80"}}`, true},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			if got := Mandatory("PreToolUse", []byte(tc.payload)); got != tc.mandatory {
				t.Fatalf("Mandatory = %t, want %t", got, tc.mandatory)
			}
		})
	}
}

func TestGuardFlagsEachStopToolByExactNameOnEachOfItsEvents(t *testing.T) {
	t.Parallel()
	if len(guard.Tools) == 0 {
		t.Fatal("guard.json names no tools")
	}
	for _, tool := range guard.Tools {
		for _, event := range guard.Events {
			if !Mandatory(event, []byte(`{"tool_name":"`+tool+`","tool_input":{"task_id":"wcn64vfub"}}`)) {
				t.Fatalf("%s on %s is not mandatory", tool, event)
			}
		}
		if !Mandatory("PreToolUse", []byte(`{"tool_name":"`+tool+`"}`)) {
			t.Fatalf("%s with no tool input is not mandatory", tool)
		}
		for name, payload := range map[string]string{
			"mcp suffix":           `{"tool_name":"mcp__orca__` + tool + `","tool_input":{"task_id":"wcn64vfub"}}`,
			"name in a list":       `{"tool_name":["` + tool + `"],"tool_input":{"task_id":"wcn64vfub"}}`,
			"name in a dict":       `{"tool_name":{"name":"` + tool + `"},"tool_input":{}}`,
			"name in command text": `{"tool_name":"Bash","tool_input":{"command":"printf '` + tool + ` wcn64vfub'"}}`,
			"output of the task":   `{"tool_name":"TaskOutput","tool_input":{"task_id":"wcn64vfub"}}`,
		} {
			if Mandatory("PreToolUse", []byte(payload)) {
				t.Fatalf("%s is mandatory for %s", name, tool)
			}
		}
	}
}

func TestGuardCoversOnlyItsEvents(t *testing.T) {
	t.Parallel()
	payload := []byte(`{"tool_name":"Bash","tool_input":{"command":"pkill -x sleep"}}`)
	for _, event := range []string{"PreToolUse", "PermissionRequest"} {
		if !Mandatory(event, payload) {
			t.Fatalf("%s is not mandatory", event)
		}
	}
	for _, event := range []string{"PostToolUse", "Stop", "UserPromptSubmit", ""} {
		if Mandatory(event, payload) {
			t.Fatalf("%q is mandatory", event)
		}
	}
}

func TestGuardFixturesCarryTheEscapedNameAsBytes(t *testing.T) {
	t.Parallel()
	for _, fixture := range loadGuardFixtures(t) {
		if fixture.Name != "json unicode escape in the name" {
			continue
		}
		if !bytes.Contains(fixture.Payload, []byte(`\u006bill`)) || bytes.Contains(fixture.Payload, []byte("kill")) {
			t.Fatalf("the escaped fixture reads %s, want the name spelled only through the escape", fixture.Payload)
		}
		return
	}
	t.Fatal("guard_fixtures.json has no escaped-name fixture")
}

func TestGuardFoldsEveryRuneWhoseSimpleFoldOrbitReachesALetter(t *testing.T) {
	t.Parallel()
	for r := rune(unicode.MaxASCII + 1); r <= unicode.MaxRune; r++ {
		for f := unicode.SimpleFold(r); f != r; f = unicode.SimpleFold(f) {
			if f >= 'a' && f <= 'z' && folds[r] != f {
				t.Errorf("U+%04X folds to %c but the guard maps it to %q", r, f, folds[r])
			}
		}
	}
	for r, letter := range folds {
		if r <= unicode.MaxASCII || letter < 'a' || letter > 'z' {
			t.Errorf("fold U+%04X -> U+%04X is not a non-ASCII rune onto a lowercase ASCII letter", r, letter)
		}
	}
}

func TestGuardInputBoundIsTheWireCeiling(t *testing.T) {
	t.Parallel()
	if guard.MaxEventInput != MaxEventInput {
		t.Fatalf("guard.json bounds input at %d; the wire reads %d", guard.MaxEventInput, MaxEventInput)
	}
}

func TestDenyEnvelopeRendersEachEventShapeWithKindOnlyDiagnostics(t *testing.T) {
	t.Parallel()
	reason := "BLOCKED: the session guard did not complete (host-unavailable), so this call could not be checked for " +
		"a session-ending program and stays denied (AGENTS.md § Protect Existing Sessions). Retry once the Captain " +
		"Hook host answers, or ask the owner to install the host or run the call themselves."
	for event, want := range map[string]string{
		"PreToolUse": `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny",` +
			`"permissionDecisionReason":"` + reason + `"}}`,
		"PermissionRequest": `{"hookSpecificOutput":{"decision":{"behavior":"deny","message":"` + reason + `"},` +
			`"hookEventName":"PermissionRequest"}}`,
	} {
		if got := DenyEnvelope(event, "host-unavailable"); got != want {
			t.Fatalf("DenyEnvelope(%s) = %s, want %s", event, got, want)
		}
	}
	for _, kind := range Kinds() {
		envelope := DenyEnvelope("PreToolUse", kind)
		var decoded struct {
			Output struct {
				Decision string `json:"permissionDecision"`
				Reason   string `json:"permissionDecisionReason"`
			} `json:"hookSpecificOutput"`
		}
		if err := json.Unmarshal([]byte(envelope), &decoded); err != nil {
			t.Fatalf("%s envelope does not decode: %v", kind, err)
		}
		if decoded.Output.Decision != "deny" || !strings.Contains(decoded.Output.Reason, "("+kind+")") {
			t.Fatalf("%s envelope = %s", kind, envelope)
		}
		if strings.Contains(envelope, "/") || strings.Contains(envelope, "{kind}") {
			t.Fatalf("%s envelope carries more than the kind: %s", kind, envelope)
		}
	}
}

func TestDenyEnvelopeRefusesAnUnknownKind(t *testing.T) {
	t.Parallel()
	defer func() {
		if recover() == nil {
			t.Fatal("DenyEnvelope accepted a kind the definition does not name")
		}
	}()
	DenyEnvelope("PreToolUse", "guessed")
}
