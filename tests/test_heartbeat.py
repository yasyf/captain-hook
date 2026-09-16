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


def test_dispatch_event_beats_once_across_its_reply_and_background(hb_db: Path, tmp_path: Path) -> None:
    raw = tool_payload("Bash", command="ls")
    _, background = dispatch_event(tmp_path, Event.PreToolUse, raw, session_dir=None)
    background()
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


def test_within_margin_skips_sync_verdict_but_keeps_heartbeat_and_background(
    hb_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time
    from pathlib import Path as _Path

    from captain_hook import cli
    from captain_hook.util import reqenv

    sync_ran = False

    def fake_dispatch(event: Event, evt: object, *, session_dir: object = None) -> dict[str, object]:
        nonlocal sync_ran
        sync_ran = True
        return {"decision": "block"}

    monkeypatch.setattr(cli, "dispatch", fake_dispatch)
    overrides = reqenv.RequestOverrides(
        env={"CAPT_HOOK_DECISIONS_DB": str(hb_db)},
        cwd=".",
        client_ppid=1,
        session_id="s1",
        deadline_unix_ms=int((time.time() + 1) * 1000),
    )
    raw = {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "echo hi"}}
    with reqenv.use_request(overrides):
        envelope, background = cli.dispatch_event(_Path("/x"), Event.PreToolUse, raw, session_dir=None)

    assert envelope is None, "the synchronous verdict must be skipped inside the margin"
    assert sync_ran is False, "no synchronous hook fan-out for a verdict that would be skipped"
    (beat,) = beats(hb_db, "s1")
    assert beat.event == "PreToolUse"
    background()


def test_outside_margin_runs_the_sync_verdict(hb_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time
    from pathlib import Path as _Path

    from captain_hook import cli
    from captain_hook.util import reqenv

    sync_ran = False

    def fake_dispatch(event: Event, evt: object, *, session_dir: object = None) -> None:
        nonlocal sync_ran
        sync_ran = True
        return None

    monkeypatch.setattr(cli, "dispatch", fake_dispatch)
    overrides = reqenv.RequestOverrides(
        env={"CAPT_HOOK_DECISIONS_DB": str(hb_db)},
        cwd=".",
        client_ppid=1,
        session_id="s2",
        deadline_unix_ms=int((time.time() + 30) * 1000),
    )
    raw = {"session_id": "s2", "tool_name": "Bash", "tool_input": {"command": "echo hi"}}
    with reqenv.use_request(overrides):
        cli.dispatch_event(_Path("/x"), Event.PreToolUse, raw, session_dir=None)

    assert sync_ran is True, "a request with budget to spare must run its synchronous hooks"
