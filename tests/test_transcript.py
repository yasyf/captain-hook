from __future__ import annotations

import pytest

from captain_hook.file import File
from captain_hook.transcript.inputs import (
    AgentInput,
    BashInput,
    EditInput,
    FileInputBase,
    GenericInput,
    GlobInput,
    GrepInput,
    ReadInput,
    SkillInput,
    TaskCreateInput,
    TaskUpdateInput,
    WriteInput,
    parse_tool_input,
)
from captain_hook.transcript import Transcript
from captain_hook.transcript.models import (
    TaskNotification,
    TextBlock,
    ToolResult,
    ToolUse,
    ToolUseBlock,
    TranscriptMessage,
    parse_content_block,
)


class TestTextBlock:
    def test_parse_from_raw_dict(self):
        result = parse_content_block({"type": "text", "text": "hello"})
        assert isinstance(result, TextBlock)
        assert result.text == "hello"

    def test_defaults_to_empty_text(self):
        result = parse_content_block({"type": "text"})
        assert isinstance(result, TextBlock)
        assert result.text == ""

    def test_frozen(self):
        block = TextBlock(text="hello")
        with pytest.raises(AttributeError):
            block.text = "world"  # type: ignore[misc]


class TestToolUseBlock:
    def test_parse_from_raw_dict(self):
        raw = {"type": "tool_use", "name": "Edit", "input": {"file_path": "foo.py"}, "id": "tu_123"}
        result = parse_content_block(raw)
        assert isinstance(result, ToolUseBlock)
        assert result.name == "Edit"
        assert result.input == {"file_path": "foo.py"}
        assert result.id == "tu_123"

    def test_frozen(self):
        block = ToolUseBlock(name="Edit", input={}, id="x")
        with pytest.raises(AttributeError):
            block.name = "Write"  # type: ignore[misc]


class TestToolResult:
    def test_parse_from_raw_dict(self):
        raw = {"type": "tool_result", "tool_use_id": "tu_123", "content": [{"text": "ok"}], "is_error": True}
        result = parse_content_block(raw)
        assert isinstance(result, ToolResult)
        assert result.tool_use_id == "tu_123"
        assert result.content == [{"text": "ok"}]
        assert result.is_error is True

    def test_defaults_is_error_to_false(self):
        raw = {"type": "tool_result", "tool_use_id": "tu_456"}
        result = parse_content_block(raw)
        assert isinstance(result, ToolResult)
        assert result.is_error is False

    def test_frozen(self):
        tr = ToolResult(tool_use_id="x")
        with pytest.raises(AttributeError):
            tr.is_error = True  # type: ignore[misc]

    def test_is_async_from_toolUseResult(self):
        t = Transcript.from_messages([
            {"type": "user", "toolUseResult": {"isAsync": True, "status": "async_launched"},
                "message": {"content": [{"type": "tool_result", "tool_use_id": "tu_a", "content": []}]}},
        ])
        [tr] = t.messages[0].tool_results
        assert tr.is_async is True

    def test_is_async_false_without_field(self):
        t = Transcript.from_messages([
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tu_a", "content": []}]}},
        ])
        [tr] = t.messages[0].tool_results
        assert tr.is_async is False


class TestParseContentBlock:
    def test_string_wraps_as_text_block(self):
        assert parse_content_block("hello") == TextBlock(text="hello")

    def test_unknown_type_returns_text_block(self):
        result = parse_content_block({"type": "unknown", "text": "fallback"})
        assert isinstance(result, TextBlock)
        assert result.text == "fallback"


