from __future__ import annotations

from captain_hook import Allow, BaseHookEvent, Block, CustomCondition, Event, Input, Tool, hook


class RewritingExistingPlan(CustomCondition):
    """True when a Write targets a plan file (`.md` under `plans/` or `specs/`) that was
    already written earlier this session, with no new plan cycle (EnterPlanMode) since the
    last Write to it.

    Reads from ``evt.ctx.prior`` (the window before the current turn's last exchange) so the
    pending Write being evaluated is never itself counted as the prior edit. A write to the
    file this session already implies it exists, so no filesystem check is needed.
    """

    def check(self, evt: BaseHookEvent) -> bool:
        fp = evt.file
        if not fp or fp.suffix != ".md" or not fp.under("plans/", "specs/"):
            return False
        if not evt.ctx.prior.has_edit_to(str(fp)):
            return False
        return not evt.ctx.prior.after(tool="Write", file=str(fp)).has_tool("EnterPlanMode")


hook(
    Event.PreToolUse,
    only_if=[Tool("Write"), RewritingExistingPlan()],
    message=(
        "This plan file was already written in this planning session. Use the Edit tool "
        "to make incremental changes instead of rewriting the entire plan with Write."
    ),
    block=True,
    tests={
        # Rewriting a plan already written this session, no new plan cycle since -> block.
        Input(
            tool="Write",
            file="/x/plans/p.md",
            content="# Plan v2",
            transcript=[
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Write", "id": "w0",
                     "input": {"file_path": "/x/plans/p.md", "content": "# Plan v1"}}]}},
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Write", "id": "w1",
                     "input": {"file_path": "/x/plans/p.md", "content": "# Plan v2"}}]}},
            ],
        ): Block(),
        # A new plan cycle (EnterPlanMode) started since the last write -> allow the rewrite.
        Input(
            tool="Write",
            file="/x/plans/p.md",
            content="# Plan v2",
            transcript=[
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Write", "id": "w0",
                     "input": {"file_path": "/x/plans/p.md", "content": "# Plan v1"}}]}},
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "EnterPlanMode", "id": "p1", "input": {}}]}},
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Write", "id": "w1",
                     "input": {"file_path": "/x/plans/p.md", "content": "# Plan v2"}}]}},
            ],
        ): Allow(),
        # First write of this plan this session -> allow.
        Input(tool="Write", file="/x/plans/p.md", content="# Plan", transcript=[]): Allow(),
        # Not a plan file -> allow.
        Input(tool="Write", file="/x/src/main.py", content="x = 1"): Allow(),
    },
)
