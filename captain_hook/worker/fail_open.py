"""Count the dispatches a session answered without some of its hooks, and say so out loud.

A hook whose transcript evidence comes back incomplete is skipped, and a dispatch whose own
evidence fails returns an empty success. Either way a guard did not run, and nothing in the
response shows it. The tally lives in the session directory so every worker shard serving the
session adds to one count, and a warning goes out at the configured count and again each time
the count doubles.
"""

from __future__ import annotations

from typing import Any

from cc_transcript.ids import SessionId
from filelock import Timeout
from loguru import logger
from pydantic import BaseModel

from captain_hook.dispatch import Envelope, format_output
from captain_hook.session import SessionSlot, ensure_session
from captain_hook.types import Action, Event, HookResult
from captain_hook.util import reqenv

WARN_AFTER_ENV = "CAPT_HOOK_FAIL_OPEN_WARN_AFTER"
DEFAULT_WARN_AFTER = 3
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
    count: int = 0
    warned_at: int = 0


def tally_fail_open(event: Event | None, session_id: str, cause: str) -> str | None:
    """Count one dispatch that ran without some of its hooks; return the warning when one is due.

    Runs on the failure path, so a tally it cannot take is logged and dropped rather than raised
    over the response. A warning due on an event whose output nobody reads, or on work finished
    after the reply (``event`` is None), stays due for the next dispatch that can carry it.
    """
    warn_after = int(reqenv.getenv(WARN_AFTER_ENV) or DEFAULT_WARN_AFTER)
    try:
        with SessionSlot(ensure_session(SessionId(session_id)), FailOpenTally).mutate(
            timeout=TALLY_LOCK_SECONDS
        ) as tally:
            tally.count += 1
            if event is None or event not in SURFACING_EVENTS or tally.count < max(warn_after, 2 * tally.warned_at):
                return None
            tally.warned_at = count = tally.count
    except (Timeout, OSError):
        logger.opt(exception=True).warning("fail-open tally skipped")
        return None
    return (
        f"capt-hook: {count} hook dispatches in this session ran without some or all of their hooks "
        f"because transcript evidence was incomplete (latest: {cause}). Those hooks' guards were not "
        f"enforced for those events. Run `capt-hook logs --session {session_id}` for the causes."
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
