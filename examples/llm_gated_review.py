# Example: LLM-gated review
#
# Demonstrates how to use `llm_gate` to register a hook that asks an
# LLM whether to block.  In real usage the LLM backend produces a
# `GateVerdict`; here we mock the LLM call to keep the example
# self-contained and runnable without API keys.

from unittest.mock import patch

from captain_hook import (
    GateVerdict,
    HookApp,
    Signal,
    Signals,
    Transcript,
    dispatch,
    llm_gate,
    mock_stop_event,
)
from captain_hook.app import _current_app
from captain_hook.types import Event

app = HookApp()
token = _current_app.set(app)

llm_gate(
    "Is the agent making excuses for external service failures "
    "rather than investigating the root cause?",
    message=lambda r: f"Excuse detected: {r.reasoning}",
    signals=Signals(
        patterns=[
            Signal(pattern=r"external.*service", weight=2),
            Signal(pattern=r"out of our control", weight=3),
        ],
        threshold=2,
    ),
    max_fires=1,
)

_current_app.reset(token)

excuse_transcript = Transcript.from_messages([
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "This failure is caused by an external service outage. It is out of our control."}]}},
])

mock_verdict = GateVerdict(block=True, reasoning="Agent blamed external service without investigating")

evt = mock_stop_event(transcript=excuse_transcript)

with patch.object(evt.ctx, "call_llm", return_value=mock_verdict):
    result = dispatch(app, Event.Stop, evt)

assert result is not None
assert result["decision"] == "block"
assert "Excuse detected" in result["reason"]

print("✓ LLM-gated review blocked excuse-making agent")
