from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from captain_hook.file import File
from captain_hook.tests.helpers import (
    make_transcript as _make_transcript,
)
from captain_hook.tests.helpers import (
    raw_msg as _msg,
)
from captain_hook.tests.helpers import (
    raw_tool_result_block as _tool_result,
)
from captain_hook.tests.helpers import (
    raw_tool_use as _tool_use,
)
from captain_hook.tests.helpers import (
    async_agent_launch,
    raw_assistant,
    raw_text,
    raw_tool_result,
)
from captain_hook.transcript import (
    Transcript,
    TranscriptMessage,
    TranscriptSlice,
    Turn,
)
from captain_hook.transcript.models import ContentBlock, TextBlock, ToolResult, ToolUseBlock


class TestToolUseSequence:
    def test_filters_errors_by_default(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""}, "tu_1"),
                    _tool_use("Bash", {"command": "ls"}, "tu_2"),
                    _tool_use("Write", {"file_path": "b.py", "content": "x"}, "tu_3"),
                ],
            ),
            _msg(
                "tool",
                [
                    _tool_result("tu_1", is_error=False),
                    _tool_result("tu_2", is_error=True),
                    _tool_result("tu_3", is_error=False),
                ],
            ),
        )
        seq = t.tool_uses
        assert len(seq) == 2

    def test_with_errors_includes_all(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""}, "tu_1"),
                    _tool_use("Bash", {"command": "ls"}, "tu_2"),
                    _tool_use("Write", {"file_path": "b.py", "content": "x"}, "tu_3"),
                ],
            ),
            _msg(
                "tool",
                [
                    _tool_result("tu_1", is_error=False),
                    _tool_result("tu_2", is_error=True),
                    _tool_result("tu_3", is_error=False),
                ],
            ),
        )
        assert len(t.tool_uses.with_errors) == 3

    def test_iteration_excludes_errors(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Bash", {"command": "ls"}, "tu_1"),
                    _tool_use("Bash", {"command": "pwd"}, "tu_2"),
                ],
            ),
            _msg(
                "tool",
                [
                    _tool_result("tu_1", is_error=True),
                    _tool_result("tu_2", is_error=False),
                ],
            ),
        )
        names = [tu.name for tu in t.tool_uses]
        assert len(names) == 1

    def test_getitem_excludes_errors(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Bash", {"command": "fail"}, "tu_1"),
                    _tool_use("Edit", {"file_path": "ok.py", "old_string": "", "new_string": ""}, "tu_2"),
                ],
            ),
            _msg(
                "tool",
                [
                    _tool_result("tu_1", is_error=True),
                    _tool_result("tu_2", is_error=False),
                ],
            ),
        )
        assert t.tool_uses[0].name == "Edit"

    def test_where_delegates_to_query(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""}, "tu_1"),
                    _tool_use("Bash", {"command": "ls"}, "tu_2"),
                ],
            ),
        )
        query = t.tool_uses.where(name="Edit")
        assert query.count() == 1


