"""Desktop notifications for review lifecycle events — the single bridge to the helper.

A candidate's PR opening or merging, and a failing reviewer streak, each become a native
banner via :mod:`captain_hook.helper.client`. :func:`notify_transition` is the seam
:meth:`ReviewStore.transition` fires on a real status write (its ``rowcount == 1`` branch, so
a converged compare-and-swap loser never double-fires); :func:`maybe_notify_failures` fires
once per failing streak from the spawn recorder.

The failing-streak marker (``notified_failing_since`` in ``review_meta``) self-heals: a clean
run resets ``failing_since`` to a fresh timestamp, so the next streak's stamp differs from the
one already announced and fires again — it deliberately does **not** reuse ``announced_status``,
which the SessionStart announcer owns.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from captain_hook.helper import client
from captain_hook.review.status import CandidateStatus

if TYPE_CHECKING:
    from collections.abc import Mapping

    from captain_hook.review.store import ReviewStore, SpawnHealth

NOTIFY_STATUSES = frozenset({CandidateStatus.PR_OPEN, CandidateStatus.ACCEPTED})
FAILURE_STREAK_THRESHOLD = 3
NOTIFIED_FAILING_KEY = "notified_failing_since"


def short_repo(repo_key: str) -> str:
    """The repo's bare name (``captain-hook`` from ``github.com/yasyf/captain-hook``), the banner subtitle."""
    return repo_key.rsplit("/", 1)[-1]


def pr_open_content(row: Mapping[str, object]) -> dict[str, object]:
    """The ``pr_open`` notification fields for a candidate row (title falls back to a generic line)."""
    return {
        "kind": "pr_open",
        "title": str(row["pr_title"]) if row["pr_title"] else "Hook PR opened",
        "subtitle": short_repo(str(row["repo_key"])),
        "body": f"Rule {row['rule']} opened",
        "url": str(row["pr_url"]),
        "repo": str(row["repo_key"]),
    }


def pr_merged_content(row: Mapping[str, object]) -> dict[str, object]:
    """The ``pr_merged`` notification fields for a candidate row whose PR just merged."""
    return {
        "kind": "pr_merged",
        "title": str(row["pr_title"]) if row["pr_title"] else "Hook PR merged",
        "subtitle": short_repo(str(row["repo_key"])),
        "body": f"Rule {row['rule']} merged",
        "url": str(row["pr_url"]),
        "repo": str(row["repo_key"]),
    }


def review_failure_content(health: SpawnHealth) -> dict[str, object]:
    """The ``review_failure`` notification fields — no URL; the reviewer failure is not a PR."""
    plural = "" if health.consecutive_failures == 1 else "s"
    return {
        "kind": "review_failure",
        "title": "Captain Hook reviewer failing",
        "subtitle": None,
        "body": f"{health.consecutive_failures} consecutive review run{plural} failed",
        "url": None,
        "repo": None,
    }


def _optional(value: object) -> str | None:
    return None if value is None else str(value)


def _send(content: Mapping[str, object]) -> None:
    outcome = client.notify(
        kind=str(content["kind"]),
        title=str(content["title"]),
        subtitle=_optional(content["subtitle"]),
        body=_optional(content["body"]),
        url=_optional(content["url"]),
        repo=_optional(content["repo"]),
    )
    if not outcome.ok:
        logger.warning(
            "capt-hook notification not delivered", kind=content["kind"], lane=str(outcome.lane), error=outcome.error
        )


def notify_transition(row: Mapping[str, object], to: CandidateStatus) -> None:
    """Fires the banner for a candidate that just entered ``to`` — ``pr_open`` or ``pr_merged``."""
    match to:
        case CandidateStatus.PR_OPEN:
            _send(pr_open_content(row))
        case CandidateStatus.ACCEPTED:
            _send(pr_merged_content(row))


def maybe_notify_failures(store: ReviewStore) -> None:
    """Fires one ``review_failure`` banner per failing streak once it reaches :data:`FAILURE_STREAK_THRESHOLD`.

    The streak's ``failing_since`` stamp is recorded in ``review_meta`` so a still-failing streak
    stays silent; a clean run mints a fresh stamp for the next streak, so the marker self-heals.
    """
    health = store.spawn_health()
    if health.consecutive_failures < FAILURE_STREAK_THRESHOLD:
        return
    if store.meta(NOTIFIED_FAILING_KEY) == health.failing_since:
        return
    _send(review_failure_content(health))
    store.set_meta(NOTIFIED_FAILING_KEY, health.failing_since)
