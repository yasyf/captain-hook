from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from captain_hook.app import _state, discover_hooks, get_matching_hooks
from captain_hook.dispatch import dispatch
from captain_hook.testing.helpers import mock_stop_event, mock_tool_event, mock_user_prompt_event
from captain_hook.tests.helpers import run_cli
from captain_hook.types import Event


def purge_hooks_modules() -> None:
    for k in [k for k in sys.modules if k == "hooks" or k.startswith("hooks.")]:
        del sys.modules[k]
    sys.path[:] = [p for p in sys.path if not (Path(p) / "hooks").is_dir()]


@pytest.fixture(autouse=True)
def purge_hooks():
    purge_hooks_modules()
    yield
    purge_hooks_modules()


@pytest.fixture()
def project_dir(tmp_path: Path) -> Path:
    result = run_cli("init", root_dir=str(tmp_path))
    assert result.returncode == 0, f"captain-hook init failed: {result.stderr}"
    return tmp_path


class TestInit:
    def test_creates_example_hook(self, project_dir: Path) -> None:
        example = project_dir / ".claude" / "hooks" / "example.py"
        assert example.exists()
        text = example.read_text()
        assert "block_command" in text
        assert "nudge" in text

    def test_creates_settings_json(self, project_dir: Path) -> None:
        settings = project_dir / ".claude" / "settings.local.json"
        assert settings.exists()
        data = json.loads(settings.read_text())
        assert "hooks" in data
        assert isinstance(data["hooks"], dict)
        assert len(data["hooks"]) > 0

    def test_settings_has_pretooluse(self, project_dir: Path) -> None:
        settings = project_dir / ".claude" / "settings.local.json"
        data = json.loads(settings.read_text())
        assert "PreToolUse" in data["hooks"]

    def test_settings_commands_use_uvx(self, project_dir: Path) -> None:
        settings = project_dir / ".claude" / "settings.local.json"
        raw = settings.read_text()
        assert "uvx captain-hook" in raw


class TestDiscoverAndDispatch:
    def test_discovers_hooks_from_init(self, project_dir: Path) -> None:
        hooks_dir = project_dir / ".claude" / "hooks"
        discover_hooks(str(hooks_dir))
        assert len(_state.hooks) > 0

    def test_dispatch_blocks_matching_command(self, project_dir: Path) -> None:
        hooks_dir = project_dir / ".claude" / "hooks"
        discover_hooks(str(hooks_dir))

        evt = mock_tool_event("Bash", event=Event.PreToolUse, command="rm -rf /")
        result = dispatch(Event.PreToolUse, evt)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_dispatch_allows_safe_command(self, project_dir: Path) -> None:
        hooks_dir = project_dir / ".claude" / "hooks"
        discover_hooks(str(hooks_dir))

        evt = mock_tool_event("Bash", event=Event.PreToolUse, command="echo hello")
        result = dispatch(Event.PreToolUse, evt)
        assert result is None or result["hookSpecificOutput"].get("permissionDecision") != "deny"

    def test_matching_hooks_returns_entries(self, project_dir: Path) -> None:
        hooks_dir = project_dir / ".claude" / "hooks"
        discover_hooks(str(hooks_dir))

        evt = mock_tool_event("Bash", event=Event.PreToolUse, command="rm -rf /")
        matching = get_matching_hooks(evt)
        assert len(matching) > 0


class TestCliDispatch:
    def test_cli_blocks_dangerous_command(self, project_dir: Path) -> None:
        hooks_dir = project_dir / ".claude" / "hooks"
        stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}})
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), root_dir=str(project_dir), stdin_data=stdin)
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_cli_allows_safe_command(self, project_dir: Path) -> None:
        hooks_dir = project_dir / ".claude" / "hooks"
        stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), root_dir=str(project_dir), stdin_data=stdin)
        assert result.returncode == 0


