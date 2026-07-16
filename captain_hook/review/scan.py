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

import json
import re
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
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
    Clause,
    FilterSpec,
    TextMatchesAny,
    event_meta,
    event_text,
    keep,
)
from cc_transcript.ids import EventRef
from cc_transcript.mining.candidates import FeedbackCandidate, dedup_key
from cc_transcript.mining.filterspec import at_least, build_candidate_filter, keep_candidate
from cc_transcript.mining.signals import mine
from cc_transcript.mining.spec import MiningSpec
from cc_transcript.models import UserEvent
from cc_transcript.parser import TranscriptParser, parse_events_from_bytes

from captain_hook.decisions import decisions_db_path, open_decision_log
from captain_hook.review.fix import HOOK_COMPLAINT, iter_hook_complaint_signals
from captain_hook.review.formats import review_spec
from captain_hook.review.repo import RepoKey, resolve_repo_key
from captain_hook.review.routing import PackIndex
from captain_hook.review.store import CandidateKind

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence
    from typing import Any

    from cc_transcript.backend import ParsedTranscript
    from cc_transcript.mining.signals import MiningSignal
    from cc_transcript.models import TranscriptEvent

    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

REVIEWER_MARKER = "capt-hook-session-reviewer"
"""The token the reviewer's own headless sessions carry in their first user message."""

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
        r"\A\s*(?:\d+\s+background agents?\s+(?:were|was)\s+stopped by the user|Background agent\s+\")",
    ),
    (
        "at_path_handoff",
        r"\A\s*@\S*/\S+\.\w+\s+(?:read it\b|read\b|pick(?:ing)? up\b|implement\w*|impl\b|go\s+ah\w*d"
        r"|approv\w*|begin\b|beign\b|bgin\b|continue\b|resume\b|delete\b|do\s+(?:the|it)\b|let'?s\b|proceed\b)"
        r"[\s.!?]*\Z",
    ),
    (
        "limits_reset",
        r"\A\s*(?:\w+,?\s+)?(?:session\s+)?limits?\s+"
        r"(?:have\s+|has\s+|were\s+|been\s+|have\s+been\s+)?reset(?:[,.]?\s+\w+)?\.?\s*\Z",
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

GATED_DETECTORS = frozenset({"transcript_message", "plan_reentry", "review_comment", "exit_plan_rejection"})
"""CREATE detectors whose surviving signal must clear the :data:`STRICT_USER` prefilter
and the paste-only structural check before it can become a candidate."""

COLLAPSE_DETECTORS = frozenset({"exit_plan_rejection", "plan_reentry", "denial", "interrupt", "review_comment"})
"""CREATE detectors whose surviving signal shadows an equal-text ``transcript_message`` at the same event."""

REASON_ENTRY: dict[str, Any] = {
    "type": "user",
    "uuid": "exit-plan-reason",
    "sessionId": "exit-plan-reason",
    "timestamp": "1970-01-01T00:00:00+00:00",
}
"""The synthetic user-turn envelope the extracted ``exit_plan_rejection`` reason rides."""


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
    text it screens. Transcript events are frozen native views — not constructible from Python
    and not :func:`dataclasses.replace`-able — so the reason is re-materialized as the
    :data:`REASON_ENTRY` user turn through the parser, never a re-texted copy of the carrier.
    """
    (event,) = parse_events_from_bytes(
        (json.dumps(REASON_ENTRY | {"message": {"role": "user", "content": text}}) + "\n").encode()
    )
    return keep(event, STRICT_USER_TEXT) and not is_paste_only(text)


def gated_survives(event: TranscriptEvent, sig: MiningSignal) -> bool:
    if sig.detector == "exit_plan_rejection":
        return keep(event, STRICT_USER_ENVELOPE) and reason_kept(sig.text)
    return keep(event, STRICT_USER) and not is_paste_only(event_text(event))


def survives(events: Sequence[TranscriptEvent], sig: MiningSignal) -> bool:
    if sig.detector in GATED_DETECTORS and not gated_survives(events[sig.event_index], sig):
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
        case "ask_user_question":
            return ("question_answer", str(sig.evidence["question"] or ""), sig.text)
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
        case "ask_user_question":
            return dict(sig.evidence)
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
    """Mines all six neutral detectors over one transcript's events.

    Args:
        events: The transcript's full ordered event stream.

    Returns:
        Every mining signal the detectors recognize, ungated; review comments
        run under the reviewer's :data:`REVIEWER_MINING_SPEC`.
    """
    return mine(events, REVIEWER_MINING_SPEC)


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


def transcript_cwd(events: Sequence[TranscriptEvent]) -> Path | None:
    return next(
        (Path(meta.cwd) for event in events if (meta := event_meta(event)) is not None if meta.cwd is not None),
        None,
    )


async def record_corrections(
    events: Sequence[TranscriptEvent], kept: Sequence[tuple[MiningSignal, FeedbackCandidate]], *, repo: Path | None
) -> None:
    """Grounds each user-correction candidate in the shared code-correction ledger.

    For every kept user-correction signal (the FIX-mode ``hook_complaint`` is a
    local hook misfire, not a code correction, so it is skipped), harvests the
    edit the feedback faults around its anchor and appends one row to the family
    ledger. Idempotent per anchor: a no-op when cc-pushback already wrote it, so
    captain-hook only fills the ledger for sessions nobody else processed.
    """
    from cc_transcript.corrections import CorrectionLog
    from cc_transcript.extract import extract_correction, usable_backend

    corrections = [(sig, candidate) for sig, candidate in kept if sig.kind != HOOK_COMPLAINT]
    if not corrections:
        return
    activity = SessionActivity.from_events(corrections[0][0].session_id, events)
    backend = usable_backend()
    log = CorrectionLog.open()
    for sig, candidate in corrections:
        await extract_correction(
            log, activity, candidate.ref, source="captain-hook", feedback=sig.text, repo=repo, backend=backend
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
        if not (sig.detector == "transcript_message" and (sig.session_id, sig.event_uuid, sig.text) in shadowed)
    ]


async def ingest(
    store: ReviewStore, parsed: ParsedTranscript, *, settings: ReviewSettings, repo_key: RepoKey | None
) -> ScanReport:
    repo_key = repo_key or transcript_repo(parsed.events)
    if repo_key is None or is_reviewer_session(parsed.events):
        await store.record_file_scan(str(parsed.path), parsed.mtime, [])
        return ScanReport(scanned=1, inserted=0)
    signals = chain(
        iter_hook_complaint_signals(
            parsed.events,
            decisions=open_decision_log(decisions_db_path()),
            index=PackIndex.load(transcript_cwd(parsed.events)),
        ),
        detect(parsed.events),
    )
    kept = collapse_cross_detector(list(candidates_from(parsed.events, signals, settings=settings)))
    inserted = await store.record_file_scan(str(parsed.path), parsed.mtime, [candidate for _, candidate in kept])
    for sig, candidate in kept:
        async with store.store.transaction():
            candidate_id = (
                await store.ensure_candidate(
                    RepoKey(target_repo) if (target_repo := sig.evidence["target_repo"]) else repo_key,
                    kind=CandidateKind.FIX,
                    rule=dedup_key(*rule_parts(sig)),
                    source_kind=sig.kind,
                    target_source_file=str(sig.evidence["target_source_file"]),
                    target_hook_name=str(sig.evidence["target_hook_name"]),
                    misfire_class=str(sig.evidence["misfire_class"]),
                    origin_repo_key=repo_key if target_repo else None,
                    pack_name=sig.evidence["pack_name"],
                )
                if sig.kind == HOOK_COMPLAINT
                else await store.ensure_candidate(
                    repo_key, kind=CandidateKind.CREATE, rule=dedup_key(*rule_parts(sig)), source_kind=sig.kind
                )
            )
            await store.record_observation(
                candidate_id, dedup_key=candidate.dedup_key, session_id=sig.session_id, occurred_at=sig.occurred_at
            )
    await record_corrections(parsed.events, kept, repo=transcript_cwd(parsed.events))
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