class TestToolUseQuery:
    def _query_transcript(self) -> Transcript:
        return _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Edit", {"file_path": "src/foo.py", "old_string": "old", "new_string": "new"}, "tu_1"),
                    _tool_use("Write", {"file_path": "tests/test_bar.py", "content": "test"}, "tu_2"),
                    _tool_use("Bash", {"command": "ls"}, "tu_3"),
                    _tool_use("Read", {"file_path": "src/baz.py"}, "tu_4"),
                ],
            ),
        )

    def test_where_name_filter(self):
        t = self._query_transcript()
        q = t.tool_uses.where(name="Edit")
        assert all(tu.name == "Edit" for tu in q.list())
        assert q.count() == 1

    def test_where_name_with_alias(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Execute", {"command": "ls"}, "tu_1"),
                    _tool_use("Bash", {"command": "pwd"}, "tu_2"),
                ],
            ),
        )
        q = t.tool_uses.where(name="Bash")
        assert q.count() == 2

    def test_where_file_glob(self):
        t = self._query_transcript()
        q = t.tool_uses.where(file="*.py")
        assert q.count() == 3

    def test_where_file_under(self):
        t = self._query_transcript()
        q = t.tool_uses.where(file_under="src/")
        assert q.count() == 2

    def test_where_is_error(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Bash", {"command": "fail"}, "tu_1"),
                    _tool_use("Bash", {"command": "ok"}, "tu_2"),
                ],
            ),
            _msg(
                "tool",
                [
                    _tool_result("tu_1", is_error=True),
                    _tool_result("tu_2", is_error=False),
                ],
            ),
        )
        q = t.tool_uses.with_errors.where(is_error=True)
        assert q.count() == 1
        assert q.first().command == "fail"

    def test_where_input_has(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Bash", {"command": "ls"}, "tu_1"),
                    _tool_use("Bash", {"command": "pwd"}, "tu_2"),
                ],
            ),
        )
        q = t.tool_uses.where(input_has={"command": "ls"})
        assert q.count() == 1

    def test_chaining(self):
        t = self._query_transcript()
        q = t.tool_uses.where(name="Edit|Write").where(file_under="src/")
        assert q.count() == 1
        assert q.first().name == "Edit"

    def test_terminal_count(self):
        t = self._query_transcript()
        assert t.tool_uses.where(name="Edit").count() == 1

    def test_terminal_any(self):
        t = self._query_transcript()
        assert t.tool_uses.where(name="Edit").any() is True
        assert t.tool_uses.where(name="Agent").any() is False

    def test_terminal_first(self):
        t = self._query_transcript()
        first = t.tool_uses.where(name="Edit|Write").first()
        assert first is not None
        assert first.name == "Edit"

    def test_terminal_last(self):
        t = self._query_transcript()
        last = t.tool_uses.where(name="Edit|Write").last()
        assert last is not None
        assert last.name == "Write"

    def test_terminal_list(self):
        t = self._query_transcript()
        items = t.tool_uses.where(name="Edit|Write").list()
        assert len(items) == 2

    def test_terminal_files(self):
        t = self._query_transcript()
        files = t.tool_uses.where(name="Edit|Write").files()
        assert len(files) == 2
        assert all(isinstance(f, File) for f in files)

    def test_empty_query_defaults(self):
        t = _make_transcript(_msg("user", "hello"))
        q = t.tool_uses.where(name="NonExistent")
        assert q.count() == 0
        assert q.any() is False
        assert q.first() is None
        assert q.last() is None
        assert q.list() == []
        assert q.files() == []


class TestTranscriptConstruction:
    def test_from_path_parses_jsonl(self, tmp_path: Path):
        p = tmp_path / "session.jsonl"
        lines = [
            json.dumps(_msg("user", "hello")),
            json.dumps(_msg("assistant", "world")),
        ]
        p.write_text("\n".join(lines))
        t = Transcript.from_path(p)
        assert len(t) == 2

    def test_from_path_handles_none(self):
        t = Transcript.from_path(None)
        assert len(t) == 0

    def test_from_path_handles_missing(self, tmp_path: Path):
        t = Transcript.from_path(tmp_path / "nonexistent.jsonl")
        assert len(t) == 0

    def test_from_messages(self):
        t = Transcript.from_messages([_msg("user", "hi")])
        assert len(t) == 1
        assert t.messages[0].type == "user"

    def test_from_simple_messages_single(self):
        t = Transcript.from_simple_messages([{"role": "assistant", "content": "hello"}])
        assert len(t) == 1
        assert t.messages[0].type == "assistant"
        assert t.messages[0].text == "hello"

    def test_from_simple_messages_multiple(self):
        t = Transcript.from_simple_messages([
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "4"},
        ])
        assert len(t) == 2
        assert t.messages[0].type == "user"
        assert t.messages[0].text == "What is 2+2?"
        assert t.messages[1].type == "assistant"
        assert t.messages[1].text == "4"

    def test_from_simple_messages_full_text(self):
        t = Transcript.from_simple_messages([
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ])
        assert "hello" in t.full_text
        assert "world" in t.full_text

    def test_from_parsed(self):
        msgs = [TranscriptMessage.from_raw(type="user", content="hi", raw={})]
        t = Transcript.from_parsed(msgs)
        assert len(t) == 1
        assert t.messages[0].type == "user"


