from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from captain_hook.app import _state, hook as register_hook, on, reset
from captain_hook.events import (
    PreToolUseEvent,
    StopEvent,
    SubagentStartEvent,
    SubagentStopEvent,
    UserPromptSubmitEvent,
)
from captain_hook.transcript import Transcript
from captain_hook.types import Action, Event, HookResult, Tool


class TestInput:
    def test_input_frozen(self):
        from captain_hook.testing.types import Input

        i = Input(command="ls")
        with pytest.raises(FrozenInstanceError):
            i.command = "other"  # type: ignore[misc]

    def test_input_all_fields_default_none(self):
        from captain_hook.testing.types import Input

        i = Input()
        assert i.command is None
        assert i.file is None
        assert i.content is None
        assert i.old is None
        assert i.tool is None
        assert i.prompt is None
        assert i.agent_type is None
        assert i.transcript is None

    def test_input_with_all_fields(self):
        from captain_hook.testing.types import Input

        i = Input(
            command="ls",
            file="test.py",
            content="hello",
            old="world",
            tool="Bash",
            prompt="do it",
            agent_type="worker",
            transcript=Path("/tmp/test.jsonl"),
        )
        assert i.command == "ls"
        assert i.file == "test.py"
        assert i.content == "hello"
        assert i.old == "world"
        assert i.tool == "Bash"
        assert i.prompt == "do it"
        assert i.agent_type == "worker"
        assert i.transcript == Path("/tmp/test.jsonl")

    def test_input_transcript_list_wraps_in_fixture(self):
        from captain_hook.testing.types import Input, TranscriptFixture

        msgs = [{"type": "user", "message": {"content": "hi"}}]
        i = Input(transcript=msgs)
        assert isinstance(i.transcript, TranscriptFixture)
        assert i.transcript.messages == msgs

    def test_input_transcript_path(self):
        from captain_hook.testing.types import Input

        p = Path("/tmp/test.jsonl")
        i = Input(transcript=p)
        assert i.transcript == p


class TestTranscriptFixture:
    def test_wraps_message_list(self):
        from captain_hook.testing.types import TranscriptFixture

        msgs = [{"type": "user", "message": {"content": "hello"}}]
        tf = TranscriptFixture(messages=msgs)
        assert tf.messages == msgs

    def test_repr(self):
        from captain_hook.testing.types import TranscriptFixture

        tf = TranscriptFixture(messages=[])
        assert "TranscriptFixture" in repr(tf)


class TestTTest:
    def test_ttest_dict_type(self):
        from captain_hook.testing.types import Allow, Block, Input, TTest, Warn

        t: TTest = {
            Input(command="ls"): Allow(),
            Input(command="rm -rf /"): Block(pattern="danger"),
            "session-uuid": Warn(),
        }
        assert len(t) == 3


