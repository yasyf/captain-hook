"""The ``capt-hook status`` dashboard: the corrections lifecycle, rendered.

Reads the reviewer's :class:`~captain_hook.review.store.ReviewStore` and renders
every candidate the reviewer tracks, bucketed by lifecycle stage — watching
(building toward the bar), eligible (a PR opens next session), PR open, and the
merged/closed/stale outcomes. Each row shows kind-aware progress toward its PR
thresholds and the one-sentence summary of what its PR would do. Open PRs are
shown from the last-synced state first, then refreshed against GitHub in the
background so the view appears instantly and updates when ``gh`` returns.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console, Group
from rich.spinner import Spinner
from rich.text import Text

from captain_hook.review.pipeline import review_log_path
from captain_hook.review.store import CandidateKind, CandidateStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from rich.console import RenderableType

    from captain_hook.app import LoadError
    from captain_hook.review.repo import RepoKey
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import CandidateView, JudgeHealth, SpawnHealth

DETAIL_WIDTH = 80
KIND_STYLE = {CandidateKind.CREATE: "cyan", CandidateKind.FIX: "magenta"}
SPAWN_STALE_AFTER = timedelta(days=7)
REJECTED_COLLAPSE_N = 5


class Stage(StrEnum):
    """A candidate's dashboard bucket — its lifecycle status, with watching split by eligibility."""

    WATCHING = "watching"
    ELIGIBLE = "eligible"
    PR_OPEN = "pr_open"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    STALE = "stale"


SECTIONS: tuple[tuple[Stage, str, str, str], ...] = (
    (Stage.WATCHING, "WATCHING", "building toward the bar", "yellow"),
    (Stage.ELIGIBLE, "ELIGIBLE", "a PR opens next session", "green"),
    (Stage.PR_OPEN, "PR OPEN", "pull request awaiting your review", "blue"),
    (Stage.ACCEPTED, "ACCEPTED", "PR merged", "green"),
    (Stage.REJECTED, "REJECTED", "PR closed or judge-retired", "red"),
    (Stage.STALE, "STALE", "PR open too long", "bright_black"),
)


def stage_of(view: CandidateView) -> Stage:
    """Buckets a candidate: its status, with ``watching`` split into eligible vs not."""
    match CandidateStatus(str(view.row["status"])):
        case CandidateStatus.PR_OPEN:
            return Stage.PR_OPEN
        case CandidateStatus.ACCEPTED:
            return Stage.ACCEPTED
        case CandidateStatus.REJECTED:
            return Stage.REJECTED
        case CandidateStatus.STALE:
            return Stage.STALE
        case CandidateStatus.WATCHING:
            return Stage.ELIGIBLE if view.eligible else Stage.WATCHING


def trim(text: str, *, width: int = DETAIL_WIDTH) -> str:
    return flat if len(flat := " ".join(text.split())) <= width else flat[: width - 1] + "…"


def bar(done: int, need: int, *, width: int = 5) -> str:
    cells = min(need, width)
    filled = min(cells, round(done / need * cells)) if need else cells
    return "█" * filled + "░" * (cells - filled)


def targets(view: CandidateView, settings: ReviewSettings) -> tuple[tuple[str, int, int], ...]:
    t = view.threshold
    match t.kind:
        case CandidateKind.CREATE:
            return (("sessions", t.sessions, settings.min_sessions), ("days", t.days, settings.min_days))
        case CandidateKind.FIX:
            return (("sessions", t.sessions, settings.min_sessions_fix), ("days", t.days, settings.min_days_fix))


def progress_text(view: CandidateView, settings: ReviewSettings) -> str:
    return "   ".join(
        f"{label} {bar(done, need)} {done}/{need}" for label, done, need in targets(view, settings) if need > 0
    )


def pr_description(view: CandidateView) -> str:
    """The one-line summary of what this candidate's PR would (or did) do."""
    row = view.row
    detail = trim(view.summary or str(row["sample_text"] or ""))
    pack = f"[{row['pack_name']}] " if row["pack_name"] else ""
    match CandidateKind(str(row["candidate_kind"])):
        case CandidateKind.CREATE:
            return f'would add a hook: "{detail}"' if detail else "would add a hook for this correction"
        case CandidateKind.FIX:
            tail = view.summary or (
                f"regression test for {row['misfire_class']}"
                if row["misfire_class"]
                else "regression test for the misfire"
            )
            return f"{pack}would fix {row['target_hook_name']} ({row['target_source_file']}): {trim(str(tail))}"


def age_days(row: dict[str, object]) -> int | None:
    if not (opened := row["pr_opened_at"]):
        return None
    return (datetime.now(UTC) - datetime.fromisoformat(str(opened))).days


