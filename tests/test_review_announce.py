from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from cc_transcript.mining.sourcekind import SourceKind

from captain_hook.review.announce import (
    ANNOUNCE_PREFIX,
    announcement_line,
    collect_announcements,
    pending_announcements,
)
from captain_hook.review.repo import RepoKey
from captain_hook.review.store import REVIEW_EXTENSIONS, REVIEW_SCHEMA, CandidateKind, CandidateStatus, ReviewStore

if TYPE_CHECKING:
    from pathlib import Path

REPO = RepoKey("github.com/yasyf/scratch")
PACK_REPO = RepoKey("github.com/yasyf/captain-hook")
PR_URL = "https://github.com/yasyf/scratch/pull/9"
PR_URL_2 = "https://github.com/yasyf/scratch/pull/10"
PACK_PR_URL = "https://github.com/yasyf/captain-hook/pull/3"


@contextlib.contextmanager
def held_write_lock(path: Path) -> Iterator[None]:
    """Holds a native-engine write transaction on ``path`` for the block's duration.

    The native engine's bundled SQLite does not share in-process locks with stdlib sqlite3, and a
    store binds to the loop it opened on, so the lock is held by a second store on its own thread.
    """
    from cc_transcript.mining.store import FeedbackStore

    ready, release, box = threading.Event(), threading.Event(), {}

    def hold() -> None:
        async def run() -> None:
            async with (
                await FeedbackStore.open(
                    path,
                    REVIEW_SCHEMA,
                    extensions=REVIEW_EXTENSIONS,
                ) as blocker,
                blocker.transaction(),
            ):
                ready.set()
                await asyncio.to_thread(release.wait)

        try:
            asyncio.run(run())
        except BaseException as exc:  # surfaced to the waiting test thread
            box["error"] = exc
        finally:
            ready.set()

    thread = threading.Thread(target=hold)
    thread.start()
    assert ready.wait(timeout=5), "blocker never acquired the write lock"
    try:
        yield
    finally:
        release.set()
        thread.join(timeout=5)
    if "error" in box:
        raise box["error"]


async def create_pr_open(store: ReviewStore, *, repo: RepoKey = REPO, url: str = PR_URL, rule: str = "r") -> int:
    candidate_id = await store.ensure_candidate(
        repo, kind=CandidateKind.CREATE, rule=rule, source_kind=SourceKind("transcript_message")
    )
    await store.transition(candidate_id, CandidateStatus.PR_OPEN, pr_url=url)
    return candidate_id


async def pack_fix_pr_open(store: ReviewStore) -> int:
    candidate_id = await store.ensure_candidate(
        PACK_REPO,
        kind=CandidateKind.FIX,
        rule="misfire",
        source_kind=SourceKind("hook_complaint"),
        target_source_file="captain_hook/packs/general/docs.py",
        target_hook_name="general.docs:nudge_1",
        misfire_class="refire",
        origin_repo_key=REPO,
        pack_name="general",
    )
    await store.transition(candidate_id, CandidateStatus.PR_OPEN, pr_url=PACK_PR_URL)
    return candidate_id


class TestAnnouncementLine:
    @pytest.mark.parametrize(
        ("row", "status", "expected"),
        [
            pytest.param(
                {"pr_url": PR_URL, "origin_repo_key": None, "repo_key": REPO},
                CandidateStatus.PR_OPEN,
                f"{ANNOUNCE_PREFIX} a hook PR is awaiting your review — {PR_URL}",
                id="same_repo_pr_open",
            ),
            pytest.param(
                {
                    "pr_url": PACK_PR_URL,
                    "origin_repo_key": REPO,
                    "repo_key": PACK_REPO,
                    "pack_name": "general",
                    "target_hook_name": "general.docs:nudge_1",
                },
                CandidateStatus.PR_OPEN,
                f"{ANNOUNCE_PREFIX} a fix PR is open against {PACK_REPO} for pack 'general' "
                f"hook general.docs:nudge_1, which misfired here — {PACK_PR_URL}",
                id="cross_repo_pr_open",
            ),
            pytest.param(
                {"pr_url": PR_URL, "origin_repo_key": None, "repo_key": REPO},
                CandidateStatus.ACCEPTED,
                f"{ANNOUNCE_PREFIX} the hook fix PR was merged — {PR_URL}",
                id="accepted_merged",
            ),
            pytest.param(
                {"pr_url": PR_URL, "origin_repo_key": None, "repo_key": REPO},
                CandidateStatus.REJECTED,
                f"{ANNOUNCE_PREFIX} the hook fix PR was closed without merging — {PR_URL}",
                id="rejected_closed",
            ),
            pytest.param(
                {"pr_url": PR_URL, "origin_repo_key": None, "repo_key": REPO},
                CandidateStatus.STALE,
                f"{ANNOUNCE_PREFIX} the hook fix PR has gone quiet with no decision yet — {PR_URL}",
                id="stale_quiet",
            ),
        ],
    )
    def test_line_copy(self, row: dict[str, object], status: CandidateStatus, expected: str) -> None:
        assert announcement_line(row, status) == expected


