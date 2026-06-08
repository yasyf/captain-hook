from __future__ import annotations

from captain_hook import (
    Allow,
    BaseHookEvent,
    Block,
    CustomCondition,
    Event,
    Input,
    RanCommand,
    TestFile,
    Tool,
    Warn,
    gate,
    nudge,
)
from captain_hook.types import Command as CommandCondition

nudge(
    """
    When a test fails, isolate the minimal failing case before retrying. Use a
    node-id suffix, `-k`, or `--last-failed`. Broad re-runs after a failure waste
    cycles and hide the real breakage.
    """,
    only_if=[Tool("Edit|Write"), TestFile()],
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
        return evt.ctx.t.all_edits_under(*self.prefixes)


class CommitsPython(CustomCondition):
    """Matches when the git command explicitly names a Python path."""

    def check(self, evt: BaseHookEvent) -> bool:
        return bool(cl := evt.command_line) and ".py" in str(cl.primary)


gate(
    "No `uv run pytest` execution found. Run tests before committing Python changes.",
    only_if=[Tool("Bash"), CommandCondition(r"git\s+commit"), CommitsPython()],
    skip_if=[RanCommand(r"uv run pytest"), UserSaid("commit", "just commit"), AllEditsUnder("docs/", ".claude/", ".github/")],
    events=Event.PreToolUse,
    tests={
        Input(command="git status"): Allow(),
        Input(command="git commit pkg/mod.py"): Block(),
    },
)


nudge(
    "No `uv run pytest` execution detected in this session. If you changed Python files, run "
    "tests before committing. If this is a docs/config-only change, proceed.",
    only_if=[Tool("Bash"), CommandCondition(r"git\s+commit")],
    skip_if=[
        RanCommand(r"uv run pytest"),
        UserSaid("commit", "just commit"),
        AllEditsUnder("docs/", ".claude/", ".github/"),
        CommitsPython(),
    ],
    events=Event.PreToolUse,
    tests={
        Input(command="git status"): Allow(),
        Input(command="git commit -m wip"): Warn(),
    },
)
