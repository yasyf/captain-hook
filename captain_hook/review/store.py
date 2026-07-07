"""The reviewer's SQLite store: feedback events, judge verdicts, and PR candidates.

Layers three review tables onto :class:`cc_transcript.mining.FeedbackStore`'s
event ledger and :class:`cc_transcript.judge.VerdictStoreMixin`'s fidelity-aware
verdict table: ``candidates`` (one row per grouped correction or misfire),
``candidate_observations`` (one row per evidencing feedback event), and ``repos``
(the per-repo watching flag). Eligibility is judge-aware: only observations whose
latest judge verdict accepts them with enough confidence count toward the thresholds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Self

from cc_transcript.judge.verdicts import VerdictStoreMixin
from cc_transcript.mining.confidence import NOISE_FLOOR, from_payload
from cc_transcript.mining.store import FEEDBACK_DDL, FeedbackStore, now
from cc_transcript.store import FileStateStore

from captain_hook.review.repo import RepoKey

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path
    from typing import Any

    from cc_transcript.corrections import Correction
    from cc_transcript.ids import SessionId
    from cc_transcript.judge.similar import KeyOverlap
    from cc_transcript.mining.candidates import DedupKey
    from cc_transcript.mining.confidence import Confidence
    from cc_transcript.mining.sourcekind import SourceKind

    from captain_hook.review.settings import ReviewSettings

SPLIT_THRESHOLD = 0.9

CANDIDATES_QUERY = """
SELECT c.*,
  (SELECT e.text FROM candidate_observations o JOIN feedback_events e ON e.dedup_key = o.dedup_key
   WHERE o.candidate_id = c.id ORDER BY o.id LIMIT 1) AS sample_text,
  (SELECT COUNT(*) FROM candidate_observations o WHERE o.candidate_id = c.id) AS observations
FROM candidates c
"""


class InvalidTransition(Exception):
    """Raised when a candidate status move is outside :data:`TRANSITIONS`."""


class CandidateKind(StrEnum):
    """The two PR shapes a candidate can produce: a new hook, or a fix to an attributed hook."""

    CREATE = "create"
    FIX = "fix"


class CandidateStatus(StrEnum):
    """A candidate's lifecycle state; ``ACCEPTED`` and ``REJECTED`` are terminal."""

    WATCHING = "watching"
    PR_OPEN = "pr_open"
    STALE = "stale"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


TRANSITIONS: Mapping[CandidateStatus, frozenset[CandidateStatus]] = {
    CandidateStatus.WATCHING: frozenset({CandidateStatus.PR_OPEN, CandidateStatus.REJECTED}),
    CandidateStatus.PR_OPEN: frozenset({CandidateStatus.STALE, CandidateStatus.ACCEPTED, CandidateStatus.REJECTED}),
    CandidateStatus.STALE: frozenset({CandidateStatus.ACCEPTED, CandidateStatus.REJECTED}),
    CandidateStatus.ACCEPTED: frozenset(),
    CandidateStatus.REJECTED: frozenset(),
}


def signal_confidence(payload_json: object) -> Confidence:
    payload: dict[str, Any] = json.loads(str(payload_json)) if payload_json else {}
    return from_payload(payload["signal"]).confidence


def judge_worthy(row: Mapping[str, object]) -> bool:
    return signal_confidence(row["payload_json"]) >= NOISE_FLOOR


@dataclass(frozen=True, slots=True)
class ThresholdStatus:
    """The judge-accepted evidence counts behind one candidate's eligibility call.

    Attributes:
        kind: The candidate's PR shape.
        status: The candidate's lifecycle status; only ``watching`` candidates
            can become eligible.
        watching: Whether the candidate's repo is watched.
        sessions: How many distinct sessions carry a judge-accepted observation.
        days: How many distinct UTC days carry a judge-accepted observation.
        open_prs: How many of the repo's candidates hold a live, non-stale PR.
        single_observation: Whether any observation is both judge-accepted and
            heuristically at least ``min_confidence_fix_single`` — the fix-mode
            single-observation path.
    """

    kind: CandidateKind
    status: CandidateStatus
    watching: bool
    sessions: int
    days: int
    open_prs: int
    single_observation: bool


