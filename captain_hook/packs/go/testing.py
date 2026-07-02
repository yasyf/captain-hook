from __future__ import annotations

from captain_hook import (
    Allow,
    BaseHookEvent,
    Block,
    Command,
    CustomCondition,
    Event,
    FilePath,
    Input,
    RanCommand,
    Tool,
    Warn,
    gate,
    nudge,
)

nudge(
    """
    When a test fails, isolate the minimal failing case before retrying. Use a
    `-run TestName/case` regex or a single package path. Broad `go test ./...`
    re-runs after a failure waste cycles and hide the real breakage.
    """,
    only_if=[Tool("Edit|Write"), FilePath("*_test.go")],
)


class UserSaid(CustomCondition):
    """Matches when the user's messages contain any of the given keywords."""

    def __init__(self, *keywords: str) -> None:
        self.keywords = keywords

    def check(self, evt: BaseHookEvent) -> bool:
        return evt.ctx.t.user_said(*self.keywords)


class AllEditsUnder(CustomCondition):
    """Matches when every edit this session is under one of the given path prefixes."""

    def __init__(self, *prefixes: str) -> None:
        self.prefixes = prefixes

    def check(self, evt: BaseHookEvent) -> bool:
        return bool(files := evt.ctx.t.edited_files) and all(f.under(*self.prefixes) for f in files)


class CommitsGo(CustomCondition):
    """Matches when the git command explicitly names a Go path."""

    def check(self, evt: BaseHookEvent) -> bool:
        return bool(cl := evt.command_line) and ".go" in str(cl.primary)


gate(
    "No `go test` execution found. Run tests before committing Go changes.",
    only_if=[Tool("Bash"), Command(r"git\s+commit"), CommitsGo()],
    skip_if=[
        RanCommand(r"go test"),
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
    only_if=[Tool("Bash"), Command(r"git\s+commit")],
    skip_if=[
        RanCommand(r"go test"),
        UserSaid("commit", "just commit"),
        AllEditsUnder("docs/", ".claude/", ".github/"),
        CommitsGo(),
    ],
    events=Event.PreToolUse,
    tests={
        Input(command="git status"): Allow(),
        Input(command="git commit -m wip"): Warn(),
    },
)
