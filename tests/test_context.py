from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from captain_hook.context import HookContext
from captain_hook.session import (
    SessionStore,
    cleanup_stale,
    ensure_session,
    session_hash,
    state_root,
)


class MyModel(BaseModel):
    name: str
    value: int


class TestSessionManagement:
    def test_ensure_session_creates_directory(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_HOOKS_STATE_DIR", str(tmp_path))
        transcript = tmp_path / "transcript.jsonl"
        transcript.touch()

        sd = ensure_session(transcript)
        assert sd.is_dir()
        assert sd.parent.name == "sessions"

    def test_session_hash_deterministic(self) -> None:
        p = Path("/some/transcript.jsonl")
        assert session_hash(p) == session_hash(p)
        assert session_hash(str(p)) == session_hash(p)

    def test_session_hash_different_paths(self) -> None:
        assert session_hash("/a") != session_hash("/b")

    def test_ensure_session_creates_marker(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_HOOKS_STATE_DIR", str(tmp_path))
        transcript = tmp_path / "transcript.jsonl"
        transcript.touch()

        sd = ensure_session(transcript)
        marker = sd / ".transcript_path"
        assert marker.exists()
        assert marker.read_text() == str(transcript)

    def test_cleanup_stale_removes_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_HOOKS_STATE_DIR", str(tmp_path))
        transcript = tmp_path / "transcript.jsonl"
        transcript.touch()

        sd = ensure_session(transcript)
        assert sd.is_dir()

        transcript.unlink()
        cleanup_stale()
        assert not sd.exists()

    def test_cleanup_stale_preserves_valid(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_HOOKS_STATE_DIR", str(tmp_path))
        transcript = tmp_path / "transcript.jsonl"
        transcript.touch()

        sd = ensure_session(transcript)
        cleanup_stale()
        assert sd.is_dir()

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
        turn_mock.start_idx = 0
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
        transcript = MagicMock()
        transcript.__str__ = lambda self: "transcript content here"
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