class TestCliTest:
    def test_test_subcommand_no_inline_tests(self, project_dir: Path) -> None:
        hooks_dir = project_dir / ".claude" / "hooks"
        result = run_cli("test", hooks_dir=str(hooks_dir))
        assert result.returncode == 0
        assert "No inline tests" in result.stdout

    def test_test_subcommand_with_inline_tests(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        (hooks_dir / "my_hook.py").write_text(
            'from captain_hook import block_command\n'
            'from captain_hook.testing import Input, Block\n'
            '\n'
            'block_command(\n'
            '    r"rm\\s+-rf",\n'
            '    reason="No rm -rf allowed",\n'
            '    tests={\n'
            '        Input(tool="Bash", command="rm -rf /"): Block(pattern="rm -rf"),\n'
            '    },\n'
            ')\n'
        )
        result = run_cli("test", hooks_dir=str(hooks_dir))
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert "PASS" in result.stdout
        assert "1 tests" in result.stdout


class TestGenerateSettings:
    def test_generate_settings_for_init_hooks(self, project_dir: Path) -> None:
        hooks_dir = project_dir / ".claude" / "hooks"
        result = run_cli("generate-settings", "--no-merge", hooks_dir=str(hooks_dir))
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert "hooks" in data
        assert "PreToolUse" in data["hooks"]

    def test_merge_preserves_existing_keys(self, project_dir: Path) -> None:
        settings_path = project_dir / ".claude" / "settings.local.json"
        existing = json.loads(settings_path.read_text())
        existing["customKey"] = "preserved"
        settings_path.write_text(json.dumps(existing))

        hooks_dir = project_dir / ".claude" / "hooks"
        result = run_cli("generate-settings", hooks_dir=str(hooks_dir), root_dir=str(project_dir))
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["customKey"] == "preserved"
        assert "hooks" in data


def write_hooks(tmp_path: Path, *files: tuple[str, str]) -> Path:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    (hooks_dir / "__init__.py").write_text("")
    for name, code in files:
        (hooks_dir / name).write_text(textwrap.dedent(code))
    return hooks_dir


class TestHandlerHooks:
    def test_on_decorator_blocks_with_custom_logic(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("guard.py", """\
            from captain_hook import on, Event, HookResult, Action

            @on(Event.PreToolUse)
            def custom_guard(evt):
                if evt.tool_name == "Bash" and evt.command and "sudo" in evt.command:
                    return HookResult(action=Action.block, message="No sudo allowed")
        """))
        discover_hooks(str(hooks_dir))
        result = dispatch(Event.PreToolUse, mock_tool_event("Bash", event=Event.PreToolUse, command="sudo rm -rf /"))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "sudo" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_on_decorator_allows_when_no_match(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("guard.py", """\
            from captain_hook import on, Event, HookResult, Action

            @on(Event.PreToolUse)
            def custom_guard(evt):
                if evt.tool_name == "Bash" and evt.command and "sudo" in evt.command:
                    return HookResult(action=Action.block, message="No sudo allowed")
        """))
        discover_hooks(str(hooks_dir))
        result = dispatch(Event.PreToolUse, mock_tool_event("Bash", event=Event.PreToolUse, command="echo hi"))
        assert result is None

    def test_on_decorator_warn_adds_context(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("advisor.py", """\
            from captain_hook import on, Event, HookResult, Action

            @on(Event.PreToolUse)
            def advise(evt):
                if evt.tool_name == "Write":
                    return HookResult(action=Action.warn, message="Remember to run tests")
        """))
        discover_hooks(str(hooks_dir))
        result = dispatch(
            Event.PreToolUse,
            mock_tool_event("Write", event=Event.PreToolUse, file="main.py", content="x = 1"),
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "Remember to run tests" in result["hookSpecificOutput"]["additionalContext"]


class TestMultiConditionHooks:
    def test_tool_plus_command_condition_blocks(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("protect.py", """\
            from captain_hook import hook, Event, Tool
            from captain_hook.types import Command

            hook(
                Event.PreToolUse,
                only_if=[Tool("Bash"), Command(r"git\\s+push\\s+--force")],
                message="Force push is forbidden",
                block=True,
            )
        """))
        discover_hooks(str(hooks_dir))
        result = dispatch(
            Event.PreToolUse,
            mock_tool_event("Bash", event=Event.PreToolUse, command="git push --force origin main"),
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_tool_plus_command_condition_allows_non_force(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("protect.py", """\
            from captain_hook import hook, Event, Tool
            from captain_hook.types import Command

            hook(
                Event.PreToolUse,
                only_if=[Tool("Bash"), Command(r"git\\s+push\\s+--force")],
                message="Force push is forbidden",
                block=True,
            )
        """))
        discover_hooks(str(hooks_dir))
        result = dispatch(
            Event.PreToolUse,
            mock_tool_event("Bash", event=Event.PreToolUse, command="git push origin main"),
        )
        assert result is None

    def test_skip_if_testfile_bypasses_hook(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("style.py", """\
            from captain_hook import hook, Event, Tool, TestFile

            hook(
                Event.PreToolUse,
                only_if=[Tool("Edit")],
                skip_if=[TestFile()],
                message="Check style before editing",
            )
        """))
        discover_hooks(str(hooks_dir))
        result_prod = dispatch(
            Event.PreToolUse,
            mock_tool_event("Edit", event=Event.PreToolUse, file="src/main.py", content="x = 1"),
        )
        assert result_prod is not None
        assert "Check style" in result_prod["hookSpecificOutput"]["additionalContext"]

        result_test = dispatch(
            Event.PreToolUse,
            mock_tool_event("Edit", event=Event.PreToolUse, file="tests/test_main.py", content="x = 1"),
        )
        assert result_test is None

    def test_filepath_condition_matches_glob(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("lockfiles.py", """\
            from captain_hook import hook, Event, Tool, FilePath

            hook(
                Event.PreToolUse,
                only_if=[Tool("Edit"), FilePath("*.lock", "*.lockb")],
                message="Don't edit lockfiles manually",
                block=True,
            )
        """))
        discover_hooks(str(hooks_dir))
        result = dispatch(
            Event.PreToolUse,
            mock_tool_event("Edit", event=Event.PreToolUse, file="package.lock", content="dep"),
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

        result_py = dispatch(
            Event.PreToolUse,
            mock_tool_event("Edit", event=Event.PreToolUse, file="main.py", content="dep"),
        )
        assert result_py is None


class TestContentCondition:
    def test_content_blocks_debugger_statements(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("no_debug.py", """\
            from captain_hook import hook, Event, Tool, Content

            hook(
                Event.PreToolUse,
                only_if=[Tool("Edit|Write"), Content(r"import\\s+pdb|breakpoint\\(\\)")],
                message="Remove debugger statements before committing",
                block=True,
            )
        """))
        discover_hooks(str(hooks_dir))
        result = dispatch(
            Event.PreToolUse,
            mock_tool_event("Edit", event=Event.PreToolUse, file="app.py", content="import pdb; pdb.set_trace()"),
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_content_allows_clean_code(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("no_debug.py", """\
            from captain_hook import hook, Event, Tool, Content

            hook(
                Event.PreToolUse,
                only_if=[Tool("Edit|Write"), Content(r"import\\s+pdb|breakpoint\\(\\)")],
                message="Remove debugger statements",
                block=True,
            )
        """))
        discover_hooks(str(hooks_dir))
        result = dispatch(
            Event.PreToolUse,
            mock_tool_event("Edit", event=Event.PreToolUse, file="app.py", content="import logging"),
        )
        assert result is None


class TestWarnCommand:
    def test_warn_command_adds_context(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("warnings.py", """\
            from captain_hook import warn_command
            from captain_hook.testing import Input, Warn

            warn_command(
                r"npm\\s+install",
                message="Prefer pnpm over npm",
                tests={
                    Input(tool="Bash", command="npm install lodash"): Warn(pattern="pnpm"),
                },
            )
        """))
        discover_hooks(str(hooks_dir))
        result = dispatch(
            Event.PostToolUse,
            mock_tool_event("Bash", event=Event.PostToolUse, command="npm install lodash"),
        )
        assert result is not None
        assert "pnpm" in result["hookSpecificOutput"]["additionalContext"]


class TestMultipleHooksInteraction:
    def test_block_takes_priority_over_warn(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("multi.py", """\
            from captain_hook import block_command, warn_command

            block_command(r"rm\\s+-rf", reason="Dangerous delete")
            warn_command(r"rm", message="Be careful with rm")
        """))
        discover_hooks(str(hooks_dir))
        result = dispatch(
            Event.PreToolUse,
            mock_tool_event("Bash", event=Event.PreToolUse, command="rm -rf /tmp/junk"),
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_hooks_across_files(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(
            tmp_path,
            ("security.py", """\
                from captain_hook import block_command
                block_command(r"curl.*-k", reason="No insecure curl")
            """),
            ("style.py", """\
                from captain_hook import hook, Event, Tool, Content
                hook(
                    Event.PreToolUse,
                    only_if=[Tool("Edit"), Content(r"TODO|FIXME|HACK")],
                    message="Clean up TODO markers",
                )
            """),
        )
        discover_hooks(str(hooks_dir))
        assert len(_state.hooks) >= 2

        curl_result = dispatch(
            Event.PreToolUse,
            mock_tool_event("Bash", event=Event.PreToolUse, command="curl -k https://evil.com"),
        )
        assert curl_result is not None
        assert curl_result["hookSpecificOutput"]["permissionDecision"] == "deny"

        todo_result = dispatch(
            Event.PreToolUse,
            mock_tool_event("Edit", event=Event.PreToolUse, file="main.py", content="# TODO: fix this"),
        )
        assert todo_result is not None
        assert "TODO" in todo_result["hookSpecificOutput"]["additionalContext"]


class TestMaxFires:
    def test_hook_respects_max_fires(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("once.py", """\
            from captain_hook import hook, Event, Tool

            hook(
                Event.PreToolUse,
                only_if=[Tool("Bash")],
                message="One-time reminder: prefer uv over pip",
                max_fires=1,
            )
        """))
        discover_hooks(str(hooks_dir))
        evt = mock_tool_event("Bash", event=Event.PreToolUse, command="pip install x", session_dir=tmp_path)

        result1 = dispatch(Event.PreToolUse, evt, session_dir=tmp_path)
        assert result1 is not None
        assert "uv" in result1["hookSpecificOutput"]["additionalContext"]

        result2 = dispatch(Event.PreToolUse, evt, session_dir=tmp_path)
        assert result2 is None


class TestStopEventHooks:
    def test_stop_hook_blocks(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("stop_guard.py", """\
            from captain_hook import on, Event, HookResult, Action

            @on(Event.Stop)
            def require_tests(evt):
                return HookResult(action=Action.block, message="Run tests before stopping")
        """))
        discover_hooks(str(hooks_dir))
        result = dispatch(Event.Stop, mock_stop_event())
        assert result is not None
        assert result["decision"] == "block"
        assert "Run tests" in result["reason"]

    def test_stop_hook_allows(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("stop_guard.py", """\
            from captain_hook import on, Event

            @on(Event.Stop)
            def pass_through(evt):
                return None
        """))
        discover_hooks(str(hooks_dir))
        result = dispatch(Event.Stop, mock_stop_event())
        assert result is None


class TestUserPromptHooks:
    def test_user_prompt_hook_warns(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("prompt_guard.py", """\
            from captain_hook import on, Event, HookResult, Action

            @on(Event.UserPromptSubmit)
            def check_prompt(evt):
                if evt.user_prompt and "password" in evt.user_prompt.lower():
                    return HookResult(action=Action.warn, message="Avoid sharing credentials in prompts")
        """))
        discover_hooks(str(hooks_dir))
        result = dispatch(
            Event.UserPromptSubmit,
            mock_user_prompt_event(prompt="my password is hunter2"),
        )
        assert result is not None
        assert "credentials" in result["hookSpecificOutput"]["additionalContext"]

    def test_user_prompt_hook_ignores_safe_prompt(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("prompt_guard.py", """\
            from captain_hook import on, Event, HookResult, Action

            @on(Event.UserPromptSubmit)
            def check_prompt(evt):
                if evt.user_prompt and "password" in evt.user_prompt.lower():
                    return HookResult(action=Action.warn, message="Avoid sharing credentials")
        """))
        discover_hooks(str(hooks_dir))
        result = dispatch(
            Event.UserPromptSubmit,
            mock_user_prompt_event(prompt="fix the login page"),
        )
        assert result is None


class TestLintPrimitive:
    def test_lint_string_check_warns_on_violation(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("lints.py", """\
            from captain_hook import lint

            def check_print_statements(source: str) -> list[str]:
                return [
                    f"line {i}: bare print() call"
                    for i, line in enumerate(source.splitlines(), 1)
                    if line.strip().startswith("print(")
                ]

            lint(check_print_statements, message="Found print statements: {violations}")
        """))
        discover_hooks(str(hooks_dir))
        result = dispatch(
            Event.PostToolUse,
            mock_tool_event(
                "Edit",
                event=Event.PostToolUse,
                file="app.py",
                content='print("debug")\nx = 1',
            ),
        )
        assert result is not None
        assert "print" in result["hookSpecificOutput"]["additionalContext"]

    def test_lint_skips_test_files_by_default(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("lints.py", """\
            from captain_hook import lint

            def check_print(source: str) -> list[str]:
                return ["found print"] if "print(" in source else []

            lint(check_print, message="No prints: {violations}")
        """))
        discover_hooks(str(hooks_dir))
        result = dispatch(
            Event.PostToolUse,
            mock_tool_event(
                "Edit",
                event=Event.PostToolUse,
                file="tests/test_app.py",
                content='print("debug")',
            ),
        )
        assert result is None


class TestComplexInlineTests:
    def test_multiple_inline_test_cases_via_cli(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("guards.py", """\
            from captain_hook import block_command, warn_command
            from captain_hook.testing import Input, Block, Allow, Warn

            block_command(
                r"git\\s+(stash|reset\\s+--hard|checkout\\s+\\.)",
                reason="Destructive git operation blocked",
                tests={
                    Input(tool="Bash", command="git stash pop"): Block(pattern="Destructive"),
                    Input(tool="Bash", command="git reset --hard HEAD"): Block(pattern="Destructive"),
                    Input(tool="Bash", command="git checkout ."): Block(pattern="Destructive"),
                    Input(tool="Bash", command="git status"): Allow(),
                    Input(tool="Bash", command="echo hello"): Allow(),
                },
            )

            warn_command(
                r"docker\\s+build",
                message="Consider using docker compose instead",
                tests={
                    Input(tool="Bash", command="docker build ."): Warn(pattern="compose"),
                },
            )
        """))
        result = run_cli("test", hooks_dir=str(hooks_dir))
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert "PASS" in result.stdout
        assert "6 tests" in result.stdout

    def test_handler_hook_with_inline_tests(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("handler.py", """\
            from captain_hook import on, Event, Tool, HookResult, Action
            from captain_hook.testing import Input, Block, Allow

            @on(
                Event.PreToolUse,
                only_if=[Tool("Bash")],
                tests={
                    Input(command="pip install requests"): Block(pattern="uv"),
                    Input(command="echo hello"): Allow(),
                },
            )
            def prefer_uv(evt):
                if evt.command and evt.command.startswith("pip "):
                    return HookResult(action=Action.block, message="Use uv instead of pip")
        """))
        result = run_cli("test", hooks_dir=str(hooks_dir))
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert "PASS" in result.stdout
        assert "2 tests" in result.stdout


class TestGenerateSettingsMultiEvent:
    def test_multi_event_hook_appears_in_all_event_sections(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(tmp_path, ("multi_event.py", """\
            from captain_hook import hook, Event

            hook(
                Event.PreToolUse | Event.PostToolUse,
                message="Audit trail: tool invocation logged",
            )
        """))
        result = run_cli("generate-settings", "--no-merge", hooks_dir=str(hooks_dir))
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert "PreToolUse" in data["hooks"]
        assert "PostToolUse" in data["hooks"]
