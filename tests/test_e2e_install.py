from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from captain_hook.app import _state, get_matching_hooks
from captain_hook.dispatch import dispatch
from captain_hook.loader import discover_hooks
from captain_hook.testing.helpers import mock_stop_event, mock_tool_event, mock_user_prompt_event
from captain_hook.types import Event
from tests.helpers import run_cli


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
    assert result.returncode == 0, f"capt-hook init failed: {result.stderr}"
    return tmp_path


class TestInit:
    def test_creates_example_hook(self, project_dir: Path) -> None:
        example = project_dir / ".claude" / "hooks" / "example.py"
        assert example.exists()
        text = example.read_text()
        assert "block_command" in text
        assert "nudge" in text

    def test_example_hook_covers_four_primitives(self, project_dir: Path) -> None:
        text = (project_dir / ".claude" / "hooks" / "example.py").read_text()
        assert "block_command(" in text
        assert "nudge(" in text
        assert "gate(" in text
        assert "prompt_check(" in text
        assert "Prompt.from_template" in text
        assert "tests={" in text

    def test_init_prints_next_steps(self, tmp_path: Path) -> None:
        result = run_cli("init", root_dir=str(tmp_path))
        assert result.returncode == 0
        assert "Next:" in result.stdout
        assert "https://yasyf.github.io/captain-hook/" in result.stdout
        assert "capt-hook test" in result.stdout
        assert "Scaffolded" in result.stdout
        assert "Session reviewer:" in result.stdout


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
        # Anchor cwd to the scaffolded project so the namespace ``hooks`` package resolves against
        # this project, not a regular ``hooks`` package on the default cwd's import path.
        result = run_cli(
            "run",
            "PreToolUse",
            hooks_dir=str(hooks_dir),
            root_dir=str(project_dir),
            stdin_data=stdin,
            cwd=str(project_dir),
        )
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_cli_allows_safe_command(self, project_dir: Path) -> None:
        hooks_dir = project_dir / ".claude" / "hooks"
        stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})
        result = run_cli("run", "PreToolUse", hooks_dir=str(hooks_dir), root_dir=str(project_dir), stdin_data=stdin)
        assert result.returncode == 0


