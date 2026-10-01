from __future__ import annotations

from captain_hook import Allow, Event, Input, Tool, ToolInput, Warn, WorkflowScript, nudge

nudge(
    "This workflow runs verification lanes with no implementation lane for a fix already in hand. "
    "Add one with `isolation: 'worktree'` and ship it only if the verdict confirms the fix.",
    only_if=[
        WorkflowScript(
            pattern=r"(?i)(\badversar|\brefut|security\s+(review|audit)|ground.?truth|\brepro(duce|duction)?\b|-race\b)"
        ),
    ],
    skip_if=[WorkflowScript(pattern=r"(?i)(worktree|\bimplement|\bapplied\b)")],
    events=Event.PreToolUse,
    tests={
        Input(
            script="const verdicts = await parallel([() => agent(`Adversarially refute: the pool "
            "double-close under concurrent Get/Put`), () => agent(`Security review of the auth "
            "diff; findings as JSON`)])"
        ): Warn(pattern="worktree"),
        Input(script="agent('Loop the repro under go test -race for a ground-truth verdict')"): Warn(),
        Input(
            script="await parallel([() => agent(`Adversarially refute: the double-close`), () => "
            "agent(`Implement the mutex fix`, {isolation: 'worktree'})])"
        ): Allow(),
        Input(script="agent('Verify the applied fix under go test -race')"): Allow(),
        Input(script="agent('render the docs site and report broken links')"): Allow(),
    },
)


nudge(
    "This spawn runs verification with no implementation lane beside it for a fix already in hand. "
    "Spawn one in an isolated worktree and ship it only if the verdict confirms the fix.",
    only_if=[
        Tool("Agent|Task"),
        ToolInput("prompt", r"(?i)(\badversar|\brefut|security\s+(review|audit)|ground.?truth|-race\b)"),
    ],
    skip_if=[ToolInput("prompt", r"(?i)(worktree|\bimplement|\bapplied\b)")],
    events=Event.PreToolUse,
    tests={
        Input(prompt="Adversarially refute: the pool double-close happens under concurrent Get/Put"): Warn(
            pattern="worktree"
        ),
        Input(prompt="Security review of the pending auth changes; findings as JSON"): Warn(),
        Input(prompt="Implement the mutex fix in an isolated worktree while the -race loop runs"): Allow(),
        Input(prompt="Loop go test -race on the applied fix for a ground-truth verdict"): Allow(),
        Input(prompt="Enumerate the callers of Close in internal/pool"): Allow(),
    },
)
