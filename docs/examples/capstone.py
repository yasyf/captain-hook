"""Compose a safety block and a workflow gate in one hooks file."""

from __future__ import annotations

from captain_hook import (
    Allow,
    Block,
    Input,
    RanCommand,
    TouchedFile,
    block_command,
    gate,
)

block_command(
    r"git\s+push\s+--force(?!-)",
    reason="Force-push rewrites shared history",
    hint="Run `git push --force-with-lease` instead",
    tests={
        Input(command="git push --force origin main"): Block(),
        Input(command="git push --force-with-lease"): Allow(),
    },
)

gate(
    "You edited Python files but never ran the tests. Run `uv run pytest` before finishing.",
    only_if=[TouchedFile("**/*.py")],
    skip_if=[RanCommand("uv", "run", "pytest"), RanCommand("pytest")],
)
