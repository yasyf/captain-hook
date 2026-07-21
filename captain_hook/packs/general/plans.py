from __future__ import annotations

from captain_hook import Allow, Block, Event, Input, RewritingExistingPlan, Tool, hook

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
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Write",
                                "id": "w0",
                                "input": {"file_path": "/x/plans/p.md", "content": "# Plan v1"},
                            }
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Write",
                                "id": "w1",
                                "input": {"file_path": "/x/plans/p.md", "content": "# Plan v2"},
                            }
                        ]
                    },
                },
            ],
        ): Block(),
        # A new plan cycle (EnterPlanMode) started since the last write -> allow the rewrite.
        Input(
            tool="Write",
            file="/x/plans/p.md",
            content="# Plan v2",
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Write",
                                "id": "w0",
                                "input": {"file_path": "/x/plans/p.md", "content": "# Plan v1"},
                            }
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": "EnterPlanMode", "id": "p1", "input": {}}]},
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Write",
                                "id": "w1",
                                "input": {"file_path": "/x/plans/p.md", "content": "# Plan v2"},
                            }
                        ]
                    },
                },
            ],
        ): Allow(),
        # First write of this plan this session -> allow.
        Input(tool="Write", file="/x/plans/p.md", content="# Plan", transcript=[]): Allow(),
        # Not a plan file -> allow.
        Input(tool="Write", file="/x/src/main.py", content="x = 1"): Allow(),
    },
)
