from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from cc_transcript.activity import SessionActivity
from cc_transcript.context import SUMMARY_LABEL, ContextWindow, TurnRef, capture_window
from cc_transcript.ids import EventRef, EventUuid, SessionId
from cc_transcript.judge.llm import resolved_model
from cc_transcript.mining.candidates import FeedbackCandidate, dedup_key
from cc_transcript.mining.confidence import firm, noise
from cc_transcript.mining.sourcekind import TRANSCRIPT_MESSAGE
from pydantic import ValidationError

from captain_hook.cli import plugin_dir
from captain_hook.review.judge import (
    DURABLE_CATEGORIES,
    JUDGE_ROLE,
    REVIEW_PROMPT_VERSION,
    JudgeReport,
    ReviewVerdict,
    build_prompt,
    judge_pass,
)
from captain_hook.review.pipeline import (
    BRAIN_ALLOWED_TOOLS,
    SPAWNED_ENV,
    SpawnReport,
    brain_argv,
    brain_prompt,
    guard_and_spawn,
    review_session,
    spawn_argv,
    spawn_brain,
    spawn_session,
)
from captain_hook.review.repo import RepoKey
from captain_hook.review.scan import REVIEWER_MARKER, scan_transcript
from captain_hook.review.settings import ReviewSettings, resolve_review_db_path
from captain_hook.review.store import ReviewStore
from tests.review_helpers import (
    CORRECTION,
    REPO,
    assistant_text,
    assistant_tool_use,
    correction_entries,
    install_judge,
    parse,
    requires_llm_backend,
    user_text,
    write_transcript,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cc_transcript.mining.confidence import CandidateSignal

    from captain_hook.review.judge import Category

GIT_REPO_KEY = RepoKey("github.com/yasyf/scratch")
SECOND_CORRECTION = "never run pip directly, always go through uv in this repo"
ALL_CATEGORIES = (
    "durable_style_rule",
    "workflow_rule",
    "tooling_rule",
    "safety_guard",
    "one_off_correction",
    "task_specific",
    "preference_unclear",
    "ambient_noise",
)


def state_dir() -> Path:
    return Path(os.environ["CAPTAIN_HOOK_STATE_DIR"])


def run_review(stdin: bytes, *, env: dict[str, str] | None = None, cwd: Path | None = None):
    return subprocess.run(
        [sys.executable, "-m", "captain_hook", "review", "run"],
        input=stdin,
        capture_output=True,
        timeout=120,
        env=os.environ | (env or {}),
        cwd=cwd,
    )


def install_brain(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, Path]]:
    calls: list[tuple[Path, Path]] = []

    def fake(transcript: Path, *, repo_root: Path, settings: ReviewSettings) -> None:
        calls.append((transcript, repo_root))

    monkeypatch.setattr("captain_hook.review.pipeline.spawn_brain", fake)
    return calls


def synthetic(text: str, signal: CandidateSignal) -> FeedbackCandidate:
    anchor = EventRef(SessionId("s1"), EventUuid(f"u-{dedup_key('transcript_message', 's1', text)}"))
    return FeedbackCandidate(
        dedup_key=dedup_key("transcript_message", "s1", text),
        source_kind=TRANSCRIPT_MESSAGE,
        occurred_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        text=text,
        window=ContextWindow(
            anchor=anchor,
            before=(),
            trigger=TurnRef(role="assistant", refs=(), preview="did a thing", tool_digests=()),
            after=(),
            fidelity="full",
            preview_chars=200,
        ),
        ref=anchor,
        session_id=SessionId("s1"),
        signal=signal,
    )


async def seed_corrections(
    store: ReviewStore, settings: ReviewSettings, tmp_path: Path, texts: Sequence[str], *, session: str = "s1"
) -> None:
    entries = [
        entry
        for text in texts
        for entry in (assistant_text("attempt", sessionId=session), user_text(text, sessionId=session))
    ]
    await scan_transcript(
        store, write_transcript(tmp_path / f"{session}.jsonl", entries), settings=settings, repo_key=REPO
    )


