from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from cc_transcript.models import AssistantEvent, ThinkingBlock, ToolUseBlock, UserEvent
from cc_transcript.tools import TaskCreateCall, TaskUpdateCall, parse_tool_call

from captain_hook.signals.nlp import NlpSignal
from captain_hook.snapshots.client import EvidenceIncomplete, RemoteSession
from captain_hook.types import Event, Signal, Signals

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent

PROSE_TOOLS: dict[str, Callable[[Mapping[str, Any]], list[str]]] = {
    "ReportFindings": lambda inp: [
        " ".join(filter(None, (f.get("summary"), f.get("failure_scenario")))) for f in inp.get("findings", ())
    ],
    "TodoWrite": lambda inp: [
        " ".join(filter(None, (t.get("content"), t.get("subject")))) for t in inp.get("todos", ())
    ],
}

TSignalPattern = Signal | NlpSignal
SignalMatches = list[tuple[str, set[int]]]


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


def matching_texts(sig: Signals, texts: list[str], *, consumed: Collection[str] = ()) -> SignalMatches:
    from captain_hook.state import text_hash

    if sig.vetoes and any(matching_signals(sig.vetoes, text) for text in texts):
        return []
    return [
        (text, set(matched))
        for text in texts
        if text_hash(text) not in consumed and (matched := matching_signals(sig.patterns, text))
    ]


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
            case ToolUseBlock(name=name, input=payload):
                match parse_tool_call(name, payload, on_error="other"):
                    case (
                        TaskCreateCall(subject=subject, description=description)
                        | TaskUpdateCall(subject=subject, description=description)
                    ):
                        yield " ".join(filter(None, (subject, description)))
                    case _ if extract := PROSE_TOOLS.get(name):
                        yield from extract(payload)
                    case _:
                        pass
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

    A fixed ``window`` counts scored prose entries, not raw JSONL events: tool calls
    and their results carry no prose, so they never crowd a message out of the window,
    and ``window=6`` means the last six texts a signal could match. The scan walks
    backwards and stops once ``window`` entries are in hand. Use ``window="turn"`` when
    the whole current turn is the unit regardless of how much prose it holds.

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

    key = (window, origin)
    if (prepared := evt.ctx.prepared_evidence) is not None:
        for saved_key, texts in prepared.signal_texts:
            if saved_key == key:
                return list(texts)
        raise EvidenceIncomplete("stale_handle", "signal evidence was not prepared before model execution")
    if key in evt.ctx.signal_evidence:
        return list(evt.ctx.signal_evidence[key])
    if isinstance(evt.ctx.t, RemoteSession):
        texts = evt.ctx.t.signal_texts(window=window, origin=origin)
        if origin == "any" and evt.event == Event.UserPromptSubmit and evt.user_prompt:
            texts = [evt.user_prompt, *texts]
        evt.ctx.signal_evidence[key] = tuple(texts)
        return texts

    def eligible(event: object) -> bool:
        return (
            isinstance(event, UserEvent | AssistantEvent)
            and not (event.meta.is_meta or event.meta.is_compact_summary)
            and not (isinstance(event, UserEvent) and event.is_agent_injected)
            and (origin == "any" or isinstance(event, AssistantEvent))
        )

    def texts_of(event: UserEvent | AssistantEvent) -> list[str]:
        return [text for text in (event.text, *block_texts(event)) if text]

    if window == "turn":
        texts = [text for event in evt.ctx.turn.events if eligible(event) for text in texts_of(event)]
    else:
        texts = []
        for event in reversed(evt.ctx.t.events):
            if len(texts) >= window:
                break
            if eligible(event):
                texts = texts_of(event) + texts
        texts = texts[-window:] if window else []
    if origin == "any" and evt.event == Event.UserPromptSubmit and evt.user_prompt:
        texts = [evt.user_prompt, *texts]
    evt.ctx.signal_evidence[key] = tuple(texts)
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
