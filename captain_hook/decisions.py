"""Hook fires become rows in the cc-transcript decision ledger, the cross-tool source of truth for attribution."""

from __future__ import annotations

import os
import time
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from cc_transcript.decisions import Decision, DecisionLog
from cc_transcript.tools import OtherCall, ToolInputError, parse_tool_call

if TYPE_CHECKING:
    from captain_hook.events import BaseHookEvent
    from captain_hook.types import HookResult, RegisteredHook


def decisions_db_path() -> Path | None:
    return Path(p) if (p := os.environ.get("CAPT_HOOK_DECISIONS_DB")) else None


@cache
def open_decision_log(path: Path | None) -> DecisionLog:
    return DecisionLog.open(path)


def parse_degraded(evt: BaseHookEvent) -> bool:
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

    if os.environ.get("CAPT_HOOK_SPAWNED") or not (session_id := evt._raw.get("session_id")):
        return
    action, message = ("note", result.note) if result.action is Action.rewrite else (result.action.value, result.message)
    open_decision_log(decisions_db_path()).append(
        Decision(
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
    )
