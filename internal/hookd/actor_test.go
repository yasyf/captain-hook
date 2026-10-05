package hookd

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
)

const actorSentinel = "sk-synthetic-actor-key-7f3a"

func clearActorCredentials(t *testing.T) {
	t.Helper()
	for _, variable := range []string{"OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"} {
		t.Setenv(variable, "")
	}
}

type actorEvaluation struct {
	response wireproto.EventResponse
	err      error
	judges   []string
	requests []wireproto.EventRequest
}

func scriptActor(t *testing.T, evaluation *actorEvaluation) {
	t.Helper()
	clearActorCredentials(t)
	previous := evaluateActor
	evaluateActor = func(request wireproto.EventRequest, judge string, _ time.Duration) (wireproto.EventResponse, error) {
		evaluation.judges = append(evaluation.judges, judge)
		evaluation.requests = append(evaluation.requests, request)
		return evaluation.response, evaluation.err
	}
	t.Cleanup(func() { evaluateActor = previous })
}

func TestRunEvaluatesAnActorEventWithoutTheHost(t *testing.T) {
	t.Setenv(actorJudgeEnv, "codex")
	client := scriptedClient{err: errors.New("the host must not be asked")}
	scriptClient(t, &client, nil)
	local := localEvaluation{err: errors.New("the transport fallback must not run")}
	scriptLocal(t, &local)
	deny := `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"BLOCKED"}}` + "\n"
	evaluation := actorEvaluation{response: wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: deny, Guard: wireproto.GuardCompleted}}
	scriptActor(t, &evaluation)
	t.Setenv("OPENAI_API_KEY", actorSentinel)
	code, stdout, stderr := runEvent(t, "PreToolUse", destructivePayload)
	if code != 0 || stdout != deny || stderr != "" {
		t.Fatalf("exit=%d stdout=%q stderr=%q, want the actor's blocking verdict", code, stdout, stderr)
	}
	if len(client.requests) != 0 || len(local.requests) != 0 {
		t.Fatalf("host requests=%d local requests=%d, want neither", len(client.requests), len(local.requests))
	}
	if len(evaluation.judges) != 1 || evaluation.judges[0] != "codex" || !evaluation.requests[0].Mandatory {
		t.Fatalf("actor evaluations = %v %+v", evaluation.judges, evaluation.requests)
	}
	encoded, err := wireproto.MarshalEventRequest(evaluation.requests[0])
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), actorSentinel) || strings.Contains(string(encoded), "OPENAI_API_KEY") {
		t.Fatalf("the actor's key reached the event request: %s", encoded)
	}
	if evaluation.requests[0].Env[actorJudgeEnv] != "codex" {
		t.Fatalf("request env = %v, want the nonsecret judge selection only", evaluation.requests[0].Env)
	}
}

func TestRunRefusesAnActorWithoutItsJudgeVisibly(t *testing.T) {
	for name, tc := range map[string]struct {
		judge, key, reason string
	}{
		"no such judge":        {"gemini", "OPENAI_API_KEY", `CAPT_HOOK_ACTOR_JUDGE="gemini" names no API judge`},
		"no codex key":         {"codex", "", "holds no OPENAI_API_KEY or CODEX_API_KEY"},
		"another provider key": {"claude", "OPENAI_API_KEY", "holds no ANTHROPIC_API_KEY"},
	} {
		for _, event := range []string{"PreToolUse", "PermissionRequest"} {
			for _, mandatory := range []bool{true, false} {
				t.Run(name+"/"+event+"/"+strconv.FormatBool(mandatory), func(t *testing.T) {
					t.Setenv(actorJudgeEnv, tc.judge)
					client := scriptedClient{err: errors.New("the host must not be asked")}
					scriptClient(t, &client, nil)
					local := localEvaluation{err: errors.New("the transport fallback must not run")}
					scriptLocal(t, &local)
					evaluation := actorEvaluation{}
					scriptActor(t, &evaluation)
					if tc.key != "" {
						t.Setenv(tc.key, actorSentinel)
					}
					payload := benignPayload
					if mandatory {
						payload = destructivePayload
					}
					code, stdout, stderr := runEvent(t, event, payload)
					switch {
					case !strings.Contains(stderr, tc.reason):
						t.Fatalf("stderr = %q, want %q", stderr, tc.reason)
					case mandatory && (code != 0 || stdout != wireproto.SkipEnvelope(event, "dependency-unavailable")+"\n" ||
						!strings.HasSuffix(stderr, "capt-hookd: the session guard did not complete (dependency-unavailable); skipped\n")):
						t.Fatalf("exit=%d stdout=%q stderr=%q, want the mandatory skip note", code, stdout, stderr)
					case !mandatory && (code != 1 || stdout != ""):
						t.Fatalf("exit=%d stdout=%q, want a visible failure", code, stdout)
					case len(evaluation.judges) != 0 || len(client.requests) != 0 || len(local.requests) != 0:
						t.Fatalf("a refused actor evaluated: actor=%d host=%d local=%d", len(evaluation.judges), len(client.requests), len(local.requests))
					case strings.Contains(stdout+stderr, actorSentinel):
						t.Fatalf("the refusal printed the key: %q %q", stdout, stderr)
					}
				})
			}
		}
	}
}

