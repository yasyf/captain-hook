"""The ``~/.capt-hook/status.json`` snapshot writer — the single codepath the widget reads from.

:func:`build_snapshot` projects the review store into the frozen ``schema_version`` 1 shape
(``tests/fixtures/status-json-v1.golden.json``): per-repo counts precomputed through
:func:`captain_hook.review.dashboard.stage_of` so the Swift widget stays dumb, the open PRs
newest-first and capped, and the reviewer's spawn/judge health. :func:`write_status` is the
only writer — it renders the compact canonical JSON and swaps it into place atomically
(tempfile in the target dir, then :func:`os.replace`), because the widget watches the
directory for the rename.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import tempfile
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from captain_hook.helper.client import status_path
from captain_hook.review import dashboard
from captain_hook.review.dashboard import Stage
from captain_hook.review.repo import RepoKey
from captain_hook.review.status import CandidateStatus

if TYPE_CHECKING:
    from pathlib import Path

    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import CandidateView, ReviewStore

SCHEMA_VERSION = 1
DIST_NAME = "capt-hook"
OPEN_PR_CAP = 20


def capt_hook_version() -> str:
    """The installed ``capt-hook`` distribution version, stamped into every snapshot."""
    return importlib.metadata.version(DIST_NAME)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso_z(value: datetime | str) -> str:
    """Normalizes a UTC timestamp to the ``...Z`` ISO 8601 form the snapshot pins (whole seconds)."""
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _open_pr_entry(view: CandidateView) -> dict[str, object]:
    row = view.row
    return {
        "candidate_id": int(str(row["id"])),
        "rule": str(row["rule"]),
        "kind": str(row["candidate_kind"]),
        "title": str(row["pr_title"]) if row["pr_title"] else dashboard.pr_description(view),
        "url": str(row["pr_url"]) if row["pr_url"] else "",
        "opened_at": _iso_z(str(row["pr_opened_at"] or row["updated_at"])),
    }


async def _repo_entry(store: ReviewStore, repo: dict[str, object], *, settings: ReviewSettings) -> dict[str, object]:
    repo_key = str(repo["repo_key"])
    views = await store.overview(RepoKey(repo_key), settings=settings)
    counts = Counter(dashboard.stage_of(view) for view in views)
    open_prs = sorted(
        (view for view in views if CandidateStatus(str(view.row["status"])) is CandidateStatus.PR_OPEN),
        key=lambda view: str(view.row["pr_opened_at"] or ""),
        reverse=True,
    )
    return {
        "key": repo_key,
        "name": short_name(repo_key),
        "watching": bool(repo["watching"]),
        "counts": {
            "watching": counts[Stage.WATCHING],
            "eligible": counts[Stage.ELIGIBLE],
            "pr_open": counts[Stage.PR_OPEN],
            "accepted": counts[Stage.ACCEPTED],
            "rejected": counts[Stage.REJECTED],
            "stale": counts[Stage.STALE],
        },
        "open_prs": [_open_pr_entry(view) for view in open_prs[:OPEN_PR_CAP]],
    }


def short_name(repo_key: str) -> str:
    """The repo's bare name (``captain-hook`` from ``github.com/yasyf/captain-hook``)."""
    return repo_key.rsplit("/", 1)[-1]


async def build_snapshot(store: ReviewStore, *, settings: ReviewSettings) -> dict[str, object]:
    """Projects the review store into the frozen ``status.json`` schema — the pure snapshot build.

    Args:
        store: The open review store.
        settings: The thresholds the per-repo counts are evaluated under.
    """
    spawn = await store.spawn_health()
    judge = await store.judge_health()
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso_z(_utcnow()),
        "capt_hook_version": capt_hook_version(),
        "repos": [await _repo_entry(store, repo, settings=settings) for repo in await store.repos()],
        "health": {
            "ok": spawn.consecutive_failures == 0,
            "consecutive_failures": spawn.consecutive_failures,
            "failing_since": _iso_z(spawn.failing_since) if spawn.failing_since else None,
            "last_run_at": _iso_z(str(spawn.last["finished_at"])) if spawn.last else None,
            "judge_pending": judge.pending,
        },
    }


async def write_status(store: ReviewStore, *, settings: ReviewSettings) -> Path:
    """Atomically writes the snapshot to :func:`status_path` — the single ``status.json`` writer.

    Renders the compact canonical JSON, writes it to a tempfile in the target directory, then
    :func:`os.replace`s it into place so the widget's directory watch sees one atomic rename.
    """
    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(await build_snapshot(store, settings=settings), separators=(",", ":"), ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".status-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, str(path))
    except BaseException:
        os.unlink(tmp)
        raise
    return path
