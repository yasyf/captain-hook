from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import StringIO
from typing import TYPE_CHECKING

import pytest
from cc_transcript.ids import SessionId
from cc_transcript.judge.similar import KeyOverlap
from cc_transcript.mining.candidates import DedupKey
from cc_transcript.mining.confidence import MEDIUM, CandidateSignal, Confidence, to_payload
from cc_transcript.mining.sourcekind import SourceKind
from click.testing import CliRunner
from rich.console import Console

from captain_hook.app import LoadError
from captain_hook.cli import cli
from captain_hook.review.dashboard import (
    REJECTED_COLLAPSE_N,
    Stage,
    brain_segment,
    pr_description,
    progress_text,
    render,
    stage_of,
    targets,
)
from captain_hook.review.repo import RepoKey
from captain_hook.review.settings import ReviewSettings
from captain_hook.review.store import (
    CandidateKind,
    CandidateStatus,
    CandidateView,
    JudgeHealth,
    ReviewStore,
    SpawnHealth,
    ThresholdStatus,
    crosses_thresholds,
)
from captain_hook.review.sync import PrState

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

REPO = RepoKey("github.com/yasyf/scratch")
NO_JUDGE = JudgeHealth(pending=0, last_verdict_at=None, splits=())
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
    canonical_key: str | None = None


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
    pack: str | None = None,
    origin: str | None = None,
) -> CandidateView:
    row: dict[str, object] = {
        "id": id,
        "candidate_kind": kind,
        "status": status,
        "source_kind": "transcript_message",
        "rule": "r",
        "repo_key": "github.com/yasyf/captain-hook" if pack else str(REPO),
        "origin_repo_key": origin,
        "pack_name": pack,
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


def colored(renderable: object, *, width: int = 200) -> str:
    buf = StringIO()
    Console(file=buf, force_terminal=True, color_system="standard", no_color=False, width=width).print(renderable)
    return buf.getvalue()


def brain_health(*, exit_code: int, prs: int, seconds: float = 142.0, eligible: tuple[int, ...] = (1,)) -> SpawnHealth:
    stamp = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    report = json.dumps(
        {"judged": 4, "eligible": list(eligible), "brain_exit": exit_code, "brain_seconds": seconds, "brain_prs": prs}
    )
    return SpawnHealth(
        last=spawn_row(ok=True, started_at=stamp, finished_at=stamp, report_json=report),
        consecutive_failures=0,
        failing_since=None,
    )


def spawn_row(
    *,
    ok: bool,
    started_at: str = "2026-06-01T00:00:00+00:00",
    finished_at: str | None = None,
    error: str | None = None,
    report_json: str | None = None,
) -> dict[str, object]:
    return {
        "id": 1,
        "started_at": started_at,
        "finished_at": finished_at or started_at,
        "transcript": "/tmp/t.jsonl",
        "ok": int(ok),
        "error": error,
        "report_json": report_json,
    }


def ok_health(*, ago: timedelta = timedelta(minutes=5), judged: int = 4) -> SpawnHealth:
    stamp = (datetime.now(UTC) - ago).isoformat()
    return SpawnHealth(
        last=spawn_row(ok=True, started_at=stamp, finished_at=stamp, report_json=json.dumps({"judged": judged})),
        consecutive_failures=0,
        failing_since=None,
    )


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
    @pytest.mark.parametrize(
        ("candidate", "expected"),
        [
            pytest.param(
                view(kind="create", summary="block force-push", sample_text="never force push"),
                'would add a hook: "block force-push"',
                id="create_prefers_verdict_summary",
            ),
            pytest.param(
                view(kind="create", summary=None, sample_text="never force push to main"),
                'would add a hook: "never force push to main"',
                id="create_falls_back_to_sample_text",
            ),
            pytest.param(
                view(
                    kind="fix",
                    status="watching",
                    summary="stop nudge firing on its own text",
                    target_hook="hooks.style:nudge_1",
                    target_file=".claude/hooks/style.py",
                    misfire="refire_on_own_text",
                ),
                "would fix hooks.style:nudge_1 (.claude/hooks/style.py): stop nudge firing on its own text",
                id="fix_names_target_and_prefers_summary",
            ),
            pytest.param(
                view(
                    kind="fix",
                    status="watching",
                    summary=None,
                    target_hook="h:n",
                    target_file="f.py",
                    misfire="refire_on_own_text",
                ),
                "would fix h:n (f.py): regression test for refire_on_own_text",
                id="fix_falls_back_to_misfire_class",
            ),
            pytest.param(
                view(
                    kind="fix",
                    status="watching",
                    summary="stop the docs nudge firing on its own text",
                    target_hook="general.docs:nudge_1",
                    target_file="captain_hook/packs/general/docs.py",
                    misfire="refire",
                    pack="general",
                ),
                "[general] would fix general.docs:nudge_1 (captain_hook/packs/general/docs.py): "
                "stop the docs nudge firing on its own text",
                id="pack_fix_prefixes_the_pack_name",
            ),
        ],
    )
    def test_description(self, candidate: CandidateView, expected: str) -> None:
        assert pr_description(candidate) == expected


class TestProgress:
    @pytest.mark.parametrize(
        ("candidate", "sessions", "days", "sessions_attr", "days_attr"),
        [
            pytest.param(
                view(kind="create", sessions=2, days=1),
                2,
                1,
                "min_sessions",
                "min_days",
                id="create_targets_use_create_thresholds",
            ),
            pytest.param(
                view(kind="fix", status="watching", sessions=1, target_hook="h", target_file="f"),
                1,
                0,
                "min_sessions_fix",
                "min_days_fix",
                id="fix_targets_use_fix_thresholds",
            ),
        ],
    )
    def test_targets_use_kind_thresholds(
        self, candidate: CandidateView, sessions: int, days: int, sessions_attr: str, days_attr: str
    ) -> None:
        settings = ReviewSettings()
        assert targets(candidate, settings) == (
            ("sessions", sessions, getattr(settings, sessions_attr)),
            ("days", days, getattr(settings, days_attr)),
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
        out = plain(
            render(
                views,
                repo=REPO,
                settings=ReviewSettings(),
                watching=True,
                health=ok_health(),
                judge=NO_JUDGE,
                open_prs=1,
            )
        )
        assert "WATCHING" in out and "ELIGIBLE" in out and "PR OPEN" in out
        assert "#1" in out and "#2" in out and "#3" in out
        assert 'would add a hook: "block force-push"' in out
        assert "https://x/pull/42" in out
        assert "[watching]" in out and "PR slots 1/2" in out

    def test_empty_repo_shows_hint(self) -> None:
        out = plain(
            render(
                [],
                repo=REPO,
                settings=ReviewSettings(),
                watching=False,
                health=ok_health(),
                judge=NO_JUDGE,
                open_prs=0,
            )
        )
        assert "[not watching]" in out
        assert "No corrections tracked yet" in out
        assert "capt-hook review enable" in out


def rendered_stages(views: list[CandidateView]) -> str:
    frame = render(
        views,
        repo=REPO,
        settings=ReviewSettings(),
        watching=True,
        health=ok_health(),
        judge=NO_JUDGE,
        open_prs=0,
    )
    return plain(frame)


class TestRejectedCollapse:
    def test_rejected_beyond_the_cap_collapse_to_a_count_line(self) -> None:
        extra = 3
        out = rendered_stages([view(id=i, status="rejected") for i in range(1, REJECTED_COLLAPSE_N + extra + 1)])
        assert all(f"#{i}" in out for i in range(1, REJECTED_COLLAPSE_N + 1))
        assert all(f"#{i}" not in out for i in range(REJECTED_COLLAPSE_N + 1, REJECTED_COLLAPSE_N + extra + 1))
        assert f"… and {extra} more rejected" in out

    def test_rejected_at_the_cap_shows_all_with_no_count_line(self) -> None:
        out = rendered_stages([view(id=i, status="rejected") for i in range(1, REJECTED_COLLAPSE_N + 1)])
        assert all(f"#{i}" in out for i in range(1, REJECTED_COLLAPSE_N + 1))
        assert "more rejected" not in out

    def test_other_stages_never_collapse(self) -> None:
        n = REJECTED_COLLAPSE_N + 4
        out = rendered_stages([view(id=i, status="watching", sessions=1, days=1) for i in range(1, n)])
        assert all(f"#{i}" in out for i in range(1, n))
        assert "more rejected" not in out


class TestPackErrors:
    def test_load_error_line_shows_pack_attribution(self) -> None:
        errors = [LoadError(source="/x/.claude/hooks/boom.py", exc=RuntimeError("kaboom"), pack="badpack")]
        out = plain(
            render(
                [],
                repo=REPO,
                settings=ReviewSettings(),
                watching=True,
                health=ok_health(),
                judge=NO_JUDGE,
                open_prs=0,
                load_errors=errors,
            )
        )
        assert "HOOK LOAD FAILED" in out
        assert "[badpack] boom.py" in out
        assert "RuntimeError: kaboom" in out

    def test_zero_errors_renders_no_line(self) -> None:
        out = plain(
            render(
                [],
                repo=REPO,
                settings=ReviewSettings(),
                watching=True,
                health=ok_health(),
                judge=NO_JUDGE,
                open_prs=0,
            )
        )
        assert "HOOK LOAD FAILED" not in out


class TestHealthLine:
    def test_failing_reviewer_renders_banner_at_the_top(self) -> None:
        health = SpawnHealth(
            last=spawn_row(
                ok=False,
                started_at="2026-06-20T09:00:00+00:00",
                error="OperationalError: no such column: fidelity",
            ),
            consecutive_failures=34,
            failing_since="2026-06-01T09:00:00+00:00",
        )
        out = plain(
            render(
                [],
                repo=REPO,
                settings=ReviewSettings(),
                watching=True,
                health=health,
                judge=NO_JUDGE,
                open_prs=0,
            )
        )
        assert out.splitlines()[0].startswith("REVIEWER FAILING")
        assert "34 consecutive since 2026-06-01T09:00:00+00:00" in out
        assert "OperationalError: no such column: fidelity" in out
        assert "spawn.log" in out

    def test_healthy_reviewer_renders_relative_time_and_judged_count(self) -> None:
        out = plain(
            render(
                [],
                repo=REPO,
                settings=ReviewSettings(),
                watching=True,
                health=ok_health(ago=timedelta(hours=2), judged=7),
                judge=NO_JUDGE,
                open_prs=0,
            )
        )
        assert "reviewer ok" in out
        assert "last run 2h ago" in out
        assert "judged 7" in out
        assert "REVIEWER FAILING" not in out

    def test_healthy_reviewer_appends_judge_segment_and_splits(self) -> None:
        judge = JudgeHealth(
            pending=3,
            last_verdict_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
            splits=(KeyOverlap("prefer-uv", "use-uv-not-pip", 0.93),),
        )
        out = plain(
            render(
                [],
                repo=REPO,
                settings=ReviewSettings(),
                watching=True,
                health=ok_health(),
                judge=judge,
                open_prs=0,
            )
        )
        assert "judge: 3 pending · last verdict 5m ago" in out
        assert "1 possible slug splits" in out

    def test_judge_segment_hides_verdict_and_splits_when_absent(self) -> None:
        out = plain(
            render(
                [],
                repo=REPO,
                settings=ReviewSettings(),
                watching=True,
                health=ok_health(),
                judge=NO_JUDGE,
                open_prs=0,
            )
        )
        assert "judge: 0 pending" in out
        assert "last verdict" not in out
        assert "possible slug splits" not in out

    def test_healthy_reviewer_renders_brain_segment_when_the_brain_ran(self) -> None:
        out = plain(
            render(
                [],
                repo=REPO,
                settings=ReviewSettings(),
                watching=True,
                health=brain_health(exit_code=0, prs=1),
                judge=NO_JUDGE,
                open_prs=0,
            )
        )
        assert "reviewer ok" in out
        assert "brain: exit 0 · 142s · 1 PR" in out

    def test_no_brain_segment_when_the_brain_did_not_run(self) -> None:
        out = plain(
            render(
                [],
                repo=REPO,
                settings=ReviewSettings(),
                watching=True,
                health=ok_health(),
                judge=NO_JUDGE,
                open_prs=0,
            )
        )
        assert "reviewer ok" in out
        assert "brain:" not in out

    def test_failing_brain_renders_red_segment(self) -> None:
        out = colored(
            render(
                [],
                repo=REPO,
                settings=ReviewSettings(),
                watching=True,
                health=brain_health(exit_code=1, prs=0),
                judge=NO_JUDGE,
                open_prs=0,
            )
        )
        assert "brain: exit 1 · 142s · 0 PRs" in out
        assert "\x1b[31m" in out

    @pytest.mark.parametrize(
        ("health", "expected"),
        [
            pytest.param(
                SpawnHealth(last=None, consecutive_failures=0, failing_since=None),
                "reviewer has never run — check the SessionEnd hook wiring",
                id="never-spawned",
            ),
            pytest.param(
                ok_health(ago=timedelta(days=8)),
                "reviewer last ran 8d ago — check the SessionEnd hook wiring",
                id="last-run-older-than-a-week",
            ),
        ],
    )
    def test_stale_reviewer_renders_yellow_warning(self, health: SpawnHealth, expected: str) -> None:
        out = plain(
            render(
                [],
                repo=REPO,
                settings=ReviewSettings(),
                watching=True,
                health=health,
                judge=NO_JUDGE,
                open_prs=0,
            )
        )
        assert expected in out
        assert "reviewer ok" not in out


class TestBrainSegment:
    def test_no_brain_run_returns_none(self) -> None:
        assert brain_segment({"judged": 4}) is None

    @pytest.mark.parametrize(
        ("report", "expected"),
        [
            pytest.param(
                {"eligible": [1], "brain_exit": 0, "brain_seconds": 142.0, "brain_prs": 1},
                ("brain: exit 0 · 142s · 1 PR", "dim"),
                id="one-pr-opened-is-dim",
            ),
            pytest.param(
                {"eligible": [1, 2], "brain_exit": 0, "brain_seconds": 88.6, "brain_prs": 2},
                ("brain: exit 0 · 89s · 2 PRs", "dim"),
                id="two-prs-plural-and-rounded",
            ),
            pytest.param(
                {"eligible": [1], "brain_exit": 2, "brain_seconds": 3.0, "brain_prs": 0},
                ("brain: exit 2 · 3s · 0 PRs", "red"),
                id="nonzero-exit-is-red",
            ),
            pytest.param(
                {"eligible": [1], "brain_exit": 0, "brain_seconds": 5.0, "brain_prs": 0},
                ("brain: exit 0 · 5s · 0 PRs", "red"),
                id="legacy-report-without-skips-and-no-pr-is-red",
            ),
            pytest.param(
                {"eligible": [1], "brain_exit": 0, "brain_seconds": 5.0, "brain_prs": 0, "brain_skips": 1},
                ("brain: exit 0 · 5s · 0 PRs", "dim"),
                id="skip-only-run-is-dim",
            ),
            pytest.param(
                {"eligible": [1, 2], "brain_exit": 0, "brain_seconds": 5.0, "brain_prs": 1, "brain_skips": 1},
                ("brain: exit 0 · 5s · 1 PR", "dim"),
                id="pr-plus-skip-covering-eligible-is-dim",
            ),
            pytest.param(
                {"eligible": [1, 2], "brain_exit": 0, "brain_seconds": 5.0, "brain_prs": 1, "brain_skips": 0},
                ("brain: exit 0 · 5s · 1 PR", "red"),
                id="unaccounted-eligible-candidate-is-red",
            ),
            pytest.param(
                {"eligible": [1], "brain_exit": 2, "brain_seconds": 5.0, "brain_prs": 0, "brain_skips": 1},
                ("brain: exit 2 · 5s · 0 PRs", "red"),
                id="nonzero-exit-red-even-with-skips",
            ),
        ],
    )
    def test_segment_text_and_style(self, report: dict[str, object], expected: tuple[str, str]) -> None:
        assert brain_segment(report) == expected


def seed_obs(
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
    store.store.conn.execute(INSERT_EVENT, (key, source, session, occurred, text, payload))
    store.record_observation(
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
        prompt_version=store.versions.create,
        model=model,
        fidelity="full",
    )


async def eligible_create(store: ReviewStore, *, rule: str, summary: str) -> int:
    candidate_id = store.ensure_candidate(
        REPO, kind=CandidateKind.CREATE, rule=rule, source_kind=SourceKind("transcript_message")
    )
    for i, (session, day) in enumerate([("s1", "2026-06-01"), ("s2", "2026-06-01"), ("s3", "2026-06-02")]):
        seed_obs(store, candidate_id, f"{rule}{i}", session=session, occurred=f"{day}T10:00:00+00:00")
        await judge_obs(store, f"{rule}{i}", summary=summary)
    return candidate_id


class TestEligibilityParity:
    async def test_crosses_thresholds_agrees_with_eligible(self, store: ReviewStore) -> None:
        settings = ReviewSettings()
        store.enable(REPO)
        ready = await eligible_create(store, rule="ready", summary="run tests")
        watching = store.ensure_candidate(
            REPO, kind=CandidateKind.CREATE, rule="early", source_kind=SourceKind("transcript_message")
        )
        seed_obs(store, watching, "early0", session="x1", occurred="2026-06-01T10:00:00+00:00")
        await judge_obs(store, "early0")
        for candidate_id, expected in ((ready, True), (watching, False)):
            status = store.threshold_status(candidate_id, settings=settings)
            direct = store.eligible(candidate_id, settings=settings)
            assert crosses_thresholds(status, settings=settings) == direct == expected


class TestOverview:
    async def test_overview_carries_eligibility_and_pr_summary(self, store: ReviewStore) -> None:
        settings = ReviewSettings()
        store.enable(REPO)
        await eligible_create(store, rule="ready", summary="run the suite before committing")
        views = store.overview(REPO, settings=settings)
        assert [v.eligible for v in views] == [True]
        assert views[0].summary == "run the suite before committing"

    async def test_batched_overview_matches_per_candidate_computation(self, store: ReviewStore) -> None:
        settings = ReviewSettings()
        store.enable(REPO)
        await eligible_create(store, rule="ready", summary="run the suite before committing")

        early = store.ensure_candidate(
            REPO, kind=CandidateKind.CREATE, rule="early", source_kind=SourceKind("transcript_message")
        )
        seed_obs(store, early, "early0", session="x1", occurred="2026-06-01T10:00:00+00:00")
        await judge_obs(store, "early0")

        rejected = store.ensure_candidate(
            REPO, kind=CandidateKind.CREATE, rule="nope", source_kind=SourceKind("transcript_message")
        )
        seed_obs(store, rejected, "nope0", session="x2", occurred="2026-06-02T10:00:00+00:00")
        await judge_obs(store, "nope0", accepted=False)

        fix = store.ensure_candidate(
            REPO,
            kind=CandidateKind.FIX,
            rule="fix-rule",
            source_kind=SourceKind("hook_complaint"),
            target_source_file="hooks/h.py",
            target_hook_name="h",
        )
        seed_obs(
            store,
            fix,
            "fix0",
            session="x3",
            occurred="2026-06-03T10:00:00+00:00",
            heuristic=0.95,
            source="hook_complaint",
        )
        await store.record_verdict(
            DedupKey("fix0"),
            FakeVerdict(accepted=True, summary="tighten the guard"),
            role="judge",
            prompt_version=store.versions.fix,
            model="m1",
            fidelity="full",
        )

        cross_target = store.ensure_candidate(
            RepoKey("github.com/yasyf/other"),
            kind=CandidateKind.CREATE,
            rule="cross-target",
            source_kind=SourceKind("transcript_message"),
        )
        store.transition(
            cross_target,
            CandidateStatus.PR_OPEN,
            pr_url=f"https://{REPO}/pull/42",
            pr_opened_at=datetime.now(UTC),
        )

        views = store.overview(REPO, settings=settings)
        assert len(views) == 4
        for v in views:
            cid = int(str(v.row["id"]))
            assert v.threshold == store.threshold_status(cid, settings=settings)
            assert v.eligible == store.eligible(cid, settings=settings)
            assert v.summary == store.pr_summary(cid, settings=settings)


def seed_spawn_runs(store: ReviewStore, *oks: bool) -> None:
    for day, ok in enumerate(oks, start=1):
        store.record_spawn_run(
            f"/t/{day}",
            started_at=datetime(2026, 6, day, tzinfo=UTC),
            ok=ok,
            error=None if ok else f"Boom: {day}",
            report_json='{"judged": 1}' if ok else None,
        )


class TestSpawnHealth:
    def test_no_rows_reports_never_run(self, store: ReviewStore) -> None:
        assert store.spawn_health() == SpawnHealth(last=None, consecutive_failures=0, failing_since=None)

    def test_streak_counts_failures_since_last_success(self, store: ReviewStore) -> None:
        seed_spawn_runs(store, False, True, False, False)
        health = store.spawn_health()
        assert health.consecutive_failures == 2
        assert health.failing_since == "2026-06-03T00:00:00+00:00"
        assert health.last is not None
        assert health.last["ok"] == 0
        assert health.last["error"] == "Boom: 4"
        assert health.last["transcript"] == "/t/4"

    def test_success_resets_the_streak(self, store: ReviewStore) -> None:
        seed_spawn_runs(store, False, True)
        health = store.spawn_health()
        assert health.consecutive_failures == 0
        assert health.failing_since is None
        assert health.last is not None
        assert health.last["ok"] == 1
        assert health.last["error"] is None


class TestUnwatchedCanary:
    def test_filters_cutoff_known_repos_and_failed_runs(self, store: ReviewStore) -> None:
        included = RepoKey("github.com/yasyf/included")
        outside = RepoKey("github.com/yasyf/outside")
        opted_out = RepoKey("github.com/yasyf/opted-out")
        enrolled = RepoKey("github.com/yasyf/enrolled")
        failed = RepoKey("github.com/yasyf/failed")
        store.disable(opted_out)
        store.enable(enrolled)
        for repo, started_at, ok in (
            (included, datetime.now(UTC) - timedelta(days=1), True),
            (outside, datetime.now(UTC) - timedelta(days=8), True),
            (opted_out, datetime.now(UTC) - timedelta(days=1), True),
            (enrolled, datetime.now(UTC) - timedelta(days=1), True),
            (failed, datetime.now(UTC) - timedelta(days=1), False),
        ):
            store.record_spawn_run(
                f"/t/{repo}",
                started_at=started_at,
                ok=ok,
                error=None if ok else "Boom: failed",
                report_json=json.dumps({"repo": str(repo), "watching": False}),
            )
        assert store.unwatched_session_repos() == [str(included)]

    def test_render_shows_warning_after_health(self) -> None:
        repos = ["github.com/yasyf/one", "github.com/yasyf/two"]
        out = plain(
            render(
                [],
                repo=REPO,
                settings=ReviewSettings(),
                watching=True,
                health=ok_health(),
                judge=NO_JUDGE,
                open_prs=0,
                unwatched=repos,
            )
        )
        assert out.splitlines()[1].startswith("reviewer ran for unwatched repos:")
        assert all(repo in out for repo in repos)
        assert "capt-hook review enable" in out

    def test_render_omits_warning_without_repos(self) -> None:
        out = plain(
            render(
                [],
                repo=REPO,
                settings=ReviewSettings(),
                watching=True,
                health=ok_health(),
                judge=NO_JUDGE,
                open_prs=0,
            )
        )
        assert "unwatched repos" not in out


def review_status(*args: str, root: Path) -> Result:
    return CliRunner().invoke(cli, ["--root", str(root), "review", "status", *args], env={"COLUMNS": "200"})


def top_status(*args: str, root: Path) -> Result:
    return CliRunner().invoke(cli, ["--root", str(root), "status", *args], env={"COLUMNS": "200"})


async def seed_db(scenario: str) -> int:
    with ReviewStore.open(ReviewSettings().db_path) as store:
        store.enable(REPO)
        watching = store.ensure_candidate(
            REPO, kind=CandidateKind.CREATE, rule="force", source_kind=SourceKind("transcript_message")
        )
        seed_obs(
            store, watching, "w0", session="a1", occurred="2026-06-01T10:00:00+00:00", text="never force-push"
        )
        await judge_obs(store, "w0", summary="block force-push to main")
        await eligible_create(store, rule="tests", summary="run the suite before committing")
        match scenario:
            case "pr_open":
                pr = store.ensure_candidate(
                    REPO, kind=CandidateKind.CREATE, rule="uv", source_kind=SourceKind("transcript_message")
                )
                seed_obs(store, pr, "p0", session="b1", occurred="2026-06-01T10:00:00+00:00", text="use uv")
                await judge_obs(store, "p0", summary="use uv instead of pip")
                store.transition(
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
        assert "reviewer has never run" in result.output

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
        monkeypatch.setattr(
            "captain_hook.review.sync.gh_pr_state", lambda _url: PrState("MERGED", "2026-07-08T15:06:25Z")
        )
        result = review_status(root=git_repo)
        assert result.exit_code == 0, result.output

        def status_of() -> str:
            with ReviewStore.open(ReviewSettings().db_path) as store:
                return str((store.candidate(candidate_id))["status"])

        assert status_of() == "accepted"
        assert "ACCEPTED" in result.output

    def test_status_outside_a_git_repo_fails(self, tmp_path: Path) -> None:
        result = review_status(root=tmp_path)
        assert result.exit_code != 0
        assert "is not a git repo with an 'origin' remote" in result.output
