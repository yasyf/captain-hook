"""The transcript scanner: mine user corrections and hook-misfire complaints into PR candidates.

The fact-recognition mechanism lives in :mod:`cc_transcript.mining` (the
six CREATE-mode user-correction detectors) and
:mod:`captain_hook.review.fix` (the FIX-mode :func:`~captain_hook.review.fix.iter_hook_complaint_signals`
detector over assistant turns); this module injects the reviewer's policy over
raw core transcript events read via :func:`cc_transcript.parser.stream` and
persists every surviving signal through one ingest codepath into
:class:`~captain_hook.review.store.ReviewStore`. Each surviving signal captures
its durable :class:`~cc_transcript.context.ContextWindow` via
:func:`~cc_transcript.context.capture_window` over the transcript's raw
bytes. The candidate floors partition
by kind: user-correction kinds gate under :data:`STRICT_USER` (event prefilter,
trigger-absence disqualification, the ``min_confidence`` floor) while
``hook_complaint`` gates under the ``STRICT_FIX`` floor (``min_confidence_fix``)
inside :func:`candidates_from`.

Dedup is scoped twice, deliberately diverging from cc-pushback's session-free
keys: each feedback event dedups per session (``dedup_key(kind, session_id,
*content)``, so a repeated correction within one session collapses to one
observation), while candidates group across sessions by the session-free
``rule = dedup_key(kind, *content)`` — the same correction in three sessions
yields three observations under one candidate row, which is what the
distinct-session eligibility thresholds count. Fix candidates group by their
attributed target, ``(hook_complaint, target_hook_name, target_source_file)``,
so two sessions' complaints about one hook collapse to one candidate.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from cc_transcript.builders import (
    build_spec,
    drop_compacted,
    drop_empty,
    drop_junk,
    drop_meta_flag,
    drop_phrases,
    drop_short,
    drop_sidechain,
    keep_only,
)
from cc_transcript.filterspec import (
    RESUME_PHRASE_SET,
    TRIVIAL_ACK_SET,
    USERS,
    Clause,
    FilterSpec,
    TextMatchesAny,
    event_meta,
    event_text,
    keep,
)
from cc_transcript.ids import EventRef
from cc_transcript.mining.candidates import FeedbackCandidate, dedup_key
from cc_transcript.mining.signals import mine
from cc_transcript.mining.spec import ALL_DETECTORS, MiningSpec
from cc_transcript.models import UserEvent
from cc_transcript.synthetic import synthetic_user_event

from captain_hook.review.fix import HOOK_COMPLAINT
from captain_hook.review.formats import review_spec
from captain_hook.review.repo import RepoKey, resolve_repo_key
from captain_hook.review.store import CandidateKind

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
    from typing import Any

    from cc_transcript.context import ContextWindow
    from cc_transcript.mining.signals import MiningSignal
    from cc_transcript.models import TranscriptEvent

    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

REVIEWER_MARKER = "capt-hook-session-reviewer"
"""The token the reviewer's own headless sessions carry in their first user message."""


class Detector(StrEnum):
    INTERRUPT = "interrupt"
    ASK_USER_QUESTION = "ask_user_question"
    PLAN_REENTRY = "plan_reentry"
    REVIEW_COMMENT = "review_comment"
    EXIT_PLAN_REJECTION = "exit_plan_rejection"
    DENIAL = "denial"
    TRANSCRIPT_MESSAGE = "transcript_message"


assert frozenset(Detector) <= frozenset(ALL_DETECTORS)

REVIEWER_MINING_SPEC = MiningSpec(review=review_spec())
"""The reviewer's mining policy: all six core detectors with the reviewer's review formats.

Scoring, provenance, reentry lookback, and edit tools take the :class:`MiningSpec`
defaults; only the review-comment policy (the three reviewer formats, ``typed``
surfaces, no structured formats) is customized.
"""

