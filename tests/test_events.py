from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from captain_hook.events import (
    BaseHookEvent,
    NotificationEvent,
    PermissionRequestEvent,
    PostToolUseEvent,
    PostToolUseFailureEvent,
    PreCompactEvent,
    PreToolUseEvent,
    SessionEndEvent,
    SessionStartEvent,
    StopEvent,
    SubagentStartEvent,
    SubagentStopEvent,
    UserPromptSubmitEvent,
)
from captain_hook.types import Action, Event


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
            SessionStartEvent: Event.SessionStart,
            SessionEndEvent: Event.SessionEnd,
            PermissionRequestEvent: Event.PermissionRequest,
        }
        for cls, expected_event in mapping.items():
            assert cls.event_name is expected_event
            evt = make_event(cls)
            assert evt.event is expected_event


class TestBaseHookEvent:
    def test_no_public_raw_input_property(self) -> None:
        evt = make_event(StopEvent, {"tool_name": "Bash"})
        assert not hasattr(type(evt), "raw_input")

    @pytest.mark.parametrize(
        ("cls", "raw", "attr", "expected"),
        [
            pytest.param(
                UserPromptSubmitEvent,
                {"prompt": "hello"},
                "user_prompt",
                "hello",
                id="user_prompt_from_raw",
            ),
            pytest.param(UserPromptSubmitEvent, {}, "user_prompt", None, id="user_prompt_none_when_missing"),
            pytest.param(StopEvent, {"agent_id": "sub-1"}, "agent_id", "sub-1", id="agent_id_from_raw"),
            pytest.param(StopEvent, {}, "agent_id", None, id="agent_id_none_when_missing"),
            pytest.param(StopEvent, {"agent_id": None}, "agent_id", None, id="agent_id_none_when_null"),
            pytest.param(StopEvent, {"agent_id": ""}, "agent_id", None, id="agent_id_none_when_empty"),
            pytest.param(StopEvent, {"agent_id": "sub-1"}, "is_subagent", True, id="is_subagent_true"),
            pytest.param(StopEvent, {}, "is_subagent", False, id="is_subagent_false"),
            pytest.param(StopEvent, {"agent_id": ""}, "is_subagent", False, id="is_subagent_false_when_empty"),
            pytest.param(StopEvent, {}, "stop_hook_active", False, id="stop_hook_active_default_false"),
            pytest.param(StopEvent, {"stop_hook_active": True}, "stop_hook_active", True, id="stop_hook_active_true"),
            pytest.param(StopEvent, {"agent_type": "worker"}, "parent_agent_type", "worker", id="parent_agent_type"),
            pytest.param(StopEvent, {}, "parent_agent_type", None, id="parent_agent_type_none"),
        ],
    )
    def test_attribute_round_trip(
        self, cls: type[BaseHookEvent], raw: dict[str, Any], attr: str, expected: object
    ) -> None:
        assert getattr(make_event(cls, raw), attr) == expected

    @pytest.mark.parametrize(
        "attr,expected",
        [
            ("tool_name", None),
            ("command", None),
            ("command_line", None),
            ("file", None),
            ("content", None),
            ("old", None),
            ("replaced", None),
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
    @pytest.mark.parametrize(
        ("raw", "attr", "expected"),
        [
            pytest.param(
                {"tool_name": "Edit", "tool_input": {"file_path": "a.py", "old_string": "x", "new_string": "y"}},
                "tool_name",
                "Edit",
                id="tool_name_from_raw",
            ),
            pytest.param({}, "tool_name", None, id="tool_name_none_when_missing"),
            pytest.param(
                {
                    "tool_name": "Edit",
                    "tool_input": {"file_path": "a.py", "old_string": "old", "new_string": "new text"},
                },
                "content",
                "new text",
                id="content_for_edit_tool",
            ),
            pytest.param(
                {"tool_name": "Bash", "tool_input": {"command": "ls"}},
                "content",
                None,
                id="content_none_for_bash_tool",
            ),
            pytest.param(
                {
                    "tool_name": "Edit",
                    "tool_input": {"file_path": "a.py", "old_string": "old text", "new_string": "new"},
                },
                "old",
                "old text",
                id="old_for_edit_tool",
            ),
            pytest.param(
                {"tool_name": "Bash", "tool_input": {"command": "ls"}},
                "old",
                None,
                id="old_none_for_bash_tool",
            ),
            pytest.param(
                {"tool_name": "Write", "tool_input": {"file_path": "b.py", "content": "print('hello')"}},
                "content",
                "print('hello')",
                id="content_for_write_tool",
            ),
            pytest.param(
                {"tool_name": "Read", "tool_input": {"file_path": "c.py"}},
                "content",
                None,
                id="content_none_for_read_tool",
            ),
        ],
    )
    def test_tool_attribute_round_trip(self, raw: dict[str, Any], attr: str, expected: object) -> None:
        assert getattr(make_event(PreToolUseEvent, raw), attr) == expected

    def test_replaced_for_edit_is_old(self) -> None:
        raw = {"tool_name": "Edit", "tool_input": {"file_path": "a.py", "old_string": "old text", "new_string": "new"}}
        assert make_event(PreToolUseEvent, raw).replaced == "old text"

    def test_replaced_for_multi_edit_joins_olds(self) -> None:
        raw = {
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": "a.py",
                "edits": [{"old_string": "one", "new_string": "1"}, {"old_string": "two", "new_string": "2"}],
            },
        }
        assert make_event(PreToolUseEvent, raw).replaced == "one\ntwo"

    def test_replaced_for_write_reads_disk(self, tmp_path: Path) -> None:
        path = tmp_path / "b.py"
        path.write_text("on disk")
        raw = {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": "new"}}
        assert make_event(PreToolUseEvent, raw).replaced == "on disk"

    def test_replaced_for_write_new_file_is_empty(self, tmp_path: Path) -> None:
        raw = {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "missing.py"), "content": "new"}}
        assert make_event(PreToolUseEvent, raw).replaced == ""

    def test_replaced_for_write_none_at_post_tool_use(self, tmp_path: Path) -> None:
        path = tmp_path / "b.py"
        path.write_text("already the new text")
        raw = {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": "already the new text"}}
        assert make_event(PostToolUseEvent, raw).replaced is None

    def test_replaced_for_write_decodes_non_utf8_lossily(self, tmp_path: Path) -> None:
        path = tmp_path / "b.py"
        path.write_bytes("café".encode("latin-1"))
        raw = {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": "new"}}
        assert make_event(PreToolUseEvent, raw).replaced == "caf�"

    def test_replaced_for_write_to_directory_is_none(self, tmp_path: Path) -> None:
        raw = {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path), "content": "new"}}
        assert make_event(PreToolUseEvent, raw).replaced is None

    def test_replaced_for_write_unreadable_file_is_none(self, tmp_path: Path) -> None:
        path = tmp_path / "b.py"
        path.write_text("hidden")
        path.chmod(0)
        raw = {"tool_name": "Write", "tool_input": {"file_path": str(path), "content": "new"}}
        assert make_event(PreToolUseEvent, raw).replaced is None

    def test_replaced_none_for_bash(self) -> None:
        assert make_event(PreToolUseEvent, {"tool_name": "Bash", "tool_input": {"command": "ls"}}).replaced is None


