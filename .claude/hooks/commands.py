from __future__ import annotations

import re

from captain_hook import (
    Allow,
    Block,
    Event,
    Input,
    Tool,
    UsedSkill,
    block_command,
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

block_command(
    r"^grep\b",
    reason="Use ripgrep (rg) instead of grep",
    hint="Replace grep with rg, or use the built-in Grep tool",
    tests={
        Input(command="grep -rn foo captain_hook/"): Block(),
        Input(command="ls | grep foo"): Block(),
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
