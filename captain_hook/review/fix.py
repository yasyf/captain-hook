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
matching survives. When no fingerprint lands within :data:`PROXIMITY_TURNS` — the
harness rendered no trace the ledger can join — a complaint that names a hook
(``"the X hook"``) falls back to :func:`named_hook_target`, attributing to a
ledger row whose ``kind`` stem uniquely matches the named hook within
:data:`NAMED_HOOK_WINDOW_MS`, failing closed on zero or ambiguous matches.
The PR target resolves by source location into a :class:`~captain_hook.review.routing.Target`
naming the file, the hook, and the repo the fix belongs to: a watched-repo hook file is the
target verbatim (``repo`` ``None`` — fixed in place), but an installed-wheel or pack-cache
``source_file`` carries no repo path, so the real hook comes from the decision ``kind``'s
module prefix routed through the scan's :class:`~captain_hook.review.routing.PackIndex`. A
``nudge()``/``gate()`` fire records the primitive file, and a ``hook()`` bundled in a pack
records the wheel or cache file — all of these route through the ``kind``: a
``<pack>.<module>`` prefix naming a module the installed builtin pack actually ships targets
the pack source inside captain-hook itself (``captain_hook/packs/<pack>/<module>.py``, repo
captain-hook), a prefix naming a cached external pack the project declares targets that pack's
``github.com/<owner>/<repo>`` at the file's in-repo path, a prefix naming a pack discovered on
an enabled Claude Code plugin is dropped (plugin packs route to no repo this wave), and any
other module prefix — including a packaged user hook whose package merely shares a builtin
pack's name — targets the repo-local ``.claude/hooks/<module>.py``.
Unattributable or unresolvable complaints (including legacy kinds whose prefix is
not a module path, e.g. ``<frozen importlib``) are dropped — precision over recall.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cc_transcript.filterspec import tool_uses
from cc_transcript.mining.confidence import MEDIUM, VERY_HIGH, CandidateSignal
from cc_transcript.mining.signals import MiningSignal
from cc_transcript.mining.sourcekind import SourceKind
from cc_transcript.mining.spec import CONFIDENCE_STEP, bump
from cc_transcript.models import (
    AssistantEvent,
    AttachmentEvent,
    HookAdditionalContext,
    HookBlockingError,
    ToolResultBlock,
    UserEvent,
)
from loguru import logger

from captain_hook.review.routing import CAPTAIN_HOOK_REPO, Target

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from cc_transcript.decisions import Decision, DecisionLog
    from cc_transcript.ids import SessionId, ToolUseId
    from cc_transcript.models import ToolUseBlock, TranscriptEvent

    from captain_hook.review.routing import PackIndex

HOOK_COMPLAINT = SourceKind("hook_complaint")
"""The source kind for an assistant turn dismissing a hook fire as a misfire."""

PROXIMITY_TURNS = 3
TIGHT_PROXIMITY_TURNS = 1
STOP_FEEDBACK_PREFIX = "Stop hook feedback:\n"
PACKS_DIR = "captain_hook/packs"
WHEEL_PACKAGE = "captain_hook/"
PACK_CACHE_SEGMENT = "captain-hook/packs/"
NAMED_HOOK_WINDOW_MS = 1_800_000
NAMED_HOOK_RE = re.compile(r"\b(?:the\s+)?([a-z][\w-]*(?:[\s-][a-z][\w-]*)?)\s+hooks?\b", re.IGNORECASE)