class TestToolUseResultLinking:
    def test_tool_use_result_linked(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""}, "tu_1"),
                ],
            ),
            _msg(
                "tool",
                [
                    _tool_result("tu_1", is_error=False),
                ],
            ),
        )
        tu = t.tool_uses.with_errors[0]
        assert tu.result is not None
        assert tu.result.is_error is False

    def test_tool_use_result_none_when_no_match(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""}, "tu_1"),
                ],
            ),
        )
        tu = t.tool_uses[0]
        assert tu.result is None


class TestTranscriptSlicing:
    def _slicing_transcript(self) -> Transcript:
        return _make_transcript(
            _msg("user", "hello"),
            _msg("assistant", [_tool_use("Read", {"file_path": "a.py"}, "tu_1")]),
            _msg("assistant", [_tool_use("Edit", {"file_path": "b.py", "old_string": "", "new_string": ""}, "tu_2")]),
            _msg("assistant", "done editing"),
            _msg("user", "now test"),
        )

    def test_after_slicing(self):
        t = self._slicing_transcript()
        s = t.after("Edit")
        assert isinstance(s, TranscriptSlice)
        assert len(s) == 2

    def test_after_no_match(self):
        t = self._slicing_transcript()
        s = t.after("Agent")
        assert len(s) == 0

    def test_before_slicing(self):
        t = self._slicing_transcript()
        s = t.before("Edit")
        assert isinstance(s, TranscriptSlice)
        assert len(s) == 2

    def test_recent_slicing(self):
        t = self._slicing_transcript()
        s = t.recent(3)
        assert isinstance(s, TranscriptSlice)
        assert len(s) == 3

    def test_prior_slicing(self):
        t = self._slicing_transcript()
        s = t.prior()
        assert isinstance(s, TranscriptSlice)
        assert len(s) < len(t)


