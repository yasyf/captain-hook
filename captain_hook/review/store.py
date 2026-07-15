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
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING, Self

from cc_transcript.judge.verdicts import VerdictStoreMixin
from cc_transcript.mining.confidence import NOISE_FLOOR, from_payload
from cc_transcript.mining.store import FEEDBACK_DDL, FeedbackStore, now
from cc_transcript.store import FileStateStore

from captain_hook.review.fix import HOOK_COMPLAINT
from captain_hook.review.prompts import CREATE_TEMPLATE, FIX_TEMPLATE
from captain_hook.review.repo import RepoKey, pr_repo_key

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path
    from typing import Any

    import aiosqlite
    from cc_transcript.corrections import Correction
    from cc_transcript.ids import SessionId
    from cc_transcript.judge.similar import KeyOverlap
    from cc_transcript.mining.candidates import DedupKey
    from cc_transcript.mining.confidence import Confidence
    from cc_transcript.mining.sourcekind import SourceKind

    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.sync import CachedPrState, PrState

SPLIT_THRESHOLD = 0.9

PROMPT_FINGERPRINT_KEY = "prompt_fingerprint"

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


@dataclass(frozen=True, slots=True)
class PromptVersions:
    """The judge's per-taxonomy prompt versions — one per candidate kind.

    Each version is a content hash of that lane's static prompt template, so
    editing a template is its own version bump: the old verdicts fall out of the
    lane's version, the next pass lazily re-judges it at full fidelity, and
    :meth:`ReviewStore.open` sweeps the orphaned rows the lane no longer runs.

    Attributes:
        create: The prompt version for CREATE-taxonomy durable-correction rows.
        fix: The prompt version for FIX-taxonomy ``hook_complaint`` rows.
    """

    create: int
    fix: int

    def of(self, kind: CandidateKind) -> int:
        """Returns the prompt version bound to a candidate ``kind``."""
        match kind:
            case CandidateKind.CREATE:
                return self.create
            case CandidateKind.FIX:
                return self.fix

    def for_row(self, row: Mapping[str, object]) -> int:
        """Returns the prompt version for a feedback row, keyed by its ``source_kind``."""
        return self.fix if str(row["source_kind"]) == HOOK_COMPLAINT else self.create


def prompt_version(template: str) -> int:
    return int(sha256(template.encode()).hexdigest()[:8], 16)


PROMPT_VERSIONS = PromptVersions(create=prompt_version(CREATE_TEMPLATE), fix=prompt_version(FIX_TEMPLATE))


class CandidateStatus(StrEnum):
    """A candidate's lifecycle state; ``REJECTED`` is terminal and ``ACCEPTED`` reopens only on recurrence."""

    WATCHING = "watching"
    PR_OPEN = "pr_open"
    STALE = "stale"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


