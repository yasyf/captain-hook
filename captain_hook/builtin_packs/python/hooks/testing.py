from __future__ import annotations

from captain_hook import (
    Allow,
    Block,
    Commits,
    Event,
    Input,
    RanCommand,
    Runs,
    TestFile,
    Tool,
    Warn,
    gate,
    nudge,
)
from captain_hook.conditions import AllEditsUnder, UserSaid

nudge(
    """
    When a test fails, isolate the minimal failing case before retrying. Use a
    node-id suffix, `-k`, or `--last-failed`. Broad re-runs after a failure waste
    cycles and hide the real breakage.
    """,
    only_if=[Tool("Edit|Write"), TestFile()],
)


gate(
    "No `uv run pytest` execution found. Run tests before committing Python changes.",
    only_if=[Tool("Bash"), Runs("git", "commit"), Commits(".py")],
    skip_if=[
        RanCommand("uv", "run", "pytest"),
        RanCommand("pytest"),
        UserSaid("commit", "just commit"),
        AllEditsUnder("docs/", ".claude/", ".github/"),
    ],
    events=Event.PreToolUse,
    tests={
        Input(command="git status"): Allow(),
        Input(command="git commit pkg/mod.py"): Block(),
    },
)


nudge(
    "No `uv run pytest` execution detected in this session. If you changed Python files, run "
    "tests before committing. If this is a docs/config-only change, proceed.",
    only_if=[Tool("Bash"), Runs("git", "commit")],
    skip_if=[
        RanCommand("uv", "run", "pytest"),
        RanCommand("pytest"),
        UserSaid("commit", "just commit"),
        AllEditsUnder("docs/", ".claude/", ".github/"),
        Commits(".py"),
    ],
    events=Event.PreToolUse,
    tests={
        Input(command="git status"): Allow(),
        Input(command="git commit -m wip"): Warn(),
    },
)
