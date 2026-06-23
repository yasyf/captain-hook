"""Break the agent out of a retry loop after repeated failures."""

from __future__ import annotations

import re

from captain_hook import Allow, Event, Input, ReadFile, Signal, Signals, UsedSkill, Warn, nudge

RETRY_SIGNALS = Signals(
    patterns=[
        Signal(pattern=r"let me try again", weight=2, flags=re.IGNORECASE),
        Signal(pattern=r"one more attempt", weight=2, flags=re.IGNORECASE),
        Signal(pattern=r"same (error|failure|issue)", weight=2, flags=re.IGNORECASE),
        Signal(pattern=r"\bretrying\b", weight=1, flags=re.IGNORECASE),
    ],
    threshold=4,
    window=10,
)

THREE_FAILURES = [
    msg
    for i in range(3)
    for msg in (
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "id": f"b{i}", "input": {"command": "uv run pytest"}},
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": f"b{i}", "is_error": True, "content": "ModuleNotFoundError"},
                ]
            },
        },
    )
]


nudge(
    "Repeated failures detected. Stop retrying and pick a debug tool:\n"
    "  - run `/codex` for a second opinion on the failing approach\n"
    "  - open the trace in your observability tool to inspect the failing span\n"
    "  - isolate the minimum failing case before changing more code",
    signals=RETRY_SIGNALS,
    events=Event.Stop,
    skip_if=[UsedSkill("codex"), ReadFile("DEBUGGING.md")],
    max_fires=1,
    tests={
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Same error again. Let me try again."},
                        ]
                    },
                },
            ]
        ): Warn(pattern="debug tool"),
        Input(
            transcript=[
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "All checks pass; wrapping up."},
                        ]
                    },
                },
            ]
        ): Allow(),
    },
)


nudge(
    "Three tool failures this turn without a debug skill. Read DEBUGGING.md before the next attempt.",
    events=Event.PostToolUseFailure,
    when=lambda evt: evt.ctx.turn.count_failures() >= 3,
    skip_if=[ReadFile("DEBUGGING.md")],
    max_fires=2,
    tests={
        Input(transcript=THREE_FAILURES): Warn(pattern="DEBUGGING.md"),
        Input(transcript=THREE_FAILURES[:2]): Allow(),
    },
)
