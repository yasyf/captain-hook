"""The two-stage SessionEnd detach: a guard-only hook path and the detached reviewer child.

``review run`` is wired to the SessionEnd hook event and must exit 0 on every
path — a non-zero or hanging hook wedges the user's session — so
:func:`guard_and_spawn` does nothing but parse, guard, and detach
``capt-hook review spawn`` via ``Popen(start_new_session=True)``; no database
reads, no scanning, no ``gh``, and no heavy imports happen on that path. The
detached child (:func:`spawn_session`, wrapping :func:`review_session`) does
the real work: resolve the repo, check it is watched, scan the transcript
incrementally, run the judge pass, sync open PR states, and — when at least
one candidate crosses its thresholds — spawn the headless brain that drafts
the PRs, recording each run's outcome for the status dashboard's health line.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypeGuard

from captain_hook.review.repo import resolve_repo_key
from captain_hook.settings import resolve_state_dir

if TYPE_CHECKING:
    from spawnllm import TModel

    from captain_hook.review.repo import RepoKey
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

SPAWNED_ENV = "CAPT_HOOK_SPAWNED"
BRAIN_TIER: TModel = "medium"
BRAIN_ALLOWED_TOOLS = ("Read", "Grep", "Glob", "Write", "Edit", "Bash", "Skill", "Agent")


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
        synced_merged: How many open PRs merged, accepting their candidate.
        synced_closed: How many open PRs closed, rejecting their candidate.
        synced_kept: How many open PRs stayed open (fresh or ``gh``-unreachable).
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
    synced_merged: int = 0
    synced_closed: int = 0
    synced_kept: int = 0


@dataclass(frozen=True, slots=True)
class BrainOutcome:
    """The headless PR-drafting brain's run outcome for one reviewer pass.

    Attributes:
        exit_code: The brain subprocess's exit status.
        seconds: The brain's wall-clock runtime in seconds.
        log_path: The spawn log the brain's stdout and stderr appended to.
    """

    exit_code: int
    seconds: float
    log_path: Path


def review_log_path() -> Path:
    return resolve_state_dir() / "review" / "spawn.log"


def spawn_argv(transcript: str, cwd: str | None) -> list[str]:
    return [
        sys.executable,
        "-m",
        "captain_hook",
        "review",
        "spawn",
        "--transcript",
        transcript,
        *(("--cwd", cwd) if cwd else ()),
    ]


def is_payload(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def parse_payload(raw: bytes) -> dict[str, object] | None:
    try:
        payload: object = json.loads(raw)
    except ValueError:
        return None
    return payload if is_payload(payload) else None


def guard_and_spawn(raw: bytes) -> None:
    """Parses the SessionEnd hook payload and detaches the reviewer child.

    Every path returns normally so the wired hook always exits 0: a set
    ``CAPT_HOOK_SPAWNED`` (the reviewer's own spawned sessions), a headless
    ``claude -p`` / SDK session (``CLAUDE_CODE_ENTRYPOINT`` in the ``sdk-*``
    family; an interactive quit is ``cli``), malformed stdin, a missing
    transcript, and a failed spawn all fall through silently. The child runs
    with ``CAPT_HOOK_SPAWNED=1`` and its output appended to
    :func:`review_log_path`.

    Args:
        raw: The hook's stdin bytes, holding the SessionEnd JSON payload.
    """
    if os.environ.get(SPAWNED_ENV):
        return
    # headless `claude -p` / SDK run — not an interactive session
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk"):
        return
    if (payload := parse_payload(raw)) is None:
        return
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str):
        return
    try:
        missing = not Path(transcript).is_file()
    except ValueError:
        return
    if missing:
        return
    cwd = payload.get("cwd")
    try:
        (log_path := review_log_path()).parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log:
            subprocess.Popen(
                spawn_argv(transcript, cwd if isinstance(cwd, str) else None),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                env=os.environ | {SPAWNED_ENV: "1"},
            )
    except OSError:
        return


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
    ``settings.brain_max_turns``. The prompt carries the reviewer marker so the
    brain's own SessionEnd self-skips, and ``CAPT_HOOK_SPAWNED=1`` keeps its
    SessionEnd hook from re-spawning the reviewer.

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
        proc = subprocess.run(
            brain_argv(max_turns=settings.brain_max_turns, max_budget_usd=settings.brain_max_budget_usd),
            input=brain_prompt(transcript).encode(),
            cwd=repo_root,
            env=os.environ | {SPAWNED_ENV: "1"},
            stdout=log,
            stderr=log,
            check=False,
        )
    return BrainOutcome(
        exit_code=proc.returncode, seconds=(datetime.now(UTC) - started).total_seconds(), log_path=log_path
    )


async def pr_open_ids(store: ReviewStore, repo: RepoKey) -> set[int]:
    from captain_hook.review.store import CandidateStatus

    return {int(str(row["id"])) for row in await store.candidates(repo, status=CandidateStatus.PR_OPEN)}


async def review_session(transcript: Path, *, cwd: str, settings: ReviewSettings) -> SpawnReport:
    """Runs the detached reviewer pass over one ended session.

    Scan, judge, and PR sync run whenever the repo is watched — verdicts
    amortize per session, and summary-fidelity verdicts re-judge once their
    windows hydrate again — and the brain spawns only when at least one
    candidate is eligible. The scan sweeps the transcript's whole parent
    directory through the mtime watermark, so ending one session also picks up
    the corrections of every still-open sibling session in the same repo, while
    the brain still acts only on the current cwd's eligible candidates.

    Args:
        transcript: The ended session's transcript file.
        cwd: The session's working directory, used to resolve the repo.
        settings: The reviewer settings.

    Returns:
        The :class:`SpawnReport` for this pass.
    """
    from captain_hook.review.judge import judge_pass
    from captain_hook.review.scan import scan
    from captain_hook.review.store import CandidateStatus, ReviewStore
    from captain_hook.review.sync import sync_open_prs
    from captain_hook.review.triage import triage_pass

    if (repo := resolve_repo_key(cwd)) is None:
        return SpawnReport(repo=None)
    async with await ReviewStore.open(settings.db_path) as store:
        if not await store.watching(repo):
            return SpawnReport(repo=repo)
        scan_report = await scan(store, settings=settings, transcripts=[transcript.parent])
        triage = await triage_pass(store, settings=settings)
        verdicts = await judge_pass(store, settings=settings, refresh_summary=True)
        sync = await sync_open_prs(store, repo, settings=settings)
        eligible = tuple(
            [
                candidate_id
                for row in await store.candidates(repo, status=CandidateStatus.WATCHING)
                if await store.eligible(candidate_id := int(str(row["id"])), settings=settings)
            ]
        )
        opened_before = await pr_open_ids(store, repo)
    outcome = spawn_brain(transcript, repo_root=Path(cwd), settings=settings) if eligible else None
    brain_prs = 0
    if outcome is not None:
        async with await ReviewStore.open(settings.db_path) as store:
            brain_prs = len(await pr_open_ids(store, repo) - opened_before)
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
        brain=bool(eligible),
        brain_exit=outcome.exit_code if outcome else None,
        brain_seconds=outcome.seconds if outcome else None,
        brain_prs=brain_prs,
        synced_merged=sync.accepted,
        synced_closed=sync.rejected,
        synced_kept=sync.kept + sync.unreachable,
    )


async def spawn_session(transcript: Path, *, cwd: str, settings: ReviewSettings | None = None) -> SpawnReport:
    """Runs :func:`review_session` and records its outcome — the ``review spawn`` entry.

    Wraps the whole reviewer pass — settings construction, repo resolve, and
    store open included (the historical crash sites were inside them) — and
    records exactly one ``spawn_runs`` row per run into a freshly opened store,
    so ``capt-hook status`` surfaces a silently crashing reviewer. A crash
    records ``ok=0`` and re-raises, so the traceback still lands in the spawn
    log; the catch is ``BaseException`` because anyio cancellation shapes are
    not ``Exception``. When settings construction itself is the crash, the row
    lands at the default db path.

    Args:
        transcript: The ended session's transcript file.
        cwd: The session's working directory, used to resolve the repo.
        settings: The reviewer settings; constructed inside the recording
            boundary when omitted, so a settings/env failure still records.

    Returns:
        The recorded :class:`SpawnReport` for this pass.
    """
    from captain_hook.review.settings import ReviewSettings, resolve_review_db_path
    from captain_hook.review.store import ReviewStore

    started = datetime.now(UTC)
    try:
        settings = settings or ReviewSettings()
        report = await review_session(transcript, cwd=cwd, settings=settings)
    except BaseException as exc:
        async with await ReviewStore.open(settings.db_path if settings else resolve_review_db_path()) as store:
            await store.record_spawn_run(
                str(transcript), started_at=started, ok=False, error=f"{type(exc).__name__}: {exc}"
            )
        raise
    async with await ReviewStore.open(settings.db_path) as store:
        await store.record_spawn_run(
            str(transcript), started_at=started, ok=True, report_json=json.dumps(asdict(report))
        )
    return report
