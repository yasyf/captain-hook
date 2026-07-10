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


def matching_signals(patterns: Sequence[TSignalPattern], text: str) -> list[int]:
    """Indices of ``patterns`` whose signal matches ``text`` (regex search or any clause hit).

    Exposes per-signal attribution so presence-union scoring can count each distinct
    signal once across window entries without double-weighting.
    """
    from captain_hook.signals.nlp import nlp_scan

    matched: list[int] = []
    for i, s in enumerate(patterns):
        match s:
            case NlpSignal(clauses=clauses) if nlp_scan(clauses, text):
                matched.append(i)
            case Signal() if re.search(s.pattern, text, s.flags):
                matched.append(i)
            case _:
                pass
    return matched


def score_signals(patterns: Sequence[TSignalPattern], text: str) -> int:
    return sum(patterns[i].weight for i in matching_signals(patterns, text))


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


def transcript_texts(
    evt: BaseHookEvent, window: int | Literal["turn"], origin: Literal["assistant", "any"] = "any"
) -> list[str]:
    """Extract prose from recent transcript events for signal scoring.

    Scans the last ``window`` events — the whole current turn when ``window`` is
    ``"turn"`` — and returns one entry per prose source: each event's ``.text``,
    each thinking block, and the prose fields of prose-carrying tool calls
    (``ReportFindings`` findings, ``TaskCreate``/``TaskUpdate`` subjects and
    descriptions, ``TodoWrite`` todos).

    ``origin`` filters candidates by author: the default ``"any"`` keeps user and
    assistant prose alike, while ``"assistant"`` drops user messages (and, on
    ``UserPromptSubmit``, the just-submitted prompt) so a stance hook scores only the
    agent's own words. Signal-driven hooks thread ``Signals.origin`` here, which
    defaults to ``"assistant"``.

    A fixed ``window`` counts raw JSONL events, not turns, so tool-call traffic
    between a target assistant message and the triggering event can crowd that
    text out of a small window (a writeup then four ``Read`` pairs drops the
    writeup at ``window=6``); pass ``window="turn"`` for whole-prior-turn semantics.

    On ``UserPromptSubmit`` the just-submitted prompt is not yet in the transcript,
    so it is prepended as its own entry ahead of that window: a UPS-scored hook
    scores the prior assistant turn (e.g. an option dump the user is replying to)
    alongside the new prompt. Use ``window=0`` for a UPS hook that must score the
    prompt alone.

    Harness-injected events — skill loads and other meta events, and compact
    summaries — are excluded: they carry the harness's prose, not the agent's, and
    scoring them lets an unrelated skill's boilerplate trip a signal gate.

    Agent-injected user events — teammate-message relay banners, scheduled-task
    prompts, and role reminders (``UserEvent.is_agent_injected``) — are dropped even
    under ``origin="any"``: a relay banner echoes another agent's prose into this
    transcript, so scoring it would let one agent's words trip this agent's gate.
    """
    scope = evt.ctx.turn if window == "turn" else evt.ctx.t.recent(window)
    texts = [
        text
        for event in scope.events
        if isinstance(event, UserEvent | AssistantEvent)
        and not (event.meta.is_meta or event.meta.is_compact_summary)
        and not (isinstance(event, UserEvent) and event.is_agent_injected)
        and (origin == "any" or isinstance(event, AssistantEvent))
        for text in (event.text, *block_texts(event))
        if text
    ]
    if origin == "any" and evt.event == Event.UserPromptSubmit and evt.user_prompt:
        return [evt.user_prompt, *texts]
    return texts


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
