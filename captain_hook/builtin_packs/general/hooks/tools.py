from __future__ import annotations

from captain_hook import (
    Allow,
    Event,
    FreshSession,
    FromSubagent,
    Input,
    Prompt,
    Warn,
    nudge,
)

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
