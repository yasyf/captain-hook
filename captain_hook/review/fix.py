"""The FIX-mode detector: mine Claude's own hook-misfire complaints into fix candidates.

The first detector over ASSISTANT turns. Three deterministic gates, all required:
a dismissal **marker** near hook vocabulary (strong dismissals score
:data:`~cc_transcript.domains.mining.confidence.VERY_HIGH`, hedged ones
:data:`~cc_transcript.domains.mining.confidence.MEDIUM`), a **de-noise** drop of
pure compliance, and **proximity** — a hook-fire fingerprint within
:data:`PROXIMITY_TURNS` preceding conversational turns. The fingerprint shapes are
enumerated from the real captured transcripts in ``tests/fixtures/hook_fires/``
(the harness's rendering of hook output), never derived from captain-hook source:

- ``attachment:hook_additional_context`` — a nudge's message, verbatim, in ``content``.
- ``attachment:hook_blocking_error`` — a Stop block's message under ``blockingError``.
- a synthetic ``isMeta`` user turn starting ``"Stop hook feedback:\\n"``.
- a synthetic ``is_error`` tool_result whose content is a PreToolUse deny's
  ``permissionDecisionReason`` verbatim (a deny leaves NO attachment — it is
  joinable only via the fire-log).

A surviving complaint attributes through :meth:`captain_hook.fire_log.FireLog.attribute`
(drop-on-miss and drop-on-ambiguity built in) and resolves its PR target
primitive-aware: a ``nudge()``/``gate()`` fire records the primitive file as its
``source_file``, so the real user hook comes from ``hook_name``'s module prefix.
Unattributable or unresolvable complaints are dropped — precision over recall.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cc_transcript.domains.mining.confidence import MEDIUM, VERY_HIGH, CandidateSignal
from cc_transcript.domains.mining.signals import CONFIDENCE_STEP, MiningSignal, adjust
from cc_transcript.domains.mining.sourcekind import SourceKind
from cc_transcript.models import AssistantEvent, OtherEvent, ToolResultBlock, UserEvent

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from typing import Any

    from cc_transcript.models import TranscriptEvent

    from captain_hook.fire_log import FireLog, FireRow

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


def fire_message(event: TranscriptEvent) -> str | None:
    match event:
        case OtherEvent(type="attachment", raw=raw):
            attachment: Mapping[str, Any] = raw["attachment"]
            match attachment.get("type"):
                case "hook_additional_context":
                    return "\n".join(str(part) for part in attachment["content"])
                case "hook_blocking_error":
                    return str(attachment["blockingError"]["blockingError"])
                case _:
                    return None
        case UserEvent(meta=meta, text=text) if meta.is_meta and text.startswith(STOP_FEEDBACK_PREFIX):
            return text.removeprefix(STOP_FEEDBACK_PREFIX)
        case UserEvent(blocks=blocks):
            return next((b.content for b in blocks if isinstance(b, ToolResultBlock) and b.is_error), None)
        case _:
            return None


def preceding_fires(events: Sequence[TranscriptEvent], index: int) -> list[tuple[int, int, str]]:
    fires: list[tuple[int, int, str]] = []
    turns = 0
    for i in range(index - 1, -1, -1):
        if (message := fire_message(events[i])) is not None:
            fires.append((i, turns, message))
        if isinstance(events[i], UserEvent | AssistantEvent):
            turns += 1
            if turns >= PROXIMITY_TURNS:
                break
    return fires


def resolve_target(fire: FireRow) -> tuple[str, str] | None:
    if PRIMITIVES_DIR not in fire.source_file:
        return fire.source_file, fire.hook_name
    module, sep, _ = fire.hook_name.partition(":")
    if not sep or not module:
        return None
    return f"{HOOKS_DIR}/{module.rsplit('.', 1)[-1]}.py", fire.hook_name


def complaint_signal(marker: Marker, turns_back: int) -> CandidateSignal:
    base = CandidateSignal(
        VERY_HIGH if marker.strength == "strong" else MEDIUM,
        (f"{marker.strength}_marker", marker.misfire_class),
    )
    return adjust(base, CONFIDENCE_STEP, "tight_proximity") if turns_back <= TIGHT_PROXIMITY_TURNS else base


def iter_hook_complaint_signals(
    events: Sequence[TranscriptEvent], *, firelog: FireLog, session_key: str
) -> Iterator[MiningSignal]:
    """Yields one :class:`~cc_transcript.domains.mining.MiningSignal` per attributed misfire complaint.

    Args:
        events: The transcript's full ordered event stream.
        firelog: The fire log joining fingerprint messages to the firing hook.
        session_key: The transcript-path hash the session's fires were recorded under.

    Returns:
        Signals of kind :data:`HOOK_COMPLAINT` whose ``evidence`` stashes the
        attribution (``hook_name``, ``source_file``, ``event``, ``action``,
        ``fire_ts``, ``fire_message``, ``marker``) plus the resolved
        ``target_source_file``/``target_hook_name``/``misfire_class``.
    """
    for index, event in enumerate(events):
        if not isinstance(event, AssistantEvent) or event.meta.is_sidechain or not event.text.strip():
            continue
        if (marker := classify_marker(event.text)) is None:
            continue
        if not (fires := preceding_fires(events, index)):
            continue
        near_ts = event.meta.timestamp.timestamp()
        attributed = [
            (i, turns_back, row)
            for i, turns_back, message in fires
            if (row := firelog.attribute(session_key, message=message, near_ts=near_ts)) is not None
        ]
        if not attributed or len({(row.source_file, row.hook_name) for _, _, row in attributed}) > 1:
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
                "hook_name": fire.hook_name,
                "source_file": fire.source_file,
                "event": fire.event,
                "action": fire.action,
                "fire_ts": fire.ts,
                "fire_message": fire.message,
                "marker": marker.matched,
                "misfire_class": marker.misfire_class,
                "target_source_file": target_source_file,
                "target_hook_name": target_hook_name,
            },
            signal=complaint_signal(marker, turns_back),
        )
