from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from captain_hook.conditions import check_condition
from captain_hook.events import BaseHookEvent, PreToolUseEvent, SubagentStopEvent
from captain_hook.transcripts import load_transcript
from captain_hook.types import Agent, Or, Tool
from tests.helpers import (
    build_ctx,
    make_event,
    make_pre_tool_event,
    raw_assistant,
    raw_text,
    raw_tool_result,
    raw_tool_use,
)


def dispatched(subagent_type: str) -> BaseHookEvent:
    return make_pre_tool_event("Agent", {"subagent_type": subagent_type, "prompt": "go"})


def running(agent_type: str) -> BaseHookEvent:
    return make_event(SubagentStopEvent, raw={"agent_type": agent_type})


class TestAgentPolymorphicMatch:
    @pytest.mark.parametrize(
        ("pattern", "evt", "expected"),
        [
            pytest.param("test-runner", dispatched("test-runner"), True, id="matches_dispatched_subagent_type"),
            pytest.param("test-runner", running("test-runner"), True, id="matches_running_agent_type"),
            pytest.param("other", dispatched("test-runner"), False, id="rejects_other_dispatched"),
            pytest.param("other", running("test-runner"), False, id="rejects_other_running"),
            pytest.param(
                "test-runner|test-writer", dispatched("test-runner"), True, id="pipe_alternation_matches_first"
            ),
            pytest.param(
                "test-runner|test-writer", dispatched("test-writer"), True, id="pipe_alternation_matches_second"
            ),
            pytest.param("test-runner|test-writer", running("test-writer"), True, id="pipe_alternation_running"),
        ],
    )
    def test_agent(self, pattern: str, evt: BaseHookEvent, expected: bool) -> None:
        assert check_condition(Agent(pattern), evt) is expected


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

        ctx = build_ctx(transcript=load_transcript(session_file))
        evt = make_event(PreToolUseEvent, raw={"tool_name": "Bash", "tool_input": {"command": "echo"}}, ctx=ctx)

        subagents = evt.ctx.turn.subagents.with_type("test-runner")
        assert len(subagents) == 1
        subagent = subagents[0]
        assert subagent.id == "tu_test_runner_1"
        assert subagent.type == "test-runner"
        assert subagent.tool_calls.failed().count() == 3
        assert subagent.failed is True


class TestOrCombinator:
    @pytest.mark.parametrize(
        ("evt", "expected"),
        [
            pytest.param(make_pre_tool_event("Bash", {"command": "echo"}), True, id="matches_first_arm"),
            pytest.param(dispatched("test-runner"), True, id="matches_second_arm"),
            pytest.param(
                make_pre_tool_event("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""}),
                False,
                id="rejects_when_neither_matches",
            ),
        ],
    )
    def test_or(self, evt: BaseHookEvent, expected: bool) -> None:
        assert check_condition(Or(Tool("Bash"), Agent("test-runner")), evt) is expected
