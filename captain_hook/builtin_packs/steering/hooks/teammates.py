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
    """You are a teammate in a shared session: every message you send re-reads and
re-caches the entire main context. Return tight digests — final answers and
decisions only. Keep raw file contents, logs, command output, and intermediate
findings in your own context; if they matter, summarize them in a few sentences.""",
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
        ): Warn(pattern="tight digests"),
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