class TestPrePostImage:
    def test_pre_image_edit_reads_full_disk(self, tmp_path: Path) -> None:
        path = tmp_path / "a.py"
        path.write_text("on disk\n")
        raw = {"tool_name": "Edit", "tool_input": {"file_path": str(path), "old_string": "on", "new_string": "OFF"}}
        assert make_event(PreToolUseEvent, raw).pre_image == "on disk\n"

    def test_pre_image_write_new_file_is_empty(self, tmp_path: Path) -> None:
        raw = {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "new.py"), "content": "x"}}
        assert make_event(PreToolUseEvent, raw).pre_image == ""

    def test_pre_image_none_off_pre_tool_use(self, tmp_path: Path) -> None:
        path = tmp_path / "a.py"
        path.write_text("on disk\n")
        raw = {"tool_name": "Edit", "tool_input": {"file_path": str(path), "old_string": "on", "new_string": "OFF"}}
        assert make_event(PostToolUseEvent, raw).pre_image is None

    def test_pre_image_none_for_bash(self) -> None:
        assert make_event(PreToolUseEvent, {"tool_name": "Bash", "tool_input": {"command": "ls"}}).pre_image is None

    def test_post_image_edit_applies_old_to_new(self, tmp_path: Path) -> None:
        path = tmp_path / "a.py"
        path.write_text("hello world\n")
        raw = {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(path), "old_string": "world", "new_string": "there"},
        }
        assert make_event(PreToolUseEvent, raw).post_image == "hello there\n"

    def test_post_image_edit_missing_old_is_none(self, tmp_path: Path) -> None:
        path = tmp_path / "a.py"
        path.write_text("hello world\n")
        raw = {"tool_name": "Edit", "tool_input": {"file_path": str(path), "old_string": "absent", "new_string": "x"}}
        assert make_event(PreToolUseEvent, raw).post_image is None

    def test_post_image_edit_replace_all(self, tmp_path: Path) -> None:
        path = tmp_path / "a.py"
        path.write_text("a a a\n")
        raw = {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(path), "old_string": "a", "new_string": "b", "replace_all": True},
        }
        assert make_event(PreToolUseEvent, raw).post_image == "b b b\n"

    def test_post_image_edit_replaces_once_by_default(self, tmp_path: Path) -> None:
        path = tmp_path / "a.py"
        path.write_text("a b a\n")
        raw = {"tool_name": "Edit", "tool_input": {"file_path": str(path), "old_string": "b", "new_string": "c"}}
        assert make_event(PreToolUseEvent, raw).post_image == "a c a\n"

    def test_post_image_edit_ambiguous_old_is_none(self, tmp_path: Path) -> None:
        # The real Edit tool rejects a non-unique old_string; we must not manufacture a first-hit deny.
        path = tmp_path / "a.py"
        path.write_text("a a a\n")
        raw = {"tool_name": "Edit", "tool_input": {"file_path": str(path), "old_string": "a", "new_string": "b"}}
        assert make_event(PreToolUseEvent, raw).post_image is None

    def test_post_image_multi_edit_folds_spans_in_order(self, tmp_path: Path) -> None:
        path = tmp_path / "a.py"
        path.write_text("one two\n")
        raw = {
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": str(path),
                "edits": [{"old_string": "one", "new_string": "1"}, {"old_string": "two", "new_string": "2"}],
            },
        }
        assert make_event(PreToolUseEvent, raw).post_image == "1 2\n"

    def test_post_image_multi_edit_absent_span_is_none(self, tmp_path: Path) -> None:
        path = tmp_path / "a.py"
        path.write_text("one\n")
        raw = {
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": str(path),
                "edits": [{"old_string": "one", "new_string": "1"}, {"old_string": "absent", "new_string": "9"}],
            },
        }
        assert make_event(PreToolUseEvent, raw).post_image is None

    def test_post_image_write_is_content(self, tmp_path: Path) -> None:
        raw = {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "b.py"), "content": "fresh\n"}}
        assert make_event(PreToolUseEvent, raw).post_image == "fresh\n"

    def test_post_image_none_off_pre_tool_use(self, tmp_path: Path) -> None:
        raw = {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "b.py"), "content": "fresh\n"}}
        assert make_event(PostToolUseEvent, raw).post_image is None


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

    def test_rewrite_returns_rewrite_result(self) -> None:
        evt = make_event(PreToolUseEvent, {"tool_name": "Bash", "tool_input": {"command": "cat x"}})
        result = evt.rewrite({"command": "ccx read x --full"}, note="ran ccx")  # type: ignore[attr-defined]
        assert result.action is Action.rewrite
        assert result.updated_input == {"command": "ccx read x --full"}
        assert result.note == "ran ccx"

    def test_rewrite_note_defaults_none(self) -> None:
        evt = make_event(PreToolUseEvent, {"tool_name": "Bash", "tool_input": {"command": "cat x"}})
        result = evt.rewrite({"command": "y"})  # type: ignore[attr-defined]
        assert result.note is None

    def test_rewrite_command_preserves_other_input_keys(self) -> None:
        evt = make_event(
            PreToolUseEvent,
            {"tool_name": "Bash", "tool_input": {"command": "cat x", "timeout": 5000, "description": "read"}},
        )
        result = evt.rewrite_command("ccx read x --full", note="swapped")  # type: ignore[attr-defined]
        assert result.action is Action.rewrite
        assert result.updated_input == {"command": "ccx read x --full", "timeout": 5000, "description": "read"}
        assert result.note == "swapped"