class TestMockEvent:
    def test_mock_event_bash_creates_correct_tool_input(self):
        from captain_hook.tests.helpers import mock_event

        evt = mock_event("PreToolUse", tool="Bash", command="ls -la")
        assert evt.tool_name == "Bash"
        assert evt._raw["tool_input"]["command"] == "ls -la"

    def test_mock_event_edit_creates_correct_tool_input(self):
        from captain_hook.tests.helpers import mock_event

        evt = mock_event("PreToolUse", tool="Edit", file="test.py", content="new", old="old")
        assert evt.tool_name == "Edit"
        ti = evt._raw["tool_input"]
        assert ti["file_path"] == "test.py"
        assert ti["old_string"] == "old"
        assert ti["new_string"] == "new"

    def test_mock_event_write_creates_correct_tool_input(self):
        from captain_hook.tests.helpers import mock_event

        evt = mock_event("PreToolUse", tool="Write", file="test.py", content="content")
        assert evt.tool_name == "Write"
        ti = evt._raw["tool_input"]
        assert ti["file_path"] == "test.py"
        assert ti["content"] == "content"

    def test_mock_event_read_creates_correct_tool_input(self):
        from captain_hook.tests.helpers import mock_event

        evt = mock_event("PreToolUse", tool="Read", file="test.py")
        assert evt.tool_name == "Read"
        assert evt._raw["tool_input"]["file_path"] == "test.py"

    def test_mock_event_agent_creates_correct_tool_input(self):
        from captain_hook.tests.helpers import mock_event

        evt = mock_event("SubagentStop", agent_type="worker")
        assert evt._raw.get("agent_type") == "worker"

    def test_mock_event_stop(self):
        from captain_hook.tests.helpers import mock_event

        evt = mock_event("Stop")
        assert isinstance(evt, StopEvent)

    def test_mock_event_subagent_stop(self):
        from captain_hook.tests.helpers import mock_event

        evt = mock_event("SubagentStop", agent_type="cleanup")
        assert isinstance(evt, SubagentStopEvent)
        assert evt._raw["agent_type"] == "cleanup"

    def test_mock_event_subagent_start(self):
        from captain_hook.tests.helpers import mock_event

        evt = mock_event("SubagentStart", agent_type="worker")
        assert isinstance(evt, SubagentStartEvent)

    def test_mock_event_user_prompt(self):
        from captain_hook.tests.helpers import mock_event

        evt = mock_event("UserPromptSubmit", prompt="do this")
        assert isinstance(evt, UserPromptSubmitEvent)
        assert evt._raw["prompt"] == "do this"

    def test_mock_event_with_transcript(self):
        from captain_hook.tests.helpers import mock_event

        transcript = Transcript.from_messages(
            [
                {"type": "user", "message": {"content": "hello"}},
            ]
        )
        evt = mock_event("PreToolUse", tool="Bash", command="ls", transcript=transcript)
        assert evt.ctx.transcript is transcript

    def test_mock_event_accepts_event_enum(self):
        from captain_hook.tests.helpers import mock_event

        evt = mock_event(Event.PreToolUse, tool="Bash", command="ls")
        assert isinstance(evt, PreToolUseEvent)

    def test_mock_event_defaults_to_bash(self):
        from captain_hook.tests.helpers import mock_event

        evt = mock_event("PreToolUse")
        assert evt.tool_name == "Bash"


class TestInputToEvent:
    def test_input_to_event_basic(self):
        from captain_hook.tests.helpers import input_to_event
        from captain_hook.testing.types import Input

        inp = Input(tool="Bash", command="ls")
        evt = input_to_event(Event.PreToolUse, inp)
        assert isinstance(evt, PreToolUseEvent)
        assert evt._raw["tool_input"]["command"] == "ls"

    def test_input_to_event_with_transcript_fixture(self):
        from captain_hook.tests.helpers import input_to_event
        from captain_hook.testing.types import Input, TranscriptFixture

        tf = TranscriptFixture(
            messages=[
                {"type": "user", "message": {"content": "hello"}},
            ]
        )
        inp = Input(tool="Bash", command="ls", transcript=tf)
        evt = input_to_event(Event.PreToolUse, inp)
        assert len(evt.ctx.transcript) == 1

    def test_input_to_event_with_path(self, tmp_path: Path):
        from captain_hook.tests.helpers import input_to_event
        from captain_hook.testing.types import Input

        p = tmp_path / "test.jsonl"
        p.write_text('{"type": "user", "message": {"content": "hi"}}\n')
        inp = Input(tool="Bash", command="ls", transcript=p)
        evt = input_to_event(Event.PreToolUse, inp)
        assert len(evt.ctx.transcript) == 1

    def test_input_to_event_none_transcript(self):
        from captain_hook.tests.helpers import input_to_event
        from captain_hook.testing.types import Input

        inp = Input(tool="Edit", file="x.py", content="new", old="old")
        evt = input_to_event(Event.PreToolUse, inp)
        assert len(evt.ctx.transcript) == 0

    def test_input_to_event_stop(self):
        from captain_hook.tests.helpers import input_to_event
        from captain_hook.testing.types import Input

        evt = input_to_event(Event.Stop, Input())
        assert isinstance(evt, StopEvent)

    def test_input_to_event_subagent_stop(self):
        from captain_hook.tests.helpers import input_to_event
        from captain_hook.testing.types import Input

        evt = input_to_event(Event.SubagentStop, Input(agent_type="cleanup"))
        assert isinstance(evt, SubagentStopEvent)
        assert evt._raw["agent_type"] == "cleanup"

    def test_input_to_event_user_prompt(self):
        from captain_hook.tests.helpers import input_to_event
        from captain_hook.testing.types import Input

        evt = input_to_event(Event.UserPromptSubmit, Input(prompt="do it"))
        assert isinstance(evt, UserPromptSubmitEvent)
        assert evt._raw["prompt"] == "do it"

    def test_input_to_event_spec_tool_fallback(self):
        from captain_hook.tests.helpers import input_to_event
        from captain_hook.testing.types import Input

        evt = input_to_event(Event.PreToolUse, Input(command="ls"), spec_tool="Bash")
        assert evt.tool_name == "Bash"