func TestRunGradesAFailedActorEvaluationLikeTheHost(t *testing.T) {
	t.Setenv(actorJudgeEnv, "codex")
	scriptClient(t, &scriptedClient{err: errors.New("the host must not be asked")}, nil)
	timedOut := "captain: the actor's evaluator did not answer within the 6s event budget"
	evaluatorError := actorEvaluation{err: errors.New(timedOut)}
	for name, tc := range map[string]struct {
		evaluation   actorEvaluation
		payload      string
		code         int
		kind, stderr string
	}{
		"mandatory evaluator error": {evaluatorError, destructivePayload, 0, "worker-error", timedOut + "\n"},
		"mandatory guard incomplete": {actorEvaluation{response: wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok"}},
			stopPayload, 0, "no-verdict", ""},
		"mandatory worker error": {actorEvaluation{response: wireproto.EventResponse{
			Schema: wireproto.Schema, Status: "error", Exit: 1, Stderr: "Traceback " + actorSentinel + "\n",
		}}, destructivePayload, 0, "worker-error", ""},
		"advisory evaluator error": {evaluatorError, benignPayload, 1, "", timedOut + "\n"},
		"advisory worker exit passes": {actorEvaluation{response: wireproto.EventResponse{
			Schema: wireproto.Schema, Status: "error", Exit: 2, Stderr: "boom\n",
		}}, benignPayload, 2, "", "boom\n"},
	} {
		for _, event := range []string{"PreToolUse", "PermissionRequest"} {
			t.Run(name+"/"+event, func(t *testing.T) {
				local := localEvaluation{err: errors.New("the transport fallback must not run")}
				scriptLocal(t, &local)
				evaluation := tc.evaluation
				scriptActor(t, &evaluation)
				t.Setenv("OPENAI_API_KEY", actorSentinel)
				code, stdout, stderr := runEvent(t, event, tc.payload)
				wantStdout, wantStderr := "", tc.stderr
				if tc.kind != "" {
					wantStdout = wireproto.SkipEnvelope(event, tc.kind) + "\n"
					wantStderr += "capt-hookd: the session guard did not complete (" + tc.kind + "); skipped\n"
				}
				if code != tc.code || stdout != wantStdout || stderr != wantStderr || len(local.requests) != 0 {
					t.Fatalf("exit=%d stdout=%q stderr=%q local=%d, want %d %q %q and no fallback",
						code, stdout, stderr, len(local.requests), tc.code, wantStdout, wantStderr)
				}
				if strings.Contains(stdout+stderr, actorSentinel) || strings.Contains(stdout, "permissionDecision") || strings.Contains(stdout, "behavior") {
					t.Fatalf("stdout=%q stderr=%q carries a decision or the key", stdout, stderr)
				}
			})
		}
	}
}

func TestRunForwardsAnActorsSettledVerdictUnchanged(t *testing.T) {
	t.Setenv(actorJudgeEnv, "codex")
	scriptClient(t, &scriptedClient{err: errors.New("the host must not be asked")}, nil)
	preToolUseDeny := `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny",` +
		`"permissionDecisionReason":"BLOCKED: pkill signals every process matching a name"},` +
		`"systemMessage":"capt-hook: the mandatory hook judged_audit did not complete (JudgeFailure)"}` + "\n"
	permissionDeny := `{"hookSpecificOutput":{"decision":{"behavior":"deny","message":"BLOCKED: pkill"},` +
		`"hookEventName":"PermissionRequest"}}` + "\n"
	for name, tc := range map[string]struct {
		event    string
		response wireproto.EventResponse
		code     int
	}{
		"PreToolUse deny without a guard completion": {"PreToolUse",
			wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: preToolUseDeny}, 0},
		"PermissionRequest deny without a guard completion": {"PermissionRequest",
			wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: permissionDeny}, 0},
		"blocking exit without a guard completion": {"PreToolUse",
			wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stderr: "BLOCKED: pkill\n", Exit: 2}, 2},
		"completed guard with a sibling's stderr": {"PreToolUse",
			wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stderr: "warned\n", Guard: wireproto.GuardCompleted}, 0},
	} {
		t.Run(name, func(t *testing.T) {
			local := localEvaluation{err: errors.New("the transport fallback must not run")}
			scriptLocal(t, &local)
			evaluation := actorEvaluation{response: tc.response}
			scriptActor(t, &evaluation)
			t.Setenv("OPENAI_API_KEY", actorSentinel)
			code, stdout, stderr := runEvent(t, tc.event, destructivePayload)
			if code != tc.code || stdout != tc.response.Stdout || stderr != tc.response.Stderr {
				t.Fatalf("exit=%d stdout=%q stderr=%q, want the actor's verdict unchanged: %d %q %q",
					code, stdout, stderr, tc.code, tc.response.Stdout, tc.response.Stderr)
			}
			if len(evaluation.requests) != 1 || !evaluation.requests[0].Mandatory || len(local.requests) != 0 {
				t.Fatalf("actor requests=%+v local=%d, want one mandatory actor request", evaluation.requests, len(local.requests))
			}
		})
	}
}

