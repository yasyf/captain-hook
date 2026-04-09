# Agent Behavior

*Detect retry loops and escalate from nudge to blocking gate.*

## The problem

Claude hits a failing test and retries the same approach over and over -- changing a variable name, adding a print statement, running the test again -- without ever stopping to isolate the minimal failing case. After 5 attempts the code is worse than when it started.

## The solution

### Simple nudge with conditions

Start with a one-time reminder that fires when Python files are edited:

```python
from captain_hook import nudge, TouchedFile

nudge(
    "Remember to run tests after editing Python files.",
    only_if=[TouchedFile("**/*.py")],
)
```

This fires once (the default `max_fires=1` for condition-based nudges) as a `PreToolUse` advisory. It is a lightweight reminder, not a detection system.

### Signal-driven nudge

To detect the retry loop itself, use signals. Signals are regex patterns that score against recent transcript text. When the cumulative score meets the threshold, the nudge fires.

```python
import re
from captain_hook import nudge, Signal, Signals

nudge(
    "Stop retrying blindly. Narrow the failing test case first, "
    "then fix the isolated failure.",
    signals=Signals(
        patterns=[
            Signal(pattern=r"let me try again", weight=2, flags=re.IGNORECASE),
            Signal(pattern=r"retry", weight=1, flags=re.IGNORECASE),
            Signal(pattern=r"one more attempt", weight=2, flags=re.IGNORECASE),
            Signal(pattern=r"same (error|failure|issue)", weight=2, flags=re.IGNORECASE),
        ],
        threshold=4,
        window=10,
    ),
)
```

How signal scoring works:

- **window**: the number of recent transcript messages to scan (here, the last 10).
- **weight**: each regex match in a message adds its weight to the score.
- **threshold**: the nudge fires when the cumulative score reaches this value.

So if the agent says "let me try again" twice in 10 messages, the score is 2 + 2 = 4, meeting the threshold.

!!! tip
    Signal-driven nudges default to `max_fires=3` and fire on `PostToolUse`. After 3 fires, the nudge stops -- preventing the hook itself from becoming noise.

### Escalate to a blocking gate

If the nudge is not enough, escalate. A gate blocks the agent from continuing until it acknowledges the problem. Use `gate` (shorthand for `nudge(..., block=True)`) or pass `block=True` directly:

```python
import re
from captain_hook import gate, Signal, Signals

gate(
    "STOP. You have retried the same approach multiple times. "
    "Do not proceed until you:\n"
    "1. Isolate the minimal failing test case\n"
    "2. Read the actual error trace\n"
    "3. Form a new hypothesis",
    signals=Signals(
        patterns=[
            Signal(pattern=r"let me try again", weight=2, flags=re.IGNORECASE),
            Signal(pattern=r"retry", weight=1, flags=re.IGNORECASE),
            Signal(pattern=r"one more attempt", weight=2, flags=re.IGNORECASE),
            Signal(pattern=r"same (error|failure|issue)", weight=2, flags=re.IGNORECASE),
        ],
        threshold=6,
        window=10,
    ),
    max_fires=1,
)
```

The gate uses a higher threshold (6) so it only fires after sustained retrying. It fires on `Stop | SubagentStop` by default, meaning the agent cannot finish its task until it changes course.

### Layered defense: nudge then gate

Combine both for progressive escalation:

```python
import re
from captain_hook import nudge, gate, Signal, Signals

RETRY_SIGNALS = [
    Signal(pattern=r"let me try again", weight=2, flags=re.IGNORECASE),
    Signal(pattern=r"retry", weight=1, flags=re.IGNORECASE),
    Signal(pattern=r"one more attempt", weight=2, flags=re.IGNORECASE),
    Signal(pattern=r"same (error|failure|issue)", weight=2, flags=re.IGNORECASE),
]

# First line: advisory nudge at score 4
nudge(
    "You appear to be retrying the same approach. "
    "Narrow the failing case before trying again.",
    signals=Signals(patterns=RETRY_SIGNALS, threshold=4, window=10),
)

# Second line: hard block at score 6
gate(
    "STOP. Repeated retries detected. Isolate the failure first.",
    signals=Signals(patterns=RETRY_SIGNALS, threshold=6, window=10),
    max_fires=1,
)
```

The nudge fires first as an early warning. If the agent ignores it and keeps retrying, the gate fires and blocks it from stopping until it changes behavior.

!!! note
    Each hook tracks its own signal consumption independently. The nudge and gate can share the same signal patterns without interfering with each other.
