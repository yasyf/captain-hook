from __future__ import annotations

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
from captain_hook.review.store import CandidateKind, CandidateStatus, ReviewStore

if TYPE_CHECKING:
    from pathlib import Path

REPO = RepoKey("github.com/yasyf/scratch")
PACK_REPO = RepoKey("github.com/yasyf/captain-hook")
PR_URL = "https://github.com/yasyf/scratch/pull/9"
PR_URL_2 = "https://github.com/yasyf/scratch/pull/10"
PACK_PR_URL = "https://github.com/yasyf/captain-hook/pull/3"


def create_pr_open(store: ReviewStore, *, repo: RepoKey = REPO, url: str = PR_URL, rule: str = "r") -> int:
    candidate_id = store.ensure_candidate(
        repo, kind=CandidateKind.CREATE, rule=rule, source_kind=SourceKind("transcript_message")
    )
    store.transition(candidate_id, CandidateStatus.PR_OPEN, pr_url=url)
    return candidate_id


def pack_fix_pr_open(store: ReviewStore) -> int:
    candidate_id = store.ensure_candidate(
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
    store.transition(candidate_id, CandidateStatus.PR_OPEN, pr_url=PACK_PR_URL)
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
    def test_same_repo_pr_open_announced_once(self, store: ReviewStore) -> None:
        create_pr_open(store)
        assert pending_announcements(store, REPO) == [
            f"{ANNOUNCE_PREFIX} a hook PR is awaiting your review — {PR_URL}"
        ]
        assert pending_announcements(store, REPO) == []

    def test_cross_repo_pack_fix_matched_by_origin(self, store: ReviewStore) -> None:
        pack_fix_pr_open(store)
        [line] = pending_announcements(store, REPO)
        assert line == (
            f"{ANNOUNCE_PREFIX} a fix PR is open against {PACK_REPO} for pack 'general' "
            f"hook general.docs:nudge_1, which misfired here — {PACK_PR_URL}"
        )
        assert pending_announcements(store, PACK_REPO) == []

    def test_status_change_reannounces(self, store: ReviewStore) -> None:
        candidate_id = create_pr_open(store)
        assert len(pending_announcements(store, REPO)) == 1
        store.transition(candidate_id, CandidateStatus.ACCEPTED)
        assert pending_announcements(store, REPO) == [f"{ANNOUNCE_PREFIX} the hook fix PR was merged — {PR_URL}"]

    @pytest.mark.parametrize("status", [CandidateStatus.WATCHING, CandidateStatus.REJECTED])
    def test_no_pr_url_never_announced(self, store: ReviewStore, status: CandidateStatus) -> None:
        candidate_id = store.ensure_candidate(
            REPO, kind=CandidateKind.CREATE, rule="r", source_kind=SourceKind("transcript_message")
        )
        if status is CandidateStatus.REJECTED:
            store.transition(candidate_id, CandidateStatus.REJECTED)
        assert pending_announcements(store, REPO) == []

    def test_mark_announced_persists_across_reopen(self, store: ReviewStore) -> None:
        # A brand-new store baselines nothing, so a freshly-transitioned candidate is
        # announced; a re-open of the same DB must still see it marked (single write path).
        create_pr_open(store)
        assert len(pending_announcements(store, REPO)) == 1
        rows = store.candidates(REPO)
        assert rows[0]["announced_status"] == CandidateStatus.PR_OPEN


class TestCollectAnnouncements:
    @pytest.fixture
    def review_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        db = tmp_path / "review.db"
        monkeypatch.setenv("HOOKS_REVIEW_DB_PATH", str(db))
        return db

    def _seed(self, db: Path, *, watching: bool = True) -> None:
        with ReviewStore.open(db) as store:
            create_pr_open(store)
            if watching:
                store.enable(REPO)

    def test_returns_line_for_the_session_repo(self, review_db: Path, git_repo: Path) -> None:

        self._seed(review_db)
        message = collect_announcements(git_repo)
        assert message is not None
        assert "awaiting your review" in message

    def test_silent_when_no_review_db(self, review_db: Path, git_repo: Path) -> None:
        assert not review_db.exists()
        assert collect_announcements(git_repo) is None

    def test_silent_when_repo_not_watching(self, review_db: Path, git_repo: Path) -> None:

        self._seed(review_db, watching=False)
        assert collect_announcements(git_repo) is None

    def test_silent_outside_a_git_repo(self, review_db: Path, tmp_path: Path) -> None:

        self._seed(review_db)
        assert collect_announcements(tmp_path) is None

    def test_silent_when_spawned(self, review_db: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:

        self._seed(review_db)
        monkeypatch.setenv("CAPT_HOOK_SPAWNED", "1")
        assert collect_announcements(git_repo) is None

    def test_write_lock_fails_fast_then_retries_when_released(self, review_db: Path, git_repo: Path) -> None:
        # A detached reviewer holding a write lock must not stall the synchronous SessionStart hook:
        # collect_announcements fails the mark_announced write fast (busy_timeout=0) and returns None,
        # leaving the announcement pending for the next uncontended session start.
        import sqlite3
        import time

        self._seed(review_db)
        blocker = sqlite3.connect(str(review_db), isolation_level=None)
        try:
            blocker.execute("PRAGMA busy_timeout = 0")
            blocker.execute("BEGIN IMMEDIATE")
            start = time.monotonic()
            assert collect_announcements(git_repo) is None
            assert time.monotonic() - start < 1.0
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        message = collect_announcements(git_repo)
        assert message is not None
        assert "awaiting your review" in message

    def _seed_two(self, db: Path) -> None:
        with ReviewStore.open(db) as store:
            create_pr_open(store, url=PR_URL, rule="r1")
            create_pr_open(store, url=PR_URL_2, rule="r2")
            store.enable(REPO)

    def test_contention_marks_no_row_then_all_resurface(self, review_db: Path, git_repo: Path) -> None:
        # With two pending announcements, a contended write lock must mark neither: the single
        # BEGIN IMMEDIATE fails before any mark, so nothing is stranded marked-but-undelivered,
        # and both lines resurface together once the lock releases.
        import sqlite3

        self._seed_two(review_db)
        blocker = sqlite3.connect(str(review_db), isolation_level=None)
        try:
            blocker.execute("PRAGMA busy_timeout = 0")
            blocker.execute("BEGIN IMMEDIATE")
            assert collect_announcements(git_repo) is None
            (marked,) = blocker.execute("SELECT COUNT(*) FROM candidates WHERE announced_status IS NOT NULL").fetchone()
            assert marked == 0
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
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

        def seed() -> None:
            with ReviewStore.open(db) as store:
                create_pr_open(store)
                store.enable(REPO)

        seed()

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
