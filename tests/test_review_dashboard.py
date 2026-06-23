from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import TYPE_CHECKING

import pytest
from cc_transcript.ids import SessionId
from cc_transcript.mining.candidates import DedupKey
from cc_transcript.mining.confidence import MEDIUM, CandidateSignal, Confidence, to_payload
from cc_transcript.mining.sourcekind import SourceKind
from click.testing import CliRunner
from rich.console import Console

from captain_hook.cli import cli
from captain_hook.review.dashboard import (
    Stage,
    pr_description,
    progress_text,
    render,
    stage_of,
    targets,
)
from captain_hook.review.judge import REVIEW_PROMPT_VERSION
from captain_hook.review.repo import RepoKey
from captain_hook.review.settings import ReviewSettings
from captain_hook.review.store import (
    CandidateKind,
    CandidateStatus,
    CandidateView,
    ReviewStore,
    ThresholdStatus,
    crosses_thresholds,
)

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

REPO = RepoKey("github.com/yasyf/scratch")
PV = REVIEW_PROMPT_VERSION
INSERT_EVENT = (
    "INSERT INTO feedback_events (dedup_key, source_kind, session_id, occurred_at, text, payload_json, "
    "context_json, ingested_at) VALUES (?, ?, ?, ?, ?, ?, '{}', '2026-06-01T00:00:00+00:00')"
)


@dataclass(frozen=True)
class FakeVerdict:
    accepted: bool = True
    confidence: float = 0.9
    category: str = "durable_style_rule"
    summary: str = "summary"
    rationale: str = "because"


def view(
    *,
    id: int = 1,
    kind: str = "create",
    status: str = "watching",
    eligible: bool = False,
    summary: str | None = None,
    sample_text: str = "",
    sessions: int = 0,
    days: int = 0,
    open_prs: int = 0,
    single: bool = False,
    pr_url: str | None = None,
    pr_opened_at: str | None = None,
    target_hook: str | None = None,
    target_file: str | None = None,
    misfire: str | None = None,
) -> CandidateView:
    row: dict[str, object] = {
        "id": id,
        "candidate_kind": kind,
        "status": status,
        "source_kind": "transcript_message",
        "rule": "r",
        "pr_url": pr_url,
        "pr_opened_at": pr_opened_at,
        "sample_text": sample_text,
        "observations": 1,
        "target_hook_name": target_hook,
        "target_source_file": target_file,
        "misfire_class": misfire,
    }
    threshold = ThresholdStatus(
        kind=CandidateKind(kind),
        status=CandidateStatus(status),
        watching=True,
        sessions=sessions,
        days=days,
        open_prs=open_prs,
        single_observation=single,
    )
    return CandidateView(row=row, threshold=threshold, eligible=eligible, summary=summary)


def plain(renderable: object, *, width: int = 200) -> str:
    buf = StringIO()
    Console(file=buf, no_color=True, width=width).print(renderable)
    return buf.getvalue()


class TestStageOf:
    @pytest.mark.parametrize(
        ("status", "eligible", "expected"),
        [
            ("watching", False, Stage.WATCHING),
            ("watching", True, Stage.ELIGIBLE),
            ("pr_open", False, Stage.PR_OPEN),
            ("accepted", False, Stage.ACCEPTED),
            ("rejected", False, Stage.REJECTED),
            ("stale", False, Stage.STALE),
        ],
        ids=lambda v: str(v),
    )
    def test_buckets_by_status_and_eligibility(self, status: str, eligible: bool, expected: Stage) -> None:
        assert stage_of(view(status=status, eligible=eligible)) is expected


class TestPrDescription:
    def test_create_prefers_verdict_summary(self) -> None:
        assert pr_description(view(kind="create", summary="block force-push", sample_text="never force push")) == (
            'would add a hook: "block force-push"'
        )

    def test_create_falls_back_to_sample_text(self) -> None:
        assert pr_description(view(kind="create", summary=None, sample_text="never force push to main")) == (
            'would add a hook: "never force push to main"'
        )

    def test_fix_names_target_and_prefers_summary(self) -> None:
        assert (
            pr_description(
                view(
                    kind="fix",
                    status="watching",
                    summary="stop nudge firing on its own text",
                    target_hook="hooks.style:nudge_1",
                    target_file=".claude/hooks/style.py",
                    misfire="refire_on_own_text",
                )
            )
            == "would fix hooks.style:nudge_1 (.claude/hooks/style.py): stop nudge firing on its own text"
        )

    def test_fix_falls_back_to_misfire_class(self) -> None:
        assert (
            pr_description(
                view(
                    kind="fix",
                    status="watching",
                    summary=None,
                    target_hook="h:n",
                    target_file="f.py",
                    misfire="refire_on_own_text",
                )
            )
            == "would fix h:n (f.py): regression test for refire_on_own_text"
        )


