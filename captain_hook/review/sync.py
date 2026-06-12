"""PR lifecycle sync: fold each open PR's GitHub state back into its candidate.

A merged PR accepts its candidate, a closed PR rejects it, and a PR open past
``stale_after_days`` goes stale — freeing its slot under ``max_open_prs``. A
``gh`` failure (not installed, not authenticated, network down) is logged and
skipped so the detached child never dies on it.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger

from captain_hook.review.store import CandidateStatus

if TYPE_CHECKING:
    from captain_hook.review.repo import RepoKey
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

GH_TIMEOUT = 30


@dataclass(frozen=True, slots=True)
class SyncReport:
    """The outcome of one PR sync pass.

    Attributes:
        accepted: How many candidates moved to accepted (PR merged).
        rejected: How many candidates moved to rejected (PR closed).
        stale: How many candidates went stale (PR open too long).
        unreachable: How many PRs ``gh`` could not report on this pass.
    """

    accepted: int
    rejected: int
    stale: int
    unreachable: int


def gh_pr_state(url: str) -> str | None:
    try:
        proc = subprocess.run(
            ["gh", "pr", "view", url, "--json", "state"], capture_output=True, text=True, timeout=GH_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return str(json.loads(proc.stdout)["state"])
    except (ValueError, KeyError):
        return None


def is_stale(opened_at: str, *, days: int) -> bool:
    return datetime.fromisoformat(opened_at) < datetime.now(UTC) - timedelta(days=days)


async def sync_open_prs(store: ReviewStore, repo: RepoKey, *, settings: ReviewSettings) -> SyncReport:
    """Folds each of the repo's open PRs' GitHub state back into its candidate.

    Args:
        store: The open review store.
        repo: The repo whose ``pr_open`` candidates to sync.
        settings: The reviewer settings supplying ``stale_after_days``.

    Returns:
        The pass's transition counts.
    """
    counts: Counter[str] = Counter()
    for row in await store.candidates(repo, status=CandidateStatus.PR_OPEN):
        candidate_id, url = int(str(row["id"])), str(row["pr_url"])
        match gh_pr_state(url):
            case "MERGED":
                await store.transition(candidate_id, CandidateStatus.ACCEPTED)
                counts["accepted"] += 1
            case "CLOSED":
                await store.transition(candidate_id, CandidateStatus.REJECTED)
                counts["rejected"] += 1
            case "OPEN" if is_stale(str(row["pr_opened_at"]), days=settings.stale_after_days):
                await store.transition(candidate_id, CandidateStatus.STALE)
                counts["stale"] += 1
            case "OPEN":
                pass
            case state:
                logger.bind(url=url, state=state).warning("gh pr state unavailable; skipping")
                counts["unreachable"] += 1
    return SyncReport(
        accepted=counts["accepted"],
        rejected=counts["rejected"],
        stale=counts["stale"],
        unreachable=counts["unreachable"],
    )
