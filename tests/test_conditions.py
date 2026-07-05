from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from captain_hook.app import on
from captain_hook.conditions import check_condition, matches_conditions, workflow_opt_matches
from captain_hook.context import load_transcript
from captain_hook.events import (
    BaseHookEvent,
    PermissionRequestEvent,
    PreToolUseEvent,
    StopEvent,
    UserPromptSubmitEvent,
)
from captain_hook.primitives.commands import block_command_pattern
from captain_hook.types import (
    Agent,
    And,
    Command,
    Content,
    Event,
    FilePath,
    FromSubagent,
    HookSpec,
    InPlanMode,
    Not,
    Or,
    Pattern,
    RanCommand,
    ReadFile,
    Runs,
    SkipPermissions,
    TCondition,
    TestFile,
    Tool,
    ToolInput,
    TouchedFile,
    UsedSkill,
    Waiting,
    WorkflowScript,
)
from tests.helpers import (
    assistant_msg,
    async_agent_launch,
    build_ctx,
    make_event,
    make_messages_ctx,
    make_transcript_ctx,
    raw_assistant,
    raw_notification,
    raw_text,
    raw_tool_msg,
    raw_tool_result,
    raw_tool_use,
    text_msg,
    waiting_evt,
    workflow_launch,
)


def make_tool_event(
    tool_name: str,
    tool_input: dict[str, Any] | None = None,
    ctx: Any = None,
    permission_mode: str | None = None,
) -> PreToolUseEvent:
    raw: dict[str, Any] = {"tool_name": tool_name, "tool_input": tool_input or {}}
    if permission_mode is not None:
        raw["permission_mode"] = permission_mode
    return PreToolUseEvent(_raw=raw, ctx=ctx or build_ctx())


class TestPatternCondition:
    @pytest.mark.parametrize(
        ("cond", "evt", "expected"),
        [
            pytest.param(
                Pattern("print($$$)"),
                make_tool_event("Edit", {"file_path": "a.py", "old_string": "", "new_string": "print('x')\n"}),
                True,
                id="string_pattern_matches",
            ),
            pytest.param(
                Pattern("print($$$)"),
                make_tool_event("Edit", {"file_path": "a.py", "old_string": "", "new_string": "logger.info('x')\n"}),
                False,
                id="string_pattern_no_match",
            ),
            pytest.param(
                Pattern("print($$$)"),
                make_tool_event("Edit", {"file_path": "a.py", "old_string": "", "new_string": "s = 'print(x)'\n"}),
                False,
                id="ignores_match_inside_string_literal",
            ),
            pytest.param(
                Pattern("panic($$$)"),
                make_tool_event(
                    "Edit", {"file_path": "main.go", "old_string": "", "new_string": 'func f() { panic("x") }\n'}
                ),
                True,
                id="language_inferred_from_extension",
            ),
            pytest.param(
                Pattern("panic($$$)", lang="go"),
                make_tool_event(
                    "Edit", {"file_path": "x.txt", "old_string": "", "new_string": 'func f() { panic("x") }\n'}
                ),
                True,
                id="lang_override_when_extension_unknown",
            ),
            pytest.param(
                Pattern("print($$$)"),
                make_tool_event("Edit", {"file_path": "notes.txt", "old_string": "", "new_string": "print('x')\n"}),
                False,
                id="unsupported_extension_no_match",
            ),
            pytest.param(
                Pattern("print($$$)"),
                make_tool_event("Read", {"file_path": "a.py"}),
                False,
                id="no_content_no_match",
            ),
            pytest.param(
                Pattern("os.system($CMD)"),
                make_tool_event("Edit", {"file_path": "a.py", "old_string": "", "new_string": 'os.system("ls")\n'}),
                True,
                id="captures_metavar",
            ),
        ],
    )
    def test_pattern(self, cond: TCondition, evt: PreToolUseEvent, expected: bool) -> None:
        assert check_condition(cond, evt) is expected

    def test_hashable_and_equal(self) -> None:
        assert hash(Pattern("print($$$)")) == hash(Pattern("print($$$)"))
        assert Pattern("print($$$)") == Pattern("print($$$)")
        assert Pattern("print($$$)", lang="go") != Pattern("print($$$)")


class TestToolCondition:
    @pytest.mark.parametrize(
        ("pattern", "tool_name", "expected"),
        [
            pytest.param("Edit|Write", "Edit", True, id="matches_edit"),
            pytest.param("Edit|Write", "Write", True, id="matches_write"),
            pytest.param("Edit|Write", "Bash", False, id="rejects_bash"),
            pytest.param("Bash", "Execute", True, id="bash_matches_execute"),
            pytest.param("Execute", "Bash", True, id="execute_matches_bash"),
            pytest.param("Write", "Create", True, id="write_matches_create"),
            pytest.param("Create", "Write", True, id="create_matches_write"),
            pytest.param("Agent", "Task", True, id="agent_matches_task"),
            pytest.param("Task", "Agent", True, id="task_matches_agent"),
            pytest.param("WebFetch", "FetchUrl", True, id="webfetch_matches_fetchurl"),
            pytest.param("FetchUrl", "WebFetch", True, id="fetchurl_matches_webfetch"),
            pytest.param("ExitPlanMode", "ExitSpecMode", True, id="exitplanmode_matches_exitspecmode"),
            pytest.param("ExitSpecMode", "ExitPlanMode", True, id="exitspecmode_matches_exitplanmode"),
        ],
    )
    def test_tool(self, pattern: str, tool_name: str, expected: bool) -> None:
        assert check_condition(Tool(pattern), make_tool_event(tool_name)) is expected

    @pytest.mark.parametrize(
        ("event_cls", "expected"),
        [
            pytest.param(StopEvent, False, id="returns_false_for_stop_event"),
            pytest.param(UserPromptSubmitEvent, False, id="returns_false_for_user_prompt_event"),
        ],
    )
    def test_tool_non_tool_event(self, event_cls: type[BaseHookEvent], expected: bool) -> None:
        assert check_condition(Tool("Bash"), make_event(event_cls)) is expected


