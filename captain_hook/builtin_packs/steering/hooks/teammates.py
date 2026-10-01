from __future__ import annotations

from captain_hook import (
    Allow,
    Event,
    FromTeammate,
    Input,
    T,
    Warn,
    nudge,
)

nudge(
    "A teammate message re-reads and re-caches the whole main context. "
    "Return only a tight digest of final answers and decisions, and keep raw output in your own context.",
    only_if=[FromTeammate()],
    events=Event.SubagentStart,
    max_fires=None,
    skip_planning_agents=False,
    tests={
        Input(
            agent_type="general-purpose",
            transcript=[
                T.user("go research the API"),
                T.assistant(T.tool("Agent", prompt="dig in", subagent_type="general-purpose", name="researcher")),
            ],
        ): Warn(pattern="tight digest"),
        Input(
            agent_type="general-purpose",
            transcript=[
                T.user("go do the thing"),
                T.assistant(T.tool("Agent", prompt="dig in", subagent_type="general-purpose")),
            ],
        ): Allow(),
        Input(
            agent_type="general-purpose",
            transcript=[
                T.user("run both"),
                *T.tool_turn("Agent", prompt="dig in", subagent_type="general-purpose", name="researcher"),
                T.assistant(T.tool("Agent", prompt="next", subagent_type="general-purpose")),
            ],
        ): Allow(),
        Input(
            agent_type="general-purpose",
            transcript=[
                T.user("spin up a researcher"),
                T.assistant(T.tool("Agent", prompt="dig in", subagent_type="general-purpose", name="researcher")),
                T.user("now run the quick check"),
                T.assistant(T.tool("Agent", prompt="quick check", subagent_type="general-purpose")),
            ],
        ): Allow(),
        Input(agent_type="general-purpose"): Allow(),
    },
)