JUNK_CREATE_GROUPS: tuple[tuple[str, str], ...] = (
    ("agent_relay", r"\A\s*Another Claude session sent a message:"),
    (
        "agent_stop_notice",
        r'\A\s*(?:\d+\s+background agents?\s+(?:were|was)\s+stopped by the user|Background agent\s+")',
    ),
    (
        "at_path_handoff",
        r"\A\s*@\S*/\S+\.\w+\s+(?:read it\b|read\b|pick(?:ing)? up\b|implement\w*|impl\b|go\s+ah\w*d"
        r"|approv\w*|begin\b|beign\b|bgin\b|continue\b|resume\b|delete\b|do\s+(?:the|it)\b|let'?s\b|proceed\b)"
        r"[\s.!?]*\z",
    ),
    (
        "limits_reset",
        r"\A\s*(?:\w+,?\s+)?(?:session\s+)?limits?\s+"
        r"(?:have\s+|has\s+|were\s+|been\s+|have\s+been\s+)?reset(?:[,.]?\s+\w+)?\.?\s*\z",
    ),
    (
        "plan_approved_go",
        r"\A\s*(?:plan\s+)?appro(?:ved?|ced?)\b[\s,.:!@-]*"
        r"(?:@|begin|beign|bgin|bgn|begi\w*|go\b|implement\w*|impl\b|start\w*|work\b|do\b|proceed\w*|end\b|now\b|handoff|pick\b)",
    ),
    ("env_command_lead", r"\A\s*(?:[A-Z][A-Z0-9_]*=\S+\s+)+\S+[^\n]*--[^\n]*\n"),
)
"""Deterministic junk-create leads: agent lifecycle relays and stop notices, standalone
``@path`` plan handoffs, session-limit resume nudges, plan-approval advance directives,
and pasted ``ENV=x cmd --flags`` invocations. Each pattern is start-anchored, and the
whole-message classes — ``at_path_handoff``, ``limits_reset`` — are end-anchored to a
bare directive, so a junk lead trailed by real feedback keeps the tail and the survivor
rides the LLM triage and judge backstops."""

QUOTE_PASTE_RE = re.compile(r">[^\n]*(?:\n(?![^\s>])[^\n]*)*\Z")
"""A message that is a blockquote paste with no un-quoted feedback paragraph: the
lead line quotes and every later column-0 line is itself a quote (wrapped
continuation and blank lines allowed), so nothing outside the quote is the user's own."""

STRICT_USER_ENVELOPE: FilterSpec = build_spec(
    keep_only("user"),
    drop_sidechain(),
    drop_meta_flag("is_meta"),
    drop_compacted(),
)
"""The kind-and-metadata half of :data:`STRICT_USER`.

Judges a turn's envelope alone — user kind, and not a sidechain, meta, compacted, or
transcript-only turn — reading no text. It screens the real carrier of an
``exit_plan_rejection``, whose own text is empty, while the text half runs against the
extracted reason (:func:`reason_kept`) rather than the empty envelope.
"""

STRICT_USER_TEXT: FilterSpec = build_spec(
    drop_empty(only_from=USERS),
    drop_junk("structural", "agent_injection", "stop_hook", "continuation", "command_echo"),
    Clause(TextMatchesAny(JUNK_CREATE_GROUPS), applies_to=USERS),
    drop_phrases(TRIVIAL_ACK_SET | RESUME_PHRASE_SET),
    drop_short(2),
)
"""The text half of :data:`STRICT_USER`.

Drops structural noise, agent-injected banners, approve-and-advance directives,
stop-hook output, command echoes, the :data:`JUNK_CREATE_GROUPS` junk-create leads,
trivial acknowledgements, very short control messages, and empty turns.
"""

STRICT_USER: FilterSpec = build_spec(*STRICT_USER_ENVELOPE.clauses, *STRICT_USER_TEXT.clauses)
"""The event-level prefilter for user-authored corrections.

Drops structural noise, agent-injected banners, approve-and-advance directives,
stop-hook output, command echoes, the :data:`JUNK_CREATE_GROUPS` junk-create leads,
trivial acknowledgements, very short control messages, and
sidechain/meta/compacted/empty turns.
"""

GATED_DETECTORS = frozenset(
    {Detector.TRANSCRIPT_MESSAGE, Detector.PLAN_REENTRY, Detector.REVIEW_COMMENT, Detector.EXIT_PLAN_REJECTION}
)
"""CREATE detectors whose surviving signal must clear the :data:`STRICT_USER` prefilter
and the paste-only structural check before it can become a candidate."""

COLLAPSE_DETECTORS = frozenset(
    {
        Detector.EXIT_PLAN_REJECTION,
        Detector.PLAN_REENTRY,
        Detector.DENIAL,
        Detector.INTERRUPT,
        Detector.REVIEW_COMMENT,
    }
)
"""CREATE detectors whose surviving signal shadows an equal-text ``transcript_message`` at the same event."""


