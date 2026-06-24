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

    @pytest.mark.parametrize(
        ("evt", "call_type"),
        [
            pytest.param(mock_tool_event(tool="Bash", command="ls"), ReadCall, id="wrong_call_type"),
            pytest.param(mock_tool_event(tool="Read", file="x.py"), BashCall, id="other_type_on_read_event"),
        ],
    )
    def test_returns_none(self, evt: BaseHookEvent, call_type: type) -> None:
        assert evt.as_input(call_type) is None


class TestCustomInputTypeCondition:
    def test_call_type_bound_from_type_argument(self) -> None:
        assert ReadOfPython._call_type is ReadCall

    @pytest.mark.parametrize(
        ("evt", "expected"),
        [
            pytest.param(mock_tool_event(tool="Read", file="x.py"), True, id="fires_on_matching_read_event"),
            pytest.param(
                mock_tool_event(tool="Read", file="notes.txt"), False, id="skips_read_event_when_check_input_false"
            ),
            pytest.param(mock_tool_event(tool="Bash", command="ls"), False, id="skips_wrong_call_type"),
        ],
    )
    def test_check(self, evt: BaseHookEvent, expected: bool) -> None:
        assert ReadOfPython().check(evt) is expected

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

    @pytest.mark.parametrize(
        ("evt", "expected"),
        [
            pytest.param(mock_tool_event(tool="Bash", command="git push origin"), True, id="delegates_when_present"),
            pytest.param(mock_tool_event(tool="Bash", command="ls -la"), False, id="delegates_false_on_non_matching"),
        ],
    )
    def test_delegates(self, evt: BaseHookEvent, expected: bool) -> None:
        assert HasGit().check(evt) is expected