func TestRunKeepsTheResidentRouteWithoutAnActorJudge(t *testing.T) {
	client := scriptedClient{response: wireproto.EventResponse{Schema: wireproto.Schema, Status: "ok", Stdout: "resident\n"}}
	scriptClient(t, &client, nil)
	evaluation := actorEvaluation{err: errors.New("the actor route must not run")}
	scriptActor(t, &evaluation)
	t.Setenv("OPENAI_API_KEY", actorSentinel)
	code, stdout, _ := runEvent(t, "PostToolUse", benignPayload)
	if code != 0 || stdout != "resident\n" || len(client.requests) != 1 || len(evaluation.judges) != 0 {
		t.Fatalf("exit=%d stdout=%q host=%d actor=%d", code, stdout, len(client.requests), len(evaluation.judges))
	}
	if _, leaked := client.requests[0].Env["OPENAI_API_KEY"]; leaked {
		t.Fatal("the resident request carries the actor's key")
	}
}

func TestActorBudgetFitsEveryNativeHookTimeout(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("..", "..", "captain_hook", "hooks", "hooks.json"))
	if err != nil {
		t.Fatal(err)
	}
	var manifest struct {
		Hooks map[string][]struct {
			Hooks []struct {
				Timeout int `json:"timeout"`
			} `json:"hooks"`
		} `json:"hooks"`
	}
	if err := json.Unmarshal(raw, &manifest); err != nil {
		t.Fatal(err)
	}
	for event, groups := range manifest.Hooks {
		for _, group := range groups {
			for _, handler := range group.Hooks {
				if handler.Timeout == 0 {
					continue
				}
				native := time.Duration(handler.Timeout) * time.Second
				if actorNativeTimeouts[event] != native {
					t.Errorf("%s declares a %s native timeout, the actor budget knows %s", event, native, actorNativeTimeouts[event])
				}
				if budget := actorBudget(event, defaultRequestTimeout); budget+actorReapGrace >= native {
					t.Errorf("%s budget %s plus reap grace %s does not finish inside its %s native timeout", event, budget, actorReapGrace, native)
				}
			}
		}
	}
	for event, want := range map[string]time.Duration{"MessageDisplay": 6 * time.Second, "PreToolUse": defaultRequestTimeout, "SessionStart": defaultRequestTimeout} {
		if got := actorBudget(event, defaultRequestTimeout); got != want {
			t.Errorf("actorBudget(%s) = %s, want %s", event, got, want)
		}
	}
	if got := actorBudget("MessageDisplay", 3*time.Second); got != 3*time.Second {
		t.Errorf("a shorter caller budget was raised to %s", got)
	}
}

type fakeEvaluator struct {
	dir string
}

func fakeActorPython(t *testing.T, script string) fakeEvaluator {
	t.Helper()
	dir := t.TempDir()
	body := "#!/bin/sh\nset -eu\necho $$ > " + filepath.Join(dir, "pid") +
		"\nprintf 'OPENAI_API_KEY=%s\\n' \"${OPENAI_API_KEY-}\" > " + filepath.Join(dir, "env") +
		"\ncat > " + filepath.Join(dir, "stdin") + "\n" + script
	path := filepath.Join(dir, "python")
	if err := os.WriteFile(path, []byte(body), 0o700); err != nil {
		t.Fatal(err)
	}
	previous, grace := actorPython, actorReapGrace
	actorPython = func() (string, error) { return path, nil }
	actorReapGrace = 200 * time.Millisecond
	t.Cleanup(func() { actorPython, actorReapGrace = previous, grace })
	return fakeEvaluator{dir: dir}
}

