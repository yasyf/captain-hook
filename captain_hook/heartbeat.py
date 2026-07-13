"""Unconditional per-(session, event) dispatch heartbeats — the wiring-gap signal.

A heartbeat is written at dispatch entry, before any hook matches, so a missing beat for an
event a session should emit means the event never reached capt-hook at all — a wiring gap
the decision ledger can never show, since it records only hooks that *fired*. Backed by
:class:`cc_transcript.heartbeats.HeartbeatLog`, sharing ``decisions.db`` with the ledger, and
written straight through (a single indexed upsert) rather than the decision writer's queue —
one beat per event is cheap enough to stay on the hot path. Like the ledger write, it never
raises into dispatch.
"""

from __future__ import annotations

import time
from functools import cache
from typing import TYPE_CHECKING

from cc_transcript.heartbeats import HeartbeatLog
from loguru import logger

from captain_hook.decisions import decisions_db_path
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from pathlib import Path

    from captain_hook.types import Event


@cache
def open_heartbeat_log(path: Path | None) -> HeartbeatLog:
    return HeartbeatLog.open(path)


def record_heartbeat(event: Event, raw: dict[str, object]) -> None:
    """Beat once for ``(session, event)`` at dispatch entry. The single heartbeat codepath; never raises."""
    if reqenv.getenv("CAPT_HOOK_SPAWNED") or not (session_id := raw.get("session_id")):
        return
    try:
        open_heartbeat_log(decisions_db_path()).beat(str(session_id), event.name, int(time.time() * 1000))
    except Exception:
        logger.opt(exception=True).warning("heartbeat write failed")