STRONG_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (misfire_class, re.compile(pattern, re.IGNORECASE))
    for misfire_class, pattern in (
        ("refire", r"\bre-?fired?\b"),
        ("misfire", r"\bmisfir(?:e[ds]?|ing)\b"),
        ("false_positive", r"\bfalse[ -]positives?\b"),
        ("false_alarm", r"\bfalse alarms?\b"),
        ("ignored_repeat", r"\bignoring (?:it|the repeats?)\b"),
        ("already_addressed", r"\balready (?:fixed|resolved|addressed)\b"),
        ("should_not_have_fired", r"\bshouldn'?t have fired\b"),
        (
            "incorrect_fire",
            r"\b(?:(?:incorrect|mistaken|erroneous)ly\b(?:\W+\w+){0,3}?\W+(?:fired|triggered|flagged)\b"
            r"|(?:fired|triggered|flagged)\b(?:\W+\w+){0,3}?\W+(?:incorrect|mistaken|erroneous)ly\b)",
        ),
        ("spurious", r"\bspurious\b"),
        ("unnecessary", r"\bunnecessar(?:y|ily)\b"),
    )
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
    if not re.search(r"\b(?:hook|reminder|nudge|gate|guard|warning)s?\b", text, re.IGNORECASE):
        return None
    hedged = re.search(
        r"(?:\bi think\b|\bseems?\b|\bmay\b|\bmight\b|\blooks like\b)"
        r"[\s\S]{0,80}?(?:false[ -]positive|false[ -]alarm|misfir|re-?fir|shouldn'?t have fired"
        r"|spurious|unnecessar|wrong(?:ly)?|incorrect(?:ly)?|mistaken(?:ly)?|erroneous(?:ly)?)",
        text,
        re.IGNORECASE,
    )
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
        case AttachmentEvent(
            detail=HookAdditionalContext(content=content, tool_use_id=tool_use_id, hook_event=hook_event)
        ):
            return Fingerprint(message="\n".join(content), tool_use_id=tool_use_id, event=hook_event)
        case AttachmentEvent(
            detail=HookBlockingError(blocking_error={"blockingError": blocking_error}, hook_event=hook_event)
        ):
            return Fingerprint(message=str(blocking_error), event=str(hook_event or "Stop"))
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


def attribute_from_fingerprints(
    decisions: DecisionLog,
    uses: Mapping[ToolUseId, ToolUseBlock],
    session_id: SessionId,
    near_ts_ms: int,
    fingerprints: Sequence[tuple[int, int, Fingerprint]],
) -> tuple[int, int, Decision] | None:
    attributed = [
        (index, turns_back, found)
        for index, turns_back, fingerprint in fingerprints
        if (found := attribute_fingerprint(decisions, uses, session_id, near_ts_ms, fingerprint)) is not None
    ]
    if not attributed or len({(found.source_file, found.kind) for _, _, found in attributed}) > 1:
        return None
    return attributed[0]


def hook_stem(kind: str) -> str | None:
    module, sep, _ = kind.partition(":")
    if not sep or not module:
        return None
    parts = module.split(".")
    return parts[-1] if all(part.isidentifier() for part in parts) else None


def name_slug(name: str) -> str:
    return re.sub(r"[-_\s]+", "", name.lower())


def named_hook_target(text: str, decisions: DecisionLog, session_id: SessionId, near_ts_ms: int) -> Decision | None:
    if not (names := {name_slug(match.group(1)) for match in NAMED_HOOK_RE.finditer(text)}):
        return None
    matched = [
        decision
        for decision in decisions.for_session(session_id)
        if decision.source_file
        and abs(decision.ts_ms - near_ts_ms) <= NAMED_HOOK_WINDOW_MS
        and (stem := hook_stem(decision.kind)) is not None
        and name_slug(stem) in names
    ]
    if len({decision.kind for decision in matched}) != 1:
        return None
    return min(matched, key=lambda decision: abs(decision.ts_ms - near_ts_ms))


def user_repo_source(source_file: str) -> bool:
    """A hook file living in the watched repo, not the installed wheel or the pack cache.

    Installed-wheel source (a ``nudge()``/``gate()`` primitive fire, or a ``hook()`` bundled
    in a builtin pack) carries ``captain_hook/`` in its path; a cached external pack carries
    the ``captain-hook/packs/`` cache segment. Neither is a repo path, so both resolve through
    the decision ``kind``'s module prefix instead of being returned verbatim.
    """
    return WHEEL_PACKAGE not in source_file and PACK_CACHE_SEGMENT not in source_file


def external_target_path(source_file: str) -> str | None:
    """The in-repo path for a cached external pack's ``source_file``.

    A cached external pack lives at ``.../captain-hook/packs/<name>@<sha>/<path>``, so
    the path within the pack's own repo is everything past the ``<name>@<sha>/`` segment.
    """
    return source_file.partition(PACK_CACHE_SEGMENT)[2].partition("/")[2] or None


