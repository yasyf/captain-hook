from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from captain_hook.events import (
    BaseHookEvent,
    NotificationEvent,
    PostToolUseEvent,
    PostToolUseFailureEvent,
    PreCompactEvent,
    PreToolUseEvent,
    StopEvent,
    SubagentStartEvent,
    SubagentStopEvent,
    UserPromptSubmitEvent,
)
from captain_hook.types import Event


def make_ctx() -> MagicMock:
    return MagicMock()


def make_event(cls: type[BaseHookEvent], raw: dict[str, Any] | None = None) -> BaseHookEvent:
    return cls(_raw=raw or {}, ctx=make_ctx())


class TestEventClassVar:
    def test_each_event_class_has_correct_event_name(self) -> None:
        mapping = {
            PreToolUseEvent: Event.PreToolUse,
            PostToolUseEvent: Event.PostToolUse,
            PostToolUseFailureEvent: Event.PostToolUseFailure,
            UserPromptSubmitEvent: Event.UserPromptSubmit,
            StopEvent: Event.Stop,
            SubagentStopEvent: Event.SubagentStop,
            SubagentStartEvent: Event.SubagentStart,
            PreCompactEvent: Event.PreCompact,
            NotificationEvent: Event.Notification,
        }
        for cls, expected_event in mapping.items():
            assert cls.event_name is expected_event
            evt = make_event(cls)
            assert evt.event is expected_event


class TestBaseHookEvent:
    def test_no_public_raw_input_property(self) -> None:
        evt = make_event(StopEvent, {"tool_name": "Bash"})
        assert not hasattr(type(evt), "raw_input")

    def test_user_prompt_from_raw(self) -> None:
        evt = make_event(UserPromptSubmitEvent, {"prompt": "hello"})
        assert evt.user_prompt == "hello"

    def test_user_prompt_none_when_missing(self) -> None:
        evt = make_event(UserPromptSubmitEvent, {})
        assert evt.user_prompt is None

    def test_is_subagent_true_when_agent_id_present(self) -> None:
        evt = make_event(StopEvent, {"agent_id": "sub-1"})
        assert evt.is_subagent is True

    def test_is_subagent_false_when_absent(self) -> None:
        evt = make_event(StopEvent, {})
        assert evt.is_subagent is False

    def test_stop_hook_active_default_false(self) -> None:
        evt = make_event(StopEvent, {})
        assert evt.stop_hook_active is False

    def test_stop_hook_active_true_when_set(self) -> None:
        evt = make_event(StopEvent, {"stop_hook_active": True})
        assert evt.stop_hook_active is True

    def test_parent_agent_type_from_raw(self) -> None:
        evt = make_event(StopEvent, {"agent_type": "worker"})
        assert evt.parent_agent_type == "worker"

    def test_parent_agent_type_none_when_missing(self) -> None:
        evt = make_event(StopEvent, {})
        assert evt.parent_agent_type is None

    @pytest.mark.parametrize(
        "attr,expected",
        [
            ("tool_name", None),
            ("command", None),
            ("command_line", None),
            ("file", None),
            ("content", None),
            ("old", None),
            ("agent_type", None),
        ],
    )
    def test_base_event_property_defaults(self, attr: str, expected: object) -> None:
        evt = make_event(StopEvent, {})
        assert getattr(evt, attr) == expected

    @pytest.mark.parametrize(
        "method,arg",
        [
            ("command_matches", r"git.*"),
            ("file_matches", "*.py"),
            ("content_matches", r"import.*"),
        ],
    )
    def test_base_event_match_methods_return_false(self, method: str, arg: str) -> None:
        evt = make_event(StopEvent, {})
        assert getattr(evt, method)(arg) is False


