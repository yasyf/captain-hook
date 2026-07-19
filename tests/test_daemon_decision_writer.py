from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from cc_transcript.decisions import Decision, DecisionLog
from cc_transcript.ids import SessionId

from captain_hook import decisions, heartbeat
from captain_hook.daemon.decision_writer import MAX_LEDGER_LOGS, DecisionWriter, install
from captain_hook.decisions import record_decision
from captain_hook.events import StopEvent
from captain_hook.types import Action, Event, HookResult, HookSpec, RegisteredHook

SESSION = SessionId("sess-dw")


def make_decision(*, ts_ms: int = 1, kind: str = "k", message: str = "m") -> Decision:
    return Decision(
        ts_ms=ts_ms,
        session_id=SESSION,
        source="captain-hook",
        kind=kind,
        source_file="/f.py",
        event="Stop",
        action="warn",
        tool_name=None,
        tool_digest=None,
        message=message,
        detail={},
    )


def stop_entry(name: str = "h") -> RegisteredHook:
    return RegisteredHook(spec=HookSpec(events=Event.Stop), name=name, source_file="/x/hooks/h.py")


def stop_evt() -> StopEvent:
    return StopEvent(_raw={"session_id": SESSION}, ctx=MagicMock())


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "decisions.db"
    monkeypatch.setenv("CAPT_HOOK_DECISIONS_DB", str(path))
    monkeypatch.delenv("CAPT_HOOK_SPAWNED", raising=False)
    return path


async def _rows(db_path: Path) -> tuple[Decision, ...]:
    async with await DecisionLog.open(db_path) as log:
        return await log.for_session(SESSION)


def rows(db_path: Path) -> tuple[Decision, ...]:
    return asyncio.run(_rows(db_path))


class TestDecisionWriter:
    def test_drain_flushes_every_queued_row(self, db_path: Path) -> None:
        writer = DecisionWriter()
        writer.start()
        writer.submit(make_decision(ts_ms=1, kind="a", message="first"))
        writer.submit(make_decision(ts_ms=2, kind="b", message="second"))
        writer.drain(timeout=5)
        assert {row.message for row in rows(db_path)} == {"first", "second"}

    def test_writer_thread_owns_all_handles(self, db_path: Path) -> None:
        # Every submit crosses into the writer thread; a cross-thread sqlite handle would raise.
        writer = DecisionWriter()
        writer.start()
        for i in range(20):
            writer.submit(make_decision(ts_ms=i, kind=f"k{i}", message=f"m{i}"))
        writer.drain(timeout=5)
        assert len(rows(db_path)) == 20

    def test_log_handles_are_lru_bounded(self, tmp_path: Path) -> None:
        # S8: many distinct decisions-db paths must not open an unbounded set of handles.
        writer = DecisionWriter()  # thread not started; drive _log directly

        async def body() -> None:
            opened = [await writer._log(tmp_path / f"db-{i}.db") for i in range(MAX_LEDGER_LOGS + 8)]
            try:
                assert len(writer._logs) == MAX_LEDGER_LOGS, "the handle set grew past its bound"
                with pytest.raises(sqlite3.ProgrammingError):
                    await opened[0].for_session(SESSION)  # the earliest handle was evicted and closed
            finally:
                for log in writer._logs.values():
                    await log.close()

        asyncio.run(body())

    def test_full_queue_drops_row_with_warning(self, db_path: Path, logcap: Any) -> None:
        writer = DecisionWriter(maxsize=1)  # thread never started, so nothing drains
        writer._queue.put((db_path, make_decision()))
        writer.submit(make_decision(message="dropped"))
        assert any("queue full" in record.message for record in logcap.records)
        assert writer._queue.qsize() == 1


class TestWriterSeam:
    def test_install_wires_and_returns_writer(self, db_path: Path) -> None:
        assert decisions._WRITER is None
        writer = install()
        try:
            assert decisions._WRITER == writer.submit
            assert heartbeat._WRITER == writer.submit_heartbeat
            record_decision(stop_entry(), stop_evt(), HookResult(action=Action.warn, message="via-install"))
        finally:
            decisions._WRITER = None
            heartbeat._WRITER = None
            writer.drain(timeout=5)
        assert [row.message for row in rows(db_path)] == ["via-install"]

    def test_writer_none_uses_direct_append(self, db_path: Path) -> None:
        assert decisions._WRITER is None
        record_decision(stop_entry(), stop_evt(), HookResult(action=Action.warn, message="direct"))
        assert [row.message for row in rows(db_path)] == ["direct"]

    def test_writer_set_routes_and_skips_direct_append(self, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: list[Decision] = []
        monkeypatch.setattr(decisions, "_WRITER", captured.append)
        record_decision(stop_entry(), stop_evt(), HookResult(action=Action.warn, message="routed"))
        assert len(captured) == 1
        assert captured[0].message == "routed"
        assert not db_path.exists()
