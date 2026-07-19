from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
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
    DISPATCH_EVENTS,
    REVIEW_RUN_DEDUP,
    SPAWNED_ENV,
    BrainOutcome,
    SpawnReport,
    _claim_stamp,
    brain_argv,
    brain_prompt,
    dispatch_review,
    enrolled,
    guard_and_spawn,
    guard_and_sweep,
    review_log_path,
    review_session,
    spawn_argv,
    spawn_brain,
    spawn_session,
    sweep_dir,
    sweep_key,
)
from captain_hook.review.repo import RepoKey
from captain_hook.review.scan import REVIEWER_MARKER, ScanReport, scan_transcript
from captain_hook.review.settings import ReviewSettings, resolve_review_db_path
from captain_hook.review.store import CandidateKind, CandidateStatus, PromptVersions, ReviewStore
from captain_hook.review.sync import PrState
from captain_hook.types import Event
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
    install_resolved_model,
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


async def seed_eligible_fix(store: ReviewStore, *, repo: RepoKey, session: str = "fs1") -> int:
    candidate_id = store.ensure_candidate(
        repo,
        kind=CandidateKind.FIX,
        rule=dedup_key("hook_complaint", FIX_TARGET_HOOK, FIX_TARGET_FILE),
        source_kind=SourceKind(HOOK_COMPLAINT),
        target_source_file=FIX_TARGET_FILE,
        target_hook_name=FIX_TARGET_HOOK,
        misfire_class="refire",
    )
    key = dedup_key("hook_complaint", session, FIX_TARGET_HOOK)
    payload = json.dumps({"signal": to_payload(CandidateSignal(Confidence(VERY_HIGH), ("marker",)))})
    store.store.execute(
        "INSERT INTO feedback_events (dedup_key, source_kind, session_id, occurred_at, text, payload_json, "
        "context_json, ingested_at) VALUES (?, ?, ?, ?, ?, ?, '{}', '2026-06-01T00:00:00+00:00')",
        (key, HOOK_COMPLAINT, session, "2026-06-01T10:00:00+00:00", "that reminder misfired again", payload),
    )
    store.record_observation(
        candidate_id,
        dedup_key=DedupKey(key),
        session_id=SessionId(session),
        occurred_at=datetime.fromisoformat("2026-06-01T10:00:00+00:00"),
    )
    store.record_verdict(
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


def verdict_fidelities(store: ReviewStore) -> list[str]:
    return [str(row["fidelity"]) for row in store.store.sql("SELECT fidelity FROM verdicts ORDER BY id")]


def count_rows(store: ReviewStore, table: str) -> int:
    return [int(row["n"]) for row in store.store.sql(f"SELECT COUNT(*) AS n FROM {table}")][0]


def sweep_stamps(cwd: str) -> tuple[Path, Path]:
    base = state_dir() / "review" / "sweeps"
    return base / f"{sweep_key(cwd)}.trigger", base / f"{sweep_key(cwd)}.sweep"


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
        # The spawned-env skip breadcrumbs but never detaches a child (no SpawnReport lands).
        log = (state_dir() / "review" / "spawn.log").read_text()
        assert "review-run skip: CAPT_HOOK_SPAWNED set" in log
        assert "SpawnReport" not in log

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
        # The sdk-entrypoint skip breadcrumbs but never detaches a child (no SpawnReport lands).
        log = (state_dir() / "review" / "spawn.log").read_text()
        assert "review-run skip: sdk entrypoint" in log
        assert "SpawnReport" not in log


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


class TestGuardBreadcrumbs:
    @pytest.mark.parametrize(
        ("env", "raw", "reason"),
        [
            pytest.param({SPAWNED_ENV: "1"}, b"{}", "review-run skip: CAPT_HOOK_SPAWNED set", id="spawned-env"),
            pytest.param({"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, b"{}", "review-run skip: sdk entrypoint", id="sdk"),
            pytest.param({}, b"not json {{{", "review-run skip: unparseable stdin", id="unparseable"),
            pytest.param(
                {},
                json.dumps({"transcript_path": 42}).encode(),
                "review-run skip: non-string transcript_path",
                id="non-string-transcript",
            ),
            pytest.param(
                {},
                json.dumps({"transcript_path": "/nonexistent/t.jsonl"}).encode(),
                "review-run skip: missing transcript file",
                id="missing-transcript",
            ),
        ],
    )
    def test_guard_breadcrumbs_each_skip_reason(
        self,
        popen_calls: list[tuple[list[str], dict[str, Any]]],
        monkeypatch: pytest.MonkeyPatch,
        env: dict[str, str],
        raw: bytes,
        reason: str,
    ) -> None:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        guard_and_spawn(raw)
        assert popen_calls == []
        assert reason in review_log_path().read_text()

    def test_guard_breadcrumbs_spawn(self, popen_calls: list[tuple[list[str], dict[str, Any]]], tmp_path: Path) -> None:
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        guard_and_spawn(json.dumps({"transcript_path": str(transcript), "cwd": str(tmp_path)}).encode())
        assert len(popen_calls) == 1
        assert f"spawned {transcript}" in review_log_path().read_text()


class TestSweepGuard:
    def payload(self, tmp_path: Path) -> tuple[Path, bytes]:
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        return transcript, json.dumps({"transcript_path": str(transcript), "cwd": str(tmp_path)}).encode()

    def test_sweep_guard_throttles(self, popen_calls: list[tuple[list[str], dict[str, Any]]], tmp_path: Path) -> None:
        transcript, payload = self.payload(tmp_path)
        guard_and_sweep(payload)
        assert len(popen_calls) == 1
        [(argv, _)] = popen_calls
        assert argv == spawn_argv(str(transcript), str(tmp_path), sweep=True)
        trigger, sweep = sweep_stamps(str(tmp_path))
        assert trigger.exists() and sweep.exists()
        sweep_mtime = sweep.stat().st_mtime
        stale = (datetime.now(UTC) - timedelta(days=1)).timestamp()
        os.utime(trigger, (stale, stale))
        guard_and_sweep(payload)
        assert len(popen_calls) == 1  # throttled: no second spawn
        assert trigger.stat().st_mtime > stale  # trigger re-touched on every invocation
        assert sweep.stat().st_mtime == sweep_mtime  # sweep stamp claimed once

    def test_sweep_guard_spawns_after_interval(
        self, popen_calls: list[tuple[list[str], dict[str, Any]]], tmp_path: Path
    ) -> None:
        _, payload = self.payload(tmp_path)
        guard_and_sweep(payload)
        assert len(popen_calls) == 1
        _, sweep = sweep_stamps(str(tmp_path))
        aged = (datetime.now(UTC) - timedelta(minutes=31)).timestamp()
        os.utime(sweep, (aged, aged))
        guard_and_sweep(payload)
        assert len(popen_calls) == 2

    @pytest.mark.parametrize(
        "env",
        [
            pytest.param({SPAWNED_ENV: "1"}, id="spawned-env"),
            pytest.param({"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, id="headless-sdk"),
        ],
    )
    def test_sweep_guard_skips_sdk_and_spawned_env(
        self,
        popen_calls: list[tuple[list[str], dict[str, Any]]],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        env: dict[str, str],
    ) -> None:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        _, payload = self.payload(tmp_path)
        guard_and_sweep(payload)
        assert popen_calls == []

    def test_sweep_guard_missing_transcript(self, popen_calls: list[tuple[list[str], dict[str, Any]]]) -> None:
        guard_and_sweep(json.dumps({"transcript_path": "/nonexistent/t.jsonl", "cwd": "/x"}).encode())
        assert popen_calls == []
        assert "sweep skip: missing transcript file" in review_log_path().read_text()


class TestReviewRunThrottle:
    def payload(self, tmp_path: Path, event: str = "SessionEnd") -> bytes:
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        return json.dumps({"transcript_path": str(transcript), "cwd": str(tmp_path), "hook_event_name": event}).encode()

    def run_stamp(self, cwd: str, event: str) -> Path:
        return sweep_dir() / f"{hashlib.sha256(f'{cwd}\x00{event}'.encode()).hexdigest()[:12]}.run"

    def test_second_run_within_window_throttled(
        self, popen_calls: list[tuple[list[str], dict[str, Any]]], tmp_path: Path
    ) -> None:
        payload = self.payload(tmp_path)
        guard_and_spawn(payload)
        guard_and_spawn(payload)
        assert len(popen_calls) == 1  # skew double-fire collapses to one reviewer child
        assert "review-run skip: throttled" in review_log_path().read_text()

    def test_run_spawns_again_after_window(
        self, popen_calls: list[tuple[list[str], dict[str, Any]]], tmp_path: Path
    ) -> None:
        payload = self.payload(tmp_path)
        guard_and_spawn(payload)
        aged = (datetime.now(UTC) - REVIEW_RUN_DEDUP - timedelta(seconds=1)).timestamp()
        os.utime(self.run_stamp(str(tmp_path), "SessionEnd"), (aged, aged))
        guard_and_spawn(payload)
        assert len(popen_calls) == 2

    def test_distinct_events_do_not_share_a_stamp(
        self, popen_calls: list[tuple[list[str], dict[str, Any]]], tmp_path: Path
    ) -> None:
        guard_and_spawn(self.payload(tmp_path, "SessionStart"))
        guard_and_spawn(self.payload(tmp_path, "SessionEnd"))
        assert len(popen_calls) == 2  # SessionStart and SessionEnd key independently


class TestEnrollmentGate:
    def payload(self, tmp_path: Path, *, sweep: bool = False) -> bytes:
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        event = "Stop" if sweep else "SessionEnd"
        return json.dumps({"transcript_path": str(transcript), "cwd": str(tmp_path), "hook_event_name": event}).encode()

    def test_spawn_gate_blocks_unwatched(
        self, popen_calls: list[tuple[list[str], dict[str, Any]]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("captain_hook.review.pipeline.enrolled", lambda cwd: False)
        guard_and_spawn(self.payload(tmp_path), gate_enrollment=True)
        assert popen_calls == []
        assert "review-run skip: not watching" in review_log_path().read_text()

    def test_spawn_gate_allows_watched(
        self, popen_calls: list[tuple[list[str], dict[str, Any]]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("captain_hook.review.pipeline.enrolled", lambda cwd: True)
        guard_and_spawn(self.payload(tmp_path), gate_enrollment=True)
        assert len(popen_calls) == 1

    def test_spawn_cli_path_ignores_enrollment(
        self, popen_calls: list[tuple[list[str], dict[str, Any]]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # gate_enrollment defaults off, so the raw `review run` CLI entry spawns regardless.
        monkeypatch.setattr("captain_hook.review.pipeline.enrolled", lambda cwd: False)
        guard_and_spawn(self.payload(tmp_path))
        assert len(popen_calls) == 1

    def test_sweep_gate_blocks_unwatched_after_throttle(
        self, popen_calls: list[tuple[list[str], dict[str, Any]]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("captain_hook.review.pipeline.enrolled", lambda cwd: False)
        guard_and_sweep(self.payload(tmp_path, sweep=True), gate_enrollment=True)
        assert popen_calls == []
        assert "sweep skip: not watching" in review_log_path().read_text()

    def test_sweep_gate_allows_watched(
        self, popen_calls: list[tuple[list[str], dict[str, Any]]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("captain_hook.review.pipeline.enrolled", lambda cwd: True)
        guard_and_sweep(self.payload(tmp_path, sweep=True), gate_enrollment=True)
        assert len(popen_calls) == 1


class TestEnrolled:
    def test_non_git_cwd_is_not_watched(self, tmp_path: Path) -> None:
        assert enrolled(str(tmp_path)) is False

    def test_fresh_git_repo_auto_watches(self, git_repo: Path) -> None:
        assert enrolled(str(git_repo)) is True

    def test_disabled_repo_is_not_watched(self, git_repo: Path) -> None:
        from captain_hook.review.repo import resolve_repo_key

        repo = resolve_repo_key(str(git_repo))
        assert repo is not None

        def disable() -> None:
            with ReviewStore.open(ReviewSettings().db_path) as store:
                store.enable(repo)
                store.disable(repo)

        disable()
        assert enrolled(str(git_repo)) is False

    def test_store_failure_fails_open(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # uncertainty spawns the child (which re-applies the authoritative gate) rather than dropping the review
        def boom(*args: object, **kwargs: object) -> object:
            raise RuntimeError("store down")

        monkeypatch.setattr(ReviewStore, "open", boom)
        assert enrolled(str(git_repo)) is True
        assert "review gate uncertain" in review_log_path().read_text()


class TestClaimStampAtomicity:
    WINDOW = timedelta(seconds=60)

    def test_concurrent_claims_resolve_to_one_winner(self, tmp_path: Path) -> None:
        stamp = tmp_path / "x.run"
        barrier = threading.Barrier(8)

        def attempt(_: int) -> bool:
            barrier.wait()
            return _claim_stamp(stamp, self.WINDOW)

        with ThreadPoolExecutor(max_workers=8) as pool:
            wins = list(pool.map(attempt, range(8)))
        assert wins.count(True) == 1

    def test_fresh_stamp_loses(self, tmp_path: Path) -> None:
        stamp = tmp_path / "x.run"
        assert _claim_stamp(stamp, self.WINDOW) is True
        assert _claim_stamp(stamp, self.WINDOW) is False

    def test_stale_stamp_reclaims(self, tmp_path: Path) -> None:
        stamp = tmp_path / "x.run"
        assert _claim_stamp(stamp, self.WINDOW) is True
        aged = (datetime.now(UTC) - self.WINDOW - timedelta(seconds=1)).timestamp()
        os.utime(stamp, (aged, aged))
        assert _claim_stamp(stamp, self.WINDOW) is True


class TestGateClaimOrdering:
    def payload(self, tmp_path: Path, *, sweep: bool = False) -> bytes:
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        event = "Stop" if sweep else "SessionEnd"
        return json.dumps({"transcript_path": str(transcript), "cwd": str(tmp_path), "hook_event_name": event}).encode()

    def test_gated_spawn_skip_does_not_burn_the_stamp(
        self, popen_calls: list[tuple[list[str], dict[str, Any]]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # native dispatch skips a non-watched repo; the raw CLI fallback within the window must still spawn
        monkeypatch.setattr("captain_hook.review.pipeline.enrolled", lambda cwd: False)
        payload = self.payload(tmp_path)
        guard_and_spawn(payload, gate_enrollment=True)
        assert popen_calls == []
        guard_and_spawn(payload)
        assert len(popen_calls) == 1

    def test_gated_sweep_skip_does_not_burn_the_stamp(
        self, popen_calls: list[tuple[list[str], dict[str, Any]]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("captain_hook.review.pipeline.enrolled", lambda cwd: False)
        payload = self.payload(tmp_path, sweep=True)
        guard_and_sweep(payload, gate_enrollment=True)
        assert popen_calls == []
        guard_and_sweep(payload)
        assert len(popen_calls) == 1


class TestDispatchReview:
    def test_routes_stop_to_sweep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            "captain_hook.review.pipeline.guard_and_sweep",
            lambda raw, *, gate_enrollment=False: calls.append(("sweep", gate_enrollment)),
        )
        monkeypatch.setattr(
            "captain_hook.review.pipeline.guard_and_spawn",
            lambda raw, *, gate_enrollment=False: calls.append(("spawn", gate_enrollment)),
        )
        dispatch_review("Stop", {"transcript_path": "/t", "cwd": "/c"})
        assert calls == [("sweep", True)]

    @pytest.mark.parametrize("event", ["SessionStart", "SessionEnd"])
    def test_routes_session_events_to_reviewer(self, monkeypatch: pytest.MonkeyPatch, event: str) -> None:
        calls: list[tuple[str, bool]] = []
        monkeypatch.setattr(
            "captain_hook.review.pipeline.guard_and_spawn",
            lambda raw, *, gate_enrollment=False: calls.append(("spawn", gate_enrollment)),
        )
        dispatch_review(event, {"transcript_path": "/t", "cwd": "/c"})
        assert calls == [("spawn", True)]


class TestNativeReviewWiring:
    def install(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
        from captain_hook import cli

        calls: list[tuple[str, dict[str, Any]]] = []
        monkeypatch.setattr(cli, "dispatch_review", lambda name, raw: calls.append((name, raw)))
        monkeypatch.setattr("captain_hook.heartbeat.record_heartbeat", lambda event, raw: None)
        return calls

    def dispatch(self, event: Event, *, async_: bool, tmp_path: Path) -> None:
        from captain_hook.cli import dispatch_event

        raw = {"transcript_path": "/t", "cwd": str(tmp_path), "hook_event_name": event.name}
        dispatch_event(tmp_path, event, raw, session_dir=None, async_=async_)

    def test_dispatch_events_are_typed(self) -> None:
        assert DISPATCH_EVENTS == frozenset({Event.SessionStart, Event.SessionEnd, Event.Stop})

    def test_async_review_event_fires(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        calls = self.install(monkeypatch)
        self.dispatch(Event.SessionEnd, async_=True, tmp_path=tmp_path)
        expected = {"transcript_path": "/t", "cwd": str(tmp_path), "hook_event_name": "SessionEnd"}
        assert calls == [("SessionEnd", expected)]

    def test_sync_review_event_does_not_fire(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        calls = self.install(monkeypatch)
        self.dispatch(Event.SessionEnd, async_=False, tmp_path=tmp_path)
        assert calls == []

    def test_async_non_review_event_does_not_fire(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        calls = self.install(monkeypatch)
        self.dispatch(Event.PreToolUse, async_=True, tmp_path=tmp_path)
        assert calls == []


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
        judged = store.judged(role=JUDGE_ROLE, prompt_version=store.versions.create)
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
        store.record_file_scan(
            "synthetic", 1.0, [synthetic("structural junk", noise("bare_marker")), synthetic(CORRECTION, firm())]
        )
        assert await judge_pass(store, settings=settings) == JudgeReport(
            judged=1, failed=0, pending=0, merged=0, retired=0, reopened=0
        )
        assert len(calls) == 1
        assert "structural junk" not in calls[0]
        unjudged = store.unjudged(role=JUDGE_ROLE, prompt_version=store.versions.create)
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

        def boom(*_: object, **__: object) -> list[object]:
            raise RuntimeError("sqlite-vec extension failed to load")

        def has_evidence(self: ReviewStore) -> bool:
            return True

        monkeypatch.setattr("cc_transcript.judge.similar.suggest_canonical_keys", boom)
        monkeypatch.setattr("cc_transcript.judge.similar.default_embedder", lambda: lambda text: text)
        monkeypatch.setattr(ReviewStore, "has_verdict_evidence", has_evidence)
        assert await judge_pass(store, settings=settings) == JudgeReport(
            judged=0, failed=1, pending=1, merged=0, retired=0, reopened=0
        )
        assert [row["text"] for row in store.unjudged(role=JUDGE_ROLE, prompt_version=store.versions.create)] == [
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
        assert not store.has_verdict_evidence()
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
        with ReviewStore.open(tmp_path / "lanes.db", versions=PromptVersions(create=4, fix=3)) as store:
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
            store.store.execute(
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
            lanes = {
                str(row["source_kind"]): int(row["prompt_version"])
                for row in store.store.sql(
                    "SELECT e.source_kind AS source_kind, v.prompt_version AS prompt_version "
                    "FROM verdicts v JOIN feedback_events e ON e.dedup_key = v.dedup_key WHERE v.role = 'judge'"
                )
            }
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
        entries = [
            user_text("add the parser"),
            assistant_tool_use("t1", "Bash", {"command": "pip install foo"}),
            user_text(CORRECTION),
        ]
        raw = "".join(json.dumps(entry) + "\n" for entry in entries).encode()
        events = parse(entries)
        window = capture_window(raw, EventRef(SessionId("sess-1"), events[-1].meta.uuid))
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
        [row] = store.candidates(REPO)
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
        [coalesced] = store.candidates(REPO)
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
        assert {row["rule"] for row in store.candidates(REPO)} == {
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
        assert verdict_fidelities(store) == ["full"]
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
        assert verdict_fidelities(store) == ["summary"]
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
        assert verdict_fidelities(store) == ["summary"]
        assert await judge_pass(store, settings=settings) == JudgeReport(
            judged=0, failed=0, pending=0, merged=0, retired=0, reopened=0
        )
        transcript.write_text(content)
        assert await judge_pass(store, settings=settings, refresh_summary=True) == JudgeReport(
            judged=1, failed=0, pending=0, merged=0, retired=0, reopened=0
        )
        assert verdict_fidelities(store) == ["full"]
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

    def test_spawn_brain_deadline_kills_and_records_sigkill_exit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        deadlines: list[float] = []

        def fake_run(argv: list[str], **kw: Any) -> SimpleNamespace:
            deadlines.append(kw["timeout"])
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kw["timeout"])

        monkeypatch.setattr("captain_hook.review.pipeline.subprocess.run", fake_run)
        outcome = spawn_brain(
            tmp_path / "t.jsonl", repo_root=tmp_path, settings=ReviewSettings(brain_deadline_seconds=7)
        )
        assert outcome.exit_code == -9
        assert outcome.seconds >= 0.0
        assert outcome.log_path == state_dir() / "review" / "spawn.log"
        assert deadlines == [7]


class TestSpawnSession:
    async def test_success_records_ok_row_with_report_json(
        self, tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db")
        install_judge(monkeypatch)
        install_fake_embedder(monkeypatch)
        install_brain(monkeypatch)
        install_resolved_model(monkeypatch)
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        report = await spawn_session(transcript, cwd=str(git_repo), settings=settings)
        assert report == SpawnReport(repo=GIT_REPO_KEY, watching=True, scanned=1)
        with ReviewStore.open(settings.db_path) as store:
            health = store.spawn_health()
        assert health.consecutive_failures == 0
        assert health.failing_since is None
        assert health.last is not None
        assert health.last["ok"] == 1
        assert health.last["error"] is None
        assert health.last["transcript"] == str(transcript)
        assert json.loads(str(health.last["report_json"])) == {
            "repo": str(GIT_REPO_KEY),
            "watching": True,
            "scanned": 1,
            "inserted": 0,
            "triaged": 0,
            "triage_junk": 0,
            "triage_rejected": 0,
            "judged": 0,
            "failed": 0,
            "eligible": [],
            "brain": False,
            "brain_exit": None,
            "brain_seconds": None,
            "brain_prs": 0,
            "brain_skips": 0,
            "synced_merged": 0,
            "synced_closed": 0,
            "synced_kept": 0,
            "sweep": False,
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

        async def boom(transcript: Path, *, cwd: str, settings: ReviewSettings, sweep: bool = False) -> SpawnReport:
            raise exc

        monkeypatch.setattr("captain_hook.review.pipeline.review_session", boom)
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        with pytest.raises(type(exc)):
            await spawn_session(transcript, cwd=str(tmp_path), settings=settings)
        with ReviewStore.open(settings.db_path) as store:
            health = store.spawn_health()
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
        with ReviewStore.open(resolve_review_db_path()) as store:
            health = store.spawn_health()
        assert health.consecutive_failures == 1
        assert health.last is not None
        assert health.last["ok"] == 0
        assert str(health.last["error"]).startswith("ValidationError:")
        assert "judge_concurrency" in str(health.last["error"])

    async def test_spawn_session_deadline_records_failed_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db", spawn_deadline_seconds=0)

        async def hang(transcript: Path, *, cwd: str, settings: ReviewSettings, sweep: bool = False) -> SpawnReport:
            await asyncio.sleep(5)
            return SpawnReport(repo=None)

        monkeypatch.setattr("captain_hook.review.pipeline.review_session", hang)
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        with pytest.raises(TimeoutError):
            await spawn_session(transcript, cwd=str(tmp_path), settings=settings)
        with ReviewStore.open(settings.db_path) as store:
            health = store.spawn_health()
        assert health.consecutive_failures == 1
        assert health.last is not None
        assert health.last["ok"] == 0
        assert "TimeoutError" in str(health.last["error"])
        assert health.last["report_json"] is None


class TestReviewSession:
    async def test_non_git_cwd_is_a_clean_skip(self, tmp_path: Path) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db")
        plain = tmp_path / "plain"
        plain.mkdir()
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        assert await review_session(transcript, cwd=str(plain), settings=settings) == SpawnReport(repo=None)
        assert not settings.db_path.exists()

    async def test_sweep_session_skips_brain_and_sync(
        self, tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db")
        install_resolved_model(monkeypatch)

        def no_brain(transcript: Path, *, repo_root: Path, settings: ReviewSettings) -> BrainOutcome:
            raise AssertionError("the brain must not run during a sweep")

        def no_sync(*args: object, **kwargs: object) -> object:
            raise AssertionError("PR sync must not run during a sweep")

        monkeypatch.setattr("captain_hook.review.pipeline.spawn_brain", no_brain)
        monkeypatch.setattr("captain_hook.review.sync.sync_open_prs", no_sync)
        with ReviewStore.open(settings.db_path) as store:
            store.enable(GIT_REPO_KEY)
            candidate_id = await seed_eligible_fix(store, repo=GIT_REPO_KEY)
        transcript = write_transcript(tmp_path / "s.jsonl", [assistant_text("nothing to correct here")])
        report = await review_session(transcript, cwd=str(git_repo), settings=settings, sweep=True)
        assert report.sweep is True
        assert (report.brain, report.eligible) == (False, ())
        assert (report.synced_merged, report.synced_closed, report.synced_kept) == (0, 0, 0)
        assert (report.watching, report.scanned) == (True, 1)
        with ReviewStore.open(settings.db_path) as store:
            candidate = store.candidate(candidate_id)
        assert CandidateStatus(str(candidate["status"])) == CandidateStatus.WATCHING

    async def test_opted_out_repo_skips_scan_judge_and_brain(
        self, tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db")
        calls = install_judge(monkeypatch)
        brains = install_brain(monkeypatch)
        with ReviewStore.open(settings.db_path) as store:
            store.disable(GIT_REPO_KEY)
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries())
        report = await review_session(transcript, cwd=str(git_repo), settings=settings)
        assert report == SpawnReport(repo=GIT_REPO_KEY)
        assert calls == []
        assert brains == []
        with ReviewStore.open(settings.db_path) as store:
            assert store.file_mtimes() == {}

    async def test_unknown_repo_auto_enrolls_and_runs(
        self, tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db")
        install_judge(monkeypatch)
        install_fake_embedder(monkeypatch)
        install_brain(monkeypatch)
        install_resolved_model(monkeypatch)
        transcript = write_transcript(tmp_path / "s.jsonl", correction_entries(cwd=str(git_repo)))
        report = await review_session(transcript, cwd=str(git_repo), settings=settings)
        assert report.watching is True
        assert report.scanned == 1
        with ReviewStore.open(settings.db_path) as store:
            assert store.watching(GIT_REPO_KEY) is True

    async def test_merged_pr_sync_counts_flow_into_report_and_stamp_resolved_at(
        self, tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db")
        install_brain(monkeypatch)
        install_resolved_model(monkeypatch)
        url = "https://github.com/yasyf/scratch/pull/1"
        with ReviewStore.open(settings.db_path) as store:
            store.enable(GIT_REPO_KEY)
            candidate_id = store.ensure_candidate(
                GIT_REPO_KEY, kind=CandidateKind.CREATE, rule=url, source_kind=TRANSCRIPT_MESSAGE
            )
            store.transition(candidate_id, CandidateStatus.PR_OPEN, pr_url=url, pr_opened_at=datetime.now(UTC))
        monkeypatch.setattr(
            "captain_hook.review.sync.gh_pr_state", lambda _url: PrState("MERGED", "2026-07-08T15:06:25Z")
        )
        transcript = write_transcript(tmp_path / "s.jsonl", [assistant_text("nothing to correct here")])
        report = await review_session(transcript, cwd=str(git_repo), settings=settings)
        assert (report.synced_merged, report.synced_closed, report.synced_kept) == (1, 0, 0)
        with ReviewStore.open(settings.db_path) as store:
            candidate = store.candidate(candidate_id)
        assert CandidateStatus(str(candidate["status"])) == CandidateStatus.ACCEPTED
        assert candidate["resolved_at"] is not None

    async def test_parent_dir_scan_sweeps_open_sibling_sessions(
        self, tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db")
        install_judge(monkeypatch)
        install_fake_embedder(monkeypatch)
        install_brain(monkeypatch)
        install_resolved_model(monkeypatch)
        proj = tmp_path / "proj"
        ended = write_transcript(proj / "ended.jsonl", correction_entries(session="ended", cwd=str(git_repo)))
        write_transcript(
            proj / "sibling.jsonl",
            [
                assistant_text("attempt", sessionId="sibling", cwd=str(git_repo)),
                user_text(SECOND_CORRECTION, sessionId="sibling", cwd=str(git_repo)),
            ],
        )
        with ReviewStore.open(settings.db_path) as store:
            store.enable(GIT_REPO_KEY)
        report = await review_session(ended, cwd=str(git_repo), settings=settings)
        assert (report.scanned, report.inserted) == (2, 2)
        with ReviewStore.open(settings.db_path) as store:
            samples = {str(row["sample_text"]) for row in store.candidates(GIT_REPO_KEY)}
        assert {CORRECTION, SECOND_CORRECTION} <= samples

    async def test_brain_outcome_flows_into_report(
        self, tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db")
        install_resolved_model(monkeypatch)

        def fake_brain(transcript: Path, *, repo_root: Path, settings: ReviewSettings) -> BrainOutcome:
            return BrainOutcome(exit_code=3, seconds=42.5, log_path=review_log_path())

        monkeypatch.setattr("captain_hook.review.pipeline.spawn_brain", fake_brain)
        with ReviewStore.open(settings.db_path) as store:
            store.enable(GIT_REPO_KEY)
            candidate_id = await seed_eligible_fix(store, repo=GIT_REPO_KEY)
        transcript = write_transcript(tmp_path / "s.jsonl", [assistant_text("nothing to correct here")])
        report = await review_session(transcript, cwd=str(git_repo), settings=settings)
        assert report.eligible == (candidate_id,)
        assert (report.brain, report.brain_exit, report.brain_seconds, report.brain_prs) == (True, 3, 42.5, 0)

    async def test_brain_prs_counts_candidates_the_brain_opened(
        self, tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db")
        install_resolved_model(monkeypatch)

        def move() -> None:
            with ReviewStore.open(settings.db_path) as store:
                for row in store.candidates(GIT_REPO_KEY, status=CandidateStatus.WATCHING):
                    store.transition(
                        int(str(row["id"])),
                        CandidateStatus.PR_OPEN,
                        pr_url="https://github.com/yasyf/scratch/pull/9",
                        pr_opened_at=datetime.now(UTC),
                    )

        def open_pr(transcript: Path, *, repo_root: Path, settings: ReviewSettings) -> BrainOutcome:
            thread = threading.Thread(target=move)
            thread.start()
            thread.join()
            return BrainOutcome(exit_code=0, seconds=12.0, log_path=review_log_path())

        monkeypatch.setattr("captain_hook.review.pipeline.spawn_brain", open_pr)
        with ReviewStore.open(settings.db_path) as store:
            store.enable(GIT_REPO_KEY)
            await seed_eligible_fix(store, repo=GIT_REPO_KEY)
        transcript = write_transcript(tmp_path / "s.jsonl", [assistant_text("nothing to correct here")])
        report = await review_session(transcript, cwd=str(git_repo), settings=settings)
        assert (report.brain, report.brain_exit, report.brain_prs) == (True, 0, 1)

    async def test_brain_skips_counts_eligible_candidates_left_watching(
        self, tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = ReviewSettings(db_path=tmp_path / "review.db")
        install_brain(monkeypatch)
        install_resolved_model(monkeypatch)
        with ReviewStore.open(settings.db_path) as store:
            store.enable(GIT_REPO_KEY)
            candidate_id = await seed_eligible_fix(store, repo=GIT_REPO_KEY)
        transcript = write_transcript(tmp_path / "s.jsonl", [assistant_text("nothing to correct here")])
        report = await review_session(transcript, cwd=str(git_repo), settings=settings)
        assert report.eligible == (candidate_id,)
        assert (report.brain, report.brain_exit, report.brain_prs, report.brain_skips) == (True, 0, 0, 1)

    async def test_concurrent_passes_spawn_at_most_one_brain(
        self, tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A SessionStart and a SessionEnd pass on one repo can run at once; the global brain lock
        # lets exactly one spawn the brain while the other sees the lock held and skips it.
        settings = ReviewSettings(db_path=tmp_path / "review.db")
        install_resolved_model(monkeypatch)
        calls: list[Path] = []
        brain_entered = threading.Event()
        release = threading.Event()

        def blocking_brain(transcript: Path, *, repo_root: Path, settings: ReviewSettings) -> BrainOutcome:
            calls.append(repo_root)
            brain_entered.set()
            assert release.wait(timeout=10)
            return BrainOutcome(exit_code=0, seconds=1.0, log_path=review_log_path())

        monkeypatch.setattr("captain_hook.review.pipeline.spawn_brain", blocking_brain)
        with ReviewStore.open(settings.db_path) as store:
            store.enable(GIT_REPO_KEY)
            await seed_eligible_fix(store, repo=GIT_REPO_KEY)
        first = write_transcript(tmp_path / "start.jsonl", [assistant_text("nothing to correct here")])
        second = write_transcript(tmp_path / "end.jsonl", [assistant_text("nothing to correct here")])

        holder: dict[str, SpawnReport] = {}

        def run_holder() -> None:
            holder["report"] = asyncio.run(review_session(first, cwd=str(git_repo), settings=settings))

        thread = threading.Thread(target=run_holder)
        thread.start()
        assert brain_entered.wait(timeout=10)
        loser = await review_session(second, cwd=str(git_repo), settings=settings)
        release.set()
        thread.join(timeout=10)

        assert len(calls) == 1
        assert holder["report"].brain is True
        assert (loser.brain, loser.eligible, loser.brain_prs) == (False, (), 0)

    async def test_cross_repo_passes_share_the_global_brain_lock(
        self, tmp_path: Path, git_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two passes in DIFFERENT repos sharing one review db would take different per-repo locks;
        # the machine-wide lock serializes them so only one brain spawns while the other skips.
        second_repo = tmp_path / "second"
        second_repo.mkdir()
        subprocess.run(["git", "init", "-q", str(second_repo)], check=True)
        subprocess.run(
            ["git", "-C", str(second_repo), "remote", "add", "origin", "git@github.com:yasyf/other.git"], check=True
        )
        second_key = RepoKey("github.com/yasyf/other")

        settings = ReviewSettings(db_path=tmp_path / "review.db")
        install_resolved_model(monkeypatch)
        calls: list[Path] = []
        brain_entered = threading.Event()
        release = threading.Event()

        def blocking_brain(transcript: Path, *, repo_root: Path, settings: ReviewSettings) -> BrainOutcome:
            calls.append(repo_root)
            brain_entered.set()
            assert release.wait(timeout=10)
            return BrainOutcome(exit_code=0, seconds=1.0, log_path=review_log_path())

        monkeypatch.setattr("captain_hook.review.pipeline.spawn_brain", blocking_brain)
        with ReviewStore.open(settings.db_path) as store:
            store.enable(GIT_REPO_KEY)
            store.enable(second_key)
            await seed_eligible_fix(store, repo=GIT_REPO_KEY, session="fs1")
            await seed_eligible_fix(store, repo=second_key, session="fs2")
        first = write_transcript(tmp_path / "start.jsonl", [assistant_text("nothing to correct here")])
        second = write_transcript(tmp_path / "end.jsonl", [assistant_text("nothing to correct here")])

        holder: dict[str, SpawnReport] = {}

        def run_holder() -> None:
            holder["report"] = asyncio.run(review_session(first, cwd=str(git_repo), settings=settings))

        thread = threading.Thread(target=run_holder)
        thread.start()
        assert brain_entered.wait(timeout=10)
        loser = await review_session(second, cwd=str(second_repo), settings=settings)
        release.set()
        thread.join(timeout=10)

        assert len(calls) == 1
        assert holder["report"].brain is True
        assert (loser.brain, loser.eligible, loser.brain_prs) == (False, (), 0)

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
        with ReviewStore.open(settings.db_path) as store:
            store.enable(GIT_REPO_KEY)
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
        [candidate] = store.candidates(REPO)
        assert (candidate["rule"], candidate["source_kind"]) == ("prefer-uv-over-pip", "transcript_message")
        assert candidate["observations"] == 2
        status = store.threshold_status(int(str(candidate["id"])), settings=settings)
        assert status.sessions == 2

    async def test_distinct_slugs_yield_two_candidates(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fake_embedder(monkeypatch)
        install_judge(monkeypatch)
        for session, text in (("s1", CORRECTION), ("s2", SECOND_CORRECTION)):
            await seed_corrections(store, settings, tmp_path, [text], session=session)
            await judge_pass(store, settings=settings)
        candidates = store.candidates(REPO)
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
        [candidate] = store.candidates(REPO)
        assert (candidate["rule"], candidate["source_kind"]) == ("prefer-specific-except", "plan_review")
        assert candidate["observations"] == 1
        status = store.threshold_status(int(str(candidate["id"])), settings=settings)
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
        [before] = store.candidates(REPO)
        assert before["rule"] == dedup_key("question_answer", QUESTION, ANSWER)
        calls = install_judge(monkeypatch, slug="prefer-selectolax-parser")
        report = await judge_pass(store, settings=settings)
        assert (report.judged, report.merged, len(calls)) == (1, 1, 1)
        [after] = store.candidates(REPO)
        assert (after["rule"], after["source_kind"]) == ("prefer-selectolax-parser", "question_answer")
        assert after["observations"] == 1
        status = store.threshold_status(int(str(after["id"])), settings=settings)
        assert status.sessions == 1


@requires_llm_backend
class TestSuggestionPlumbing:
    async def test_prior_slug_evidence_ranks_the_matching_slug_first(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_fake_embedder(monkeypatch)
        await seed_corrections(store, settings, tmp_path, [CORRECTION], session="s1")
        store.record_verdict(
            dedup_key("transcript_message", "s1", CORRECTION),
            Verdict(canonical_key="prefer-uv-over-pip", summary="always use uv"),
            role=JUDGE_ROLE,
            prompt_version=store.versions.create,
            model="m1",
            fidelity="full",
        )
        await seed_corrections(store, settings, tmp_path, [SECOND_CORRECTION], session="s2")
        store.record_verdict(
            dedup_key("transcript_message", "s2", SECOND_CORRECTION),
            Verdict(canonical_key="prefer-frozen-dataclasses", summary="always freeze config"),
            role=JUDGE_ROLE,
            prompt_version=store.versions.create,
            model="m1",
            fidelity="full",
        )
        assert store.has_verdict_evidence()
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
        store.record_verdict(
            dedup_key("transcript_message", "s1", CORRECTION),
            Verdict(canonical_key="prefer-uv-over-pip"),
            role=JUDGE_ROLE,
            prompt_version=store.versions.create,
            model="m1",
            fidelity="full",
        )
        await seed_corrections(store, settings, tmp_path, [SECOND_CORRECTION], session="s2")
        digest_key = "deadbeef" * 8
        store.record_verdict(
            dedup_key("transcript_message", "s2", SECOND_CORRECTION),
            Verdict(canonical_key=digest_key),
            role=JUDGE_ROLE,
            prompt_version=store.versions.create,
            model="m1",
            fidelity="full",
        )
        raw = suggest_canonical_keys(store, THIRD_CORRECTION, prompt_version=store.versions.create, k=5)
        assert {"prefer-uv-over-pip", digest_key} <= {suggestion.canonical_key for suggestion in raw}
        await seed_corrections(store, settings, tmp_path, [THIRD_CORRECTION], session="s3")
        calls = install_judge(monkeypatch, slug="prefer-uv-over-pip")
        await judge_pass(store, settings=settings)
        [prompt] = calls
        assert "- prefer-uv-over-pip (" in prompt
        assert digest_key not in prompt


class TestRescanIdempotency:
    def rounds(self, *questions: str) -> list[dict[str, Any]]:
        return [
            entry
            for i, question in enumerate(questions)
            for entry in ask_user_question_round(question, notes=ANSWER, session="s1", tool_id=f"q{i}")
        ]

    async def test_rescan_grown_transcript_is_idempotent(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path
    ) -> None:
        chatter = [
            assistant_text("continuing the work", sessionId="s1"),
            assistant_text("all wrapped up", sessionId="s1"),
        ]
        path = tmp_path / "s.jsonl"
        write_transcript(path, self.rounds(QUESTION))
        assert await scan_transcript(store, path, settings=settings, repo_key=REPO) == ScanReport(scanned=1, inserted=1)
        assert (count_rows(store, "candidate_observations"), count_rows(store, "feedback_events")) == (1, 1)
        watermark = (store.file_mtimes())[str(path)]

        write_transcript(path, [*self.rounds(QUESTION), *chatter])
        os.utime(path, (watermark + 10, watermark + 10))
        assert (await scan_transcript(store, path, settings=settings, repo_key=REPO)).scanned == 1
        assert (count_rows(store, "candidate_observations"), count_rows(store, "feedback_events")) == (1, 1)
        advanced = (store.file_mtimes())[str(path)]
        assert advanced > watermark

        write_transcript(path, [*self.rounds(QUESTION, OTHER_QUESTION), *chatter])
        os.utime(path, (advanced + 10, advanced + 10))
        await scan_transcript(store, path, settings=settings, repo_key=REPO)
        assert count_rows(store, "candidate_observations") == 2
        assert count_rows(store, "feedback_events") == 2
