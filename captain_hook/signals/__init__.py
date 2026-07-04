from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from cc_transcript.models import AssistantEvent, ThinkingBlock, ToolUseBlock, UserEvent

from captain_hook.signals.nlp import NlpSignal
from captain_hook.types import Event, Signal, Signals

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent

PROSE_TOOLS: dict[str, Callable[[Mapping[str, Any]], list[str]]] = {
    "ReportFindings": lambda inp: [
        " ".join(filter(None, (f.get("summary"), f.get("failure_scenario")))) for f in inp.get("findings", ())
    ],
    "TaskCreate": lambda inp: [" ".join(filter(None, (inp.get("subject"), inp.get("description"))))],
    "TaskUpdate": lambda inp: [" ".join(filter(None, (inp.get("subject"), inp.get("description"))))],
    "TodoWrite": lambda inp: [
        " ".join(filter(None, (t.get("content"), t.get("subject")))) for t in inp.get("todos", ())
    ],
}

TSignalPattern = Signal | NlpSignal


def score_signals(patterns: Sequence[TSignalPattern], text: str) -> int:
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
    from captain_hook.signals.nlp import nlp_scan

    result: list[str] = []
    for s in patterns:
        match s:
            case NlpSignal(clauses=clauses):
                result.extend(nlp_scan(clauses, text))
            case Signal():
                result.extend(line for line in text.splitlines() if re.search(s.pattern, line, s.flags))
    return result


def block_texts(event: UserEvent | AssistantEvent) -> Iterator[str]:
    for block in event.blocks:
        match block:
            case ThinkingBlock(thinking=thinking):
                yield thinking
            case ToolUseBlock(name=name, input=payload) if extract := PROSE_TOOLS.get(name):
                yield from extract(payload)
            case _:
                pass


def transcript_texts(evt: BaseHookEvent, window: int | Literal["turn"]) -> list[str]:
    """Extract prose from recent transcript events for signal scoring.

    For ``UserPromptSubmit`` events, returns just the user prompt. Otherwise scans
    the last ``window`` events — the whole current turn when ``window`` is
    ``"turn"`` — and returns one entry per prose source: each event's ``.text``,
    each thinking block, and the prose fields of prose-carrying tool calls
    (``ReportFindings`` findings, ``TaskCreate``/``TaskUpdate`` subjects and
    descriptions, ``TodoWrite`` todos).
    """
    if evt.event == Event.UserPromptSubmit and evt.user_prompt:
        return [evt.user_prompt]
    scope = evt.ctx.turn if window == "turn" else evt.ctx.t.recent(window)
    return [
        text
        for event in scope.events
        if isinstance(event, UserEvent | AssistantEvent)
        for text in (event.text, *block_texts(event))
        if text
    ]


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
