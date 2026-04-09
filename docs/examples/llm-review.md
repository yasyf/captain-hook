# LLM Review

*Use an LLM reviewer to catch rationalization and excuse-making.*

## The problem

Claude rationalizes bugs instead of investigating them. It sees a count of zero and writes "the count is zero because the API doesn't return that field" instead of checking whether the API call failed. It explains away empty results as "expected behavior" without looking at the actual data. These rationalizations slip past pattern matching because the language varies every time.

## The solution

### LLM gate with signal triggers

An `llm_gate` combines signal-based triggering with LLM judgment. Signals detect when rationalization language appears in the transcript, then an LLM evaluates whether the agent is genuinely making excuses.

```python
import re
from captain_hook import llm_gate, Signal, Signals

llm_gate(
    "The agent may be rationalizing a bug instead of investigating it. "
    "Look for patterns where the agent explains away unexpected values "
    "(zeros, empty lists, None) without checking the actual data source. "
    "Return block=True if the agent is making excuses rather than debugging.",
    message=lambda r: f"Rationalization detected: {r.reasoning}",
    signals=Signals(
        patterns=[
            Signal(pattern=r"expected.*because", weight=2, flags=re.IGNORECASE),
            Signal(pattern=r"the (count|value|result) is (zero|0|empty|none)", weight=3, flags=re.IGNORECASE),
            Signal(pattern=r"doesn't return", weight=2, flags=re.IGNORECASE),
            Signal(pattern=r"not (available|supported|implemented)", weight=1, flags=re.IGNORECASE),
        ],
        threshold=4,
        window=10,
    ),
    max_fires=1,
)
```

How this works step by step:

1. **Signals score the transcript.** As the agent writes, each message is scanned against the signal patterns. Weights accumulate over the sliding window.
2. **Threshold triggers evaluation.** When the score reaches 4, the hook fires and calls the LLM.
3. **LLM decides.** The LLM receives the prompt and the matching transcript context. It returns a `GateVerdict` with `block: bool` and `reasoning: str`.
4. **Block or pass.** If `block=True`, the agent is stopped with the reasoning as the message.

### The GateVerdict model

The LLM responds with a structured `GateVerdict`:

```python
from pydantic import BaseModel

class GateVerdict(BaseModel):
    block: bool
    reasoning: str
```

The `message` parameter can be a static string or a callable that receives the verdict. Using a callable lets you include the LLM's reasoning in the block message:

```python
message=lambda r: f"Rationalization detected: {r.reasoning}"
```

### Softer version: LLM nudge

If you want to warn instead of block, use `llm_nudge`. It has the same API but adds advisory context instead of stopping the agent:

```python
import re
from captain_hook import llm_nudge, Signal, Signals

llm_nudge(
    "The agent may be speculating about data values instead of "
    "observing them. Check if the agent is making claims about "
    "what data 'should' contain without inspecting actual traces "
    "or database records. Return fire=True if the agent is guessing.",
    message="Observe, don't infer. Check the actual data before drawing conclusions.",
    signals=Signals(
        patterns=[
            Signal(pattern=r"should (contain|have|be|return)", weight=2, flags=re.IGNORECASE),
            Signal(pattern=r"probably", weight=1, flags=re.IGNORECASE),
            Signal(pattern=r"i (think|believe|assume)", weight=2, flags=re.IGNORECASE),
        ],
        threshold=3,
        window=8,
    ),
    max_fires=3,
)
```

The `NudgeVerdict` model uses `fire: bool` instead of `block: bool`:

```python
class NudgeVerdict(BaseModel):
    fire: bool
    reasoning: str
```

!!! tip
    `llm_nudge` defaults to `max_fires=3` and fires on `PostToolUse`, making it a recurring advisory. `llm_gate` defaults to `max_fires=1` and fires on `Stop | SubagentStop`, making it a one-shot blocker at task boundaries.

### Building custom prompts

For full control over the LLM prompt, use the `Prompt` builder:

```python
from captain_hook import Prompt

prompt = (
    Prompt()
    .system(
        "You are a code review assistant. Your job is to detect "
        "when an AI agent is rationalizing bugs instead of investigating them."
    )
    .context("rules", """
        A rationalization is when the agent explains away an unexpected value
        (zero, empty, None) without checking the data source. Examples:
        - "The count is zero because the API doesn't return that field"
        - "This is empty because the feature isn't implemented yet"
        - "None is expected here because the record was deleted"
    """)
    .ask("Is the agent rationalizing in the transcript context provided?")
)
```

The `Prompt` builder produces structured text with XML-wrapped context sections. Pass the rendered string to `llm_gate` or `llm_nudge` as the first argument.

### Layered defense: nudge then gate

Combine `llm_nudge` for early warnings with `llm_gate` for hard stops:

```python
import re
from captain_hook import llm_nudge, llm_gate, Signal, Signals

RATIONALIZATION_SIGNALS = [
    Signal(pattern=r"expected.*because", weight=2, flags=re.IGNORECASE),
    Signal(pattern=r"the (count|value|result) is (zero|0|empty|none)", weight=3, flags=re.IGNORECASE),
    Signal(pattern=r"doesn't return", weight=2, flags=re.IGNORECASE),
]

# Advisory warning at lower threshold
llm_nudge(
    "Is the agent explaining away unexpected values without checking data?",
    message="Check the actual data before rationalizing unexpected values.",
    signals=Signals(patterns=RATIONALIZATION_SIGNALS, threshold=3, window=8),
)

# Hard block at higher threshold
llm_gate(
    "Is the agent persistently rationalizing bugs instead of investigating?",
    message=lambda r: f"BLOCKED: {r.reasoning}",
    signals=Signals(patterns=RATIONALIZATION_SIGNALS, threshold=5, window=10),
    max_fires=1,
)
```

!!! note
    The LLM call only happens after signal scoring passes the threshold. This means the LLM is not invoked on every tool use -- only when the transcript shows suspicious patterns. This keeps latency and cost low.
