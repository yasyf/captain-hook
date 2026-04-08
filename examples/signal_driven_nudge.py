# Example: Signal-driven nudge
#
# Demonstrates how to use `nudge` with `Signals` and `Signal` to
# detect patterns in recent transcript text and warn the agent.
# Signal scoring sums weights from matching regex patterns; the
# nudge fires when the cumulative score meets the threshold.

import re

from captain_hook import (
    HookApp,
    Signal,
    Signals,
    Transcript,
    dispatch,
    mock_tool_event,
    nudge,
)
from captain_hook.app import _current_app
from captain_hook.types import Event

app = HookApp()
token = _current_app.set(app)

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

_current_app.reset(token)

transcript = Transcript.from_messages([
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Let me try again — I'll retry with a tweaked config."}]}},
])

evt = mock_tool_event(
    "Edit", event=Event.PostToolUse, file="test.py", content="pass", transcript=transcript,
)
result = dispatch(app, Event.PostToolUse, evt)
assert result is not None
output = result["hookSpecificOutput"]
assert "additionalContext" in output

clean_transcript = Transcript.from_messages([
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "The fix looks correct."}]}},
])
evt_clean = mock_tool_event(
    "Edit", event=Event.PostToolUse, file="test.py", content="pass", transcript=clean_transcript,
)
result_clean = dispatch(app, Event.PostToolUse, evt_clean)
assert result_clean is None

print("✓ Signal-driven nudge fired on retry language, silent on clean text")
