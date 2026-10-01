from __future__ import annotations

from captain_hook import (
    Allow,
    Block,
    Commits,
    Event,
    FilePath,
    Input,
    RanCommand,
    Runs,
    Tool,
    Warn,
    hook,
    nudge,
)
from captain_hook.conditions import AllEditsUnder, UserSaid

nudge(
    "Verify test edits with the narrowest run, not the whole suite. Run `go test -run TestName ./path/to/pkg`.",
    only_if=[Tool("Edit|Write"), FilePath("*_test.go")],
    tests={
        Input(file="internal/cli/root_test.go", content="package cli"): Warn(),
        Input(file="internal/cli/root.go", content="package cli"): Allow(),
    },
)


hook(
    Event.PreToolUse,
    "No `go test` run found this session. Run `go test ./...` before committing Go changes.",
    only_if=[Tool("Bash"), Runs("git", "commit"), Commits(".go")],
    skip_if=[
        RanCommand("go", "test"),
        UserSaid("commit", "just commit"),
        AllEditsUnder("docs/", ".claude/", ".github/"),
    ],
    block=True,
    tests={
        Input(command="git status"): Allow(),
        Input(command="git commit internal/cli/root.go"): Block(),
    },
)


nudge(
    "No `go test` run exists and the commit names no paths. Run `go test ./...` before committing Go changes.",
    only_if=[Tool("Bash"), Runs("git", "commit")],
    skip_if=[
        RanCommand("go", "test"),
        UserSaid("commit", "just commit"),
        AllEditsUnder("docs/", ".claude/", ".github/"),
        Commits(".go"),
    ],
    events=Event.PreToolUse,
    tests={
        Input(command="git status"): Allow(),
        Input(command="git commit -m wip"): Warn(),
    },
)