class TestFilePathCondition:
    @pytest.mark.parametrize(
        ("cond", "file_path", "expected"),
        [
            pytest.param(FilePath("*.py", "*.ts"), "a.py", True, id="matches_py"),
            pytest.param(FilePath("*.py", "*.ts"), "b.ts", True, id="matches_ts"),
            pytest.param(FilePath("*.py", "*.ts"), "c.rs", False, id="rejects_rs"),
        ],
    )
    def test_filepath_relative(self, cond: TCondition, file_path: str, expected: bool) -> None:
        evt = make_tool_event("Edit", {"file_path": file_path, "old_string": "", "new_string": ""})
        assert check_condition(cond, evt) is expected

    def test_filepath_returns_false_when_no_file(self) -> None:

        evt = make_tool_event("Bash", {"command": "echo hello"})
        assert check_condition(FilePath("*.py"), evt) is False

    def test_filepath_returns_false_for_stop_event(self) -> None:

        evt = make_event(StopEvent)
        assert check_condition(FilePath("*.py"), evt) is False

    @pytest.mark.parametrize(
        ("cond", "file_path", "expected"),
        [
            pytest.param(FilePath("*.py"), "a.py", True, id="matches_absolute_inside_project"),
            pytest.param(FilePath("*.py"), "/tmp/regex_check.py", False, id="rejects_absolute_outside_project"),
            pytest.param(
                FilePath("*.py", project_only=False),
                "/tmp/regex_check.py",
                True,
                id="opt_out_matches_absolute_outside_project",
            ),
        ],
    )
    def test_filepath_absolute(self, cond: TCondition, file_path: str, expected: bool, tmp_path: Path) -> None:
        path = file_path if file_path.startswith("/") else str(tmp_path / file_path)
        evt = make_tool_event(
            "Edit",
            {"file_path": path, "old_string": "", "new_string": ""},
            ctx=build_ctx(project_root=tmp_path),
        )
        assert check_condition(cond, evt) is expected

    def test_filepath_keyword_only_option(self) -> None:
        assert FilePath("*.py").project_only is True
        assert FilePath("*.py", project_only=False).project_only is False

    def test_filepath_rejects_unknown_kwarg(self) -> None:
        with pytest.raises(TypeError):
            FilePath("*.py", projectonly=False)  # type: ignore[call-arg]