class TestPendingAnnouncements:
    async def test_same_repo_pr_open_announced_once(self, store: ReviewStore) -> None:
        await create_pr_open(store)
        assert await pending_announcements(store, REPO) == [
            f"{ANNOUNCE_PREFIX} a hook PR is awaiting your review — {PR_URL}"
        ]
        assert await pending_announcements(store, REPO) == []

    async def test_cross_repo_pack_fix_matched_by_origin(self, store: ReviewStore) -> None:
        await pack_fix_pr_open(store)
        [line] = await pending_announcements(store, REPO)
        assert line == (
            f"{ANNOUNCE_PREFIX} a fix PR is open against {PACK_REPO} for pack 'general' "
            f"hook general.docs:nudge_1, which misfired here — {PACK_PR_URL}"
        )
        assert await pending_announcements(store, PACK_REPO) == []

    async def test_status_change_reannounces(self, store: ReviewStore) -> None:
        candidate_id = await create_pr_open(store)
        assert len(await pending_announcements(store, REPO)) == 1
        await store.transition(candidate_id, CandidateStatus.ACCEPTED)
        assert await pending_announcements(store, REPO) == [f"{ANNOUNCE_PREFIX} the hook fix PR was merged — {PR_URL}"]

    @pytest.mark.parametrize("status", [CandidateStatus.WATCHING, CandidateStatus.REJECTED])
    async def test_no_pr_url_never_announced(self, store: ReviewStore, status: CandidateStatus) -> None:
        candidate_id = await store.ensure_candidate(
            REPO, kind=CandidateKind.CREATE, rule="r", source_kind=SourceKind("transcript_message")
        )
        if status is CandidateStatus.REJECTED:
            await store.transition(candidate_id, CandidateStatus.REJECTED)
        assert await pending_announcements(store, REPO) == []

    async def test_mark_announced_persists_across_reopen(self, store: ReviewStore) -> None:
        # A brand-new store baselines nothing, so a freshly-transitioned candidate is
        # announced; a re-open of the same DB must still see it marked (single write path).
        await create_pr_open(store)
        assert len(await pending_announcements(store, REPO)) == 1
        rows = await store.candidates(REPO)
        assert rows[0]["announced_status"] == CandidateStatus.PR_OPEN


