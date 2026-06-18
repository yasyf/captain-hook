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
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from rich.console import Console, Group
from rich.spinner import Spinner
from rich.text import Text

from captain_hook.review.store import CandidateKind, CandidateStatus

if TYPE_CHECKING:
    from rich.console import RenderableType

    from captain_hook.review.repo import RepoKey
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import CandidateView

BAR_FILLED = "█"
BAR_EMPTY = "░"
DETAIL_WIDTH = 80
KIND_STYLE = {CandidateKind.CREATE: "cyan", CandidateKind.FIX: "magenta"}


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
    (Stage.REJECTED, "REJECTED", "PR closed", "red"),
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
    return BAR_FILLED * filled + BAR_EMPTY * (cells - filled)


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
    match CandidateKind(str(row["candidate_kind"])):
        case CandidateKind.CREATE:
            return f'would add a hook: "{detail}"' if detail else "would add a hook for this correction"
        case CandidateKind.FIX:
            tail = view.summary or (
                f"regression test for {row['misfire_class']}" if row["misfire_class"] else "regression test for the misfire"
            )
            return f"would fix {row['target_hook_name']} ({row['target_source_file']}): {trim(str(tail))}"


def age_days(row: dict[str, object]) -> int | None:
    if not (opened := row["pr_opened_at"]):
        return None
    return (datetime.now(UTC) - datetime.fromisoformat(str(opened))).days


def pr_link(view: CandidateView) -> str:
    url = str(view.row["pr_url"] or "(no url)")
    return f"{url}   ·   {days}d open" if stage_of(view) is Stage.PR_OPEN and (days := age_days(view.row)) is not None else url


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


def header(repo: RepoKey, views: list[CandidateView], settings: ReviewSettings, *, watching: bool) -> RenderableType:
    open_n = sum(1 for v in views if stage_of(v) is Stage.PR_OPEN)
    line = Text.assemble(
        ("captain-hook", "bold"),
        ("  ·  ", "dim"),
        (str(repo), "cyan"),
        "    ",
        (f"[{'watching' if watching else 'not watching'}]", "green" if watching else "yellow"),
        "    ",
        (f"PR slots {open_n}/{settings.max_open_prs}", "dim"),
    )
    if watching:
        return line
    return Group(line, Text("  run `capt-hook review enable` to start tracking this repo.", style="dim"))


def render(
    views: list[CandidateView], *, repo: RepoKey, settings: ReviewSettings, watching: bool, syncing: bool = False
) -> RenderableType:
    """The whole dashboard frame: header, then a section per non-empty lifecycle stage."""
    sections = [
        block
        for stage, title, desc, style in SECTIONS
        if (members := [v for v in views if stage_of(v) is stage])
        for block in (
            Text.assemble((title, f"bold {style}"), (f"   {desc}", "dim")),
            *(candidate_block(v, settings) for v in members),
            Text(""),
        )
    ]
    empty = [] if views else [Text("No corrections tracked yet — they appear here as you correct Claude.", style="dim")]
    spinner = [Spinner("dots", text=Text("syncing open PRs with GitHub…", style="dim"))] if syncing else []
    return Group(header(repo, views, settings, watching=watching), Text(""), *sections, *empty, *spinner)


async def run_status(repo: RepoKey, *, sync: bool) -> None:
    """Renders the dashboard for ``repo``, refreshing open-PR state in the background when ``sync``."""
    from rich.live import Live

    from captain_hook.review.judge import REVIEW_PROMPT_VERSION
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore
    from captain_hook.review.sync import sync_open_prs

    settings = ReviewSettings()
    console = Console()
    async with await ReviewStore.open(settings.db_path) as store:
        watching = await store.watching(repo)
        views = await store.overview(repo, settings=settings, prompt_version=REVIEW_PROMPT_VERSION)
        if not (sync and any(stage_of(v) is Stage.PR_OPEN for v in views)):
            console.print(render(views, repo=repo, settings=settings, watching=watching))
            return
        with Live(render(views, repo=repo, settings=settings, watching=watching, syncing=True), console=console) as live:
            await sync_open_prs(store, repo, settings=settings)
            fresh = await store.overview(repo, settings=settings, prompt_version=REVIEW_PROMPT_VERSION)
            live.update(render(fresh, repo=repo, settings=settings, watching=watching))


def status_command(repo: RepoKey, *, sync: bool) -> None:
    """The synchronous CLI boundary for ``review status`` / ``capt-hook status``."""
    asyncio.run(run_status(repo, sync=sync))