class TestCommandCondition:
    @pytest.mark.parametrize(
        ("cond", "evt", "expected"),
        [
            pytest.param(
                Command(r"git\s+push\s+--force"),
                make_tool_event("Bash", {"command": "git push --force origin"}),
                True,
                id="matches_git_push_force",
            ),
            pytest.param(
                Command(r"git\s+push\s+--force"),
                make_tool_event("Bash", {"command": 'echo "git push --force"'}),
                True,
                id="matches_inside_echo",
            ),
            pytest.param(
                Command(r"git"),
                make_tool_event("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""}),
                False,
                id="returns_false_for_edit_event",
            ),
            pytest.param(Command(r"git"), make_event(StopEvent), False, id="returns_false_for_stop_event"),
        ],
    )
    def test_command(self, cond: TCondition, evt: BaseHookEvent, expected: bool) -> None:
        assert check_condition(cond, evt) is expected

    def test_command_uses_search_not_match(self) -> None:

        evt = make_tool_event("Bash", {"command": "uv run mtest run bioqa/"})
        assert check_condition(Command(r"mtest\s+run"), evt) is True
        assert check_condition(Command(r"git"), evt) is False


class TestContentCondition:
    @pytest.mark.parametrize(
        ("cond", "evt", "expected"),
        [
            pytest.param(
                Content(r"import\s+pdb"),
                make_tool_event(
                    "Edit", {"file_path": "a.py", "old_string": "", "new_string": "import pdb; pdb.set_trace()"}
                ),
                True,
                id="matches_import_pdb",
            ),
            pytest.param(
                Content(r"import\s+pdb"),
                make_tool_event("Edit", {"file_path": "a.py", "old_string": "", "new_string": "import logging"}),
                False,
                id="rejects_import_logging",
            ),
            pytest.param(
                Content(r"import\s+pdb"),
                make_tool_event("Bash", {"command": "echo hi"}),
                False,
                id="returns_false_for_bash_event",
            ),
            pytest.param(Content(r"import"), make_event(StopEvent), False, id="returns_false_for_stop_event"),
        ],
    )
    def test_content(self, cond: TCondition, evt: BaseHookEvent, expected: bool) -> None:
        assert check_condition(cond, evt) is expected

    @pytest.mark.parametrize(
        ("cond", "expected"),
        [
            pytest.param(Content(r"import\s+re"), False, id="rejects_absolute_outside_project"),
            pytest.param(
                Content(r"import\s+re", project_only=False), True, id="opt_out_matches_absolute_outside_project"
            ),
        ],
    )
    def test_content_absolute_outside_project(self, cond: TCondition, expected: bool, tmp_path: Path) -> None:
        evt = make_tool_event(
            "Write",
            {"file_path": "/tmp/regex_check.py", "content": "import re"},
            ctx=build_ctx(project_root=tmp_path),
        )
        assert check_condition(cond, evt) is expected


class TestAgentCondition:
    @pytest.mark.parametrize(
        ("cond", "evt", "expected"),
        [
            pytest.param(
                Agent("cleanup|quality-gate"),
                make_tool_event("Agent", {"subagent_type": "cleanup", "prompt": "run cleanup"}),
                True,
                id="matches_cleanup",
            ),
            pytest.param(
                Agent("cleanup|quality-gate"),
                make_tool_event("Agent", {"subagent_type": "quality-gate", "prompt": "review code"}),
                True,
                id="matches_quality_gate",
            ),
            pytest.param(
                Agent("cleanup|quality-gate"),
                make_tool_event("Agent", {"subagent_type": "worker", "prompt": "do work"}),
                False,
                id="rejects_non_matching",
            ),
            pytest.param(
                Agent("cleanup"),
                make_tool_event("Bash", {"command": "echo hi"}),
                False,
                id="returns_false_for_bash",
            ),
            pytest.param(Agent("cleanup"), make_event(StopEvent), False, id="returns_false_for_stop"),
            pytest.param(
                Agent("quality-gate"),
                make_event(
                    PermissionRequestEvent,
                    {"tool_name": "Bash", "tool_input": {"command": "echo hi"}, "agent_type": "quality-gate"},
                ),
                True,
                id="matches_permission_request_raw_agent_type",
            ),
            pytest.param(
                Agent("quality-gate"),
                make_event(
                    PermissionRequestEvent,
                    {"tool_name": "Bash", "tool_input": {"command": "echo hi"}, "agent_type": "worker"},
                ),
                False,
                id="rejects_permission_request_other_agent",
            ),
        ],
    )
    def test_agent(self, cond: TCondition, evt: BaseHookEvent, expected: bool) -> None:
        assert check_condition(cond, evt) is expected


class TestToolInputCondition:
    @pytest.mark.parametrize(
        ("cond", "evt", "expected"),
        [
            pytest.param(
                ToolInput("model", r"(?i)\bhaiku\b"),
                make_tool_event("Agent", {"model": "haiku", "prompt": "do it"}),
                True,
                id="matches_haiku",
            ),
            pytest.param(
                ToolInput("model", r"(?i)\bhaiku\b"),
                make_tool_event("Agent", {"model": "claude-haiku-4-5", "prompt": "do it"}),
                True,
                id="matches_versioned_haiku",
            ),
            pytest.param(
                ToolInput("model", r"(?i)\bhaiku\b"),
                make_tool_event("Agent", {"model": "sonnet", "prompt": "do it"}),
                False,
                id="rejects_non_matching_model",
            ),
            pytest.param(
                ToolInput("model", r"(?i)\bhaiku\b"),
                make_tool_event("Agent", {"prompt": "do it"}),
                False,
                id="rejects_missing_field",
            ),
            pytest.param(
                ToolInput("edits", r"haiku"),
                make_tool_event("MultiEdit", {"file_path": "x.py", "edits": [{"old_string": "a", "new_string": "b"}]}),
                False,
                id="rejects_non_string_field",
            ),
            pytest.param(ToolInput("model", r"haiku"), make_event(StopEvent), False, id="returns_false_for_stop"),
        ],
    )
    def test_toolinput(self, cond: TCondition, evt: BaseHookEvent, expected: bool) -> None:
        assert check_condition(cond, evt) is expected


class TestWorkflowScriptCondition:
    def test_matches_inline_script(self) -> None:
        evt = make_tool_event("Workflow", {"script": "steps:\n  - agent: reviewer\n    model: 'haiku'\n"})
        assert check_condition(WorkflowScript(model="haiku"), evt) is True

    def test_rejects_inline_script_without_match(self) -> None:
        evt = make_tool_event("Workflow", {"script": "steps:\n  - agent: reviewer\n    model: 'sonnet'\n"})
        assert check_condition(WorkflowScript(model="haiku"), evt) is False

    @pytest.mark.parametrize(
        ("pinned", "expected"),
        [
            ("model: 'haiku'", True),
            ('model: "haiku"', True),
            ("model:'claude-haiku-4-5-20251001'", True),
            ("model: 'sonnet'", False),
            ("# a comment mentioning haiku with no pin", False),
        ],
        ids=["single-quoted", "double-quoted", "full-id-no-space", "other-model", "unpinned-mention"],
    )
    def test_model_matches_pinned_values_only(self, pinned: str, expected: bool) -> None:
        evt = make_tool_event("Workflow", {"script": f"await agent('x', {{ {pinned} }})"})
        assert check_condition(WorkflowScript(model="haiku"), evt) is expected

    def test_pattern_searches_raw_source(self) -> None:
        evt = make_tool_event("Workflow", {"script": "const FLEET = 12"})
        assert check_condition(WorkflowScript(r"FLEET = \d+"), evt) is True

    def test_requires_pattern_or_opts(self) -> None:
        with pytest.raises(ValueError, match="pattern and/or"):
            WorkflowScript()

    def test_pattern_and_opts_combine_and(self) -> None:
        cond = WorkflowScript(r"pipeline", model="haiku")
        pinned = make_tool_event("Workflow", {"script": "await agent('x', { model: 'haiku' })\nawait pipeline([])"})
        assert check_condition(cond, pinned) is True
        no_pipeline = make_tool_event("Workflow", {"script": "await agent('x', { model: 'haiku' })"})
        assert check_condition(cond, no_pipeline) is False

    def test_kwargs_normalized_order_insensitive_and_hashable(self) -> None:
        assert WorkflowScript(model="a", effort="b") == WorkflowScript(effort="b", model="a")
        assert hash(WorkflowScript(model="a", effort="b")) == hash(WorkflowScript(effort="b", model="a"))
        assert len({WorkflowScript(model="a", effort="b"), WorkflowScript(effort="b", model="a")}) == 1

    def test_workflow_opt_matches_reports_spans(self) -> None:
        source = "agent('x', {model: 'haiku'}); agent('y', {model: 'opus'})"
        matches = workflow_opt_matches(source, "model")
        assert [m.group(1) for m in matches] == ["haiku", "opus"]
        assert [source[m.start() : m.end()] for m in matches] == ["model: 'haiku'", "model: 'opus'"]

    @pytest.mark.parametrize(
        ("cond", "pinned", "expected"),
        [
            pytest.param(WorkflowScript(model="haiku"), 'model: "haiku"', True, id="double_quoted"),
            pytest.param(WorkflowScript(label=r"^mine:"), "label: `mine:${a.key}`", True, id="template_literal"),
            pytest.param(WorkflowScript(model=r"a\.model"), "model: a.model", True, id="bare_token_by_name"),
            pytest.param(WorkflowScript(model="haiku"), "model: a.model", False, id="bare_token_not_runtime_value"),
            pytest.param(WorkflowScript(retries=r"^3$"), "retries: 3", True, id="bare_number"),
            pytest.param(WorkflowScript(model="haiku"), 'model: "o\'haiku"', True, id="quote_mix_regression"),
            pytest.param(WorkflowScript(effort="high"), "effort: 'high'", True, id="per_key_high"),
            pytest.param(WorkflowScript(effort="low"), "effort: 'high'", False, id="per_key_reject"),
            pytest.param(WorkflowScript(effort="low"), "effort: 'very-low'", True, id="substring_documented"),
            pytest.param(
                WorkflowScript(effort=r"^low$"), "effort: 'very-low'", False, id="anchored_excludes_substring"
            ),
            pytest.param(WorkflowScript(model="high"), "effort: 'high'", False, id="cross_key_isolation"),
            pytest.param(WorkflowScript(agentType="x"), "subagentType: 'x'", False, id="key_boundary_word"),
        ],
    )
    def test_opt_value_forms(self, cond: WorkflowScript, pinned: str, expected: bool) -> None:
        evt = make_tool_event("Workflow", {"script": f"await agent('x', {{ {pinned} }})"})
        assert check_condition(cond, evt) is expected

    @pytest.mark.parametrize(
        ("script", "expected"),
        [
            pytest.param("await agent('x', { model: 'haiku', effort: 'low' })", True, id="both_pinned"),
            pytest.param("await agent('x', { model: 'haiku', effort: 'high' })", False, id="effort_mismatch"),
            pytest.param("await agent('x', { model: 'haiku' })", False, id="effort_absent"),
        ],
    )
    def test_multi_kwarg_ands(self, script: str, expected: bool) -> None:
        evt = make_tool_event("Workflow", {"script": script})
        assert check_condition(WorkflowScript(model="haiku", effort="low"), evt) is expected

    @pytest.mark.parametrize(
        ("script", "expected"),
        [
            pytest.param("// a comment mentioning model without a pin\nlog('go')", False, id="comment_prose_no_pin"),
            pytest.param('log(`uvx cc-transcript grep "model:" --tool Workflow`)', False, id="grep_line_in_template"),
        ],
    )
    def test_absence_and_adversarial(self, script: str, expected: bool) -> None:
        evt = make_tool_event("Workflow", {"script": script})
        assert check_condition(WorkflowScript(model="haiku"), evt) is expected

    def test_realism_mine_model_heuristics(self) -> None:
        script = (
            "const ANGLES = [{ key: 'a', model: 'opus' }]\n"
            "const found = await agent('mine', { label: `mine:${a.key}`, schema: SCHEMA, model: a.model })\n"
        )
        evt = make_tool_event("Workflow", {"script": script})
        assert check_condition(WorkflowScript(model="opus"), evt) is True
        assert check_condition(WorkflowScript(model="haiku"), evt) is False

    def test_valueless_key_does_not_swallow_next_line(self) -> None:
        # An empty `model:` at end of line must not capture the following line's token.
        evt = make_tool_event("Workflow", {"script": "steps:\n    model:\n    prompt: use haiku please\n"})
        assert check_condition(WorkflowScript(model=r"prompt"), evt) is False
        assert check_condition(WorkflowScript(model=r"haiku"), evt) is False

    def test_matches_script_path(self, tmp_path: Path) -> None:
        script = tmp_path / "wf.yaml"
        script.write_text("steps:\n  - agent: reviewer\n    model: 'haiku'\n")
        evt = make_tool_event("Workflow", {"script_path": str(script)})
        assert check_condition(WorkflowScript(model="haiku"), evt) is True

    def test_rejects_missing_script_path(self, tmp_path: Path) -> None:
        evt = make_tool_event("Workflow", {"script_path": str(tmp_path / "nope.yaml")})
        assert check_condition(WorkflowScript(r"haiku"), evt) is False

    def test_rejects_oversized_script_path(self, tmp_path: Path) -> None:
        script = tmp_path / "big.yaml"
        script.write_text("model: 'haiku'\n" + "x" * 1_000_001)
        evt = make_tool_event("Workflow", {"script_path": str(script)})
        assert check_condition(WorkflowScript(r"haiku"), evt) is False

    def test_returns_false_for_non_workflow_tool(self) -> None:
        evt = make_tool_event("Bash", {"command": "echo model: haiku"})
        assert check_condition(WorkflowScript(r"haiku"), evt) is False

    def test_returns_false_for_stop_event(self) -> None:
        assert check_condition(WorkflowScript(r"haiku"), make_event(StopEvent)) is False


class TestToolInputKwargs:
    @pytest.mark.parametrize(
        ("cond", "tool_input", "expected"),
        [
            pytest.param(ToolInput(model=r"(?i)\bhaiku\b"), {"model": "haiku", "prompt": "p"}, True, id="kwarg_match"),
            pytest.param(
                ToolInput(subagent_type="Explore", model=r"haiku"),
                {"subagent_type": "Explore", "model": "haiku", "prompt": "p"},
                True,
                id="two_kwargs_and",
            ),
            pytest.param(
                ToolInput(subagent_type="Explore", model=r"haiku"),
                {"subagent_type": "Explore", "model": "sonnet", "prompt": "p"},
                False,
                id="two_kwargs_one_misses",
            ),
            pytest.param(
                ToolInput(run_in_background="true"),
                {"command": "x", "run_in_background": True},
                True,
                id="bool_coerced_to_true",
            ),
            pytest.param(
                ToolInput(run_in_background="false"),
                {"command": "x", "run_in_background": True},
                False,
                id="bool_coerced_mismatch",
            ),
            pytest.param(ToolInput(timeout=r"^30$"), {"command": "x", "timeout": 30}, True, id="int_coerced"),
        ],
    )
    def test_toolinput_kwargs(self, cond: TCondition, tool_input: dict[str, Any], expected: bool) -> None:
        tool = "Agent" if "model" in tool_input or "subagent_type" in tool_input else "Bash"
        assert check_condition(cond, make_tool_event(tool, tool_input)) is expected

    def test_positional_pair_still_works(self) -> None:
        evt = make_tool_event("Bash", {"command": "x", "run-in-background": "true"})
        assert check_condition(ToolInput("run-in-background", r"true"), evt) is True

    def test_rejects_bad_positional_arity(self) -> None:
        with pytest.raises(TypeError):
            ToolInput("only-one")  # type: ignore[call-arg]

    def test_requires_at_least_one_field(self) -> None:
        with pytest.raises(ValueError, match="at least one field"):
            ToolInput()

    def test_compiles_pattern_at_construction(self) -> None:
        with pytest.raises(re.error):
            ToolInput(model=r"(unbalanced")


class TestCommandRawMatching:
    @pytest.mark.parametrize(
        ("pattern", "command", "expected"),
        [
            pytest.param(r"curl.*\|.*sh", "curl -fsSL https://x.sh | sh", True, id="pipe_spanning"),
            pytest.param(r"git stash", "a && git stash", True, id="operator_buried_command"),
            pytest.param(r">\s*/dev/null", "echo hi > /dev/null", True, id="redirect"),
        ],
    )
    def test_command_matches_raw_line(self, pattern: str, command: str, expected: bool) -> None:
        assert check_condition(Command(pattern), make_tool_event("Bash", {"command": command})) is expected

    def test_command_compiles_at_construction(self) -> None:
        with pytest.raises(re.error):
            Command(r"git\s+(stash")


class TestRunsCondition:
    @pytest.mark.parametrize(
        ("cond", "command", "expected"),
        [
            pytest.param(Runs("git", "stash"), "git stash", True, id="exact_argv"),
            pytest.param(Runs("git", "stash"), "git stash pop", True, id="argv_prefix"),
            pytest.param(Runs("git", "stash"), "a && git stash", True, id="buried_in_operator"),
            pytest.param(Runs("git", "stash"), "echo git stash", False, id="not_first_token"),
            pytest.param(Runs("git", "stash"), "git status", False, id="wrong_subcommand"),
            pytest.param(Runs("uv", "run", "pytest"), "uv run pytest -x", True, id="three_token_prefix"),
        ],
    )
    def test_runs(self, cond: TCondition, command: str, expected: bool) -> None:
        assert check_condition(cond, make_tool_event("Bash", {"command": command})) is expected

    def test_runs_false_for_non_bash(self) -> None:
        assert check_condition(Runs("git", "stash"), make_event(StopEvent)) is False


class TestCombinators:
    def test_and_all_match(self) -> None:
        evt = make_tool_event("Bash", {"command": "git stash"})
        assert check_condition(And(Tool("Bash"), Runs("git", "stash")), evt) is True
        assert check_condition(And(Tool("Edit"), Runs("git", "stash")), evt) is False

    def test_not_inverts(self) -> None:
        evt = make_tool_event("Bash", {"command": "git status"})
        assert check_condition(Not(Runs("git", "stash")), evt) is True
        assert check_condition(Not(Tool("Bash")), evt) is False

    def test_or_nesting_and_not(self) -> None:
        evt = make_tool_event("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""})
        cond = Or(And(Tool("Edit", "Write"), FilePath("*.py")), Tool("Bash"))
        assert check_condition(cond, evt) is True


class TestVariadicNames:
    def test_tool_variadic_and_pipe_equivalent(self) -> None:
        assert Tool("Bash", "Execute") == Tool("Bash|Execute")
        assert check_condition(Tool("Edit", "Write"), make_tool_event("Write")) is True

    def test_agent_variadic_and_pipe_equivalent(self) -> None:
        assert Agent("Explore", "claude-code-guide") == Agent("Explore|claude-code-guide")
        evt = make_tool_event("Agent", {"subagent_type": "Explore", "prompt": "p"})
        assert check_condition(Agent("Explore", "claude-code-guide"), evt) is True


class TestTestFileCondition:
    @pytest.mark.parametrize(
        ("cond", "evt", "expected"),
        [
            pytest.param(
                TestFile(),
                make_tool_event("Edit", {"file_path": "tests/test_foo.py", "old_string": "", "new_string": ""}),
                True,
                id="matches_test_file",
            ),
            pytest.param(
                TestFile(),
                make_tool_event("Edit", {"file_path": "bioqa/main.py", "old_string": "", "new_string": ""}),
                False,
                id="rejects_non_test_file",
            ),
            pytest.param(
                TestFile(), make_tool_event("Bash", {"command": "echo hi"}), False, id="returns_false_for_bash"
            ),
            pytest.param(TestFile(), make_event(StopEvent), False, id="returns_false_for_stop"),
        ],
    )
    def test_testfile(self, cond: TCondition, evt: BaseHookEvent, expected: bool) -> None:
        assert check_condition(cond, evt) is expected

    @pytest.mark.parametrize(
        ("cond", "expected"),
        [
            pytest.param(TestFile(), False, id="rejects_absolute_outside_project"),
            pytest.param(TestFile(project_only=False), True, id="opt_out_matches_absolute_outside_project"),
        ],
    )
    def test_testfile_absolute_outside_project(self, cond: TCondition, expected: bool, tmp_path: Path) -> None:
        evt = make_tool_event(
            "Edit",
            {"file_path": "/tmp/test_regex_check.py", "old_string": "", "new_string": ""},
            ctx=build_ctx(project_root=tmp_path),
        )
        assert check_condition(cond, evt) is expected


def skill_evt(skill: str) -> BaseHookEvent:
    ctx = make_messages_ctx([raw_tool_msg("Skill", {"skill": skill})])
    return make_tool_event("Bash", {"command": "echo"}, ctx=ctx)


def read_evt(path: str) -> BaseHookEvent:
    ctx = make_messages_ctx([raw_tool_msg("Read", {"file_path": path})])
    return make_tool_event("Bash", {"command": "echo"}, ctx=ctx)


class TestUsedSkillCondition:
    @pytest.mark.parametrize(
        ("cond", "skill", "expected"),
        [
            pytest.param(UsedSkill("codex"), "codex", True, id="bare_matches_bare"),
            pytest.param(UsedSkill("codex"), "codex:codex", True, id="bare_matches_plugin_suffix"),
            pytest.param(UsedSkill("slop-cop-check"), "slop-cop:slop-cop-check", True, id="suffix_across_plugin"),
            pytest.param(UsedSkill("codex", "agent-browser"), "agent-browser", True, id="variadic_second_name"),
            pytest.param(UsedSkill("codex|agent-browser"), "agent-browser", True, id="pipe_back_compat"),
            pytest.param(UsedSkill("codex"), "other", False, id="rejects_non_matching"),
            pytest.param(UsedSkill("codex"), "codex:other", False, id="rejects_wrong_suffix"),
        ],
    )
    def test_usedskill(self, cond: TCondition, skill: str, expected: bool) -> None:
        assert check_condition(cond, skill_evt(skill)) is expected


class TestReadFileCondition:
    @pytest.mark.parametrize(
        ("cond", "path", "expected"),
        [
            pytest.param(ReadFile("*.md"), "/repo/STYLEGUIDE.md", True, id="glob_star_md"),
            pytest.param(ReadFile("STYLEGUIDE.md"), "/repo/sub/STYLEGUIDE.md", True, id="basename_any_dir"),
            pytest.param(ReadFile("**/docs/**"), "/repo/docs/guide/x.qmd", True, id="glob_recursive_dir"),
            pytest.param(ReadFile("**/docs/styleguide/**"), "/repo/docs/styleguide/a.md", True, id="glob_nested"),
            pytest.param(ReadFile("STYLEGUIDE.md", "**/docs/**"), "/repo/docs/x.md", True, id="any_of_globs"),
            pytest.param(ReadFile("docs/**"), "docs/guide/x.qmd", True, id="glob_relative_path"),
            pytest.param(ReadFile("*.md"), "/repo/main.py", False, id="rejects_non_matching_ext"),
            pytest.param(ReadFile("**/docs/**"), "/repo/src/main.py", False, id="rejects_wrong_dir"),
        ],
    )
    def test_readfile(self, cond: TCondition, path: str, expected: bool) -> None:
        assert check_condition(cond, read_evt(path)) is expected


class TestTouchedFileCondition:
    def test_touchedfile_matches_edit(self) -> None:

        ctx = make_transcript_ctx(has_edit_to_result=True)
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx)
        assert check_condition(TouchedFile("**/www/src/**"), evt) is True
        ctx.transcript.has_edit_to.assert_called_once_with("**/www/src/**", subagents=False)

    def test_touchedfile_rejects_non_matching(self) -> None:

        ctx = make_transcript_ctx(has_edit_to_result=False)
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx)
        assert check_condition(TouchedFile("**/www/src/**"), evt) is False


