# Example: LLM-gated review
#
# Demonstrates how to use `llm_gate` to register a hook that asks an
# LLM whether to block.  In real usage the LLM backend produces a
# `GateVerdict`; here we just register the hook — the LLM call
# happens at dispatch time via the event context.

import re

from captain_hook import Signal, Signals, llm_gate

# Block the agent when it makes excuses for external failures
# instead of investigating root causes.
llm_gate(
    "Is the agent making excuses for external service failures "
    "rather than investigating the root cause?",
    message=lambda r: f"Excuse detected: {r.reasoning}",
    signals=Signals(
        patterns=[
            Signal(pattern=r"external.*service", weight=2),
            Signal(pattern=r"out of our control", weight=3, flags=re.IGNORECASE),
        ],
        threshold=2,
    ),
    max_fires=1,
)
