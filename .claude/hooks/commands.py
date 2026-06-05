from __future__ import annotations

import re

from captain_hook import (
    Allow,
    BaseHookEvent,
    Block,
    CustomCondition,
    Event,
    Input,
    Tool,
    UsedSkill,
    block_command,
    hook,
    nudge,
)
from captain_hook.events import PostToolUseFailureEvent

block_command(
    ["git", "stash"],
    reason="git stash is not allowed",
    hint="Commit your changes to a branch instead",
    tests={
        Input(command="git stash"): Block(),
        Input(command="git stash pop"): Block(),
        Input(command="git status"): Allow(),
    },
)

class UnpipedGrep(CustomCondition):
    """True when a `grep` command does not consume piped input.

    Allows the stream-filter idiom (`… | grep`) while still blocking grep used
    for file searching, whether standalone, heading a pipe, or in a `&&`/`;` chain.
    """

    def check(self, evt: BaseHookEvent) -> bool:
        if not (cl := evt.command_line):
            return False
        return any(
            cmd.matches(r"^grep\b") and (i == 0 or cl.parts[i - 1][1] != "|")
            for i, (cmd, _) in enumerate(cl.parts)
        )


hook(
    Event.PreToolUse,
    only_if=[Tool("Bash"), UnpipedGrep()],
    message="BLOCKED: Use ripgrep (rg) instead of grep. Replace grep with rg, or use the built-in Grep tool.",
    block=True,
    tests={
        Input(command="grep -rn foo captain_hook/"): Block(),
        Input(command="ls | grep foo"): Allow(),
        Input(command="cat x | grep foo | sort"): Allow(),
        Input(command="grep foo file.py | wc -l"): Block(),
        Input(command="grep foo a && echo done"): Block(),
        Input(command="git log --grep=fix"): Allow(),
        Input(command='git log --grep "fix bug"'): Allow(),
    },
)

block_command(
    r"^ruff\b",
    reason="Do not run ruff manually — mechanical linting is auto-fixed by tooling",
    hint="See AGENTS.md § Mechanical Linting. Only fix issues requiring human judgment.",
    tests={
        Input(command="ruff check ."): Block(),
        Input(command="ruff format captain_hook/"): Block(),
        Input(command="pre-commit run --hook ruff"): Allow(),
    },
)

nudge(
    "MISSING DEPENDENCY: Run `uv sync --extra dev` (or `uv pip install <package>`) to fix this. "
    "Do NOT make imports lazy, remove the importing code, or restructure "
    "code to avoid the import.",
    events=Event.PostToolUseFailure,
    only_if=[Tool("Bash")],
    when=lambda evt: isinstance(evt, PostToolUseFailureEvent)
    and bool(re.search(r"ModuleNotFoundError|ImportError: (?:cannot import|No module named)", evt.error)),
    max_fires=2,
)

nudge(
    """
    Multiple tool failures detected without a /codex invocation. After 2 failed
    approaches, get a second opinion from `/codex` before attempting a 3rd —
    Codex catches errors that Claude may miss.
    """,
    skip_if=[UsedSkill("codex")],
    events=Event.PostToolUseFailure,
    when=lambda evt: evt.ctx.turn.count_failures() >= 2 and not evt.ctx.t.has_command(r"codex"),
)
