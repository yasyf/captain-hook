"""PR lifecycle sync: fold each open PR's GitHub state back into its candidate.

A merged PR accepts its candidate, a closed PR rejects it, and a PR open past
``stale_after_days`` goes stale — freeing its slot in its kind's pool (``max_open_prs``
for create, ``max_open_prs_fix`` for fix). GitHub reports ``MERGED`` only for merges it
performed itself: the Graphite merge queue lands its own rebased commit on the base
branch and then closes the PR, which GitHub records as a plain close with no closer.
So a ``CLOSED`` PR is accepted, not rejected, when a commit landed it — the close
event's closer commit, or a base-branch commit inside :data:`LANDING_WINDOW_BEFORE` /
:data:`LANDING_WINDOW_AFTER` of the close whose headline cites the PR number — and a
close whose landing check could not be completed leaves the candidate untouched. A
``gh`` failure (not installed, not authenticated, network down) is logged and skipped
so the detached child never dies on it.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple

from loguru import logger

from captain_hook.review.store import CandidateStatus

if TYPE_CHECKING:
    from captain_hook.review.repo import RepoKey
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

PR_STATE_TTL = timedelta(minutes=15)
LANDING_WINDOW_BEFORE = timedelta(hours=3)
LANDING_WINDOW_AFTER = timedelta(minutes=15)
LANDING_PAGE = 100
LANDING_MAX_PAGES = 5
PR_QUERY = """
query($url: URI!) {
  resource(url: $url) {
    ... on PullRequest {
      number state mergedAt closedAt baseRefName
      repository { owner { login } name }
      timelineItems(last: 1, itemTypes: CLOSED_EVENT) {
        nodes { ... on ClosedEvent { closer { __typename ... on Commit { committedDate } } } }
      }
    }
  }
}
"""
HISTORY_QUERY = f"""
query($owner: String!, $name: String!, $ref: String!, $since: GitTimestamp!, $until: GitTimestamp!, $after: String) {{
  repository(owner: $owner, name: $name) {{
    ref(qualifiedName: $ref) {{
      target {{
        ... on Commit {{
          history(since: $since, until: $until, first: {LANDING_PAGE}, after: $after) {{
            pageInfo {{ hasNextPage endCursor }}
            nodes {{ committedDate message }}
          }}
        }}
      }}
    }}
  }}
}}
"""


@dataclass(frozen=True, slots=True)
class PrState:
    """A PR's GitHub state as of one sync pass.

    Attributes:
        state: The GitHub state string — ``MERGED``, ``CLOSED``, or ``OPEN``.
        merged_at: The merge timestamp when GitHub performed the merge, else ``None``.
        landed_at: For a ``CLOSED`` PR, when a commit landed it on the base branch — the
            close event's closer commit, or the base-branch commit citing the PR number
            near the close — else ``None``.
        landing_checked: Whether the landing check ran to completion; ``False`` when it
            errored or the history window overflowed, so the close must not be read as
            a rejection.
    """

    state: str
    merged_at: str | None
    landed_at: str | None = None
    landing_checked: bool = True


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
        accepted: How many candidates moved to accepted (PR merged or landed by commit).
        rejected: How many candidates moved to rejected (PR closed without landing).
        stale: How many candidates went stale (PR open too long).
        unreachable: How many PRs ``gh`` could not report on this pass.
        kept: How many PRs left their candidate untouched — still open, or closed with an
            inconclusive landing check.
    """

    accepted: int
    rejected: int
    stale: int
    unreachable: int
    kept: int = 0


