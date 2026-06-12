"""The FIX-mode detector: mine Claude's own hook-misfire complaints into fix candidates.

The first detector over ASSISTANT turns. Three deterministic gates, all required:
a dismissal **marker** near hook vocabulary (strong dismissals score
:data:`~cc_transcript.mining.VERY_HIGH`, hedged ones
:data:`~cc_transcript.mining.MEDIUM`), a **de-noise** drop of
pure compliance, and **proximity** — a hook-fire fingerprint within
:data:`PROXIMITY_TURNS` preceding conversational turns. The fingerprint shapes are
enumerated from the real captured transcripts in ``tests/fixtures/hook_fires/``
(the harness's rendering of hook output), never derived from captain-hook source:

- ``attachment:hook_additional_context`` — a nudge's message, verbatim, in ``content``,
  with the firing tool call's ``toolUseID``.
- ``attachment:hook_blocking_error`` — a Stop block's message under ``blockingError``.
- a synthetic ``isMeta`` user turn starting ``"Stop hook feedback:\\n"``.
- a synthetic ``is_error`` tool_result whose content is a PreToolUse deny's
  ``permissionDecisionReason`` verbatim (a deny leaves NO attachment — it is
  joinable only via the decision ledger).

A surviving complaint attributes through the
:class:`~cc_transcript.decisions.DecisionLog`: tool-shaped fingerprints join by
the tool call's content digest via
:meth:`~cc_transcript.decisions.DecisionLog.attribute_tool`, and digestless
shapes (Stop feedback, blocking errors) fall back to
:meth:`~cc_transcript.decisions.DecisionLog.attribute_nearest` with the
decision's recorded message as the tiebreak — the only place message-substring
matching survives. The PR target resolves primitive-aware: a ``nudge()``/``gate()``
fire records the primitive file as its ``source_file``, so the real user hook
comes from the decision ``kind``'s module prefix. Unattributable or unresolvable
complaints are dropped — precision over recall.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cc_transcript.filterspec import tool_uses
from cc_transcript.mining.confidence import MEDIUM, VERY_HIGH, CandidateSignal
from cc_transcript.mining.signals import CONFIDENCE_STEP, MiningSignal, adjust
from cc_transcript.mining.sourcekind import SourceKind
from cc_transcript.models import AssistantEvent, OtherEvent, ToolResultBlock, UserEvent

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from typing import Any

    from cc_transcript.decisions import Decision, DecisionLog
    from cc_transcript.ids import SessionId, ToolUseId
    from cc_transcript.models import ToolUseBlock, TranscriptEvent

HOOK_COMPLAINT = SourceKind("hook_complaint")
"""The source kind for an assistant turn dismissing a hook fire as a misfire."""

PROXIMITY_TURNS = 3
TIGHT_PROXIMITY_TURNS = 1
STOP_FEEDBACK_PREFIX = "Stop hook feedback:\n"
PRIMITIVES_DIR = "captain_hook/primitives/"
HOOKS_DIR = ".claude/hooks"

HOOK_VOCAB_RE = re.compile(r"\b(?:hook|reminder|nudge|gate|guard)s?\b", re.IGNORECASE)

STRONG_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (misfire_class, re.compile(pattern, re.IGNORECASE))
    for misfire_class, pattern in (
        ("refire", r"\bre-?fired?\b"),
        ("misfire", r"\bmisfir(?:e[ds]?|ing)\b"),
        ("false_positive", r"\bfalse[ -]positives?\b"),
        ("ignored_repeat", r"\bignoring (?:it|the repeats?)\b"),
        ("already_addressed", r"\balready (?:fixed|resolved|addressed)\b"),
        ("should_not_have_fired", r"\bshouldn'?t have fired\b"),
        ("spurious", r"\bspurious\b"),
        ("unnecessary", r"\bunnecessar(?:y|ily)\b"),
    )
)

HEDGED_MARKER_RE = re.compile(
    r"(?:\bi think\b|\bseems?\b|\bmay\b|\bmight\b|\blooks like\b)"
    r"[\s\S]{0,80}?(?:false[ -]positive|misfir|re-?fir|shouldn'?t have fired|spurious|unnecessar|wrong(?:ly)?)",
    re.IGNORECASE,
)

COMPLIANCE_RE = re.compile(
    r"(?:\bI'?ll\b|\blet me\b|\bgoing to\b|\bgood (?:point|catch)\b|\bnoted\b)"
    r"[\s\S]{0,40}?(?:hook|reminder|gate|nudge)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Marker:
    """One dismissal marker matched in an assistant turn.

    Attributes:
        strength: Whether the dismissal is outright or hedged.
        misfire_class: The misfire taxonomy label the marker implies.
        matched: The matched marker text, verbatim.
    """

    strength: Literal["strong", "hedged"]
    misfire_class: str
    matched: str


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """One hook-fire trace the harness rendered into the transcript.

    Attributes:
        message: The hook's fire message, verbatim.
        tool_use_id: The firing tool call's id, when the trace carries one —
            the handle to the tool digest the ledger joins on.
        event: The hook event name for digestless traces, e.g. ``Stop``.
    """

    message: str
    tool_use_id: ToolUseId | None = None
    event: str | None = None


def classify_marker(text: str) -> Marker | None:
    if not HOOK_VOCAB_RE.search(text):
        return None
    hedged = HEDGED_MARKER_RE.search(text)
    strong = next(((cls, m) for cls, rx in STRONG_MARKERS if (m := rx.search(text))), None)
    if strong is not None and hedged is None:
        return Marker("strong", strong[0], strong[1].group(0))
    if COMPLIANCE_RE.search(text):
        return None
    if hedged is not None:
        return Marker("hedged", strong[0] if strong is not None else "suspected", hedged.group(0))
    return None


def fingerprint_of(event: TranscriptEvent) -> Fingerprint | None:
    match event:
        case OtherEvent(type="attachment", raw=raw):
            attachment: Mapping[str, Any] = raw["attachment"]
            match attachment.get("type"):
                case "hook_additional_context":
                    return Fingerprint(
                        message="\n".join(str(part) for part in attachment["content"]),
                        tool_use_id=attachment.get("toolUseID"),
                        event=attachment.get("hookEvent"),
                    )
                case "hook_blocking_error":
                    return Fingerprint(
                        message=str(attachment["blockingError"]["blockingError"]),
                        event=str(attachment.get("hookEvent") or "Stop"),
                    )
                case _:
                    return None
        case UserEvent(meta=meta, text=text) if meta.is_meta and text.startswith(STOP_FEEDBACK_PREFIX):
            return Fingerprint(message=text.removeprefix(STOP_FEEDBACK_PREFIX), event="Stop")
        case UserEvent(blocks=blocks):
            return next(
                (
                    Fingerprint(message=b.content, tool_use_id=b.tool_use_id)
                    for b in blocks
                    if isinstance(b, ToolResultBlock) and b.is_error
                ),
                None,
            )
        case _:
            return None


def preceding_fingerprints(events: Sequence[TranscriptEvent], index: int) -> list[tuple[int, int, Fingerprint]]:
    fingerprints: list[tuple[int, int, Fingerprint]] = []
    turns = 0
    for i in range(index - 1, -1, -1):
        if (fingerprint := fingerprint_of(events[i])) is not None:
            fingerprints.append((i, turns, fingerprint))
        if isinstance(events[i], UserEvent | AssistantEvent):
            turns += 1
            if turns >= PROXIMITY_TURNS:
                break
    return fingerprints


def attribute_fingerprint(
    decisions: DecisionLog,
    uses: Mapping[ToolUseId, ToolUseBlock],
    session_id: SessionId,
    near_ts_ms: int,
    fingerprint: Fingerprint,
) -> Decision | None:
    if fingerprint.tool_use_id is not None and (block := uses.get(fingerprint.tool_use_id)) is not None:
        found = decisions.attribute_tool(session_id, tool_digest=block.call.digest, near_ts_ms=near_ts_ms)
        return found if found is not None and found.source_file else None
    if fingerprint.event is None:
        return None
    found = decisions.attribute_nearest(session_id, event=fingerprint.event, near_ts_ms=near_ts_ms)
    if found is None or not found.source_file or not found.message or found.message not in fingerprint.message:
        return None
    return found


def resolve_target(decision: Decision) -> tuple[str, str] | None:
    if PRIMITIVES_DIR not in decision.source_file:
        return decision.source_file, decision.kind
    module, sep, _ = decision.kind.partition(":")
    if not sep or not module:
        return None
    return f"{HOOKS_DIR}/{module.rsplit('.', 1)[-1]}.py", decision.kind


def complaint_signal(marker: Marker, turns_back: int) -> CandidateSignal:
    base = CandidateSignal(
        VERY_HIGH if marker.strength == "strong" else MEDIUM,
        (f"{marker.strength}_marker", marker.misfire_class),
    )
    return adjust(base, CONFIDENCE_STEP, "tight_proximity") if turns_back <= TIGHT_PROXIMITY_TURNS else base


def iter_hook_complaint_signals(events: Sequence[TranscriptEvent], *, decisions: DecisionLog) -> Iterator[MiningSignal]:
    """Yields one :class:`~cc_transcript.mining.MiningSignal` per attributed misfire complaint.

    Fires are joined by the events' own session UUID — the only session key —
    plus the tool call's content digest when the fingerprint carries a tool-use
    id, or by event name and timestamp proximity when it does not.

    Args:
        events: The transcript's full ordered event stream.
        decisions: The decision ledger joining fingerprint traces to the firing hook.

    Returns:
        Signals of kind :data:`HOOK_COMPLAINT` whose ``evidence`` stashes the
        attribution (``hook_name``, ``source_file``, ``event``, ``action``,
        ``fire_ts_ms``, ``fire_message``, ``marker``) plus the resolved
        ``target_source_file``/``target_hook_name``/``misfire_class``.
    """
    uses = tool_uses(events)
    for index, event in enumerate(events):
        if not isinstance(event, AssistantEvent) or event.meta.is_sidechain or not event.text.strip():
            continue
        if (marker := classify_marker(event.text)) is None:
            continue
        if not (fingerprints := preceding_fingerprints(events, index)):
            continue
        near_ts_ms = int(event.meta.timestamp.timestamp() * 1000)
        attributed = [
            (i, turns_back, found)
            for i, turns_back, fingerprint in fingerprints
            if (found := attribute_fingerprint(decisions, uses, event.meta.session_id, near_ts_ms, fingerprint))
            is not None
        ]
        if not attributed or len({(found.source_file, found.kind) for _, _, found in attributed}) > 1:
            continue
        trigger_index, turns_back, fire = attributed[0]
        if (target := resolve_target(fire)) is None:
            continue
        target_source_file, target_hook_name = target
        yield MiningSignal(
            kind=HOOK_COMPLAINT,
            detector="hook_complaint",
            session_id=event.meta.session_id,
            event_index=index,
            event_uuid=event.meta.uuid,
            occurred_at=event.meta.timestamp,
            text=event.text,
            cc_version=event.meta.cc_version,
            trigger_index=trigger_index,
            evidence={
                "hook_name": fire.kind,
                "source_file": fire.source_file,
                "event": fire.event,
                "action": fire.action,
                "fire_ts_ms": fire.ts_ms,
                "fire_message": fire.message,
                "marker": marker.matched,
                "misfire_class": marker.misfire_class,
                "target_source_file": target_source_file,
                "target_hook_name": target_hook_name,
            },
            signal=complaint_signal(marker, turns_back),
        )