class TestRanCommandCondition:
    def test_rancommand_matches(self) -> None:

        ctx = make_transcript_ctx(has_command_result=True)
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx)
        assert check_condition(RanCommand("codex", "exec"), evt) is True
        ctx.transcript.has_command.assert_called_once_with("codex", "exec", subagents=True)

    def test_rancommand_rejects(self) -> None:

        ctx = make_transcript_ctx(has_command_result=False)
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx)
        assert check_condition(RanCommand("codex", "exec"), evt) is False


class TestInPlanModeCondition:
    @pytest.mark.parametrize(
        ("count_tools_map", "permission_mode", "expected"),
        [
            pytest.param({"EnterPlanMode": 2, "ExitPlanMode": 1}, None, True, id="matches_when_more_enter"),
            pytest.param({"EnterPlanMode": 1, "ExitPlanMode": 1}, None, False, id="rejects_when_equal"),
            pytest.param({}, None, False, id="rejects_when_zero"),
            pytest.param({}, "plan", True, id="matches_when_permission_mode_plan"),
            pytest.param({}, "default", False, id="rejects_when_permission_mode_default"),
            pytest.param(
                {"EnterPlanMode": 1, "ExitPlanMode": 0},
                None,
                True,
                id="falls_back_to_tool_count_when_permission_mode_unset",
            ),
        ],
    )
    def test_inplanmode(self, count_tools_map: dict[str, int], permission_mode: str | None, expected: bool) -> None:
        ctx = make_transcript_ctx(count_tools_map=count_tools_map)
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx, permission_mode=permission_mode)
        assert check_condition(InPlanMode(), evt) is expected