class TestProgress:
    def test_create_targets_use_create_thresholds(self) -> None:
        settings = ReviewSettings()
        assert targets(view(kind="create", sessions=2, days=1), settings) == (
            ("sessions", 2, settings.min_sessions),
            ("days", 1, settings.min_days),
        )

    def test_fix_targets_use_fix_thresholds(self) -> None:
        settings = ReviewSettings()
        assert targets(view(kind="fix", status="watching", sessions=1, target_hook="h", target_file="f"), settings) == (
            ("sessions", 1, settings.min_sessions_fix),
            ("days", 0, settings.min_days_fix),
        )

    def test_progress_text_drops_zero_target_and_shows_counts(self) -> None:
        text = progress_text(
            view(kind="fix", status="watching", sessions=1, target_hook="h", target_file="f"), ReviewSettings()
        )
        assert "sessions" in text and "1/2" in text
        assert "days" not in text


class TestRenderFrame:
    def test_groups_candidates_under_stage_headers(self) -> None:
        views = [
            view(id=1, kind="create", status="watching", sessions=2, days=2, summary="block force-push"),
            view(id=2, kind="create", status="watching", eligible=True, sessions=3, days=2, summary="run tests"),
            view(
                id=3,
                kind="create",
                status="pr_open",
                summary="use uv",
                pr_url="https://x/pull/42",
                pr_opened_at="2026-06-10T00:00:00+00:00",
            ),
        ]
        out = plain(render(views, repo=REPO, settings=ReviewSettings(), watching=True))
        assert "WATCHING" in out and "ELIGIBLE" in out and "PR OPEN" in out
        assert "#1" in out and "#2" in out and "#3" in out
        assert 'would add a hook: "block force-push"' in out
        assert "https://x/pull/42" in out
        assert "[watching]" in out and "PR slots 1/2" in out

    def test_empty_repo_shows_hint(self) -> None:
        out = plain(render([], repo=REPO, settings=ReviewSettings(), watching=False))
        assert "[not watching]" in out
        assert "No corrections tracked yet" in out
        assert "capt-hook review enable" in out


async def seed_obs(
    store: ReviewStore,
    candidate_id: int,
    key: str,
    *,
    session: str,
    occurred: str,
    heuristic: float = MEDIUM,
    source: str = "transcript_message",
    text: str = "correction text",
) -> None:
    payload = json.dumps({"signal": to_payload(CandidateSignal(Confidence(heuristic), ("marker",)))})
    await store.store.conn.execute(INSERT_EVENT, (key, source, session, occurred, text, payload))
    await store.record_observation(
        candidate_id,
        dedup_key=DedupKey(key),
        session_id=SessionId(session),
        occurred_at=datetime.fromisoformat(occurred),
    )


async def judge_obs(
    store: ReviewStore, key: str, *, accepted: bool = True, summary: str = "encode this rule", model: str = "m1"
) -> None:
    await store.record_verdict(
        DedupKey(key),
        FakeVerdict(accepted=accepted, summary=summary),
        role="judge",
        prompt_version=PV,
        model=model,
        fidelity="full",
    )


async def eligible_create(store: ReviewStore, *, rule: str, summary: str) -> int:
    candidate_id = await store.ensure_candidate(
        REPO, kind=CandidateKind.CREATE, rule=rule, source_kind=SourceKind("transcript_message")
    )
    for i, (session, day) in enumerate([("s1", "2026-06-01"), ("s2", "2026-06-01"), ("s3", "2026-06-02")]):
        await seed_obs(store, candidate_id, f"{rule}{i}", session=session, occurred=f"{day}T10:00:00+00:00")
        await judge_obs(store, f"{rule}{i}", summary=summary)
    return candidate_id


