from __future__ import annotations

from captain_hook import (
    Allow,
    BaseHookEvent,
    Event,
    HookResult,
    Input,
    RanCommand,
    TestFile,
    Tool,
    Warn,
    nudge,
    on,
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


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CommandCondition(r"git\s+commit")],
    skip_if=[RanCommand(r"uv run pytest")],
    tests={
        Input(command="git status"): Allow(),
        Input(command="git commit -m wip"): Warn(),
    },
)
def commit_test_gate(evt: BaseHookEvent) -> HookResult | None:
    if evt.ctx.t.user_said("commit", "just commit"):
        return None
    if evt.ctx.t.all_edits_under("docs/", ".claude/", ".github/"):
        return None

    if not (cl := evt.command_line) or ".py" not in str(cl.primary):
        return evt.warn("""
            No `uv run pytest` execution detected in this session. If you changed
            Python files, run tests before committing. If this is a docs/config-only
            change, proceed.
        """)

    return evt.block("No `uv run pytest` execution found. Run tests before committing Python changes.")
