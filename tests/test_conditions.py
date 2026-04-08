from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock

from captain_hook.events import (
    BaseHookEvent,
    PreToolUseEvent,
    StopEvent,
    UserPromptSubmitEvent,
)
from captain_hook.types import (
    Agent,
    Command,
    Content,
    Event,
    FilePath,
    HookSpec,
    InPlanMode,
    RanCommand,
    ReadFile,
    TestFile,
    Tool,
    TouchedFile,
    UsedSkill,
    tokens_to_regex,
)


def make_event(
    event_cls: type[BaseHookEvent],
    raw: dict[str, Any] | None = None,
    ctx: Any = None,
) -> BaseHookEvent:
    return event_cls(_raw=raw or {}, ctx=ctx)


def make_tool_event(
    tool_name: str,
    tool_input: dict[str, Any] | None = None,
    ctx: Any = None,
) -> PreToolUseEvent:
    raw: dict[str, Any] = {"tool_name": tool_name, "tool_input": tool_input or {}}
    return PreToolUseEvent(_raw=raw, ctx=ctx)


def make_transcript_ctx(
    has_skill_result: bool = False,
    has_read_result: bool = False,
    has_edit_to_result: bool = False,
    has_command_result: bool = False,
    count_tools_map: dict[str, int] | None = None,
) -> Any:
    transcript = MagicMock()
    transcript.has_skill = MagicMock(return_value=has_skill_result)
    transcript.has_read = MagicMock(return_value=has_read_result)
    transcript.has_edit_to = MagicMock(return_value=has_edit_to_result)
    transcript.has_command = MagicMock(return_value=has_command_result)
    transcript.count_tools = MagicMock(side_effect=lambda t: (count_tools_map or {}).get(t, 0))
    ctx = MagicMock()
    ctx.transcript = transcript
    return ctx


# --- VAL-COND-001: Tool condition matches by tool_name (pipe-separated OR) ---


