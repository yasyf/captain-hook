"""The review notification seam: transition-fired banners and the failing-streak guard.

Every test overrides the conftest ``client.notify`` stub with a recorder so it can assert the
exact fields a transition or a failing streak would send, and that each fires exactly once.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from captain_hook.helper import client
from captain_hook.helper.client import Lane, NotifyOutcome
from captain_hook.review import notify
from captain_hook.review.repo import RepoKey
from captain_hook.review.store import CandidateKind, CandidateStatus
from captain_hook.review.sync import PrState

if TYPE_CHECKING:
    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

REPO = RepoKey("github.com/yasyf/captain-hook")
PR_URL = "https://github.com/yasyf/captain-hook/pull/12"


@pytest.fixture
def notes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def record(**kwargs: object) -> NotifyOutcome:
        calls.append(kwargs)
        return NotifyOutcome(Lane.socket, ok=True, error=None)

    monkeypatch.setattr(client, "notify", record)
    return calls


def open_candidate(store: ReviewStore, *, rule: str = "guard-rm-rf", title: str | None = "Block force-pushes") -> int:
    cid = store.ensure_candidate(REPO, kind=CandidateKind.CREATE, rule=rule, source_kind="transcript_message")
    store.transition(cid, CandidateStatus.PR_OPEN, pr_url=PR_URL, pr_title=title, pr_opened_at=datetime.now(UTC))
    return cid


def test_pr_open_fires_once_with_exact_content(store: ReviewStore, notes: list[dict[str, object]]) -> None:
    open_candidate(store)
    assert notes == [
        {
            "kind": "pr_open",
            "title": "Block force-pushes",
            "subtitle": "captain-hook",
            "body": "Rule guard-rm-rf opened",
            "url": PR_URL,
            "repo": str(REPO),
        }
    ]


def test_pr_open_title_falls_back(store: ReviewStore, notes: list[dict[str, object]]) -> None:
    open_candidate(store, title=None)
    assert notes[0]["title"] == "Hook PR opened"


def test_merged_flip_fires_pr_merged_once(
    store: ReviewStore, settings: ReviewSettings, notes: list[dict[str, object]]
) -> None:
    cid = open_candidate(store)
    notes.clear()
    store.cache_pr_state(PR_URL, PrState(state="MERGED", merged_at="2026-07-16T00:00:00+00:00"))
    moved = store.transition(cid, CandidateStatus.ACCEPTED, expected_pr_url=PR_URL, expected_generation=1)
    assert moved is True
    assert notes == [
        {
            "kind": "pr_merged",
            "title": "Block force-pushes",
            "subtitle": "captain-hook",
            "body": "Rule guard-rm-rf merged",
            "url": PR_URL,
            "repo": str(REPO),
        }
    ]


def test_closed_and_stale_never_notify(store: ReviewStore, notes: list[dict[str, object]]) -> None:
    cid = open_candidate(store)
    notes.clear()
    store.transition(cid, CandidateStatus.STALE)
    other = store.ensure_candidate(REPO, kind=CandidateKind.CREATE, rule="r2", source_kind="transcript_message")
    store.transition(other, CandidateStatus.REJECTED)
    assert notes == []


def test_cas_loser_does_not_double_notify(store: ReviewStore, notes: list[dict[str, object]]) -> None:
    cid = store.ensure_candidate(REPO, kind=CandidateKind.CREATE, rule="guard-rm-rf", source_kind="transcript_message")
    real = store.store.conn

    class Proxy:
        def __init__(self) -> None:
            self.armed = True

        def execute(self, sql: str, params: object = ()):  # noqa: ANN201 - forwards a sqlite cursor
            cur = real.execute(sql, params)
            if self.armed and sql.startswith("SELECT status, pr_url, generation"):
                self.armed = False
                # A peer commits the same move between this SELECT and the UPDATE, so the caller's
                # UPDATE matches zero rows and its next pass converges as a no-op — and must not notify.
                real.execute(
                    "UPDATE candidates SET status = 'pr_open', pr_url = ?, pr_opened_at = ? WHERE id = ?",
                    (PR_URL, datetime.now(UTC).isoformat(), cid),
                )
            return cur

        def __getattr__(self, name: str) -> object:
            return getattr(real, name)

    store.store.conn = Proxy()  # type: ignore[assignment]
    try:
        moved = store.transition(cid, CandidateStatus.PR_OPEN, pr_url=PR_URL, pr_title="Block force-pushes")
    finally:
        store.store.conn = real
    assert moved is True
    assert notes == []  # the converged loser never reaches the rowcount==1 notify branch


def record_run(store: ReviewStore, *, ok: bool, started: str) -> None:
    store.record_spawn_run(
        f"/t-{started}.jsonl",
        started_at=datetime.fromisoformat(started),
        ok=ok,
        error=None if ok else "BoomError: crashed",
        report_json="{}" if ok else None,
    )


def test_failure_streak_two_silent_three_fires_once(store: ReviewStore, notes: list[dict[str, object]]) -> None:
    record_run(store, ok=False, started="2026-07-15T10:00:00+00:00")
    notify.maybe_notify_failures(store)
    record_run(store, ok=False, started="2026-07-15T10:01:00+00:00")
    notify.maybe_notify_failures(store)
    assert notes == []  # two failures: below the threshold

    record_run(store, ok=False, started="2026-07-15T10:02:00+00:00")
    notify.maybe_notify_failures(store)
    assert len(notes) == 1
    assert notes[0]["kind"] == "review_failure"
    assert notes[0]["url"] is None
    assert notes[0]["body"] == "3 consecutive review runs failed"

    record_run(store, ok=False, started="2026-07-15T10:03:00+00:00")
    notify.maybe_notify_failures(store)
    assert len(notes) == 1  # fourth failure in the same streak stays silent


def test_failure_marker_self_heals_after_success(store: ReviewStore, notes: list[dict[str, object]]) -> None:
    for minute in range(3):
        record_run(store, ok=False, started=f"2026-07-15T10:0{minute}:00+00:00")
    notify.maybe_notify_failures(store)
    assert len(notes) == 1

    record_run(store, ok=True, started="2026-07-15T10:05:00+00:00")
    notify.maybe_notify_failures(store)
    assert len(notes) == 1  # streak reset by the clean run

    for minute in range(6, 9):
        record_run(store, ok=False, started=f"2026-07-15T10:0{minute}:00+00:00")
    notify.maybe_notify_failures(store)
    assert len(notes) == 2  # a fresh streak's new failing_since fires again
