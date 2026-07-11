from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from cc_transcript.activity import SessionActivity
from cc_transcript.context import SUMMARY_LABEL, ContextWindow, TurnRef, capture_window
from cc_transcript.ids import EventRef, EventUuid, SessionId
from cc_transcript.judge import canonical_slug
from cc_transcript.judge.llm import resolved_model
from cc_transcript.judge.similar import Suggestion, suggest_canonical_keys
from cc_transcript.mining.candidates import DedupKey, FeedbackCandidate, dedup_key
from cc_transcript.mining.confidence import VERY_HIGH, CandidateSignal, Confidence, firm, noise, to_payload
from cc_transcript.mining.sourcekind import TRANSCRIPT_MESSAGE, SourceKind
from pydantic import ValidationError

from captain_hook.cli import plugin_dir
from captain_hook.review.fix import HOOK_COMPLAINT
from captain_hook.review.judge import (
    DURABLE_CATEGORIES,
    JUDGE_ROLE,
    JudgeReport,
    ReviewVerdict,
    build_create_prompt,
    build_fix_prompt,
    build_prompt,
    judge_pass,
    question_answer_block,
)
from captain_hook.review.pipeline import (
    BRAIN_ALLOWED_TOOLS,
    SPAWNED_ENV,
    BrainOutcome,
    SpawnReport,
    brain_argv,
    brain_prompt,
    guard_and_spawn,
    review_log_path,
    review_session,
    spawn_argv,
    spawn_brain,
    spawn_session,
)
from captain_hook.review.repo import RepoKey
from captain_hook.review.scan import REVIEWER_MARKER, ScanReport, scan_transcript
from captain_hook.review.settings import ReviewSettings, resolve_review_db_path
from captain_hook.review.store import CandidateKind, CandidateStatus, PromptVersions, ReviewStore
from captain_hook.review.sync import PrState
from tests.review_helpers import (
    CORRECTION,
    REPO,
    Verdict,
    ask_user_question_round,
    assistant_text,
    assistant_tool_use,
    correction_entries,
    default_slug_for,
    install_fake_embedder,
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
THIRD_CORRECTION = "never use os.path in this repo, always use pathlib for filesystem work"
FIX_TARGET_FILE = ".claude/hooks/style.py"
FIX_TARGET_HOOK = "style:nudge_deadbeef"
QUESTION = "Which HTML parser should we standardize on?"
OTHER_QUESTION = "Which cache backend should we use for the session store?"
ANSWER = "use selectolax everywhere, it is much faster than lxml for our workload"
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

    def fake(transcript: Path, *, repo_root: Path, settings: ReviewSettings) -> BrainOutcome:
        calls.append((transcript, repo_root))
        return BrainOutcome(exit_code=0, seconds=0.0, log_path=review_log_path())

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


async def seed_eligible_fix(store: ReviewStore, *, repo: RepoKey) -> int:
    candidate_id = await store.ensure_candidate(
        repo,
        kind=CandidateKind.FIX,
        rule=dedup_key("hook_complaint", FIX_TARGET_HOOK, FIX_TARGET_FILE),
        source_kind=SourceKind(HOOK_COMPLAINT),
        target_source_file=FIX_TARGET_FILE,
        target_hook_name=FIX_TARGET_HOOK,
        misfire_class="refire",
    )
    key = dedup_key("hook_complaint", "fs1", FIX_TARGET_HOOK)
    payload = json.dumps({"signal": to_payload(CandidateSignal(Confidence(VERY_HIGH), ("marker",)))})
    await store.store.conn.execute(
        "INSERT INTO feedback_events (dedup_key, source_kind, session_id, occurred_at, text, payload_json, "
        "context_json, ingested_at) VALUES (?, ?, ?, ?, ?, ?, '{}', '2026-06-01T00:00:00+00:00')",
        (key, HOOK_COMPLAINT, "fs1", "2026-06-01T10:00:00+00:00", "that reminder misfired again", payload),
    )
    await store.record_observation(
        candidate_id,
        dedup_key=DedupKey(key),
        session_id=SessionId("fs1"),
        occurred_at=datetime.fromisoformat("2026-06-01T10:00:00+00:00"),
    )
    await store.record_verdict(
        DedupKey(key),
        Verdict(summary="stop the nudge misfiring on its own text"),
        role=JUDGE_ROLE,
        prompt_version=store.versions.fix,
        model="m1",
        fidelity="full",
    )
    return candidate_id


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
        assert report == JudgeReport(judged=2, failed=0, pending=0, merged=2, retired=0, reopened=0)
        assert len(calls) == 2
        judged = await store.judged(role=JUDGE_ROLE, prompt_version=store.versions.create)
        assert {bool(row["accepted"]) for row in judged} == {True}
        assert {str(row["model"]) for row in judged} == {resolved_model(settings.judge_tier)}
        assert await judge_pass(store, settings=settings) == JudgeReport(
            judged=0, failed=0, pending=0, merged=0, retired=0, reopened=0
        )
        assert len(calls) == 2

    @requires_llm_backend
    async def test_cap_limits_calls_and_pending_rows_retry_next_pass(
        self, store: ReviewStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db", max_judge_calls_per_session=1)
        calls = install_judge(monkeypatch)
        await seed_corrections(store, settings, tmp_path, [CORRECTION, SECOND_CORRECTION])
        assert await judge_pass(store, settings=settings) == JudgeReport(
            judged=1, failed=0, pending=1, merged=1, retired=0, reopened=0
        )
        assert len(calls) == 1
        assert await judge_pass(store, settings=settings) == JudgeReport(
            judged=1, failed=0, pending=0, merged=1, retired=0, reopened=0
        )

    @requires_llm_backend
    async def test_limit_overrides_the_session_cap(
        self, store: ReviewStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db", max_judge_calls_per_session=1)
        calls = install_judge(monkeypatch)
        await seed_corrections(store, settings, tmp_path, [CORRECTION, SECOND_CORRECTION])
        assert await judge_pass(store, settings=settings, limit=2) == JudgeReport(
            judged=2, failed=0, pending=0, merged=2, retired=0, reopened=0
        )
        assert len(calls) == 2

    @requires_llm_backend
    async def test_noise_floor_rows_never_reach_the_judge(
        self, store: ReviewStore, settings: ReviewSettings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = install_judge(monkeypatch)
        await store.record_file_scan(
            "synthetic", 1.0, [synthetic("structural junk", noise("bare_marker")), synthetic(CORRECTION, firm())]
        )
        assert await judge_pass(store, settings=settings) == JudgeReport(
            judged=1, failed=0, pending=0, merged=0, retired=0, reopened=0
        )
        assert len(calls) == 1
        assert "structural junk" not in calls[0]
        unjudged = await store.unjudged(role=JUDGE_ROLE, prompt_version=store.versions.create)
        assert [row["text"] for row in unjudged] == ["structural junk"]

    @requires_llm_backend
    async def test_failed_judge_leaves_row_unjudged_for_retry(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_judge(monkeypatch, fail_on=f"FEEDBACK TO CLASSIFY ===\n{SECOND_CORRECTION}")
        await seed_corrections(store, settings, tmp_path, [CORRECTION, SECOND_CORRECTION])
        assert await judge_pass(store, settings=settings) == JudgeReport(
            judged=1, failed=1, pending=1, merged=1, retired=0, reopened=0
        )
        install_judge(monkeypatch)
        assert await judge_pass(store, settings=settings) == JudgeReport(
            judged=1, failed=0, pending=0, merged=1, retired=0, reopened=0
        )

    @requires_llm_backend
    async def test_suggestion_retrieval_failure_counts_row_failed_not_crash(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_judge(monkeypatch)
        await seed_corrections(store, settings, tmp_path, [CORRECTION])

        async def boom(*_: object, **__: object) -> list[object]:
            raise RuntimeError("sqlite-vec extension failed to load")

        async def has_evidence(self: ReviewStore) -> bool:
            return True

        monkeypatch.setattr("cc_transcript.judge.similar.suggest_canonical_keys", boom)
        monkeypatch.setattr("cc_transcript.judge.similar.default_embedder", lambda: lambda text: text)
        monkeypatch.setattr(ReviewStore, "has_verdict_evidence", has_evidence)
        assert await judge_pass(store, settings=settings) == JudgeReport(
            judged=0, failed=1, pending=1, merged=0, retired=0, reopened=0
        )
        assert [row["text"] for row in await store.unjudged(role=JUDGE_ROLE, prompt_version=store.versions.create)] == [
            CORRECTION
        ]

    @requires_llm_backend
    async def test_cold_corpus_prewarms_the_embedder_once_for_durable_creates(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import numpy as np
        from cc_transcript.judge.similar import EMBED_DIM, Embedder

        install_judge(monkeypatch)
        loads: list[None] = []

        def embedder(_text: str) -> np.ndarray:
            return np.ones(EMBED_DIM, dtype=np.float32)

        def loader() -> Embedder:
            if not loads:
                time.sleep(0.1)  # widen the concurrent-miss window the cached loader leaves open
                loads.append(None)
            return embedder

        monkeypatch.setattr("cc_transcript.judge.similar.default_embedder", loader)
        await seed_corrections(store, settings, tmp_path, [CORRECTION, SECOND_CORRECTION])
        assert not await store.has_verdict_evidence()
        assert await judge_pass(store, settings=settings) == JudgeReport(
            judged=2, failed=0, pending=0, merged=2, retired=0, reopened=0
        )
        assert loads == [None]

    @requires_llm_backend
    async def test_persists_each_lane_at_its_bound_version(
        self, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fake_embedder(monkeypatch)

        async def judge(prompt: str) -> ReviewVerdict:
            if "REMARK TO CLASSIFY" in prompt:
                return ReviewVerdict(category="misfire_confirmed", summary="s", confidence=0.9, rationale="r")
            return ReviewVerdict(
                category="durable_style_rule",
                summary="s",
                confidence=0.9,
                rationale="r",
                rule_slug="prefer-uv-over-pip",
            )

        monkeypatch.setattr("captain_hook.review.judge.structured_judge", lambda *_, **__: judge)
        async with await ReviewStore.open(tmp_path / "lanes.db", versions=PromptVersions(create=4, fix=3)) as store:
            await seed_corrections(store, settings, tmp_path, [CORRECTION])
            window = ContextWindow(
                anchor=EventRef(SessionId("fix1"), EventUuid("uf1")),
                before=(TurnRef(role="assistant", refs=(), preview="running git status", tool_digests=()),),
                trigger=None,
                after=(),
                fidelity="full",
                preview_chars=200,
            )
            payload = json.dumps(
                {
                    "signal": to_payload(firm()),
                    "target_hook_name": "status_nudge:nudge_c424798f",
                    "event": "PreToolUse",
                    "action": "warn",
                    "fire_message": "Remember to use the project's task tracker.",
                }
            )
            await store.store.conn.execute(
                "INSERT INTO feedback_events "
                "(dedup_key, source_kind, session_id, occurred_at, text, payload_json, context_json, ingested_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, '2026-06-01T00:00:00+00:00')",
                (
                    dedup_key("hook_complaint", "fix1", "status_nudge"),
                    HOOK_COMPLAINT,
                    "fix1",
                    "2026-06-01T12:00:00+00:00",
                    "that task-tracker reminder re-fired on text I already handled - ignoring it",
                    payload,
                    window.to_json(),
                ),
            )
            await judge_pass(store, settings=settings)
            cur = await store.store.conn.execute(
                "SELECT e.source_kind AS source_kind, v.prompt_version AS prompt_version "
                "FROM verdicts v JOIN feedback_events e ON e.dedup_key = v.dedup_key WHERE v.role = 'judge'"
            )
            lanes = {str(row["source_kind"]): int(row["prompt_version"]) async for row in cur}
            assert lanes == {TRANSCRIPT_MESSAGE: store.versions.create, HOOK_COMPLAINT: store.versions.fix}

    @pytest.mark.parametrize("category", ALL_CATEGORIES)
    def test_accepted_derives_from_durable_categories(self, category: Category) -> None:
        slug = "a-durable-rule" if category in DURABLE_CATEGORIES else None
        verdict = ReviewVerdict(category=category, summary="s", confidence=0.5, rationale="r", rule_slug=slug)
        assert verdict.accepted is (category in DURABLE_CATEGORIES)

    def test_non_durable_category_rejects_a_rule_slug(self) -> None:
        with pytest.raises(ValidationError):
            ReviewVerdict(
                category="one_off_correction", summary="s", confidence=0.5, rationale="r", rule_slug="poisoned-rule"
            )

    def test_fix_category_rejects_a_rule_slug(self) -> None:
        with pytest.raises(ValidationError):
            ReviewVerdict(
                category="misfire_confirmed", summary="s", confidence=0.5, rationale="r", rule_slug="poisoned-rule"
            )

    def test_durable_category_accepts_a_rule_slug(self) -> None:
        verdict = ReviewVerdict(
            category="tooling_rule", summary="s", confidence=0.5, rationale="r", rule_slug="prefer-uv-over-pip"
        )
        assert verdict.rule_slug == "prefer-uv-over-pip"
        assert verdict.canonical_key == "prefer-uv-over-pip"

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


class TestQuestionAnswer:
    async def test_answered_round_scans_to_one_candidate_with_question_digest(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path
    ) -> None:
        transcript = write_transcript(
            tmp_path / "s.jsonl",
            [
                user_text("decide the parser"),
                *ask_user_question_round(QUESTION, notes=ANSWER, options=("lxml", "html.parser (Recommended)")),
            ],
        )
        report = await scan_transcript(store, transcript, settings=settings, repo_key=REPO)
        assert report == ScanReport(scanned=1, inserted=1)
        [row] = await store.candidates(REPO)
        assert row["source_kind"] == "question_answer"
        assert row["rule"] == dedup_key("question_answer", QUESTION, ANSWER)
        assert row["observations"] == 1
        assert row["sample_text"] == ANSWER

    async def test_same_question_coalesces_across_sessions_but_distinct_questions_do_not(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path
    ) -> None:
        for session in ("s1", "s2"):
            await scan_transcript(
                store,
                write_transcript(
                    tmp_path / f"{session}.jsonl", ask_user_question_round(QUESTION, notes=ANSWER, session=session)
                ),
                settings=settings,
                repo_key=REPO,
            )
        [coalesced] = await store.candidates(REPO)
        assert coalesced["rule"] == dedup_key("question_answer", QUESTION, ANSWER)
        assert coalesced["observations"] == 2
        await scan_transcript(
            store,
            write_transcript(
                tmp_path / "s3.jsonl", ask_user_question_round(OTHER_QUESTION, notes=ANSWER, session="s3")
            ),
            settings=settings,
            repo_key=REPO,
        )
        assert {row["rule"] for row in await store.candidates(REPO)} == {
            dedup_key("question_answer", QUESTION, ANSWER),
            dedup_key("question_answer", OTHER_QUESTION, ANSWER),
        }

    def test_build_create_prompt_renders_question_block_for_question_answer_only(self) -> None:
        payload = {"question": QUESTION, "picked_labels": [], "multi_select": False, "recommended_pick": False}
        qa_row = {
            "source_kind": "question_answer",
            "payload_json": json.dumps(payload),
            "text": ANSWER,
            "context_json": "{}",
        }
        qa_prompt = build_create_prompt(qa_row, "CONTEXT")
        assert "=== QUESTION THE ASSISTANT ASKED ===" in qa_prompt
        assert QUESTION in qa_prompt
        assert "developer's answer to that question" in qa_prompt
        assert qa_prompt.endswith(f"=== FEEDBACK TO CLASSIFY ===\n{ANSWER}")
        tm_row = {
            "source_kind": "transcript_message",
            "payload_json": "null",
            "text": CORRECTION,
            "context_json": "{}",
        }
        tm_prompt = build_create_prompt(tm_row, "CONTEXT")
        assert "QUESTION THE ASSISTANT ASKED" not in tm_prompt
        assert CORRECTION in tm_prompt

    def test_question_block_marks_recommended_pick_and_multi_select(self) -> None:
        payload = {
            "question": QUESTION,
            "picked_labels": ["lxml", "html.parser (Recommended)"],
            "multi_select": True,
            "recommended_pick": True,
        }
        row = {
            "source_kind": "question_answer",
            "payload_json": json.dumps(payload),
            "text": ANSWER,
            "context_json": "{}",
        }
        block = question_answer_block(row)
        assert "options lxml; html.parser (Recommended)" in block
        assert "the option the assistant marked (Recommended)" in block


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
        assert await judge_pass(store, settings=settings) == JudgeReport(
            judged=1, failed=0, pending=0, merged=1, retired=0, reopened=0
        )
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
        assert await judge_pass(store, settings=settings) == JudgeReport(
            judged=1, failed=0, pending=0, merged=1, retired=0, reopened=0
        )
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
        assert await judge_pass(store, settings=settings) == JudgeReport(
            judged=1, failed=0, pending=0, merged=1, retired=0, reopened=0
        )
        assert await verdict_fidelities(store) == ["summary"]
        assert await judge_pass(store, settings=settings) == JudgeReport(
            judged=0, failed=0, pending=0, merged=0, retired=0, reopened=0
        )
        transcript.write_text(content)
        assert await judge_pass(store, settings=settings, refresh_summary=True) == JudgeReport(
            judged=1, failed=0, pending=0, merged=0, retired=0, reopened=0
        )
        assert await verdict_fidelities(store) == ["full"]
        assert len(calls) == 2
        assert await judge_pass(store, settings=settings, refresh_summary=True) == JudgeReport(
            judged=0, failed=0, pending=0, merged=0, retired=0, reopened=0
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

        def fake_run(argv: list[str], **kw: Any) -> SimpleNamespace:
            runs.append((argv, kw))
            return SimpleNamespace(returncode=5)

        monkeypatch.setattr("captain_hook.review.pipeline.subprocess.run", fake_run)
        outcome = spawn_brain(
            tmp_path / "t.jsonl",
            repo_root=tmp_path,
            settings=ReviewSettings(brain_max_turns=7, brain_max_budget_usd=2.5),
        )
        assert outcome.exit_code == 5
        assert outcome.seconds >= 0.0
        assert outcome.log_path == state_dir() / "review" / "spawn.log"
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
            "brain_exit": None,
            "brain_seconds": None,
            "brain_prs": 0,
            "synced_merged": 0,
            "synced_closed": 0,
            "synced_kept": 0,
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

    async def test_merged_pr_sync_counts_flow_into_report_and_stamp_resolved_at(
        self, tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db")
        install_brain(monkeypatch)
        url = "https://github.com/yasyf/scratch/pull/1"
        async with await ReviewStore.open(settings.db_path) as store:
            await store.enable(GIT_REPO_KEY)
            candidate_id = await store.ensure_candidate(
                GIT_REPO_KEY, kind=CandidateKind.CREATE, rule=url, source_kind=TRANSCRIPT_MESSAGE
            )
            await store.transition(candidate_id, CandidateStatus.PR_OPEN, pr_url=url, pr_opened_at=datetime.now(UTC))
        monkeypatch.setattr(
            "captain_hook.review.sync.gh_pr_state", lambda _url: PrState("MERGED", "2026-07-08T15:06:25Z")
        )
        transcript = write_transcript(tmp_path / "s.jsonl", [assistant_text("nothing to correct here")])
        report = await review_session(transcript, cwd=str(git_repo), settings=settings)
        assert (report.synced_merged, report.synced_closed, report.synced_kept) == (1, 0, 0)
        async with await ReviewStore.open(settings.db_path) as store:
            candidate = await store.candidate(candidate_id)
        assert CandidateStatus(str(candidate["status"])) == CandidateStatus.ACCEPTED
        assert candidate["resolved_at"] is not None

    async def test_parent_dir_scan_sweeps_open_sibling_sessions(
        self, tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db")
        install_judge(monkeypatch)
        install_fake_embedder(monkeypatch)
        install_brain(monkeypatch)
        proj = tmp_path / "proj"
        ended = write_transcript(proj / "ended.jsonl", correction_entries(session="ended", cwd=str(git_repo)))
        write_transcript(
            proj / "sibling.jsonl",
            [
                assistant_text("attempt", sessionId="sibling", cwd=str(git_repo)),
                user_text(SECOND_CORRECTION, sessionId="sibling", cwd=str(git_repo)),
            ],
        )
        async with await ReviewStore.open(settings.db_path) as store:
            await store.enable(GIT_REPO_KEY)
        report = await review_session(ended, cwd=str(git_repo), settings=settings)
        assert (report.scanned, report.inserted) == (2, 2)
        async with await ReviewStore.open(settings.db_path) as store:
            samples = {str(row["sample_text"]) for row in await store.candidates(GIT_REPO_KEY)}
        assert {CORRECTION, SECOND_CORRECTION} <= samples

    async def test_brain_outcome_flows_into_report(
        self, tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db")

        def fake_brain(transcript: Path, *, repo_root: Path, settings: ReviewSettings) -> BrainOutcome:
            return BrainOutcome(exit_code=3, seconds=42.5, log_path=review_log_path())

        monkeypatch.setattr("captain_hook.review.pipeline.spawn_brain", fake_brain)
        async with await ReviewStore.open(settings.db_path) as store:
            await store.enable(GIT_REPO_KEY)
            candidate_id = await seed_eligible_fix(store, repo=GIT_REPO_KEY)
        transcript = write_transcript(tmp_path / "s.jsonl", [assistant_text("nothing to correct here")])
        report = await review_session(transcript, cwd=str(git_repo), settings=settings)
        assert report.eligible == (candidate_id,)
        assert (report.brain, report.brain_exit, report.brain_seconds, report.brain_prs) == (True, 3, 42.5, 0)

    async def test_brain_prs_counts_candidates_the_brain_opened(
        self, tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db")

        async def move() -> None:
            async with await ReviewStore.open(settings.db_path) as store:
                for row in await store.candidates(GIT_REPO_KEY, status=CandidateStatus.WATCHING):
                    await store.transition(
                        int(str(row["id"])),
                        CandidateStatus.PR_OPEN,
                        pr_url="https://github.com/yasyf/scratch/pull/9",
                        pr_opened_at=datetime.now(UTC),
                    )

        def open_pr(transcript: Path, *, repo_root: Path, settings: ReviewSettings) -> BrainOutcome:
            thread = threading.Thread(target=lambda: asyncio.run(move()))
            thread.start()
            thread.join()
            return BrainOutcome(exit_code=0, seconds=12.0, log_path=review_log_path())

        monkeypatch.setattr("captain_hook.review.pipeline.spawn_brain", open_pr)
        async with await ReviewStore.open(settings.db_path) as store:
            await store.enable(GIT_REPO_KEY)
            await seed_eligible_fix(store, repo=GIT_REPO_KEY)
        transcript = write_transcript(tmp_path / "s.jsonl", [assistant_text("nothing to correct here")])
        report = await review_session(transcript, cwd=str(git_repo), settings=settings)
        assert (report.brain, report.brain_exit, report.brain_prs) == (True, 0, 1)

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
                tmp_path / f"{session}.jsonl",
                correction_entries(session=session, timestamp=timestamp, cwd=str(git_repo)),
            )
            reports.append(await review_session(transcript, cwd=str(git_repo), settings=settings))
        assert [report.judged for report in reports] == [1, 1, 1]
        assert [report.brain for report in reports[:2]] == [False, False]
        assert reports[2].brain is expect_brain
        assert len(brains) == (1 if expect_brain else 0)
        if expect_brain:
            assert reports[2].eligible != ()
            assert brains[0][1] == Path(str(git_repo))


class TestReviewVerdictValidation:
    def test_durable_category_without_slug_raises(self) -> None:
        with pytest.raises(ValidationError):
            ReviewVerdict(category="durable_style_rule", summary="s", confidence=0.5, rationale="r")

    def test_non_durable_slug_none_yields_null_canonical_key(self) -> None:
        verdict = ReviewVerdict(category="one_off_correction", summary="s", confidence=0.5, rationale="r")
        assert (verdict.rule_slug, verdict.canonical_key, verdict.accepted) == (None, None, False)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("Use UV, not pip!", "use-uv-not-pip", id="strips-punctuation-and-lowercases"),
            pytest.param("  Always Frozen Dataclasses  ", "always-frozen-dataclasses", id="trims-and-collapses-runs"),
            pytest.param("prefer-uv-over-pip", "prefer-uv-over-pip", id="canonical-slug-is-idempotent"),
        ],
    )
    def test_slug_normalizes_through_canonical_slug(self, raw: str, expected: str) -> None:
        verdict = ReviewVerdict(category="tooling_rule", summary="s", confidence=0.5, rationale="r", rule_slug=raw)
        assert verdict.rule_slug == expected == canonical_slug(raw)

    @pytest.mark.parametrize(
        "bad",
        [
            pytest.param("word", id="single-word-below-two-group-floor"),
            pytest.param("deadbeef" * 8, id="sixty-four-hex-digest"),
            pytest.param("one two three four five six seven", id="seven-words-above-six-group-ceiling"),
        ],
    )
    def test_non_slug_pattern_rejected_not_coerced(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            ReviewVerdict(category="workflow_rule", summary="s", confidence=0.5, rationale="r", rule_slug=bad)


class TestSlugPromptRendering:
    def test_create_prompt_renders_seeded_suggestion_lines(self) -> None:
        suggestions = (
            Suggestion("prefer-uv-over-pip", 0.87, ("always use uv, never pip",)),
            Suggestion("never-bare-except", 0.42, ("catch the specific parser error",)),
        )
        row = {"source_kind": "transcript_message", "text": CORRECTION}
        prompt = build_create_prompt(row, "CONTEXT", suggestions)
        assert '- prefer-uv-over-pip (0.87) — "always use uv, never pip"' in prompt
        assert '- never-bare-except (0.42) — "catch the specific parser error"' in prompt
        assert "(none similar)" not in prompt

    def test_create_prompt_without_suggestions_renders_none_similar(self) -> None:
        row = {"source_kind": "transcript_message", "text": CORRECTION}
        prompt = build_create_prompt(row, "CONTEXT")
        assert "Suggested slugs (existing durable rules, most similar first):\n(none similar)" in prompt

    def test_fix_prompt_carries_no_slug_instruction(self) -> None:
        row = {
            "source_kind": "hook_complaint",
            "payload_json": json.dumps(
                {
                    "target_hook_name": "status_nudge:nudge_c424798f",
                    "event": "PreToolUse",
                    "action": "warn",
                    "fire_message": "Remember to use the project's task tracker.",
                }
            ),
            "text": "that reminder re-fired on text I already handled",
        }
        prompt = build_fix_prompt(row, "CONTEXT")
        assert "[hook: status_nudge:nudge_c424798f (PreToolUse/warn)]" in prompt
        assert "rule_slug" not in prompt
        assert "Suggested slugs" not in prompt
        assert "kebab-case" not in prompt


@requires_llm_backend
class TestParaphraseGrouping:
    async def test_same_emitted_slug_groups_to_one_candidate(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fake_embedder(monkeypatch)
        install_judge(monkeypatch, slug="prefer-uv-over-pip")
        for session, text in (("s1", CORRECTION), ("s2", SECOND_CORRECTION)):
            await seed_corrections(store, settings, tmp_path, [text], session=session)
            await judge_pass(store, settings=settings)
        [candidate] = await store.candidates(REPO)
        assert (candidate["rule"], candidate["source_kind"]) == ("prefer-uv-over-pip", "transcript_message")
        assert candidate["observations"] == 2
        status = await store.threshold_status(int(str(candidate["id"])), settings=settings)
        assert status.sessions == 2

    async def test_distinct_slugs_yield_two_candidates(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fake_embedder(monkeypatch)
        install_judge(monkeypatch)
        for session, text in (("s1", CORRECTION), ("s2", SECOND_CORRECTION)):
            await seed_corrections(store, settings, tmp_path, [text], session=session)
            await judge_pass(store, settings=settings)
        candidates = await store.candidates(REPO)
        assert len(candidates) == 2
        assert {str(c["rule"]) for c in candidates} == {
            default_slug_for(CORRECTION),
            default_slug_for(SECOND_CORRECTION),
        }
        assert {int(str(c["observations"])) for c in candidates} == {1}


@requires_llm_backend
class TestSlugRegroupE2E:
    async def test_cross_detector_collapse_hides_shadowed_row_from_the_judge(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fake_embedder(monkeypatch)
        calls = install_judge(monkeypatch, slug="prefer-specific-except")
        entries = [
            assistant_tool_use("t1", "Edit", {"file_path": "foo.py", "old_string": "a", "new_string": "b"}),
            {"type": "mode", "sessionId": "sess-1", "mode": "plan"},
            user_text(CORRECTION),
        ]
        path = write_transcript(tmp_path / "s.jsonl", entries)
        assert await scan_transcript(store, path, settings=settings, repo_key=REPO) == ScanReport(scanned=1, inserted=1)
        report = await judge_pass(store, settings=settings)
        assert len(calls) == 1
        assert report.judged == 1
        [candidate] = await store.candidates(REPO)
        assert (candidate["rule"], candidate["source_kind"]) == ("prefer-specific-except", "plan_review")
        assert candidate["observations"] == 1
        status = await store.threshold_status(int(str(candidate["id"])), settings=settings)
        assert status.sessions == 1

    async def test_answered_question_regroups_under_the_durable_slug(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fake_embedder(monkeypatch)
        transcript = write_transcript(
            tmp_path / "s.jsonl",
            [
                user_text("decide the parser"),
                *ask_user_question_round(QUESTION, notes=ANSWER, options=("lxml", "selectolax (Recommended)")),
            ],
        )
        assert await scan_transcript(store, transcript, settings=settings, repo_key=REPO) == ScanReport(
            scanned=1, inserted=1
        )
        [before] = await store.candidates(REPO)
        assert before["rule"] == dedup_key("question_answer", QUESTION, ANSWER)
        calls = install_judge(monkeypatch, slug="prefer-selectolax-parser")
        report = await judge_pass(store, settings=settings)
        assert (report.judged, report.merged, len(calls)) == (1, 1, 1)
        [after] = await store.candidates(REPO)
        assert (after["rule"], after["source_kind"]) == ("prefer-selectolax-parser", "question_answer")
        assert after["observations"] == 1
        status = await store.threshold_status(int(str(after["id"])), settings=settings)
        assert status.sessions == 1


@requires_llm_backend
class TestSuggestionPlumbing:
    async def test_prior_slug_evidence_ranks_the_matching_slug_first(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fake_embedder(monkeypatch)
        await seed_corrections(store, settings, tmp_path, [CORRECTION], session="s1")
        await store.record_verdict(
            dedup_key("transcript_message", "s1", CORRECTION),
            Verdict(canonical_key="prefer-uv-over-pip", summary="always use uv"),
            role=JUDGE_ROLE,
            prompt_version=store.versions.create,
            model="m1",
            fidelity="full",
        )
        await seed_corrections(store, settings, tmp_path, [SECOND_CORRECTION], session="s2")
        await store.record_verdict(
            dedup_key("transcript_message", "s2", SECOND_CORRECTION),
            Verdict(canonical_key="prefer-frozen-dataclasses", summary="always freeze config"),
            role=JUDGE_ROLE,
            prompt_version=store.versions.create,
            model="m1",
            fidelity="full",
        )
        assert await store.has_verdict_evidence()
        query = f"{CORRECTION}\nalways use uv"
        await seed_corrections(store, settings, tmp_path, [query], session="s3")
        calls = install_judge(monkeypatch, slug="prefer-uv-over-pip")
        await judge_pass(store, settings=settings)
        [prompt] = calls
        assert "- prefer-uv-over-pip (1.00) — " in prompt
        assert f'— "{CORRECTION}"' in prompt
        assert "- prefer-frozen-dataclasses (" in prompt
        assert prompt.index("- prefer-uv-over-pip (") < prompt.index("- prefer-frozen-dataclasses (")

    async def test_digest_era_key_is_filtered_while_a_valid_slug_reaches_the_prompt(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fake_embedder(monkeypatch)
        await seed_corrections(store, settings, tmp_path, [CORRECTION], session="s1")
        await store.record_verdict(
            dedup_key("transcript_message", "s1", CORRECTION),
            Verdict(canonical_key="prefer-uv-over-pip"),
            role=JUDGE_ROLE,
            prompt_version=store.versions.create,
            model="m1",
            fidelity="full",
        )
        await seed_corrections(store, settings, tmp_path, [SECOND_CORRECTION], session="s2")
        digest_key = "deadbeef" * 8
        await store.record_verdict(
            dedup_key("transcript_message", "s2", SECOND_CORRECTION),
            Verdict(canonical_key=digest_key),
            role=JUDGE_ROLE,
            prompt_version=store.versions.create,
            model="m1",
            fidelity="full",
        )
        raw = await suggest_canonical_keys(store, THIRD_CORRECTION, prompt_version=store.versions.create, k=5)
        assert {"prefer-uv-over-pip", digest_key} <= {suggestion.canonical_key for suggestion in raw}
        await seed_corrections(store, settings, tmp_path, [THIRD_CORRECTION], session="s3")
        calls = install_judge(monkeypatch, slug="prefer-uv-over-pip")
        await judge_pass(store, settings=settings)
        [prompt] = calls
        assert "- prefer-uv-over-pip (" in prompt
        assert digest_key not in prompt
