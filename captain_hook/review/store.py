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
from cc_transcript.mining.confidence import from_payload
from cc_transcript.mining.store import FEEDBACK_DDL, FeedbackStore, now
from cc_transcript.store import FileStateStore

from captain_hook.review.repo import RepoKey

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path
    from typing import Any

    from cc_transcript.ids import SessionId
    from cc_transcript.mining.candidates import DedupKey
    from cc_transcript.mining.confidence import Confidence
    from cc_transcript.mining.sourcekind import SourceKind

    from captain_hook.review.settings import ReviewSettings

REVIEW_DDL = """
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
"""

INSERT_CANDIDATE = """
INSERT INTO candidates (
  repo_key, candidate_kind, rule, source_kind, status,
  target_source_file, target_hook_name, misfire_class, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
"""

INSERT_OBSERVATION = """
INSERT INTO candidate_observations (candidate_id, dedup_key, session_id, occurred_at)
VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING
"""

ACCEPTED_OBSERVATIONS_QUERY = """
WITH latest AS (
  SELECT v.dedup_key, v.{accepted} AS accepted, v.confidence, ROW_NUMBER() OVER (
    PARTITION BY v.dedup_key ORDER BY v.judged_at DESC, v.id DESC
  ) AS rn
  FROM {table} v
  WHERE v.role = 'judge' AND v.prompt_version = ?
)
SELECT o.session_id, substr(o.occurred_at, 1, 10) AS day, e.payload_json
FROM candidate_observations o
JOIN latest l ON l.dedup_key = o.dedup_key AND l.rn = 1
JOIN feedback_events e ON e.dedup_key = o.dedup_key
WHERE o.candidate_id = ? AND l.accepted = 1 AND l.confidence >= ?
"""

OPEN_PRS_QUERY = "SELECT COUNT(*) AS n FROM candidates WHERE repo_key = ? AND status = ? AND pr_opened_at > ?"

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
    CandidateStatus.WATCHING: frozenset({CandidateStatus.PR_OPEN}),
    CandidateStatus.PR_OPEN: frozenset({CandidateStatus.STALE, CandidateStatus.ACCEPTED, CandidateStatus.REJECTED}),
    CandidateStatus.STALE: frozenset({CandidateStatus.ACCEPTED, CandidateStatus.REJECTED}),
    CandidateStatus.ACCEPTED: frozenset(),
    CandidateStatus.REJECTED: frozenset(),
}


def signal_confidence(payload_json: object) -> Confidence:
    payload: dict[str, Any] = json.loads(str(payload_json)) if payload_json else {}
    return from_payload(payload["signal"]).confidence


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
        return cls(await FileStateStore.open(path, extra_schema=FEEDBACK_DDL + cls.verdicts_ddl() + REVIEW_DDL))

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
            INSERT_CANDIDATE,
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
            INSERT_OBSERVATION,
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
        cur = await conn.execute("SELECT repo_key, candidate_kind, status FROM candidates WHERE id = ?", (candidate_id,))
        if not (candidates := [dict(row) async for row in cur]):
            raise LookupError(f"no candidate with id {candidate_id}")
        repo, kind = RepoKey(str(candidates[0]["repo_key"])), CandidateKind(str(candidates[0]["candidate_kind"]))
        status = CandidateStatus(str(candidates[0]["status"]))

        accepted_cur = await conn.execute(
            ACCEPTED_OBSERVATIONS_QUERY.format(table=self.VERDICT_TABLE, accepted=self.ACCEPTED_COLUMN),
            (prompt_version, candidate_id, settings.min_judge_confidence),
        )
        accepted = [dict(row) async for row in accepted_cur]

        cutoff = (datetime.now(UTC) - timedelta(days=settings.stale_after_days)).isoformat()
        prs_cur = await conn.execute(OPEN_PRS_QUERY, (repo, CandidateStatus.PR_OPEN, cutoff))

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

        Create candidates need ``min_sessions`` distinct judge-accepted sessions
        across ``min_days`` distinct UTC days. Fix candidates need the
        ``min_sessions_fix``/``min_days_fix`` pair, or one observation that is
        both judge-accepted and heuristically at least
        ``min_confidence_fix_single``. Both kinds require the repo watched and a
        free slot under ``max_open_prs``.

        Args:
            candidate_id: The candidate to check.
            settings: The thresholds and judge knobs to check under.
            prompt_version: The judge prompt version whose verdicts apply.
        """
        status = await self.threshold_status(candidate_id, settings=settings, prompt_version=prompt_version)
        if status.status != CandidateStatus.WATCHING or not status.watching or status.open_prs >= settings.max_open_prs:
            return False
        match status.kind:
            case CandidateKind.CREATE:
                return status.sessions >= settings.min_sessions and status.days >= settings.min_days
            case CandidateKind.FIX:
                return (
                    status.sessions >= settings.min_sessions_fix and status.days >= settings.min_days_fix
                ) or status.single_observation