class TestWaitingCondition:
    @pytest.mark.parametrize(
        ("tool", "tool_input", "expected"),
        [
            pytest.param("Agent", {"prompt": "x", "run_in_background": True}, True, id="agent_run_in_background"),
            pytest.param("Agent", {"prompt": "x"}, True, id="fork_minimal_agent"),
            pytest.param("Agent", {"name": "x", "prompt": "y"}, True, id="fork_bare_agent"),
            pytest.param("Agent", {"subagent_type": "test-runner", "prompt": "y"}, False, id="typed_subagent"),
            pytest.param("Task", {"prompt": "x", "run_in_background": True}, True, id="task_run_in_background"),
            pytest.param("Task", {"name": "x", "prompt": "y"}, True, id="fork_bare_task"),
            pytest.param("Task", {"subagent_type": "general-purpose", "prompt": "y"}, False, id="typed_task"),
            pytest.param("Bash", {"command": "build", "run_in_background": True}, True, id="bash_run_in_background"),
            pytest.param("Bash", {"command": "ls"}, False, id="bash_sync"),
            pytest.param("Monitor", {"name": "build"}, True, id="monitor"),
            pytest.param("TeamCreate", {"members": []}, True, id="team_create"),
            pytest.param("ScheduleWakeup", {"delaySeconds": 600}, True, id="schedule_wakeup"),
            pytest.param("SendMessage", {"to": "agent-1", "message": "go"}, True, id="send_message"),
        ],
    )
    def test_single_tool_matches(self, tool: str, tool_input: dict[str, Any], expected: bool) -> None:

        ctx = make_messages_ctx([assistant_msg((tool, tool_input))])
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx)
        assert check_condition(Waiting(), evt) is expected

    def test_only_sync_tools_rejects(self) -> None:

        ctx = make_messages_ctx(
            [
                assistant_msg(
                    ("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""}),
                    ("Read", {"file_path": "a.py"}),
                    ("Grep", {"pattern": "foo"}),
                )
            ]
        )
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx)
        assert check_condition(Waiting(), evt) is False

    def test_walks_past_trailing_text_message(self) -> None:

        ctx = make_messages_ctx(
            [
                assistant_msg(("Agent", {"prompt": "x", "run_in_background": True})),
                text_msg("Spawned 4 background agents, waiting for them to report back."),
            ]
        )
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx)
        assert check_condition(Waiting(), evt) is True

    def test_async_launched_tool_result(self) -> None:

        raw = async_agent_launch(id="tu_x", input={"subagent_type": "general-purpose", "name": "ed", "prompt": "y"})
        assert check_condition(Waiting(), waiting_evt(raw)) is True

    def test_completed_tool_result_is_not_waiting(self) -> None:

        raw = [
            raw_assistant(raw_tool_use("Agent", {"subagent_type": "web-analyzer", "prompt": "y"}, "tu_x")),
            raw_tool_result(
                "tu_x", content=[{"type": "text", "text": "done"}], tool_use_result={"status": "completed"}
            ),
        ]
        assert check_condition(Waiting(), waiting_evt(raw)) is False

    def test_async_launched_then_notified_is_not_waiting(self) -> None:

        raw = [*async_agent_launch(id="tu_x"), raw_notification("tu_x")]
        assert check_condition(Waiting(), waiting_evt(raw)) is False

    def test_async_launched_then_unrelated_notification_still_waiting(self) -> None:

        raw = [*async_agent_launch(id="tu_x"), raw_notification("tu_other")]
        assert check_condition(Waiting(), waiting_evt(raw)) is True

    def test_workflow_launched_is_waiting(self) -> None:

        assert check_condition(Waiting(), waiting_evt(workflow_launch(id="toolu_wf"))) is True

    def test_workflow_then_notified_is_not_waiting(self) -> None:

        raw = [*workflow_launch(id="toolu_wf"), raw_notification("toolu_wf", task_id="waux9o41y")]
        assert check_condition(Waiting(), waiting_evt(raw)) is False

    def test_workflow_then_unrelated_notification_still_waiting(self) -> None:

        raw = [*workflow_launch(id="toolu_wf"), raw_notification("toolu_other", task_id="other-task")]
        assert check_condition(Waiting(), waiting_evt(raw)) is True

    def test_async_in_progress_with_sync_followup_still_waiting(self) -> None:

        raw = [
            *async_agent_launch(id="tu_x"),
            raw_assistant(raw_tool_use("Bash", {"command": "ls"}, "tu_y")),
            raw_tool_result("tu_y", content=[], tool_use_result={"status": "completed"}),
        ]
        assert check_condition(Waiting(), waiting_evt(raw)) is True

    def test_workflow_orphaned_across_user_message_still_waiting(self) -> None:

        raw = [*workflow_launch(id="toolu_wf"), raw_text("user", "actually, also update the changelog")]
        assert check_condition(Waiting(), waiting_evt(raw)) is True

    def test_workflow_orphaned_then_notified_in_later_turn_is_not_waiting(self) -> None:

        raw = [
            *workflow_launch(id="toolu_wf"),
            raw_text("user", "actually, also update the changelog"),
            raw_notification("toolu_wf", task_id="waux9o41y"),
        ]
        assert check_condition(Waiting(), waiting_evt(raw)) is False

    def test_async_agent_orphaned_across_user_message_still_waiting(self) -> None:

        raw = [*async_agent_launch(id="tu_x"), raw_text("user", "while that runs, what's the plan?")]
        assert check_condition(Waiting(), waiting_evt(raw)) is True

    def test_empty_transcript_rejects(self) -> None:

        ctx = make_messages_ctx([])
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx)
        assert check_condition(Waiting(), evt) is False

    def test_no_transcript_rejects(self) -> None:

        ctx = make_messages_ctx(None)
        evt = make_tool_event("Bash", {"command": "echo"}, ctx=ctx)
        assert check_condition(Waiting(), evt) is False