class TestToolCondition:
    def test_tool_matches_edit(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Edit")
        assert check_condition(Tool("Edit|Write"), evt) is True

    def test_tool_matches_write(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Write")
        assert check_condition(Tool("Edit|Write"), evt) is True

    def test_tool_rejects_bash(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Bash")
        assert check_condition(Tool("Edit|Write"), evt) is False

    # --- VAL-COND-002: Tool condition expands aliases ---

    def test_tool_bash_matches_execute(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Execute")
        assert check_condition(Tool("Bash"), evt) is True

    def test_tool_execute_matches_bash(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Bash")
        assert check_condition(Tool("Execute"), evt) is True

    def test_tool_write_matches_create(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Create")
        assert check_condition(Tool("Write"), evt) is True

    def test_tool_create_matches_write(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Write")
        assert check_condition(Tool("Create"), evt) is True

    def test_tool_agent_matches_task(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Task")
        assert check_condition(Tool("Agent"), evt) is True

    def test_tool_task_matches_agent(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Agent")
        assert check_condition(Tool("Task"), evt) is True

    def test_tool_webfetch_matches_fetchurl(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("FetchUrl")
        assert check_condition(Tool("WebFetch"), evt) is True

    def test_tool_fetchurl_matches_webfetch(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("WebFetch")
        assert check_condition(Tool("FetchUrl"), evt) is True

    def test_tool_exitplanmode_matches_exitspecmode(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("ExitSpecMode")
        assert check_condition(Tool("ExitPlanMode"), evt) is True

    def test_tool_exitspecmode_matches_exitplanmode(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("ExitPlanMode")
        assert check_condition(Tool("ExitSpecMode"), evt) is True

    # --- VAL-COND-003: Tool condition returns False when tool_name is None ---

    def test_tool_returns_false_for_stop_event(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_event(StopEvent)
        assert check_condition(Tool("Bash"), evt) is False

    def test_tool_returns_false_for_user_prompt_event(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_event(UserPromptSubmitEvent)
        assert check_condition(Tool("Bash"), evt) is False


# --- VAL-COND-004: FilePath condition matches against file path (fnmatch OR) ---


class TestFilePathCondition:
    def test_filepath_matches_py(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""})
        assert check_condition(FilePath("*.py", "*.ts"), evt) is True

    def test_filepath_matches_ts(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Edit", {"file_path": "b.ts", "old_string": "", "new_string": ""})
        assert check_condition(FilePath("*.py", "*.ts"), evt) is True

    def test_filepath_rejects_rs(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Edit", {"file_path": "c.rs", "old_string": "", "new_string": ""})
        assert check_condition(FilePath("*.py", "*.ts"), evt) is False

    # --- VAL-COND-005: FilePath condition returns False when no file ---

    def test_filepath_returns_false_when_no_file(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Bash", {"command": "echo hello"})
        assert check_condition(FilePath("*.py"), evt) is False

    def test_filepath_returns_false_for_stop_event(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_event(StopEvent)
        assert check_condition(FilePath("*.py"), evt) is False


# --- VAL-COND-006: Command condition matches against primary command (regex) ---


class TestCommandCondition:
    def test_command_matches_git_push_force(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Bash", {"command": "git push --force origin"})
        assert check_condition(Command(r"git\s+push\s+--force"), evt) is True

    # --- VAL-COND-007: Command condition does not match heredoc/echo content ---

    def test_command_rejects_echo_git_push(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Bash", {"command": 'echo "git push --force"'})
        assert check_condition(Command(r"git\s+push\s+--force"), evt) is False

    # --- VAL-COND-008: Command condition returns False when no command_line ---

    def test_command_returns_false_for_edit_event(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""})
        assert check_condition(Command(r"git"), evt) is False

    def test_command_returns_false_for_stop_event(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_event(StopEvent)
        assert check_condition(Command(r"git"), evt) is False

    # --- VAL-COND-027: Command condition uses re.match (start-anchored), not re.search ---

    def test_command_uses_match_not_search(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Bash", {"command": "git push"})
        assert check_condition(Command(r"push"), evt) is False
        assert check_condition(Command(r"git\s+push"), evt) is True


# --- VAL-COND-009: Content condition matches against content (regex, MULTILINE) ---


class TestContentCondition:
    def test_content_matches_import_pdb(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event(
            "Edit",
            {
                "file_path": "a.py",
                "old_string": "",
                "new_string": "import pdb; pdb.set_trace()",
            },
        )
        assert check_condition(Content(r"import\s+pdb"), evt) is True

    def test_content_rejects_import_logging(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event(
            "Edit",
            {
                "file_path": "a.py",
                "old_string": "",
                "new_string": "import logging",
            },
        )
        assert check_condition(Content(r"import\s+pdb"), evt) is False

    # --- VAL-COND-010: Content condition returns False when no content ---

    def test_content_returns_false_for_bash_event(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Bash", {"command": "echo hi"})
        assert check_condition(Content(r"import\s+pdb"), evt) is False

    def test_content_returns_false_for_stop_event(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_event(StopEvent)
        assert check_condition(Content(r"import"), evt) is False


# --- VAL-COND-011: Agent condition matches agent_type (pipe-separated OR) ---


class TestAgentCondition:
    def test_agent_matches_cleanup(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Agent", {"subagent_type": "cleanup", "prompt": "run cleanup"})
        assert check_condition(Agent("cleanup|quality-gate"), evt) is True

    def test_agent_matches_quality_gate(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Agent", {"subagent_type": "quality-gate", "prompt": "review code"})
        assert check_condition(Agent("cleanup|quality-gate"), evt) is True

    def test_agent_rejects_non_matching(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Agent", {"subagent_type": "worker", "prompt": "do work"})
        assert check_condition(Agent("cleanup|quality-gate"), evt) is False

    # --- VAL-COND-012: Agent condition returns False when no agent_type ---

    def test_agent_returns_false_for_bash(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Bash", {"command": "echo hi"})
        assert check_condition(Agent("cleanup"), evt) is False

    def test_agent_returns_false_for_stop(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_event(StopEvent)
        assert check_condition(Agent("cleanup"), evt) is False


# --- VAL-COND-013: TestFile condition matches when file is a test file ---


class TestTestFileCondition:
    def test_testfile_matches_test_file(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event(
            "Edit",
            {
                "file_path": "tests/test_foo.py",
                "old_string": "",
                "new_string": "",
            },
        )
        assert check_condition(TestFile(), evt) is True

    def test_testfile_rejects_non_test_file(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event(
            "Edit",
            {
                "file_path": "bioqa/main.py",
                "old_string": "",
                "new_string": "",
            },
        )
        assert check_condition(TestFile(), evt) is False

    # --- VAL-COND-014: TestFile condition returns False when no file ---

    def test_testfile_returns_false_for_bash(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_tool_event("Bash", {"command": "echo hi"})
        assert check_condition(TestFile(), evt) is False

    def test_testfile_returns_false_for_stop(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_event(StopEvent)
        assert check_condition(TestFile(), evt) is False


# --- VAL-COND-015: UsedSkill condition checks transcript (pipe-separated OR) ---


class TestUsedSkillCondition:
    def test_usedskill_matches_codex(self) -> None:
        from captain_hook.conditions import check_condition

        ctx = make_transcript_ctx(has_skill_result=True)
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx)
        assert check_condition(UsedSkill("codex|agent-browser"), evt) is True
        ctx.transcript.has_skill.assert_called_once_with("codex", "agent-browser")

    def test_usedskill_rejects_non_matching(self) -> None:
        from captain_hook.conditions import check_condition

        ctx = make_transcript_ctx(has_skill_result=False)
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx)
        assert check_condition(UsedSkill("codex|agent-browser"), evt) is False


# --- VAL-COND-016: ReadFile condition checks transcript (multi-pattern) ---


class TestReadFileCondition:
    def test_readfile_matches_styleguide(self) -> None:
        from captain_hook.conditions import check_condition

        ctx = make_transcript_ctx(has_read_result=True)
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx)
        assert check_condition(ReadFile("STYLEGUIDE.md", "docs/styleguide/"), evt) is True

    def test_readfile_rejects_non_matching(self) -> None:
        from captain_hook.conditions import check_condition

        ctx = make_transcript_ctx(has_read_result=False)
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx)
        assert check_condition(ReadFile("STYLEGUIDE.md"), evt) is False


# --- VAL-COND-017: TouchedFile condition checks transcript for edits ---


class TestTouchedFileCondition:
    def test_touchedfile_matches_edit(self) -> None:
        from captain_hook.conditions import check_condition

        ctx = make_transcript_ctx(has_edit_to_result=True)
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx)
        assert check_condition(TouchedFile("**/www/src/**"), evt) is True
        ctx.transcript.has_edit_to.assert_called_once_with("**/www/src/**")

    def test_touchedfile_rejects_non_matching(self) -> None:
        from captain_hook.conditions import check_condition

        ctx = make_transcript_ctx(has_edit_to_result=False)
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx)
        assert check_condition(TouchedFile("**/www/src/**"), evt) is False


# --- VAL-COND-018: RanCommand condition checks transcript for commands ---


class TestRanCommandCondition:
    def test_rancommand_matches(self) -> None:
        from captain_hook.conditions import check_condition

        ctx = make_transcript_ctx(has_command_result=True)
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx)
        assert check_condition(RanCommand(r"codex exec"), evt) is True
        ctx.transcript.has_command.assert_called_once_with("codex exec")

    def test_rancommand_rejects(self) -> None:
        from captain_hook.conditions import check_condition

        ctx = make_transcript_ctx(has_command_result=False)
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx)
        assert check_condition(RanCommand(r"codex exec"), evt) is False


# --- VAL-COND-019: InPlanMode condition checks plan mode balance ---


class TestInPlanModeCondition:
    def test_inplanmode_matches_when_more_enter(self) -> None:
        from captain_hook.conditions import check_condition

        ctx = make_transcript_ctx(count_tools_map={"EnterPlanMode": 2, "ExitPlanMode": 1})
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx)
        assert check_condition(InPlanMode(), evt) is True

    def test_inplanmode_rejects_when_equal(self) -> None:
        from captain_hook.conditions import check_condition

        ctx = make_transcript_ctx(count_tools_map={"EnterPlanMode": 1, "ExitPlanMode": 1})
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx)
        assert check_condition(InPlanMode(), evt) is False

    def test_inplanmode_rejects_when_zero(self) -> None:
        from captain_hook.conditions import check_condition

        ctx = make_transcript_ctx(count_tools_map={})
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx)
        assert check_condition(InPlanMode(), evt) is False


# --- VAL-COND-020: only_if semantics: AND (all must match) ---


class TestOnlyIfSemantics:
    def test_only_if_all_must_match(self) -> None:
        from captain_hook.conditions import matches_conditions

        spec = HookSpec(
            events=Event.PreToolUse,
            only_if=(Tool("Bash"), Command(r"git\s+push")),
        )
        evt_match = make_tool_event("Bash", {"command": "git push origin"})
        assert matches_conditions(spec, evt_match) is True

        evt_wrong_cmd = make_tool_event("Bash", {"command": "echo hello"})
        assert matches_conditions(spec, evt_wrong_cmd) is False

    def test_only_if_one_false_fails(self) -> None:
        from captain_hook.conditions import matches_conditions

        spec = HookSpec(
            events=Event.PreToolUse,
            only_if=(Tool("Edit"), Command(r"git")),
        )
        evt = make_tool_event("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""})
        assert matches_conditions(spec, evt) is False


# --- VAL-COND-021: skip_if semantics: OR (any causes skip) ---


class TestSkipIfSemantics:
    def test_skip_if_any_causes_skip(self) -> None:
        from captain_hook.conditions import matches_conditions

        spec = HookSpec(
            events=Event.PreToolUse,
            skip_if=(TestFile(), FilePath("*.lock")),
        )
        evt_test = make_tool_event(
            "Edit",
            {
                "file_path": "tests/test_x.py",
                "old_string": "",
                "new_string": "",
            },
        )
        assert matches_conditions(spec, evt_test) is False

        evt_lock = make_tool_event(
            "Edit",
            {
                "file_path": "package.lock",
                "old_string": "",
                "new_string": "",
            },
        )
        assert matches_conditions(spec, evt_lock) is False

    def test_skip_if_none_true_passes(self) -> None:
        from captain_hook.conditions import matches_conditions

        spec = HookSpec(
            events=Event.PreToolUse,
            skip_if=(TestFile(), FilePath("*.lock")),
        )
        evt = make_tool_event(
            "Edit",
            {
                "file_path": "bioqa/main.py",
                "old_string": "",
                "new_string": "",
            },
        )
        assert matches_conditions(spec, evt) is True


# --- VAL-COND-022: only_if AND skip_if combined ---


class TestCombinedConditions:
    def test_only_if_and_skip_if_combined(self) -> None:
        from captain_hook.conditions import matches_conditions

        spec = HookSpec(
            events=Event.PreToolUse,
            only_if=(Tool("Edit"),),
            skip_if=(TestFile(),),
        )
        evt_non_test = make_tool_event(
            "Edit",
            {
                "file_path": "bioqa/main.py",
                "old_string": "",
                "new_string": "",
            },
        )
        assert matches_conditions(spec, evt_non_test) is True

        evt_test = make_tool_event(
            "Edit",
            {
                "file_path": "tests/test_x.py",
                "old_string": "",
                "new_string": "",
            },
        )
        assert matches_conditions(spec, evt_test) is False


# --- VAL-COND-023: Condition on non-tool event gracefully returns False ---


class TestNonToolEventConditions:
    def test_tool_condition_on_stop_is_false(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_event(StopEvent)
        assert check_condition(Tool("Bash"), evt) is False

    def test_command_condition_on_stop_is_false(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_event(StopEvent)
        assert check_condition(Command(r"git"), evt) is False

    def test_content_condition_on_stop_is_false(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_event(StopEvent)
        assert check_condition(Content(r"import"), evt) is False

    def test_filepath_condition_on_stop_is_false(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_event(StopEvent)
        assert check_condition(FilePath("*.py"), evt) is False

    def test_conditions_on_user_prompt_graceful(self) -> None:
        from captain_hook.conditions import check_condition

        evt = make_event(UserPromptSubmitEvent)
        assert check_condition(Tool("Bash"), evt) is False
        assert check_condition(Command(r"git"), evt) is False
        assert check_condition(Content(r"import"), evt) is False
        assert check_condition(FilePath("*.py"), evt) is False
        assert check_condition(TestFile(), evt) is False
        assert check_condition(Agent("cleanup"), evt) is False


# --- VAL-COND-024: tokens_to_regex converts token lists to regex ---


class TestTokensToRegex:
    def test_simple_tokens(self) -> None:
        result = tokens_to_regex(["git", "stash"])
        assert re.match(result, "git stash pop")
        assert re.match(result, "git  stash")

    def test_wildcard(self) -> None:
        result = tokens_to_regex(["git", "*"])
        assert re.match(result, "git commit -m x")
        assert re.match(result, "git push")

    def test_pipe_alternation(self) -> None:
        result = tokens_to_regex(["git", "commit|split"])
        assert re.match(result, "git commit -m x")
        assert re.match(result, "git split")
        assert not re.match(result, "git push")

    # --- VAL-COND-028: tokens_to_regex with empty list returns empty string ---

    def test_empty_list_returns_empty_string(self) -> None:
        assert tokens_to_regex([]) == ""


# --- VAL-COND-025: only_if=() (empty) means always matches ---


class TestEmptyConditions:
    def test_empty_only_if_always_matches(self) -> None:
        from captain_hook.conditions import matches_conditions

        spec = HookSpec(events=Event.PreToolUse, only_if=())
        evt = make_tool_event("Bash", {"command": "echo hi"})
        assert matches_conditions(spec, evt) is True

    # --- VAL-COND-026: skip_if=() (empty) means never skipped ---

    def test_empty_skip_if_never_skipped(self) -> None:
        from captain_hook.conditions import matches_conditions

        spec = HookSpec(events=Event.PreToolUse, skip_if=())
        evt = make_tool_event("Bash", {"command": "echo hi"})
        assert matches_conditions(spec, evt) is True

    def test_both_empty_conditions_matches(self) -> None:
        from captain_hook.conditions import matches_conditions

        spec = HookSpec(events=Event.PreToolUse, only_if=(), skip_if=())
        evt = make_tool_event("Bash", {"command": "echo hi"})
        assert matches_conditions(spec, evt) is True


# --- VAL-COND-029: CustomCondition protocol dispatches to check() ---


class TestCustomCondition:
    def test_custom_condition_returns_true(self) -> None:
        from dataclasses import dataclass

        from captain_hook.conditions import check_condition

        @dataclass(frozen=True, slots=True)
        class AlwaysTrue:
            def check(self, evt: BaseHookEvent) -> bool:
                return True

        evt = make_tool_event("Bash", {"command": "echo"})
        assert check_condition(AlwaysTrue(), evt) is True

    def test_custom_condition_returns_false(self) -> None:
        from dataclasses import dataclass

        from captain_hook.conditions import check_condition

        @dataclass(frozen=True, slots=True)
        class AlwaysFalse:
            def check(self, evt: BaseHookEvent) -> bool:
                return False

        evt = make_tool_event("Bash", {"command": "echo"})
        assert check_condition(AlwaysFalse(), evt) is False

    def test_custom_condition_receives_event(self) -> None:
        from dataclasses import dataclass

        from captain_hook.conditions import check_condition

        @dataclass(frozen=True, slots=True)
        class CheckToolName:
            expected: str

            def check(self, evt: BaseHookEvent) -> bool:
                return evt.tool_name == self.expected

        evt = make_tool_event("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""})
        assert check_condition(CheckToolName("Edit"), evt) is True
        assert check_condition(CheckToolName("Bash"), evt) is False

    def test_custom_condition_in_skip_if(self) -> None:
        from dataclasses import dataclass

        from captain_hook.conditions import matches_conditions

        @dataclass(frozen=True, slots=True)
        class AlwaysTrue:
            def check(self, evt: BaseHookEvent) -> bool:
                return True

        spec = HookSpec(
            events=Event.PreToolUse,
            skip_if=(AlwaysTrue(),),
        )
        evt = make_tool_event("Bash", {"command": "echo"})
        assert matches_conditions(spec, evt) is False

    def test_non_protocol_object_returns_false(self) -> None:
        from dataclasses import dataclass

        from captain_hook.conditions import check_condition

        @dataclass(frozen=True, slots=True)
        class NotACondition:
            value: int

        evt = make_tool_event("Bash", {"command": "echo"})
        assert check_condition(NotACondition(42), evt) is False  # type: ignore[arg-type]


# --- VAL-COND-030: CustomCondition reads event _raw for filesystem-based checks ---


class TestCustomConditionFilesystem:
    def test_settings_based_condition(self, tmp_path: Any) -> None:
        import json
        from dataclasses import dataclass
        from pathlib import Path

        from captain_hook.conditions import check_condition

        @dataclass(frozen=True, slots=True)
        class HasTag:
            tag: str

            def check(self, evt: BaseHookEvent) -> bool:
                if not (tp := evt._raw.get("transcript_path")):
                    return False
                p = Path(tp)
                settings = p.parent / f"{p.stem}.settings.json"
                if not settings.exists():
                    return False
                tags = json.loads(settings.read_text()).get("tags", [])
                return any(t.get("name") == self.tag for t in tags)

        transcript = tmp_path / "session.jsonl"
        transcript.touch()
        settings = tmp_path / "session.settings.json"
        settings.write_text(json.dumps({"tags": [{"name": "mission-worker"}]}))

        evt = make_event(StopEvent, raw={"transcript_path": str(transcript)})
        assert check_condition(HasTag("mission-worker"), evt) is True
        assert check_condition(HasTag("other"), evt) is False

    def test_settings_based_condition_no_file(self) -> None:
        from dataclasses import dataclass
        from pathlib import Path

        from captain_hook.conditions import check_condition

        @dataclass(frozen=True, slots=True)
        class HasTag:
            tag: str

            def check(self, evt: BaseHookEvent) -> bool:
                if not (tp := evt._raw.get("transcript_path")):
                    return False
                p = Path(tp)
                settings = p.parent / f"{p.stem}.settings.json"
                return settings.exists()

        evt = make_event(StopEvent, raw={"transcript_path": "/nonexistent/session.jsonl"})
        assert check_condition(HasTag("anything"), evt) is False

    def test_settings_based_condition_no_transcript_path(self) -> None:
        from dataclasses import dataclass

        from captain_hook.conditions import check_condition

        @dataclass(frozen=True, slots=True)
        class HasTag:
            tag: str

            def check(self, evt: BaseHookEvent) -> bool:
                return bool(evt._raw.get("transcript_path"))

        evt = make_event(StopEvent)
        assert check_condition(HasTag("anything"), evt) is False
