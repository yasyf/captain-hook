from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from captain_hook.app import get_matching_hooks, on
from captain_hook.dispatch import execute_hook
from captain_hook.events import StopEvent
from captain_hook.fire_log import FireLog, open_fire_log, record_fire
from captain_hook.session import session_hash
from captain_hook.settings import resolve_fire_log_enabled, resolve_fire_log_path
from captain_hook.tests.helpers import mock_stop_event
from captain_hook.types import Action, Event, HookResult, HookSpec, RegisteredHook


def entry(name: str = "h", source_file: str = "/x/hooks/h.py") -> RegisteredHook:
    return RegisteredHook(spec=HookSpec(events=Event.Stop), name=name, source_file=source_file)


def stop_evt(transcript_path: Path | None, session_id: str = "claude-sess") -> StopEvent:
    raw: dict = {"session_id": session_id}
    if transcript_path is not None:
        raw["transcript_path"] = str(transcript_path)
    return StopEvent(_raw=raw, ctx=MagicMock())


def seed(db: FireLog) -> None:
    db.append(
        ts=1.0,
        session_id="s",
        claude_session_id=None,
        repo_key=None,
        hook_name="h1",
        source_file="a.py",
        event="Stop",
        action="warn",
        message="alpha warning",
    )
    db.append(
        ts=2.0,
        session_id="s",
        claude_session_id=None,
        repo_key=None,
        hook_name="h2",
        source_file="b.py",
        event="Stop",
        action="warn",
        message="beta warning",
    )


@pytest.fixture
def db(tmp_path: Path) -> FireLog:
    return FireLog.open(tmp_path / "fires.db")


class TestStore:
    def test_append_round_trips(self, db: FireLog) -> None:
        db.append(
            ts=1.0,
            session_id="s",
            claude_session_id="c",
            repo_key="r",
            hook_name="h",
            source_file="a.py",
            event="Stop",
            action="warn",
            message="hi",
        )
        (row,) = db.fires_for_session("s")
        assert (row.hook_name, row.source_file, row.event, row.action, row.message) == (
            "h",
            "a.py",
            "Stop",
            "warn",
            "hi",
        )
        assert row.claude_session_id == "c"
        assert row.repo_key == "r"

    def test_fires_for_session_isolates_by_session(self, db: FireLog) -> None:
        seed(db)
        assert len(db.fires_for_session("s")) == 2
        assert db.fires_for_session("other") == []


class TestAttribute:
    def test_nearest_preceding_substring_match(self, db: FireLog) -> None:
        seed(db)
        r = db.attribute("s", message="here it is: beta warning, done", near_ts=3.0)
        assert r is not None and r.source_file == "b.py"

    def test_near_ts_excludes_later_fires(self, db: FireLog) -> None:
        seed(db)
        r = db.attribute("s", message="alpha warning", near_ts=1.5)
        assert r is not None and r.source_file == "a.py"

    def test_multi_warn_blob_is_ambiguous(self, db: FireLog) -> None:
        seed(db)
        assert db.attribute("s", message="alpha warning\n\nbeta warning", near_ts=3.0) is None

    def test_no_substring_match_returns_none(self, db: FireLog) -> None:
        seed(db)
        assert db.attribute("s", message="something unrelated", near_ts=3.0) is None

    def test_empty_source_file_is_unattributable(self, db: FireLog) -> None:
        db.append(
            ts=1.0,
            session_id="s",
            claude_session_id=None,
            repo_key=None,
            hook_name="decl",
            source_file="",
            event="Stop",
            action="warn",
            message="declarative msg",
        )
        assert db.attribute("s", message="declarative msg", near_ts=2.0) is None

    def test_tie_break_prefers_latest_id_within_one_source(self, db: FireLog) -> None:
        for _ in range(2):
            db.append(
                ts=1.0,
                session_id="s",
                claude_session_id=None,
                repo_key=None,
                hook_name="h",
                source_file="a.py",
                event="Stop",
                action="warn",
                message="dup",
            )
        r = db.attribute("s", message="dup", near_ts=2.0)
        assert r is not None and r.id == 2 and r.source_file == "a.py"

    def test_event_filter_narrows(self, db: FireLog) -> None:
        seed(db)
        assert db.attribute("s", message="beta warning", event="PreToolUse", near_ts=3.0) is None
        assert db.attribute("s", message="beta warning", event="Stop", near_ts=3.0) is not None