class TestTranscriptMessage:
    def test_from_raw_stores_parsed_content_blocks(self):
        msg = TranscriptMessage.from_raw(
            type="assistant",
            content=[
                {"type": "text", "text": "hello"},
                {"type": "tool_use", "name": "Edit", "input": {}, "id": "tu_1"},
            ],
            raw={},
        )
        assert isinstance(msg.content[0], TextBlock)
        assert isinstance(msg.content[1], ToolUseBlock)

    def test_text_joins_text_blocks(self):
        msg = TranscriptMessage.from_raw(
            type="assistant",
            content=[
                {"type": "text", "text": "a"},
                {"type": "tool_use", "name": "Edit", "input": {}, "id": "tu_1"},
                {"type": "text", "text": "b"},
            ],
            raw={},
        )
        assert msg.text == "a\nb"

    def test_text_for_string_content(self):
        msg = TranscriptMessage.from_raw(type="user", content="plain", raw={})
        assert msg.text == "plain"

    def test_tool_uses_extracts_tool_use_blocks(self):
        msg = TranscriptMessage.from_raw(
            type="assistant",
            content=[
                {"type": "text", "text": "thinking..."},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "foo.py"}, "id": "tu_1"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}, "id": "tu_2"},
            ],
            raw={},
        )
        tool_uses = msg.tool_uses
        assert len(tool_uses) == 2
        assert tool_uses[0].name == "Edit"
        assert tool_uses[1].name == "Bash"

    def test_tool_results_extracts_tool_results(self):
        msg = TranscriptMessage.from_raw(
            type="tool",
            content=[
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok", "is_error": False},
            ],
            raw={},
        )
        results = msg.tool_results
        assert len(results) == 1
        assert results[0].tool_use_id == "tu_1"

    def test_tool_uses_empty_for_string_content(self):
        msg = TranscriptMessage.from_raw(type="user", content="hello", raw={})
        assert msg.tool_uses == []

    def test_tool_results_empty_for_string_content(self):
        msg = TranscriptMessage.from_raw(type="user", content="hello", raw={})
        assert msg.tool_results == []

    def test_notification_on_queue_operation(self):
        t = Transcript.from_messages([
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "content": "<task-notification><tool-use-id>tu_z</tool-use-id></task-notification>",
            },
        ])
        n = t.messages[0].notification
        assert n is not None
        assert n.tool_use_id == "tu_z"

    def test_notification_none_for_non_queue_operation(self):
        t = Transcript.from_messages([
            {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}},
        ])
        assert t.messages[0].notification is None

    def test_notification_none_when_content_missing_id(self):
        t = Transcript.from_messages([
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "content": "<task-notification></task-notification>",
            },
        ])
        assert t.messages[0].notification is None


