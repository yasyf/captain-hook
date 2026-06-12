from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from cc_transcript.decisions import Decision, DecisionLog
from cc_transcript.ids import SessionId, tool_digest

from captain_hook.app import get_matching_hooks
from captain_hook.decisions import open_decision_log, record_decision
from captain_hook.dispatch import execute_hook
from captain_hook.events import PreToolUseEvent, StopEvent
from captain_hook.primitives.nudge import nudge
from captain_hook.tests.helpers import mock_tool_event
from captain_hook.types import Action, Event, HookResult, HookSpec, RegisteredHook

SESSION_ID = SessionId("claude-sess")

EDIT_INPUT = {"file_path": "a.py", "old_string": "x", "new_string": "y"}


def entry(name: str = "h", source_file: str = "/x/hooks/h.py") -> RegisteredHook:
    return RegisteredHook(spec=HookSpec(events=Event.Stop), name=name, source_file=source_file)


def pre_tool_evt(tool: str, tool_input: dict[str, Any]) -> PreToolUseEvent:
    return PreToolUseEvent(
        _raw={"session_id": SESSION_ID, "tool_name": tool, "tool_input": tool_input}, ctx=MagicMock()
    )


def stop_evt(session_id: str | None = SESSION_ID) -> StopEvent:
    raw: dict[str, Any] = {"session_id": session_id} if session_id else {}
    return StopEvent(_raw=raw, ctx=MagicMock())


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "decisions.db"
    monkeypatch.setenv("CAPT_HOOK_DECISIONS_DB", str(path))
    monkeypatch.delenv("CAPT_HOOK_SPAWNED", raising=False)
    return path


def rows(db_path: Path) -> tuple[Decision, ...]:
    return DecisionLog.open(db_path).for_session(SESSION_ID)


class TestRecordDecision:
    def test_pre_tool_use_warn_lands_tool_row(self, db_path: Path) -> None:
        before_ms = int(time.time() * 1000)
        record_decision(
            entry("myhook", "/h/myhook.py"),
            pre_tool_evt("Edit", EDIT_INPUT),
            HookResult(action=Action.warn, message="watch out"),
        )
        (row,) = rows(db_path)
        assert (row.source, row.kind, row.source_file, row.event, row.action, row.message) == (
            "captain-hook",
            "myhook",
            "/h/myhook.py",
            "PreToolUse",
            "warn",
            "watch out",
        )
        assert row.tool_name == "Edit"
        assert row.tool_digest == tool_digest("Edit", EDIT_INPUT)
        assert row.event_uuid is None
        assert row.detail == {}
        assert row.ts_ms >= before_ms

    def test_stop_block_lands_null_tool_fields(self, db_path: Path) -> None:
        record_decision(entry("gatekeeper"), stop_evt(), HookResult(action=Action.block, message="not yet"))
        (row,) = rows(db_path)
        assert (row.kind, row.event, row.action, row.message) == ("gatekeeper", "Stop", "block", "not yet")
        assert row.tool_name is None
        assert row.tool_digest is None

    def test_degraded_parse_marks_detail(self, db_path: Path) -> None:
        malformed = {"file_path": "a.py", "old_string": "x"}
        record_decision(entry(), pre_tool_evt("Edit", malformed), HookResult(action=Action.warn, message="m"))
        (row,) = rows(db_path)
        assert row.detail == {"degraded": True}
        assert row.tool_name == "Edit"
        assert row.tool_digest == tool_digest("Edit", malformed)

    def test_unknown_tool_is_not_degraded(self, db_path: Path) -> None:
        record_decision(
            entry(), pre_tool_evt("mcp__github__search", {"q": "x"}), HookResult(action=Action.warn, message="m")
        )
        (row,) = rows(db_path)
        assert row.detail == {}
        assert row.tool_name == "mcp__github__search"
        assert row.tool_digest == tool_digest("mcp__github__search", {"q": "x"})

    def test_spawned_run_does_not_write(self, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CAPT_HOOK_SPAWNED", "1")
        record_decision(entry(), stop_evt(), HookResult(action=Action.warn, message="x"))
        assert rows(db_path) == ()

    def test_missing_session_id_skips(self, db_path: Path) -> None:
        record_decision(entry(), stop_evt(session_id=None), HookResult(action=Action.warn, message="x"))
        assert not db_path.exists()


class TestDispatchIntegration:
    def test_primitive_built_hook_keeps_source_pair(self, db_path: Path, tmp_path: Path) -> None:
        nudge("Remember to run tests", when=lambda evt: True)

        evt = mock_tool_event(tool="Edit", file="a.py", content="y", old="x", session_dir=tmp_path)
        evt._raw["session_id"] = SESSION_ID
        for e in get_matching_hooks(evt):
            execute_hook(e, evt, tmp_path)

        (row,) = rows(db_path)
        assert row.kind.split(":")[-1].startswith("nudge")
        assert row.source_file.endswith("captain_hook/primitives/nudge.py")
        assert (row.event, row.action, row.message) == ("PreToolUse", "warn", "Remember to run tests")
        assert row.tool_name == "Edit"
        assert row.tool_digest == evt.tool_digest

    def test_dispatch_survives_decision_write_failure(self, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import captain_hook.dispatch as dispatch_mod

        def boom(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("db locked")

        monkeypatch.setattr(dispatch_mod, "record_decision", boom)
        nudge("kept", when=lambda evt: True)

        evt = mock_tool_event(tool="Edit", file="a.py")
        evt._raw["session_id"] = SESSION_ID
        results = [execute_hook(e, evt) for e in get_matching_hooks(evt)]
        assert any(r is not None and r.message == "kept" for r in results)


class TestOpenDecisionLog:
    def test_cached_per_path(self, db_path: Path) -> None:
        assert open_decision_log(db_path) is open_decision_log(db_path)
