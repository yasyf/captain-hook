"""Nudge toward the corrections lifecycle when a session ends on a durable correction."""

from __future__ import annotations

import re

from captain_hook import Allow, Event, Input, Signal, Signals, Warn, nudge

CORRECTION_SIGNALS = Signals(
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
)


nudge(
    "You just took a durable correction from the user. You don't have to hand-write the hook: "
    "captain-hook's session reviewer is already mining corrections like this one. Run "
    "`uvx capt-hook status` to watch it climb toward a hook pull request.",
    signals=CORRECTION_SIGNALS,
    events=Event.Stop,
    max_fires=1,
    tests={
        Input(
            transcript=[
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {"type": "text", "text": "No — never force-push to main, always open a pull request."},
                        ]
                    },
                },
            ]
        ): Warn(pattern="capt-hook status"),
        Input(
            transcript=[
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Looks good to me, ship it."},
                        ]
                    },
                },
            ]
        ): Allow(),
    },
)
