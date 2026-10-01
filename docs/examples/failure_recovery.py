"""Break the agent out of a retry loop after repeated failures."""

from __future__ import annotations

import re

from captain_hook import Allow, Event, Input, ReadFile, Signal, Signals, T, UsedSkill, Warn, nudge

nudge(
    "Repeated failures mean the current approach is wrong. Run `/codex` for a second opinion before retrying.",
    signals=Signals(
        patterns=[
            Signal(pattern=r"let me try again", weight=2, flags=re.IGNORECASE),
            Signal(pattern=r"one more attempt", weight=2, flags=re.IGNORECASE),
            Signal(pattern=r"same (error|failure|issue)", weight=2, flags=re.IGNORECASE),
            Signal(pattern=r"\bretrying\b", weight=1, flags=re.IGNORECASE),
        ],
        threshold=4,
        window=10,
    ),
    events=Event.Stop,
    skip_if=[UsedSkill("codex", scope="session"), ReadFile("DEBUGGING.md")],
    max_fires=1,
    tests={
        Input(transcript=[T.assistant("Same error again. Let me try again.")]): Warn(pattern="/codex"),
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