class TestRecordFire:
    @pytest.fixture(autouse=True)
    def fire_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HOOKS_FIRE_LOG_PATH", str(tmp_path / "fires.db"))
        monkeypatch.setenv("HOOKS_FIRE_LOG_ENABLED", "1")
        monkeypatch.delenv("CAPT_HOOK_SPAWNED", raising=False)
        open_fire_log.cache_clear()
        yield
        open_fire_log.cache_clear()

    def log(self) -> FireLog:
        return open_fire_log(resolve_fire_log_path())

    def test_writes_a_row_on_fire(self, tmp_path: Path) -> None:
        tp = tmp_path / "t.jsonl"
        record_fire(entry("myhook", "/h/myhook.py"), stop_evt(tp), HookResult(action=Action.warn, message="watch out"))
        (row,) = self.log().fires_for_session(session_hash(tp))
        assert (row.hook_name, row.source_file, row.event, row.action, row.message) == (
            "myhook",
            "/h/myhook.py",
            "Stop",
            "warn",
            "watch out",
        )
        assert row.claude_session_id == "claude-sess"

    def test_spawned_run_does_not_self_log(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CAPT_HOOK_SPAWNED", "1")
        record_fire(entry(), stop_evt(tmp_path / "t.jsonl"), HookResult(action=Action.warn, message="x"))
        assert self.log().conn.execute("SELECT COUNT(*) FROM fires").fetchone()[0] == 0

    def test_disabled_skips(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOOKS_FIRE_LOG_ENABLED", "0")
        record_fire(entry(), stop_evt(tmp_path / "t.jsonl"), HookResult(action=Action.warn, message="x"))
        assert self.log().conn.execute("SELECT COUNT(*) FROM fires").fetchone()[0] == 0

    def test_missing_transcript_skips(self, tmp_path: Path) -> None:
        record_fire(entry(), stop_evt(None), HookResult(action=Action.warn, message="x"))
        assert self.log().conn.execute("SELECT COUNT(*) FROM fires").fetchone()[0] == 0


class TestDispatchIntegration:
    @pytest.fixture(autouse=True)
    def fire_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HOOKS_FIRE_LOG_PATH", str(tmp_path / "fires.db"))
        monkeypatch.delenv("CAPT_HOOK_SPAWNED", raising=False)
        open_fire_log.cache_clear()
        yield
        open_fire_log.cache_clear()

    def evt(self, tmp_path: Path) -> StopEvent:
        evt = mock_stop_event(session_dir=tmp_path)
        evt._raw["transcript_path"] = str(tmp_path / "t.jsonl")
        evt._raw["session_id"] = "claude-sess"
        return evt

    def test_fire_through_dispatch_writes_row_with_handler_source(self, tmp_path: Path) -> None:
        @on(Event.Stop)
        def my_stop_hook(evt):
            return HookResult(action=Action.warn, message="stop nudge")

        evt = self.evt(tmp_path)
        for e in get_matching_hooks(evt):
            execute_hook(e, evt, tmp_path)
        log = open_fire_log(resolve_fire_log_path())
        (row,) = log.fires_for_session(session_hash(tmp_path / "t.jsonl"))
        assert row.hook_name == "my_stop_hook"
        assert row.source_file.endswith("test_fire_log.py")
        assert (row.event, row.action, row.message) == ("Stop", "warn", "stop nudge")

    def test_dispatch_survives_fire_log_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import captain_hook.dispatch as dispatch_mod

        def boom(*_a, **_k):
            raise RuntimeError("db locked")

        monkeypatch.setattr(dispatch_mod, "record_fire", boom)

        @on(Event.Stop)
        def my_stop_hook(evt):
            return HookResult(action=Action.warn, message="kept")

        evt = self.evt(tmp_path)
        results = [execute_hook(e, evt, tmp_path) for e in get_matching_hooks(evt)]
        assert any(r is not None and r.message == "kept" for r in results)


class TestFireLogSettings:
    def test_path_defaults_under_state_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HOOKS_FIRE_LOG_PATH", raising=False)
        monkeypatch.setenv("CAPTAIN_HOOK_STATE_DIR", str(tmp_path))
        assert resolve_fire_log_path() == tmp_path / "hooks" / "fires.db"

    def test_path_env_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOOKS_FIRE_LOG_PATH", str(tmp_path / "x.db"))
        assert resolve_fire_log_path() == tmp_path / "x.db"

    @pytest.mark.parametrize(
        ("val", "expected"),
        [("1", True), ("true", True), ("0", False), ("false", False), ("no", False), ("", False)],
    )
    def test_enabled_parsing(self, val: str, expected: bool, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOOKS_FIRE_LOG_ENABLED", val)
        assert resolve_fire_log_enabled() is expected
