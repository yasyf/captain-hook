"""Name each hook a session ran without, once, out loud.

A hook whose transcript evidence comes back incomplete is skipped, and a dispatch whose own
evidence fails returns an empty success. Either way a guard did not run, and nothing in the
response shows it. The record lives in the session directory so every worker shard serving the
session shares it: the first skip of each hook is logged and named in one visible line, and
later skips of that hook stay quiet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from cc_transcript.ids import SessionId
from filelock import Timeout
from loguru import logger
from pydantic import BaseModel

from captain_hook.dispatch import Envelope, format_output
from captain_hook.session import SessionSlot, ensure_session
from captain_hook.types import Action, Event, HookResult

if TYPE_CHECKING:
    from collections.abc import Sequence

ALL_HOOKS = "all hooks"
TALLY_LOCK_SECONDS = 0.5
SURFACING_EVENTS = (
    Event.SessionStart
    | Event.UserPromptSubmit
    | Event.PreToolUse
    | Event.PostToolUse
    | Event.PostToolUseFailure
    | Event.Stop
    | Event.SubagentStop
)


class FailOpenTally(BaseModel):
    reported: list[str] = []
    pending: dict[str, str] = {}


def tally_fail_open(event: Event | None, session_id: str, gaps: Sequence[str]) -> str | None:
    """Record the hooks one dispatch ran without; return the line naming the newly skipped ones.

    Each gap reads ``"<hook>: <status>: <reason>"``. Runs on the failure path, so a record it cannot
    take is logged and dropped rather than raised over the response. A line due on an event whose
    output nobody reads, or on work finished after the reply (``event`` is None), stays due for the
    next dispatch that can carry it.
    """
    try:
        with SessionSlot(ensure_session(SessionId(session_id)), FailOpenTally).mutate(
            timeout=TALLY_LOCK_SECONDS
        ) as tally:
            for gap in gaps:
                hook, _, cause = gap.partition(": ")
                if hook not in tally.reported and hook not in tally.pending:
                    logger.bind(hook=hook, cause=cause).warning("hook skipped: evidence incomplete")
                    tally.pending[hook] = cause
            if event is None or event not in SURFACING_EVENTS or not tally.pending:
                return None
            due = tally.pending
            tally.reported.extend(due)
            tally.pending = {}
    except (Timeout, OSError):
        logger.opt(exception=True).warning("fail-open tally skipped")
        return None
    skipped = "; ".join(f"{hook} ({cause})" for hook, cause in due.items())
    return (
        f"capt-hook: skipped {skipped} because transcript evidence was incomplete, so those guards were "
        f"not enforced. Each hook is named once per session; `capt-hook logs --session {session_id}` "
        "lists every skip."
    )


def fail_open_envelope(event: Event, message: str) -> dict[str, Any]:
    context = format_output(event, HookResult(action=Action.warn, message=message, approve=False))
    if event in (Event.Stop | Event.SubagentStop) or not isinstance(context, dict):
        context = {}
    return context | {"systemMessage": message}


def with_warning(event: Event, output: Envelope | None, message: str) -> Envelope:
    if not isinstance(output, dict):
        return fail_open_envelope(event, message)
    notice = "\n\n".join(part for part in (output.get("systemMessage"), message) if part)
    return output | {"systemMessage": notice}