def crosses_thresholds(status: ThresholdStatus, *, settings: ReviewSettings) -> bool:
    """Whether a candidate's judge-accepted evidence clears its kind's PR thresholds.

    The single eligibility predicate, shared by :meth:`ReviewStore.eligible` and
    the status dashboard so a candidate shown as eligible is exactly one the
    reviewer would act on. Create candidates need ``min_sessions`` distinct
    judge-accepted sessions across ``min_days`` distinct UTC days; fix candidates
    need the ``min_sessions_fix``/``min_days_fix`` pair or one observation that is
    both judge-accepted and heuristically at least ``min_confidence_fix_single``.
    Both require the repo watched and a free slot under ``max_open_prs``.
    """
    if status.status != CandidateStatus.WATCHING or not status.watching or status.open_prs >= settings.max_open_prs:
        return False
    match status.kind:
        case CandidateKind.CREATE:
            return status.sessions >= settings.min_sessions and status.days >= settings.min_days
        case CandidateKind.FIX:
            return (
                status.sessions >= settings.min_sessions_fix and status.days >= settings.min_days_fix
            ) or status.single_observation


@dataclass(frozen=True, slots=True)
class CandidateView:
    """One candidate's full dashboard record: its row, evidence counts, eligibility, and the PR it would open.

    Attributes:
        row: The :meth:`ReviewStore.candidates` row (status, kind, ``pr_url``,
            ``sample_text``, ``observations``, and the fix targets).
        threshold: The judge-accepted evidence counts behind the eligibility call.
        eligible: Whether :func:`crosses_thresholds` accepts ``threshold``.
        summary: The highest-confidence accepted verdict's one-sentence summary —
            what the candidate's PR would do — or ``None`` while still unjudged.
    """

    row: dict[str, object]
    threshold: ThresholdStatus
    eligible: bool
    summary: str | None


@dataclass(frozen=True, slots=True)
class SpawnHealth:
    """The detached reviewer's run health — what the status dashboard's top line reads.

    Attributes:
        last: The newest ``spawn_runs`` row, or ``None`` before the first recorded run.
        consecutive_failures: How many runs have failed since the last success.
        failing_since: The failing streak's first ``started_at``, or ``None`` while healthy.
    """

    last: dict[str, object] | None
    consecutive_failures: int
    failing_since: str | None


@dataclass(frozen=True, slots=True)
class JudgeHealth:
    """The judge lane's dashboard health: backlog, recency, and the slug-split signal.

    Attributes:
        pending: How many judge-worthy corrections still lack a verdict at the
            current prompt version.
        last_verdict_at: The newest judge verdict's timestamp at the current
            prompt version, or ``None`` before any verdict lands.
        splits: Distinct canonical-key pairs whose evidence centroids nearly
            coincide — possible slug splits the reviewer may want to reconcile.
    """

    pending: int
    last_verdict_at: str | None
    splits: tuple[KeyOverlap, ...]


