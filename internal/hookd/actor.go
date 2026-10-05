package hookd

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"slices"
	"strings"
	"time"

	"github.com/yasyf/captain-hook/internal/wireproto"
)

const actorJudgeEnv = "CAPT_HOOK_ACTOR_JUDGE"

var (
	actorKeys = map[string][]string{
		"codex":  {"OPENAI_API_KEY", "CODEX_API_KEY"},
		"claude": {"ANTHROPIC_API_KEY"},
	}
	actorNativeTimeouts = map[string]time.Duration{"MessageDisplay": 10 * time.Second}
	actorReapGrace      = 2 * time.Second
	actorPython         = installedPython
)

func actorReady(judge string) error {
	names, ok := actorKeys[judge]
	if !ok {
		return fmt.Errorf("capt-hookd: %s=%q names no API judge; set codex or claude", actorJudgeEnv, judge)
	}
	if !slices.ContainsFunc(names, func(name string) bool { return os.Getenv(name) != "" }) {
		return fmt.Errorf("capt-hookd: %s=%s, but the actor's environment holds no %s", actorJudgeEnv, judge, strings.Join(names, " or "))
	}
	return nil
}

func actorBudget(event string, timeout time.Duration) time.Duration {
	if native, ok := actorNativeTimeouts[event]; ok {
		return min(timeout, native-2*actorReapGrace)
	}
	return timeout
}

var evaluateActor = func(request wireproto.EventRequest, judge string, timeout time.Duration) (wireproto.EventResponse, error) {
	python, err := actorPython()
	if err != nil {
		return wireproto.EventResponse{}, err
	}
	budget := actorBudget(request.Event, timeout)
	deadline := time.Now().Add(budget)
	request.DeadlineUnixMS = deadline.UnixMilli()
	payload, err := wireproto.MarshalEventRequest(request)
	if err != nil {
		return wireproto.EventResponse{}, err
	}
	ctx, cancel := context.WithDeadline(context.Background(), deadline.Add(actorReapGrace))
	defer cancel()
	cmd := exec.CommandContext(ctx, python, "-P", "-m", "captain_hook.worker", "evaluate-actor", judge)
	cmd.Dir, cmd.Stdin = workerDir(request.Root), bytes.NewReader(payload)
	cmd.WaitDelay = actorReapGrace
	reply, err := cmd.StdoutPipe()
	if err != nil {
		return wireproto.EventResponse{}, err
	}
	if err := cmd.Start(); err != nil {
		return wireproto.EventResponse{}, fmt.Errorf("captain: evaluate for the actor: %w", err)
	}
	result, readErr := io.ReadAll(reply)
	answered := ctx.Err() == nil
	waitErr := cmd.Wait()
	switch {
	case !answered:
		return wireproto.EventResponse{}, fmt.Errorf("captain: the actor's evaluator did not answer within the %s event budget", budget)
	case readErr != nil:
		return wireproto.EventResponse{}, fmt.Errorf("captain: read the actor's evaluation: %w", readErr)
	}
	var response wireproto.EventResponse
	if err := decodeStrict(result, &response); err != nil {
		return wireproto.EventResponse{}, fmt.Errorf("captain: decode the actor's event response: %w (evaluator: %v)", err, waitErr)
	}
	if err := response.Validate(); err != nil {
		return wireproto.EventResponse{}, err
	}
	if waitErr != nil {
		response.Stderr += fmt.Sprintf("capt-hookd: the actor's background hooks did not finish within the %s event budget: %v\n", budget, waitErr)
	}
	return response, nil
}
