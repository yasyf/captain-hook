from __future__ import annotations

from captain_hook import (
    Allow,
    Annotated,
    Block,
    Event,
    FromSubagent,
    FromTeammate,
    Input,
    Or,
    T,
    Tool,
    ToolInput,
    Warn,
    hook,
    nudge,
)

WAITING_SPAWN = r"(?i)\b(watch\w*|wait\w*|until|sleep\w*|timers?|poll\w*|pr-watcher|slack-waiter)\b"

nudge(
    "A teammate message re-reads and re-caches the whole main context. "
    "Return only a tight digest of final answers and decisions, and keep raw output in your own context.",
    only_if=[FromTeammate()],
    events=Event.SubagentStart,
    max_fires=1,
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

hook(
    Event.PreToolUse,
    only_if=[
        Tool("Agent|Task"),
        FromSubagent(),
        Or(
            ToolInput("prompt", WAITING_SPAWN),
            ToolInput("subagent_type", WAITING_SPAWN),
        ),
    ],
    skip_if=[Annotated("raw")],
    message=(
        "A subagent that waits holds a teammate's turn open, so you stop hearing messages until it returns. "
        "End your turn and let team-lead's watcher wake you."
    ),
    block=True,
    tests={
        Input(
            tool="Agent",
            tool_input={"prompt": "watch PR 1 until green", "subagent_type": "general-purpose"},
            agent_id="tm1",
        ): Block(pattern="End your turn"),
        Input(
            tool="Agent",
            tool_input={"prompt": "pr: 1", "subagent_type": "open-pr:pr-watcher", "run_in_background": True},
            agent_id="tm1",
        ): Block(pattern="End your turn"),
        Input(
            tool="Agent",
            tool_input={"prompt": "thread: C1/p2", "subagent_type": "cc-slack:slack-waiter"},
            agent_id="tm1",
        ): Block(),
        Input(
            tool="Agent",
            tool_input={"prompt": "Sleep until 01:02Z, then SendMessage the lane", "subagent_type": "general-purpose"},
            agent_id="tm1",
        ): Block(),
        Input(
            tool="Agent",
            tool_input={"prompt": "watch PR 1 until green", "subagent_type": "general-purpose"},
        ): Allow(),
        Input(
            tool="Agent",
            tool_input={"prompt": "find every caller of X", "subagent_type": "general-purpose"},
            agent_id="tm1",
        ): Allow(),
        Input(
            tool="Agent",
            tool_input={"prompt": "ccx: raw\nwatch PR 1 until green", "subagent_type": "general-purpose"},
            agent_id="tm1",
        ): Allow(),
    },
)
