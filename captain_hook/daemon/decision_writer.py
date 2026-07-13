"""Serialized decision-ledger writer for the resident daemon.

sqlite connections are single-thread (``check_same_thread``), and many event threads record
decisions concurrently. This routes every :class:`~cc_transcript.decisions.Decision` through one
queue to a dedicated writer thread that owns all :class:`~cc_transcript.decisions.DecisionLog`
handles. :func:`captain_hook.decisions.record_decision` reaches it through its ``_WRITER`` seam;
cold, ``_WRITER`` is ``None`` and the direct append runs unchanged. The db path is resolved at submit
time inside the request scope (the writer thread has no bound request), so each decision carries its
own target ledger. A full queue drops the row with a warning rather than blocking dispatch.
"""

from __future__ import annotations

import queue
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING

from loguru import logger

from captain_hook import decisions

if TYPE_CHECKING:
    from pathlib import Path

    from cc_transcript.decisions import Decision, DecisionLog

MAX_QUEUE = 10_000
MAX_DECISION_LOGS = 64


class Stop:
    __slots__ = ()


STOP = Stop()


class DecisionWriter:
    def __init__(self, *, maxsize: int = MAX_QUEUE) -> None:
        self._queue: queue.Queue[tuple[Path | None, Decision] | Stop] = queue.Queue(maxsize)
        self._logs: OrderedDict[Path | None, DecisionLog] = OrderedDict()
        self._thread = threading.Thread(target=self._run, name="capt-hook-decision-writer", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, decision: Decision) -> None:
        try:
            self._queue.put_nowait((decisions.decisions_db_path(), decision))
        except queue.Full:
            logger.bind(session_id=decision.session_id).warning("decision queue full; dropping a ledger row")

    def drain(self, timeout: float | None = None) -> None:
        self._queue.put(STOP)
        self._thread.join(timeout)

    def _run(self) -> None:
        while not isinstance(item := self._queue.get(), Stop):
            path, decision = item
            try:
                self._log(path).append(decision)
            except Exception:
                logger.opt(exception=True).warning("decision write failed")
        for log in self._logs.values():
            log.conn.close()

    def _log(self, path: Path | None) -> DecisionLog:
        # LRU-bound the open handles (like SessionFileRouter) so a peer varying the decisions-db path
        # cannot exhaust fds; the writer thread owns this dict, so no lock is needed.
        from cc_transcript.decisions import DecisionLog

        if (log := self._logs.get(path)) is not None:
            self._logs.move_to_end(path)
            return log
        self._logs[path] = log = DecisionLog.open(path)
        while len(self._logs) > MAX_DECISION_LOGS:
            self._logs.popitem(last=False)[1].conn.close()
        return log


def install() -> DecisionWriter:
    writer = DecisionWriter()
    writer.start()
    decisions._WRITER = writer.submit
    return writer