def is_paste_only(text: str) -> bool:
    """Whether ``text`` is a verbatim paste — a fenced block or blockquote — with no feedback tail.

    Pairs with :data:`STRICT_USER` as the structural half of the junk-create prefilter:
    the regex leads there can't reason about a multi-line quote or fence closing before a
    substantive tail, so this handles the two paste shapes in Python. A paste trailed by
    the user's own feedback keeps the tail (this returns ``False``), matching the
    continue-with-tail semantics the regex leads preserve by anchoring.
    """
    stripped = text.strip()
    if QUOTE_PASTE_RE.match(stripped):
        return True
    if not stripped.startswith("```"):
        return False
    if (close := re.search(r"\n[ \t]*```[ \t]*(?:\n|\Z)", stripped[3:])) is None:
        return True
    return not stripped[3:][close.end() :].strip()


@dataclass(frozen=True, slots=True)
class ScanReport:
    """The outcome of one scan pass.

    Attributes:
        scanned: The number of transcripts parsed and recorded.
        inserted: The number of newly inserted feedback events.
    """

    scanned: int
    inserted: int


def reason_kept(text: str) -> bool:
    """Whether an extracted ``exit_plan_rejection`` reason clears the text prefilter.

    ``exit_plan_rejection`` fires on the tool-result turn that carries the rejection, whose
    own ``text`` is empty — the miner lifts the user's reason into ``sig.text``. Gating the
    empty envelope would drop every real rejection, so the prefilter runs against the
    extracted reason instead; every other gated detector already fires on the user turn whose
    text it screens. The reason is minted into a synthetic user turn via the upstream
    constructor and run through the same strict filterspec gate.
    """
    event = synthetic_user_event(text, uuid="exit-plan-reason", session_id="exit-plan-reason")
    return keep(event, STRICT_USER_TEXT) and not is_paste_only(text)


def gated_survives(event: TranscriptEvent, sig: MiningSignal) -> bool:
    if sig.detector == Detector.EXIT_PLAN_REJECTION:
        return keep(event, STRICT_USER_ENVELOPE) and reason_kept(sig.text)
    return keep(event, STRICT_USER) and not is_paste_only(event_text(event))


def survives(events: Sequence[TranscriptEvent], sig: MiningSignal) -> bool:
    if sig.detector in GATED_DETECTORS and not gated_survives(events[sig.event_index], sig):
        return False
    return not (sig.detector == Detector.TRANSCRIPT_MESSAGE and sig.trigger_index is None)


def rule_parts(sig: MiningSignal) -> tuple[str, ...]:
    match sig.detector:
        case detector if detector == HOOK_COMPLAINT:
            return (HOOK_COMPLAINT, str(sig.evidence["target_hook_name"]), str(sig.evidence["target_source_file"]))
        case Detector.TRANSCRIPT_MESSAGE:
            return (Detector.TRANSCRIPT_MESSAGE, sig.text)
        case Detector.EXIT_PLAN_REJECTION:
            return ("plan_review", "exit_plan", sig.text)
        case Detector.PLAN_REENTRY:
            return ("plan_review", Detector.PLAN_REENTRY, sig.text)
        case Detector.DENIAL | Detector.INTERRUPT:
            return ("interrupt_rejection", sig.text)
        case Detector.REVIEW_COMMENT:
            return (
                Detector.REVIEW_COMMENT,
                sig.evidence["file"] or "",
                str(sig.evidence["line_start"] or ""),
                str(sig.evidence["line_end"] or ""),
                sig.text,
            )
        case Detector.ASK_USER_QUESTION:
            return ("question_answer", str(sig.evidence["question"] or ""), sig.text)
        case _:
            raise AssertionError(sig.detector)


def parts(sig: MiningSignal) -> tuple[str, ...]:
    kind, *content = rule_parts(sig)
    return (kind, sig.session_id, *content)


def payload_of(sig: MiningSignal) -> Mapping[str, Any] | None:
    match sig.detector:
        case detector if detector == HOOK_COMPLAINT:
            return dict(sig.evidence)
        case Detector.TRANSCRIPT_MESSAGE:
            return None
        case Detector.EXIT_PLAN_REJECTION | Detector.PLAN_REENTRY | Detector.INTERRUPT:
            return {"detector": sig.detector}
        case Detector.DENIAL:
            return dict(sig.evidence) or None
        case Detector.REVIEW_COMMENT:
            return {key: sig.evidence[key] for key in ("format", "file", "line_start", "line_end")}
        case Detector.ASK_USER_QUESTION:
            return dict(sig.evidence)
        case _:
            raise AssertionError(sig.detector)


