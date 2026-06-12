"""The transcript scanner: mine user corrections and hook-misfire complaints into PR candidates.

The fact-recognition mechanism lives in :mod:`cc_transcript.mining` (the
six CREATE-mode user-correction detectors) and
:mod:`captain_hook.review.fix` (the FIX-mode :func:`~captain_hook.review.fix.iter_hook_complaint_signals`
detector over assistant turns); this module injects the reviewer's policy over
raw core transcript events read via :class:`cc_transcript.TranscriptParser` and
persists every surviving signal through one ingest codepath into
:class:`~captain_hook.review.store.ReviewStore`. Each surviving signal captures
its durable :class:`~cc_transcript.context.ContextWindow` via
:func:`~cc_transcript.context.capture_window` over the transcript lifted into a
:class:`~cc_transcript.activity.SessionActivity`. The candidate floors partition
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

from dataclasses import dataclass
from itertools import chain
from typing import TYPE_CHECKING

from cc_transcript.activity import SessionActivity
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
from cc_transcript.context import capture_window
from cc_transcript.discovery import TranscriptDiscovery
from cc_transcript.filterspec import (
    RESUME_PHRASE_SET,
    TRIVIAL_ACK_SET,
    USERS,
    FilterSpec,
    event_meta,
    keep,
)
from cc_transcript.ids import EventRef
from cc_transcript.mining.candidates import FeedbackCandidate, dedup_key
from cc_transcript.mining.filterspec import at_least, build_candidate_filter, keep_candidate
from cc_transcript.mining.signals import (
    iter_interrupt_marker_signals,
    iter_plan_reentry_signals,
    iter_plan_rejection_signals,
    iter_review_comment_signals,
    iter_tool_denial_signals,
    iter_user_message_signals,
)
from cc_transcript.models import UserEvent
from cc_transcript.parser import TranscriptParser

from captain_hook.decisions import decisions_db_path, open_decision_log
from captain_hook.review.fix import HOOK_COMPLAINT, iter_hook_complaint_signals
from captain_hook.review.formats import formats
from captain_hook.review.repo import resolve_repo_key
from captain_hook.review.store import CandidateKind

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence
    from pathlib import Path
    from typing import Any

    from cc_transcript.backend import ParsedTranscript
    from cc_transcript.mining.signals import MiningSignal
    from cc_transcript.models import TranscriptEvent

    from captain_hook.review.repo import RepoKey
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

REVIEWER_MARKER = "capt-hook-session-reviewer"
"""The token the reviewer's own headless sessions carry in their first user message."""

SPEC_DETECTORS = frozenset({"transcript_message", "plan_reentry", "review_comment"})

STRICT_USER: FilterSpec = build_spec(
    keep_only("user"),
    drop_sidechain(),
    drop_meta_flag("is_meta"),
    drop_compacted(),
    drop_empty(only_from=USERS),
    drop_junk("structural", "agent_injection", "stop_hook", "continuation", "command_echo"),
    drop_phrases(TRIVIAL_ACK_SET | RESUME_PHRASE_SET),
    drop_short(2),
)
"""The event-level prefilter for user-authored corrections.

Drops structural noise, agent-injected banners, approve-and-advance directives,
stop-hook output, command echoes, trivial acknowledgements, very short control
messages, and sidechain/meta/compacted/empty turns.
"""


@dataclass(frozen=True, slots=True)
class ScanReport:
    """The outcome of one scan pass.

    Attributes:
        scanned: The number of transcripts parsed and recorded.
        inserted: The number of newly inserted feedback events.
    """

    scanned: int
    inserted: int


def survives(events: Sequence[TranscriptEvent], sig: MiningSignal) -> bool:
    if sig.detector in SPEC_DETECTORS and not keep(events[sig.event_index], STRICT_USER):
        return False
    return not (sig.detector == "transcript_message" and sig.trigger_index is None)


