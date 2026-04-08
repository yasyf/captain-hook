from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

from captain_hook.session import (
    SessionSlot,
    SessionStore,
    cleanup_stale,
    ensure_session,
    session_hash,
    state_root,
)

# ── Test models ──────────────────────────────────────────────────────────────


class MyModel(BaseModel):
    name: str
    value: int


class MyCustomModel(BaseModel):
    score: float


class AnotherModel(BaseModel):
    label: str


# ── VAL-SESS-001: ensure_session creates session directory ───────────────────


def test_ensure_session_creates_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOOKS_STATE_DIR", str(tmp_path))
    transcript = tmp_path / "transcript.jsonl"
    transcript.touch()

    sd = ensure_session(transcript)
    assert sd.is_dir()
    assert sd.parent.name == "sessions"


# ── VAL-SESS-002: Session hash is deterministic ─────────────────────────────


def test_session_hash_deterministic() -> None:
    p = Path("/some/transcript.jsonl")
    assert session_hash(p) == session_hash(p)
    assert session_hash(str(p)) == session_hash(p)


def test_session_hash_different_paths() -> None:
    assert session_hash("/a") != session_hash("/b")


# ── VAL-SESS-003: Session directory contains .transcript_path marker ─────────


def test_ensure_session_creates_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOOKS_STATE_DIR", str(tmp_path))
    transcript = tmp_path / "transcript.jsonl"
    transcript.touch()

    sd = ensure_session(transcript)
    marker = sd / ".transcript_path"
    assert marker.exists()
    assert marker.read_text() == str(transcript)


# ── VAL-SESS-004: cleanup_stale removes directories for missing transcripts ──


def test_cleanup_stale_removes_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOOKS_STATE_DIR", str(tmp_path))
    transcript = tmp_path / "transcript.jsonl"
    transcript.touch()

    sd = ensure_session(transcript)
    assert sd.is_dir()

    transcript.unlink()
    cleanup_stale()
    assert not sd.exists()


# ── VAL-SESS-005: cleanup_stale preserves valid sessions ─────────────────────


def test_cleanup_stale_preserves_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOOKS_STATE_DIR", str(tmp_path))
    transcript = tmp_path / "transcript.jsonl"
    transcript.touch()

    sd = ensure_session(transcript)
    cleanup_stale()
    assert sd.is_dir()


# ── VAL-SESS-006: Atomic writes for session state ────────────────────────────