TRANSITIONS: Mapping[CandidateStatus, frozenset[CandidateStatus]] = {
    CandidateStatus.WATCHING: frozenset({CandidateStatus.PR_OPEN, CandidateStatus.REJECTED}),
    CandidateStatus.PR_OPEN: frozenset({CandidateStatus.STALE, CandidateStatus.ACCEPTED, CandidateStatus.REJECTED}),
    CandidateStatus.STALE: frozenset({CandidateStatus.ACCEPTED, CandidateStatus.REJECTED}),
    CandidateStatus.ACCEPTED: frozenset({CandidateStatus.WATCHING}),
    CandidateStatus.REJECTED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class ColumnMigration:
    column: str
    ddl: str
    backfill: str | None = None


CANDIDATE_MIGRATIONS: tuple[ColumnMigration, ...] = (
    ColumnMigration("generation", "generation INTEGER NOT NULL DEFAULT 1"),
    ColumnMigration(
        "resolved_at", "resolved_at TEXT", "UPDATE candidates SET resolved_at = updated_at WHERE status = 'accepted'"
    ),
    ColumnMigration("origin_repo_key", "origin_repo_key TEXT"),
    ColumnMigration("pack_name", "pack_name TEXT"),
    ColumnMigration(
        "announced_status",
        "announced_status TEXT",
        "UPDATE candidates SET announced_status = status WHERE status NOT IN ('watching', 'pr_open')",
    ),
)

FEEDBACK_MIGRATIONS: tuple[ColumnMigration, ...] = (ColumnMigration("triage", "triage TEXT"),)

TRIAGE_JUNK = "junk"
TRIAGE_KEEP = "keep"


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
        watching: Whether the repo gating eligibility is watched — the origin repo
            the misfire fired in for a pack fix, else the candidate's own repo.
        sessions: How many distinct sessions carry a judge-accepted observation.
        days: How many distinct UTC days carry a judge-accepted observation.
        open_prs: How many live, non-stale PRs target the candidate's repo.
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
        pending: How many judge-worthy corrections still lack a verdict at their
            lane's bound version.
        last_verdict_at: The newest judge verdict's timestamp across both lanes'
            bound versions, or ``None`` before any verdict lands.
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
        ...     await store.eligible(candidate_id, settings=settings)
    """

    def __init__(self, store: FileStateStore, versions: PromptVersions) -> None:
        super().__init__(store)
        self.versions = versions

    @classmethod
    async def open(
        cls, path: Path, *, versions: PromptVersions = PROMPT_VERSIONS, busy_timeout_ms: int | None = None
    ) -> Self:
        """Opens the review database at ``path`` under ``versions``, self-healing stale verdicts.

        Creates the schema if needed, then sweeps any verdict rows recorded at a
        version their lane no longer runs — the single purge codepath, so
        ``list``/``show``/``status``/``threshold-check`` never count orphans.

        Args:
            path: The database file path.
            versions: The per-lane prompt versions gating verdict freshness.
            busy_timeout_ms: When set, the connection's ``busy_timeout`` is lowered to
                this before the migration and first-upgrade purge run, so those writes
                fail fast (``SQLITE_BUSY``) under a concurrent lock instead of stalling on
                SQLite's default five seconds. The SessionStart announcer passes ``0``;
                the normal reviewer path leaves the default.
        """
        store = cls(
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
CREATE TABLE IF NOT EXISTS review_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pr_states (
  pr_url TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  merged_at TEXT,
  fetched_at TEXT NOT NULL
);
""",
            ),
            versions,
        )
        if busy_timeout_ms is not None:
            await store.store.conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        await store.migrate_columns("candidates", CANDIDATE_MIGRATIONS)
        await store.migrate_columns("feedback_events", FEEDBACK_MIGRATIONS)
        await store.purge_stale_verdicts_if_changed()
        return store

    async def migrate_columns(self, table: str, migrations: tuple[ColumnMigration, ...]) -> None:
        """Adds this version's ``table`` columns to an older database, backfilling each once.

        The guarded-ALTER migration, run on :meth:`open` before
        :meth:`purge_stale_verdicts` over both the ``candidates`` table
        (:data:`CANDIDATE_MIGRATIONS`) and the dedup-key-keyed ``feedback_events``
        table (:data:`FEEDBACK_MIGRATIONS`): every column in ``migrations`` the
        table lacks is added and its backfill runs exactly once, in the branch that
        just added it, so a database already at this schema is untouched and a fresh
        one takes the columns against an empty table.

        A lock-free pre-check keeps the already-migrated hot path (the SessionStart
        announcer's open) from ever taking the write lock. When any column is
        missing, the re-check, ``ALTER``, and backfill run inside one
        ``BEGIN IMMEDIATE`` transaction so concurrent first opens serialize on the
        committed schema — the loser re-reads it and skips every ``ALTER`` — and an
        interrupted migration rolls back its column and backfill together.
        """

        async def pending(conn: aiosqlite.Connection) -> list[ColumnMigration]:
            cur = await conn.execute(f"PRAGMA table_info({table})")
            existing = {str(row["name"]) async for row in cur}
            return [migration for migration in migrations if migration.column not in existing]

        if not await pending(self.store.conn):
            return
        async with self.store.transaction() as conn:
            for migration in await pending(conn):
                await conn.execute(f"ALTER TABLE {table} ADD COLUMN {migration.ddl}")
                if migration.backfill is not None:
                    await conn.execute(migration.backfill)

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

    async def enroll(self, repo: RepoKey) -> bool:
        await self.store.conn.execute(
            "INSERT INTO repos (repo_key, watching) VALUES (?, 1) ON CONFLICT(repo_key) DO NOTHING", (repo,)
        )
        return await self.watching(repo)

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
        origin_repo_key: RepoKey | None = None,
        pack_name: str | None = None,
    ) -> int:
        """Finds or creates the candidate for a grouping key, returning its id.

        New candidates start in :attr:`CandidateStatus.WATCHING`; an existing
        candidate with the same grouping key is returned untouched, so the
        provenance a pack-hook candidate takes is the one from its first misfire.

        Args:
            repo: The candidate's repo — the pack's home repo for a pack hook, so its
                fix PR opens there rather than in the watched repo.
            kind: The candidate's PR shape.
            rule: The slug grouping equivalent corrections.
            source_kind: The detector that produced the first evidence.
            target_source_file: The misfiring hook's file (fix candidates only).
            target_hook_name: The misfiring hook's registered name (fix candidates only).
            misfire_class: The misfire taxonomy label, when classified (fix candidates only).
            origin_repo_key: The watched repo the misfire was observed in, when it differs
                from ``repo`` (a pack hook); ``None`` for a hook living in ``repo`` itself.
            pack_name: The pack the hook belongs to (pack fix candidates only).

        Returns:
            The candidate's id.
        """
        stamp = now()
        await self.store.conn.execute(
            """
INSERT INTO candidates (
  repo_key, candidate_kind, rule, source_kind, status,
  target_source_file, target_hook_name, misfire_class, origin_repo_key, pack_name, created_at, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
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
                origin_repo_key,
                pack_name,
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

    async def untriaged_create_events(self, *, limit: int) -> list[dict[str, object]]:
        """Returns un-triaged create feedback events still evidencing a watching create candidate.

        The rows one junk-triage pass classifies, oldest first, capped at ``limit``: a
        create-kind feedback event whose ``triage`` is unset and which still evidences a
        watching create candidate the judge would otherwise spend a call on. Fix
        (``hook_complaint``) events are never triaged, and an event whose candidates all
        left ``watching`` is skipped — its verdict no longer gates a PR. An event the
        judge already verdicted at the create lane's version is skipped too: the judge
        is the authority once it has ruled, so a later junk retry must not overturn a
        reparented acceptance.
        """
        cur = await self.store.conn.execute(
            f"""
SELECT e.dedup_key, e.text FROM feedback_events e
WHERE e.triage IS NULL AND e.source_kind != ?
  AND EXISTS (
    SELECT 1 FROM candidate_observations o JOIN candidates c ON c.id = o.candidate_id
    WHERE o.dedup_key = e.dedup_key AND c.candidate_kind = ? AND c.status = ?
  )
  AND NOT EXISTS (
    SELECT 1 FROM {self.VERDICT_TABLE} v
    WHERE v.dedup_key = e.dedup_key AND v.role = 'judge' AND v.prompt_version = ?
  )
ORDER BY e.id LIMIT ?
""",
            (HOOK_COMPLAINT, CandidateKind.CREATE, CandidateStatus.WATCHING, self.versions.create, limit),
        )
        return [dict(row) async for row in cur]

    async def record_triage(self, dedup_key: DedupKey, *, junk: bool) -> bool:
        """Stamps one feedback event's junk-triage verdict, keyed by dedup key.

        The single triage-write codepath, a compare-and-set against the still-untriaged
        row: a ``junk`` verdict marks the event so the judge queue skips it and
        :meth:`reject_junk_triaged` can retire its candidate without an LLM call; a keep
        verdict marks it triaged so it is not re-triaged, and leaves it for the judge as
        the backstop. The ``triage IS NULL`` guard makes the first concurrent pass win —
        a losing pass cannot overwrite the verdict a peer already committed, so a keep and
        a junk from two detached reviewers can never leave torn state.

        Returns:
            Whether this call claimed the row; ``False`` when a concurrent pass wrote first.
        """
        cur = await self.store.conn.execute(
            "UPDATE feedback_events SET triage = ? WHERE dedup_key = ? AND triage IS NULL",
            (TRIAGE_JUNK if junk else TRIAGE_KEEP, dedup_key),
        )
        return cur.rowcount == 1

    async def reject_junk_triaged(self) -> int:
        """Rejects every watching create candidate all of whose evidence junk-triaged.

        Run once at the close of a triage pass, mirroring :meth:`regroup_create`'s
        retire step but keyed on the deterministic triage verdict rather than a judge
        verdict: a watching create candidate with at least one observation and no
        observation left un-triaged or kept is retired to
        :attr:`CandidateStatus.REJECTED` without ever reaching the judge. A candidate
        holding one kept observation stays watching for the judge, and one already
        carrying accepted judge evidence at the create lane's version is spared entirely
        — a concurrent judge acceptance outranks a junk retry, so the terminal rejection
        can never orphan a reparented acceptance.

        Returns:
            The number of candidates rejected.
        """
        async with self.store.transaction() as conn:
            reject = [
                int(row["id"])
                async for row in await conn.execute(
                    f"""
SELECT c.id FROM candidates c
WHERE c.candidate_kind = ? AND c.status = ?
  AND EXISTS (SELECT 1 FROM candidate_observations o WHERE o.candidate_id = c.id)
  AND NOT EXISTS (
    SELECT 1 FROM candidate_observations o JOIN feedback_events e ON e.dedup_key = o.dedup_key
    WHERE o.candidate_id = c.id AND (e.triage IS NULL OR e.triage != ?)
  )
  AND NOT EXISTS (
    SELECT 1 FROM candidate_observations o
    JOIN {self.VERDICT_TABLE} v ON v.dedup_key = o.dedup_key
    WHERE o.candidate_id = c.id AND v.role = 'judge' AND v.prompt_version = ? AND v.{self.ACCEPTED_COLUMN} = 1
  )
""",
                    (CandidateKind.CREATE, CandidateStatus.WATCHING, TRIAGE_JUNK, self.versions.create),
                )
            ]
            for candidate_id in reject:
                await self.transition(candidate_id, CandidateStatus.REJECTED)
        return len(reject)

    async def revive_junk_rejected(self) -> int:
        """Reinstates a junk-rejected create candidate the judge has since accepted — judge wins.

        Closes the triage/judge race: a triage pass can mark a create event junk and
        terminally reject its candidate (:meth:`reject_junk_triaged`) while a concurrent
        judge pass — whose queue predates the junk mark — is still ruling on that same
        event. When the judge then persists an accepted verdict, the candidate is stranded
        ``rejected`` and :meth:`regroup_create` (watching-only) can never surface it. Run
        at the close of each judge pass before :meth:`regroup_create`, this returns to
        :attr:`CandidateStatus.WATCHING` every create candidate that is ``rejected``, whose
        every observation is junk-triaged (the junk-rejection signature), and which now
        carries a judge acceptance at the create lane's bound version — so the judge
        outranks the deterministic triage screen and its accepted evidence counts again.
        The all-junk-plus-acceptance signature is unique to this race: the judge queue
        skips junk keys, so a non-raced candidate never holds both.

        This is the sole path that moves ``rejected -> watching``, gated to this
        provenance; :data:`TRANSITIONS` keeps ``rejected`` terminal for every other caller,
        so the flip writes the status directly rather than through :meth:`transition`.

        Returns:
            The number of candidates reinstated to watching.
        """
        async with self.store.transaction() as conn:
            revive = [
                int(row["id"])
                async for row in await conn.execute(
                    f"""
SELECT c.id FROM candidates c
WHERE c.candidate_kind = 'create' AND c.status = ?
  AND EXISTS (SELECT 1 FROM candidate_observations o WHERE o.candidate_id = c.id)
  AND NOT EXISTS (
    SELECT 1 FROM candidate_observations o JOIN feedback_events e ON e.dedup_key = o.dedup_key
    WHERE o.candidate_id = c.id AND (e.triage IS NULL OR e.triage != ?)
  )
  AND EXISTS (
    SELECT 1 FROM candidate_observations o
    JOIN {self.VERDICT_TABLE} v ON v.dedup_key = o.dedup_key
    WHERE o.candidate_id = c.id AND v.role = 'judge' AND v.prompt_version = ? AND v.{self.ACCEPTED_COLUMN} = 1
  )
""",
                    (CandidateStatus.REJECTED, TRIAGE_JUNK, self.versions.create),
                )
            ]
            for candidate_id in revive:
                await conn.execute(
                    "UPDATE candidates SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                    (CandidateStatus.WATCHING, now(), candidate_id, CandidateStatus.REJECTED),
                )
        return len(revive)

    async def junk_triaged_keys(self) -> set[str]:
        """Returns the dedup keys of every junk-triaged feedback event — the judge queue's skip set."""
        cur = await self.store.conn.execute("SELECT dedup_key FROM feedback_events WHERE triage = ?", (TRIAGE_JUNK,))
        return {str(row["dedup_key"]) async for row in cur}

    async def candidates(
        self, repo: RepoKey | None = None, *, status: CandidateStatus | None = None
    ) -> list[dict[str, object]]:
        """Returns candidate rows, newest first, optionally narrowed by repo and status.

        Each row carries the ``candidates`` columns plus ``sample_text`` (the
        earliest observation's verbatim correction) and ``observations`` (the
        evidence count). The repo filter matches a candidate whose PR targets ``repo``
        *or* whose misfire was observed there (``origin_repo_key``), so a pack fix routed
        to the pack's repo stays visible from the watched repo it fired in.

        Args:
            repo: When set, restrict to candidates targeting or originating in this repo.
            status: When set, restrict to this status.
        """
        filters = [
            (clause, values)
            for clause, values in (
                ("(c.repo_key = ? OR c.origin_repo_key = ?)", (repo, repo) if repo is not None else None),
                ("c.status = ?", (status,) if status is not None else None),
            )
            if values is not None
        ]
        query = (
            CANDIDATES_QUERY
            + (f"WHERE {' AND '.join(clause for clause, _ in filters)}\n" if filters else "")
            + "ORDER BY c.id DESC"
        )
        cur = await self.store.conn.execute(query, tuple(value for _, values in filters for value in values))
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

    async def mark_announced(self, candidate_id: int, status: CandidateStatus) -> None:
        """Stamps a candidate's ``announced_status``, so its PR outcome is surfaced at most once per change.

        The single write path for the SessionStart announcer: after a candidate's
        status is announced, its ``announced_status`` catches up to ``status`` and the
        next session start stays silent until the PR outcome changes again.
        """
        await self.store.conn.execute("UPDATE candidates SET announced_status = ? WHERE id = ?", (status, candidate_id))

    async def transition(
        self,
        candidate_id: int,
        to: CandidateStatus,
        *,
        pr_url: str | None = None,
        pr_opened_at: datetime | None = None,
        resolved_at: datetime | None = None,
        expected_pr_url: str | None = None,
        expected_generation: int | None = None,
    ) -> bool:
        """Moves a candidate along :data:`TRANSITIONS` — the only status-write codepath.

        The move is a compare-and-swap against the status it validated: the ``UPDATE``
        matches ``id`` *and* the read status, so a concurrent writer that already moved
        the row cannot be overwritten. On a lost CAS the current status is re-read: when
        it already equals the requested target — two passes ran the *same* transition and
        a peer committed first — the move converges as a no-op and returns; otherwise it
        re-validates and raises on an illegal move. A direct call whose current status
        already equals ``to`` (no lost race) stays invalid, since a self-loop is never in
        :data:`TRANSITIONS`.

        ``expected_pr_url``/``expected_generation`` are the sync path's anti-ABA guard:
        when set, the CAS additionally matches the snapshotted ``pr_url`` and
        ``generation``, so a delayed PR result cannot resolve a candidate that was
        meanwhile accepted, reopened (``generation`` bumped), and re-PR'd — the guard
        finds the row changed and the call no-ops (returns ``False``, the candidate stays
        untouched) instead of stamping the new generation from the old PR's outcome.

        Entering :attr:`CandidateStatus.ACCEPTED` stamps ``resolved_at``, the
        watermark a later reopen counts fresh recurrences against — from the
        authoritative merge timestamp when the caller supplies one (a synced MERGED
        PR passes GitHub's ``merged_at``), else the current time. Every other move
        (the ``accepted -> watching`` reopen included) leaves it untouched, and
        ``updated_at`` always records the wall-clock move time.

        Args:
            candidate_id: The candidate to move.
            to: The target status.
            pr_url: When set, stamped onto the candidate (the PR-opening move).
            pr_opened_at: When set, stamped in UTC alongside ``pr_url``.
            resolved_at: The authoritative resolution time for an ``accepted`` move
                (GitHub's merge time); defaults to now when unset. Ignored otherwise.
            expected_pr_url: The sync path's snapshotted ``pr_url``; the CAS guards on it.
            expected_generation: The sync path's snapshotted ``generation``; the CAS
                guards on it. Passing it arms the anti-ABA guard.

        Returns:
            Whether the move applied — ``True`` when this call (or a converged peer)
            reached the target, ``False`` when the anti-ABA guard found the row changed.

        Raises:
            InvalidTransition: If the move is not allowed from the current status.
            LookupError: If no candidate carries ``candidate_id``.
        """
        guarded = expected_generation is not None
        cas_failed = False
        while True:
            cur = await self.store.conn.execute(
                "SELECT status, pr_url, generation FROM candidates WHERE id = ?", (candidate_id,)
            )
            if not (rows := [dict(row) async for row in cur]):
                raise LookupError(f"no candidate with id {candidate_id}")
            current = CandidateStatus(str(rows[0]["status"]))
            if guarded and (rows[0]["pr_url"] != expected_pr_url or int(rows[0]["generation"]) != expected_generation):
                return False
            if cas_failed and current == to:
                return True
            if to not in TRANSITIONS[current]:
                raise InvalidTransition(f"{current} -> {to}")
            stamp = now()
            resolution = (
                (resolved_at.astimezone(UTC).isoformat() if resolved_at is not None else stamp)
                if to == CandidateStatus.ACCEPTED
                else None
            )
            cur = await self.store.conn.execute(
                "UPDATE candidates SET status = ?, updated_at = ?, "
                "pr_url = COALESCE(?, pr_url), pr_opened_at = COALESCE(?, pr_opened_at), "
                "resolved_at = COALESCE(?, resolved_at) WHERE id = ? AND status = ?"
                + (" AND pr_url IS ? AND generation = ?" if guarded else ""),
                (
                    to,
                    stamp,
                    pr_url,
                    pr_opened_at.astimezone(UTC).isoformat() if pr_opened_at else None,
                    resolution,
                    candidate_id,
                    current,
                    *((expected_pr_url, expected_generation) if guarded else ()),
                ),
            )
            if cur.rowcount == 1:
                return True
            cas_failed = True

    async def pr_state_cache(self, url: str) -> CachedPrState | None:
        """Returns the last-fetched GitHub state for ``url``, with its fetch time — or ``None`` if uncached.

        The single read of the ``pr_states`` TTL cache: :func:`sync_open_prs` uses it
        to skip a fresh ``gh`` call when the entry is young enough. A stale entry is
        never folded into a lifecycle transition — when ``gh`` is down on a forced or
        expired refresh the PR is treated as unreachable and left ``pr_open``.
        """
        from captain_hook.review.sync import CachedPrState, PrState

        cur = await self.store.conn.execute(
            "SELECT state, merged_at, fetched_at FROM pr_states WHERE pr_url = ?", (url,)
        )
        if not (rows := [dict(row) async for row in cur]):
            return None
        return CachedPrState(
            PrState(
                state=str(rows[0]["state"]),
                merged_at=str(m) if (m := rows[0]["merged_at"]) is not None else None,
            ),
            datetime.fromisoformat(str(rows[0]["fetched_at"])),
        )

    async def cache_pr_state(self, url: str, pr: PrState) -> None:
        """Records ``url``'s freshly-fetched GitHub state — the only ``pr_states`` write."""
        await self.store.conn.execute(
            "INSERT INTO pr_states (pr_url, state, merged_at, fetched_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(pr_url) DO UPDATE SET state = excluded.state, "
            "merged_at = excluded.merged_at, fetched_at = excluded.fetched_at",
            (url, pr.state, pr.merged_at, now()),
        )

    async def regroup_create(self) -> tuple[int, int]:
        """Re-parents, retires, and sweeps watching create candidates onto their durable slugs.

        The treadmill that turns the scanner's per-session digest candidates into
        durable rule candidates, run once at the close of each judge pass under a
        single transaction. In order:

        1. **Re-parent** every observation on a watching create candidate whose
           judge verdict at the create lane's bound version is accepted and names
           a ``canonical_key`` different from the candidate's rule, onto the create
           candidate keyed by that slug — created on first need, taking the
           observation's source kind — earliest observation first so the slug
           candidate keeps earliest-evidence order.
        2. **Retire** every watching create candidate all of whose observations
           are judged with none accepted to :attr:`CandidateStatus.REJECTED`.
        3. **Sweep** every watching create candidate left with no observations
           whose ``updated_at`` predates this pass, deleting it; a candidate a
           concurrent ingest just created is newer and survives.

        Fix candidates and terminal or PR-open create candidates are never touched.

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
                    (self.versions.create, CandidateStatus.WATCHING),
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
                    (CandidateStatus.WATCHING, self.versions.create),
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

    async def reopen_recurrent_fixes(self) -> int:
        """Reopens accepted fix candidates whose merged fix still misfires — the recurrence treadmill.

        Run once at the close of each judge pass beside :meth:`regroup_create`. An
        accepted fix candidate carries a ``resolved_at`` stamp from the move that
        accepted it; a judge-accepted ``hook_complaint`` observation at the fix
        lane's bound version whose ``occurred_at`` is after that stamp means the
        shipped fix was insufficient, so the candidate returns to
        :attr:`CandidateStatus.WATCHING` with its ``generation`` bumped, keeping the
        prior ``pr_url`` (the insufficient fix) until a new PR overwrites it. Create
        candidates are never touched — a create recurrence arrives as a fresh
        ``hook_complaint``, not a reopen.

        Returns:
            The number of fix candidates reopened.
        """
        async with self.store.transaction() as conn:
            reopen = [
                int(row["id"])
                async for row in await conn.execute(
                    f"""
SELECT c.id FROM candidates c
WHERE c.candidate_kind = 'fix' AND c.status = ? AND c.resolved_at IS NOT NULL
  AND EXISTS (
    SELECT 1 FROM candidate_observations o
    JOIN {self.VERDICT_TABLE} v ON v.dedup_key = o.dedup_key AND v.role = 'judge' AND v.prompt_version = ?
    WHERE o.candidate_id = c.id AND v.{self.ACCEPTED_COLUMN} = 1 AND o.occurred_at > c.resolved_at
  )
""",
                    (CandidateStatus.ACCEPTED, self.versions.fix),
                )
            ]
            for candidate_id in reopen:
                await self.transition(candidate_id, CandidateStatus.WATCHING)
                await conn.execute("UPDATE candidates SET generation = generation + 1 WHERE id = ?", (candidate_id,))
        return len(reopen)

    async def open_pr_targets(self, *, settings: ReviewSettings) -> dict[RepoKey, int]:
        cutoff = (datetime.now(UTC) - timedelta(days=settings.stale_after_days)).isoformat()
        cur = await self.store.conn.execute(
            "SELECT repo_key, pr_url FROM candidates WHERE status = ? AND pr_opened_at > ?",
            (CandidateStatus.PR_OPEN, cutoff),
        )
        counts: Counter[RepoKey] = Counter()
        seen: set[str] = set()
        async for row in cur:
            match row["pr_url"]:
                case None:
                    counts[RepoKey(str(row["repo_key"]))] += 1
                case url if (u := str(url)) not in seen:
                    seen.add(u)
                    counts[pr_repo_key(u)] += 1
        return dict(counts)

    async def threshold_status(self, candidate_id: int, *, settings: ReviewSettings) -> ThresholdStatus:
        """Returns the judge-accepted evidence counts behind one candidate's eligibility.

        An observation counts only when its dedup key's latest judge verdict at
        the candidate kind's bound version accepts it with confidence at or above
        ``settings.min_judge_confidence``; unjudged observations count as
        not-yet, so they retry on the next session's pass. A reopened candidate
        (``generation`` past 1) counts only evidence newer than its ``resolved_at``
        watermark, so a single strong-marker recurrence re-qualifies it through the
        ``single_observation`` path without the stale pre-fix evidence.

        Args:
            candidate_id: The candidate to inspect.
            settings: The thresholds and judge knobs to count under.

        Returns:
            The counts the eligibility call (and the CLI's explanation) reads.

        Raises:
            LookupError: If no candidate carries ``candidate_id``.
        """
        conn = self.store.conn
        cur = await conn.execute(
            "SELECT repo_key, origin_repo_key, candidate_kind, status, generation, resolved_at "
            "FROM candidates WHERE id = ?",
            (candidate_id,),
        )
        if not (candidates := [dict(row) async for row in cur]):
            raise LookupError(f"no candidate with id {candidate_id}")
        repo, kind = RepoKey(str(candidates[0]["repo_key"])), CandidateKind(str(candidates[0]["candidate_kind"]))
        status = CandidateStatus(str(candidates[0]["status"]))
        watching_repo = RepoKey(str(origin)) if (origin := candidates[0]["origin_repo_key"]) else repo
        since = candidates[0]["resolved_at"] if int(candidates[0]["generation"]) > 1 else None

        accepted_cur = await conn.execute(
            f"""
SELECT o.session_id, substr(o.occurred_at, 1, 10) AS day, e.payload_json
FROM candidate_observations o
JOIN {self.VERDICT_TABLE} v ON v.dedup_key = o.dedup_key AND v.role = 'judge' AND v.prompt_version = ?
JOIN feedback_events e ON e.dedup_key = o.dedup_key
WHERE o.candidate_id = ? AND v.{self.ACCEPTED_COLUMN} = 1 AND v.confidence >= ?
  AND (? IS NULL OR o.occurred_at > ?)
""",
            (self.versions.of(kind), candidate_id, settings.min_judge_confidence, since, since),
        )
        accepted = [dict(row) async for row in accepted_cur]

        return ThresholdStatus(
            kind=kind,
            status=status,
            watching=await self.watching(watching_repo),
            sessions=len({row["session_id"] for row in accepted}),
            days=len({row["day"] for row in accepted}),
            open_prs=(await self.open_pr_targets(settings=settings)).get(repo, 0),
            single_observation=any(
                signal_confidence(row["payload_json"]) >= settings.min_confidence_fix_single for row in accepted
            ),
        )

    async def eligible(self, candidate_id: int, *, settings: ReviewSettings) -> bool:
        """Returns whether a candidate's judge-accepted evidence crosses its thresholds.

        Delegates to :func:`crosses_thresholds` over the candidate's
        :meth:`threshold_status`, so the dashboard and the reviewer agree on what
        is eligible.

        Args:
            candidate_id: The candidate to check.
            settings: The thresholds and judge knobs to check under.
        """
        return crosses_thresholds(await self.threshold_status(candidate_id, settings=settings), settings=settings)

    async def pr_summary(self, candidate_id: int, *, settings: ReviewSettings) -> str | None:
        """Returns the candidate's most-confident accepted verdict summary — what its PR would do.

        Reads the same judge-accepted observations the thresholds count and
        returns the highest-confidence verdict's one-sentence summary, or ``None``
        while no observation is judged-accepted yet.

        Args:
            candidate_id: The candidate to describe.
            settings: The judge knobs supplying ``min_judge_confidence``.

        Raises:
            LookupError: If no candidate carries ``candidate_id``.
        """
        kind_cur = await self.store.conn.execute(
            "SELECT candidate_kind, generation, resolved_at FROM candidates WHERE id = ?", (candidate_id,)
        )
        if not (rows := [dict(row) async for row in kind_cur]):
            raise LookupError(f"no candidate with id {candidate_id}")
        since = rows[0]["resolved_at"] if int(rows[0]["generation"]) > 1 else None
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
WHERE o.candidate_id = ? AND l.accepted = 1 AND l.confidence >= ? AND (? IS NULL OR o.occurred_at > ?)
ORDER BY l.confidence DESC, o.id DESC
LIMIT 1
""",
            (
                self.versions.of(CandidateKind(str(rows[0]["candidate_kind"]))),
                candidate_id,
                settings.min_judge_confidence,
                since,
                since,
            ),
        )
        return str(summary_rows[0]["summary"]) if (summary_rows := [dict(row) async for row in cur]) else None

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

    async def threshold_statuses(
        self, rows: Sequence[Mapping[str, object]], *, settings: ReviewSettings
    ) -> dict[int, ThresholdStatus]:
        """Returns the :class:`ThresholdStatus` for every candidate in ``rows`` in a fixed number of queries.

        The set-based sibling of :meth:`threshold_status`, built for :meth:`overview`
        so the dashboard reads N candidates without N per-row round trips: one accepted-
        evidence scan over all candidates (lane version by kind, evidence gated past a
        reopened candidate's ``resolved_at``), one ``repos`` read, and one open-PR target
        scan. It computes the exact same fields as :meth:`threshold_status` — a
        parity test pins the two together — so :func:`crosses_thresholds` stays the one
        eligibility predicate over either.
        """
        if not rows:
            return {}
        ids = [int(str(row["id"])) for row in rows]
        placeholders = ",".join("?" * len(ids))
        accepted_cur = await self.store.conn.execute(
            f"""
SELECT o.candidate_id, o.session_id, substr(o.occurred_at, 1, 10) AS day, e.payload_json
FROM candidate_observations o
JOIN candidates c ON c.id = o.candidate_id
JOIN {self.VERDICT_TABLE} v ON v.dedup_key = o.dedup_key AND v.role = 'judge'
  AND v.prompt_version = CASE WHEN c.candidate_kind = ? THEN ? ELSE ? END
JOIN feedback_events e ON e.dedup_key = o.dedup_key
WHERE o.candidate_id IN ({placeholders}) AND v.{self.ACCEPTED_COLUMN} = 1 AND v.confidence >= ?
  AND (c.generation <= 1 OR o.occurred_at > c.resolved_at)
""",
            (CandidateKind.FIX, self.versions.fix, self.versions.create, *ids, settings.min_judge_confidence),
        )
        accepted: dict[int, list[Mapping[str, object]]] = {}
        async for row in accepted_cur:
            accepted.setdefault(int(row["candidate_id"]), []).append(dict(row))

        watching_cur = await self.store.conn.execute("SELECT repo_key, watching FROM repos")
        watching = {str(row["repo_key"]): bool(row["watching"]) async for row in watching_cur}

        open_prs = await self.open_pr_targets(settings=settings)

        def status_for(row: Mapping[str, object]) -> ThresholdStatus:
            obs = accepted.get(int(str(row["id"])), [])
            watching_repo = str(row["origin_repo_key"] or row["repo_key"])
            return ThresholdStatus(
                kind=CandidateKind(str(row["candidate_kind"])),
                status=CandidateStatus(str(row["status"])),
                watching=watching.get(watching_repo, False),
                sessions=len({o["session_id"] for o in obs}),
                days=len({o["day"] for o in obs}),
                open_prs=open_prs.get(RepoKey(str(row["repo_key"])), 0),
                single_observation=any(
                    signal_confidence(o["payload_json"]) >= settings.min_confidence_fix_single for o in obs
                ),
            )

        return {int(str(row["id"])): status_for(row) for row in rows}

    async def pr_summaries(self, rows: Sequence[Mapping[str, object]], *, settings: ReviewSettings) -> dict[int, str]:
        """Returns each candidate's highest-confidence accepted verdict summary in one query.

        The set-based sibling of :meth:`pr_summary` for :meth:`overview`: candidates
        with no judge-accepted evidence (post-``resolved_at`` for a reopened one) are
        simply absent from the mapping.
        """
        if not rows:
            return {}
        ids = [int(str(row["id"])) for row in rows]
        placeholders = ",".join("?" * len(ids))
        cur = await self.store.conn.execute(
            f"""
WITH latest AS (
  SELECT v.dedup_key, v.prompt_version, v.{self.ACCEPTED_COLUMN} AS accepted,
    v.{self.SUMMARY_COLUMN} AS summary, v.confidence,
    ROW_NUMBER() OVER (
      PARTITION BY v.dedup_key, v.prompt_version ORDER BY v.judged_at DESC, v.id DESC
    ) AS rn
  FROM {self.VERDICT_TABLE} v WHERE v.role = 'judge'
)
SELECT o.candidate_id, l.summary AS summary,
  ROW_NUMBER() OVER (PARTITION BY o.candidate_id ORDER BY l.confidence DESC, o.id DESC) AS pick
FROM candidate_observations o
JOIN candidates c ON c.id = o.candidate_id
JOIN latest l ON l.dedup_key = o.dedup_key AND l.rn = 1
  AND l.prompt_version = CASE WHEN c.candidate_kind = ? THEN ? ELSE ? END
WHERE o.candidate_id IN ({placeholders}) AND l.accepted = 1 AND l.confidence >= ?
  AND (c.generation <= 1 OR o.occurred_at > c.resolved_at)
""",
            (CandidateKind.FIX, self.versions.fix, self.versions.create, *ids, settings.min_judge_confidence),
        )
        return {int(row["candidate_id"]): str(row["summary"]) async for row in cur if int(row["pick"]) == 1}

    async def overview(self, repo: RepoKey | None = None, *, settings: ReviewSettings) -> list[CandidateView]:
        """Returns a :class:`CandidateView` per candidate — the status dashboard's whole read.

        Assembles each view from the batched :meth:`threshold_statuses` and
        :meth:`pr_summaries` — a fixed number of queries over the whole candidate set
        rather than a per-row fan-out — then gates eligibility through the shared
        :func:`crosses_thresholds`.

        Args:
            repo: When set, restrict to this repo.
            settings: The thresholds and judge knobs to evaluate under.
        """
        rows = await self.candidates(repo)
        statuses = await self.threshold_statuses(rows, settings=settings)
        summaries = await self.pr_summaries(rows, settings=settings)
        return [
            CandidateView(
                row=row,
                threshold=(threshold := statuses[candidate_id := int(str(row["id"]))]),
                eligible=crosses_thresholds(threshold, settings=settings),
                summary=summaries.get(candidate_id),
            )
            for row in rows
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

    async def unwatched_session_repos(self, *, days: int = 7) -> list[str]:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        cur = await self.store.conn.execute(
            """
SELECT DISTINCT json_extract(report_json, '$.repo') AS repo
FROM spawn_runs
WHERE ok = 1 AND started_at > ? AND json_extract(report_json, '$.watching') = 0
  AND repo IS NOT NULL AND repo NOT IN (SELECT repo_key FROM repos)
ORDER BY repo
""",
            (cutoff,),
        )
        return [str(row["repo"]) async for row in cur]

    async def judge_queue(
        self, *, refresh_summary: bool = False, probe_hydration: bool = True
    ) -> list[dict[str, object]]:
        """Returns the rows one judge pass judges, each under its taxonomy's bound version.

        Each lane is fetched at its own version and post-filtered to its
        taxonomy — the create-version call keeps every non-``hook_complaint`` row,
        the fix-version call keeps the ``hook_complaint`` rows — then the two
        concatenate create-then-fix, each call's event order preserved, capped
        per session by the judge pass.

        Args:
            refresh_summary: When True, also re-yields summary-fidelity rows for a
                full-fidelity re-judge once their windows hydrate again.
            probe_hydration: Forwarded to :meth:`unjudged`; the judging path leaves
                it True so a dead-transcript summary row drops, while the display
                backlog count passes False to skip the per-row transcript rglob.
        """
        junk = await self.junk_triaged_keys()
        create_lane = [
            row
            for row in await self.unjudged(
                role="judge",
                prompt_version=self.versions.create,
                refresh_summary=refresh_summary,
                probe_hydration=probe_hydration,
            )
            if str(row["source_kind"]) != HOOK_COMPLAINT and str(row["dedup_key"]) not in junk
        ]
        fix_lane = [
            row
            for row in await self.unjudged(
                role="judge",
                prompt_version=self.versions.fix,
                refresh_summary=refresh_summary,
                probe_hydration=probe_hydration,
            )
            if str(row["source_kind"]) == HOOK_COMPLAINT
        ]
        return create_lane + fix_lane

    async def judge_backlog(self) -> int:
        """Counts judge-worthy corrections still lacking a verdict at their lane's bound version.

        The dashboard's pending count: every :meth:`judge_queue` row (summary-refresh
        included) that clears :func:`judge_worthy` — the same noise-floor predicate
        the judge pass sends rows on, so the count matches what the next pass judges.
        A display-only read, so ``probe_hydration=False`` skips the per-row transcript
        rglob a summary-refresh probe would run — the backlog count over-counts a dead
        transcript by at most one row rather than scanning the projects tree per row.
        """
        return sum(judge_worthy(row) for row in await self.judge_queue(refresh_summary=True, probe_hydration=False))

    async def has_verdict_evidence(self) -> bool:
        """Whether any canonical-key evidence is stored at the create lane's bound version to suggest from.

        Gates the judge pass's per-row slug suggestions: with the companion
        ``verdict_evidence`` table absent or empty at that version, no suggestion
        is possible by construction, so the pass skips the embedder load entirely.
        FIX verdicts never carry a ``canonical_key``, so the evidence store is
        create-lane-only and this reads the create version.
        """
        cur = await self.store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'verdict_evidence'"
        )
        if await cur.fetchone() is None:
            return False
        cur = await self.store.conn.execute(
            "SELECT 1 FROM verdict_evidence WHERE prompt_version = ? LIMIT 1", (self.versions.create,)
        )
        return await cur.fetchone() is not None

    async def slug_splits(self, *, threshold: float = SPLIT_THRESHOLD) -> list[KeyOverlap]:
        """Returns canonical-key pairs whose evidence centroids nearly coincide — possible slug splits.

        Delegates to :func:`cc_transcript.judge.near_duplicate_keys` over this
        store's create-lane verdict-evidence vectors; nothing merges automatically,
        the caller decides what to do with a flagged pair.

        Args:
            threshold: The exclusive cosine-similarity floor a pair must clear.
        """
        from cc_transcript.judge.similar import near_duplicate_keys

        return await near_duplicate_keys(self, prompt_version=self.versions.create, threshold=threshold)

    async def purge_stale_verdicts_if_changed(self) -> int:
        """Runs :meth:`purge_stale_verdicts` only when the prompt fingerprint moved since the last open.

        The gate on the sole purge codepath, run on :meth:`open`: an unchanged
        ``(create, fix)`` version pair means no template edit could have staled a
        verdict since the last sweep, so the purge — and its evidence-vector scan —
        is skipped, keeping a hot store-open (the SessionStart announcer's) cheap. A
        database with no recorded fingerprint (fresh, or first open after this
        upgrade) purges once and records the pair, then stays quiet until a lane's
        template hash bumps.

        Returns:
            The number of verdict rows deleted, or ``0`` when the purge was skipped.
        """
        fingerprint = f"{self.versions.create}:{self.versions.fix}"
        cur = await self.store.conn.execute("SELECT value FROM review_meta WHERE key = ?", (PROMPT_FINGERPRINT_KEY,))
        if (stored := [str(row["value"]) async for row in cur]) and stored[0] == fingerprint:
            return 0
        purged = await self.purge_stale_verdicts()
        await self.store.conn.execute(
            "INSERT INTO review_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (PROMPT_FINGERPRINT_KEY, fingerprint),
        )
        return purged

    async def purge_stale_verdicts(self) -> int:
        """Deletes verdict and evidence rows recorded at a version their lane no longer runs.

        The only verdict-delete codepath, run once on :meth:`open`: an edited
        prompt template moves its lane's hash version, orphaning that lane's
        verdicts, and this sweeps them lane-aware — a ``hook_complaint`` verdict is stale unless
        it carries the fix version, every other verdict is stale unless it carries
        the create version (so with ``create=4 fix=3`` the stale create rows at 3
        die while the live fix rows at 3 survive). Only the judge role's rows are
        swept — the lanes' versions say nothing about any other role's. The
        create-lane evidence vectors follow, since FIX verdicts never carry a
        ``canonical_key``.

        Purging forecloses a retroactive cross-version flip report (cc_transcript's
        eval harness compares two passes' verdicts): export :meth:`judged` for the
        outgoing version before bumping a lane if one is wanted.

        Returns:
            The number of verdict rows deleted.
        """
        from cc_transcript.judge.similar import prepare_evidence_removal

        removable = await prepare_evidence_removal(self.store)
        async with self.store.transaction() as conn:
            purged = (
                await conn.execute(
                    f"""
DELETE FROM {self.VERDICT_TABLE} WHERE id IN (
  SELECT v.id FROM {self.VERDICT_TABLE} v
  JOIN feedback_events e ON e.dedup_key = v.dedup_key
  WHERE v.role = 'judge' AND v.prompt_version != CASE WHEN e.source_kind = ? THEN ? ELSE ? END
)
""",
                    (HOOK_COMPLAINT, self.versions.fix, self.versions.create),
                )
            ).rowcount
            if removable:
                await conn.execute(
                    "DELETE FROM verdict_vectors WHERE vector_id IN "
                    "(SELECT vector_id FROM verdict_evidence WHERE prompt_version != ?)",
                    (self.versions.create,),
                )
                await conn.execute("DELETE FROM verdict_evidence WHERE prompt_version != ?", (self.versions.create,))
        return purged

    async def judge_health(self) -> JudgeHealth:
        """Returns the judge lane's dashboard health at each lane's bound version — the only judge-health read.

        Bundles the backlog count, the newest live verdict's timestamp across both
        lanes — lane-exact, so a bumped lane's stale rows never masquerade as
        recency — and the slug-split signal so the status dashboard reads them in
        one call.
        """
        cur = await self.store.conn.execute(
            f"""
SELECT MAX(v.judged_at) AS last FROM {self.VERDICT_TABLE} v
JOIN feedback_events e ON e.dedup_key = v.dedup_key
WHERE v.role = 'judge' AND v.prompt_version = CASE WHEN e.source_kind = ? THEN ? ELSE ? END
""",
            (HOOK_COMPLAINT, self.versions.fix, self.versions.create),
        )
        last = [row["last"] async for row in cur][0]
        return JudgeHealth(
            pending=await self.judge_backlog(),
            last_verdict_at=str(last) if last is not None else None,
            splits=tuple(await self.slug_splits()),
        )
