"""Nudge toward the corrections lifecycle when a session ends on a durable correction."""

from __future__ import annotations

import re

from captain_hook import Allow, Event, Input, Signal, Signals, T, Warn, nudge

nudge(
    "A durable correction was just given, and the session reviewer turns those into hooks. "
    "Run `uvx capt-hook status` to see its progress.",
    signals=Signals(
        patterns=[
            Signal(pattern=r"\bnever\b", weight=2, flags=re.IGNORECASE),
            Signal(pattern=r"\b(always|from now on)\b", weight=2, flags=re.IGNORECASE),
            Signal(pattern=r"\b(use|prefer)\b.+\b(not|instead of)\b", weight=2, flags=re.IGNORECASE),
            Signal(pattern=r"\b(stop|don'?t)\b", weight=1, flags=re.IGNORECASE),
            Signal(pattern=r"\b(no,|actually,|that'?s wrong|i told you)\b", weight=1, flags=re.IGNORECASE),
        ],
        threshold=3,
        window=6,
        origin="any",
    ),
    events=Event.Stop,
    max_fires=1,
    tests={
        Input(transcript=[T.user("No — never force-push to main, always open a pull request.")]): Warn(
            pattern="capt-hook status"
        ),
        Input(transcript=[T.user("Looks good to me, ship it.")]): Allow(),
    },
)
