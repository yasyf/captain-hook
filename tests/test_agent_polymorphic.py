from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from captain_hook.conditions import check_condition
from captain_hook.events import PreToolUseEvent, SubagentStopEvent
from captain_hook.tests.helpers import (
    build_ctx,
    make_event,
    make_pre_tool_event,
    raw_assistant,
    raw_text,
    raw_tool_result,
    raw_tool_use,
)
from captain_hook.transcript import Transcript
from captain_hook.types import Agent, Or, Tool


class TestAgentPolymorphicMatch:
    def test_agent_matches_dispatched_subagent_type(self) -> None:
        evt = make_pre_tool_event("Agent", {"subagent_type": "test-runner", "prompt": "go"})
        assert check_condition(Agent("test-runner"), evt) is True

    def test_agent_matches_running_agent_type(self) -> None:
        evt = make_event(SubagentStopEvent, raw={"agent_type": "test-runner"})
        assert check_condition(Agent("test-runner"), evt) is True

    def test_agent_rejects_other_dispatched(self) -> None:
        evt = make_pre_tool_event("Agent", {"subagent_type": "test-runner", "prompt": "go"})
        assert check_condition(Agent("other"), evt) is False

    def test_agent_rejects_other_running(self) -> None:
        evt = make_event(SubagentStopEvent, raw={"agent_type": "test-runner"})
        assert check_condition(Agent("other"), evt) is False

    def test_agent_pipe_alternation_matches_first(self) -> None:
        evt = make_pre_tool_event("Agent", {"subagent_type": "test-runner", "prompt": "go"})
        assert check_condition(Agent("test-runner|test-writer"), evt) is True

    def test_agent_pipe_alternation_matches_second(self) -> None:
        evt = make_pre_tool_event("Agent", {"subagent_type": "test-writer", "prompt": "go"})
        assert check_condition(Agent("test-runner|test-writer"), evt) is True

    def test_agent_pipe_alternation_running(self) -> None:
        evt = make_event(SubagentStopEvent, raw={"agent_type": "test-writer"})
        assert check_condition(Agent("test-runner|test-writer"), evt) is True


class TestTurnSubagentAccessor:
    def test_with_type_returns_one_accessor(self, tmp_path: Path) -> None:
        session_dir = tmp_path
        session_file = session_dir / "session.jsonl"
        subagents_dir = session_dir / "session" / "subagents"
        subagents_dir.mkdir(parents=True)

        parent_msgs = [
            raw_text("user", "go"),
            raw_assistant(
                raw_tool_use("Agent", {"subagent_type": "test-runner", "prompt": "run tests"}, "tu_test_runner_1")
            ),
        ]
        session_file.write_text("\n".join(json.dumps(m) for m in parent_msgs) + "\n")

        sub_msgs: list[dict[str, Any]] = []
        for i in range(3):
            tu_id = f"sub_tu_{i}"
            sub_msgs.append(raw_assistant(raw_tool_use("Bash", {"command": f"echo {i}"}, tu_id)))
            sub_msgs.append(raw_tool_result(tu_id, "boom", is_error=True))

        sub_jsonl = subagents_dir / "agent-tu_test_runner_1.jsonl"
        sub_jsonl.write_text("\n".join(json.dumps(m) for m in sub_msgs) + "\n")

        ctx = build_ctx(transcript=Transcript.from_path(session_file))
        evt = make_event(PreToolUseEvent, raw={"tool_name": "Bash", "tool_input": {"command": "echo"}}, ctx=ctx)

        accessors = evt.ctx.turn.subagents.with_type("test-runner")
        assert len(accessors) == 1
        accessor = accessors[0]
        assert accessor.id == "tu_test_runner_1"
        assert accessor.type == "test-runner"
        assert len(list(accessor.tool_uses.with_errors)) == 3
        assert accessor.failed is True


class TestOrCombinator:
    def test_or_matches_first_arm(self) -> None:
        evt = make_pre_tool_event("Bash", {"command": "echo"})
        assert check_condition(Or(Tool("Bash"), Agent("test-runner")), evt) is True

    def test_or_matches_second_arm(self) -> None:
        evt = make_pre_tool_event("Agent", {"subagent_type": "test-runner", "prompt": "go"})
        assert check_condition(Or(Tool("Bash"), Agent("test-runner")), evt) is True

    def test_or_rejects_when_neither_matches(self) -> None:
        evt = make_pre_tool_event("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""})
        assert check_condition(Or(Tool("Bash"), Agent("test-runner")), evt) is False