class ReviewStore(VerdictStoreMixin, FeedbackStore):
    """The session reviewer's persistent store over a :class:`FileStateStore`.

    Keeps the verdict mixin's generic physical names (``verdicts`` with
    ``accepted``/``summary`` columns) and layers the review tables on top of the
    feedback-event ledger. Candidates group equivalent evidence — create
    candidates by ``(repo_key, rule)``, fix candidates by ``(repo_key,
    target_hook_name, target_source_file)`` — and every write is idempotent, so
    re-scanning a session is a no-op.

    Example:
        >>> async with await ReviewStore.open(settings.db_path) as store:
        ...     await store.eligible(candidate_id, settings=settings, prompt_version=1)
    """

    @classmethod
    async def open(cls, path: Path) -> Self:
        """Opens (creating if needed) the review database at ``path``."""
        return cls(
            await FileStateStore.open(
                path,
                extra_schema=FEEDBACK_DDL
                + cls.verdicts_ddl()
                + """
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_key TEXT NOT NULL,
  candidate_kind TEXT NOT NULL CHECK (candidate_kind IN ('create', 'fix')),
  rule TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('watching', 'pr_open', 'stale', 'accepted', 'rejected')),
  pr_url TEXT,
  pr_opened_at TEXT,
  target_source_file TEXT,
  target_hook_name TEXT,
  misfire_class TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK (
    (candidate_kind = 'create' AND target_source_file IS NULL AND target_hook_name IS NULL
      AND misfire_class IS NULL)
    OR (candidate_kind = 'fix' AND target_source_file IS NOT NULL AND target_hook_name IS NOT NULL)
  )
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_create_key
  ON candidates(repo_key, rule) WHERE candidate_kind = 'create';
CREATE UNIQUE INDEX IF NOT EXISTS idx_candidates_fix_key
  ON candidates(repo_key, target_hook_name, target_source_file) WHERE candidate_kind = 'fix';
CREATE INDEX IF NOT EXISTS idx_candidates_repo_status ON candidates(repo_key, status);
CREATE TABLE IF NOT EXISTS candidate_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id INTEGER NOT NULL REFERENCES candidates(id),
  dedup_key TEXT NOT NULL REFERENCES feedback_events(dedup_key),
  session_id TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  UNIQUE(candidate_id, dedup_key)
);
CREATE INDEX IF NOT EXISTS idx_observations_dedup ON candidate_observations(dedup_key);
CREATE TABLE IF NOT EXISTS repos (
  repo_key TEXT PRIMARY KEY,
  watching INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS spawn_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  transcript TEXT NOT NULL,
  ok INTEGER NOT NULL,
  error TEXT,
  report_json TEXT,
  CHECK ((ok = 1) = (error IS NULL))
);
""",
            )
        )

    async def enable(self, repo: RepoKey) -> None:
        """Marks ``repo`` watched, allowing its candidates to become eligible."""
        await self.store.conn.execute(
            "INSERT INTO repos (repo_key, watching) VALUES (?, 1) ON CONFLICT(repo_key) DO UPDATE SET watching = 1",
            (repo,),
        )

    async def disable(self, repo: RepoKey) -> None:
        """Marks ``repo`` unwatched; its candidates stay recorded but never become eligible."""
        await self.store.conn.execute(
            "INSERT INTO repos (repo_key, watching) VALUES (?, 0) ON CONFLICT(repo_key) DO UPDATE SET watching = 0",
            (repo,),
        )

    async def watching(self, repo: RepoKey) -> bool:
        """Returns whether ``repo`` is watched; unknown repos are not."""
        cur = await self.store.conn.execute("SELECT watching FROM repos WHERE repo_key = ?", (repo,))
        return bool(rows[0]["watching"]) if (rows := [row async for row in cur]) else False

    async def ensure_candidate(
        self,
        repo: RepoKey,
        *,
        kind: CandidateKind,
        rule: str,
        source_kind: SourceKind,
        target_source_file: str | None = None,
        target_hook_name: str | None = None,
        misfire_class: str | None = None,
    ) -> int:
        """Finds or creates the candidate for a grouping key, returning its id.

        New candidates start in :attr:`CandidateStatus.WATCHING`; an existing
        candidate with the same grouping key is returned untouched.

        Args:
            repo: The candidate's repo.
            kind: The candidate's PR shape.
            rule: The slug grouping equivalent corrections.
            source_kind: The detector that produced the first evidence.
            target_source_file: The misfiring hook's file (fix candidates only).
            target_hook_name: The misfiring hook's registered name (fix candidates only).
            misfire_class: The misfire taxonomy label, when classified (fix candidates only).

        Returns:
            The candidate's id.
        """
        stamp = now()
        await self.store.conn.execute(
            """
INSERT INTO candidates (
  repo_key, candidate_kind, rule, source_kind, status,
  target_source_file, target_hook_name, misfire_class, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
""",
            (
                repo,
                kind,
                rule,
                source_kind,
                CandidateStatus.WATCHING,
                target_source_file,
                target_hook_name,
                misfire_class,
                stamp,
                stamp,
            ),
        )
        match kind:
            case CandidateKind.CREATE:
                query = "SELECT id FROM candidates WHERE repo_key = ? AND candidate_kind = ? AND rule = ?"
                params: tuple[object, ...] = (repo, kind, rule)
            case CandidateKind.FIX:
                query = (
                    "SELECT id FROM candidates WHERE repo_key = ? AND candidate_kind = ? "
                    "AND target_hook_name = ? AND target_source_file = ?"
                )
                params = (repo, kind, target_hook_name, target_source_file)
        cur = await self.store.conn.execute(query, params)
        return int([row["id"] async for row in cur][0])

    async def record_observation(
        self, candidate_id: int, *, dedup_key: DedupKey, session_id: SessionId, occurred_at: datetime
    ) -> None:
        """Links one evidencing feedback event to a candidate, idempotently.

        ``occurred_at`` is stored normalized to UTC so distinct-day counts read
        calendar days off the stored prefix. Re-recording the same
        ``(candidate, dedup_key)`` pair is a no-op.

        Args:
            candidate_id: The candidate the event evidences.
            dedup_key: The stored feedback event's dedup key.
            session_id: The session the event came from.
            occurred_at: When the feedback was given.
        """
        await self.store.conn.execute(
            """
INSERT INTO candidate_observations (candidate_id, dedup_key, session_id, occurred_at)
VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING
""",
            (candidate_id, dedup_key, session_id, occurred_at.astimezone(UTC).isoformat()),
        )

    async def candidates(
        self, repo: RepoKey | None = None, *, status: CandidateStatus | None = None
    ) -> list[dict[str, object]]:
        """Returns candidate rows, newest first, optionally narrowed by repo and status.

        Each row carries the ``candidates`` columns plus ``sample_text`` (the
        earliest observation's verbatim correction) and ``observations`` (the
        evidence count).

        Args:
            repo: When set, restrict to this repo.
            status: When set, restrict to this status.
        """
        filters = [
            (clause, value)
            for clause, value in (("c.repo_key = ?", repo), ("c.status = ?", status))
            if value is not None
        ]
        query = (
            CANDIDATES_QUERY
            + (f"WHERE {' AND '.join(clause for clause, _ in filters)}\n" if filters else "")
            + "ORDER BY c.id DESC"
        )
        cur = await self.store.conn.execute(query, tuple(value for _, value in filters))
        return [dict(row) async for row in cur]

    async def candidate(self, candidate_id: int) -> dict[str, object]:
        """Returns one candidate's row in :meth:`candidates` shape.

        Raises:
            LookupError: If no candidate carries ``candidate_id``.
        """
        cur = await self.store.conn.execute(CANDIDATES_QUERY + "WHERE c.id = ?", (candidate_id,))
        if not (rows := [dict(row) async for row in cur]):
            raise LookupError(f"no candidate with id {candidate_id}")
        return rows[0]

    async def transition(
        self,
        candidate_id: int,
        to: CandidateStatus,
        *,
        pr_url: str | None = None,
        pr_opened_at: datetime | None = None,
    ) -> None:
        """Moves a candidate along :data:`TRANSITIONS` — the only status-write codepath.

        Args:
            candidate_id: The candidate to move.
            to: The target status.
            pr_url: When set, stamped onto the candidate (the PR-opening move).
            pr_opened_at: When set, stamped in UTC alongside ``pr_url``.

        Raises:
            InvalidTransition: If the move is not allowed from the current status.
            LookupError: If no candidate carries ``candidate_id``.
        """
        cur = await self.store.conn.execute("SELECT status FROM candidates WHERE id = ?", (candidate_id,))
        if not (rows := [str(row["status"]) async for row in cur]):
            raise LookupError(f"no candidate with id {candidate_id}")
        if to not in TRANSITIONS[current := CandidateStatus(rows[0])]:
            raise InvalidTransition(f"{current} -> {to}")
        await self.store.conn.execute(
            "UPDATE candidates SET status = ?, updated_at = ?, "
            "pr_url = COALESCE(?, pr_url), pr_opened_at = COALESCE(?, pr_opened_at) WHERE id = ?",
            (to, now(), pr_url, pr_opened_at.astimezone(UTC).isoformat() if pr_opened_at else None, candidate_id),
        )

    async def regroup_create(self, *, prompt_version: int) -> tuple[int, int]:
        """Re-parents, retires, and sweeps watching create candidates onto their durable slugs.

        The treadmill that turns the scanner's per-session digest candidates into
        durable rule candidates, run once at the close of each judge pass under a
        single transaction. In order:

        1. **Re-parent** every observation on a watching create candidate whose
           judge verdict at ``prompt_version`` is accepted and names a
           ``canonical_key`` different from the candidate's rule, onto the create
           candidate keyed by that slug — created on first need, taking the
           observation's source kind — earliest observation first so the slug
           candidate keeps earliest-evidence order.
        2. **Retire** every watching create candidate all of whose observations
           are judged with none accepted to :attr:`CandidateStatus.REJECTED`.
        3. **Sweep** every watching create candidate left with no observations
           whose ``updated_at`` predates this pass, deleting it; a candidate a
           concurrent ingest just created is newer and survives.

        Fix candidates and terminal or PR-open create candidates are never touched.

        Args:
            prompt_version: The judge prompt version whose verdicts decide acceptance.

        Returns:
            ``(merged, retired)`` — observations re-parented and candidates rejected.
        """
        from cc_transcript.ids import SessionId
        from cc_transcript.mining.candidates import DedupKey
        from cc_transcript.mining.sourcekind import SourceKind

        started = now()
        async with self.store.transaction() as conn:
            reparent = [
                dict(row)
                async for row in await conn.execute(
                    f"""
SELECT o.id AS obs_id, o.dedup_key, o.session_id, o.occurred_at, c.repo_key, e.source_kind, v.canonical_key
FROM candidate_observations o
JOIN candidates c ON c.id = o.candidate_id
JOIN feedback_events e ON e.dedup_key = o.dedup_key
JOIN {self.VERDICT_TABLE} v ON v.dedup_key = o.dedup_key AND v.role = 'judge' AND v.prompt_version = ?
WHERE c.candidate_kind = 'create' AND c.status = ?
  AND v.{self.ACCEPTED_COLUMN} = 1 AND v.canonical_key IS NOT NULL AND v.canonical_key != c.rule
ORDER BY o.id
""",
                    (prompt_version, CandidateStatus.WATCHING),
                )
            ]
            for row in reparent:
                new_id = await self.ensure_candidate(
                    RepoKey(str(row["repo_key"])),
                    kind=CandidateKind.CREATE,
                    rule=str(row["canonical_key"]),
                    source_kind=SourceKind(str(row["source_kind"])),
                )
                await self.record_observation(
                    new_id,
                    dedup_key=DedupKey(str(row["dedup_key"])),
                    session_id=SessionId(str(row["session_id"])),
                    occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
                )
                await conn.execute("DELETE FROM candidate_observations WHERE id = ?", (row["obs_id"],))

            retire = [
                int(row["id"])
                async for row in await conn.execute(
                    f"""
SELECT c.id FROM candidates c
WHERE c.candidate_kind = 'create' AND c.status = ?
  AND EXISTS (SELECT 1 FROM candidate_observations o WHERE o.candidate_id = c.id)
  AND NOT EXISTS (
    SELECT 1 FROM candidate_observations o
    LEFT JOIN {self.VERDICT_TABLE} v
      ON v.dedup_key = o.dedup_key AND v.role = 'judge' AND v.prompt_version = ?
    WHERE o.candidate_id = c.id AND (v.id IS NULL OR v.{self.ACCEPTED_COLUMN} = 1)
  )
""",
                    (CandidateStatus.WATCHING, prompt_version),
                )
            ]
            for candidate_id in retire:
                await self.transition(candidate_id, CandidateStatus.REJECTED)

            await conn.execute(
                """
DELETE FROM candidates
WHERE candidate_kind = 'create' AND status = ? AND updated_at < ?
  AND NOT EXISTS (SELECT 1 FROM candidate_observations o WHERE o.candidate_id = candidates.id)
""",
                (CandidateStatus.WATCHING, started),
            )
        return len(reparent), len(retire)

    async def threshold_status(
        self, candidate_id: int, *, settings: ReviewSettings, prompt_version: int
    ) -> ThresholdStatus:
        """Returns the judge-accepted evidence counts behind one candidate's eligibility.

        An observation counts only when its dedup key's latest judge verdict at
        ``prompt_version`` accepts it with confidence at or above
        ``settings.min_judge_confidence``; unjudged observations count as
        not-yet, so they retry on the next session's pass.

        Args:
            candidate_id: The candidate to inspect.
            settings: The thresholds and judge knobs to count under.
            prompt_version: The judge prompt version whose verdicts apply.

        Returns:
            The counts the eligibility call (and the CLI's explanation) reads.

        Raises:
            LookupError: If no candidate carries ``candidate_id``.
        """
        conn = self.store.conn
        cur = await conn.execute(
            "SELECT repo_key, candidate_kind, status FROM candidates WHERE id = ?", (candidate_id,)
        )
        if not (candidates := [dict(row) async for row in cur]):
            raise LookupError(f"no candidate with id {candidate_id}")
        repo, kind = RepoKey(str(candidates[0]["repo_key"])), CandidateKind(str(candidates[0]["candidate_kind"]))
        status = CandidateStatus(str(candidates[0]["status"]))

        accepted_cur = await conn.execute(
            f"""
SELECT o.session_id, substr(o.occurred_at, 1, 10) AS day, e.payload_json
FROM candidate_observations o
JOIN {self.VERDICT_TABLE} v ON v.dedup_key = o.dedup_key AND v.role = 'judge' AND v.prompt_version = ?
JOIN feedback_events e ON e.dedup_key = o.dedup_key
WHERE o.candidate_id = ? AND v.{self.ACCEPTED_COLUMN} = 1 AND v.confidence >= ?
""",
            (prompt_version, candidate_id, settings.min_judge_confidence),
        )
        accepted = [dict(row) async for row in accepted_cur]

        cutoff = (datetime.now(UTC) - timedelta(days=settings.stale_after_days)).isoformat()
        prs_cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM candidates WHERE repo_key = ? AND status = ? AND pr_opened_at > ?",
            (repo, CandidateStatus.PR_OPEN, cutoff),
        )

        return ThresholdStatus(
            kind=kind,
            status=status,
            watching=await self.watching(repo),
            sessions=len({row["session_id"] for row in accepted}),
            days=len({row["day"] for row in accepted}),
            open_prs=[int(row["n"]) async for row in prs_cur][0],
            single_observation=any(
                signal_confidence(row["payload_json"]) >= settings.min_confidence_fix_single for row in accepted
            ),
        )

    async def eligible(self, candidate_id: int, *, settings: ReviewSettings, prompt_version: int) -> bool:
        """Returns whether a candidate's judge-accepted evidence crosses its thresholds.

        Delegates to :func:`crosses_thresholds` over the candidate's
        :meth:`threshold_status`, so the dashboard and the reviewer agree on what
        is eligible.

        Args:
            candidate_id: The candidate to check.
            settings: The thresholds and judge knobs to check under.
            prompt_version: The judge prompt version whose verdicts apply.
        """
        return crosses_thresholds(
            await self.threshold_status(candidate_id, settings=settings, prompt_version=prompt_version),
            settings=settings,
        )

    async def pr_summary(self, candidate_id: int, *, settings: ReviewSettings, prompt_version: int) -> str | None:
        """Returns the candidate's most-confident accepted verdict summary — what its PR would do.

        Reads the same judge-accepted observations the thresholds count and
        returns the highest-confidence verdict's one-sentence summary, or ``None``
        while no observation is judged-accepted yet.

        Args:
            candidate_id: The candidate to describe.
            settings: The judge knobs supplying ``min_judge_confidence``.
            prompt_version: The judge prompt version whose verdicts apply.
        """
        cur = await self.store.conn.execute(
            f"""
WITH latest AS (
  SELECT v.dedup_key, v.{self.ACCEPTED_COLUMN} AS accepted, v.{self.SUMMARY_COLUMN} AS summary, v.confidence,
    ROW_NUMBER() OVER (
      PARTITION BY v.dedup_key ORDER BY v.judged_at DESC, v.id DESC
    ) AS rn
  FROM {self.VERDICT_TABLE} v
  WHERE v.role = 'judge' AND v.prompt_version = ?
)
SELECT l.summary AS summary
FROM candidate_observations o
JOIN latest l ON l.dedup_key = o.dedup_key AND l.rn = 1
WHERE o.candidate_id = ? AND l.accepted = 1 AND l.confidence >= ?
ORDER BY l.confidence DESC, o.id DESC
LIMIT 1
""",
            (prompt_version, candidate_id, settings.min_judge_confidence),
        )
        return str(rows[0]["summary"]) if (rows := [dict(row) async for row in cur]) else None

    async def correction_evidence(self, candidate_id: int) -> tuple[Correction, ...]:
        """Returns the shared-ledger code corrections grounding a candidate's observations.

        Joins each observation back to its feedback anchor ``(session_id,
        event_uuid)`` and pulls the corrections the family ledger holds for that
        anchor — the offending before/after edit the PR-drafting brain needs. The
        reviewer's own per-session pass writes these rows, so a candidate that
        crossed its thresholds carries its faulted edits.
        """
        from cc_transcript.corrections import CorrectionLog
        from cc_transcript.ids import EventUuid, SessionId

        cur = await self.store.conn.execute(
            """
SELECT DISTINCT e.session_id, e.event_uuid
FROM candidate_observations o
JOIN feedback_events e ON e.dedup_key = o.dedup_key
WHERE o.candidate_id = ? AND e.session_id IS NOT NULL AND e.event_uuid IS NOT NULL
ORDER BY o.id
""",
            (candidate_id,),
        )
        log = CorrectionLog.open()
        return tuple(
            correction
            for row in [dict(row) async for row in cur]
            for correction in log.for_anchor(SessionId(str(row["session_id"])), EventUuid(str(row["event_uuid"])))
        )

    async def candidate_view(
        self, row: dict[str, object], *, settings: ReviewSettings, prompt_version: int
    ) -> CandidateView:
        """Assembles one :class:`CandidateView` from a :meth:`candidates` row."""
        candidate_id = int(str(row["id"]))
        threshold = await self.threshold_status(candidate_id, settings=settings, prompt_version=prompt_version)
        return CandidateView(
            row=row,
            threshold=threshold,
            eligible=crosses_thresholds(threshold, settings=settings),
            summary=await self.pr_summary(candidate_id, settings=settings, prompt_version=prompt_version),
        )

    async def overview(
        self, repo: RepoKey | None = None, *, settings: ReviewSettings, prompt_version: int
    ) -> list[CandidateView]:
        """Returns a :class:`CandidateView` per candidate — the status dashboard's whole read.

        Args:
            repo: When set, restrict to this repo.
            settings: The thresholds and judge knobs to evaluate under.
            prompt_version: The judge prompt version whose verdicts apply.
        """
        return [
            await self.candidate_view(row, settings=settings, prompt_version=prompt_version)
            for row in await self.candidates(repo)
        ]

    async def record_spawn_run(
        self,
        transcript: str,
        *,
        started_at: datetime,
        ok: bool,
        error: str | None = None,
        report_json: str | None = None,
    ) -> None:
        """Records one detached reviewer run's outcome — the only spawn-health write.

        ``finished_at`` is stamped here, so the row's span is start-to-record.

        Args:
            transcript: The reviewed session's transcript path.
            started_at: When the run started.
            ok: Whether the run finished cleanly.
            error: The crash's ``TypeName: message`` line (failed runs only).
            report_json: The run's serialized ``SpawnReport`` (clean runs only).
        """
        await self.store.conn.execute(
            """
INSERT INTO spawn_runs (started_at, finished_at, transcript, ok, error, report_json)
VALUES (?, ?, ?, ?, ?, ?)
""",
            (started_at.astimezone(UTC).isoformat(), now(), transcript, int(ok), error, report_json),
        )

    async def spawn_health(self) -> SpawnHealth:
        """Returns the detached reviewer's run health — the only spawn-health read.

        The failing streak is every run after the last clean one, so a single
        success resets both ``consecutive_failures`` and ``failing_since``.
        """
        last_cur = await self.store.conn.execute("SELECT * FROM spawn_runs ORDER BY id DESC LIMIT 1")
        streak_cur = await self.store.conn.execute(
            """
WITH streak AS (
  SELECT id, started_at FROM spawn_runs
  WHERE id > COALESCE((SELECT MAX(id) FROM spawn_runs WHERE ok = 1), 0)
)
SELECT
  (SELECT COUNT(*) FROM streak) AS consecutive_failures,
  (SELECT started_at FROM streak ORDER BY id LIMIT 1) AS failing_since
"""
        )
        streak = [dict(row) async for row in streak_cur][0]
        return SpawnHealth(
            last=rows[0] if (rows := [dict(row) async for row in last_cur]) else None,
            consecutive_failures=int(streak["consecutive_failures"]),
            failing_since=str(since) if (since := streak["failing_since"]) is not None else None,
        )

    async def judge_backlog(self, *, prompt_version: int) -> int:
        """Counts judge-worthy corrections still lacking a verdict at ``prompt_version``.

        The dashboard's pending count: every :meth:`unjudged` row (summary-refresh
        included) that clears :func:`judge_worthy` — the same noise-floor predicate
        the judge pass sends rows on, so the count matches what the next pass judges.
        """
        return sum(
            judge_worthy(row)
            for row in await self.unjudged(role="judge", prompt_version=prompt_version, refresh_summary=True)
        )

    async def has_verdict_evidence(self, *, prompt_version: int) -> bool:
        """Whether any canonical-key evidence is stored at ``prompt_version`` to suggest from.

        Gates the judge pass's per-row slug suggestions: with the companion
        ``verdict_evidence`` table absent or empty at this version, no suggestion
        is possible by construction, so the pass skips the embedder load entirely.
        """
        cur = await self.store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'verdict_evidence'"
        )
        if await cur.fetchone() is None:
            return False
        cur = await self.store.conn.execute(
            "SELECT 1 FROM verdict_evidence WHERE prompt_version = ? LIMIT 1", (prompt_version,)
        )
        return await cur.fetchone() is not None

    async def slug_splits(self, *, prompt_version: int, threshold: float = SPLIT_THRESHOLD) -> list[KeyOverlap]:
        """Returns canonical-key pairs whose evidence centroids nearly coincide — possible slug splits.

        Delegates to :func:`cc_transcript.judge.near_duplicate_keys` over this
        store's verdict-evidence vectors; nothing merges automatically, the caller
        decides what to do with a flagged pair.

        Args:
            prompt_version: The prompt version whose evidence to compare.
            threshold: The exclusive cosine-similarity floor a pair must clear.
        """
        from cc_transcript.judge.similar import near_duplicate_keys

        return await near_duplicate_keys(self, prompt_version=prompt_version, threshold=threshold)

    async def judge_health(self, *, prompt_version: int) -> JudgeHealth:
        """Returns the judge lane's dashboard health at ``prompt_version`` — the only judge-health read.

        Bundles the backlog count, the newest verdict's timestamp, and the
        slug-split signal so the status dashboard reads them in one call.
        """
        cur = await self.store.conn.execute(
            f"SELECT MAX(judged_at) AS last FROM {self.VERDICT_TABLE} WHERE role = 'judge' AND prompt_version = ?",
            (prompt_version,),
        )
        last = [row["last"] async for row in cur][0]
        return JudgeHealth(
            pending=await self.judge_backlog(prompt_version=prompt_version),
            last_verdict_at=str(last) if last is not None else None,
            splits=tuple(await self.slug_splits(prompt_version=prompt_version)),
        )