class TestTurn:
    def test_current_turn(self):
        t = _make_transcript(
            _msg("user", "first request"),
            _msg("assistant", "response 1"),
            _msg("user", "second request"),
            _msg("assistant", [_tool_use("Edit", {"file_path": "foo.py", "old_string": "", "new_string": ""}, "tu_1")]),
        )
        turn = t.current_turn
        assert isinstance(turn, Turn)
        assert turn.user_text == "second request"

    def test_turn_edited_files(self):
        t = _make_transcript(
            _msg("user", "edit stuff"),
            _msg(
                "assistant",
                [
                    _tool_use("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""}, "tu_1"),
                    _tool_use("Write", {"file_path": "b.py", "content": "x"}, "tu_2"),
                ],
            ),
        )
        turn = t.current_turn
        files = turn.edited_files
        assert isinstance(files, set)
        assert len(files) == 2

    def test_turn_start_idx(self):
        t = _make_transcript(
            _msg("user", "first"),
            _msg("assistant", "resp"),
            _msg("user", "second"),
            _msg("assistant", "resp2"),
        )
        turn = t.current_turn
        assert turn.start_idx == 3


TranscriptQuery = Callable[..., bool]
SubagentTranscriptFactory = Callable[..., Transcript]


@pytest.fixture
def transcript_with_subagent_tool_use(tmp_path: Path) -> SubagentTranscriptFactory:
    def make(
        sub_tool_use: dict[str, Any],
        main_tool_uses: list[dict[str, Any]] | None = None,
    ) -> Transcript:
        session_dir = tmp_path / "session"
        subagents_dir = session_dir / "session" / "subagents"
        subagents_dir.mkdir(parents=True)

        session_file = session_dir / "session.jsonl"
        if main_tool_uses:
            session_file.write_text(json.dumps(_msg("assistant", main_tool_uses)) + "\n")
        else:
            session_file.write_text(json.dumps(_msg("user", "hello")) + "\n")

        sub1 = subagents_dir / "sub1.jsonl"
        sub1.write_text(json.dumps(_msg("assistant", [sub_tool_use])) + "\n")

        return Transcript.from_path(session_file)

    return make


def _has_skill_codex(t: Transcript, **kw: bool) -> bool:
    return t.has_skill("codex", **kw)


def _has_command_codex_exec(t: Transcript, **kw: bool) -> bool:
    return t.has_command(r"codex exec", **kw)


def _has_edit_to_foo(t: Transcript, **kw: bool) -> bool:
    return t.has_edit_to("**/foo.py", **kw)


def _has_read_testing(t: Transcript, **kw: bool) -> bool:
    return t.has_read("TESTING.md", **kw)


def _has_tool_skill(t: Transcript, **kw: bool) -> bool:
    return t.has_tool("Skill", **kw)


class TestSubagents:
    def test_loads_from_filesystem(
        self,
        transcript_with_subagent_tool_use: SubagentTranscriptFactory,
    ) -> None:
        t = transcript_with_subagent_tool_use(_tool_use("Read", {"file_path": "docs/TESTING.md"}, "tu_sub"))
        assert len(t.subagents) == 1

    def test_skips_appledouble_sidecars(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "session"
        subagents_dir = session_dir / "session" / "subagents"
        subagents_dir.mkdir(parents=True)
        session_file = session_dir / "session.jsonl"
        session_file.write_text(json.dumps(_msg("user", "hello")) + "\n")
        sub = subagents_dir / "agent-1.jsonl"
        sub.write_text(json.dumps(_msg("assistant", [_tool_use("Bash", {"command": "codex exec -p 'hi'"}, "tu_sub")])) + "\n")
        (subagents_dir / "._agent-1.jsonl").write_bytes(b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X\xb0")

        t = Transcript.from_path(session_file)
        assert len(t.subagents) == 1
        assert t.has_command(r"codex exec", subagents=True) is True

    def test_from_path_tolerates_undecodable_file(self, tmp_path: Path) -> None:
        junk = tmp_path / "junk.jsonl"
        junk.write_bytes(b"\x00\x05\x16\x07\xb0")
        assert Transcript.from_path(junk).messages == []

    @pytest.mark.parametrize(
        ("tool_use", "query"),
        [
            pytest.param(_tool_use("Skill", {"skill": "codex"}, "tu_a"), _has_skill_codex, id="has_skill"),
            pytest.param(_tool_use("Bash", {"command": "codex exec -p 'hi'"}, "tu_b"), _has_command_codex_exec, id="has_command"),
            pytest.param(_tool_use("Edit", {"file_path": "src/foo.py", "old_string": "", "new_string": ""}, "tu_c"), _has_edit_to_foo, id="has_edit_to"),
            pytest.param(_tool_use("Read", {"file_path": "docs/TESTING.md"}, "tu_d"), _has_read_testing, id="has_read"),
            pytest.param(_tool_use("Skill", {"skill": "codex"}, "tu_e"), _has_tool_skill, id="has_tool"),
        ],
    )
    def test_query_recurses_into_subagents(
        self,
        transcript_with_subagent_tool_use: SubagentTranscriptFactory,
        tool_use: dict[str, Any],
        query: TranscriptQuery,
    ) -> None:
        t = transcript_with_subagent_tool_use(tool_use)
        assert query(t) is True
        assert query(t, subagents=False) is False

    def test_count_tools_ignores_subagents(
        self,
        transcript_with_subagent_tool_use: SubagentTranscriptFactory,
    ) -> None:
        main_uses: list[dict[str, Any]] = [_tool_use("EnterPlanMode", {}, f"tu_main_{i}") for i in range(2)]
        t = transcript_with_subagent_tool_use(_tool_use("EnterPlanMode", {}, "tu_sub_0"), main_uses)
        assert t.count_tools("EnterPlanMode") == 2

    def test_all_edits_under_ignores_subagents(
        self,
        transcript_with_subagent_tool_use: SubagentTranscriptFactory,
    ) -> None:
        main_uses: list[dict[str, Any]] = [
            _tool_use("Edit", {"file_path": "src/a.py", "old_string": "", "new_string": ""}, "tu_main"),
        ]
        t = transcript_with_subagent_tool_use(
            _tool_use("Edit", {"file_path": "tests/b.py", "old_string": "", "new_string": ""}, "tu_sub"),
            main_uses,
        )
        assert t.all_edits_under("src/") is True

    def test_has_read_no_subagents(self) -> None:
        t = _make_transcript(
            _msg("assistant", [_tool_use("Read", {"file_path": "README.md"}, "tu_1")]),
        )
        assert t.has_read("README.md") is True
        assert t.has_read("MISSING.md") is False


class TestUserSaid:
    def test_matches_user_text(self):
        t = _make_transcript(
            _msg("user", "I need help debugging this"),
            _msg("assistant", "Sure, let me help"),
        )
        assert t.user_said("help", "debug") is True

    def test_ignores_assistant_text(self):
        t = _make_transcript(
            _msg("user", "fix the bug"),
            _msg("assistant", "I need help figuring this out"),
        )
        assert t.user_said("help") is False

    def test_case_insensitive(self):
        t = _make_transcript(_msg("user", "Please HELP me"))
        assert t.user_said("help") is True


class TestAllEditsUnder:
    def test_all_under_prefix(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Edit", {"file_path": "src/a.py", "old_string": "", "new_string": ""}, "tu_1"),
                    _tool_use("Write", {"file_path": "src/b.py", "content": ""}, "tu_2"),
                ],
            ),
        )
        assert t.all_edits_under("src/") is True

    def test_not_all_under_prefix(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Edit", {"file_path": "src/a.py", "old_string": "", "new_string": ""}, "tu_1"),
                    _tool_use("Write", {"file_path": "tests/b.py", "content": ""}, "tu_2"),
                ],
            ),
        )
        assert t.all_edits_under("src/") is False

    def test_empty_edits_returns_false(self):
        t = _make_transcript(_msg("user", "hello"))
        assert t.all_edits_under("src/") is False