class TestCollectAnnouncements:
    @pytest.fixture
    def review_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        db = tmp_path / "review.db"
        monkeypatch.setenv("HOOKS_REVIEW_DB_PATH", str(db))
        return db

    async def _seed(self, db: Path, *, watching: bool = True) -> None:
        async with await ReviewStore.open(db) as store:
            await create_pr_open(store)
            if watching:
                await store.enable(REPO)

    def test_returns_line_for_the_session_repo(self, review_db: Path, git_repo: Path) -> None:

        asyncio.run(self._seed(review_db))
        message = collect_announcements(git_repo)
        assert message is not None
        assert "awaiting your review" in message

    def test_silent_when_no_review_db(self, review_db: Path, git_repo: Path) -> None:
        assert not review_db.exists()
        assert collect_announcements(git_repo) is None

    def test_silent_when_repo_not_watching(self, review_db: Path, git_repo: Path) -> None:

        asyncio.run(self._seed(review_db, watching=False))
        assert collect_announcements(git_repo) is None

    def test_silent_outside_a_git_repo(self, review_db: Path, tmp_path: Path) -> None:

        asyncio.run(self._seed(review_db))
        assert collect_announcements(tmp_path) is None

    def test_silent_when_spawned(self, review_db: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:

        asyncio.run(self._seed(review_db))
        monkeypatch.setenv("CAPT_HOOK_SPAWNED", "1")
        assert collect_announcements(git_repo) is None

    def test_write_lock_fails_fast_then_retries_when_released(self, review_db: Path, git_repo: Path) -> None:
        # Under a held write lock, collect_announcements fails the mark fast (busy_timeout=0), returns None,
        # and leaves the announcement pending for the next uncontended start.
        import time

        asyncio.run(self._seed(review_db))
        with held_write_lock(review_db):
            start = time.monotonic()
            assert collect_announcements(git_repo) is None
            assert time.monotonic() - start < 1.0
        message = collect_announcements(git_repo)
        assert message is not None
        assert "awaiting your review" in message

    async def _seed_two(self, db: Path) -> None:
        async with await ReviewStore.open(db) as store:
            await create_pr_open(store, url=PR_URL, rule="r1")
            await create_pr_open(store, url=PR_URL_2, rule="r2")
            await store.enable(REPO)

    def test_contention_marks_no_row_then_all_resurface(self, review_db: Path, git_repo: Path) -> None:
        # A contended write lock must mark neither of two pending rows: the single BEGIN IMMEDIATE fails
        # before any mark, so both lines resurface together once released (proving nothing was stranded).
        asyncio.run(self._seed_two(review_db))
        with held_write_lock(review_db):
            assert collect_announcements(git_repo) is None
        message = collect_announcements(git_repo)
        assert message is not None
        assert message.count(ANNOUNCE_PREFIX) == 2
        assert collect_announcements(git_repo) is None


class TestSessionStartDispatch:
    def test_announcement_lands_in_additional_context(
        self, tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        from captain_hook.dispatch import dispatch
        from captain_hook.events import SessionStartEvent
        from captain_hook.loader import register_pr_announcements
        from captain_hook.types import Event
        from tests.helpers import build_ctx

        db = tmp_path / "review.db"
        monkeypatch.setenv("HOOKS_REVIEW_DB_PATH", str(db))

        async def seed() -> None:
            async with await ReviewStore.open(db) as store:
                await create_pr_open(store)
                await store.enable(REPO)

        asyncio.run(seed())

        register_pr_announcements()
        evt = SessionStartEvent(_raw={"source": "startup"}, ctx=build_ctx(project_root=git_repo))
        output = dispatch(Event.SessionStart, evt, session_dir=tmp_path / "session")
        assert output is not None
        assert "awaiting your review" in output["hookSpecificOutput"]["additionalContext"]

    def test_no_output_when_repo_has_no_review_db(
        self, tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from captain_hook.dispatch import dispatch
        from captain_hook.events import SessionStartEvent
        from captain_hook.loader import register_pr_announcements
        from captain_hook.types import Event
        from tests.helpers import build_ctx

        monkeypatch.setenv("HOOKS_REVIEW_DB_PATH", str(tmp_path / "absent.db"))
        register_pr_announcements()
        evt = SessionStartEvent(_raw={"source": "startup"}, ctx=build_ctx(project_root=git_repo))
        assert dispatch(Event.SessionStart, evt, session_dir=tmp_path / "session") is None


class TestDiscoverRegistration:
    def test_discover_registers_announcer_unconditionally(self, tmp_path: Path) -> None:
        # A discovery pass over a repo with no settings still registers the announcer;
        # collect_announcements owns every gate at fire time.
        from captain_hook.app import _state
        from captain_hook.cli import CliState
        from captain_hook.types import Event

        (hooks := tmp_path / ".claude" / "hooks").mkdir(parents=True)
        (hooks / "__init__.py").write_text("")
        CliState(root=tmp_path, hooks=str(hooks)).discover()
        assert any(h.name == "announce_pr_status" and Event.SessionStart in h.spec.events for h in _state.hooks)
