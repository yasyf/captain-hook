"""Hook fires become rows in the cc-transcript decision ledger, the cross-tool source of truth for attribution."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING

from cc_transcript.decisions import Decision, DecisionLog
from cc_transcript.tools import FallbackCall, OtherCall, ToolInputError, parse_tool_call

from captain_hook.util import reqenv

if TYPE_CHECKING:
    from collections.abc import Callable

    from captain_hook.events import BaseHookEvent
    from captain_hook.types import HookResult, RegisteredHook

_WRITER: Callable[[Decision], None] | None = None
_CACHED_LOG: DecisionLog | None = None


def decisions_db_path() -> Path | None:
    return Path(p) if (p := reqenv.getenv("CAPT_HOOK_DECISIONS_DB")) else None


async def open_decision_log(path: Path | None) -> DecisionLog:
    return await DecisionLog.open(path)


async def _append(decision: Decision) -> None:
    # One handle per cold process, reused across successive asyncio.run bridges (the actor captures
    # the running loop per call); never closed per-call, so N firing hooks open one ledger, not N.
    global _CACHED_LOG
    if _CACHED_LOG is None:
        _CACHED_LOG = await open_decision_log(decisions_db_path())
    await _CACHED_LOG.append(decision)


def reset_cached_log() -> None:
    """Closes and drops the process-cached cold-path handle — tests call this for per-case isolation."""
    global _CACHED_LOG
    if _CACHED_LOG is not None:
        log, _CACHED_LOG = _CACHED_LOG, None
        asyncio.run(log.close())


def parse_degraded(evt: BaseHookEvent) -> bool:
    if isinstance(evt.input, FallbackCall):
        return True
    if not isinstance(evt.input, OtherCall) or not evt.tool_name:
        return False
    try:
        parse_tool_call(evt.tool_name, evt.input.raw)
    except ToolInputError:
        return True
    return False


def record_decision(entry: RegisteredHook, evt: BaseHookEvent, result: HookResult) -> None:
    """Append one ledger row for a fired hook. The single decision-write codepath; never raises into dispatch."""
    from captain_hook.types import Action

    if reqenv.getenv("CAPT_HOOK_SPAWNED") or not (session_id := evt._raw.get("session_id")):
        return
    action, message = (
        ("note", result.note) if result.action is Action.rewrite else (result.action.value, result.message)
    )
    decision = Decision(
        ts_ms=int(time.time() * 1000),
        session_id=session_id,
        source="captain-hook",
        kind=entry.name,
        source_file=entry.source_file,
        event=evt.event_name.name,
        action=action,
        tool_name=evt.tool_name,
        tool_digest=evt.tool_digest,
        message=message,
        detail={"degraded": True} if parse_degraded(evt) else {},
    )
    if _WRITER is not None:
        _WRITER(decision)
    else:
        asyncio.run(_append(decision))
