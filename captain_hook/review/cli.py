"""The ``capt-hook review`` command group: the SessionEnd reviewer's CLI surface.

``review run`` is the wired SessionEnd hook and must exit 0 fast on every path,
so this module imports nothing heavy at load; every command pulls the review
machinery lazily once it actually runs, and async store calls bridge with
``asyncio.run`` at the command boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from typing import Any

    from cc_transcript.corrections import Correction

    from captain_hook.cli import CliState
    from captain_hook.review.judge import JudgeReport
    from captain_hook.review.repo import RepoKey
    from captain_hook.review.scan import ScanReport
    from captain_hook.review.store import ThresholdStatus
    from captain_hook.review.sync import SyncReport

REVIEW_RUN_COMMAND = "capt-hook review run"
STATUS_CHOICES = ("watching", "pr_open", "stale", "accepted", "rejected")


def current_repo(root: Path) -> RepoKey:
    from captain_hook.review.repo import repo_key

    if (repo := repo_key(root)) is None:
        raise click.ClickException(f"{root} is not inside a git repository")
    return repo


def resolve_repo(repo_: str | None, root: Path) -> RepoKey:
    from captain_hook.review.repo import RepoKey

    return RepoKey(repo_) if repo_ else current_repo(root)


def group_commands(group: dict[str, Any]) -> list[str]:
    entries: list[dict[str, Any]] = group.get("hooks") or []
    return [str(entry.get("command") or "") for entry in entries]


def review_wired(hooks: dict[str, Any]) -> bool:
    groups: list[dict[str, Any]] = hooks.get("SessionEnd") or []
    return any(REVIEW_RUN_COMMAND in command for group in groups for command in group_commands(group))


def ensure_review_wiring(settings_path: Path) -> bool:
    from captain_hook.cli import sibling_settings, write_settings

    existing: dict[str, Any] = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    sibling = sibling_settings(settings_path)
    sibling_hooks: dict[str, Any] = (json.loads(sibling.read_text()).get("hooks") or {}) if sibling.exists() else {}
    hooks: dict[str, Any] = existing.get("hooks") or {}
    if review_wired(hooks) or review_wired(sibling_hooks):
        return False
    group = {"hooks": [{"type": "command", "command": f"uvx {REVIEW_RUN_COMMAND}", "async": True}]}
    write_settings(
        settings_path,
        existing | {"hooks": hooks | {"SessionEnd": [*(hooks.get("SessionEnd") or []), group]}},
    )
    return True


def watch_repo(repo: RepoKey) -> None:
    """Flip the global watching bit for ``repo``.

    The single persistence path shared by ``init`` and ``review enable``.
    """
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

    async def go() -> None:
        async with await ReviewStore.open(ReviewSettings().db_path) as store:
            await store.enable(repo)

    asyncio.run(go())


def candidate_line(row: dict[str, object]) -> str:
    text = str(row["sample_text"] or "").replace("\n", " ")
    return (
        f"#{row['id']} [{row['status']}] {row['candidate_kind']}/{row['source_kind']} "
        f"x{row['observations']}: {text[:80]}"
    )


@click.group()
def review() -> None:
    """Mine ended sessions for durable corrections and draft hook PRs."""


@review.command(name="run")
def run_hook() -> None:
    """SessionEnd hook entry: guard and detach the reviewer (always exits 0)."""
    from captain_hook.review.pipeline import guard_and_spawn

    guard_and_spawn(sys.stdin.buffer.read())


@review.command(hidden=True)
@click.option("--transcript", required=True, type=click.Path(path_type=Path), help="The ended session's transcript")
@click.option("--cwd", "cwd", default=None, help="The session's working directory (default: the process cwd)")
def spawn(transcript: Path, cwd: str | None) -> None:
    """Run the detached reviewer pass over one ended session (spawned by ``review run``)."""
    from captain_hook.review.pipeline import review_session
    from captain_hook.review.settings import ReviewSettings

    click.echo(asyncio.run(review_session(transcript, cwd=cwd or os.getcwd(), settings=ReviewSettings())))


@review.command()
@click.pass_obj
def enable(state: CliState) -> None:
    """Watch the current repo, register the captain-hook plugin, and wire the SessionEnd hook."""
    from captain_hook.cli import register_marketplace

    repo = current_repo(state.root)
    watch_repo(repo)
    register_marketplace(state.root)
    wired = ensure_review_wiring(state.root / ".claude" / "settings.json")
    click.echo(f"watching {repo}" + (" (SessionEnd hook wired into .claude/settings.json)" if wired else ""))


@review.command()
@click.pass_obj
def disable(state: CliState) -> None:
    """Stop watching the current repo (its candidates stay recorded)."""
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

    repo = current_repo(state.root)

    async def go() -> None:
        async with await ReviewStore.open(ReviewSettings().db_path) as store:
            await store.disable(repo)

    asyncio.run(go())
    click.echo(f"not watching {repo}")


@review.command()
@click.option(
    "--transcript",
    "transcripts",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="A transcript file to scan",
)
@click.option(
    "--dir",
    "dirs",
    multiple=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="A directory to scan recursively for *.jsonl transcripts",
)
def scan(transcripts: tuple[Path, ...], dirs: tuple[Path, ...]) -> None:
    """Scan explicit transcripts for corrections, incrementally."""
    from captain_hook.review.scan import scan as run_scan
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

    if not transcripts and not dirs:
        raise click.UsageError("pass at least one --transcript or --dir")
    settings = ReviewSettings()

    async def go() -> ScanReport:
        async with await ReviewStore.open(settings.db_path) as store:
            return await run_scan(store, settings=settings, transcripts=[*transcripts, *dirs])

    report = asyncio.run(go())
    click.echo(f"scanned {report.scanned} transcripts, {report.inserted} new corrections")


@review.command()
@click.option("--limit", type=int, default=None, help="Judge at most this many rows (default: the per-session cap)")
def triage(limit: int | None) -> None:
    """Judge stored corrections lacking a verdict (manual/backfill)."""
    from captain_hook.review.judge import judge_pass
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

    settings = ReviewSettings()

    async def go() -> JudgeReport:
        async with await ReviewStore.open(settings.db_path) as store:
            return await judge_pass(store, settings=settings, limit=limit)

    report = asyncio.run(go())
    click.echo(f"judged {report.judged}, failed {report.failed}, pending {report.pending}")


@review.command(name="status")
@click.option("--repo", "repo_", default=None, help="Repo key (default: the current repo)")
@click.option("--sync/--no-sync", default=True, help="Refresh open PR states from GitHub in the background")
@click.pass_obj
def status(state: CliState, repo_: str | None, sync: bool) -> None:
    """Show the tracked corrections, their progress toward a PR, and open PR status."""
    from captain_hook.review.dashboard import status_command

    status_command(resolve_repo(repo_, state.root), sync=sync)


@review.command(name="list")
@click.option("--repo", "repo_", default=None, help="Repo key (default: the current repo)")
@click.pass_obj
def list_candidates(state: CliState, repo_: str | None) -> None:
    """List the repo's PR candidates."""
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

    repo = resolve_repo(repo_, state.root)

    async def go() -> list[dict[str, object]]:
        async with await ReviewStore.open(ReviewSettings().db_path) as store:
            return await store.candidates(repo)

    if not (rows := asyncio.run(go())):
        click.echo(f"no candidates for {repo}")
    for row in rows:
        click.echo(candidate_line(row))


