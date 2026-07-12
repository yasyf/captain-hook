"""SessionStart PR announcements: tell the user, once, about each candidate's changed PR outcome.

The detached reviewer opens, merges, and closes hook PRs behind the user's back, so a
session start surfaces every candidate whose PR outcome has moved since it was last
announced — the PR that just opened for review, the follow-up that merged or closed, or
the one that went quiet. Each change is announced exactly once via ``announced_status``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from captain_hook.review.store import CandidateStatus

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from captain_hook.review.repo import RepoKey
    from captain_hook.review.store import ReviewStore

ANNOUNCE_PREFIX = "captain-hook review:"

ANNOUNCE_STATUSES = frozenset(
    {CandidateStatus.PR_OPEN, CandidateStatus.ACCEPTED, CandidateStatus.REJECTED, CandidateStatus.STALE}
)


def announcement_line(row: Mapping[str, object], status: CandidateStatus) -> str:
    url = str(row["pr_url"])
    match status:
        case CandidateStatus.PR_OPEN if row["origin_repo_key"]:
            return (
                f"{ANNOUNCE_PREFIX} a fix PR is open against {row['repo_key']} for pack "
                f"'{row['pack_name']}' hook {row['target_hook_name']}, which misfired here — {url}"
            )
        case CandidateStatus.PR_OPEN:
            return f"{ANNOUNCE_PREFIX} a hook PR is awaiting your review — {url}"
        case CandidateStatus.ACCEPTED:
            return f"{ANNOUNCE_PREFIX} the hook fix PR was merged — {url}"
        case CandidateStatus.REJECTED:
            return f"{ANNOUNCE_PREFIX} the hook fix PR was closed without merging — {url}"
        case CandidateStatus.STALE:
            return f"{ANNOUNCE_PREFIX} the hook fix PR has gone quiet with no decision yet — {url}"
        case _:
            raise ValueError(f"{status} is not an announceable status")


async def pending_announcements(store: ReviewStore, repo: RepoKey) -> list[str]:
    """Returns one line per candidate whose PR outcome changed since it was last announced, marking each.

    Reads the repo's candidates (matched by PR-target or misfire-origin repo), keeps
    those in a PR-bearing state (:data:`ANNOUNCE_STATUSES`) whose ``announced_status``
    lags the current ``status`` and that carry a ``pr_url`` — so a judged-and-dropped
    ``rejected`` create candidate with no PR is never announced — then stamps
    ``announced_status`` through :meth:`ReviewStore.mark_announced`.

    Args:
        store: The open review store.
        repo: The session's repo; a candidate matches on ``repo_key`` or ``origin_repo_key``.
    """
    lines: list[str] = []
    for row in await store.candidates(repo):
        status = CandidateStatus(str(row["status"]))
        if status not in ANNOUNCE_STATUSES or row["announced_status"] == status or not row["pr_url"]:
            continue
        lines.append(announcement_line(row, status))
        await store.mark_announced(int(str(row["id"])), status)
    return lines


def collect_announcements(root: Path | None) -> str | None:
    """Bridges :func:`pending_announcements` for the sync SessionStart hook: guards, then joins the lines.

    Registered unconditionally at discovery, so every gate lives here. Announces nothing
    — returning ``None`` — inside a spawned reviewer run (``CAPT_HOOK_SPAWNED``, else the
    brain would consume the user's announcements), outside a git repo with an ``origin``
    remote, when no review database exists yet, or when the repo is not being watched — so
    a session start never opens or creates a store in an unrelated repo and stays silent in
    a repo the reviewer isn't tracking.

    The announcer's ``mark_announced`` write runs with ``busy_timeout = 0``, so a
    detached reviewer holding a write lock fails it immediately (``SQLITE_BUSY``)
    rather than stalling the synchronous hook for SQLite's default five seconds; the
    outcome is announced at the next uncontended session start instead.

    Args:
        root: The session's project root; the repo is resolved from it.
    """
    import asyncio
    import os
    import sqlite3

    from captain_hook.review.pipeline import SPAWNED_ENV
    from captain_hook.review.repo import repo_key
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

    if os.environ.get(SPAWNED_ENV):
        return None
    if (repo := repo_key(root)) is None:
        return None
    if not (db_path := ReviewSettings().db_path).exists():
        return None

    async def go() -> list[str]:
        async with await ReviewStore.open(db_path) as store:
            await store.store.conn.execute("PRAGMA busy_timeout = 0")
            if not await store.watching(repo):
                return []
            return await pending_announcements(store, repo)

    try:
        lines = asyncio.run(go())
    except sqlite3.OperationalError as exc:
        if exc.sqlite_errorcode != sqlite3.SQLITE_BUSY:
            raise
        return None
    return "\n".join(lines) if lines else None
