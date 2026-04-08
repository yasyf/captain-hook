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
    """Sum the weights of all signal patterns that match the given text.

    Evaluates ``Signal`` patterns via ``re.search`` and ``NlpSignal`` patterns
    via ``nlp_scan``. Negative weights reduce the total.

    Args:
        patterns: Signal and/or NlpSignal instances to evaluate.
        text: Text to match against.

    Returns:
        Cumulative score (can be negative).
    """
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
    """Extract matching lines (for regex) or sentences (for NLP) from text.

    Args:
        patterns: Signal and/or NlpSignal patterns.
        text: Source text to extract context from.

    Returns:
        List of matching lines or sentences.
    """
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

    Args:
        evt: The current hook event.
        window: Number of recent messages to include.

    Returns:
        List of non-empty text strings.
    """
    return (
        [evt.user_prompt]
        if evt.event == Event.UserPromptSubmit and evt.user_prompt
        else [msg.text for msg in evt.ctx.t.recent(window).messages if msg.text]
    )


def cite_message(sig: Signals, triggering: list[str], message: str) -> str:
    """Append trigger context to a message when signal matches are found.

    Args:
        sig: The Signals bundle whose patterns produced the match.
        triggering: Transcript texts that triggered the signal.
        message: Base message to augment.

    Returns:
        Message with ``"Triggered by: ..."`` appended, or the bare message.
    """
    return (
        f"{message}\n\nTriggered by: {'; '.join(context)}"
        if (context := extract_signal_context(sig.patterns, "\n".join(triggering)))
        else message
    )


def resolve_signals(signals: Sequence[Signal | NlpSignal] | Signals | None) -> Signals | None:
    """Normalize signals input into a ``Signals`` bundle, or None.

    Accepts a ``Signals`` instance, a bare sequence of patterns (wrapped with
    threshold=1), or None.

    Args:
        signals: Raw signals input from a primitive registration.

    Returns:
        Normalized Signals bundle, or None.
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