class TestDispatchTest:
    def test_dispatch_test_returns_result(self):
        from captain_hook.tests.helpers import dispatch_test

        reset()
        register_hook(
            Event.PreToolUse,
            only_if=[Tool("Bash")],
            message="blocked",
            block=True,
        )
        result = dispatch_test(
            Event.PreToolUse,
            tool="Bash",
            command="ls",
        )
        assert result is not None
        out = result["hookSpecificOutput"]
        assert out["permissionDecision"] == "deny"

    def test_dispatch_test_returns_none_for_no_match(self):
        from captain_hook.tests.helpers import dispatch_test

        reset()
        result = dispatch_test(Event.PreToolUse, tool="Bash", command="ls")
        assert result is None


class TestAssertResult:
    def test_assert_result_allow_none(self):
        from captain_hook.tests.helpers import assert_result
        from captain_hook.testing.types import Allow

        assert_result(None, Allow())

    def test_assert_result_allow_action(self):
        from captain_hook.tests.helpers import assert_result
        from captain_hook.testing.types import Allow

        assert_result(HookResult(action=Action.allow), Allow())

    def test_assert_result_allow_fails_on_block(self):
        from captain_hook.tests.helpers import assert_result
        from captain_hook.testing.types import Allow

        with pytest.raises(AssertionError):
            assert_result(HookResult(action=Action.block, message="no"), Allow())

    def test_assert_result_block_matches(self):
        from captain_hook.tests.helpers import assert_result
        from captain_hook.testing.types import Block

        assert_result(
            HookResult(action=Action.block, message="not allowed here"),
            Block(pattern="not allowed"),
        )

    def test_assert_result_block_no_pattern(self):
        from captain_hook.tests.helpers import assert_result
        from captain_hook.testing.types import Block

        assert_result(
            HookResult(action=Action.block, message="whatever"),
            Block(),
        )

    def test_assert_result_block_fails_on_none(self):
        from captain_hook.tests.helpers import assert_result
        from captain_hook.testing.types import Block

        with pytest.raises(AssertionError):
            assert_result(None, Block(pattern="x"))

    def test_assert_result_block_fails_on_wrong_pattern(self):
        from captain_hook.tests.helpers import assert_result
        from captain_hook.testing.types import Block

        with pytest.raises(AssertionError):
            assert_result(
                HookResult(action=Action.block, message="something else"),
                Block(pattern="^totally different$"),
            )

    def test_assert_result_warn_matches(self):
        from captain_hook.tests.helpers import assert_result
        from captain_hook.testing.types import Warn

        assert_result(
            HookResult(action=Action.warn, message="check style"),
            Warn(pattern="style"),
        )

    def test_assert_result_warn_fails_on_allow(self):
        from captain_hook.tests.helpers import assert_result
        from captain_hook.testing.types import Warn

        with pytest.raises(AssertionError):
            assert_result(None, Warn(pattern="x"))


class TestRunInlineTests:
    def test_discovers_and_executes_inline_tests(self):
        from captain_hook.testing.helpers import run_inline_tests
        from captain_hook.testing.types import Allow, Block, Input

        reset()
        register_hook(
            Event.PreToolUse,
            only_if=[Tool("Bash")],
            message="blocked command",
            block=True,
            tests={
                Input(tool="Bash", command="rm -rf /"): Block(pattern="blocked"),
                Input(tool="Edit", file="x.py", content="new", old="old"): Allow(),
            },
        )
        results = run_inline_tests()
        assert len(results) == 2
        assert all(r[2] for r in results), f"Failed: {results}"

    def test_returns_empty_for_no_tests(self):
        from captain_hook.testing.helpers import run_inline_tests

        reset()
        register_hook(Event.PreToolUse, message="no tests", only_if=[Tool("Bash")])
        results = run_inline_tests()
        assert results == []

    def test_captures_failing_test(self):
        from captain_hook.testing.helpers import run_inline_tests
        from captain_hook.testing.types import Block, Input

        reset()
        register_hook(
            Event.PreToolUse,
            only_if=[Tool("Bash")],
            message="warned",
            tests={Input(tool="Bash", command="ls"): Block(pattern="wrong")},
        )
        results = run_inline_tests()
        assert len(results) == 1
        assert not results[0][2]
