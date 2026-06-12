"""The two-stage SessionEnd detach: a guard-only hook path and the detached reviewer child.

``review run`` is wired to the SessionEnd hook event and must exit 0 on every
path — a non-zero or hanging hook wedges the user's session — so
:func:`guard_and_spawn` does nothing but parse, guard, and detach
``capt-hook review spawn`` via ``Popen(start_new_session=True)``; no database
reads, no scanning, no ``gh``, and no heavy imports happen on that path. The
detached child (:func:`review_session`) does the real work: resolve the repo,
check it is watched, scan the transcript incrementally, run the judge pass,
sync open PR states, and — when at least one candidate crosses its
thresholds — spawn the headless brain that drafts the PRs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeGuard

from captain_hook.review.repo import resolve_repo_key
from captain_hook.settings import resolve_state_dir

if TYPE_CHECKING:
    from spawnllm import TModel

    from captain_hook.review.repo import RepoKey
    from captain_hook.review.settings import ReviewSettings

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
        judged: How many rows the judge pass verdicted.
        failed: How many judge calls failed and stay pending.
        eligible: The candidate ids that crossed their PR thresholds.
        brain: Whether the headless brain was spawned.
    """

    repo: RepoKey | None
    watching: bool = False
    scanned: int = 0
    inserted: int = 0
    judged: int = 0
    failed: int = 0
    eligible: tuple[int, ...] = ()
    brain: bool = False


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
    ``CAPT_HOOK_SPAWNED`` (the reviewer's own spawned sessions), malformed
    stdin, a missing transcript, and a failed spawn all fall through silently.
    The child runs with ``CAPT_HOOK_SPAWNED=1`` and its output appended to
    :func:`review_log_path`.

    Args:
        raw: The hook's stdin bytes, holding the SessionEnd JSON payload.
    """
    if os.environ.get(SPAWNED_ENV):
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
        f"/scanning-sessions --transcript {transcript}\n\n"
        f"[{REVIEWER_MARKER}] Review this repo's eligible candidates and open at most one pull request per"
        " candidate. Work in one continuous run: do not stop to summarize after drafting — you are done only"
        " when every eligible candidate has a PR recorded via `review update <id> pr_open --pr-url <url>` or"
        " is explicitly skipped with a reason."
    )


def brain_argv(*, max_turns: int, max_budget_usd: float) -> list[str]:
    from captain_hook.llm import ClaudeBackend

    backend = ClaudeBackend()
    argv = backend.build_command(backend.models[BRAIN_TIER], None, agent=True)
    argv[argv.index("--permission-mode") + 1] = "acceptEdits"
    argv[argv.index("--max-budget-usd") + 1] = str(max_budget_usd)
    return [*argv, "--max-turns", str(max_turns), "--allowedTools", ",".join(BRAIN_ALLOWED_TOOLS)]


def spawn_brain(transcript: Path, *, repo_root: Path, settings: ReviewSettings) -> None:
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
    """
    (log_path := review_log_path()).parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        subprocess.run(
            brain_argv(max_turns=settings.brain_max_turns, max_budget_usd=settings.brain_max_budget_usd),
            input=brain_prompt(transcript).encode(),
            cwd=repo_root,
            env=os.environ | {SPAWNED_ENV: "1"},
            stdout=log,
            stderr=log,
            check=False,
        )


async def review_session(transcript: Path, *, cwd: str, settings: ReviewSettings) -> SpawnReport:
    """Runs the detached reviewer pass over one ended session.

    Scan, judge, and PR sync run whenever the repo is watched — verdicts
    amortize per session, and summary-fidelity verdicts re-judge once their
    windows hydrate again — and the brain spawns only when at least one
    candidate is eligible.

    Args:
        transcript: The ended session's transcript file.
        cwd: The session's working directory, used to resolve the repo.
        settings: The reviewer settings.

    Returns:
        The :class:`SpawnReport` for this pass.
    """
    from captain_hook.review.judge import REVIEW_PROMPT_VERSION, judge_pass
    from captain_hook.review.scan import scan_transcript
    from captain_hook.review.store import CandidateStatus, ReviewStore
    from captain_hook.review.sync import sync_open_prs

    if (repo := resolve_repo_key(cwd)) is None:
        return SpawnReport(repo=None)
    async with await ReviewStore.open(settings.db_path) as store:
        if not await store.watching(repo):
            return SpawnReport(repo=repo)
        scan = await scan_transcript(store, transcript, settings=settings, repo_key=repo)
        verdicts = await judge_pass(store, settings=settings, refresh_summary=True)
        await sync_open_prs(store, repo, settings=settings)
        eligible = tuple(
            [
                candidate_id
                for row in await store.candidates(repo, status=CandidateStatus.WATCHING)
                if await store.eligible(
                    candidate_id := int(str(row["id"])), settings=settings, prompt_version=REVIEW_PROMPT_VERSION
                )
            ]
        )
    if eligible:
        spawn_brain(transcript, repo_root=Path(cwd), settings=settings)
    return SpawnReport(
        repo=repo,
        watching=True,
        scanned=scan.scanned,
        inserted=scan.inserted,
        judged=verdicts.judged,
        failed=verdicts.failed,
        eligible=eligible,
        brain=bool(eligible),
    )