class TestFromSubagentCondition:
    @pytest.mark.parametrize(
        ("raw_extra", "expected"),
        [
            pytest.param({"agent_id": "tm1"}, True, id="matches_when_agent_id_present"),
            pytest.param({}, False, id="rejects_when_agent_id_absent"),
        ],
    )
    def test_from_subagent(self, raw_extra: dict[str, Any], expected: bool) -> None:
        evt = make_event(PermissionRequestEvent, {"tool_name": "Bash", "tool_input": {"command": "echo"}} | raw_extra)
        assert check_condition(FromSubagent(), evt) is expected


class TestSkipPermissionsCondition:
    @pytest.mark.parametrize(
        "skip",
        [
            pytest.param(True, id="matches_when_bypass_available"),
            pytest.param(False, id="rejects_without_bypass"),
        ],
    )
    def test_skip_permissions(self, skip: bool) -> None:
        evt = make_event(PermissionRequestEvent, {"tool_name": "Bash", "tool_input": {"command": "echo"}})
        evt.__dict__["skip_permissions"] = skip
        assert check_condition(SkipPermissions(), evt) is skip


class TestPermissionRequestRegistration:
    def test_tool_conditions_register_without_error(self) -> None:
        from captain_hook.app import _state

        @on(Event.PermissionRequest, only_if=[Tool("Bash")], skip_if=[ToolInput("command", "x")])
        def handler(evt: BaseHookEvent) -> None:
            return None

        assert _state.hooks[-1].spec.events is Event.PermissionRequest


