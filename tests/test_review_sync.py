from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from cc_transcript.mining.sourcekind import TRANSCRIPT_MESSAGE

from captain_hook.review.repo import RepoKey
from captain_hook.review.settings import ReviewSettings
from captain_hook.review.store import CandidateKind, CandidateStatus, ReviewStore
from captain_hook.review.sync import LANDING_MAX_PAGES, PrState, SyncReport, gh_pr_state, sync_open_prs

if TYPE_CHECKING:
    from typing import Any

REPO = RepoKey("github.com/yasyf/captain-hook")
OTHER_REPO = RepoKey("github.com/yasyf/other")
MERGED_AT = "2026-07-08T15:06:25Z"
LANDED_AT = "2026-09-19T15:02:46Z"
LANDED = PrState("CLOSED", None, landed_at=LANDED_AT)
INCONCLUSIVE = PrState("CLOSED", None, landing_checked=False)


def install_gh(monkeypatch: pytest.MonkeyPatch, states: dict[str, str | PrState | None]) -> list[str]:
    calls: list[str] = []

    def fake(url: str) -> PrState | None:
        calls.append(url)
        match states[url]:
            case None:
                return None
            case PrState() as pr:
                return pr
            case state:
                return PrState(state, merged_at=MERGED_AT if state == "MERGED" else None)

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
            pytest.param(LANDED, CandidateStatus.ACCEPTED, SyncReport(1, 0, 0, 0), id="closed-but-landed-accepts"),
            pytest.param(
                INCONCLUSIVE, CandidateStatus.PR_OPEN, SyncReport(0, 0, 0, 0, kept=1), id="closed-unchecked-keeps"
            ),
            pytest.param(None, CandidateStatus.PR_OPEN, SyncReport(0, 0, 0, 1), id="gh-down-skips"),
        ],
    )
    async def test_state_transitions(
        self,
        store: ReviewStore,
        settings: ReviewSettings,
        monkeypatch: pytest.MonkeyPatch,
        state: str | PrState | None,
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
        assert await sync_open_prs(store, REPO, settings=settings) == SyncReport(0, 0, 0, 0, kept=1)
        assert await status_of(store, candidate_id) == CandidateStatus.PR_OPEN

    async def test_merged_pr_stamps_resolved_at_from_github_merge_time(
        self, store: ReviewStore, settings: ReviewSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        url = "https://github.com/yasyf/captain-hook/pull/4"
        candidate_id = await open_pr(store, url)
        assert (await store.candidate(candidate_id))["resolved_at"] is None
        install_gh(monkeypatch, {url: "MERGED"})
        assert await sync_open_prs(store, REPO, settings=settings) == SyncReport(1, 0, 0, 0)
        assert await status_of(store, candidate_id) == CandidateStatus.ACCEPTED
        # resolved_at carries GitHub's merge time (normalized to UTC), not the sync wall-clock,
        # so a complaint occurring after the merge but before the sync still counts as recurrence.
        assert (await store.candidate(candidate_id))["resolved_at"] == "2026-07-08T15:06:25+00:00"

    async def test_landed_pr_stamps_resolved_at_from_the_landing_commit(
        self, store: ReviewStore, settings: ReviewSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        url = "https://github.com/yasyf/captain-hook/pull/17"
        candidate_id = await open_pr(store, url)
        install_gh(monkeypatch, {url: LANDED})
        assert await sync_open_prs(store, REPO, settings=settings) == SyncReport(1, 0, 0, 0)
        assert await status_of(store, candidate_id) == CandidateStatus.ACCEPTED
        assert (await store.candidate(candidate_id))["resolved_at"] == "2026-09-19T15:02:46+00:00"

    async def test_cached_closed_state_is_inconclusive_until_refetched(
        self, store: ReviewStore, settings: ReviewSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        url = "https://github.com/yasyf/captain-hook/pull/18"
        candidate_id = await open_pr(store, url)
        await store.cache_pr_state(url, PrState("CLOSED", None))
        calls = install_gh(monkeypatch, {url: LANDED})
        assert await sync_open_prs(store, REPO, settings=settings) == SyncReport(0, 0, 0, 0, kept=1)
        assert calls == []
        assert await status_of(store, candidate_id) == CandidateStatus.PR_OPEN
        assert await sync_open_prs(store, REPO, settings=settings, force_refresh=True) == SyncReport(1, 0, 0, 0)
        assert await status_of(store, candidate_id) == CandidateStatus.ACCEPTED

    async def test_closed_pr_leaves_resolved_at_unstamped(
        self, store: ReviewStore, settings: ReviewSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        url = "https://github.com/yasyf/captain-hook/pull/5"
        candidate_id = await open_pr(store, url)
        install_gh(monkeypatch, {url: "CLOSED"})
        assert await sync_open_prs(store, REPO, settings=settings) == SyncReport(0, 1, 0, 0)
        assert await status_of(store, candidate_id) == CandidateStatus.REJECTED
        assert (await store.candidate(candidate_id))["resolved_at"] is None

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


class TestPrStateCache:
    async def test_cache_miss_fetches_and_records(
        self, store: ReviewStore, settings: ReviewSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        url = "https://github.com/yasyf/captain-hook/pull/10"
        await open_pr(store, url, opened_days_ago=1)
        calls = install_gh(monkeypatch, {url: "OPEN"})
        assert await sync_open_prs(store, REPO, settings=settings) == SyncReport(0, 0, 0, 0, kept=1)
        assert calls == [url]
        cached = await store.pr_state_cache(url)
        assert cached is not None and cached.pr == PrState("OPEN", None)

    async def test_warm_cache_within_ttl_skips_gh(
        self, store: ReviewStore, settings: ReviewSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        url = "https://github.com/yasyf/captain-hook/pull/11"
        candidate_id = await open_pr(store, url, opened_days_ago=1)
        calls = install_gh(monkeypatch, {url: "OPEN"})
        await sync_open_prs(store, REPO, settings=settings)
        assert await sync_open_prs(store, REPO, settings=settings) == SyncReport(0, 0, 0, 0, kept=1)
        assert calls == [url]
        assert await status_of(store, candidate_id) == CandidateStatus.PR_OPEN

    async def test_force_refresh_bypasses_a_warm_cache(
        self, store: ReviewStore, settings: ReviewSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        url = "https://github.com/yasyf/captain-hook/pull/12"
        candidate_id = await open_pr(store, url)
        install_gh(monkeypatch, {url: "OPEN"})
        await sync_open_prs(store, REPO, settings=settings)
        calls = install_gh(monkeypatch, {url: "MERGED"})
        assert await sync_open_prs(store, REPO, settings=settings, force_refresh=True) == SyncReport(1, 0, 0, 0)
        assert calls == [url]
        assert await status_of(store, candidate_id) == CandidateStatus.ACCEPTED

    @pytest.mark.parametrize("cached_state", ["OPEN", "MERGED", "CLOSED"])
    async def test_gh_down_on_forced_refresh_never_applies_cached_state(
        self, store: ReviewStore, settings: ReviewSettings, monkeypatch: pytest.MonkeyPatch, cached_state: str
    ) -> None:
        # A gh outage during a forced refresh must never fold a stale cached state into a
        # lifecycle transition: the PR counts unreachable and stays pr_open, whatever was cached.
        url = "https://github.com/yasyf/captain-hook/pull/13"
        candidate_id = await open_pr(store, url)
        await store.cache_pr_state(url, PrState(cached_state, MERGED_AT if cached_state == "MERGED" else None))
        calls = install_gh(monkeypatch, {url: None})
        assert await sync_open_prs(store, REPO, settings=settings, force_refresh=True) == SyncReport(0, 0, 0, 1)
        assert calls == [url]
        assert await status_of(store, candidate_id) == CandidateStatus.PR_OPEN

    async def test_gh_down_on_expired_cache_keeps_stale_eligible_pr_open(
        self, store: ReviewStore, settings: ReviewSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A PR past stale_after_days with an EXPIRED cached OPEN state and gh down stays pr_open
        # (unreachable=1): an expired cache never authorizes the destructive OPEN->STALE transition.
        url = "https://github.com/yasyf/captain-hook/pull/14"
        candidate_id = await open_pr(store, url, opened_days_ago=settings.stale_after_days + 1)
        await store.cache_pr_state(url, PrState("OPEN", None))
        expired = (datetime.now(UTC) - timedelta(minutes=20)).isoformat()
        await store.db.execute("UPDATE pr_states SET fetched_at = ? WHERE pr_url = ?", (expired, url))
        calls = install_gh(monkeypatch, {url: None})
        assert await sync_open_prs(store, REPO, settings=settings) == SyncReport(0, 0, 0, 1)
        assert calls == [url]
        assert await status_of(store, candidate_id) == CandidateStatus.PR_OPEN


PR_URL = "https://github.com/yasyf/captain-hook/pull/9"


def pr_payload(
    state: str, *, merged_at: str | None = None, closer: dict[str, Any] | None = None, number: int = 9
) -> str:
    resource = {
        "number": number,
        "state": state,
        "mergedAt": merged_at,
        "closedAt": "2026-09-19T15:05:28Z",
        "baseRefName": "dev",
        "repository": {"owner": {"login": "yasyf"}, "name": "captain-hook"},
        "timelineItems": {"nodes": [{"closer": closer}] if state == "CLOSED" else []},
    }
    return json.dumps({"data": {"resource": resource}})


def history_payload(messages: list[tuple[str, str]], *, more: bool = False) -> str:
    history = {
        "pageInfo": {"hasNextPage": more, "endCursor": "cursor-1"},
        "nodes": [{"committedDate": date, "message": message} for date, message in messages],
    }
    return json.dumps({"data": {"repository": {"ref": {"target": {"history": history}}}}})


def install_graphql(monkeypatch: pytest.MonkeyPatch, replies: list[str | Exception]) -> list[list[str]]:
    recorded: list[list[str]] = []

    def fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded.append(argv)
        match replies.pop(0):
            case Exception() as exc:
                raise exc
            case "nonzero":
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="gh: not found")
            case stdout:
                return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("captain_hook.review.sync.subprocess.run", fake)
    return recorded


class TestGhPrState:
    def test_merged_pr_reads_state_and_merge_time_in_one_query(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded = install_graphql(monkeypatch, [pr_payload("MERGED", merged_at=MERGED_AT)])
        assert gh_pr_state(PR_URL) == PrState("MERGED", MERGED_AT)
        assert len(recorded) == 1
        assert recorded[0][:4] == ["gh", "api", "graphql", "-f"]
        assert recorded[0][-2:] == ["-F", f"url={PR_URL}"]

    def test_open_pr_reports_null_merged_at(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_graphql(monkeypatch, [pr_payload("OPEN")])
        assert gh_pr_state(PR_URL) == PrState("OPEN", None)

    def test_closed_by_a_commit_lands_without_reading_history(self, monkeypatch: pytest.MonkeyPatch) -> None:
        closer = {"__typename": "Commit", "committedDate": LANDED_AT}
        recorded = install_graphql(monkeypatch, [pr_payload("CLOSED", closer=closer)])
        assert gh_pr_state(PR_URL) == PrState("CLOSED", None, landed_at=LANDED_AT)
        assert len(recorded) == 1

    def test_graphite_close_lands_via_the_base_branch_commit_citing_the_number(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        history = history_payload(
            [
                (
                    "2026-09-19T15:03:19Z",
                    "[capt-hook] Verify a branch actually pushed before reporting it done (#17221)",
                ),
                (LANDED_AT, "[capt-hook] Rebase onto dev before opening or updating a PR (#9)\n\nBody mentions #90."),
                ("2026-09-19T15:01:06Z", "infra/ci: give the reaper a v2 (#90)"),
            ]
        )
        recorded = install_graphql(monkeypatch, [pr_payload("CLOSED"), history])
        assert gh_pr_state(PR_URL) == PrState("CLOSED", None, landed_at=LANDED_AT)
        assert len(recorded) == 2
        assert "since=2026-09-19T12:05:28+00:00" in recorded[1]
        assert "until=2026-09-19T15:20:28+00:00" in recorded[1]
        assert "ref=refs/heads/dev" in recorded[1]

    def test_number_cited_only_in_a_body_does_not_land(self, monkeypatch: pytest.MonkeyPatch) -> None:
        history = history_payload([("2026-09-19T15:03:19Z", "other: fix (#91)\n\nStacked on #9.")])
        install_graphql(monkeypatch, [pr_payload("CLOSED"), history])
        assert gh_pr_state(PR_URL) == PrState("CLOSED", None)

    def test_genuine_close_with_a_fully_read_window_rejects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        history = history_payload([("2026-09-19T15:03:19Z", "unrelated: change (#91)")])
        install_graphql(monkeypatch, [pr_payload("CLOSED"), history])
        assert gh_pr_state(PR_URL) == PrState("CLOSED", None, landed_at=None, landing_checked=True)

    def test_landing_is_found_on_a_later_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        first = history_payload([("2026-09-19T15:10:00Z", "later: change (#91)")], more=True)
        second = history_payload([(LANDED_AT, "mine: change (#9)")])
        recorded = install_graphql(monkeypatch, [pr_payload("CLOSED"), first, second])
        assert gh_pr_state(PR_URL) == PrState("CLOSED", None, landed_at=LANDED_AT)
        assert "after=cursor-1" in recorded[2]
        assert not any(arg.startswith("after=") for arg in recorded[1])

    def test_window_overflow_is_inconclusive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pages = [history_payload([("2026-09-19T15:10:00Z", "busy: change (#91)")], more=True)] * LANDING_MAX_PAGES
        install_graphql(monkeypatch, [pr_payload("CLOSED"), *pages])
        assert gh_pr_state(PR_URL) == PrState("CLOSED", None, landing_checked=False)

    @pytest.mark.parametrize(
        "reply",
        [
            pytest.param("nonzero", id="history-query-fails"),
            pytest.param(subprocess.TimeoutExpired(["gh"], 30), id="history-query-times-out"),
            pytest.param('{"data": {"repository": {"ref": null}}}', id="base-branch-gone"),
        ],
    )
    def test_unreadable_history_is_inconclusive(self, monkeypatch: pytest.MonkeyPatch, reply: str | Exception) -> None:
        install_graphql(monkeypatch, [pr_payload("CLOSED"), reply])
        assert gh_pr_state(PR_URL) == PrState("CLOSED", None, landing_checked=False)

    @pytest.mark.parametrize(
        ("behavior", "stdout"),
        [
            pytest.param("nonzero", "", id="gh-exit-nonzero"),
            pytest.param("raise-oserror", "", id="gh-not-installed"),
            pytest.param("raise-timeout", "", id="gh-timeout"),
            pytest.param("ok", "not json", id="garbage-stdout"),
            pytest.param("ok", '{"data": {"resource": {}}}', id="missing-state-key"),
            pytest.param("ok", '{"data": {"resource": null}}', id="not-a-pull-request"),
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
        assert gh_pr_state(PR_URL) is None