def rule_parts(sig: MiningSignal) -> tuple[str, ...]:
    match sig.detector:
        case "hook_complaint":
            return ("hook_complaint", str(sig.evidence["target_hook_name"]), str(sig.evidence["target_source_file"]))
        case "transcript_message":
            return ("transcript_message", sig.text)
        case "exit_plan_rejection":
            return ("plan_review", "exit_plan", sig.text)
        case "plan_reentry":
            return ("plan_review", "plan_reentry", sig.text)
        case "denial" | "interrupt":
            return ("interrupt_rejection", sig.text)
        case "review_comment":
            return (
                "review_comment",
                sig.evidence["file"] or "",
                str(sig.evidence["line_start"] or ""),
                str(sig.evidence["line_end"] or ""),
                sig.text,
            )
        case _:
            raise AssertionError(sig.detector)


def parts(sig: MiningSignal) -> tuple[str, ...]:
    kind, *content = rule_parts(sig)
    return (kind, sig.session_id, *content)


def payload_of(sig: MiningSignal) -> Mapping[str, Any] | None:
    match sig.detector:
        case "hook_complaint":
            return dict(sig.evidence)
        case "transcript_message":
            return None
        case "exit_plan_rejection" | "plan_reentry" | "interrupt":
            return {"detector": sig.detector}
        case "denial":
            return dict(sig.evidence) or None
        case "review_comment":
            return {key: sig.evidence[key] for key in ("format", "file", "line_start", "line_end")}
        case _:
            raise AssertionError(sig.detector)


def to_candidate(activity: SessionActivity, sig: MiningSignal) -> FeedbackCandidate:
    anchor = EventRef(sig.session_id, sig.event_uuid)
    return FeedbackCandidate(
        dedup_key=dedup_key(*parts(sig)),
        source_kind=sig.kind,
        occurred_at=sig.occurred_at,
        text=sig.text,
        window=capture_window(activity, anchor),
        ref=anchor,
        session_id=sig.session_id,
        cc_version=sig.cc_version,
        payload=payload_of(sig),
        signal=sig.signal,
    )


def detect(events: Sequence[TranscriptEvent]) -> Iterator[MiningSignal]:
    """Composes all six neutral mining iterators over one transcript's events.

    Args:
        events: The transcript's full ordered event stream.

    Returns:
        Every mining signal the detectors recognize, ungated; review comments
        run under the reviewer's :func:`~captain_hook.review.formats.formats`.
    """
    return chain(
        iter_user_message_signals(events),
        iter_plan_rejection_signals(events),
        iter_plan_reentry_signals(events),
        iter_tool_denial_signals(events),
        iter_interrupt_marker_signals(events),
        iter_review_comment_signals(events, formats()),
    )


def candidates_from(
    events: Sequence[TranscriptEvent], signals: Iterable[MiningSignal], *, settings: ReviewSettings
) -> Iterator[tuple[MiningSignal, FeedbackCandidate]]:
    strict_user = build_candidate_filter(at_least(settings.min_confidence))
    strict_fix = build_candidate_filter(at_least(settings.min_confidence_fix))
    activity: SessionActivity | None = None
    for sig in signals:
        if not survives(events, sig):
            continue
        if activity is None:
            activity = SessionActivity.from_events(sig.session_id, events)
        candidate = to_candidate(activity, sig)
        if keep_candidate(candidate, strict_fix if sig.kind == HOOK_COMPLAINT else strict_user):
            yield sig, candidate


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


