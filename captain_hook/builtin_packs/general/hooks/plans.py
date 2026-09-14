from __future__ import annotations

from captain_hook import (
    Allow,
    Block,
    Clause,
    Event,
    Input,
    Phrase,
    RewritingExistingPlan,
    T,
    Tool,
    UsedTool,
    UserSaid,
    hook,
)

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
                T.assistant(T.tool("Write", file_path="/x/plans/p.md", content="# Plan v1")),
                T.assistant(T.tool("Write", file_path="/x/plans/p.md", content="# Plan v2")),
            ],
        ): Block(),
        # A new plan cycle (EnterPlanMode) started since the last write -> allow the rewrite.
        Input(
            tool="Write",
            file="/x/plans/p.md",
            content="# Plan v2",
            transcript=[
                T.assistant(T.tool("Write", file_path="/x/plans/p.md", content="# Plan v1")),
                T.assistant(T.tool("EnterPlanMode")),
                T.assistant(T.tool("Write", file_path="/x/plans/p.md", content="# Plan v2")),
            ],
        ): Allow(),
        # First write of this plan this session -> allow.
        Input(tool="Write", file="/x/plans/p.md", content="# Plan", transcript=[]): Allow(),
        # Not a plan file -> allow.
        Input(tool="Write", file="/x/src/main.py", content="x = 1"): Allow(),
    },
)


hook(
    Event.PreToolUse,
    only_if=[
        Tool.EditTools,
        UserSaid(
            Clause(
                noun=Phrase("mode", "planning"),
                verb=Phrase("enter", "re-enter", "reenter", "return", "go", "switch", "get", "come"),
                subject=("unnamed",),
            ),
            Clause(noun=Phrase("work"), verb=Phrase("do"), negated=True),
            Clause(noun=Phrase("work"), verb=Phrase("stop", "halt", "pause")),
        ),
    ],
    skip_if=[UsedTool("EnterPlanMode")],
    message=(
        "The user told you to stop and go back into plan mode. Call EnterPlanMode and "
        "present a plan for approval before making any more edits."
    ),
    block=True,
    tests={
        Input(
            tool="Edit",
            file="/x/src/main.py",
            content="x = 1",
            transcript=[T.user("Re-enter plan mode, don't do any more work.")],
        ): Block(pattern="plan mode"),
        Input(
            tool="Write",
            file="/x/src/main.py",
            content="x = 1",
            transcript=[T.user("Stop all work until we agree on a plan.")],
        ): Block(),
        Input(
            tool="Edit",
            file="/x/src/main.py",
            content="x = 1",
            transcript=[
                T.user("Re-enter plan mode, don't do any more work."),
                T.assistant(T.tool("EnterPlanMode")),
            ],
        ): Allow(),
        Input(
            tool="Edit",
            file="/x/src/main.py",
            content="x = 1",
            transcript=[T.user("please fix the typo in main.py")],
        ): Allow(),
    },
)