def resolve_target(decision: Decision, index: PackIndex) -> Target | None:
    module, sep, _ = decision.kind.partition(":")
    # A discovered plugin pack's hook fires from the plugin's install dir — a path that reads as
    # a repo file to ``user_repo_source`` — so drop it here on the ``kind`` prefix, ahead of both
    # the verbatim early return and the ``.claude/hooks`` fallback, either of which would misfile
    # its fix as a repo-local PR. Plugin-pack kinds are always ``<pack>.<mod>`` (two segments); a
    # single-segment repo hook that merely shares a plugin's name keeps its repo-local route.
    if sep and module and len(segments := module.split(".")) >= 2 and segments[0] in index.plugins:
        logger.bind(hook=decision.kind).debug("dropped plugin-pack misfire complaint; no repo target this wave")
        return None
    if user_repo_source(decision.source_file):
        return Target(decision.source_file, decision.kind, repo=None, pack=None)
    if not sep or not module:
        return None
    match module.split("."):
        case [pack, mod] if (pack_dir := index.builtins.get(pack)) and (pack_dir / f"{mod}.py").is_file():
            return Target(f"{PACKS_DIR}/{pack}/{mod}.py", decision.kind, CAPTAIN_HOOK_REPO, pack)
        case [pack, _mod] if (route := index.externals.get(pack)) and (
            path := external_target_path(decision.source_file)
        ):
            return Target(path, decision.kind, route.repo, route.pack_name)
        case parts if all(part.isidentifier() for part in parts):
            return Target(f".claude/hooks/{parts[-1]}.py", decision.kind, repo=None, pack=None)
        case _:
            return None


def complaint_signal(marker: Marker, turns_back: int | None) -> CandidateSignal:
    base = CandidateSignal(
        VERY_HIGH if marker.strength == "strong" else MEDIUM,
        (f"{marker.strength}_marker", marker.misfire_class),
    )
    tight = turns_back is not None and turns_back <= TIGHT_PROXIMITY_TURNS
    return bump(base, CONFIDENCE_STEP, "tight_proximity") if tight else base


def iter_hook_complaint_signals(
    events: Sequence[TranscriptEvent], *, decisions: DecisionLog, index: PackIndex
) -> Iterator[MiningSignal]:
    """Yields one :class:`~cc_transcript.mining.MiningSignal` per attributed misfire complaint.

    Fires are joined by the events' own session UUID — the only session key —
    plus the tool call's content digest when the fingerprint carries a tool-use
    id, or by event name and timestamp proximity when it does not.

    Args:
        events: The transcript's full ordered event stream.
        decisions: The decision ledger joining fingerprint traces to the firing hook.
        index: The project's pack-to-repo map, routing a pack hook's fix to the
            pack's repo (see :class:`~captain_hook.review.routing.PackIndex`).

    Returns:
        Signals of kind :data:`HOOK_COMPLAINT` whose ``evidence`` stashes the
        attribution (``hook_name``, ``source_file``, ``event``, ``action``,
        ``fire_ts_ms``, ``fire_message``, ``marker``, ``attribution`` — either
        ``fingerprint`` or ``hook_name``) plus the resolved
        ``target_source_file``/``target_hook_name``/``target_repo``/``pack_name``/``misfire_class``.
    """
    uses = tool_uses(events)
    for event_index, event in enumerate(events):
        if not isinstance(event, AssistantEvent) or event.meta.is_sidechain or not event.text.strip():
            continue
        if (marker := classify_marker(event.text)) is None:
            continue
        near_ts_ms = int(event.meta.timestamp.timestamp() * 1000)
        session_id = event.meta.session_id
        if (
            primary := attribute_from_fingerprints(
                decisions, uses, session_id, near_ts_ms, preceding_fingerprints(events, event_index)
            )
        ) is not None:
            trigger_index, turns_back, fire = primary
            attribution = "fingerprint"
        elif (fire := named_hook_target(event.text, decisions, session_id, near_ts_ms)) is not None:
            trigger_index, turns_back, attribution = None, None, "hook_name"
        else:
            continue
        if (target := resolve_target(fire, index)) is None:
            continue
        yield MiningSignal(
            kind=HOOK_COMPLAINT,
            detector="hook_complaint",
            session_id=session_id,
            event_index=event_index,
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
                "attribution": attribution,
                "misfire_class": marker.misfire_class,
                "target_source_file": target.source_file,
                "target_hook_name": target.hook_name,
                "target_repo": target.repo,
                "pack_name": target.pack,
            },
            signal=complaint_signal(marker, turns_back),
        )
