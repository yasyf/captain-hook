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
	ExemptHeads   []string          `json:"exempt_heads"`
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
	assignment  = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*=`)
	opaqueShell = []string{"$(", "`", "<(", ">("}
)

const (
	segmentBreaks = ";\n|()"
	quotedEscapes = "$`\"\\\n"
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
// payload decodes, it is no Bash call whose every command head is exempt, and
// its tool name or any string under tool_input — dict values, list items, and
// every all-string list joined — names a guarded program once quoting is
// stripped and case folds applied.
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
	if fields.ToolName == "Bash" && firstPartyCommand(fields.ToolInput) {
		return false
	}
	return namesGuardedValue(fields.ToolName) || namesGuardedValue(fields.ToolInput)
}

type shellWord struct {
	raw      string
	cooked   string
	redirect bool
}

func firstPartyCommand(toolInput any) bool {
	fields, ok := toolInput.(map[string]any)
	if !ok {
		return false
	}
	command, ok := fields["command"].(string)
	if !ok || slices.ContainsFunc(opaqueShell, func(opaque string) bool { return strings.Contains(command, opaque) }) {
		return false
	}
	segments, ok := shellSegments(command)
	return ok && !slices.ContainsFunc(segments, func(words []shellWord) bool { return !exemptSegment(words) })
}

func exemptSegment(words []shellWord) bool {
	for _, word := range words {
		if !assignment.MatchString(word.raw) {
			return !word.redirect && slices.Contains(guard.ExemptHeads, word.cooked[strings.LastIndex(word.cooked, "/")+1:])
		}
	}
	return true
}

func shellSegments(command string) ([][]shellWord, bool) {
	text := []rune(command)
	segments := [][]shellWord{nil}
	var raw, cooked strings.Builder
	started, redirect, afterRedirect := false, false, false
	endWord := func() {
		if started {
			segments[len(segments)-1] = append(segments[len(segments)-1], shellWord{raw.String(), cooked.String(), redirect})
		}
		raw.Reset()
		cooked.Reset()
		started, redirect = false, false
	}
	for index := 0; index < len(text); index++ {
		char := text[index]
		redirecting := false
		switch {
		case char == '\\' && index+1 == len(text):
			raw.WriteRune(char)
			cooked.WriteRune(char)
			started = true
		case char == '\\':
			index++
			if text[index] != '\n' {
				raw.WriteString(string(text[index-1 : index+1]))
				cooked.WriteRune(text[index])
				started = true
			}
		case char == '\'':
			end := slices.Index(text[index+1:], '\'')
			if end < 0 {
				return nil, false
			}
			end += index + 1
			raw.WriteString(string(text[index : end+1]))
			cooked.WriteString(string(text[index+1 : end]))
			started = true
			index = end
		case char == '"':
			end := index + 1
			for ; end < len(text) && text[end] != '"'; end++ {
				if text[end] == '\\' && end+1 < len(text) && strings.ContainsRune(quotedEscapes, text[end+1]) {
					end++
					if text[end] == '\n' {
						continue
					}
				}
				cooked.WriteRune(text[end])
			}
			if end == len(text) {
				return nil, false
			}
			raw.WriteString(string(text[index : end+1]))
			started = true
			index = end
		case char == ' ' || char == '\t':
			endWord()
		case strings.ContainsRune(segmentBreaks, char) ||
			char == '&' && !afterRedirect && (index+1 == len(text) || text[index+1] != '>'):
			endWord()
			segments = append(segments, nil)
		default:
			raw.WriteRune(char)
			cooked.WriteRune(char)
			started = true
			redirecting = char == '<' || char == '>'
			redirect = redirect || redirecting
		}
		afterRedirect = redirecting
	}
	endWord()
	return segments, true
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