func (f fakeEvaluator) read(t *testing.T, name string) string {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(f.dir, name))
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}

func (f fakeEvaluator) gone(t *testing.T) {
	t.Helper()
	pid, err := strconv.Atoi(strings.TrimSpace(f.read(t, "pid")))
	if err != nil {
		t.Fatal(err)
	}
	if err := syscall.Kill(pid, 0); !errors.Is(err, syscall.ESRCH) {
		t.Fatalf("the owned evaluator %d is still alive: %v", pid, err)
	}
}

func actorRequest(t *testing.T, event string) wireproto.EventRequest {
	t.Helper()
	clearActorCredentials(t)
	t.Setenv("CLAUDE_PROJECT_DIR", t.TempDir())
	t.Setenv("OPENAI_API_KEY", actorSentinel)
	request, err := eventRequest(event, strings.NewReader(benignPayload))
	if err != nil {
		t.Fatal(err)
	}
	return request
}

const fakeReply = `{"schema":1,"status":"ok","stdout":"verdict\n","stderr":"","exit":0,"elapsed_ms":1,"warmup":false}`

func TestEvaluateActorAwaitsBackgroundHooksAfterTheReply(t *testing.T) {
	fake := fakeActorPython(t, "printf '%s' '"+fakeReply+"'\nexec 1>&-\nsleep 0.3\ntouch "+"\"$(dirname \"$0\")/background\"\n")
	request := actorRequest(t, "PostToolUse")
	started := time.Now()
	response, err := evaluateActor(request, "codex", 5*time.Second)
	if err != nil || response.Stdout != "verdict\n" || response.Stderr != "" {
		t.Fatalf("evaluateActor = %+v, %v", response, err)
	}
	if _, err := os.Stat(filepath.Join(fake.dir, "background")); err != nil {
		t.Fatalf("evaluateActor returned before its background hooks finished: %v", err)
	}
	if !strings.Contains(fake.read(t, "env"), "OPENAI_API_KEY="+actorSentinel) {
		t.Fatal("the owned evaluator did not inherit the actor's key")
	}
	var sent wireproto.EventRequest
	if err := json.Unmarshal([]byte(fake.read(t, "stdin")), &sent); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(fake.read(t, "stdin"), actorSentinel) {
		t.Fatal("the actor's key reached the evaluator's request")
	}
	if deadline := time.UnixMilli(sent.DeadlineUnixMS); deadline.Before(started.Add(5*time.Second-time.Second)) || deadline.After(started.Add(5*time.Second+time.Second)) {
		t.Fatalf("request deadline %s is not the event budget from %s", deadline, started)
	}
	fake.gone(t)
}

func TestEvaluateActorReapsAnEvaluatorThatNeverAnswers(t *testing.T) {
	fake := fakeActorPython(t, "exec sleep 30\n")
	request := actorRequest(t, "PostToolUse")
	started := time.Now()
	_, err := evaluateActor(request, "codex", time.Second)
	if err == nil || !strings.Contains(err.Error(), "did not answer within the 1s event budget") {
		t.Fatalf("evaluateActor = %v, want the typed timeout", err)
	}
	if elapsed := time.Since(started); elapsed > time.Second+2*actorReapGrace+time.Second {
		t.Fatalf("a stalled evaluator held the hook for %s", elapsed)
	}
	fake.gone(t)
}

func TestEvaluateActorBoundsBackgroundHooksToTheEventBudget(t *testing.T) {
	fake := fakeActorPython(t, "printf '%s' '"+fakeReply+"'\nexec 1>&-\nexec sleep 30\n")
	request := actorRequest(t, "MessageDisplay")
	started := time.Now()
	response, err := evaluateActor(request, "codex", time.Second)
	if err != nil || response.Stdout != "verdict\n" || !strings.Contains(response.Stderr, "background hooks did not finish within the 1s event budget") {
		t.Fatalf("evaluateActor = %+v, %v; want the verdict and a visible background timeout", response, err)
	}
	if elapsed := time.Since(started); elapsed > time.Second+2*actorReapGrace+time.Second {
		t.Fatalf("background hooks held the hook for %s", elapsed)
	}
	fake.gone(t)
}

func TestEvaluateActorRejectsAMalformedReply(t *testing.T) {
	fake := fakeActorPython(t, "printf '%s' '{\"schema\":1,\"status\":\"ok\",\"surprise\":true}'\n")
	_, err := evaluateActor(actorRequest(t, "PostToolUse"), "codex", 5*time.Second)
	if err == nil || !strings.Contains(err.Error(), "decode the actor's event response") {
		t.Fatalf("evaluateActor = %v, want a strict decode failure", err)
	}
	fake.gone(t)
}
