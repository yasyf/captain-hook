"""Break the agent out of a retry loop after repeated failures."""

from __future__ import annotations

import re

from captain_hook import Allow, Event, Input, ReadFile, Signal, Signals, T, UsedSkill, Warn, nudge

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


nudge(
    "Repeated failures detected. Stop retrying and pick a debug tool:\n"
    "  - run `/codex` for a second opinion on the failing approach\n"
    "  - open the trace in your observability tool to inspect the failing span\n"
    "  - isolate the minimum failing case before changing more code",
    signals=RETRY_SIGNALS,
    events=Event.Stop,
    skip_if=[UsedSkill("codex", scope="session"), ReadFile("DEBUGGING.md")],
    max_fires=1,
    tests={
        Input(transcript=[T.assistant("Same error again. Let me try again.")]): Warn(pattern="debug tool"),
        Input(transcript=[T.assistant("All checks pass; wrapping up.")]): Allow(),
    },
)


nudge(
    "Three tool failures this turn without a debug skill. Read DEBUGGING.md before the next attempt.",
    events=Event.PostToolUseFailure,
    when=lambda evt: evt.ctx.turn.count_failures() >= 3,
    skip_if=[ReadFile("DEBUGGING.md")],
    max_fires=2,
    tests={
        Input(
            transcript=[
                line
                for _ in range(3)
                for line in T.tool_turn("Bash", result="ModuleNotFoundError", is_error=True, command="uv run pytest")
            ]
        ): Warn(pattern="DEBUGGING.md"),
        Input(
            transcript=T.tool_turn("Bash", result="ModuleNotFoundError", is_error=True, command="uv run pytest")
        ): Allow(),
    },
)