class TestOnlyIfSemantics:
    def test_only_if_all_must_match(self) -> None:

        spec = HookSpec(
            events=Event.PreToolUse,
            only_if=(Tool("Bash"), Command(r"git\s+push")),
        )
        evt_match = make_tool_event("Bash", {"command": "git push origin"})
        assert matches_conditions(spec, evt_match) is True

        evt_wrong_cmd = make_tool_event("Bash", {"command": "echo hello"})
        assert matches_conditions(spec, evt_wrong_cmd) is False

    def test_only_if_one_false_fails(self) -> None:

        spec = HookSpec(
            events=Event.PreToolUse,
            only_if=(Tool("Edit"), Command(r"git")),
        )
        evt = make_tool_event("Edit", {"file_path": "a.py", "old_string": "", "new_string": ""})
        assert matches_conditions(spec, evt) is False


class TestSkipIfSemantics:
    def test_skip_if_any_causes_skip(self) -> None:

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


class TestCombinedConditions:
    def test_only_if_and_skip_if_combined(self) -> None:

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


class TestNonToolEventConditions:
    def test_conditions_on_user_prompt_graceful(self) -> None:

        evt = make_event(UserPromptSubmitEvent)
        assert check_condition(Tool("Bash"), evt) is False
        assert check_condition(Command(r"git"), evt) is False
        assert check_condition(Content(r"import"), evt) is False
        assert check_condition(FilePath("*.py"), evt) is False
        assert check_condition(TestFile(), evt) is False
        assert check_condition(Agent("cleanup"), evt) is False


