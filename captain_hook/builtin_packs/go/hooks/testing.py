from __future__ import annotations

from captain_hook import (
    Allow,
    Block,
    Commits,
    Event,
    FilePath,
    Input,
    LambdaCondition,
    RanCommand,
    Runs,
    Tool,
    Warn,
    hook,
    nudge,
)
from captain_hook.conditions import AllEditsUnder, UserSaid
from captain_hook.util.builds import LOAD_PER_CORE, HostLoad

SATURATED = {LOAD_PER_CORE: "0"}
HEALTHY = {LOAD_PER_CORE: "1000000"}
HOST_SATURATED = LambdaCondition(lambda _: HostLoad.sample().saturated)

nudge(
    "Verify test edits with the narrowest run, not the whole suite. Run `go test -run TestName ./path/to/pkg`.",
    only_if=[Tool("Edit|Write"), FilePath("*_test.go")],
    skip_if=[HOST_SATURATED],
    tests={
        Input(file="internal/cli/root_test.go", content="package cli", env=HEALTHY): Warn(),
        Input(file="internal/cli/root_test.go", content="package cli", env=SATURATED): Allow(),
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
        HOST_SATURATED,
    ],
    block=True,
    tests={
        Input(command="git status"): Allow(),
        Input(command="git commit internal/cli/root.go", env=HEALTHY): Block(),
        Input(command="git commit internal/cli/root.go", env=SATURATED): Allow(),
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
        HOST_SATURATED,
    ],
    events=Event.PreToolUse,
    tests={
        Input(command="git status"): Allow(),
        Input(command="git commit -m wip", env=HEALTHY): Warn(),
        Input(command="git commit -m wip", env=SATURATED): Allow(),
    },
)