class TestToolHookEvent:
    def test_tool_name_from_raw(self) -> None:
        evt = make_event(
            PreToolUseEvent,
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": "a.py", "old_string": "x", "new_string": "y"},
            },
        )
        assert evt.tool_name == "Edit"

    def test_tool_name_none_when_missing(self) -> None:
        evt = make_event(PreToolUseEvent, {})
        assert evt.tool_name is None

    def test_content_for_edit_tool(self) -> None:
        evt = make_event(
            PreToolUseEvent,
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": "a.py", "old_string": "old", "new_string": "new text"},
            },
        )
        assert evt.content == "new text"

    def test_content_none_for_bash_tool(self) -> None:
        evt = make_event(
            PreToolUseEvent,
            {
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
            },
        )
        assert evt.content is None

    def test_old_for_edit_tool(self) -> None:
        evt = make_event(
            PreToolUseEvent,
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": "a.py", "old_string": "old text", "new_string": "new"},
            },
        )
        assert evt.old == "old text"

    def test_old_none_for_bash_tool(self) -> None:
        evt = make_event(
            PreToolUseEvent,
            {
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
            },
        )
        assert evt.old is None

    def test_content_for_write_tool(self) -> None:
        evt = make_event(
            PreToolUseEvent,
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "b.py", "content": "print('hello')"},
            },
        )
        assert evt.content == "print('hello')"

    def test_content_none_for_read_tool(self) -> None:
        evt = make_event(
            PreToolUseEvent,
            {
                "tool_name": "Read",
                "tool_input": {"file_path": "c.py"},
            },
        )
        assert evt.content is None


class TestPreToolUseEvent:
    def test_file_from_edit_input(self) -> None:
        evt = make_event(
            PreToolUseEvent,
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": "a.py", "old_string": "x", "new_string": "y"},
            },
        )
        assert evt.file is not None
        from pathlib import Path

        assert evt.file.path == Path("a.py")


class TestPostToolUseEvent:
    def test_tool_response_from_raw(self) -> None:
        evt = PostToolUseEvent(
            _raw={"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": "file1.txt\nfile2.txt"},
            ctx=make_ctx(),
        )
        assert evt.tool_response == "file1.txt\nfile2.txt"

    def test_tool_response_none_when_missing(self) -> None:
        evt = PostToolUseEvent(_raw={"tool_name": "Bash", "tool_input": {}}, ctx=make_ctx())
        assert evt.tool_response is None


class TestPostToolUseFailureEvent:
    def test_error_from_raw(self) -> None:
        evt = PostToolUseFailureEvent(
            _raw={"tool_name": "Bash", "tool_input": {}, "error": "command failed"},
            ctx=make_ctx(),
        )
        assert evt.error == "command failed"

    def test_error_raises_key_error_when_missing(self) -> None:
        evt = PostToolUseFailureEvent(_raw={"tool_name": "Bash", "tool_input": {}}, ctx=make_ctx())
        with pytest.raises(KeyError):
            evt.error

    def test_is_interrupt_from_raw(self) -> None:
        evt = PostToolUseFailureEvent(
            _raw={"tool_name": "Bash", "tool_input": {}, "error": "fail", "is_interrupt": True},
            ctx=make_ctx(),
        )
        assert evt.is_interrupt is True

    def test_is_interrupt_default_false(self) -> None:
        evt = PostToolUseFailureEvent(
            _raw={"tool_name": "Bash", "tool_input": {}, "error": "fail"},
            ctx=make_ctx(),
        )
        assert evt.is_interrupt is False


class TestPreCompactEvent:
    def test_trigger_and_custom_instructions(self) -> None:
        evt = PreCompactEvent(
            _raw={"trigger": "auto", "custom_instructions": "keep context"},
            ctx=make_ctx(),
        )
        assert evt.trigger == "auto"
        assert evt.custom_instructions == "keep context"

    def test_trigger_none_when_missing(self) -> None:
        evt = PreCompactEvent(_raw={}, ctx=make_ctx())
        assert evt.trigger is None
        assert evt.custom_instructions is None


class TestNotificationEvent:
    def test_message_title_type(self) -> None:
        evt = NotificationEvent(
            _raw={"message": "hello", "title": "Alert", "notification_type": "info"},
            ctx=make_ctx(),
        )
        assert evt.message == "hello"
        assert evt.title == "Alert"
        assert evt.notification_type == "info"

    def test_all_none_when_missing(self) -> None:
        evt = NotificationEvent(_raw={}, ctx=make_ctx())
        assert evt.message is None
        assert evt.title is None
        assert evt.notification_type is None


