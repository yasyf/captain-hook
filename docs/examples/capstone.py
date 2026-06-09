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

# A complete .claude/hooks/ file: a safety block and a workflow gate composing in one
# place. block_command is unit-tested below; the gate fires on live session state, so
# you verify it by replaying a session, not with inline tests.

# 1. Safety: never let the agent force-push.
block_command(
    r"git\s+push\s+--force(?!-)",
    reason="Force-push rewrites shared history",
    hint="Use --force-with-lease",
    tests={
        Input(command="git push --force origin main"): Block(),
        Input(command="git push --force-with-lease"): Allow(),
    },
)

# 2. Workflow: don't finish a Python change without running the tests.
gate(
    "You edited Python files but never ran the tests. Run `uv run pytest` before finishing.",
    only_if=[TouchedFile("**/*.py")],
    skip_if=[RanCommand(r"\bpytest\b")],
)
