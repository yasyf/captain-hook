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
    gate,
    nudge,
)
from captain_hook.conditions import AllEditsUnder, UserSaid

nudge(
    """
    When a test fails, isolate the minimal failing case before retrying. Use a
    `-run TestName/case` regex or a single package path. Broad `go test ./...`
    re-runs after a failure waste cycles and hide the real breakage.
    """,
    only_if=[Tool("Edit|Write"), FilePath("*_test.go")],
)


gate(
    "No `go test` execution found. Run tests before committing Go changes.",
    only_if=[Tool("Bash"), Runs("git", "commit"), Commits(".go")],
    skip_if=[
        RanCommand("go", "test"),
        UserSaid("commit", "just commit"),
        AllEditsUnder("docs/", ".claude/", ".github/"),
    ],
    events=Event.PreToolUse,
    tests={
        Input(command="git status"): Allow(),
        Input(command="git commit internal/cli/root.go"): Block(),
    },
)


nudge(
    "No `go test` execution detected in this session. If you changed Go files, run tests "
    "before committing. If this is a docs/config-only change, proceed.",
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
