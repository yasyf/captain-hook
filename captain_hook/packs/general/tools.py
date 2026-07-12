from __future__ import annotations

from captain_hook import (
    Allow,
    BaseHookEvent,
    CustomCondition,
    Event,
    FromSubagent,
    Input,
    Prompt,
    Warn,
    nudge,
)
from captain_hook.events import SessionStartEvent


class FreshSession(CustomCondition):
    """True when a SessionStart fires from a fresh ``startup`` or ``clear``, not a ``resume`` or ``compact``."""

    def check(self, evt: BaseHookEvent) -> bool:
        return isinstance(evt, SessionStartEvent) and evt.source in ("startup", "clear")


nudge(
    str(Prompt.load("tools/preload_nudge")),
    only_if=[FreshSession()],
    skip_if=[FromSubagent()],
    events=Event.SessionStart,
    max_fires=1,
    tests={
        Input(source="startup"): Warn(pattern="select:TaskCreate"),
        Input(source="clear"): Warn(pattern="select:TaskCreate"),
        Input(source="resume"): Allow(),
        Input(source="compact"): Allow(),
        Input(source="startup", agent_id="sub-1"): Allow(),
        Input(source="clear", agent_id="sub-1"): Allow(),
    },
)