class TestTurnStart:
    def test_skips_tool_result_user_wrappers(self):
        from captain_hook.classifiers.conductor import classifier as conductor_classifier

        messages = [
            TranscriptMessage.from_raw(type="user", content=[{"type": "text", "text": "real user prompt"}], raw={}),
            TranscriptMessage.from_raw(
                type="assistant",
                content=[{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}, "id": "tu_1"}],
                raw={},
            ),
            TranscriptMessage.from_raw(
                type="user",
                content=[{"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"}],
                raw={},
            ),
            TranscriptMessage.from_raw(
                type="assistant",
                content=[{"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}, "id": "tu_2"}],
                raw={},
            ),
            TranscriptMessage.from_raw(
                type="user",
                content=[{"type": "tool_result", "tool_use_id": "tu_2", "content": "/"}],
                raw={},
            ),
        ]
        t = Transcript(messages, classifier=conductor_classifier)
        assert t.turn_start == 1


class TestTaskNotification:
    def test_from_content_extracts_tool_use_id(self):
        n = TaskNotification.from_content(
            "<task-notification><tool-use-id>tu_abc</tool-use-id></task-notification>"
        )
        assert n is not None
        assert n.tool_use_id == "tu_abc"

    def test_from_content_missing_tag_returns_none(self):
        assert (
            TaskNotification.from_content("<task-notification>no id here</task-notification>") is None
        )

    def test_from_content_empty_string_returns_none(self):
        assert TaskNotification.from_content("") is None


class TestToolUse:
    def test_result_linked_via_tool_use_id(self):
        result = ToolResult(tool_use_id="tu_1", is_error=False)
        tu = ToolUse(name="Edit", raw_input={"file_path": "foo.py"}, id="tu_1", result=result)
        assert tu.result is result
        assert tu.result.is_error is False

    def test_result_none_when_no_match(self):
        tu = ToolUse(name="Edit", raw_input={"file_path": "foo.py"}, id="tu_1")
        assert tu.result is None

    def test_is_error_delegates_to_result_true(self):
        result = ToolResult(tool_use_id="tu_1", is_error=True)
        tu = ToolUse(name="Edit", raw_input={}, id="tu_1", result=result)
        assert tu.is_error is True

    def test_is_error_delegates_to_result_false(self):
        result = ToolResult(tool_use_id="tu_1", is_error=False)
        tu = ToolUse(name="Edit", raw_input={}, id="tu_1", result=result)
        assert tu.is_error is False

    def test_is_error_none_when_no_result(self):
        tu = ToolUse(name="Edit", raw_input={}, id="tu_1")
        assert tu.is_error is None

    def test_input_lazy_parses_bash(self):
        tu = ToolUse(name="Bash", raw_input={"command": "ls -la", "timeout": 30}, id="tu_1")
        inp = tu.input
        assert isinstance(inp, BashInput)
        assert inp.command == "ls -la"

    def test_input_lazy_parses_edit(self):
        tu = ToolUse(
            name="Edit",
            raw_input={"file_path": "foo.py", "old_string": "old", "new_string": "new"},
            id="tu_1",
        )
        inp = tu.input
        assert isinstance(inp, EditInput)
        assert inp.file_path == "foo.py"
        assert inp.old == "old"
        assert inp.new == "new"

    def test_file_delegates_to_file_input_base(self):
        tu = ToolUse(name="Edit", raw_input={"file_path": "/tmp/foo.py", "old_string": "", "new_string": ""}, id="tu_1")
        assert tu.file is not None
        assert isinstance(tu.file, File)
        assert str(tu.file) == "/tmp/foo.py"

    def test_file_none_for_non_file_tool(self):
        tu = ToolUse(name="Bash", raw_input={"command": "ls"}, id="tu_1")
        assert tu.file is None

    def test_command_from_bash_input(self):
        tu = ToolUse(name="Bash", raw_input={"command": "echo hello"}, id="tu_1")
        assert tu.command == "echo hello"

    def test_command_none_for_non_bash(self):
        tu = ToolUse(name="Edit", raw_input={"file_path": "x", "old_string": "", "new_string": ""}, id="tu_1")
        assert tu.command is None

    def test_command_line_parses_bash(self):
        tu = ToolUse(name="Bash", raw_input={"command": "echo hello"}, id="tu_1")
        cl = tu.command_line
        assert cl is not None

    def test_command_line_none_for_non_bash(self):
        tu = ToolUse(name="Edit", raw_input={"file_path": "x", "old_string": "", "new_string": ""}, id="tu_1")
        assert tu.command_line is None

    def test_agent_type_from_agent_input(self):
        tu = ToolUse(name="Agent", raw_input={"prompt": "do stuff", "subagent_type": "worker"}, id="tu_1")
        assert tu.agent_type == "worker"

    def test_agent_type_none_for_non_agent(self):
        tu = ToolUse(name="Bash", raw_input={"command": "ls"}, id="tu_1")
        assert tu.agent_type is None

    def test_frozen(self):
        tu = ToolUse(name="Edit", raw_input={}, id="tu_1")
        with pytest.raises(AttributeError):
            tu.name = "Write"  # type: ignore[misc]


class TestBashInput:
    def test_from_raw(self):
        inp = BashInput.from_raw({"command": "ls -la", "timeout": 30})
        assert inp.command == "ls -la"
        assert inp.timeout == 30

    def test_timeout_defaults_none(self):
        inp = BashInput.from_raw({"command": "echo hello"})
        assert inp.timeout is None


class TestEditInput:
    def test_from_raw_maps_old_string_new_string(self):
        inp = EditInput.from_raw({"file_path": "foo.py", "old_string": "old", "new_string": "new"})
        assert inp.old == "old"
        assert inp.new == "new"
        assert inp.file_path == "foo.py"

    def test_file_returns_file_object(self):
        inp = EditInput.from_raw({"file_path": "/tmp/foo.py", "old_string": "", "new_string": ""})
        assert isinstance(inp.file, File)
        assert str(inp.file) == "/tmp/foo.py"

    def test_is_file_input_base(self):
        inp = EditInput.from_raw({"file_path": "foo.py", "old_string": "", "new_string": ""})
        assert isinstance(inp, FileInputBase)


class TestWriteInput:
    def test_from_raw(self):
        inp = WriteInput.from_raw({"file_path": "bar.py", "content": "hello world"})
        assert inp.content == "hello world"
        assert inp.file_path == "bar.py"


class TestReadInput:
    def test_from_raw_with_limit_offset(self):
        inp = ReadInput.from_raw({"file_path": "baz.py", "limit": 100, "offset": 50})
        assert inp.limit == 100
        assert inp.offset == 50

    def test_from_raw_optional_limit_offset(self):
        inp = ReadInput.from_raw({"file_path": "baz.py"})
        assert inp.limit is None
        assert inp.offset is None


class TestAgentInput:
    def test_from_raw_normalizes_subagent_type(self):
        inp = AgentInput.from_raw({"prompt": "do stuff", "subagent_type": "worker"})
        assert inp.agent_type == "worker"

    def test_from_raw_normalizes_agent_type(self):
        inp = AgentInput.from_raw({"prompt": "do stuff", "agent_type": "searcher"})
        assert inp.agent_type == "searcher"

    def test_subagent_type_takes_precedence(self):
        inp = AgentInput.from_raw({"prompt": "do stuff", "subagent_type": "worker", "agent_type": "searcher"})
        assert inp.agent_type == "worker"


class TestGrepInput:
    def test_from_raw_maps_type_to_file_type(self):
        inp = GrepInput.from_raw({"pattern": "def foo", "type": "py"})
        assert inp.file_type == "py"
        assert inp.pattern == "def foo"

    def test_from_raw_no_type(self):
        inp = GrepInput.from_raw({"pattern": "hello"})
        assert inp.file_type is None


class TestGlobInput:
    def test_from_raw(self):
        inp = GlobInput.from_raw({"pattern": "*.py"})
        assert inp.pattern == "*.py"


class TestTaskCreateInput:
    def test_from_raw(self):
        inp = TaskCreateInput.from_raw({"subject": "Fix bug", "description": "Details here"})
        assert inp.subject == "Fix bug"
        assert inp.description == "Details here"


class TestTaskUpdateInput:
    def test_from_raw_maps_taskId(self):
        inp = TaskUpdateInput.from_raw({"taskId": "T-1", "status": "done"})
        assert inp.task_id == "T-1"
        assert inp.status == "done"

    def test_from_raw_maps_task_id(self):
        inp = TaskUpdateInput.from_raw({"task_id": "T-2", "status": "pending"})
        assert inp.task_id == "T-2"


class TestSkillInput:
    def test_from_raw(self):
        inp = SkillInput.from_raw({"skill": "codex", "args": "--check"})
        assert inp.skill == "codex"
        assert inp.args == "--check"


class TestGenericInput:
    def test_get_delegates_to_raw(self):
        inp = GenericInput(raw={"key": "value"})
        assert inp.get("key") == "value"
        assert inp.get("missing", "default") == "default"


class TestParseToolInput:
    def test_dispatches_by_tool_name(self):
        assert isinstance(parse_tool_input("Bash", {"command": "ls"}), BashInput)
        assert isinstance(parse_tool_input("Edit", {"file_path": "f", "old_string": "o", "new_string": "n"}), EditInput)
        assert isinstance(parse_tool_input("Write", {"file_path": "f", "content": "c"}), WriteInput)
        assert isinstance(parse_tool_input("Read", {"file_path": "f"}), ReadInput)
        assert isinstance(parse_tool_input("Agent", {"prompt": "p"}), AgentInput)
        assert isinstance(parse_tool_input("Grep", {"pattern": "p"}), GrepInput)
        assert isinstance(parse_tool_input("Glob", {"pattern": "p"}), GlobInput)
        assert isinstance(parse_tool_input("TaskCreate", {"subject": "s"}), TaskCreateInput)
        assert isinstance(parse_tool_input("TaskUpdate", {"taskId": "t"}), TaskUpdateInput)
        assert isinstance(parse_tool_input("Skill", {"skill": "s"}), SkillInput)

    def test_alias_expansion(self):
        assert isinstance(parse_tool_input("Execute", {"command": "ls"}), BashInput)
        assert isinstance(parse_tool_input("Create", {"file_path": "f", "content": "c"}), WriteInput)
        assert isinstance(parse_tool_input("Task", {"prompt": "p"}), AgentInput)

    def test_falls_back_to_generic_input(self):
        result = parse_tool_input("UnknownTool", {"foo": "bar"})
        assert isinstance(result, GenericInput)
        assert result.raw == {"foo": "bar"}

    def test_malformed_input_falls_back(self):
        result = parse_tool_input("Edit", {"wrong_key": "value"})
        assert isinstance(result, GenericInput)


class TestInputBaseAs:
    def test_returns_self_for_matching_type(self):
        inp = BashInput.from_raw({"command": "ls"})
        assert inp.as_(BashInput) is inp

    def test_returns_none_for_non_matching(self):
        inp = BashInput.from_raw({"command": "ls"})
        assert inp.as_(EditInput) is None

    def test_returns_default_for_non_matching(self):
        inp = BashInput.from_raw({"command": "ls"})
        default = EditInput(file_path="x", old="", new="", raw={})
        assert inp.as_(EditInput, default) is default


class TestFileInputBaseFile:
    def test_edit_input_file(self):
        inp = EditInput.from_raw({"file_path": "/tmp/test.py", "old_string": "a", "new_string": "b"})
        assert isinstance(inp.file, File)
        assert str(inp.file.path) == "/tmp/test.py"

    def test_write_input_file(self):
        inp = WriteInput.from_raw({"file_path": "/tmp/out.py", "content": "x"})
        assert isinstance(inp.file, File)

    def test_read_input_file(self):
        inp = ReadInput.from_raw({"file_path": "/tmp/read.py"})
        assert isinstance(inp.file, File)


class TestMalformedToolInput:
    def test_string_raw_input_normalized(self):
        result = parse_tool_input("Bash", "not a dict")  # type: ignore[arg-type]
        assert isinstance(result, GenericInput)
        assert result.raw == {}

    def test_list_raw_input_normalized(self):
        result = parse_tool_input("Edit", ["a", "b"])  # type: ignore[arg-type]
        assert isinstance(result, GenericInput)
        assert result.raw == {}

    def test_none_raw_input_normalized(self):
        result = parse_tool_input("Write", None)  # type: ignore[arg-type]
        assert isinstance(result, GenericInput)
        assert result.raw == {}

    def test_int_raw_input_normalized(self):
        result = parse_tool_input("Read", 42)  # type: ignore[arg-type]
        assert isinstance(result, GenericInput)
        assert result.raw == {}

    def test_unknown_tool_with_non_dict_normalized(self):
        result = parse_tool_input("Unknown", True)  # type: ignore[arg-type]
        assert isinstance(result, GenericInput)
        assert result.raw == {}


class TestMultiEditInput:
    def test_multi_edit_maps_to_edit_input(self):
        raw = {"file_path": "/tmp/test.py", "old_string": "foo", "new_string": "bar"}
        result = parse_tool_input("MultiEdit", raw)
        assert isinstance(result, EditInput)
        assert result.file_path == "/tmp/test.py"
        assert result.old == "foo"
        assert result.new == "bar"

    def test_multi_edit_file_extraction(self):
        raw = {"file_path": "/tmp/test.py", "old_string": "a", "new_string": "b"}
        result = parse_tool_input("MultiEdit", raw)
        assert isinstance(result, EditInput)
        assert isinstance(result.file, File)
        assert str(result.file.path) == "/tmp/test.py"

    def test_multi_edit_tool_use_file(self):
        tu = ToolUse(name="MultiEdit", raw_input={"file_path": "/src/main.py", "old_string": "x", "new_string": "y"}, id="tu_1")
        assert tu.file is not None
        assert str(tu.file.path) == "/src/main.py"