@pytest.fixture
def projects_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr("cc_transcript.discovery.CLAUDE_PROJECTS_DIR", tmp_path)
    return tmp_path


async def verdict_fidelities(store: ReviewStore) -> list[str]:
    cur = await store.store.conn.execute("SELECT fidelity FROM verdicts ORDER BY id")
    return [str(row["fidelity"]) async for row in cur]


@pytest.fixture
def popen_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, Any]]]:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(
        "captain_hook.review.pipeline.subprocess.Popen", lambda argv, **kwargs: calls.append((argv, kwargs))
    )
    return calls


class TestExitZeroInvariant:
    @pytest.mark.parametrize(
        "stdin",
        [
            pytest.param(b"{}", id="empty-payload"),
            pytest.param(b"", id="empty-stdin"),
            pytest.param(b"not json {{{", id="garbage-text"),
            pytest.param(b"\xff\xfe\x00garbage", id="garbage-bytes"),
            pytest.param(b"[1, 2]", id="non-dict-payload"),
            pytest.param(json.dumps({"transcript_path": 42, "cwd": "/x"}).encode(), id="non-string-transcript"),
            pytest.param(json.dumps({"transcript_path": "/nonexistent/t.jsonl"}).encode(), id="missing-transcript"),
        ],
    )
    def test_exits_zero_silently_and_fast(self, stdin: bytes) -> None:
        start = time.monotonic()
        proc = run_review(stdin)
        assert time.monotonic() - start < 30
        assert proc.returncode == 0
        assert proc.stdout == b""

    def test_spawned_env_exits_zero_without_spawning(self, tmp_path: Path) -> None:
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        payload = json.dumps({"transcript_path": str(transcript), "cwd": str(tmp_path)}).encode()
        proc = run_review(payload, env={SPAWNED_ENV: "1"})
        assert proc.returncode == 0
        assert proc.stdout == b""
        assert not (state_dir() / "review").exists()

    def test_non_git_cwd_exits_zero_and_detached_child_finishes_clean(self, tmp_path: Path) -> None:
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        payload = json.dumps({"transcript_path": str(transcript), "cwd": str(tmp_path), "reason": "other"}).encode()
        start = time.monotonic()
        proc = run_review(payload, cwd=tmp_path)
        assert time.monotonic() - start < 30
        assert proc.returncode == 0
        assert proc.stdout == b""
        log = state_dir() / "review" / "spawn.log"
        assert log.exists()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if "SpawnReport(repo=None" in log.read_text():
                return
            time.sleep(0.2)
        pytest.fail(f"detached child never finished: {log.read_text()!r}")

    def test_sdk_entrypoint_exits_zero_without_spawning(self, tmp_path: Path) -> None:
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        payload = json.dumps({"transcript_path": str(transcript), "cwd": str(tmp_path), "reason": "other"}).encode()
        proc = run_review(payload, env={"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, cwd=tmp_path)
        assert proc.returncode == 0
        assert proc.stdout == b""
        assert not (state_dir() / "review").exists()


class TestGuardAndSpawn:
    def test_spawns_detached_child_with_log_and_marker_env(
        self, popen_calls: list[tuple[list[str], dict[str, Any]]], tmp_path: Path
    ) -> None:
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        guard_and_spawn(
            json.dumps({"transcript_path": str(transcript), "cwd": str(tmp_path), "reason": "other"}).encode()
        )
        [(argv, kwargs)] = popen_calls
        assert argv == spawn_argv(str(transcript), str(tmp_path))
        assert argv[:5] == [sys.executable, "-m", "captain_hook", "review", "spawn"]
        assert kwargs["start_new_session"] is True
        assert kwargs["env"][SPAWNED_ENV] == "1"
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert Path(kwargs["stdout"].name) == state_dir() / "review" / "spawn.log"
        assert kwargs["stderr"] is kwargs["stdout"]

    def test_omits_cwd_flag_when_payload_has_none(
        self, popen_calls: list[tuple[list[str], dict[str, Any]]], tmp_path: Path
    ) -> None:
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        guard_and_spawn(json.dumps({"transcript_path": str(transcript), "reason": "other"}).encode())
        [(argv, _)] = popen_calls
        assert "--cwd" not in argv

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param(b"", id="empty"),
            pytest.param(b"not json {{{", id="garbage"),
            pytest.param(b"\xff\xfe\x00", id="bad-utf8"),
            pytest.param(b"[1, 2]", id="non-dict"),
            pytest.param(b"{}", id="no-transcript"),
            pytest.param(json.dumps({"transcript_path": 42}).encode(), id="non-string-transcript"),
            pytest.param(json.dumps({"transcript_path": "/nonexistent/t.jsonl"}).encode(), id="missing-file"),
            pytest.param(json.dumps({"transcript_path": "bad\x00null"}).encode(), id="null-byte-path"),
        ],
    )
    def test_guards_never_spawn(self, popen_calls: list[tuple[list[str], dict[str, Any]]], raw: bytes) -> None:
        guard_and_spawn(raw)
        assert popen_calls == []

    def test_spawned_env_guard_blocks_respawn(
        self, popen_calls: list[tuple[list[str], dict[str, Any]]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(SPAWNED_ENV, "1")
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        guard_and_spawn(json.dumps({"transcript_path": str(transcript)}).encode())
        assert popen_calls == []

    @pytest.mark.parametrize(
        ("entrypoint", "spawns"),
        [
            pytest.param("cli", True, id="interactive-cli"),
            pytest.param("vscode", True, id="interactive-vscode"),
            pytest.param("sdk-cli", False, id="headless-sdk-cli"),
            pytest.param("sdk-py", False, id="headless-sdk-py"),
        ],
    )
    def test_entrypoint_gates_spawn(
        self,
        popen_calls: list[tuple[list[str], dict[str, Any]]],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        entrypoint: str,
        spawns: bool,
    ) -> None:
        monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", entrypoint)
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        guard_and_spawn(
            json.dumps({"transcript_path": str(transcript), "cwd": str(tmp_path), "reason": "other"}).encode()
        )
        assert bool(popen_calls) is spawns


class TestJudgePass:
    @requires_llm_backend
    async def test_judges_all_then_noop(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = install_judge(monkeypatch)
        await seed_corrections(store, settings, tmp_path, [CORRECTION, SECOND_CORRECTION])
        report = await judge_pass(store, settings=settings)
        assert report == JudgeReport(judged=2, failed=0, pending=0)
        assert len(calls) == 2
        judged = await store.judged(role=JUDGE_ROLE, prompt_version=REVIEW_PROMPT_VERSION)
        assert {bool(row["accepted"]) for row in judged} == {True}
        assert {str(row["model"]) for row in judged} == {resolved_model(settings.judge_tier)}
        assert await judge_pass(store, settings=settings) == JudgeReport(judged=0, failed=0, pending=0)
        assert len(calls) == 2

    @requires_llm_backend
    async def test_cap_limits_calls_and_pending_rows_retry_next_pass(
        self, store: ReviewStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db", max_judge_calls_per_session=1)
        calls = install_judge(monkeypatch)
        await seed_corrections(store, settings, tmp_path, [CORRECTION, SECOND_CORRECTION])
        assert await judge_pass(store, settings=settings) == JudgeReport(judged=1, failed=0, pending=1)
        assert len(calls) == 1
        assert await judge_pass(store, settings=settings) == JudgeReport(judged=1, failed=0, pending=0)

    @requires_llm_backend
    async def test_limit_overrides_the_session_cap(
        self, store: ReviewStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db", max_judge_calls_per_session=1)
        calls = install_judge(monkeypatch)
        await seed_corrections(store, settings, tmp_path, [CORRECTION, SECOND_CORRECTION])
        assert await judge_pass(store, settings=settings, limit=2) == JudgeReport(judged=2, failed=0, pending=0)
        assert len(calls) == 2

    @requires_llm_backend
    async def test_noise_floor_rows_never_reach_the_judge(
        self, store: ReviewStore, settings: ReviewSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = install_judge(monkeypatch)
        await store.record_file_scan(
            "synthetic", 1.0, [synthetic("structural junk", noise("bare_marker")), synthetic(CORRECTION, firm())]
        )
        assert await judge_pass(store, settings=settings) == JudgeReport(judged=1, failed=0, pending=0)
        assert len(calls) == 1
        assert "structural junk" not in calls[0]
        unjudged = await store.unjudged(
            role=JUDGE_ROLE, prompt_version=REVIEW_PROMPT_VERSION, model=resolved_model(settings.judge_tier)
        )
        assert [row["text"] for row in unjudged] == ["structural junk"]

    @requires_llm_backend
    async def test_failed_judge_leaves_row_unjudged_for_retry(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_judge(monkeypatch, fail_on=f"FEEDBACK TO CLASSIFY ===\n{SECOND_CORRECTION}")
        await seed_corrections(store, settings, tmp_path, [CORRECTION, SECOND_CORRECTION])
        assert await judge_pass(store, settings=settings) == JudgeReport(judged=1, failed=1, pending=1)
        install_judge(monkeypatch)
        assert await judge_pass(store, settings=settings) == JudgeReport(judged=1, failed=0, pending=0)

    @pytest.mark.parametrize("category", ALL_CATEGORIES)
    def test_accepted_derives_from_durable_categories(self, category: Category) -> None:
        verdict = ReviewVerdict(category=category, summary="s", confidence=0.5, rationale="r")
        assert verdict.accepted is (category in DURABLE_CATEGORIES)

    async def test_build_prompt_renders_context_trigger_and_text(self) -> None:
        events = parse(
            [
                user_text("add the parser"),
                assistant_tool_use("t1", "Bash", {"command": "pip install foo"}),
                user_text(CORRECTION),
            ]
        )
        activity = SessionActivity.from_events(SessionId("sess-1"), events)
        window = capture_window(activity, EventRef(SessionId("sess-1"), events[-1].meta.uuid))
        row = {"source_kind": "transcript_message", "context_json": window.to_json(), "text": CORRECTION}
        prompt, fidelity = await build_prompt(row)
        assert fidelity == "summary"
        assert "[source: transcript_message]" in prompt
        assert "add the parser" in prompt
        assert "pip install foo" in prompt
        assert CORRECTION in prompt
        assert "DURABLE correction worth encoding as an" in prompt


@requires_llm_backend
class TestFidelity:
    async def test_verdict_records_full_fidelity_while_the_transcript_lives(
        self,
        store: ReviewStore,
        settings: ReviewSettings,
        tmp_path: Path,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = install_judge(monkeypatch)
        await seed_corrections(store, settings, tmp_path, [CORRECTION])
        assert await judge_pass(store, settings=settings) == JudgeReport(judged=1, failed=0, pending=0)
        assert await verdict_fidelities(store) == ["full"]
        assert SUMMARY_LABEL not in calls[0]
        assert "the turn the feedback arrived in" in calls[0]

    async def test_expired_transcript_judges_at_summary_with_the_label(
        self,
        store: ReviewStore,
        settings: ReviewSettings,
        tmp_path: Path,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = install_judge(monkeypatch)
        await seed_corrections(store, settings, tmp_path, [CORRECTION])
        (tmp_path / "s1.jsonl").unlink()
        assert await judge_pass(store, settings=settings) == JudgeReport(judged=1, failed=0, pending=0)
        assert await verdict_fidelities(store) == ["summary"]
        assert SUMMARY_LABEL in calls[0]

    async def test_refresh_summary_rejudges_once_the_window_hydrates_again(
        self,
        store: ReviewStore,
        settings: ReviewSettings,
        tmp_path: Path,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = install_judge(monkeypatch)
        await seed_corrections(store, settings, tmp_path, [CORRECTION])
        transcript = tmp_path / "s1.jsonl"
        content = transcript.read_text()
        transcript.unlink()
        assert await judge_pass(store, settings=settings) == JudgeReport(judged=1, failed=0, pending=0)
        assert await verdict_fidelities(store) == ["summary"]
        assert await judge_pass(store, settings=settings) == JudgeReport(judged=0, failed=0, pending=0)
        transcript.write_text(content)
        assert await judge_pass(store, settings=settings, refresh_summary=True) == JudgeReport(
            judged=1, failed=0, pending=0
        )
        assert await verdict_fidelities(store) == ["full"]
        assert len(calls) == 2
        assert await judge_pass(store, settings=settings, refresh_summary=True) == JudgeReport(
            judged=0, failed=0, pending=0
        )


class TestBrain:
    def test_brain_argv_composes_backend_base_with_reviewer_scope(self) -> None:
        argv = brain_argv(max_turns=40, max_budget_usd=5.0)
        assert argv[:2] == ["claude", "-p"]
        assert "--no-session-persistence" in argv
        assert argv[argv.index("--model") + 1] == "sonnet"
        assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
        assert "auto" not in argv
        assert argv[argv.index("--max-turns") + 1] == "40"
        assert argv[argv.index("--allowedTools") + 1] == ",".join(BRAIN_ALLOWED_TOOLS)
        assert argv[argv.index("--max-budget-usd") + 1] == "5.0"
        assert argv[argv.index("--plugin-dir") + 1] == str(plugin_dir())

    def test_brain_prompt_carries_skill_and_reviewer_marker(self) -> None:
        prompt = brain_prompt(Path("/tmp/t.jsonl"))
        assert prompt.startswith("/captain-hook:scanning-sessions --transcript /tmp/t.jsonl")
        assert REVIEWER_MARKER in prompt

    def test_spawn_brain_runs_in_repo_with_marker_env_and_prompt_on_stdin(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        runs: list[tuple[list[str], dict[str, Any]]] = []
        monkeypatch.setattr("captain_hook.review.pipeline.subprocess.run", lambda argv, **kw: runs.append((argv, kw)))
        spawn_brain(
            tmp_path / "t.jsonl",
            repo_root=tmp_path,
            settings=ReviewSettings(brain_max_turns=7, brain_max_budget_usd=2.5),
        )
        [(argv, kwargs)] = runs
        assert argv == brain_argv(max_turns=7, max_budget_usd=2.5)
        assert kwargs["cwd"] == tmp_path
        assert kwargs["env"][SPAWNED_ENV] == "1"
        assert REVIEWER_MARKER in kwargs["input"].decode()
        assert Path(kwargs["stdout"].name) == state_dir() / "review" / "spawn.log"


class TestSpawnSession:
    async def test_success_records_ok_row_with_report_json(self, tmp_path: Path, git_repo: Path) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db")
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        report = await spawn_session(transcript, cwd=str(git_repo), settings=settings)
        assert report == SpawnReport(repo=GIT_REPO_KEY)
        async with await ReviewStore.open(settings.db_path) as store:
            health = await store.spawn_health()
        assert health.consecutive_failures == 0
        assert health.failing_since is None
        assert health.last is not None
        assert health.last["ok"] == 1
        assert health.last["error"] is None
        assert health.last["transcript"] == str(transcript)
        assert json.loads(str(health.last["report_json"])) == {
            "repo": str(GIT_REPO_KEY),
            "watching": False,
            "scanned": 0,
            "inserted": 0,
            "judged": 0,
            "failed": 0,
            "eligible": [],
            "brain": False,
        }

    @pytest.mark.parametrize(
        ("exc", "recorded"),
        [
            pytest.param(
                RuntimeError("no such column: fidelity"),
                "RuntimeError: no such column: fidelity",
                id="exception",
            ),
            pytest.param(
                KeyboardInterrupt("cancelled"),
                "KeyboardInterrupt: cancelled",
                id="base-exception-cancellation-shape",
            ),
        ],
    )
    async def test_crash_records_failure_row_and_reraises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: BaseException, recorded: str
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db")

        async def boom(transcript: Path, *, cwd: str, settings: ReviewSettings) -> SpawnReport:
            raise exc

        monkeypatch.setattr("captain_hook.review.pipeline.review_session", boom)
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        with pytest.raises(type(exc)):
            await spawn_session(transcript, cwd=str(tmp_path), settings=settings)
        async with await ReviewStore.open(settings.db_path) as store:
            health = await store.spawn_health()
        assert health.consecutive_failures == 1
        assert health.last is not None
        assert health.last["ok"] == 0
        assert health.last["error"] == recorded
        assert health.last["report_json"] is None
        assert health.failing_since == health.last["started_at"]

    async def test_settings_failure_records_failure_row_at_default_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOOKS_REVIEW_JUDGE_CONCURRENCY", "not-a-number")
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        with pytest.raises(ValidationError):
            await spawn_session(transcript, cwd=str(tmp_path))
        async with await ReviewStore.open(resolve_review_db_path()) as store:
            health = await store.spawn_health()
        assert health.consecutive_failures == 1
        assert health.last is not None
        assert health.last["ok"] == 0
        assert str(health.last["error"]).startswith("ValidationError:")
        assert "judge_concurrency" in str(health.last["error"])


class TestReviewSession:
    async def test_non_git_cwd_is_a_clean_skip(self, tmp_path: Path) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db")
        plain = tmp_path / "plain"
        plain.mkdir()
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        assert await review_session(transcript, cwd=str(plain), settings=settings) == SpawnReport(repo=None)
        assert not settings.db_path.exists()

    async def test_unwatched_repo_skips_scan_judge_and_brain(
        self, tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db")
        calls = install_judge(monkeypatch)
        brains = install_brain(monkeypatch)
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        report = await review_session(transcript, cwd=str(git_repo), settings=settings)
        assert report == SpawnReport(repo=GIT_REPO_KEY)
        assert calls == []
        assert brains == []
        async with await ReviewStore.open(settings.db_path) as store:
            assert await store.file_mtimes() == {}

    @pytest.mark.parametrize(
        ("category", "expect_brain"),
        [
            pytest.param("durable_style_rule", True, id="judge-accepts-brain-spawns"),
            pytest.param("one_off_correction", False, id="judge-rejects-brain-stays-down"),
        ],
    )
    @requires_llm_backend
    async def test_brain_spawns_only_when_judge_accepted_evidence_crosses_thresholds(
        self,
        tmp_path: Path,
        git_repo: Path,
        projects_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        category: Category,
        expect_brain: bool,
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db")
        install_judge(monkeypatch, category=category)
        brains = install_brain(monkeypatch)
        async with await ReviewStore.open(settings.db_path) as store:
            await store.enable(GIT_REPO_KEY)
        sessions = [
            ("s1", "2026-06-01T10:00:00+00:00"),
            ("s2", "2026-06-01T15:00:00+00:00"),
            ("s3", "2026-06-02T10:00:00+00:00"),
        ]
        reports = []
        for session, timestamp in sessions:
            transcript = write_transcript(
                tmp_path / f"{session}.jsonl", correction_entries(session=session, timestamp=timestamp)
            )
            reports.append(await review_session(transcript, cwd=str(git_repo), settings=settings))
        assert [report.judged for report in reports] == [1, 1, 1]
        assert [report.brain for report in reports[:2]] == [False, False]
        assert reports[2].brain is expect_brain
        assert len(brains) == (1 if expect_brain else 0)
        if expect_brain:
            assert reports[2].eligible != ()
            assert brains[0][1] == Path(str(git_repo))
