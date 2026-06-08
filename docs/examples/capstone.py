from __future__ import annotations

from captain_hook import (
    Allow,
    Block,
    Event,
    Input,
    RanCommand,
    TouchedFile,
    audit,
    block_command,
    gate,
)

# A complete .claude/hooks/ file: safety, workflow, and an audit trail composing in
# one place. block_command is unit-tested below; the gate and audit fire on live
# session state, so you verify them by replaying a session, not with inline tests.

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

# 3. Audit: record every tool call and stop for later review.
audit(Event.PreToolUse | Event.PostToolUse | Event.Stop, log_dir=".context/hook-audit")