def gh_graphql(query: str, **variables: str) -> dict[str, Any] | None:
    argv = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        argv += ["-F", f"{key}={value}"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)["data"]
    except (ValueError, KeyError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def cites_pull(message: str, number: int) -> bool:
    return re.search(rf"(?<![\d#])#{number}(?!\d)", message.partition("\n")[0]) is not None


def landing_commit(pr: dict[str, Any]) -> tuple[str | None, bool]:
    closer = next((node.get("closer") for node in pr["timelineItems"]["nodes"]), None)
    if closer and closer.get("__typename") == "Commit":
        return str(closer["committedDate"]), True
    closed_at = datetime.fromisoformat(str(pr["closedAt"]))
    variables = {
        "owner": str(pr["repository"]["owner"]["login"]),
        "name": str(pr["repository"]["name"]),
        "ref": f"refs/heads/{pr['baseRefName']}",
        "since": (closed_at - LANDING_WINDOW_BEFORE).isoformat(),
        "until": (closed_at + LANDING_WINDOW_AFTER).isoformat(),
    }
    number, after = int(pr["number"]), None
    for _ in range(LANDING_MAX_PAGES):
        if (data := gh_graphql(HISTORY_QUERY, **variables, **({"after": after} if after else {}))) is None:
            return None, False
        try:
            history = data["repository"]["ref"]["target"]["history"]
            landed = next(
                (str(node["committedDate"]) for node in history["nodes"] if cites_pull(str(node["message"]), number)),
                None,
            )
            if landed is not None or not history["pageInfo"]["hasNextPage"]:
                return landed, True
            after = str(history["pageInfo"]["endCursor"])
        except (KeyError, TypeError):
            return None, False
    return None, False


def gh_pr_state(url: str) -> PrState | None:
    if (data := gh_graphql(PR_QUERY, url=url)) is None:
        return None
    try:
        pr = data["resource"]
        state, merged_at = str(pr["state"]), pr["mergedAt"]
        if state != "CLOSED" or merged_at is not None:
            return PrState(state=state, merged_at=merged_at)
        landed_at, checked = landing_commit(pr)
    except (KeyError, TypeError, ValueError):
        return None
    return PrState(state=state, merged_at=None, landed_at=landed_at, landing_checked=checked)


def is_stale(opened_at: str, *, days: int) -> bool:
    return datetime.fromisoformat(opened_at) < datetime.now(UTC) - timedelta(days=days)


async def sync_open_prs(
    store: ReviewStore, repo: RepoKey, *, settings: ReviewSettings, force_refresh: bool = False
) -> SyncReport:
    """Folds each of the repo's open PRs' GitHub state back into its candidate.

    Each PR's state is served from the ``pr_states`` cache when its entry is younger
    than :data:`PR_STATE_TTL`, so a status dashboard's background sync never re-hits
    ``gh`` per open PR within the window; ``force_refresh`` (``review sync-prs``)
    bypasses the cache. When ``gh`` is down on a forced or expired refresh, the PR
    counts unreachable and stays ``pr_open`` — a stale cached state is never folded
    into a lifecycle transition, so an outage can never move a candidate on its own.
    The cache carries no landing evidence, so a cached ``CLOSED`` is inconclusive and
    keeps its candidate until a fresh fetch settles it.

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
        return None

    counts: Counter[str] = Counter()
    rows = await store.candidates(repo, status=CandidateStatus.PR_OPEN)
    states = await asyncio.gather(*(resolve(str(row["pr_url"])) for row in rows))
    for row, pr in zip(rows, states, strict=True):
        # The snapshotted url + generation arm transition()'s anti-ABA guard: a result for a PR
        # the candidate has since replaced (accepted, reopened, re-PR'd) finds the row changed
        # and no-ops (returns False, counted kept) rather than resolving the new generation.
        candidate_id, url, generation = int(str(row["id"])), str(row["pr_url"]), int(str(row["generation"]))
        match pr:
            case PrState(state="MERGED", merged_at=merged_at):
                if await store.transition(
                    candidate_id,
                    CandidateStatus.ACCEPTED,
                    resolved_at=datetime.fromisoformat(merged_at) if merged_at else None,
                    expected_pr_url=url,
                    expected_generation=generation,
                ):
                    logger.bind(
                        candidate_id=candidate_id, transition="pr_open->accepted", url=url, merged_at=merged_at
                    ).info("PR merged; candidate accepted")
                    counts["accepted"] += 1
                else:
                    counts["kept"] += 1
            case PrState(state="CLOSED", landed_at=str() as landed_at):
                if await store.transition(
                    candidate_id,
                    CandidateStatus.ACCEPTED,
                    resolved_at=datetime.fromisoformat(landed_at),
                    expected_pr_url=url,
                    expected_generation=generation,
                ):
                    logger.bind(
                        candidate_id=candidate_id, transition="pr_open->accepted", url=url, landed_at=landed_at
                    ).info("PR landed by commit; candidate accepted")
                    counts["accepted"] += 1
                else:
                    counts["kept"] += 1
            case PrState(state="CLOSED", landing_checked=True):
                if await store.transition(
                    candidate_id, CandidateStatus.REJECTED, expected_pr_url=url, expected_generation=generation
                ):
                    logger.bind(candidate_id=candidate_id, transition="pr_open->rejected", url=url).info(
                        "PR closed without landing; candidate rejected"
                    )
                    counts["rejected"] += 1
                else:
                    counts["kept"] += 1
            case PrState(state="CLOSED"):
                logger.bind(candidate_id=candidate_id, url=url).warning(
                    "PR closed but the landing check was inconclusive; candidate kept"
                )
                counts["kept"] += 1
            case PrState(state="OPEN") if is_stale(str(row["pr_opened_at"]), days=settings.stale_after_days):
                if await store.transition(
                    candidate_id, CandidateStatus.STALE, expected_pr_url=url, expected_generation=generation
                ):
                    logger.bind(candidate_id=candidate_id, transition="pr_open->stale", url=url).info(
                        "PR stale; candidate slot freed"
                    )
                    counts["stale"] += 1
                else:
                    counts["kept"] += 1
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
