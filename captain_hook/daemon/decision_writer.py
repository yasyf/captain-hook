"""Serialized ledger writer for the resident daemon.

The async ledgers own a single connection actor each, and many event threads record decisions and
heartbeats concurrently. This routes every :class:`~cc_transcript.decisions.Decision` and every
dispatch heartbeat through one queue to a dedicated writer thread that runs one persistent
``asyncio.run(self._arun())`` owning all :class:`~cc_transcript.decisions.DecisionLog` and
:class:`~cc_transcript.heartbeats.HeartbeatLog` handles — opened once, awaited per queue item.
:func:`captain_hook.decisions.record_decision` and :func:`captain_hook.heartbeat.record_heartbeat`
reach it through their ``_WRITER`` seams; cold, both are ``None`` and each bridges its own
``asyncio.run`` unchanged. The db path is resolved at submit time inside the request scope (the
writer thread has no bound request), so each item carries its own target ledger. A full queue drops
the row with a warning rather than blocking dispatch.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING, NamedTuple

from loguru import logger

from captain_hook import decisions, heartbeat

if TYPE_CHECKING:
    from pathlib import Path

    from cc_transcript.decisions import Decision, DecisionLog
    from cc_transcript.heartbeats import HeartbeatLog

MAX_QUEUE = 10_000
MAX_LEDGER_LOGS = 64


class Beat(NamedTuple):
    path: Path | None
    session_id: str
    event: str
    ts_ms: int


class Stop:
    __slots__ = ()


STOP = Stop()


class DecisionWriter:
    def __init__(self, *, maxsize: int = MAX_QUEUE) -> None:
        self._queue: queue.Queue[tuple[Path | None, Decision] | Beat | Stop] = queue.Queue(maxsize)
        self._logs: OrderedDict[Path | None, DecisionLog] = OrderedDict()
        self._heartbeats: OrderedDict[Path | None, HeartbeatLog] = OrderedDict()
        self._thread = threading.Thread(target=self._run, name="capt-hook-decision-writer", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, decision: Decision) -> None:
        try:
            self._queue.put_nowait((decisions.decisions_db_path(), decision))
        except queue.Full:
            logger.bind(session_id=decision.session_id).warning("decision queue full; dropping a ledger row")

    def submit_heartbeat(self, session_id: str, event: str, ts_ms: int) -> None:
        try:
            self._queue.put_nowait(Beat(decisions.decisions_db_path(), session_id, event, ts_ms))
        except queue.Full:
            logger.bind(session_id=session_id).warning("heartbeat queue full; dropping a beat")

    def drain(self, timeout: float | None = None) -> None:
        self._queue.put(STOP)
        self._thread.join(timeout)

    def _run(self) -> None:
        asyncio.run(self._arun())

    async def _arun(self) -> None:
        try:
            while not isinstance(item := self._queue.get(), Stop):
                try:
                    match item:
                        case Beat(path, session_id, event, ts_ms):
                            await (await self._heartbeat(path)).beat(session_id, event, ts_ms)
                        case (path, decision):
                            await (await self._log(path)).append(decision)
                except Exception:
                    logger.opt(exception=True).warning("ledger write failed")
        finally:
            for log in self._logs.values():
                await log.close()
            for beat_log in self._heartbeats.values():
                await beat_log.close()

    async def _log(self, path: Path | None) -> DecisionLog:
        # LRU-bound the open handles (like SessionFileRouter) so a peer varying the decisions-db path
        # cannot exhaust fds; the writer thread owns this dict, so no lock is needed.
        from cc_transcript.decisions import DecisionLog

        if (log := self._logs.get(path)) is not None:
            self._logs.move_to_end(path)
            return log
        self._logs[path] = log = await DecisionLog.open(path)
        while len(self._logs) > MAX_LEDGER_LOGS:
            await self._logs.popitem(last=False)[1].close()
        return log

    async def _heartbeat(self, path: Path | None) -> HeartbeatLog:
        from cc_transcript.heartbeats import HeartbeatLog

        if (log := self._heartbeats.get(path)) is not None:
            self._heartbeats.move_to_end(path)
            return log
        self._heartbeats[path] = log = await HeartbeatLog.open(path)
        while len(self._heartbeats) > MAX_LEDGER_LOGS:
            await self._heartbeats.popitem(last=False)[1].close()
        return log


def install() -> DecisionWriter:
    writer = DecisionWriter()
    writer.start()
    decisions._WRITER = writer.submit
    heartbeat._WRITER = writer.submit_heartbeat
    return writer
