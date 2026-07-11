"""PR lifecycle sync: fold each open PR's GitHub state back into its candidate.

A merged PR accepts its candidate, a closed PR rejects it, and a PR open past
``stale_after_days`` goes stale — freeing its slot under ``max_open_prs``. A
``gh`` failure (not installed, not authenticated, network down) is logged and
skipped so the detached child never dies on it.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple

from loguru import logger

from captain_hook.review.store import CandidateStatus

if TYPE_CHECKING:
    from captain_hook.review.repo import RepoKey
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

GH_TIMEOUT = 30
PR_STATE_TTL = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class PrState:
    """A PR's GitHub state as of one sync pass.

    Attributes:
        state: The ``gh`` state string — ``MERGED``, ``CLOSED``, or ``OPEN``.
        merged_at: The merge timestamp when merged, else ``None``.
    """

    state: str
    merged_at: str | None


class CachedPrState(NamedTuple):
    """A :class:`PrState` read back from the ``pr_states`` cache with its fetch time.

    Attributes:
        pr: The cached GitHub state.
        fetched_at: When the state was last fetched from ``gh``.
    """

    pr: PrState
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class SyncReport:
    """The outcome of one PR sync pass.

    Attributes:
        accepted: How many candidates moved to accepted (PR merged).
        rejected: How many candidates moved to rejected (PR closed).
        stale: How many candidates went stale (PR open too long).
        unreachable: How many PRs ``gh`` could not report on this pass.
        kept: How many PRs stayed open, leaving their candidate untouched.
    """

    accepted: int
    rejected: int
    stale: int
    unreachable: int
    kept: int = 0


def gh_pr_state(url: str) -> PrState | None:
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", url, "--json", "state,mergedAt"], capture_output=True, text=True, timeout=GH_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
        return PrState(state=str(data["state"]), merged_at=data["mergedAt"])
    except (ValueError, KeyError):
        return None


def is_stale(opened_at: str, *, days: int) -> bool:
    return datetime.fromisoformat(opened_at) < datetime.now(UTC) - timedelta(days=days)


async def sync_open_prs(
    store: ReviewStore, repo: RepoKey, *, settings: ReviewSettings, force_refresh: bool = False
) -> SyncReport:
    """Folds each of the repo's open PRs' GitHub state back into its candidate.

    Each PR's state is served from the ``pr_states`` cache when its entry is younger
    than :data:`PR_STATE_TTL`, so a status dashboard's background sync never re-hits
    ``gh`` per open PR within the window; ``force_refresh`` (``review sync-prs``)
    bypasses the cache. When ``gh`` is down on a forced or expired refresh, a stale
    cached state is folded in rather than treating the PR as unreachable — only a PR
    with no cached state at all counts unreachable and stays ``pr_open``.

    Args:
        store: The open review store.
        repo: The repo whose ``pr_open`` candidates to sync.
        settings: The reviewer settings supplying ``stale_after_days``.
        force_refresh: When True, ignore the cache and re-fetch every PR from ``gh``.

    Returns:
        The pass's transition counts.
    """
    cutoff = datetime.now(UTC) - PR_STATE_TTL

    async def resolve(url: str) -> PrState | None:
        cached = await store.pr_state_cache(url)
        if not force_refresh and cached is not None and cached.fetched_at >= cutoff:
            return cached.pr
        if (pr := await asyncio.to_thread(gh_pr_state, url)) is not None:
            await store.cache_pr_state(url, pr)
            return pr
        return cached.pr if cached is not None else None

    counts: Counter[str] = Counter()
    rows = await store.candidates(repo, status=CandidateStatus.PR_OPEN)
    states = await asyncio.gather(*(resolve(str(row["pr_url"])) for row in rows))
    for row, pr in zip(rows, states, strict=True):
        candidate_id, url = int(str(row["id"])), str(row["pr_url"])
        match pr:
            case PrState(state="MERGED", merged_at=merged_at):
                await store.transition(candidate_id, CandidateStatus.ACCEPTED)
                logger.bind(
                    candidate_id=candidate_id, transition="pr_open->accepted", url=url, merged_at=merged_at
                ).info("PR merged; candidate accepted")
                counts["accepted"] += 1
            case PrState(state="CLOSED"):
                await store.transition(candidate_id, CandidateStatus.REJECTED)
                logger.bind(candidate_id=candidate_id, transition="pr_open->rejected", url=url).info(
                    "PR closed; candidate rejected"
                )
                counts["rejected"] += 1
            case PrState(state="OPEN") if is_stale(str(row["pr_opened_at"]), days=settings.stale_after_days):
                await store.transition(candidate_id, CandidateStatus.STALE)
                logger.bind(candidate_id=candidate_id, transition="pr_open->stale", url=url).info(
                    "PR stale; candidate slot freed"
                )
                counts["stale"] += 1
            case PrState(state="OPEN"):
                counts["kept"] += 1
            case None:
                logger.bind(url=url).warning("gh pr state unavailable; skipping")
                counts["unreachable"] += 1
            case PrState(state=state):
                logger.bind(url=url, state=state).warning("gh pr state unavailable; skipping")
                counts["unreachable"] += 1
    return SyncReport(
        accepted=counts["accepted"],
        rejected=counts["rejected"],
        stale=counts["stale"],
        unreachable=counts["unreachable"],
        kept=counts["kept"],
    )
