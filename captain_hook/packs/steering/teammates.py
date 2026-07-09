from __future__ import annotations

from captain_hook import (
    Allow,
    BaseHookEvent,
    CustomCondition,
    Event,
    Input,
    TaskCall,
    Warn,
    nudge,
)


class FromTeammate(CustomCondition):
    """True when the current turn's in-flight subagent spawn is a named teammate.

    A teammate spawn carries a ``name`` in its Agent/Task input, which cc-transcript
    surfaces as ``TaskCall.agent_name``; a plain subagent leaves it unset. Scoped to
    the current turn so a teammate still running from an earlier turn never re-fires
    against a later unnamed spawn.
    """

    def check(self, evt: BaseHookEvent) -> bool:
        return any(
            isinstance(use.call, TaskCall) and bool(use.call.agent_name)
            for use in evt.ctx.t.current_turn.tool_calls.named("Task")
            if use.result is None
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
                {"type": "user", "message": {"content": [{"type": "text", "text": "go research the API"}]}},
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Agent",
                                "input": {"prompt": "dig in", "subagent_type": "general-purpose", "name": "researcher"},
                                "id": "t1",
                            }
                        ]
                    },
                },
            ],
        ): Warn(pattern="tight digests"),
        Input(
            agent_type="general-purpose",
            transcript=[
                {"type": "user", "message": {"content": [{"type": "text", "text": "go do the thing"}]}},
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Agent",
                                "input": {"prompt": "dig in", "subagent_type": "general-purpose"},
                                "id": "t1",
                            }
                        ]
                    },
                },
            ],
        ): Allow(),
        Input(
            agent_type="general-purpose",
            transcript=[
                {"type": "user", "message": {"content": [{"type": "text", "text": "run both"}]}},
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Agent",
                                "input": {"prompt": "dig in", "subagent_type": "general-purpose", "name": "researcher"},
                                "id": "t1",
                            }
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Agent",
                                "input": {"prompt": "next", "subagent_type": "general-purpose"},
                                "id": "t2",
                            }
                        ]
                    },
                },
            ],
        ): Allow(),
        Input(
            agent_type="general-purpose",
            transcript=[
                {"type": "user", "message": {"content": [{"type": "text", "text": "spin up a researcher"}]}},
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Agent",
                                "input": {"prompt": "dig in", "subagent_type": "general-purpose", "name": "researcher"},
                                "id": "t1",
                            }
                        ]
                    },
                },
                {"type": "user", "message": {"content": [{"type": "text", "text": "now run the quick check"}]}},
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Agent",
                                "input": {"prompt": "quick check", "subagent_type": "general-purpose"},
                                "id": "t2",
                            }
                        ]
                    },
                },
            ],
        ): Allow(),
        Input(agent_type="general-purpose"): Allow(),
    },
)
