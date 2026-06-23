from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from cc_transcript.mining.sourcekind import TRANSCRIPT_MESSAGE

from captain_hook.review.repo import RepoKey
from captain_hook.review.settings import ReviewSettings
from captain_hook.review.store import CandidateKind, CandidateStatus, ReviewStore
from captain_hook.review.sync import SyncReport, gh_pr_state, sync_open_prs

if TYPE_CHECKING:
    from typing import Any

REPO = RepoKey("github.com/yasyf/captain-hook")
OTHER_REPO = RepoKey("github.com/yasyf/other")


def install_gh(monkeypatch: pytest.MonkeyPatch, states: dict[str, str | None]) -> list[str]:
    calls: list[str] = []

    def fake(url: str) -> str | None:
        calls.append(url)
        return states[url]

    monkeypatch.setattr("captain_hook.review.sync.gh_pr_state", fake)
    return calls


async def open_pr(
    store: ReviewStore, url: str, *, repo: RepoKey = REPO, opened_days_ago: int = 0, rule: str | None = None
) -> int:
    candidate_id = await store.ensure_candidate(
        repo, kind=CandidateKind.CREATE, rule=rule or url, source_kind=TRANSCRIPT_MESSAGE
    )
    await store.transition(
        candidate_id,
        CandidateStatus.PR_OPEN,
        pr_url=url,
        pr_opened_at=datetime.now(UTC) - timedelta(days=opened_days_ago),
    )
    return candidate_id


async def status_of(store: ReviewStore, candidate_id: int) -> CandidateStatus:
    return CandidateStatus(str((await store.candidate(candidate_id))["status"]))


class TestSyncOpenPrs:
    @pytest.mark.parametrize(
        ("state", "expected_status", "expected_report"),
        [
            pytest.param("MERGED", CandidateStatus.ACCEPTED, SyncReport(1, 0, 0, 0), id="merged-accepts"),
            pytest.param("CLOSED", CandidateStatus.REJECTED, SyncReport(0, 1, 0, 0), id="closed-rejects"),
            pytest.param(None, CandidateStatus.PR_OPEN, SyncReport(0, 0, 0, 1), id="gh-down-skips"),
        ],
    )
    async def test_state_transitions(
        self,
        store: ReviewStore,
        settings: ReviewSettings,
        monkeypatch: pytest.MonkeyPatch,
        state: str | None,
        expected_status: CandidateStatus,
        expected_report: SyncReport,
    ) -> None:
        url = "https://github.com/yasyf/captain-hook/pull/1"
        candidate_id = await open_pr(store, url)
        install_gh(monkeypatch, {url: state})
        assert await sync_open_prs(store, REPO, settings=settings) == expected_report
        assert await status_of(store, candidate_id) == expected_status

    async def test_open_pr_past_stale_after_days_goes_stale(
        self, store: ReviewStore, settings: ReviewSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        url = "https://github.com/yasyf/captain-hook/pull/2"
        candidate_id = await open_pr(store, url, opened_days_ago=settings.stale_after_days + 1)
        install_gh(monkeypatch, {url: "OPEN"})
        assert await sync_open_prs(store, REPO, settings=settings) == SyncReport(0, 0, 1, 0)
        assert await status_of(store, candidate_id) == CandidateStatus.STALE

    async def test_fresh_open_pr_stays_open(
        self, store: ReviewStore, settings: ReviewSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        url = "https://github.com/yasyf/captain-hook/pull/3"
        candidate_id = await open_pr(store, url, opened_days_ago=1)
        install_gh(monkeypatch, {url: "OPEN"})
        assert await sync_open_prs(store, REPO, settings=settings) == SyncReport(0, 0, 0, 0)
        assert await status_of(store, candidate_id) == CandidateStatus.PR_OPEN

    async def test_mixed_pass_counts_each_transition(
        self, store: ReviewStore, settings: ReviewSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        urls = {
            f"https://github.com/yasyf/captain-hook/pull/{n}": state
            for n, state in enumerate(("MERGED", "CLOSED", None))
        }
        ids = {url: await open_pr(store, url) for url in urls}
        calls = install_gh(monkeypatch, urls)
        assert await sync_open_prs(store, REPO, settings=settings) == SyncReport(1, 1, 0, 1)
        assert sorted(calls) == sorted(urls)
        assert await status_of(store, ids["https://github.com/yasyf/captain-hook/pull/0"]) == CandidateStatus.ACCEPTED
        assert await status_of(store, ids["https://github.com/yasyf/captain-hook/pull/1"]) == CandidateStatus.REJECTED
        assert await status_of(store, ids["https://github.com/yasyf/captain-hook/pull/2"]) == CandidateStatus.PR_OPEN

    async def test_scopes_to_the_given_repo_and_pr_open_status(
        self, store: ReviewStore, settings: ReviewSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        foreign = "https://github.com/yasyf/other/pull/1"
        await open_pr(store, foreign, repo=OTHER_REPO)
        await store.ensure_candidate(
            REPO, kind=CandidateKind.CREATE, rule="watching-only", source_kind=TRANSCRIPT_MESSAGE
        )
        calls = install_gh(monkeypatch, {foreign: "MERGED"})
        assert await sync_open_prs(store, REPO, settings=settings) == SyncReport(0, 0, 0, 0)
        assert calls == []


class TestGhPrState:
    def test_parses_state_from_gh_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[list[str]] = []

        def fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            recorded.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout='{"state": "MERGED"}', stderr="")

        monkeypatch.setattr("captain_hook.review.sync.subprocess.run", fake)
        assert gh_pr_state("https://github.com/yasyf/captain-hook/pull/9") == "MERGED"
        assert recorded == [["gh", "pr", "view", "https://github.com/yasyf/captain-hook/pull/9", "--json", "state"]]

    @pytest.mark.parametrize(
        ("behavior", "stdout"),
        [
            pytest.param("nonzero", '{"state": "OPEN"}', id="gh-exit-nonzero"),
            pytest.param("raise-oserror", "", id="gh-not-installed"),
            pytest.param("raise-timeout", "", id="gh-timeout"),
            pytest.param("ok", "not json", id="garbage-stdout"),
            pytest.param("ok", "{}", id="missing-state-key"),
        ],
    )
    def test_failures_return_none(self, monkeypatch: pytest.MonkeyPatch, behavior: str, stdout: str) -> None:
        def fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            match behavior:
                case "raise-oserror":
                    raise OSError("no gh")
                case "raise-timeout":
                    raise subprocess.TimeoutExpired(argv, 30)
                case "nonzero":
                    return subprocess.CompletedProcess(argv, 1, stdout=stdout, stderr="no pull requests found")
                case _:
                    return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

        monkeypatch.setattr("captain_hook.review.sync.subprocess.run", fake)
        assert gh_pr_state("https://github.com/yasyf/captain-hook/pull/9") is None