def to_candidate(window: ContextWindow, sig: MiningSignal) -> FeedbackCandidate:
    anchor = window.anchor
    return FeedbackCandidate(
        dedup_key=dedup_key(*parts(sig)),
        source_kind=sig.kind,
        occurred_at=sig.occurred_at,
        text=sig.text,
        window=window,
        ref=anchor,
        session_id=sig.session_id,
        cc_version=sig.cc_version,
        payload=payload_of(sig),
        signal=sig.signal,
    )


def detect(events: Sequence[TranscriptEvent]) -> Iterator[MiningSignal]:
    """Mines all six neutral detectors over one transcript's events.

    Args:
        events: The transcript's full ordered event stream.

    Returns:
        Every mining signal the detectors recognize, ungated; review comments
        run under the reviewer's :data:`REVIEWER_MINING_SPEC`.
    """
    return mine(events, REVIEWER_MINING_SPEC)


def candidates_from(
    events: Sequence[TranscriptEvent],
    signals: Iterable[MiningSignal],
    *,
    capture: Callable[[Sequence[EventRef]], Sequence[ContextWindow]],
    min_confidence: float,
    min_confidence_fix: float,
) -> Iterator[tuple[MiningSignal, FeedbackCandidate]]:
    kept = [
        sig
        for sig in signals
        if survives(events, sig)
        and sig.signal.confidence >= (min_confidence_fix if sig.kind == HOOK_COMPLAINT else min_confidence)
    ]
    if not kept:
        return
    windows = capture([EventRef(sig.session_id, sig.event_uuid) for sig in kept])
    for sig, window in zip(kept, windows, strict=True):
        yield sig, to_candidate(window, sig)


def is_reviewer_session(events: Sequence[TranscriptEvent]) -> bool:
    return next((REVIEWER_MARKER in event.text for event in events if isinstance(event, UserEvent)), False)


def transcript_repo(events: Sequence[TranscriptEvent]) -> RepoKey | None:
    return next(
        (
            key
            for event in events
            if (meta := event_meta(event)) is not None
            if meta.cwd is not None
            if (key := resolve_repo_key(meta.cwd)) is not None
        ),
        None,
    )


def transcript_cwd(events: Sequence[TranscriptEvent]) -> Path | None:
    return next(
        (Path(meta.cwd) for event in events if (meta := event_meta(event)) is not None if meta.cwd is not None),
        None,
    )


def collapse_cross_detector(
    kept: Sequence[tuple[MiningSignal, FeedbackCandidate]],
) -> list[tuple[MiningSignal, FeedbackCandidate]]:
    shadowed = {
        (sig.session_id, sig.event_uuid, sig.text)
        for sig, _ in kept
        if sig.detector in COLLAPSE_DETECTORS and sig.event_uuid is not None
    }
    return [
        (sig, candidate)
        for sig, candidate in kept
        if not (sig.detector == Detector.TRANSCRIPT_MESSAGE and (sig.session_id, sig.event_uuid, sig.text) in shadowed)
    ]


async def ingest(
    store: ReviewStore,
    prepared: Mapping[str, Any],
    *,
    corrections: Sequence[Mapping[str, Any]],
    repo_key: RepoKey | None = None,
) -> ScanReport:
    from cc_transcript.mining.store import event_row, now

    from captain_hook.snapshots.review import decode_candidate, record_correction_drafts

    repo_key = repo_key or prepared["repo_key"]
    kept = [decode_candidate(raw) for raw in prepared["candidates_json"]]
    async with store.db.transaction():
        ingested_at = now()
        inserted = len(await store.db.insert_candidates([event_row(candidate, ingested_at) for candidate, _ in kept]))
        await store.db.record_file(prepared["canonical_path"], int(prepared["mtime_ns"]) / 1_000_000_000)
        for candidate, rule in kept:
            if repo_key is None:
                raise ValueError("prepared candidates require a repository identity")
            evidence = candidate.payload or {}
            candidate_id = (
                await store.ensure_candidate(
                    RepoKey(target_repo) if (target_repo := evidence["target_repo"]) else repo_key,
                    kind=CandidateKind.FIX,
                    rule=dedup_key(*rule),
                    source_kind=candidate.source_kind,
                    target_source_file=str(evidence["target_source_file"]),
                    target_hook_name=str(evidence["target_hook_name"]),
                    misfire_class=str(evidence["misfire_class"]),
                    origin_repo_key=repo_key if target_repo else None,
                    pack_name=evidence["pack_name"],
                )
                if candidate.source_kind == HOOK_COMPLAINT
                else await store.ensure_candidate(
                    repo_key, kind=CandidateKind.CREATE, rule=dedup_key(*rule), source_kind=candidate.source_kind
                )
            )
            await store.record_observation(
                candidate_id,
                dedup_key=candidate.dedup_key,
                session_id=candidate.ref.session_id,
                occurred_at=candidate.occurred_at,
            )
    await record_correction_drafts(corrections)
    return ScanReport(scanned=1, inserted=inserted)


