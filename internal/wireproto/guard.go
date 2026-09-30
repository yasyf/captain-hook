package wireproto

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"regexp"
	"slices"
	"strings"
)

// guardDefinition is the one definition of the session guard's prefilter,
// shared with the Python guard and the stdlib shim, which render it at build
// time. It decides only whether an event is mandatory — one the client must
// see the guard complete before the call may proceed — never a verdict.
//
//go:embed guard.json
var guardDefinition []byte

type guardSpec struct {
	Events        []string          `json:"events"`
	Quoting       string            `json:"quoting"`
	Guarded       string            `json:"guarded"`
	Folds         map[string]string `json:"folds"`
	Kinds         []string          `json:"kinds"`
	Reason        string            `json:"reason"`
	MaxEventInput int               `json:"max_event_input"`
}

var (
	guard       = loadGuard()
	guardedWord = regexp.MustCompile("(?i)" + guard.Guarded)
	quoting     = regexp.MustCompile(guard.Quoting)
	folds       = loadFolds(guard.Folds)
)

func loadGuard() guardSpec {
	var spec guardSpec
	if err := json.Unmarshal(guardDefinition, &spec); err != nil {
		panic(fmt.Sprintf("captain: guard definition: %v", err))
	}
	return spec
}

// loadFolds maps every rune whose case-fold orbit reaches an ASCII letter onto
// that letter, so the ASCII word boundary in guarded sees the letter the
// Python guard's Unicode case folding already matches (KELVIN SIGN for k, LONG
// S for s, the dotted and dotless capital i for i).
func loadFolds(table map[string]string) map[rune]rune {
	folded := make(map[rune]rune, len(table))
	for source, target := range table {
		folded[[]rune(source)[0]] = []rune(target)[0]
	}
	return folded
}

// Kinds names every way a mandatory event can end without the guard's
// completion; DenyEnvelope accepts exactly these.
func Kinds() []string {
	return slices.Clone(guard.Kinds)
}

// Mandatory reports whether the guard's prefilter covers this event: the
// payload decodes, and its tool name or any string anywhere under tool_input —
// dict values, list items, and every all-string list joined — names a guarded
// program once the guard's quoting characters are stripped and its case folds
// applied. A payload the worker could not decode either is not mandatory.
func Mandatory(event string, payload []byte) bool {
	if !slices.Contains(guard.Events, event) {
		return false
	}
	var fields struct {
		ToolName  any `json:"tool_name"`
		ToolInput any `json:"tool_input"`
	}
	if err := json.Unmarshal(payload, &fields); err != nil {
		return false
	}
	return namesGuardedValue(fields.ToolName) || namesGuardedValue(fields.ToolInput)
}

func namesGuarded(text string) bool {
	return guardedWord.MatchString(strings.Map(foldRune, quoting.ReplaceAllLiteralString(text, "")))
}

func foldRune(r rune) rune {
	if folded, ok := folds[r]; ok {
		return folded
	}
	return r
}

func namesGuardedValue(value any) bool {
	switch value := value.(type) {
	case string:
		return namesGuarded(value)
	case []any:
		if joined, ok := joinedStrings(value); ok && namesGuarded(joined) {
			return true
		}
		return slices.ContainsFunc(value, namesGuardedValue)
	case map[string]any:
		for _, item := range value {
			if namesGuardedValue(item) {
				return true
			}
		}
	}
	return false
}

func joinedStrings(items []any) (string, bool) {
	words := make([]string, 0, len(items))
	for _, item := range items {
		word, ok := item.(string)
		if !ok {
			return "", false
		}
		words = append(words, word)
	}
	return strings.Join(words, " "), true
}

// DenyEnvelope renders the hook stdout that denies a mandatory event whose
// guard did not complete for kind, in the shape event's hook protocol reads.
// The reason names the kind and nothing else: no payload, path, or error text.
func DenyEnvelope(event, kind string) string {
	if !slices.Contains(guard.Kinds, kind) {
		panic(fmt.Sprintf("captain: unknown guard kind %q", kind))
	}
	reason := strings.ReplaceAll(guard.Reason, "{kind}", kind)
	var output map[string]any
	if event == "PermissionRequest" {
		output = map[string]any{
			"hookEventName": event,
			"decision":      map[string]any{"behavior": "deny", "message": reason},
		}
	} else {
		output = map[string]any{
			"hookEventName":            event,
			"permissionDecision":       "deny",
			"permissionDecisionReason": reason,
		}
	}
	envelope, err := Marshal(map[string]any{"hookSpecificOutput": output})
	if err != nil {
		panic(fmt.Sprintf("captain: encode deny envelope: %v", err))
	}
	return string(envelope)
}
