from __future__ import annotations

from typing import TYPE_CHECKING

from captain_hook import Event, on
from captain_hook.tickets.orca import record_terminal

if TYPE_CHECKING:
    from captain_hook import BaseHookEvent, HookResult


@on(
    Event.SessionStart | Event.UserPromptSubmit | Event.PreToolUse,
    respect_gitignore=False,
    skip_planning_agents=False,
)
def record_orca_terminal(evt: BaseHookEvent) -> HookResult | None:
    record_terminal(evt)
    return None