class TestEligibilityParity:
    async def test_crosses_thresholds_agrees_with_eligible(self, store: ReviewStore) -> None:
        settings = ReviewSettings()
        await store.enable(REPO)
        ready = await eligible_create(store, rule="ready", summary="run tests")
        watching = await store.ensure_candidate(
            REPO, kind=CandidateKind.CREATE, rule="early", source_kind=SourceKind("transcript_message")
        )
        await seed_obs(store, watching, "early0", session="x1", occurred="2026-06-01T10:00:00+00:00")
        await judge_obs(store, "early0")
        for candidate_id, expected in ((ready, True), (watching, False)):
            status = await store.threshold_status(candidate_id, settings=settings, prompt_version=PV)
            direct = await store.eligible(candidate_id, settings=settings, prompt_version=PV)
            assert crosses_thresholds(status, settings=settings) == direct == expected


class TestOverview:
    async def test_overview_carries_eligibility_and_pr_summary(self, store: ReviewStore) -> None:
        settings = ReviewSettings()
        await store.enable(REPO)
        await eligible_create(store, rule="ready", summary="run the suite before committing")
        views = await store.overview(REPO, settings=settings, prompt_version=PV)
        assert [v.eligible for v in views] == [True]
        assert views[0].summary == "run the suite before committing"


def review_status(*args: str, root: Path) -> Result:
    return CliRunner().invoke(cli, ["--root", str(root), "review", "status", *args], env={"COLUMNS": "200"})


def top_status(*args: str, root: Path) -> Result:
    return CliRunner().invoke(cli, ["--root", str(root), "status", *args], env={"COLUMNS": "200"})


async def seed_db(scenario: str) -> int:
    async with await ReviewStore.open(ReviewSettings().db_path) as store:
        await store.enable(REPO)
        watching = await store.ensure_candidate(
            REPO, kind=CandidateKind.CREATE, rule="force", source_kind=SourceKind("transcript_message")
        )
        await seed_obs(
            store, watching, "w0", session="a1", occurred="2026-06-01T10:00:00+00:00", text="never force-push"
        )
        await judge_obs(store, "w0", summary="block force-push to main")
        await eligible_create(store, rule="tests", summary="run the suite before committing")
        match scenario:
            case "pr_open":
                pr = await store.ensure_candidate(
                    REPO, kind=CandidateKind.CREATE, rule="uv", source_kind=SourceKind("transcript_message")
                )
                await seed_obs(store, pr, "p0", session="b1", occurred="2026-06-01T10:00:00+00:00", text="use uv")
                await judge_obs(store, "p0", summary="use uv instead of pip")
                await store.transition(
                    pr,
                    CandidateStatus.PR_OPEN,
                    pr_url="https://github.com/yasyf/scratch/pull/42",
                    pr_opened_at=datetime.now(UTC) - timedelta(days=4),
                )
                return pr
        return watching


class TestStatusCommand:
    def test_review_status_renders_funnel(self, git_repo: Path) -> None:
        asyncio.run(seed_db("watching"))
        result = review_status("--no-sync", root=git_repo)
        assert result.exit_code == 0, result.output
        assert "WATCHING" in result.output and "ELIGIBLE" in result.output
        assert "would add a hook" in result.output
        assert "[watching]" in result.output

    def test_top_level_status_matches_review_status(self, git_repo: Path) -> None:
        asyncio.run(seed_db("watching"))
        review = review_status("--no-sync", root=git_repo)
        top = top_status("--no-sync", root=git_repo)
        assert top.exit_code == 0, top.output
        assert top.output == review.output

    def test_no_sync_never_shells_out_to_gh(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        asyncio.run(seed_db("pr_open"))
        calls: list[str] = []
        monkeypatch.setattr("captain_hook.review.sync.gh_pr_state", lambda url: calls.append(url) or "OPEN")
        result = review_status("--no-sync", root=git_repo)
        assert result.exit_code == 0, result.output
        assert calls == []
        assert "PR OPEN" in result.output and "pull/42" in result.output

    def test_background_sync_folds_merged_pr_into_accepted(
        self, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        candidate_id = asyncio.run(seed_db("pr_open"))
        monkeypatch.setattr("captain_hook.review.sync.gh_pr_state", lambda _url: "MERGED")
        result = review_status(root=git_repo)
        assert result.exit_code == 0, result.output

        async def status_of() -> str:
            async with await ReviewStore.open(ReviewSettings().db_path) as store:
                return str((await store.candidate(candidate_id))["status"])

        assert asyncio.run(status_of()) == "accepted"
        assert "ACCEPTED" in result.output

    def test_status_outside_a_git_repo_fails(self, tmp_path: Path) -> None:
        result = review_status(root=tmp_path)
        assert result.exit_code != 0
        assert "is not a git repo with an 'origin' remote" in result.output