def test_atomic_write_produces_valid_json(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    slot = store[MyModel]
    slot.set(MyModel(name="test", value=42))

    content = slot.path.read_text()
    parsed = json.loads(content)
    assert parsed["name"] == "test"
    assert parsed["value"] == 42


# ── VAL-SESS-007: Session dir parent creation is automatic ───────────────────


def test_session_slot_creates_parent_dirs(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "nested" / "dir"
    store = SessionStore(nested)
    slot = store[MyModel]
    slot.set(MyModel(name="test", value=1))
    assert slot.get() == MyModel(name="test", value=1)


# ── VAL-STATE (SessionSlot/SessionStore basics needed for context) ───────────


def test_session_slot_get_returns_none_when_empty(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    assert store[MyModel].get() is None


def test_session_slot_set_get_roundtrip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    slot = store[MyModel]
    obj = MyModel(name="hello", value=99)
    slot.set(obj)
    assert slot.get() == obj


def test_session_slot_snake_case_filename(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    slot = store[MyCustomModel]
    assert slot.path.name == "my_custom_model.json"


def test_session_slot_delete(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    slot = store[MyModel]
    slot.set(MyModel(name="x", value=1))
    assert slot.get() is not None
    slot.delete()
    assert slot.get() is None


def test_session_store_none_dir() -> None:
    store = SessionStore(None)
    slot = store[MyModel]
    assert slot.get() is None
    slot.set(MyModel(name="x", value=1))
    assert slot.get() is None


def test_multiple_models_share_dir(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    store[MyModel].set(MyModel(name="a", value=1))
    store[AnotherModel].set(AnotherModel(label="b"))
    assert (tmp_path / "my_model.json").exists()
    assert (tmp_path / "another_model.json").exists()


def test_corrupted_json_returns_none(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    slot = store[MyModel]
    slot.path.write_text("not valid json {{{")
    assert slot.get() is None


def test_session_slot_generic_type(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    slot = store[MyModel]
    assert isinstance(slot, SessionSlot)
    slot.set(MyModel(name="test", value=1))
    result = slot.get()
    assert isinstance(result, MyModel)


def test_session_slot_set_creates_parents(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c"
    store = SessionStore(nested)
    slot = store[MyModel]
    slot.set(MyModel(name="deep", value=42))
    assert slot.get() == MyModel(name="deep", value=42)


def test_session_slot_set_oserror_caught(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SessionStore(tmp_path)
    slot = store[MyModel]
    monkeypatch.setattr(Path, "write_text", lambda *a, **kw: (_ for _ in ()).throw(OSError("no perms")))
    slot.set(MyModel(name="fail", value=0))


def test_session_slot_delete_nonexistent(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    slot = store[MyModel]
    slot.delete()


# ── VAL-CTX: HookContext ─────────────────────────────────────────────────────


def test_context_t_returns_transcript() -> None:
    from captain_hook.context import HookContext

    transcript = MagicMock()
    ctx = HookContext(session=SessionStore(None), transcript=transcript, settings=None)
    assert ctx.t is transcript
    assert ctx.t is ctx.transcript


def test_context_s_returns_session_store() -> None:
    from captain_hook.context import HookContext

    store = SessionStore(None)
    ctx = HookContext(session=store, transcript=MagicMock(), settings=None)
    assert ctx.s is store
    assert ctx.s is ctx.session


def test_context_conf_returns_settings() -> None:
    from captain_hook.context import HookContext

    settings = MagicMock()
    ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=settings)
    assert ctx.conf is settings
    assert ctx.c is settings
    assert ctx.conf is ctx.c


def test_context_conf_returns_none_when_no_settings() -> None:
    from captain_hook.context import HookContext

    ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
    assert ctx.conf is None
    assert ctx.c is None


def test_context_turn_cached() -> None:
    from captain_hook.context import HookContext

    transcript = MagicMock()
    turn_mock = MagicMock()
    turn_mock.start_idx = 0
    transcript.current_turn = turn_mock
    ctx = HookContext(session=SessionStore(None), transcript=transcript, settings=None)
    assert ctx.turn is ctx.turn
    assert ctx.turn is turn_mock


def test_context_prior_cached() -> None:
    from captain_hook.context import HookContext

    transcript = MagicMock()
    prior_mock = MagicMock()
    transcript.prior.return_value = prior_mock
    ctx = HookContext(session=SessionStore(None), transcript=transcript, settings=None)
    assert ctx.prior is prior_mock
    assert ctx.prior is ctx.prior


# ── VAL-CTX-004: call_cli subprocess execution ──────────────────────────────


def test_call_cli_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook.context import HookContext

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp")
    ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
    result = ctx.call_cli(["echo", "hello"])
    assert "hello" in result


def test_call_cli_raises_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook.context import HookContext

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp")
    ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        ctx.call_cli(["false"])
    assert exc_info.value.returncode != 0


# ── VAL-CTX-012: call_cli uses CLAUDE_PROJECT_DIR as cwd ────────────────────


def test_call_cli_uses_project_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook.context import HookContext

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("FACTORY_PROJECT_DIR", raising=False)
    ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
    result = ctx.call_cli(["pwd"])
    assert result.strip() == str(tmp_path)


def test_call_cli_uses_factory_project_dir_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook.context import HookContext

    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setenv("FACTORY_PROJECT_DIR", str(tmp_path))
    ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)
    result = ctx.call_cli(["pwd"])
    assert result.strip() == str(tmp_path)


# ── VAL-CTX-005/006/007: call_llm with backend dispatch ─────────────────────


def test_call_llm_backend_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook.context import HookContext

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp")
    ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)

    with patch.object(ctx, "call_cli", return_value="mocked response") as mock_cli:
        result = ctx.call_llm("test prompt", specialty="review")
        assert result == "mocked response"
        cmd = mock_cli.call_args[0][0]
        assert "codex" in cmd


def test_call_llm_general_uses_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook.context import HookContext

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp")
    ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)

    with patch.object(ctx, "call_cli", return_value="mocked response") as mock_cli:
        result = ctx.call_llm("test prompt", specialty="general")
        assert result == "mocked response"
        cmd = mock_cli.call_args[0][0]
        assert "claude" in cmd


def test_call_llm_with_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook.context import HookContext

    transcript = MagicMock()
    transcript.__str__ = lambda self: "transcript content here"
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp")
    ctx = HookContext(session=SessionStore(None), transcript=transcript, settings=None)

    with patch.object(ctx, "call_cli", return_value="mocked") as mock_cli:
        ctx.call_llm("analyze this", transcript=True)
        input_text = mock_cli.call_args[1].get("input", "")
        assert "transcript content here" in input_text
        assert "<task>" in input_text


def test_call_llm_with_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook.context import HookContext

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp")
    ctx = HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)

    with patch.object(ctx, "call_cli", return_value="mocked") as mock_cli:
        ctx.call_llm("test", agent=True, specialty="general")
        cmd = mock_cli.call_args[0][0]
        assert "--permission-mode" in cmd


def test_call_llm_with_response_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook.context import HookContext

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


# ── VAL-CTX: ctx.state accessor ──────────────────────────────────────────────


def test_context_state_returns_session_store() -> None:
    from captain_hook.context import HookContext

    store = SessionStore(None)
    ctx = HookContext(session=store, transcript=MagicMock(), settings=None)
    assert ctx.state is store
    assert ctx.state is ctx.s


def test_context_state_getitem_works(tmp_path: Path) -> None:
    from captain_hook.context import HookContext

    store = SessionStore(tmp_path)
    ctx = HookContext(session=store, transcript=MagicMock(), settings=None)
    ctx.state[MyModel].set(MyModel(name="via_state", value=123))
    result = ctx.state[MyModel].get()
    assert result is not None
    assert result.name == "via_state"
    assert result.value == 123


def test_context_state_and_s_share_storage(tmp_path: Path) -> None:
    from captain_hook.context import HookContext

    store = SessionStore(tmp_path)
    ctx = HookContext(session=store, transcript=MagicMock(), settings=None)
    ctx.s[MyModel].set(MyModel(name="through_s", value=42))
    result = ctx.state[MyModel].get()
    assert result is not None
    assert result.name == "through_s"


# ── state_root helper ────────────────────────────────────────────────────────


def test_state_root_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_HOOKS_STATE_DIR", "/custom/state")
    assert state_root() == Path("/custom/state")


def test_state_root_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_HOOKS_STATE_DIR", raising=False)
    result = state_root()
    assert result == Path.home() / ".claude" / "state"