def correction_lines(correction: Correction) -> tuple[str, ...]:
    match correction.correction_origin:
        case "session" | "git":
            return (
                f"  correction ({correction.correction_origin}):",
                f"    - {correction.correction_old}",
                f"    + {correction.correction_new}",
            )
        case _ if correction.correction_text:
            return (f"  correction note: {correction.correction_text}",)
        case _:
            return ()


def correction_block(correction: Correction) -> str:
    return "\n".join(
        (
            f"- {correction.incorrect_file} (session {correction.session_id}):",
            f"    - {correction.incorrect_old}",
            f"    + {correction.incorrect_new}",
            *correction_lines(correction),
        )
    )


@review.command()
@click.argument("candidate_id", type=int)
def show(candidate_id: int) -> None:
    """Show one candidate's row, its threshold status, and the shared ledger's faulted edits."""
    from captain_hook.review.judge import REVIEW_PROMPT_VERSION
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

    settings = ReviewSettings()

    async def go() -> tuple[dict[str, object], ThresholdStatus, bool, tuple[Correction, ...]]:
        async with await ReviewStore.open(settings.db_path) as store:
            return (
                await store.candidate(candidate_id),
                await store.threshold_status(candidate_id, settings=settings, prompt_version=REVIEW_PROMPT_VERSION),
                await store.eligible(candidate_id, settings=settings, prompt_version=REVIEW_PROMPT_VERSION),
                await store.correction_evidence(candidate_id),
            )

    try:
        row, status, ok, evidence = asyncio.run(go())
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc
    for key, value in row.items():
        click.echo(f"{key}: {value}")
    click.echo(
        f"thresholds: sessions={status.sessions} days={status.days} open_prs={status.open_prs} "
        f"single_observation={status.single_observation} eligible={ok}"
    )
    if evidence:
        click.echo("correction_evidence:")
        for correction in evidence:
            click.echo(correction_block(correction))