class TestTokensToRegex:
    def test_simple_tokens(self) -> None:
        result = block_command_pattern(["git", "stash"])
        assert re.match(result, "git stash pop")
        assert re.match(result, "git  stash")

    def test_wildcard(self) -> None:
        result = block_command_pattern(["git", "*"])
        assert re.match(result, "git commit -m x")
        assert re.match(result, "git push")

    def test_pipe_alternation(self) -> None:
        result = block_command_pattern(["git", "commit|split"])
        assert re.match(result, "git commit -m x")
        assert re.match(result, "git split")
        assert not re.match(result, "git push")

    def test_empty_list_returns_empty_string(self) -> None:
        assert block_command_pattern([]) == ""


class TestEmptyConditions:
    @pytest.mark.parametrize(
        "spec",
        [
            pytest.param(HookSpec(events=Event.PreToolUse, only_if=()), id="empty_only_if_always_matches"),
            pytest.param(HookSpec(events=Event.PreToolUse, skip_if=()), id="empty_skip_if_never_skipped"),
            pytest.param(HookSpec(events=Event.PreToolUse, only_if=(), skip_if=()), id="both_empty_conditions_matches"),
        ],
    )
    def test_empty_conditions(self, spec: HookSpec) -> None:
        evt = make_tool_event("Bash", {"command": "echo hi"})
        assert matches_conditions(spec, evt) is True


class TestCustomCondition:
    def test_custom_condition_returns_true(self) -> None:
        from dataclasses import dataclass

        @dataclass(frozen=True, slots=True)
        class AlwaysTrue:
            def check(self, evt: BaseHookEvent) -> bool:
                return True

        evt = make_tool_event("Bash", {"command": "echo"})
        assert check_condition(AlwaysTrue(), evt) is True

    def test_custom_condition_returns_false(self) -> None:
        from dataclasses import dataclass

        @dataclass(frozen=True, slots=True)
        class AlwaysFalse:
            def check(self, evt: BaseHookEvent) -> bool:
                return False

        evt = make_tool_event("Bash", {"command": "echo"})
        assert check_condition(AlwaysFalse(), evt) is False

    def test_custom_condition_receives_event(self) -> None:
        from dataclasses import dataclass

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

        @dataclass(frozen=True, slots=True)
        class NotACondition:
            value: int

        evt = make_tool_event("Bash", {"command": "echo"})
        assert check_condition(NotACondition(42), evt) is False  # type: ignore[arg-type]


@pytest.fixture
def event_with_subagent_tool_use(tmp_path: Path) -> Callable[[dict[str, Any]], BaseHookEvent]:
    def make(tool_use: dict[str, Any]) -> BaseHookEvent:
        session_dir = tmp_path / "session"
        subagents_dir = session_dir / "session" / "subagents"
        subagents_dir.mkdir(parents=True)

        session_file = session_dir / "session.jsonl"
        session_file.write_text(json.dumps(raw_text("user", "hi")) + "\n")

        sub1 = subagents_dir / "sub1.jsonl"
        sub1.write_text(json.dumps(raw_assistant(tool_use)) + "\n")

        return make_event(
            PreToolUseEvent,
            raw={"tool_name": "Bash", "tool_input": {"command": "echo"}},
            ctx=build_ctx(transcript=load_transcript(session_file)),
        )

    return make


class TestSubagentFlags:
    @pytest.mark.parametrize(
        ("tool_use", "default_cond", "explicit_cond", "default_expected", "explicit_expected"),
        [
            pytest.param(
                {"type": "tool_use", "name": "Skill", "input": {"skill": "codex"}, "id": "tu_a"},
                UsedSkill("codex"),
                UsedSkill("codex", subagents=False),
                True,
                False,
                id="UsedSkill",
            ),
            pytest.param(
                {"type": "tool_use", "name": "Bash", "input": {"command": "codex exec -p 'hi'"}, "id": "tu_b"},
                RanCommand("codex", "exec"),
                RanCommand("codex", "exec", subagents=False),
                True,
                False,
                id="RanCommand",
            ),
            pytest.param(
                {
                    "type": "tool_use",
                    "name": "Edit",
                    "input": {"file_path": "src/foo.py", "old_string": "", "new_string": ""},
                    "id": "tu_c",
                },
                TouchedFile("**/foo.py"),
                TouchedFile("**/foo.py", subagents=True),
                False,
                True,
                id="TouchedFile",
            ),
            pytest.param(
                {"type": "tool_use", "name": "Read", "input": {"file_path": "docs/TESTING.md"}, "id": "tu_d"},
                ReadFile("TESTING.md"),
                ReadFile("TESTING.md", subagents=False),
                True,
                False,
                id="ReadFile",
            ),
        ],
    )
    def test_condition_subagent_flag(
        self,
        event_with_subagent_tool_use: Callable[[dict[str, Any]], BaseHookEvent],
        tool_use: dict[str, Any],
        default_cond: TCondition,
        explicit_cond: TCondition,
        default_expected: bool,
        explicit_expected: bool,
    ) -> None:
        evt = event_with_subagent_tool_use(tool_use)
        assert check_condition(default_cond, evt) is default_expected
        assert check_condition(explicit_cond, evt) is explicit_expected


class TestCustomConditionFilesystem:
    def test_settings_based_condition(self, tmp_path: Any) -> None:
        import json
        from dataclasses import dataclass
        from pathlib import Path

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

        @dataclass(frozen=True, slots=True)
        class HasTag:
            tag: str

            def check(self, evt: BaseHookEvent) -> bool:
                return bool(evt._raw.get("transcript_path"))

        evt = make_event(StopEvent)
        assert check_condition(HasTag("anything"), evt) is False
