"""The two-stage SessionEnd detach: a guard-only hook path and the detached reviewer child.

``review run`` is wired to the SessionEnd hook event and must exit 0 on every
path — a non-zero or hanging hook wedges the user's session — so
:func:`guard_and_spawn` does nothing but parse, guard, and detach
``capt-hook review spawn`` via ``Popen(start_new_session=True)``; no scanning,
no ``gh``, and no heavy imports happen on that path, and the only database
touch is native dispatch's fail-fast, fail-open enrollment probe. The
detached child (:func:`spawn_session`, wrapping :func:`review_session`) does
the real work: resolve the repo, check it is watched, scan the transcript
incrementally, run the judge pass, sync open PR states, and — when at least
one candidate crosses its thresholds — spawn the headless brain that drafts
the PRs, recording each run's outcome for the status dashboard's health line.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, TypeGuard

from captain_hook.review.repo import resolve_repo_key
from captain_hook.settings import resolve_state_dir
from captain_hook.types import Event
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from collections.abc import Iterator

    from spawnllm import TModel

    from captain_hook.review.repo import RepoKey
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

SPAWNED_ENV = "CAPT_HOOK_SPAWNED"
BRAIN_TIER: TModel = "medium"
BRAIN_ALLOWED_TOOLS = ("Read", "Grep", "Glob", "Write", "Edit", "Bash", "Skill", "Agent")
# The events whose native `run <Event>` dispatch fires the reviewer/sweep.
DISPATCH_EVENTS = frozenset({Event.SessionStart, Event.SessionEnd, Event.Stop})
# Window collapsing a stale plugin's raw `review run` racing native dispatch to one reviewer child.
REVIEW_RUN_DEDUP = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class SpawnReport:
    """The detached child's outcome for one ended session.

    Attributes:
        repo: The session's resolved repo key, or ``None`` outside a git repo.
        watching: Whether the repo is watched (nothing runs when it is not).
        scanned: How many transcripts the incremental scan parsed.
        inserted: How many new feedback events the scan recorded.
        triaged: How many surviving create events the junk pre-screen classified.
        triage_junk: How many of those the pre-screen marked junk.
        triage_rejected: How many candidates the pre-screen retired without a judge call.
        judged: How many rows the judge pass verdicted.
        failed: How many judge calls failed and stay pending.
        eligible: The candidate ids that crossed their PR thresholds.
        brain: Whether the headless brain was spawned.
        brain_exit: The brain subprocess's exit code, or ``None`` when it did not run.
        brain_seconds: The brain's wall-clock runtime in seconds, or ``None`` when it did not run.
        brain_prs: How many candidates the brain moved into ``pr_open`` this pass.
        brain_skips: How many eligible candidates the brain left ``watching`` — a
            logged skip (vanished target, no verifiable evidence), not a failure.
        synced_merged: How many open PRs merged, accepting their candidate.
        synced_closed: How many open PRs closed, rejecting their candidate.
        synced_kept: How many open PRs stayed open (fresh or ``gh``-unreachable).
        sweep: Whether this was a Stop-triggered sweep (no PR sync, no brain).
    """

    repo: RepoKey | None
    watching: bool = False
    scanned: int = 0
    inserted: int = 0
    triaged: int = 0
    triage_junk: int = 0
    triage_rejected: int = 0
    judged: int = 0
    failed: int = 0
    eligible: tuple[int, ...] = ()
    brain: bool = False
    brain_exit: int | None = None
    brain_seconds: float | None = None
    brain_prs: int = 0
    brain_skips: int = 0
    synced_merged: int = 0
    synced_closed: int = 0
    synced_kept: int = 0
    sweep: bool = False


@dataclass(frozen=True, slots=True)
class BrainOutcome:
    """The headless PR-drafting brain's run outcome for one reviewer pass.

    Attributes:
        exit_code: The brain subprocess's exit status; ``-9`` when it overran
            ``brain_deadline_seconds`` and was killed.
        seconds: The brain's wall-clock runtime in seconds.
        log_path: The spawn log the brain's stdout and stderr appended to.
    """

    exit_code: int
    seconds: float
    log_path: Path


def review_log_path() -> Path:
    return resolve_state_dir() / "review" / "spawn.log"


def breadcrumb(reason: str) -> None:
    try:
        (path := review_log_path()).parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as log:
            log.write(f"{datetime.now(UTC).isoformat()} {reason}\n")
    except OSError:
        return


def sweep_dir() -> Path:
    return resolve_state_dir() / "review" / "sweeps"


def sweep_key(cwd: str) -> str:
    return hashlib.sha256(cwd.encode()).hexdigest()[:12]


def spawn_argv(transcript: str, cwd: str | None, *, sweep: bool = False) -> list[str]:
    return [
        sys.executable,
        "-m",
        "captain_hook",
        "review",
        "spawn",
        "--transcript",
        transcript,
        *(("--cwd", cwd) if cwd else ()),
        *(("--sweep",) if sweep else ()),
    ]


def is_payload(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def parse_payload(raw: bytes) -> dict[str, object] | None:
    try:
        payload: object = json.loads(raw)
    except ValueError:
        return None
    return payload if is_payload(payload) else None


def payload_transcript(raw: bytes, *, label: str) -> tuple[str, str | None] | None:
    if (payload := parse_payload(raw)) is None:
        breadcrumb(f"{label} skip: unparseable stdin")
        return None
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str):
        breadcrumb(f"{label} skip: non-string transcript_path")
        return None
    try:
        exists = Path(transcript).is_file()
    except ValueError:
        exists = False
    if not exists:
        breadcrumb(f"{label} skip: missing transcript file")
        return None
    cwd = payload.get("cwd")
    return transcript, cwd if isinstance(cwd, str) else None


def detach(argv: list[str], *, spawned: str) -> None:
    try:
        (log_path := review_log_path()).parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log:
            subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                cwd=reqenv.cwd(),
                env=reqenv.env_map() | {SPAWNED_ENV: "1"},
            )
    except OSError:
        breadcrumb(f"detach failed: {spawned}")
        return
    breadcrumb(spawned)


def payload_event(raw: bytes) -> str:
    """The payload's ``hook_event_name`` (``""`` when absent) — the review-run dedup discriminator."""
    payload = parse_payload(raw)
    name = payload.get("hook_event_name") if payload else None
    return name if isinstance(name, str) else ""


