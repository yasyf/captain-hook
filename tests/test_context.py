from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cc_transcript.ids import SessionId
from pydantic import BaseModel

from captain_hook.context import HookContext
from captain_hook.session import (
    STALE_AGE_SECONDS,
    SessionStore,
    cleanup_stale,
    ensure_session,
    state_root,
)


class MyModel(BaseModel):
    name: str
    value: int


SESSION_ID = "11111111-2222-3333-4444-555555555555"


def age_dir(path: Path, *, seconds: int) -> None:
    stale = time.time() - seconds
    os.utime(path, (stale, stale))


class TestSessionManagement:
    def test_ensure_session_creates_directory_keyed_by_session_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_HOOKS_STATE_DIR", str(tmp_path))

        sd = ensure_session(SessionId(SESSION_ID))
        assert sd.is_dir()
        assert sd.name == SESSION_ID
        assert sd.parent.name == "sessions"
        assert list(sd.iterdir()) == []

    def test_cleanup_stale_removes_old_dir_without_transcript(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_HOOKS_STATE_DIR", str(tmp_path))
        monkeypatch.setattr("captain_hook.session.find_transcript_sync", lambda session_id: None)

        sd = ensure_session(SessionId(SESSION_ID))
        age_dir(sd, seconds=STALE_AGE_SECONDS + 60)
        cleanup_stale()
        assert not sd.exists()

    def test_cleanup_stale_preserves_recent_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_HOOKS_STATE_DIR", str(tmp_path))
        monkeypatch.setattr("captain_hook.session.find_transcript_sync", lambda session_id: None)

        sd = ensure_session(SessionId(SESSION_ID))
        cleanup_stale()
        assert sd.is_dir()

    def test_cleanup_stale_preserves_old_dir_with_living_transcript(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CLAUDE_HOOKS_STATE_DIR", str(tmp_path))
        transcript = tmp_path / f"{SESSION_ID}.jsonl"
        transcript.touch()
        seen: list[str] = []

        def fake_find(session_id):
            seen.append(str(session_id))
            return transcript

        monkeypatch.setattr("captain_hook.session.find_transcript_sync", fake_find)

        sd = ensure_session(SessionId(SESSION_ID))
        age_dir(sd, seconds=STALE_AGE_SECONDS + 60)
        cleanup_stale()
        assert sd.is_dir()
        assert seen == [SESSION_ID]

    def test_atomic_write_produces_valid_json(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        slot = store[MyModel]
        slot.set(MyModel(name="test", value=42))

        content = slot.path.read_text()
        parsed = json.loads(content)
        assert parsed["name"] == "test"
        assert parsed["value"] == 42

    def test_session_slot_creates_parent_dirs(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested" / "dir"
        store = SessionStore(nested)
        slot = store[MyModel]
        slot.set(MyModel(name="test", value=1))
        assert slot.get() == MyModel(name="test", value=1)


class TestContextCaching:
    def test_turn_cached(self) -> None:
        transcript = MagicMock()
        turn_mock = MagicMock()
        transcript.current_turn = turn_mock
        ctx = HookContext(session=SessionStore(None), transcript=transcript, settings=None)
        assert ctx.turn is ctx.turn
        assert ctx.turn is turn_mock

    def test_prior_cached(self) -> None:
        transcript = MagicMock()
        prior_mock = MagicMock()
        transcript.prior.return_value = prior_mock
        ctx = HookContext(session=SessionStore(None), transcript=transcript, settings=None)
        assert ctx.prior is prior_mock
        assert ctx.prior is ctx.prior


class TestCallCli:
    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp")
        ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
        result = ctx.call_cli(["echo", "hello"])
        assert "hello" in result

    def test_raises_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp")
        ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            ctx.call_cli(["false"])
        assert exc_info.value.returncode != 0

    def test_failure_attaches_stderr_as_note(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp")
        ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            ctx.call_cli(["sh", "-c", "echo Invalid API key >&2; exit 1"])
        assert "Invalid API key" in exc_info.value.stderr
        assert any("Invalid API key" in note for note in getattr(exc_info.value, "__notes__", []))

    def test_failure_attaches_stdout_as_note(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp")
        ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            ctx.call_cli(["sh", "-c", "echo partial-output; exit 2"])
        assert any("partial-output" in note for note in getattr(exc_info.value, "__notes__", []))

    def test_uses_project_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        monkeypatch.delenv("FACTORY_PROJECT_DIR", raising=False)
        ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
        result = ctx.call_cli(["pwd"])
        assert result.strip() == str(tmp_path)

    def test_uses_factory_project_dir_fallback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.setenv("FACTORY_PROJECT_DIR", str(tmp_path))
        ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
        result = ctx.call_cli(["pwd"])
        assert result.strip() == str(tmp_path)


class TestCallLlm:
    def test_backend_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp")
        ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)

        with patch.object(ctx, "call_cli", return_value="mocked response") as mock_cli:
            result = ctx.call_llm("test prompt", specialty="review")
            assert result == "mocked response"
            cmd = mock_cli.call_args[0][0]
            assert "codex" in cmd

    def test_general_uses_claude(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp")
        ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)

        with patch.object(ctx, "call_cli", return_value="mocked response") as mock_cli:
            result = ctx.call_llm("test prompt", specialty="general")
            assert result == "mocked response"
            cmd = mock_cli.call_args[0][0]
            assert "claude" in cmd

    def test_with_transcript(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from captain_hook.testing.helpers import fixture_session
        from captain_hook.tests.helpers import raw_text

        transcript = fixture_session([raw_text("user", "transcript content here")])
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp")
        ctx = HookContext(session=SessionStore(None), transcript=transcript, settings=None)

        with patch.object(ctx, "call_cli", return_value="mocked") as mock_cli:
            ctx.call_llm("analyze this", transcript=True)
            input_text = mock_cli.call_args[1].get("input", "")
            assert "transcript content here" in input_text
            assert "<task>" in input_text

    def test_with_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp")
        ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)

        with patch.object(ctx, "call_cli", return_value="mocked") as mock_cli:
            ctx.call_llm("test", agent=True, specialty="general")
            cmd = mock_cli.call_args[0][0]
            assert "--permission-mode" in cmd

    def test_with_response_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Verdict(BaseModel):
            should_block: bool
            reason: str

        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp")
        ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)

        with patch.object(ctx, "call_cli", return_value='{"should_block": true, "reason": "bad"}'):
            result = ctx.call_llm("test", response_model=Verdict, specialty="review")
            assert isinstance(result, Verdict)
            assert result.should_block is True
            assert result.reason == "bad"


class TestContextState:
    def test_getitem_works(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        ctx = HookContext(session=store, transcript=MagicMock(), settings=None)
        ctx.state[MyModel].set(MyModel(name="via_state", value=123))
        result = ctx.state[MyModel].get()
        assert result == MyModel(name="via_state", value=123)


class TestStateRoot:
    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_HOOKS_STATE_DIR", "/custom/state")
        assert state_root() == Path("/custom/state")

    def test_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDE_HOOKS_STATE_DIR", raising=False)
        result = state_root()
        assert result == Path.home() / ".claude" / "state"


def git_in(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def make_repo(path: Path, *, branch: str = "test-branch") -> None:
    git_in(path, "init", "--initial-branch", branch)
    git_in(path, "config", "user.email", "test@example.com")
    git_in(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hi\n")
    git_in(path, "add", "README.md")
    git_in(path, "commit", "-m", "init")


def make_ctx_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HookContext:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("FACTORY_PROJECT_DIR", raising=False)
    return HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)


class TestChangedPaths:
    def test_returns_paths_with_net_diff(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        make_repo(tmp_path)
        (tmp_path / "README.md").write_text("changed\n")
        ctx = make_ctx_in(tmp_path, monkeypatch)
        assert ctx.changed_paths == frozenset({(tmp_path / "README.md").resolve()})

    def test_empty_when_clean(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        make_repo(tmp_path)
        ctx = make_ctx_in(tmp_path, monkeypatch)
        assert ctx.changed_paths == frozenset()

    def test_none_outside_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = make_ctx_in(tmp_path, monkeypatch)
        assert ctx.changed_paths is None

    def test_cached_no_reshell_on_second_access(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        make_repo(tmp_path)
        (tmp_path / "README.md").write_text("changed\n")
        ctx = make_ctx_in(tmp_path, monkeypatch)
        with patch("captain_hook.context.subprocess.run", wraps=subprocess.run) as spy:
            assert ctx.changed_paths is not None
            count_after_first = spy.call_count
            assert ctx.changed_paths is not None
            assert spy.call_count == count_after_first


class TestRepoRoot:
    def test_uses_configured_project_root(self, tmp_path: Path) -> None:
        ctx = HookContext(
            session=SessionStore(None),
            transcript=MagicMock(),
            settings=None,
            project_root=tmp_path,
        )
        assert ctx.repo_root == tmp_path.resolve()

    def test_from_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        make_repo(tmp_path)
        ctx = make_ctx_in(tmp_path, monkeypatch)
        assert ctx.repo_root == tmp_path.resolve()

    def test_from_subdir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        make_repo(tmp_path)
        (subdir := tmp_path / "sub").mkdir()
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(subdir))
        monkeypatch.delenv("FACTORY_PROJECT_DIR", raising=False)
        ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
        assert ctx.repo_root == tmp_path.resolve()

    def test_none_outside_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = make_ctx_in(tmp_path, monkeypatch)
        assert ctx.repo_root is None


class TestCurrentBranch:
    def test_returns_branch_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        make_repo(tmp_path, branch="test-branch")
        ctx = make_ctx_in(tmp_path, monkeypatch)
        assert ctx.current_branch == "test-branch"

    def test_none_on_detached_head(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        make_repo(tmp_path)
        git_in(tmp_path, "checkout", "--detach")
        ctx = make_ctx_in(tmp_path, monkeypatch)
        assert ctx.current_branch is None

    def test_none_outside_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = make_ctx_in(tmp_path, monkeypatch)
        assert ctx.current_branch is None