class TestFirstUserMessage:
    def test_returns_first_user_text(self):
        t = _make_transcript(
            _msg("user", "first question"),
            _msg("assistant", "answer"),
            _msg("user", "second question"),
        )
        assert t.first_user_message() == "first question"

    def test_returns_none_for_no_user_messages(self):
        t = _make_transcript(_msg("assistant", "monologue"))
        assert t.first_user_message() is None


class TestExtractFiles:
    def test_extract_all_files(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""}, "tu_1"),
                    _tool_use("Read", {"file_path": "b.py"}, "tu_2"),
                    _tool_use("Bash", {"command": "ls"}, "tu_3"),
                ],
            ),
        )
        files = t.extract_files()
        assert len(files) == 2

    def test_extract_with_tool_filter(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""}, "tu_1"),
                    _tool_use("Read", {"file_path": "b.py"}, "tu_2"),
                ],
            ),
        )
        files = t.extract_files(["Read"])
        assert len(files) == 1
        assert str(files[0]) == "b.py"


class TestEditOpsWriteOpsTaskOps:
    def test_edit_ops(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Edit", {"file_path": "a.py", "old_string": "old", "new_string": "new"}, "tu_1"),
                    _tool_use("Bash", {"command": "ls"}, "tu_2"),
                ],
            ),
        )
        ops = t.edit_ops()
        assert len(ops) == 1
        assert str(ops[0].file_path) == "a.py"

    def test_write_ops(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Write", {"file_path": "b.py", "content": "hello"}, "tu_1"),
                ],
            ),
        )
        ops = t.write_ops()
        assert len(ops) == 1

    def test_task_ops(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("TaskCreate", {"subject": "fix bug"}, "tu_1"),
                    _tool_use("TaskUpdate", {"taskId": "T-1", "status": "done"}, "tu_2"),
                    _tool_use("TaskGet", {"taskId": "T-1"}, "tu_3"),
                ],
            ),
        )
        ops = t.task_ops()
        assert len(ops) == 3
        assert ops[1].task_id == "T-1"
        assert ops[2].task_id == "T-1"


class TestCountFailuresLegacy:
    def test_counts_errors_from_tool_results(self):
        t = _make_transcript(
            _msg("assistant", [_tool_use("Bash", {"command": "fail"}, "tu_1")]),
            _msg("tool", [_tool_result("tu_1", is_error=True)]),
        )
        assert t.count_failures() == 1


