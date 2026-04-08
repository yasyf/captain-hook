# Example: Signal-driven nudge
#
# Demonstrates how to use `nudge` with `Signals` and `Signal` to
# detect patterns in recent transcript text and warn the agent.
# Signal scoring sums weights from matching regex patterns; the
# nudge fires when the cumulative score meets the threshold.

import re

from captain_hook import Signal, Signals, nudge

# Warn when the agent shows signs of blind retrying.
nudge(
    "Stop retrying blindly — narrow the failing test case first.",
    signals=Signals(
        patterns=[
            Signal(pattern=r"let me try again", weight=2, flags=re.IGNORECASE),
            Signal(pattern=r"retry", weight=1, flags=re.IGNORECASE),
            Signal(pattern=r"one more attempt", weight=2, flags=re.IGNORECASE),
        ],
        threshold=3,
        window=5,
    ),
)
