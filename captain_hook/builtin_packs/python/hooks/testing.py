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
    hook,
    nudge,
)
from captain_hook.conditions import AllEditsUnder, UserSaid

nudge(
    "Verify test edits with the narrowest run, not the whole suite. Run `uv run pytest path/to/test.py::test_name`.",
    only_if=[Tool("Edit|Write"), TestFile()],
    tests={
        Input(file="tests/test_mod.py", content="def test_x(): ..."): Warn(),
        Input(file="pkg/mod.py", content="x = 1"): Allow(),
    },
)


hook(
    Event.PreToolUse,
    "No `uv run pytest` run found this session. Run `uv run pytest` before committing Python changes.",
    only_if=[Tool("Bash"), Runs("git", "commit"), Commits(".py")],
    skip_if=[
        RanCommand("uv", "run", "pytest"),
        RanCommand("pytest"),
        UserSaid("commit", "just commit"),
        AllEditsUnder("docs/", ".claude/", ".github/"),
    ],
    block=True,
    tests={
        Input(command="git status"): Allow(),
        Input(command="git commit pkg/mod.py"): Block(),
    },
)


nudge(
    "No pytest run exists and the commit names no paths. Run `uv run pytest` before committing Python changes.",
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