class TestHasSkill:
    def test_has_skill_match(self):
        t = _make_transcript(
            _msg("assistant", [_tool_use("Skill", {"skill": "codex"}, "tu_1")]),
        )
        assert t.has_skill("codex") is True

    def test_has_skill_no_match(self):
        t = _make_transcript(
            _msg("assistant", [_tool_use("Skill", {"skill": "codex"}, "tu_1")]),
        )
        assert t.has_skill("test-runner") is False


class TestHasOverride:
    def test_true_when_token_present(self):
        t = _make_transcript(
            _msg("user", "OVERRIDE_TOKEN"),
            _msg("assistant", "ok"),
        )
        assert t.has_override("OVERRIDE_TOKEN") is True

    def test_false_after_invalidating_edit(self):
        t = _make_transcript(
            _msg("user", "OVERRIDE_TOKEN"),
            _msg("assistant", [_tool_use("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""}, "tu_1")]),
        )
        assert t.has_override("OVERRIDE_TOKEN", invalidated_by=["Edit"]) is False

    def test_false_when_token_absent(self):
        t = _make_transcript(
            _msg("user", "hello"),
        )
        assert t.has_override("OVERRIDE_TOKEN") is False


class TestAssistantText:
    def test_filters_assistant_messages(self):
        t = _make_transcript(
            _msg("user", "hello"),
            _msg("assistant", "response one"),
            _msg("user", "more"),
            _msg("assistant", "response two"),
        )
        text = t.assistant_text()
        assert "response one" in text
        assert "response two" in text
        assert "hello" not in text

    def test_truncates_per_message(self):
        t = _make_transcript(
            _msg("assistant", "x" * 1000),
        )
        text = t.assistant_text(max_per_msg=100)
        segments = text.split("\n---\n")
        assert all(len(s) <= 100 for s in segments)

    def test_limits_segments(self):
        msgs = [_msg("assistant", f"msg {i}") for i in range(20)]
        t = _make_transcript(*msgs)
        text = t.assistant_text(n=5)
        segments = text.split("\n---\n")
        assert len(segments) <= 5


class TestGetItem:
    def test_int_returns_message(self):
        t = _make_transcript(
            _msg("user", "hello"),
            _msg("assistant", "world"),
        )
        result = t[0]
        assert isinstance(result, TranscriptMessage)
        assert result.type == "user"

    def test_slice_returns_transcript_slice(self):
        t = _make_transcript(
            _msg("user", "hello"),
            _msg("assistant", "world"),
            _msg("user", "bye"),
        )
        result = t[0:2]
        assert isinstance(result, TranscriptSlice)
        assert len(result) == 2


class TestStrOutput:
    def test_produces_xml(self):
        t = _make_transcript(
            _msg("user", "hello"),
            _msg(
                "assistant",
                [
                    _tool_use("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""}, "tu_1"),
                    _tool_use("Write", {"file_path": "b.py", "content": ""}, "tu_2"),
                    _tool_use("Bash", {"command": "ls"}, "tu_3"),
                ],
            ),
            _msg("assistant", "done editing"),
            _msg("user", "thanks"),
            _msg("assistant", "you're welcome"),
        )
        s = str(t)
        assert "<transcript>" in s
        assert "<metadata>" in s
        assert "<messages>5</messages>" in s
        assert "<tool_uses>" in s
        assert "<edits>" in s
        assert "<errors>" in s
        assert "<tail>" in s
        assert "</transcript>" in s

    def test_tail_truncation(self):
        msgs = [_msg("assistant", f"line {i}") for i in range(300)]
        t = _make_transcript(*msgs)
        s = str(t)
        tail_start = s.index("<tail>") + len("<tail>\n")
        tail_end = s.index("\n</tail>")
        tail_content = s[tail_start:tail_end]
        tail_lines = [line for line in tail_content.splitlines() if line.strip()]
        assert len(tail_lines) <= 200

    def test_path_in_metadata(self, tmp_path: Path):
        p = tmp_path / "session.jsonl"
        p.write_text(json.dumps(_msg("user", "hi")) + "\n")
        t = Transcript.from_path(p)
        s = str(t)
        assert f"<path>{p}</path>" in s


class TestCommands:
    def test_commands_property(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Bash", {"command": "echo hello"}, "tu_1"),
                    _tool_use("Bash", {"command": "ls -la"}, "tu_2"),
                ],
            ),
        )
        cmds = t.commands
        assert len(cmds) >= 2

    def test_has_command_regex(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Bash", {"command": "uv run mtest run tests/"}, "tu_1"),
                ],
            ),
        )
        assert t.has_command(r"uv\s+run\s+mtest") is True
        assert t.has_command(r"npm\s+test") is False