class TestCliTest:
    def test_scaffold_inline_tests_pass(self, project_dir: Path) -> None:
        hooks_dir = project_dir / ".claude" / "hooks"
        # Anchor cwd to the scaffolded project so the namespace ``hooks`` package resolves against
        # this project, not a regular ``hooks`` package on the default cwd's import path.
        result = run_cli("test", hooks_dir=str(hooks_dir), cwd=str(project_dir))
        assert result.returncode == 0
        assert "PASS" in result.stdout
        assert "0 failed" in result.stdout

    def test_test_subcommand_with_inline_tests(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        (hooks_dir / "my_hook.py").write_text(
            "from captain_hook import block_command\n"
            "from captain_hook.testing import Input, Block\n"
            "\n"
            "block_command(\n"
            '    r"rm\\s+-rf",\n'
            '    reason="No rm -rf allowed",\n'
            "    tests={\n"
            '        Input(tool="Bash", command="rm -rf /"): Block(pattern="rm -rf"),\n'
            "    },\n"
            ")\n"
        )
        result = run_cli("test", hooks_dir=str(hooks_dir))
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert "PASS" in result.stdout
        assert "1 tests" in result.stdout

    def test_test_json_mode_emits_one_record_per_line(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        (hooks_dir / "my_hook.py").write_text(
            "from captain_hook import block_command\n"
            "from captain_hook.testing import Input, Block, Allow\n"
            "\n"
            "block_command(\n"
            '    r"rm\\s+-rf",\n'
            '    reason="No rm -rf allowed",\n'
            "    tests={\n"
            '        Input(tool="Bash", command="rm -rf /"): Block(pattern="rm -rf"),\n'
            '        Input(tool="Bash", command="echo hi"): Allow(),\n'
            "    },\n"
            ")\n"
        )
        result = run_cli("test", "--json", hooks_dir=str(hooks_dir))
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        assert len(lines) == 2
        records = [json.loads(ln) for ln in lines]
        for record in records:
            assert set(record) >= {"id", "status", "expected", "reason"}
            assert record["status"] == "pass"
        kinds = {r["expected"] for r in records}
        assert kinds == {"block", "allow"}

    def test_test_json_mode_nonzero_exit_on_failure(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        (hooks_dir / "my_hook.py").write_text(
            "from captain_hook import block_command\n"
            "from captain_hook.testing import Input, Block\n"
            "\n"
            "block_command(\n"
            '    r"never-matches",\n'
            '    reason="No",\n'
            "    tests={\n"
            '        Input(tool="Bash", command="ls"): Block(),\n'
            "    },\n"
            ")\n"
        )
        result = run_cli("test", "--json", hooks_dir=str(hooks_dir))
        assert result.returncode != 0
        records = [json.loads(ln) for ln in result.stdout.splitlines() if ln.strip()]
        assert any(r["status"] == "fail" for r in records)


def write_hooks(tmp_path: Path, *files: tuple[str, str]) -> Path:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    (hooks_dir / "__init__.py").write_text("")
    for name, code in files:
        (hooks_dir / name).write_text(textwrap.dedent(code))
    return hooks_dir


FORCE_PUSH_HOOK = """\
    from captain_hook import hook, Event, Tool
    from captain_hook.types import Command

    hook(
        Event.PreToolUse,
        only_if=[Tool("Bash"), Command(r"git\\s+push\\s+--force")],
        message="Force push is forbidden",
        block=True,
    )
"""

DEBUGGER_HOOK = """\
    from captain_hook import hook, Event, Tool, Content

    hook(
        Event.PreToolUse,
        only_if=[Tool("Edit|Write"), Content(r"import\\s+pdb|breakpoint\\(\\)")],
        message="Remove debugger statements before committing",
        block=True,
    )
"""

SUDO_GUARD_HOOK = """\
    from captain_hook import on, Event, HookResult, Action

    @on(Event.PreToolUse)
    def custom_guard(evt):
        if evt.tool_name == "Bash" and "sudo" in evt.command.raw:
            return HookResult(action=Action.block, message="No sudo allowed")
"""

BLOCK_OVER_WARN_HOOK = """\
    from captain_hook import block_command, warn_command

    block_command(r"rm\\s+-rf", reason="Dangerous delete")
    warn_command(r"rm", message="Be careful with rm")
"""

STOP_PASS_HOOK = """\
    from captain_hook import on, Event

    @on(Event.Stop)
    def pass_through(evt):
        return None
"""

PROMPT_GUARD_HOOK = """\
    from captain_hook import on, Event, HookResult, Action

    @on(Event.UserPromptSubmit)
    def check_prompt(evt):
        if evt.user_prompt and "password" in evt.user_prompt.lower():
            return HookResult(action=Action.warn, message="Avoid sharing credentials in prompts")
"""

LINT_PRINT_HOOK = """\
    from captain_hook import lint

    def check_print(source: str) -> list[str]:
        return ["found print"] if "print(" in source else []

    lint(check_print, message="No prints: {violations}")
"""

WARN_NPM_HOOK = """\
    from captain_hook import warn_command
    from captain_hook.testing import Input, Warn

    warn_command(
        r"npm\\s+install",
        message="Prefer pnpm over npm",
        tests={
            Input(tool="Bash", command="npm install lodash"): Warn(pattern="pnpm"),
        },
    )
"""

LINT_PRINT_LINES_HOOK = """\
    from captain_hook import lint

    def check_print_statements(source: str) -> list[str]:
        return [
            f"line {i}: bare print() call"
            for i, line in enumerate(source.splitlines(), 1)
            if line.strip().startswith("print(")
        ]

    lint(check_print_statements, message="Found print statements: {violations}")
"""


class TestDispatch:
    @pytest.mark.parametrize(
        ("source", "event", "evt"),
        [
            pytest.param(
                FORCE_PUSH_HOOK,
                Event.PreToolUse,
                mock_tool_event("Bash", event=Event.PreToolUse, command="git push --force origin main"),
                id="tool_plus_command_force_push",
            ),
            pytest.param(
                DEBUGGER_HOOK,
                Event.PreToolUse,
                mock_tool_event("Edit", event=Event.PreToolUse, file="app.py", content="import pdb; pdb.set_trace()"),
                id="content_debugger_statement",
            ),
            pytest.param(
                BLOCK_OVER_WARN_HOOK,
                Event.PreToolUse,
                mock_tool_event("Bash", event=Event.PreToolUse, command="rm -rf /tmp/junk"),
                id="block_takes_priority_over_warn",
            ),
        ],
    )
    def test_deny(self, tmp_path: Path, source: str, event: Event, evt: object) -> None:
        discover_hooks(str(write_hooks(tmp_path, ("hook.py", source))))
        result = dispatch(event, evt)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    @pytest.mark.parametrize(
        ("source", "event", "evt"),
        [
            pytest.param(
                SUDO_GUARD_HOOK,
                Event.PreToolUse,
                mock_tool_event("Bash", event=Event.PreToolUse, command="echo hi"),
                id="on_handler_no_match",
            ),
            pytest.param(
                FORCE_PUSH_HOOK,
                Event.PreToolUse,
                mock_tool_event("Bash", event=Event.PreToolUse, command="git push origin main"),
                id="tool_plus_command_non_force",
            ),
            pytest.param(
                DEBUGGER_HOOK,
                Event.PreToolUse,
                mock_tool_event("Edit", event=Event.PreToolUse, file="app.py", content="import logging"),
                id="content_clean_code",
            ),
            pytest.param(
                STOP_PASS_HOOK,
                Event.Stop,
                mock_stop_event(),
                id="stop_pass_through",
            ),
            pytest.param(
                PROMPT_GUARD_HOOK,
                Event.UserPromptSubmit,
                mock_user_prompt_event(prompt="fix the login page"),
                id="user_prompt_safe",
            ),
            pytest.param(
                LINT_PRINT_HOOK,
                Event.PostToolUse,
                mock_tool_event("Edit", event=Event.PostToolUse, file="tests/test_app.py", content='print("debug")'),
                id="lint_skips_test_file",
            ),
        ],
    )
    def test_no_op(self, tmp_path: Path, source: str, event: Event, evt: object) -> None:
        discover_hooks(str(write_hooks(tmp_path, ("hook.py", source))))
        assert dispatch(event, evt) is None

    @pytest.mark.parametrize(
        ("source", "event", "evt", "expected"),
        [
            pytest.param(
                WARN_NPM_HOOK,
                Event.PostToolUse,
                mock_tool_event("Bash", event=Event.PostToolUse, command="npm install lodash"),
                "pnpm",
                id="warn_command",
            ),
            pytest.param(
                PROMPT_GUARD_HOOK,
                Event.UserPromptSubmit,
                mock_user_prompt_event(prompt="my password is hunter2"),
                "credentials",
                id="user_prompt_warns",
            ),
            pytest.param(
                LINT_PRINT_LINES_HOOK,
                Event.PostToolUse,
                mock_tool_event("Edit", event=Event.PostToolUse, file="app.py", content='print("debug")\nx = 1'),
                "print",
                id="lint_violation",
            ),
        ],
    )
    def test_adds_context(self, tmp_path: Path, source: str, event: Event, evt: object, expected: str) -> None:
        discover_hooks(str(write_hooks(tmp_path, ("hook.py", source))))
        result = dispatch(event, evt)
        assert result is not None
        assert expected in result["hookSpecificOutput"]["additionalContext"]

    def test_on_decorator_blocks_with_custom_logic(self, tmp_path: Path) -> None:
        discover_hooks(str(write_hooks(tmp_path, ("guard.py", SUDO_GUARD_HOOK))))
        result = dispatch(Event.PreToolUse, mock_tool_event("Bash", event=Event.PreToolUse, command="sudo rm -rf /"))
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "sudo" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_on_decorator_warn_adds_context(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(
            tmp_path,
            (
                "advisor.py",
                """\
            from captain_hook import on, Event, HookResult, Action

            @on(Event.PreToolUse)
            def advise(evt):
                if evt.tool_name == "Write":
                    return HookResult(action=Action.warn, message="Remember to run tests")
        """,
            ),
        )
        discover_hooks(str(hooks_dir))
        result = dispatch(
            Event.PreToolUse,
            mock_tool_event("Write", event=Event.PreToolUse, file="main.py", content="x = 1"),
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "Remember to run tests" in result["hookSpecificOutput"]["additionalContext"]

    def test_skip_if_testfile_bypasses_hook(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(
            tmp_path,
            (
                "style.py",
                """\
            from captain_hook import hook, Event, Tool, TestFile

            hook(
                Event.PreToolUse,
                only_if=[Tool("Edit")],
                skip_if=[TestFile()],
                message="Check style before editing",
            )
        """,
            ),
        )
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
        hooks_dir = write_hooks(
            tmp_path,
            (
                "lockfiles.py",
                """\
            from captain_hook import hook, Event, Tool, FilePath

            hook(
                Event.PreToolUse,
                only_if=[Tool("Edit"), FilePath("*.lock", "*.lockb")],
                message="Don't edit lockfiles manually",
                block=True,
            )
        """,
            ),
        )
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

    def test_hooks_across_files(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(
            tmp_path,
            (
                "security.py",
                """\
                from captain_hook import block_command
                block_command(r"curl.*-k", reason="No insecure curl")
            """,
            ),
            (
                "style.py",
                """\
                from captain_hook import hook, Event, Tool, Content
                hook(
                    Event.PreToolUse,
                    only_if=[Tool("Edit"), Content(r"TODO|FIXME|HACK")],
                    message="Clean up TODO markers",
                )
            """,
            ),
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

    def test_hook_respects_max_fires(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(
            tmp_path,
            (
                "max_fires.py",
                """\
            from captain_hook import hook, Event, Tool

            hook(
                Event.PreToolUse,
                only_if=[Tool("Bash")],
                message="One-time reminder: prefer uv over pip",
                max_fires=1,
            )
        """,
            ),
        )
        discover_hooks(str(hooks_dir))
        evt = mock_tool_event("Bash", event=Event.PreToolUse, command="pip install x", session_dir=tmp_path)

        result1 = dispatch(Event.PreToolUse, evt, session_dir=tmp_path)
        assert result1 is not None
        assert "uv" in result1["hookSpecificOutput"]["additionalContext"]

        result2 = dispatch(Event.PreToolUse, evt, session_dir=tmp_path)
        assert result2 is None

    def test_stop_hook_blocks(self, tmp_path: Path) -> None:
        hooks_dir = write_hooks(
            tmp_path,
            (
                "stop_guard.py",
                """\
            from captain_hook import on, Event, HookResult, Action

            @on(Event.Stop)
            def require_tests(evt):
                return HookResult(action=Action.block, message="Run tests before stopping")
        """,
            ),
        )
        discover_hooks(str(hooks_dir))
        result = dispatch(Event.Stop, mock_stop_event())
        assert result is not None
        assert result["decision"] == "block"
        assert "Run tests" in result["reason"]


class TestComplexInlineTests:
    @pytest.mark.parametrize(
        ("source", "test_count"),
        [
            pytest.param(
                """\
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
                """,
                "6 tests",
                id="block_and_warn_commands",
            ),
            pytest.param(
                """\
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
                    if evt.command.raw.startswith("pip "):
                        return HookResult(action=Action.block, message="Use uv instead of pip")
                """,
                "2 tests",
                id="on_handler",
            ),
        ],
    )
    def test_inline_tests_via_cli(self, tmp_path: Path, source: str, test_count: str) -> None:
        hooks_dir = write_hooks(tmp_path, ("hook.py", source))
        result = run_cli("test", hooks_dir=str(hooks_dir))
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert "PASS" in result.stdout
        assert test_count in result.stdout


class TestSkillsInstall:
    def test_init_registers_plugin(self, project_dir: Path) -> None:
        settings = json.loads((project_dir / ".claude" / "settings.json").read_text())
        assert settings["enabledPlugins"] == {"captain-hook@captain-hook": True}
        assert settings["extraKnownMarketplaces"]["captain-hook"] == {
            "source": {"source": "github", "repo": "yasyf/captain-hook"},
            "autoUpdate": True,
        }
        assert not (project_dir / ".claude" / "skills").exists()

    def test_init_reports_plugin(self, tmp_path: Path) -> None:
        result = run_cli("init", root_dir=str(tmp_path))
        assert result.returncode == 0
        assert "registered captain-hook@captain-hook" in result.stdout

    def test_init_rerun_preserves_foreign_settings(self, tmp_path: Path) -> None:
        settings_path = tmp_path / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
        result = run_cli("init", root_dir=str(tmp_path))
        assert result.returncode == 0
        settings = json.loads(settings_path.read_text())
        assert settings["permissions"] == {"allow": ["Bash(ls)"]}
        assert settings["enabledPlugins"] == {"captain-hook@captain-hook": True}

    def test_skills_install_standalone(self, tmp_path: Path) -> None:
        result = run_cli("skills", "install", root_dir=str(tmp_path))
        assert result.returncode == 0
        assert "registered captain-hook@captain-hook" in result.stdout
        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        assert settings["enabledPlugins"] == {"captain-hook@captain-hook": True}
        assert not (tmp_path / ".claude" / "skills").exists()
