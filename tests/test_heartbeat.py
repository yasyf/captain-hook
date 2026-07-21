from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from cc_transcript.heartbeats import Heartbeat, HeartbeatLog

from captain_hook.cli import dispatch_event
from captain_hook.heartbeat import record_heartbeat
from captain_hook.types import Event
from tests.helpers import tool_payload

if TYPE_CHECKING:
    from pathlib import Path


async def _beats(db: Path, session_id: str) -> tuple[Heartbeat, ...]:
    async with await HeartbeatLog.open(db) as log:
        return await log.for_session(session_id)


def beats(db: Path, session_id: str) -> tuple[Heartbeat, ...]:
    return asyncio.run(_beats(db, session_id))


@pytest.fixture
def hb_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "decisions.db"
    monkeypatch.setenv("CAPT_HOOK_DECISIONS_DB", str(db))
    monkeypatch.delenv("CAPT_HOOK_SPAWNED", raising=False)
    return db


def test_record_heartbeat_upserts_count(hb_db: Path) -> None:
    record_heartbeat(Event.PreToolUse, {"session_id": "s1"})
    record_heartbeat(Event.PreToolUse, {"session_id": "s1"})
    (beat,) = beats(hb_db, "s1")
    assert beat.event == "PreToolUse"
    assert beat.count == 2


def test_record_heartbeat_noop_without_session_id(hb_db: Path) -> None:
    record_heartbeat(Event.PreToolUse, {})
    assert beats(hb_db, "s1") == ()


def test_record_heartbeat_noop_when_spawned(hb_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAPT_HOOK_SPAWNED", "1")
    record_heartbeat(Event.PreToolUse, {"session_id": "s1"})
    assert beats(hb_db, "s1") == ()


def test_distinct_events_beat_separately(hb_db: Path) -> None:
    record_heartbeat(Event.UserPromptSubmit, {"session_id": "s1"})
    record_heartbeat(Event.PreToolUse, {"session_id": "s1"})
    record_heartbeat(Event.Stop, {"session_id": "s1"})
    assert {beat.event for beat in beats(hb_db, "s1")} == {"UserPromptSubmit", "PreToolUse", "Stop"}


def test_dispatch_event_beats_on_sync_not_async(hb_db: Path, tmp_path: Path) -> None:
    raw = tool_payload("Bash", command="ls")
    dispatch_event(tmp_path, Event.PreToolUse, raw, session_dir=None, async_=True)
    assert beats(hb_db, "s1") == ()  # async process must not double-beat
    dispatch_event(tmp_path, Event.PreToolUse, raw, session_dir=None, async_=False)
    (beat,) = beats(hb_db, "s1")
    assert beat.event == "PreToolUse"
    assert beat.count == 1


def test_cold_beats_reuse_one_handle(hb_db: Path) -> None:
    # Cold record_heartbeat caches one ledger handle for the process; later beats reuse it rather
    # than spinning a fresh ConnectionActor per beat (the pre-@cache-drop regression this pins).
    import threading

    import captain_hook.heartbeat as heartbeat_mod

    assert heartbeat_mod._CACHED_LOG is None
    baseline = threading.active_count()
    record_heartbeat(Event.PreToolUse, {"session_id": "s1"})
    handle = heartbeat_mod._CACHED_LOG
    assert handle is not None
    record_heartbeat(Event.Stop, {"session_id": "s1"})
    assert heartbeat_mod._CACHED_LOG is handle  # same object, not reopened
    assert threading.active_count() <= baseline + 1  # one actor thread across both beats, not two
    assert {b.event for b in beats(hb_db, "s1")} == {"PreToolUse", "Stop"}
