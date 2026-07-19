"""Unconditional per-(session, event) dispatch heartbeats — the wiring-gap signal.

A heartbeat is written at dispatch entry, before any hook matches, so a missing beat for an
event a session should emit means the event never reached capt-hook at all — a wiring gap
the decision ledger can never show, since it records only hooks that *fired*. Backed by
:class:`cc_transcript.heartbeats.HeartbeatLog`, sharing ``decisions.db`` with the ledger. The
cold hook process beats through its own ``asyncio.run``; the resident daemon routes the beat
through the decision writer's one persistent ledger loop via the ``_WRITER`` seam, so a single
thread owns every ledger handle. Like the ledger write, it never raises into dispatch.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from cc_transcript.heartbeats import HeartbeatLog
from loguru import logger

from captain_hook.decisions import decisions_db_path
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from captain_hook.types import Event

_WRITER: Callable[[str, str, int], None] | None = None
_CACHED_LOG: HeartbeatLog | None = None


async def open_heartbeat_log(path: Path | None) -> HeartbeatLog:
    return await HeartbeatLog.open(path)


async def _beat(session_id: str, event: str, ts_ms: int) -> None:
    # One handle per cold process, reused across successive asyncio.run bridges; never closed
    # per-call, so a session's per-event beats open one ledger rather than one actor per beat.
    global _CACHED_LOG
    if _CACHED_LOG is None:
        _CACHED_LOG = await open_heartbeat_log(decisions_db_path())
    await _CACHED_LOG.beat(session_id, event, ts_ms)


def reset_cached_log() -> None:
    """Closes and drops the process-cached cold-path handle — tests call this for per-case isolation."""
    global _CACHED_LOG
    if _CACHED_LOG is not None:
        log, _CACHED_LOG = _CACHED_LOG, None
        asyncio.run(log.close())


def record_heartbeat(event: Event, raw: dict[str, object]) -> None:
    """Beat once for ``(session, event)`` at dispatch entry. The single heartbeat codepath; never raises."""
    if reqenv.getenv("CAPT_HOOK_SPAWNED") or not (session_id := raw.get("session_id")):
        return
    ts_ms = int(time.time() * 1000)
    try:
        if _WRITER is not None:
            _WRITER(str(session_id), event.name, ts_ms)
        else:
            asyncio.run(_beat(str(session_id), event.name, ts_ms))
    except Exception:
        logger.opt(exception=True).warning("heartbeat write failed")