async def ingest(
    store: ReviewStore, parsed: ParsedTranscript, *, settings: ReviewSettings, repo_key: RepoKey | None
) -> ScanReport:
    repo_key = repo_key or transcript_repo(parsed.events)
    if repo_key is None or is_reviewer_session(parsed.events):
        await store.record_file_scan(str(parsed.path), parsed.mtime, [])
        return ScanReport(scanned=1, inserted=0)
    signals = chain(
        iter_hook_complaint_signals(parsed.events, decisions=open_decision_log(decisions_db_path())),
        detect(parsed.events),
    )
    kept = list(candidates_from(parsed.events, signals, settings=settings))
    inserted = await store.record_file_scan(str(parsed.path), parsed.mtime, [candidate for _, candidate in kept])
    for sig, candidate in kept:
        candidate_id = (
            await store.ensure_candidate(
                repo_key,
                kind=CandidateKind.FIX,
                rule=dedup_key(*rule_parts(sig)),
                source_kind=sig.kind,
                target_source_file=str(sig.evidence["target_source_file"]),
                target_hook_name=str(sig.evidence["target_hook_name"]),
                misfire_class=str(sig.evidence["misfire_class"]),
            )
            if sig.kind == HOOK_COMPLAINT
            else await store.ensure_candidate(
                repo_key, kind=CandidateKind.CREATE, rule=dedup_key(*rule_parts(sig)), source_kind=sig.kind
            )
        )
        await store.record_observation(
            candidate_id, dedup_key=candidate.dedup_key, session_id=sig.session_id, occurred_at=sig.occurred_at
        )
    return ScanReport(scanned=1, inserted=inserted)


async def scan_transcript(
    store: ReviewStore, path: Path, *, settings: ReviewSettings, repo_key: RepoKey | None = None
) -> ScanReport:
    """Scans one transcript for user corrections and hook-misfire complaints, incrementally.

    The transcript is parsed only when new or modified since the last recorded
    scan; a transcript that fails to parse — for example one Claude Code is
    still appending to — is left unrecorded, so the next scan retries it. The
    reviewer's own headless sessions (first user message carrying
    :data:`REVIEWER_MARKER`) and transcripts whose ``cwd`` is not a git repo are
    recorded with no candidates.

    Args:
        store: The store to read mtimes from and write events and candidates to.
        path: The transcript file to scan — for example the exact path the
            SessionEnd hook received on stdin.
        settings: The reviewer settings supplying the ``min_confidence`` floor.
        repo_key: The repo the session belongs to; resolved from the
            transcript's ``cwd`` metadata when omitted.

    Returns:
        The :class:`ScanReport` for this pass.
    """
    known = await store.file_mtimes()
    mtime = await TranscriptDiscovery.stat_mtime(path)
    if mtime is None or ((prev := known.get(str(path))) is not None and prev >= mtime):
        return ScanReport(scanned=0, inserted=0)
    async for parsed in TranscriptParser.stream_transcripts([(path, mtime)]):
        return await ingest(store, parsed, settings=settings, repo_key=repo_key)
    return ScanReport(scanned=0, inserted=0)


async def scan(store: ReviewStore, *, settings: ReviewSettings, transcripts: Sequence[Path]) -> ScanReport:
    """Scans explicit transcript files and directories for corrections and misfire complaints, incrementally.

    ``cc_transcript`` hardcodes its projects directory, so every entry point
    here takes explicit paths instead: directories are searched recursively for
    ``*.jsonl`` transcripts, files are scanned directly, and each transcript is
    parsed only when new or modified since the last recorded scan. The repo each
    transcript belongs to is resolved from its ``cwd`` metadata.

    Args:
        store: The store to read mtimes from and write events and candidates to.
        settings: The reviewer settings supplying the ``min_confidence`` floor.
        transcripts: Transcript files and/or directories to scan.

    Returns:
        The combined :class:`ScanReport` for this pass.
    """
    known = await store.file_mtimes()
    paths: list[tuple[Path, float]] = []
    for entry in transcripts:
        if entry.is_dir():
            paths.extend(await TranscriptDiscovery.find_in(entry, known_mtimes=known))
        elif (mtime := await TranscriptDiscovery.stat_mtime(entry)) is not None and (
            (prev := known.get(str(entry))) is None or prev < mtime
        ):
            paths.append((entry, mtime))
    scanned = 0
    inserted = 0
    async for parsed in TranscriptParser.stream_transcripts(paths):
        report = await ingest(store, parsed, settings=settings, repo_key=None)
        scanned += report.scanned
        inserted += report.inserted
    return ScanReport(scanned=scanned, inserted=inserted)