def enrolled(cwd: str | None) -> bool:
    """Whether the repo at *cwd* is watched — the native-dispatch review gate.

    Applies the detached child's own ``store.enroll`` check (auto-watching a brand-new repo, honoring
    an explicit ``review disable``) so native ``run <Event>`` dispatch skips a non-watched or non-git
    repo before any reviewer child spawns. A non-git *cwd* short-circuits without opening the store.
    The store opens with ``busy_timeout = 0`` so a held writer lock cannot stall the hook, and any
    store failure counts as watched — the detached child re-applies the authoritative gate, so
    uncertainty costs one short-lived child instead of a dropped review.
    """
    if (repo := resolve_repo_key(cwd or str(reqenv.cwd()))) is None:
        return False
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

    try:
        with ReviewStore.open(ReviewSettings().db_path, busy_timeout_ms=0) as store:
            return store.enroll(repo)
    except Exception:
        breadcrumb("review gate uncertain: enroll check failed — deferring to the spawned child")
        return True


def _claim_stamp(stamp: Path, window: timedelta) -> bool:
    """Atomically claim *stamp*; ``True`` when this caller won and may proceed.

    ``O_CREAT|O_EXCL`` decides the absent-stamp race, a stamp fresher than *window* loses, and a
    stale stamp is unlinked then re-claimed with ``O_EXCL`` — so concurrent callers (native dispatch
    racing a stale plugin's raw entry) resolve to exactly one winner.
    """
    for _ in range(2):
        try:
            os.close(os.open(stamp, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            try:
                age = datetime.now(UTC) - datetime.fromtimestamp(stamp.stat().st_mtime, tz=UTC)
            except FileNotFoundError:
                continue
            if age < window:
                return False
            try:
                stamp.unlink()
            except FileNotFoundError:
                pass
            continue
        return True
    return False


def claim_review_run(cwd: str | None, event_name: str) -> bool:
    """Claim the short review-run dedup stamp for (*cwd*, *event_name*); ``True`` if the caller may spawn.

    Keyed like :func:`guard_and_sweep`'s throttle but per (cwd, event) and over :data:`REVIEW_RUN_DEDUP`,
    it collapses the version-skew double-fire and any concurrent same-repo passes (the whole-directory
    scan already covers those) via the atomic :func:`_claim_stamp`. Fails open on ``OSError`` — a broken
    state dir loses the reviewer's log either way, so prefer running over silently dropping the review.
    """
    key = hashlib.sha256(f"{cwd or ''}\0{event_name}".encode()).hexdigest()[:12]
    try:
        (stamps := sweep_dir()).mkdir(parents=True, exist_ok=True)
        return _claim_stamp(stamps / f"{key}.run", REVIEW_RUN_DEDUP)
    except OSError:
        return True


def guard_and_spawn(raw: bytes, *, gate_enrollment: bool = False) -> None:
    """Parses the SessionStart/SessionEnd hook payload and detaches the reviewer child.

    Every path returns normally so the wired hook always exits 0: a set
    ``CAPT_HOOK_SPAWNED`` (the reviewer's own spawned sessions), a headless
    ``claude -p`` / SDK session (``CLAUDE_CODE_ENTRYPOINT`` in the ``sdk-*``
    family; an interactive quit is ``cli``), malformed stdin, a missing
    transcript, a throttled duplicate, and a failed spawn all fall through
    silently, each leaving a breadcrumb line on :func:`review_log_path`. The
    child runs with ``CAPT_HOOK_SPAWNED=1`` and its output appended to the same
    log.

    The :func:`claim_review_run` throttle runs on every caller so native ``run
    <Event>`` dispatch and a stale plugin's raw ``review run`` entry collapse to
    one spawn. ``gate_enrollment`` (set only by native dispatch) additionally
    skips a non-watched repo — checked *before* the claim, so a gated skip never
    burns the stamp the raw fallback needs; the raw CLI entry leaves it off, so
    the detached child stays the authoritative enrollment gate for that path.

    Args:
        raw: The hook's stdin bytes, holding the SessionStart/SessionEnd JSON payload.
        gate_enrollment: Skip the spawn for a non-watched repo (native dispatch only).
    """
    if reqenv.getenv(SPAWNED_ENV):
        breadcrumb("review-run skip: CAPT_HOOK_SPAWNED set")
        return
    # headless `claude -p` / SDK run — not an interactive session
    if reqenv.getenv("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk"):
        breadcrumb("review-run skip: sdk entrypoint")
        return
    if (parsed := payload_transcript(raw, label="review-run")) is None:
        return
    transcript, cwd = parsed
    if gate_enrollment and not enrolled(cwd):
        breadcrumb("review-run skip: not watching")
        return
    if not claim_review_run(cwd, payload_event(raw)):
        breadcrumb("review-run skip: throttled")
        return
    detach(spawn_argv(transcript, cwd), spawned=f"spawned {transcript}")


def guard_and_sweep(raw: bytes, *, gate_enrollment: bool = False) -> None:
    """Parses the Stop hook payload and detaches a throttled repo-wide reviewer sweep.

    Mirrors :func:`guard_and_spawn`'s skips (each breadcrumbed) and detach, plus a
    file-based, database-free throttle: every invocation that passes the skips
    touches ``<key>.trigger`` under :func:`sweep_dir`, keyed by the payload cwd's
    sha256, and a sweep spawns only when the matching ``<key>.sweep`` stamp is
    older than ``sweep_interval_minutes`` (or absent). The stamp is claimed
    atomically (:func:`_claim_stamp`) just before the detach, so a burst of Stop
    events yields at most one sweep per interval, and native ``run Stop`` dispatch
    collapses with a stale plugin's raw ``review sweep`` entry. The child runs
    ``review spawn --sweep``: scan, triage, and judge, but never PR sync or the
    working-copy-editing brain.

    ``gate_enrollment`` (set only by native dispatch) skips a non-watched repo,
    checked *after* the read-only throttle peek (keeping the enrollment read off
    the hot Stop path) but *before* the claim, so a gated skip never burns the
    stamp the raw fallback needs — the raw CLI entry leaves it off and the child
    gates that path instead.

    Args:
        raw: The hook's stdin bytes, holding the Stop JSON payload.
        gate_enrollment: Skip the spawn for a non-watched repo (native dispatch only).
    """
    from captain_hook.review.settings import ReviewSettings

    if reqenv.getenv(SPAWNED_ENV):
        breadcrumb("sweep skip: CAPT_HOOK_SPAWNED set")
        return
    if reqenv.getenv("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk"):
        breadcrumb("sweep skip: sdk entrypoint")
        return
    if (parsed := payload_transcript(raw, label="sweep")) is None:
        return
    transcript, cwd = parsed
    interval = timedelta(minutes=ReviewSettings().sweep_interval_minutes)
    key = sweep_key(cwd or "")
    try:
        (stamps := sweep_dir()).mkdir(parents=True, exist_ok=True)
        (stamps / f"{key}.trigger").touch()
        stamp = stamps / f"{key}.sweep"
        if stamp.exists() and datetime.now(UTC) - datetime.fromtimestamp(stamp.stat().st_mtime, tz=UTC) < interval:
            breadcrumb("sweep skip: throttled")
            return
    except OSError:
        breadcrumb("sweep skip: stamp OSError")
        return
    if gate_enrollment and not enrolled(cwd):
        breadcrumb("sweep skip: not watching")
        return
    try:
        if not _claim_stamp(stamp, interval):
            breadcrumb("sweep skip: throttled")
            return
    except OSError:
        breadcrumb("sweep skip: stamp OSError")
        return
    detach(spawn_argv(transcript, cwd, sweep=True), spawned=f"sweep spawned {transcript}")


def dispatch_review(event_name: str, payload: dict[str, object]) -> None:
    """Native ``run <Event>`` entry: fire the enrollment-gated reviewer or throttled sweep.

    The counterpart to the retired raw ``review run``/``review sweep`` hooks.json entries — called from
    the async dispatch of a :data:`DISPATCH_EVENTS` member, on both the cold CLI and the daemon. Routes
    to the same guard-and-detach the CLI entry points use, with the enrollment gate switched on so a
    non-watched repo never spawns and a stale plugin's raw entry collapses through the shared throttle.
    """
    raw = json.dumps(payload).encode()
    if event_name == "Stop":
        guard_and_sweep(raw, gate_enrollment=True)
    else:
        guard_and_spawn(raw, gate_enrollment=True)


def brain_prompt(transcript: Path) -> str:
    from captain_hook.review.scan import REVIEWER_MARKER

    return (
        f"/captain-hook:scanning-sessions --transcript {transcript}\n\n"
        f"[{REVIEWER_MARKER}] Review this repo's eligible candidates and open at most one pull request per"
        " candidate. Work in one continuous run: do not stop to summarize after drafting — you are done only"
        " when every eligible candidate has a PR recorded via `review update <id> pr_open --pr-url <url>` or"
        " is explicitly skipped with a reason."
    )


def brain_argv(*, max_turns: int, max_budget_usd: float) -> list[str]:
    from spawnllm import ClaudeCliBackend, ClaudeConfig, RunSpec

    from captain_hook.cli import plugin_dir

    backend = ClaudeCliBackend()
    spec = RunSpec(
        prompt="",
        model=backend.models[BRAIN_TIER],
        agent=True,
        provider_configs={"claude": ClaudeConfig(permission_mode="acceptEdits", max_budget_usd=max_budget_usd)},
    )
    return [
        *backend.build_command(spec),
        "--plugin-dir",
        str(plugin_dir()),
        "--max-turns",
        str(max_turns),
        "--allowedTools",
        ",".join(BRAIN_ALLOWED_TOOLS),
    ]


def spawn_brain(transcript: Path, *, repo_root: Path, settings: ReviewSettings) -> BrainOutcome:
    """Runs the headless PR-drafting brain over the repo's eligible candidates.

    The argv comes from the spawnllm Claude backend with the permission mode
    forced to ``acceptEdits``, the tool scope narrowed to
    :data:`BRAIN_ALLOWED_TOOLS`, and the turn budget capped by
    ``settings.brain_max_turns``. The subprocess is killed at
    ``settings.brain_deadline_seconds`` — the deadline for the surrounding pass
    cannot interrupt this blocking call, so the brain carries its own wall-clock
    bound. The prompt carries the reviewer marker so the brain's own SessionEnd
    self-skips, and ``CAPT_HOOK_SPAWNED=1`` keeps its SessionEnd hook from
    re-spawning the reviewer.

    Args:
        transcript: The just-ended session's transcript, named in the prompt.
        repo_root: The repo the brain works in.
        settings: The reviewer settings supplying the turn budget.

    Returns:
        The :class:`BrainOutcome` — exit code, runtime, and log path — the
        status dashboard's health line reads to surface a silently failing brain.
    """
    (log_path := review_log_path()).parent.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC)
    with log_path.open("ab") as log:
        try:
            proc = subprocess.run(
                brain_argv(max_turns=settings.brain_max_turns, max_budget_usd=settings.brain_max_budget_usd),
                input=brain_prompt(transcript).encode(),
                cwd=repo_root,
                env=reqenv.env_map() | {SPAWNED_ENV: "1"},
                stdout=log,
                stderr=log,
                check=False,
                timeout=settings.brain_deadline_seconds,
            )
        except subprocess.TimeoutExpired:
            return BrainOutcome(exit_code=-9, seconds=(datetime.now(UTC) - started).total_seconds(), log_path=log_path)
    return BrainOutcome(
        exit_code=proc.returncode, seconds=(datetime.now(UTC) - started).total_seconds(), log_path=log_path
    )


def pr_open_ids(store: ReviewStore, repo: RepoKey) -> set[int]:
    from captain_hook.review.store import CandidateStatus

    return {int(str(row["id"])) for row in store.candidates(repo, status=CandidateStatus.PR_OPEN)}


def watching_ids(store: ReviewStore, repo: RepoKey) -> set[int]:
    from captain_hook.review.store import CandidateStatus

    return {int(str(row["id"])) for row in store.candidates(repo, status=CandidateStatus.WATCHING)}


@contextmanager
def brain_lock(settings: ReviewSettings) -> Iterator[bool]:
    """Claims the machine-wide brain lock non-blockingly, yielding whether it was acquired.

    At most one PR-drafting brain runs across the machine at a time. A per-repo lock
    is bypassable: :meth:`ReviewStore.candidates` aliases one cross-repo candidate into
    both its target and origin repo, so concurrent passes in each would take *different*
    per-repo locks yet enumerate the same eligible candidate and spawn competing brains
    before either records ``pr_open``. A single global lock serializes every pass instead;
    per-repo eligibility still scopes what each acting brain works on. The lock is an OS
    ``flock`` under the review state dir — independent of any database connection, so it
    spans the store's open/close while the brain updates that same database. It is
    non-blocking: a second concurrent pass yields ``False`` and skips the brain instead of
    queueing behind the first.
    """
    (path := settings.db_path.parent / "locks" / "brain.lock").parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


async def review_session(transcript: Path, *, cwd: str, settings: ReviewSettings, sweep: bool = False) -> SpawnReport:
    """Runs the detached reviewer pass over one ended session.

    Scan, judge, and PR sync run whenever the repo is watched — verdicts
    amortize per session, and summary-fidelity verdicts re-judge once their
    windows hydrate again — and the brain spawns only when at least one
    candidate is eligible. The scan sweeps the transcript's whole parent
    directory through the mtime watermark, so ending one session also picks up
    the corrections of every still-open sibling session in the same repo, while
    the brain still acts only on the current cwd's eligible candidates.

    A ``sweep`` pass runs the same enroll, scan, triage, and judge, but skips PR
    sync and the entire brain block — a sweep only refreshes the mined evidence
    and never touches the live working copy.

    Args:
        transcript: The ended session's transcript file.
        cwd: The session's working directory, used to resolve the repo.
        settings: The reviewer settings.
        sweep: Whether this is a throttled Stop-triggered sweep (no sync, no brain).

    Returns:
        The :class:`SpawnReport` for this pass.
    """
    from loguru import logger

    from captain_hook.review.judge import judge_pass
    from captain_hook.review.scan import scan
    from captain_hook.review.store import CandidateStatus, ReviewStore
    from captain_hook.review.sync import sync_open_prs
    from captain_hook.review.triage import triage_pass

    if (repo := resolve_repo_key(cwd)) is None:
        return SpawnReport(repo=None, sweep=sweep)
    with ReviewStore.open(settings.db_path) as store:
        if not store.enroll(repo):
            return SpawnReport(repo=repo, sweep=sweep)
        scan_report = await scan(store, settings=settings, transcripts=[transcript.parent])
        triage = await triage_pass(store, settings=settings)
        verdicts = await judge_pass(store, settings=settings, refresh_summary=True)
        sync = None if sweep else await sync_open_prs(store, repo, settings=settings)
    eligible: tuple[int, ...] = ()
    brain = False
    brain_exit: int | None = None
    brain_seconds: float | None = None
    brain_prs = brain_skips = 0
    # The global lock spans the eligibility read through the brain and the pr_open set-diff,
    # so a concurrent pass — in this repo or any other sharing the db — can neither
    # double-spawn nor leak its transitions into this count.
    if not sweep:
        with brain_lock(settings) as claimed:
            if claimed:
                with ReviewStore.open(settings.db_path) as store:
                    eligible = tuple(
                        candidate_id
                        for row in store.candidates(repo, status=CandidateStatus.WATCHING)
                        if store.eligible(candidate_id := int(str(row["id"])), settings=settings)
                    )
                    opened_before = pr_open_ids(store, repo)
                if eligible:
                    brain = True
                    outcome = spawn_brain(transcript, repo_root=Path(cwd), settings=settings)
                    brain_exit, brain_seconds = outcome.exit_code, outcome.seconds
                    with ReviewStore.open(settings.db_path) as store:
                        brain_prs = len(pr_open_ids(store, repo) - opened_before)
                        brain_skips = len(set(eligible) & watching_ids(store, repo))
            else:
                logger.bind(repo=repo).info("reviewer brain skipped: another pass holds this repo's lock")
    return SpawnReport(
        repo=repo,
        watching=True,
        scanned=scan_report.scanned,
        inserted=scan_report.inserted,
        triaged=triage.triaged,
        triage_junk=triage.junk,
        triage_rejected=triage.rejected,
        judged=verdicts.judged,
        failed=verdicts.failed,
        eligible=eligible,
        brain=brain,
        brain_exit=brain_exit,
        brain_seconds=brain_seconds,
        brain_prs=brain_prs,
        brain_skips=brain_skips,
        synced_merged=sync.accepted if sync else 0,
        synced_closed=sync.rejected if sync else 0,
        synced_kept=(sync.kept + sync.unreachable) if sync else 0,
        sweep=sweep,
    )


async def spawn_session(
    transcript: Path, *, cwd: str, settings: ReviewSettings | None = None, sweep: bool = False
) -> SpawnReport:
    """Runs :func:`review_session` and records its outcome — the ``review spawn`` entry.

    Wraps the whole reviewer pass — settings construction, repo resolve, and
    store open included (the historical crash sites were inside them) — and
    records exactly one ``spawn_runs`` row per run into a freshly opened store,
    so ``capt-hook status`` surfaces a silently crashing reviewer. The pass runs
    under ``settings.spawn_deadline_seconds`` so a hang in the async pipeline
    becomes a recorded ``TimeoutError`` failure rather than a stalled child; the
    deadline is only evaluated between awaits, so the blocking brain subprocess
    is bounded separately by ``brain_deadline_seconds`` inside
    :func:`spawn_brain` (a killed brain records ``brain_exit=-9`` in a healthy
    run). The failure record opens the store with a low ``busy_timeout`` so a
    locked database can't hang the record too. A crash records ``ok=0`` and re-raises, so the traceback
    still lands in the spawn log; the catch is ``BaseException`` because
    ``asyncio.CancelledError`` is not an ``Exception``. When settings construction itself
    is the crash, the row lands at the default db path.

    Args:
        transcript: The ended session's transcript file.
        cwd: The session's working directory, used to resolve the repo.
        settings: The reviewer settings; constructed inside the recording
            boundary when omitted, so a settings/env failure still records.
        sweep: Whether to run the throttled sweep (no PR sync, no brain).

    Returns:
        The recorded :class:`SpawnReport` for this pass.
    """
    from loguru import logger

    from captain_hook.review.settings import ReviewSettings, resolve_review_db_path
    from captain_hook.review.store import ReviewStore

    logger.info(f"review spawn child start: transcript={transcript} cwd={cwd} sweep={sweep}")
    started = datetime.now(UTC)
    try:
        settings = settings or ReviewSettings()
        async with asyncio.timeout(settings.spawn_deadline_seconds):
            report = await review_session(transcript, cwd=cwd, settings=settings, sweep=sweep)
    except BaseException as exc:
        db_path = settings.db_path if settings else resolve_review_db_path()
        with ReviewStore.open(db_path, busy_timeout_ms=2000) as store:
            store.record_spawn_run(
                str(transcript), started_at=started, ok=False, error=f"{type(exc).__name__}: {exc}"
            )
        raise
    with ReviewStore.open(settings.db_path) as store:
        store.record_spawn_run(str(transcript), started_at=started, ok=True, report_json=json.dumps(asdict(report)))
    return report