@review.command(name="threshold-check")
@click.argument("candidate_id", type=int, required=False)
@click.option("--repo", "repo_", default=None, help="Repo key (default: the current repo)")
@click.pass_obj
def threshold_check(state: CliState, candidate_id: int | None, repo_: str | None) -> None:
    """Report which candidates cross their PR thresholds."""
    from captain_hook.review.judge import REVIEW_PROMPT_VERSION
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

    settings = ReviewSettings()

    async def go() -> list[str]:
        async with await ReviewStore.open(settings.db_path) as store:
            ids = (
                [candidate_id]
                if candidate_id is not None
                else [int(str(row["id"])) for row in await store.candidates(resolve_repo(repo_, state.root))]
            )
            return [
                f"#{cid} eligible={await store.eligible(cid, settings=settings, prompt_version=REVIEW_PROMPT_VERSION)}"
                f" sessions={status.sessions}/{settings.min_sessions} days={status.days}/{settings.min_days}"
                f" open_prs={status.open_prs}/{settings.max_open_prs} watching={status.watching}"
                for cid in ids
                if (
                    status := await store.threshold_status(cid, settings=settings, prompt_version=REVIEW_PROMPT_VERSION)
                )
            ]

    try:
        lines = asyncio.run(go())
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc
    if not lines:
        click.echo("no candidates")
    for line in lines:
        click.echo(line)


@review.command()
@click.argument("candidate_id", type=int)
@click.argument("status", type=click.Choice(STATUS_CHOICES))
@click.option("--pr-url", default=None, help="PR URL stamped with the move (also stamps pr_opened_at)")
def update(candidate_id: int, status: str, pr_url: str | None) -> None:
    """Move a candidate to a new status."""
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import CandidateStatus, InvalidTransition, ReviewStore

    async def go() -> None:
        async with await ReviewStore.open(ReviewSettings().db_path) as store:
            await store.transition(
                candidate_id,
                CandidateStatus(status),
                pr_url=pr_url,
                pr_opened_at=datetime.now(UTC) if pr_url else None,
            )

    try:
        asyncio.run(go())
    except (InvalidTransition, LookupError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"#{candidate_id} -> {status}")


@review.command(name="sync-prs")
@click.option("--repo", "repo_", default=None, help="Repo key (default: the current repo)")
@click.pass_obj
def sync_prs(state: CliState, repo_: str | None) -> None:
    """Fold open PR states back into candidate statuses."""
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore
    from captain_hook.review.sync import sync_open_prs

    settings = ReviewSettings()
    repo = resolve_repo(repo_, state.root)

    async def go() -> SyncReport:
        async with await ReviewStore.open(settings.db_path) as store:
            return await sync_open_prs(store, repo, settings=settings)

    report = asyncio.run(go())
    click.echo(
        f"accepted {report.accepted}, rejected {report.rejected}, "
        f"stale {report.stale}, unreachable {report.unreachable}"
    )