class TestCountTools:
    def test_count_with_alias(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Execute", {"command": "ls"}, "tu_1"),
                    _tool_use("Bash", {"command": "pwd"}, "tu_2"),
                    _tool_use("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""}, "tu_3"),
                ],
            ),
        )
        assert t.count_tools("Bash") == 2
        assert t.count_tools("Edit") == 1


class TestHasEditTo:
    def test_glob_matching(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Edit", {"file_path": "src/foo.py", "old_string": "", "new_string": ""}, "tu_1"),
                ],
            ),
        )
        assert t.has_edit_to("*.py") is True
        assert t.has_edit_to("*.js") is False


class TestFullText:
    def test_full_text_joins(self):
        t = _make_transcript(
            _msg("user", "hello"),
            _msg("assistant", "world"),
        )
        ft = t.full_text
        assert "hello" in ft
        assert "world" in ft


class TestCountFailuresRegression:
    def test_counts_from_tool_result_is_error(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Bash", {"command": "ls"}, "tu_1"),
                    _tool_use("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""}, "tu_2"),
                ],
            ),
            _msg(
                "tool",
                [
                    _tool_result("tu_1", is_error=True),
                    _tool_result("tu_2", is_error=False),
                ],
            ),
        )
        assert t.count_failures() == 1

    def test_zero_when_no_errors(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Bash", {"command": "echo ok"}, "tu_1"),
                ],
            ),
            _msg(
                "tool",
                [
                    _tool_result("tu_1", is_error=False),
                ],
            ),
        )
        assert t.count_failures() == 0

    def test_multiple_errors(self):
        t = _make_transcript(
            _msg("assistant", [_tool_use("Bash", {"command": "bad"}, "tu_1")]),
            _msg("tool", [_tool_result("tu_1", is_error=True)]),
            _msg("assistant", [_tool_use("Bash", {"command": "worse"}, "tu_2")]),
            _msg("tool", [_tool_result("tu_2", is_error=True)]),
        )
        assert t.count_failures() == 2

    def test_empty_transcript(self):
        t = _make_transcript()
        assert t.count_failures() == 0

    def test_no_string_search_false_positive(self):
        t = _make_transcript(
            _msg("assistant", 'The text "is_error":true was discussed'),
            _msg("assistant", "FAILED attempt discussed in meeting"),
            _msg("assistant", "Traceback info mentioned in docs"),
        )
        assert t.count_failures() == 0


class TestCommandAliases:
    def test_commands_includes_execute_tool(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Execute", {"command": "echo hello"}, "tu_1"),
                ],
            ),
        )
        assert len(t.commands) == 1
        assert "echo" in str(t.commands[0])

    def test_commands_includes_both_bash_and_execute(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Bash", {"command": "ls -la"}, "tu_1"),
                ],
            ),
            _msg(
                "assistant",
                [
                    _tool_use("Execute", {"command": "pwd"}, "tu_2"),
                ],
            ),
        )
        assert len(t.commands) == 2

    def test_has_command_matches_execute_tool(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Execute", {"command": "uv run mtest run tests/"}, "tu_1"),
                ],
            ),
        )
        assert t.has_command(r"uv\s+run\s+mtest")

    def test_has_command_no_match_without_alias(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""}, "tu_1"),
                ],
            ),
        )
        assert not t.has_command(r"uv\s+run")


