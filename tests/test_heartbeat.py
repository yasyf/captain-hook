from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from cc_transcript.heartbeats import HeartbeatLog

from captain_hook.cli import dispatch_event
from captain_hook.heartbeat import open_heartbeat_log, record_heartbeat
from captain_hook.types import Event

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def hb_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "decisions.db"
    monkeypatch.setenv("CAPT_HOOK_DECISIONS_DB", str(db))
    monkeypatch.delenv("CAPT_HOOK_SPAWNED", raising=False)
    open_heartbeat_log.cache_clear()
    try:
        yield db
    finally:
        open_heartbeat_log.cache_clear()


def test_record_heartbeat_upserts_count(hb_db: Path) -> None:
    record_heartbeat(Event.PreToolUse, {"session_id": "s1"})
    record_heartbeat(Event.PreToolUse, {"session_id": "s1"})
    (beat,) = HeartbeatLog.open(hb_db).for_session("s1")
    assert beat.event == "PreToolUse"
    assert beat.count == 2


def test_record_heartbeat_noop_without_session_id(hb_db: Path) -> None:
    record_heartbeat(Event.PreToolUse, {})
    assert HeartbeatLog.open(hb_db).for_session("s1") == ()


def test_record_heartbeat_noop_when_spawned(hb_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAPT_HOOK_SPAWNED", "1")
    record_heartbeat(Event.PreToolUse, {"session_id": "s1"})
    assert HeartbeatLog.open(hb_db).for_session("s1") == ()


def test_distinct_events_beat_separately(hb_db: Path) -> None:
    record_heartbeat(Event.UserPromptSubmit, {"session_id": "s1"})
    record_heartbeat(Event.PreToolUse, {"session_id": "s1"})
    record_heartbeat(Event.Stop, {"session_id": "s1"})
    assert HeartbeatLog.open(hb_db).events_seen("s1") == frozenset({"UserPromptSubmit", "PreToolUse", "Stop"})


def test_dispatch_event_beats_on_sync_not_async(hb_db: Path, tmp_path: Path) -> None:
    raw = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls"}}
    dispatch_event(tmp_path, Event.PreToolUse, raw, session_dir=None, async_=True)
    assert HeartbeatLog.open(hb_db).for_session("s1") == ()  # async process must not double-beat
    dispatch_event(tmp_path, Event.PreToolUse, raw, session_dir=None, async_=False)
    (beat,) = HeartbeatLog.open(hb_db).for_session("s1")
    assert beat.event == "PreToolUse"
    assert beat.count == 1