class TestPermissionRequestEvent:
    def test_permission_suggestions_from_raw(self) -> None:
        suggestions = [{"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "ls:*"}]}]
        evt = make_event(
            PermissionRequestEvent,
            {"tool_name": "Bash", "tool_input": {"command": "ls"}, "permission_suggestions": suggestions},
        )
        assert evt.permission_suggestions == suggestions

    def test_permission_suggestions_default_empty_list(self) -> None:
        evt = make_event(PermissionRequestEvent, {"tool_name": "Bash", "tool_input": {"command": "ls"}})
        assert evt.permission_suggestions == []

    def test_agent_type_reads_raw_payload(self) -> None:
        evt = make_event(
            PermissionRequestEvent,
            {"tool_name": "Bash", "tool_input": {"command": "ls"}, "agent_type": "quality-gate"},
        )
        assert evt.agent_type == "quality-gate"

    def test_agent_type_ignores_task_call_subagent_type(self) -> None:
        # ToolHookEvent.agent_type reads the TaskCall subagent_type; the override reads only the raw payload.
        evt = make_event(
            PermissionRequestEvent,
            {"tool_name": "Task", "tool_input": {"subagent_type": "worker", "prompt": "go"}},
        )
        assert evt.agent_type is None

    def test_rewrite_command_available(self) -> None:
        evt = make_event(PermissionRequestEvent, {"tool_name": "Bash", "tool_input": {"command": "cat x"}})
        result = evt.rewrite_command("ccx read x --full")
        assert result.action is Action.rewrite
        assert result.updated_input == {"command": "ccx read x --full"}

    @pytest.mark.parametrize("injected", [True, False], ids=["true", "false"])
    def test_skip_permissions_cached_property_injection(self, injected: bool) -> None:
        evt = make_event(PermissionRequestEvent, {"tool_name": "Bash", "tool_input": {"command": "ls"}})
        evt.__dict__["skip_permissions"] = injected
        assert evt.skip_permissions is injected


class TestPostToolUseEvent:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param(
                {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": "file1.txt\nfile2.txt"},
                "file1.txt\nfile2.txt",
                id="tool_response_from_raw",
            ),
            pytest.param({"tool_name": "Bash", "tool_input": {}}, None, id="tool_response_none_when_missing"),
        ],
    )
    def test_tool_response(self, raw: dict[str, Any], expected: object) -> None:
        assert make_event(PostToolUseEvent, raw).tool_response == expected


class TestPostToolUseFailureEvent:
    @pytest.mark.parametrize(
        ("raw", "attr", "expected"),
        [
            pytest.param(
                {"tool_name": "Bash", "tool_input": {}, "error": "command failed"},
                "error",
                "command failed",
                id="error_from_raw",
            ),
            pytest.param(
                {"tool_name": "Bash", "tool_input": {}, "error": "fail", "is_interrupt": True},
                "is_interrupt",
                True,
                id="is_interrupt_from_raw",
            ),
            pytest.param(
                {"tool_name": "Bash", "tool_input": {}, "error": "fail"},
                "is_interrupt",
                False,
                id="is_interrupt_default_false",
            ),
        ],
    )
    def test_attribute_round_trip(self, raw: dict[str, Any], attr: str, expected: object) -> None:
        assert getattr(make_event(PostToolUseFailureEvent, raw), attr) == expected

    def test_error_raises_key_error_when_missing(self) -> None:
        evt = make_event(PostToolUseFailureEvent, {"tool_name": "Bash", "tool_input": {}})
        with pytest.raises(KeyError):
            evt.error


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


class TestSessionStartEvent:
    @pytest.mark.parametrize("source", ["startup", "resume", "clear", "compact"])
    def test_source_from_raw(self, source: str) -> None:
        evt = SessionStartEvent(_raw={"source": source}, ctx=make_ctx())
        assert evt.source == source

    def test_source_raises_key_error_when_missing(self) -> None:
        evt = SessionStartEvent(_raw={}, ctx=make_ctx())
        with pytest.raises(KeyError):
            evt.source

    def test_full_payload_parses(self) -> None:
        evt = SessionStartEvent(
            _raw={
                "session_id": "abc123",
                "transcript_path": "/tmp/t.jsonl",
                "cwd": "/tmp",
                "hook_event_name": "SessionStart",
                "source": "resume",
            },
            ctx=make_ctx(),
        )
        assert evt.event is Event.SessionStart
        assert evt.session_id == "abc123"
        assert evt.transcript_path == Path("/tmp/t.jsonl")
        assert evt.source == "resume"


class TestSessionEndEvent:
    @pytest.mark.parametrize("reason", ["clear", "logout", "prompt_input_exit", "other"])
    def test_reason_from_raw(self, reason: str) -> None:
        evt = SessionEndEvent(_raw={"reason": reason}, ctx=make_ctx())
        assert evt.reason == reason

    def test_reason_raises_key_error_when_missing(self) -> None:
        evt = SessionEndEvent(_raw={}, ctx=make_ctx())
        with pytest.raises(KeyError):
            evt.reason

    def test_full_payload_parses(self) -> None:
        evt = SessionEndEvent(
            _raw={
                "session_id": "abc123",
                "transcript_path": "/tmp/t.jsonl",
                "cwd": "/tmp",
                "hook_event_name": "SessionEnd",
                "reason": "clear",
            },
            ctx=make_ctx(),
        )
        assert evt.event is Event.SessionEnd
        assert evt.session_id == "abc123"
        assert evt.transcript_path == Path("/tmp/t.jsonl")
        assert evt.reason == "clear"


class TestWarnParts:
    @pytest.mark.parametrize(
        ("parts", "expected"),
        [
            pytest.param(("plain message",), "plain message", id="single_str_part_verbatim"),
            pytest.param((("files", ["a.py", "b.py"]),), 'files: ["a.py", "b.py"]', id="label_value_tuple_json"),
            pytest.param((("path", Path("/tmp/x")),), 'path: "/tmp/x"', id="label_value_tuple_default_str"),
            pytest.param(({"k": 1},), '{"k": 1}', id="bare_dict_renders_json"),
            pytest.param(("line1", ("n", 3), {"x": 1}), 'line1\nn: 3\n{"x": 1}', id="multiple_parts_join_newline"),
            pytest.param(("a\nb\nc",), "a\nb\nc", id="legacy_pre_joined_string"),
        ],
    )
    def test_warn_message(self, parts: tuple[object, ...], expected: str) -> None:
        assert make_event(StopEvent, {}).warn(*parts).message == expected

    def test_warn_action_is_warn(self) -> None:
        assert make_event(StopEvent, {}).warn("x").action is Action.warn


class TestTranscriptPath:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param({"transcript_path": "/tmp/x.jsonl"}, Path("/tmp/x.jsonl"), id="returns_path_when_present"),
            pytest.param({}, None, id="returns_none_when_absent"),
        ],
    )
    def test_transcript_path(self, raw: dict[str, Any], expected: Path | None) -> None:
        from tests.helpers import make_ctx as make_padded_ctx

        assert StopEvent(_raw=raw, ctx=make_padded_ctx()).transcript_path == expected