class TestMcpToolNameMatching:
    def test_bare_query_matches_mcp_variant(self):
        t = _make_transcript(
            _msg("assistant", [_tool_use("mcp__conductor__AskUserQuestion", {"q": "?"}, "tu_1")]),
        )
        assert t.has_tool("AskUserQuestion")

    def test_bare_query_matches_arbitrary_mcp_server(self):
        t = _make_transcript(
            _msg("assistant", [_tool_use("mcp__plugin_sentry_sentry__search_issues", {}, "tu_1")]),
        )
        assert t.has_tool("search_issues")

    def test_bare_query_matches_both_native_and_mcp(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("AskUserQuestion", {}, "tu_1"),
                    _tool_use("mcp__conductor__AskUserQuestion", {}, "tu_2"),
                    _tool_use("mcp__factory__AskUserQuestion", {}, "tu_3"),
                ],
            ),
        )
        assert t.tool_uses.where(name="AskUserQuestion").count() == 3

    def test_qualified_mcp_query_only_matches_exact(self):
        t = _make_transcript(
            _msg(
                "assistant",
                [
                    _tool_use("AskUserQuestion", {}, "tu_1"),
                    _tool_use("mcp__conductor__AskUserQuestion", {}, "tu_2"),
                    _tool_use("mcp__factory__AskUserQuestion", {}, "tu_3"),
                ],
            ),
        )
        q = t.tool_uses.where(name="mcp__conductor__AskUserQuestion")
        assert q.count() == 1
        assert q.first().name == "mcp__conductor__AskUserQuestion"

    def test_no_false_positive_on_unrelated_mcp(self):
        t = _make_transcript(
            _msg("assistant", [_tool_use("mcp__conductor__GetTerminalOutput", {}, "tu_1")]),
        )
        assert not t.has_tool("AskUserQuestion")

    def test_after_works_with_mcp_variant(self):
        t = _make_transcript(
            _msg("assistant", [_tool_use("Read", {"file_path": "a.py"}, "tu_1")]),
            _msg("assistant", [_tool_use("mcp__conductor__AskUserQuestion", {}, "tu_2")]),
            _msg("assistant", [_tool_use("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""}, "tu_3")]),
        )
        sliced = t.after("AskUserQuestion")
        assert sliced.tool_uses.where(name="Edit").count() == 1


def _block_sig(b: ContentBlock) -> tuple[Any, ...]:
    match b:
        case TextBlock():
            return ("text", b.text)
        case ToolUseBlock():
            return ("tool_use", b.name, b.id, b.input)
        case ToolResult():
            return ("tool_result", b.tool_use_id, b.is_error, b.is_async)


def _structure(t: Transcript) -> list[tuple[str, list[tuple[Any, ...]]]]:
    return [(msg.type, [_block_sig(b) for b in msg.content]) for msg in t.messages]


class TestConstructionEquivalence:
    """from_path (core-routed) and from_messages (synthetic) must agree on block structure."""

    def test_from_path_matches_from_messages(self, tmp_path: Path) -> None:
        msgs = [
            raw_text("user", "fix the bug"),
            raw_assistant(_tool_use("Read", {"file_path": "/x"}, "tu_1")),
            raw_tool_result("tu_1", "file contents", is_error=False),
            *async_agent_launch(id="tu_2"),
            raw_text("assistant", "done"),
        ]

        synthetic = Transcript.from_messages(msgs)

        p = tmp_path / "session.jsonl"
        p.write_text("\n".join(json.dumps(m) for m in msgs) + "\n")
        core_routed = Transcript.from_path(p)

        assert _structure(core_routed) == _structure(synthetic)

    def test_async_tool_result_flows_through_both_paths(self, tmp_path: Path) -> None:
        msgs = list(async_agent_launch(id="tu_async"))

        synthetic = Transcript.from_messages(msgs)
        p = tmp_path / "async.jsonl"
        p.write_text("\n".join(json.dumps(m) for m in msgs) + "\n")
        core_routed = Transcript.from_path(p)

        for t in (synthetic, core_routed):
            [result] = [tr for msg in t.messages for tr in msg.tool_results]
            assert result.is_async is True
