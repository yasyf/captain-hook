"""Count the dispatches a session answered without running its hooks, and say so out loud.

A dispatch whose transcript evidence comes back incomplete fails open: Claude Code gets an empty,
successful response and no hook runs. One is harmless; a run of them means every guard in the
session is off, and nothing in the response shows it. The tally lives in the session directory so
every worker shard serving the session adds to one count, and a warning goes out at the configured
count and again each time the count doubles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cc_transcript.ids import SessionId
from pydantic import BaseModel

from captain_hook.dispatch import format_output
from captain_hook.session import SessionSlot, ensure_session
from captain_hook.types import Action, Event, HookResult
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from captain_hook.snapshots.client import EvidenceIncomplete

WARN_AFTER_ENV = "CAPT_HOOK_FAIL_OPEN_WARN_AFTER"
DEFAULT_WARN_AFTER = 3


class FailOpenTally(BaseModel):
    count: int = 0
    warned_at: int = 0


def tally_fail_open(event: Event, session_id: str, exc: EvidenceIncomplete) -> dict[str, object] | None:
    """Count one fail-open for the session; return the envelope to send when a warning is due."""
    warn_after = int(reqenv.getenv(WARN_AFTER_ENV) or DEFAULT_WARN_AFTER)
    with SessionSlot(ensure_session(SessionId(session_id)), FailOpenTally).mutate() as tally:
        tally.count += 1
        if event is Event.PreCompact or tally.count < max(warn_after, 2 * tally.warned_at):
            return None
        tally.warned_at = count = tally.count
    message = (
        f"capt-hook: {count} hook dispatches in this session failed open, so none of their hooks ran "
        f"(latest: {exc.status}: {exc.reason}). Guards such as Stop gates and permission checks are "
        f"not being enforced. Run `capt-hook logs --session {session_id}` for the causes."
    )
    return fail_open_envelope(event, message)


def fail_open_envelope(event: Event, message: str) -> dict[str, object]:
    context = format_output(event, HookResult(action=Action.warn, message=message, approve=False))
    if event in (Event.Stop | Event.SubagentStop) or not isinstance(context, dict):
        context = {}
    return context | {"systemMessage": message}
