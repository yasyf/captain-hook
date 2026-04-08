from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from captain_hook.signals.nlp import NlpSignal
from captain_hook.types import Event, Signal, Signals

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent

TSignalPattern = Signal | NlpSignal


def score_signals(patterns: Sequence[TSignalPattern], text: str) -> int:
    """Sum the weights of all signal patterns that match the given text."""
    from captain_hook.signals.nlp import nlp_scan

    total = 0
    for s in patterns:
        match s:
            case NlpSignal(clauses=clauses) if nlp_scan(clauses, text):
                total += s.weight
            case Signal() if re.search(s.pattern, text, s.flags):
                total += s.weight
            case _:
                pass
    return total


def extract_signal_context(patterns: Sequence[TSignalPattern], text: str) -> list[str]:
    """Extract matching lines (for regex) or sentences (for NLP) from text."""
    from captain_hook.signals.nlp import nlp_scan

    result: list[str] = []
    for s in patterns:
        match s:
            case NlpSignal(clauses=clauses):
                result.extend(nlp_scan(clauses, text))
            case Signal():
                result.extend(line for line in text.splitlines() if re.search(s.pattern, line, s.flags))
    return result


def transcript_texts(evt: BaseHookEvent, window: int) -> list[str]:
    """Extract text from recent transcript messages for signal scoring.

    For ``UserPromptSubmit`` events, returns just the user prompt.
    Otherwise returns ``.text`` from the last ``window`` messages.
    """
    return (
        [evt.user_prompt]
        if evt.event == Event.UserPromptSubmit and evt.user_prompt
        else [msg.text for msg in evt.ctx.t.recent(window).messages if msg.text]
    )


def cite_message(sig: Signals, triggering: list[str], message: str) -> str:
    """Append trigger context to a message when signal matches are found."""
    return (
        f"{message}\n\nTriggered by: {'; '.join(context)}"
        if (context := extract_signal_context(sig.patterns, "\n".join(triggering)))
        else message
    )


def resolve_signals(signals: Sequence[Signal | NlpSignal] | Signals | None) -> Signals | None:
    """Normalize signals input into a ``Signals`` bundle, or None.

    A bare ``list[Signal]`` is wrapped with ``threshold=1`` (any single match triggers).
    """
    if signals is None:
        return None
    if isinstance(signals, Signals):
        return signals
    return Signals(patterns=list(signals), threshold=1)


__all__ = [
    "Signal",
    "Signals",
    "cite_message",
    "extract_signal_context",
    "resolve_signals",
    "score_signals",
    "transcript_texts",
]
