from __future__ import annotations

import re

from captain_hook import Event, ReadFile, Signal, Signals, UsedSkill, nudge

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
    skip_if=[UsedSkill("codex"), ReadFile("DEBUGGING.md")],
    max_fires=1,
)


nudge(
    "Three consecutive tool failures without a debug skill. "
    "Read DEBUGGING.md before the next attempt.",
    events=Event.PostToolUseFailure,
    when=lambda evt: evt.ctx.turn.count_failures() >= 3,
    skip_if=[ReadFile("DEBUGGING.md")],
    max_fires=2,
)
