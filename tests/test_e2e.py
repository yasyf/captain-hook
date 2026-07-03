from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from captain_hook.app import (
    _state,
    get_matching_hooks,
)
from captain_hook.app import (
    hook as register_hook,
)
from captain_hook.dispatch import format_output
from captain_hook.events import (
    PostToolUseEvent,
    PostToolUseFailureEvent,
    PreToolUseEvent,
    StopEvent,
    SubagentStopEvent,
    UserPromptSubmitEvent,
)
from captain_hook.loader import discover_hooks
from captain_hook.session import SessionStore
from captain_hook.state import HookState, PrimitiveState
from captain_hook.types import Action, Event, HookResult
from tests.helpers import build_ctx, run_cli

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "client_hooks"


def stdin_json(**raw: Any) -> str:
    return json.dumps(raw)


class TestEntryPointExecution:
    def test_e2e_001_bin_hooks_run_blocks_git_stash(self, tmp_path: Path) -> None:
        stdin = stdin_json(tool_name="Bash", tool_input={"command": "git stash"})
        result = run_cli(
            "run",
            "PreToolUse",
            hooks_dir=str(FIXTURES_DIR),
            root_dir=str(tmp_path),
            stdin_data=stdin,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "use the team VCS workflow" in output["hookSpecificOutput"]["permissionDecisionReason"]

    def test_e2e_002_bin_hooks_run_allows_safe_command(self, tmp_path: Path) -> None:
        stdin = stdin_json(tool_name="Bash", tool_input={"command": "echo hi"})
        result = run_cli(
            "run",
            "PreToolUse",
            hooks_dir=str(FIXTURES_DIR),
            root_dir=str(tmp_path),
            stdin_data=stdin,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout.strip() in ("", "{}")

    def test_e2e_003_bin_hooks_run_stop_event(self, tmp_path: Path) -> None:
        stdin = stdin_json(stop_hook_active=False)
        result = run_cli(
            "run",
            "Stop",
            hooks_dir=str(FIXTURES_DIR),
            root_dir=str(tmp_path),
            stdin_data=stdin,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"


class TestHookDiscovery:
    @pytest.fixture(autouse=True)
    def clean_hooks_modules(self) -> None:
        stale = [k for k in sys.modules if k == "client_hooks" or k.startswith("client_hooks.")]
        for k in stale:
            del sys.modules[k]
        if str(FIXTURES_DIR.parent) not in sys.path:
            sys.path.insert(0, str(FIXTURES_DIR.parent))

    def test_e2e_010_all_fixture_files_discovered(self) -> None:
        discover_hooks(str(FIXTURES_DIR))
        assert len(_state.hooks) > 0
        assert _state.settings is not None

    def test_e2e_011_conf_settings_subclass_loaded(self) -> None:
        discover_hooks(str(FIXTURES_DIR))
        assert type(_state.settings).__name__ == "Settings"
        assert _state.settings.test_command == "pytest -q"
        assert _state.settings.require_review_before_stop is True

    def test_e2e_012_command_hook_discovered(self) -> None:
        discover_hooks(str(FIXTURES_DIR))
        hook_names = {h.name for h in _state.hooks}
        assert any(n.startswith("declarative_") for n in hook_names), (
            f"No declarative block_command hook found in {hook_names}"
        )

    def test_e2e_013_named_workflow_hook_discovered(self) -> None:
        discover_hooks(str(FIXTURES_DIR))
        hook_names = {h.name for h in _state.hooks}
        assert "require_review_before_stop" in hook_names, f"require_review_before_stop not in {hook_names}"

    def test_e2e_014_no_import_errors(self) -> None:
        discover_hooks(str(FIXTURES_DIR))

    def test_e2e_015_discovery_via_cli_subprocess(self, tmp_path: Path) -> None:
        stdin = stdin_json(tool_name="Bash", tool_input={"command": "echo hi"})
        result = run_cli(
            "run",
            "PreToolUse",
            hooks_dir=str(FIXTURES_DIR),
            root_dir=str(tmp_path),
            stdin_data=stdin,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"


class TestInlineTests:
    def test_e2e_020_inline_tests_pass(self) -> None:
        discover_hooks(str(FIXTURES_DIR))
        from captain_hook.testing.helpers import run_inline_tests

        results = run_inline_tests()
        assert len(results) > 0, "No inline tests found"
        failures = [(name, detail) for name, status, ok, detail in results if status == "fail"]
        errors = [(name, detail) for name, status, ok, detail in results if status == "error"]
        assert not failures, f"Inline test failures: {failures}"
        assert not errors, f"Inline test errors: {errors}"

    def test_e2e_021_test_subcommand_via_cli(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        hook_file = hooks_dir / "my_hook.py"
        hook_file.write_text(
            textwrap.dedent("""\
            from captain_hook.app import hook, on
            from captain_hook.types import Event, Tool
            from captain_hook.testing import Input, Allow, Block

            hook(
                Event.PreToolUse,
                message="blocked cmd",
                block=True,
                only_if=[Tool("Bash")],
                tests={
                    Input(tool="Bash", command="echo hi"): Block(pattern="blocked"),
                },
            )
        """)
        )
        result = run_cli("test", hooks_dir=str(hooks_dir))
        assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
        assert "PASS" in result.stdout

    def test_e2e_022_test_subcommand_fails_on_bad_test(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "__init__.py").write_text("")
        hook_file = hooks_dir / "my_hook.py"
        hook_file.write_text(
            textwrap.dedent("""\
            from captain_hook.app import hook, on
            from captain_hook.types import Event, Tool
            from captain_hook.testing import Input, Allow, Block

            hook(
                Event.PreToolUse,
                message="blocked cmd",
                block=True,
                only_if=[Tool("Bash")],
                tests={
                    Input(tool="Bash", command="echo hi"): Allow(),
                },
            )
        """)
        )
        result = run_cli("test", hooks_dir=str(hooks_dir))
        assert result.returncode != 0
        assert "FAIL" in result.stdout


class TestEventDispatchRoundTrip:
    def test_e2e_031_pretooluse_allow_no_output(self, tmp_path: Path) -> None:
        stdin = stdin_json(tool_name="Bash", tool_input={"command": "git status"})
        result = run_cli(
            "run",
            "PreToolUse",
            hooks_dir=str(FIXTURES_DIR),
            root_dir=str(tmp_path),
            stdin_data=stdin,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert result.stdout.strip() in ("", "{}")

    @pytest.mark.parametrize(
        ("event", "stdin"),
        [
            pytest.param("Stop", stdin_json(), id="032_stop_event_dispatch"),
            pytest.param(
                "UserPromptSubmit",
                stdin_json(prompt="hello world"),
                id="033_user_prompt_submit_dispatch",
            ),
            pytest.param(
                "SubagentStop",
                stdin_json(agent_type="worker", agent_id="abc"),
                id="034_subagent_stop_dispatch",
            ),
            pytest.param(
                "SubagentStart",
                stdin_json(agent_type="cleanup", agent_id="abc"),
                id="035_subagent_start_dispatch",
            ),
            pytest.param(
                "PostToolUse",
                stdin_json(tool_name="Bash", tool_input={"command": "echo hi"}, tool_response="hi"),
                id="036_post_tool_use_dispatch",
            ),
            pytest.param(
                "PostToolUseFailure",
                stdin_json(tool_name="Bash", tool_input={"command": "false"}, error="command failed"),
                id="037_post_tool_use_failure_dispatch",
            ),
        ],
    )
    def test_e2e_03x_event_dispatch(self, tmp_path: Path, event: str, stdin: str) -> None:
        result = run_cli(
            "run",
            event,
            hooks_dir=str(FIXTURES_DIR),
            root_dir=str(tmp_path),
            stdin_data=stdin,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

    def test_e2e_038_format_output_pretooluse_block(self) -> None:
        result = HookResult(action=Action.block, message="not allowed")
        output = format_output(Event.PreToolUse, result)
        assert output is not None
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert output["hookSpecificOutput"]["permissionDecisionReason"] == "not allowed"

    def test_e2e_039_format_output_pretooluse_warn(self) -> None:
        result = HookResult(action=Action.warn, message="be careful")
        output = format_output(Event.PreToolUse, result)
        assert output is not None
        assert output["hookSpecificOutput"]["additionalContext"] == "be careful"
        assert output["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_e2e_040_format_output_stop_block(self) -> None:
        result = HookResult(action=Action.block, message="incomplete")
        output = format_output(Event.Stop, result)
        assert output is not None
        assert output["decision"] == "block"
        assert output["reason"] == "incomplete"


class TestRegisterHooks:
    def test_e2e_050_register_hooks_valid_json(self, tmp_path: Path) -> None:
        result = run_cli(
            "register-hooks",
            "--dry-run",
            hooks_dir=str(FIXTURES_DIR),
            root_dir=str(tmp_path),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert "hooks" in data
        assert isinstance(data["hooks"], dict)

    def test_e2e_051_register_hooks_has_expected_events(self, tmp_path: Path) -> None:
        result = run_cli(
            "register-hooks",
            "--dry-run",
            hooks_dir=str(FIXTURES_DIR),
            root_dir=str(tmp_path),
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        events = set(data["hooks"].keys())
        assert "PreToolUse" in events
        assert "Stop" in events

    def test_e2e_052_register_hooks_commands_have_uvx(self, tmp_path: Path) -> None:
        result = run_cli(
            "register-hooks",
            "--hooks-dir",
            "custom/hooks",
            "--dry-run",
            hooks_dir=str(FIXTURES_DIR),
            root_dir=str(tmp_path),
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        raw = json.dumps(data)
        assert "uvx capt-hook" in raw
        assert "$CLAUDE_PROJECT_DIR/custom/hooks" in raw

    def test_e2e_053_register_hooks_with_from_source(self, tmp_path: Path) -> None:
        result = run_cli(
            "register-hooks",
            "--hooks-dir",
            "custom/hooks",
            "--from",
            "./local/path",
            "--dry-run",
            hooks_dir=str(FIXTURES_DIR),
            root_dir=str(tmp_path),
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        raw = json.dumps(data)
        assert "uvx --from ./local/path capt-hook" in raw


class TestStateModelSerialization:
    def test_e2e_060_hook_state_round_trip(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        original = HookState(fire_count=5)
        store[HookState].set(original)
        loaded = store[HookState].get()
        assert loaded is not None
        assert loaded.fire_count == 5

    def test_e2e_061_primitive_state_round_trip(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        original = PrimitiveState(
            last_fired_at=10,
            consumed={"abc123", "def456"},
            echo_lemmas={"test", "code"},
            echo_window_end=20,
        )
        store[PrimitiveState].set(original)
        loaded = store[PrimitiveState].get()
        assert loaded is not None
        assert loaded.last_fired_at == 10
        assert loaded.consumed == {"abc123", "def456"}
        assert loaded.echo_lemmas == {"test", "code"}
        assert loaded.echo_window_end == 20

    def test_e2e_062_client_state_models_round_trip(self, tmp_path: Path) -> None:
        import sys

        discover_hooks(str(FIXTURES_DIR))

        ReviewLedger = sys.modules["client_hooks.workflow"].ReviewLedger

        store = SessionStore(tmp_path)
        store[ReviewLedger].set(ReviewLedger(reviewed_files=["a.py", "b.py"], pending=False))
        loaded = store[ReviewLedger].get()
        assert loaded is not None
        assert loaded.reviewed_files == ["a.py", "b.py"]
        assert loaded.pending is False


class TestEvtResultMethods:
    def test_e2e_070_evt_allow(self) -> None:
        evt = PreToolUseEvent(
            _raw={"tool_name": "Bash", "tool_input": {"command": "echo"}},
            ctx=MagicMock(),
        )
        result = evt.allow()
        assert result.action == Action.allow

    def test_e2e_071_evt_warn(self) -> None:
        evt = PreToolUseEvent(
            _raw={"tool_name": "Bash", "tool_input": {"command": "echo"}},
            ctx=MagicMock(),
        )
        result = evt.warn("be careful")
        assert result.action == Action.warn
        assert result.message == "be careful"

    def test_e2e_072_evt_block(self) -> None:
        evt = PreToolUseEvent(
            _raw={"tool_name": "Bash", "tool_input": {"command": "echo"}},
            ctx=MagicMock(),
        )
        result = evt.block("not allowed")
        assert result.action == Action.block
        assert result.message == "not allowed"

    def test_e2e_073_evt_error_on_failure_event(self) -> None:
        evt = PostToolUseFailureEvent(
            _raw={
                "tool_name": "Bash",
                "tool_input": {},
                "error": "ModuleNotFoundError: No module named 'foo'",
            },
            ctx=MagicMock(),
        )
        assert evt.error == "ModuleNotFoundError: No module named 'foo'"

    def test_e2e_074_evt_error_raises_on_missing(self) -> None:
        evt = PostToolUseFailureEvent(
            _raw={"tool_name": "Bash", "tool_input": {}},
            ctx=MagicMock(),
        )
        with pytest.raises(KeyError):
            evt.error

    def test_e2e_075_evt_allow_returns_hookresult(self) -> None:
        evt = StopEvent(_raw={}, ctx=MagicMock())
        result = evt.allow()
        assert isinstance(result, HookResult)
        assert result.action == Action.allow

    def test_e2e_076_evt_warn_returns_hookresult(self) -> None:
        evt = SubagentStopEvent(
            _raw={"agent_type": "worker"},
            ctx=MagicMock(),
        )
        result = evt.warn("check agent")
        assert isinstance(result, HookResult)
        assert result.action == Action.warn
        assert result.message == "check agent"

    def test_e2e_077_evt_block_returns_hookresult(self) -> None:
        evt = UserPromptSubmitEvent(
            _raw={"prompt": "test"},
            ctx=MagicMock(),
        )
        result = evt.block("not permitted")
        assert isinstance(result, HookResult)
        assert result.action == Action.block

    def test_e2e_078_no_error_on_non_failure_events(self) -> None:
        for cls in (PreToolUseEvent, PostToolUseEvent, StopEvent, SubagentStopEvent):
            evt = cls(
                _raw={"tool_name": "Bash", "tool_input": {}},
                ctx=MagicMock(),
            )
            has_own_error = "error" in type(evt).__dict__
            assert not has_own_error or cls is PostToolUseFailureEvent


class TestConditionsWithNoneTranscript:
    def test_e2e_080_transcript_conditions_safe_with_none(self, tmp_path: Path) -> None:
        from captain_hook.types import ReadFile

        register_hook(
            Event.PreToolUse,
            message="should not crash",
            skip_if=[ReadFile("STYLEGUIDE.md")],
        )
        ctx = build_ctx(transcript=None, session_dir=tmp_path)
        evt = PreToolUseEvent(
            _raw={"tool_name": "Bash", "tool_input": {"command": "echo hi"}},
            ctx=ctx,
        )
        matching = get_matching_hooks(evt)
        assert len(matching) == 1

    def test_e2e_081_ran_command_condition_safe_with_none(self, tmp_path: Path) -> None:
        from captain_hook.conditions import check_condition
        from captain_hook.types import RanCommand

        evt = PreToolUseEvent(
            _raw={"tool_name": "Bash", "tool_input": {"command": "echo"}},
            ctx=build_ctx(transcript=None, session_dir=tmp_path),
        )
        assert check_condition(RanCommand("uv", "run", "mtest"), evt) is False

    def test_e2e_082_used_skill_condition_safe_with_none(self, tmp_path: Path) -> None:
        from captain_hook.conditions import check_condition
        from captain_hook.types import UsedSkill

        evt = PreToolUseEvent(
            _raw={"tool_name": "Bash", "tool_input": {"command": "echo"}},
            ctx=build_ctx(transcript=None, session_dir=tmp_path),
        )
        assert check_condition(UsedSkill("codex"), evt) is False

    def test_e2e_083_in_plan_mode_safe_with_none(self, tmp_path: Path) -> None:
        from captain_hook.conditions import check_condition
        from captain_hook.types import InPlanMode

        evt = PreToolUseEvent(
            _raw={"tool_name": "Bash", "tool_input": {"command": "echo"}},
            ctx=build_ctx(transcript=None, session_dir=tmp_path),
        )
        assert check_condition(InPlanMode(), evt) is False