def prepare_source(
    client: Any, path: Path, settings: ReviewSettings, repo_key: RepoKey | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from captain_hook.decisions import decisions_db_path
    from captain_hook.snapshots.review import REVIEW_POLICY, ProjectionBudget, decode_candidate
    from captain_hook.util.paths import resolve_claude_config_dir

    session = client.acquire(path)
    budget = ProjectionBudget()
    try:
        prepared: dict[str, Any] | None = None
        candidates: list[str] = []
        for page in client.pages(
            "prepare_review",
            domain=True,
            view=session.view(),
            policy=REVIEW_POLICY,
            min_confidence=settings.min_confidence,
            min_confidence_fix=settings.min_confidence_fix,
            repo_key=repo_key,
            decision_log_path=str(decision_path.absolute())
            if (decision_path := decisions_db_path()) is not None
            else None,
            claude_config_dir=str(resolve_claude_config_dir().absolute()),
        ):
            if prepared is None:
                prepared = dict(page)
            budget.add(page)
            candidates.extend(page["candidates_json"])
        if prepared is None:
            raise ValueError("review preparation completed without metadata")
        prepared["candidates_json"] = candidates
        eligible = [
            candidate for raw in candidates if (candidate := decode_candidate(raw)[0]).source_kind != HOOK_COMPLAINT
        ]
        corrections: list[dict[str, Any]] = []
        for offset in range(0, len(eligible), 256):
            batch = eligible[offset : offset + 256]
            for page in client.pages(
                "prepare_corrections",
                domain=True,
                view=session.view(),
                policy=REVIEW_POLICY,
                anchors=[asdict(candidate.ref) for candidate in batch],
                feedback=[candidate.text for candidate in batch],
                repo=prepared["cwd"],
            ):
                budget.add(page)
                corrections.extend(page["corrections"])
        return prepared, corrections
    finally:
        session.release()


async def scan_transcript(
    store: ReviewStore, path: Path, *, settings: ReviewSettings, repo_key: RepoKey | None = None
) -> ScanReport:
    return await scan(store, settings=settings, transcripts=[path], repo_key=repo_key)


async def scan(
    store: ReviewStore, *, settings: ReviewSettings, transcripts: Sequence[Path], repo_key: RepoKey | None = None
) -> ScanReport:
    import asyncio

    from captain_hook.snapshots.review import review_client

    roots = sorted({str(path.absolute()) for path in transcripts})
    if not roots:
        return ScanReport(0, 0)
    checkpoint_key = f"snapshot_discovery:{dedup_key(*roots)}"
    checkpoint = await store.meta(checkpoint_key)
    scanned = inserted = 0
    next_checkpoint = None
    from captain_hook.snapshots.client import EvidenceIncomplete

    with review_client() as client:
        try:
            pages = await asyncio.to_thread(lambda: list(client.pages("discover", roots=roots, checkpoint=checkpoint)))
        except EvidenceIncomplete as exc:
            if exc.status != "stale_cursor":
                raise
            pages = await asyncio.to_thread(lambda: list(client.pages("discover", roots=roots, checkpoint=None)))
        for page in pages:
            next_checkpoint = page["checkpoint"]
            for entry in page["entries"]:
                if entry["state"] != "present":
                    continue
                path = entry["path"]
                revision_key = f"snapshot_revision:{dedup_key(path)}"
                if await store.meta(revision_key) == entry["revision"]:
                    continue
                prepared, corrections = await asyncio.to_thread(prepare_source, client, Path(path), settings, repo_key)
                report = await ingest(store, prepared, corrections=corrections, repo_key=repo_key)
                await store.set_meta(revision_key, entry["revision"])
                scanned += report.scanned
                inserted += report.inserted
    if next_checkpoint is not None:
        await store.set_meta(checkpoint_key, next_checkpoint)
    return ScanReport(scanned, inserted)