def pr_link(view: CandidateView) -> str:
    url = str(view.row["pr_url"] or "(no url)")
    return (
        f"{url}   ·   {days}d open"
        if stage_of(view) is Stage.PR_OPEN and (days := age_days(view.row)) is not None
        else url
    )


def lead_detail(view: CandidateView, settings: ReviewSettings) -> str:
    match stage_of(view):
        case Stage.WATCHING:
            return progress_text(view, settings)
        case Stage.ELIGIBLE:
            return f"ready  ·  {progress_text(view, settings)}"
        case _:
            return pr_link(view)


def candidate_block(view: CandidateView, settings: ReviewSettings) -> RenderableType:
    kind = CandidateKind(str(view.row["candidate_kind"]))
    return Group(
        Text.assemble(
            (f"  #{view.row['id']}", "bold"),
            "  ",
            (kind.value.ljust(6), KIND_STYLE[kind]),
            "  ",
            lead_detail(view, settings),
        ),
        Text(f"      {pr_description(view)}", style="dim"),
    )


def relative(stamp: str) -> str:
    minutes = int((datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds()) // 60
    match minutes:
        case m if m < 60:
            return f"{m}m ago"
        case m if m < 1440:
            return f"{m // 60}h ago"
        case m:
            return f"{m // 1440}d ago"


def spawn_stale(stamp: str) -> bool:
    return datetime.now(UTC) - datetime.fromisoformat(stamp) > SPAWN_STALE_AFTER


def judge_segment(judge: JudgeHealth) -> str:
    verdict = f" · last verdict {relative(judge.last_verdict_at)}" if judge.last_verdict_at else ""
    splits = f"  ·  {n} possible slug splits" if (n := len(judge.splits)) else ""
    return f"judge: {judge.pending} pending{verdict}{splits}"


def brain_segment(report: dict[str, object]) -> tuple[str, str] | None:
    """The PR-drafting brain's ``exit · duration · PRs`` segment, red on a silent failure.

    ``None`` when the reviewer pass never spawned the brain (no eligible
    candidate). Otherwise renders red when the brain exited non-zero or left
    eligible candidates neither PR'd nor watching-skipped — the keychain /
    text-only-reply silent-failure surface — and dim when every eligible
    candidate got a PR or a logged skip (``brain_skips``; reports predating the
    field count skips as zero).
    """
    if report.get("brain_exit") is None:
        return None
    exit_code = int(str(report["brain_exit"]))
    prs = int(str(report["brain_prs"]))
    skips = int(str(report.get("brain_skips", 0)))
    seconds = float(str(report["brain_seconds"]))
    eligible = report["eligible"]
    failing = exit_code != 0 or (isinstance(eligible, list) and prs + skips < len(eligible))
    return f"brain: exit {exit_code} · {seconds:.0f}s · {prs} PR{'' if prs == 1 else 's'}", "red" if failing else "dim"


def health_line(health: SpawnHealth, judge: JudgeHealth) -> RenderableType:
    match health.last:
        case None:
            return Text("reviewer has never run — check the SessionEnd hook wiring", style="yellow")
        case {"ok": 0, "error": error}:
            return Group(
                Text.assemble(
                    ("REVIEWER FAILING", "bold red"),
                    (
                        f"   {health.consecutive_failures} consecutive since {health.failing_since}"
                        f"  ·  {trim(str(error))}",
                        "red",
                    ),
                ),
                Text(f"  see {review_log_path()}", style="dim"),
            )
        case {"finished_at": finished} if spawn_stale(str(finished)):
            return Text(
                f"reviewer last ran {relative(str(finished))} — check the SessionEnd hook wiring", style="yellow"
            )
        case last:
            report = json.loads(str(last["report_json"]))
            line = (
                f"reviewer ok  ·  last run {relative(str(last['finished_at']))}"
                f"  ·  judged {report['judged']}  ·  {judge_segment(judge)}"
            )
            match brain_segment(report):
                case None:
                    return Text(line, style="dim")
                case (segment, style):
                    return Text.assemble((f"{line}  ·  ", "dim"), (segment, style))


def unwatched_line(repos: Sequence[str]) -> RenderableType:
    return Text(
        f"reviewer ran for unwatched repos: {', '.join(repos)} — run `capt-hook review enable` in each repo",
        style="yellow",
    )


def pack_errors_lines(load_errors: Sequence[LoadError]) -> list[RenderableType]:
    """One red line per degraded hook/pack load, tagged with its pack; empty when nothing failed."""
    return [
        Text.assemble(
            ("HOOK LOAD FAILED", "bold red"),
            (
                f"   {f'[{e.pack}] ' if e.pack else ''}{Path(e.source).name}: {type(e.exc).__name__}: {e.exc}",
                "red",
            ),
        )
        for e in load_errors
    ]


def header(repo: RepoKey, settings: ReviewSettings, *, watching: bool, open_prs: int) -> RenderableType:
    line = Text.assemble(
        ("captain-hook", "bold"),
        ("  ·  ", "dim"),
        (str(repo), "cyan"),
        "    ",
        (f"[{'watching' if watching else 'not watching'}]", "green" if watching else "yellow"),
        "    ",
        (f"PR slots {open_prs}/{settings.max_open_prs}", "dim"),
    )
    if watching:
        return line
    return Group(line, Text("  run `capt-hook review enable` to start tracking this repo.", style="dim"))


def section_blocks(
    stage: Stage, title: str, desc: str, style: str, members: list[CandidateView], settings: ReviewSettings
) -> tuple[RenderableType, ...]:
    """One lifecycle section's blocks: its header, a block per candidate, then a spacer.

    The ``rejected`` bucket is the junk graveyard — deterministic prefilters, junk-triage,
    and judge-retire all land here — so beyond :data:`REJECTED_COLLAPSE_N` its members
    collapse to a single ``… and N more rejected`` count line rather than burying the
    live candidates above. Every other stage lists all its members.
    """
    shown = members[:REJECTED_COLLAPSE_N] if stage is Stage.REJECTED else members
    hidden = len(members) - len(shown)
    return (
        Text.assemble((title, f"bold {style}"), (f"   {desc}", "dim")),
        *(candidate_block(v, settings) for v in shown),
        *((Text(f"      … and {hidden} more rejected", style="dim"),) if hidden else ()),
        Text(""),
    )


def render(
    views: list[CandidateView],
    *,
    repo: RepoKey,
    settings: ReviewSettings,
    watching: bool,
    health: SpawnHealth,
    judge: JudgeHealth,
    open_prs: int,
    load_errors: Sequence[LoadError] = (),
    unwatched: Sequence[str] = (),
    syncing: bool = False,
) -> RenderableType:
    """The whole dashboard frame: reviewer health, degraded loads, header, then a section per lifecycle stage."""
    sections = [
        block
        for stage, title, desc, style in SECTIONS
        if (members := [v for v in views if stage_of(v) is stage])
        for block in section_blocks(stage, title, desc, style, members, settings)
    ]
    empty = [] if views else [Text("No corrections tracked yet — they appear here as you correct Claude.", style="dim")]
    spinner = [Spinner("dots", text=Text("syncing open PRs with GitHub…", style="dim"))] if syncing else []
    return Group(
        health_line(health, judge),
        *([unwatched_line(unwatched)] if unwatched else []),
        *pack_errors_lines(load_errors),
        header(repo, settings, watching=watching, open_prs=open_prs),
        Text(""),
        *sections,
        *empty,
        *spinner,
    )


async def run_status(repo: RepoKey, *, sync: bool, load_errors: Sequence[LoadError] = ()) -> None:
    """Renders the dashboard for ``repo``, refreshing open-PR state in the background when ``sync``."""
    from rich.live import Live

    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore
    from captain_hook.review.sync import sync_open_prs

    settings = ReviewSettings()
    console = Console()
    with ReviewStore.open(settings.db_path) as store:
        health = store.spawn_health()
        judge = store.judge_health()
        unwatched = store.unwatched_session_repos()
        watching = store.watching(repo)
        views = store.overview(repo, settings=settings)
        open_prs = store.open_pr_targets(settings=settings).get(repo, 0)
        if not (sync and any(stage_of(v) is Stage.PR_OPEN for v in views)):
            console.print(
                render(
                    views,
                    repo=repo,
                    settings=settings,
                    watching=watching,
                    health=health,
                    judge=judge,
                    open_prs=open_prs,
                    load_errors=load_errors,
                    unwatched=unwatched,
                )
            )
            return
        with Live(
            render(
                views,
                repo=repo,
                settings=settings,
                watching=watching,
                health=health,
                judge=judge,
                open_prs=open_prs,
                load_errors=load_errors,
                unwatched=unwatched,
                syncing=True,
            ),
            console=console,
        ) as live:
            await sync_open_prs(store, repo, settings=settings)
            fresh = store.overview(repo, settings=settings)
            open_prs = store.open_pr_targets(settings=settings).get(repo, 0)
            live.update(
                render(
                    fresh,
                    repo=repo,
                    settings=settings,
                    watching=watching,
                    health=health,
                    judge=judge,
                    open_prs=open_prs,
                    load_errors=load_errors,
                    unwatched=unwatched,
                )
            )


def status_command(repo: RepoKey, *, sync: bool, load_errors: Sequence[LoadError] = ()) -> None:
    """The synchronous CLI boundary for ``review status`` / ``capt-hook status``."""
    asyncio.run(run_status(repo, sync=sync, load_errors=load_errors))
