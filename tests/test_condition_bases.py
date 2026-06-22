from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from cc_transcript.tools import BashCall, ReadCall

from captain_hook.events import BaseHookEvent
from captain_hook.testing.helpers import mock_tool_event
from captain_hook.types import CustomCommandLineCondition, CustomInputTypeCondition

if TYPE_CHECKING:
    from captain_hook.command import CommandLine


class ReadOfPython(CustomInputTypeCondition[ReadCall]):
    def check_input(self, evt: BaseHookEvent, call: ReadCall) -> bool:
        return call.file_path.endswith(".py")


class HasGit(CustomCommandLineCondition):
    def check_command_line(self, evt: BaseHookEvent, cl: CommandLine) -> bool:
        return "git" in str(cl)


class TestAsInput:
    def test_returns_call_on_matching_event(self) -> None:
        evt = mock_tool_event(tool="Read", file="x.py")
        narrowed = evt.as_input(ReadCall)
        assert isinstance(narrowed, ReadCall)
        assert narrowed.file_path == "x.py"

    def test_returns_none_on_wrong_call_type(self) -> None:
        evt = mock_tool_event(tool="Bash", command="ls")
        assert evt.as_input(ReadCall) is None

    def test_returns_none_for_other_type_on_read_event(self) -> None:
        evt = mock_tool_event(tool="Read", file="x.py")
        assert evt.as_input(BashCall) is None


class TestCustomInputTypeCondition:
    def test_call_type_bound_from_type_argument(self) -> None:
        assert ReadOfPython._call_type is ReadCall

    def test_fires_on_matching_read_event(self) -> None:
        evt = mock_tool_event(tool="Read", file="x.py")
        assert ReadOfPython().check(evt) is True

    def test_skips_read_event_when_check_input_false(self) -> None:
        evt = mock_tool_event(tool="Read", file="notes.txt")
        assert ReadOfPython().check(evt) is False

    def test_skips_wrong_call_type(self) -> None:
        evt = mock_tool_event(tool="Bash", command="ls")
        assert ReadOfPython().check(evt) is False

    def test_missing_type_argument_raises_at_subclass_time(self) -> None:
        with pytest.raises(TypeError, match="must parameterize CustomInputTypeCondition"):

            class Untyped(CustomInputTypeCondition):  # type: ignore[type-arg]
                def check_input(self, evt: BaseHookEvent, call: ReadCall) -> bool:
                    return True


class TestCustomCommandLineCondition:
    def test_returns_false_when_command_line_absent(self) -> None:
        evt = mock_tool_event(tool="Read", file="x.py")
        assert evt.command_line is None
        assert HasGit().check(evt) is False

    def test_delegates_when_command_line_present(self) -> None:
        evt = mock_tool_event(tool="Bash", command="git push origin")
        assert HasGit().check(evt) is True

    def test_delegates_returns_false_on_non_matching_command_line(self) -> None:
        evt = mock_tool_event(tool="Bash", command="ls -la")
        assert HasGit().check(evt) is False
