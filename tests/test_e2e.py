from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from captain_hook.app import (
    _state,
    discover_hooks,
    get_matching_hooks,
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
from captain_hook.session import SessionStore
from captain_hook.state import HookState, PrimitiveState
from captain_hook.types import Action, Event, HookResult

PKG_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = PKG_DIR.parent.parent
CLIENT_DIR = PROJECT_ROOT / ".claude" / "hooks"


def run_cli(
    *args: str,
    stdin_data: str = "",
    hooks_dir: str | None = None,
    root_dir: str | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "captain_hook"]
    if hooks_dir:
        cmd.extend(["--hooks", hooks_dir])
    if root_dir:
        cmd.extend(["--root", root_dir])
    cmd.extend(args)
    return subprocess.run(
        cmd,
        input=stdin_data,
        capture_output=True,
        text=True,
        cwd=str(PKG_DIR),
    )


from conftest import build_ctx



class TestEntryPointExecution:
    def test_e2e_001_bin_hooks_run_produces_valid_json(self, tmp_path: Path) -> None:
        stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git stash"}})
        result = run_cli(
            "run",
            "PreToolUse",
            hooks_dir=str(CLIENT_DIR),
            root_dir=str(tmp_path),
            stdin_data=stdin,
        )
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "git stash" in output["hookSpecificOutput"]["permissionDecisionReason"]

    def test_e2e_002_bin_hooks_run_allows_safe_command(self, tmp_path: Path) -> None:
        stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})
        result = run_cli(
            "run",
            "PreToolUse",
            hooks_dir=str(CLIENT_DIR),
            root_dir=str(tmp_path),
            stdin_data=stdin,
        )
        assert result.returncode == 0

    def test_e2e_003_bin_hooks_run_stop_event(self, tmp_path: Path) -> None:
        stdin = json.dumps({"stop_hook_active": False})
        result = run_cli(
            "run",
            "Stop",
            hooks_dir=str(CLIENT_DIR),
            root_dir=str(tmp_path),
            stdin_data=stdin,
        )
        assert result.returncode == 0


class TestHookDiscovery:
    @pytest.fixture(autouse=True)
    def clean_hooks_modules(self) -> None:
        stale = [k for k in sys.modules if k == "hooks" or k.startswith("hooks.")]
        for k in stale:
            del sys.modules[k]
        sys.path[:] = [p for p in sys.path if not (Path(p) / "hooks").is_dir() or p == str(CLIENT_DIR.parent)]
        if str(CLIENT_DIR.parent) not in sys.path:
            sys.path.insert(0, str(CLIENT_DIR.parent))

    def test_e2e_010_all_client_files_discovered(self) -> None:
        discover_hooks(str(CLIENT_DIR))
        assert len(_state.hooks) > 0
        assert _state.settings is not None

    def test_e2e_011_conf_loaded_first(self) -> None:
        discover_hooks(str(CLIENT_DIR))
        assert type(_state.settings).__name__ == "BioqaSettings"
        assert hasattr(_state.settings, "test_command")
        assert _state.settings.test_command == "uv run mtest"

    def test_e2e_012_subpackage_workflow_discovered(self) -> None:
        discover_hooks(str(CLIENT_DIR))
        hook_names = {h.name for h in _state.hooks}
        assert any("task" in n for n in hook_names), f"No task hooks found in {hook_names}"

    def test_e2e_013_subpackage_testing_discovered(self) -> None:
        discover_hooks(str(CLIENT_DIR))
        hook_names = {h.name for h in _state.hooks}
        assert "commit_test_gate" in hook_names, f"commit_test_gate not in {hook_names}"
        assert "verify_test_execution" in hook_names, f"verify_test_execution not in {hook_names}"

    def test_e2e_014_no_import_errors(self) -> None:
        discover_hooks(str(CLIENT_DIR))

    def test_e2e_015_discovery_via_cli_subprocess(self, tmp_path: Path) -> None:
        stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}})
        result = run_cli(
            "run",
            "PreToolUse",
            hooks_dir=str(CLIENT_DIR),
            root_dir=str(tmp_path),
            stdin_data=stdin,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"


class TestInlineTests:
    def test_e2e_020_inline_tests_pass(self) -> None:
        discover_hooks(str(CLIENT_DIR))
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
    def test_e2e_030_pretooluse_block_json(self, tmp_path: Path) -> None:
        stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git stash"}})
        result = run_cli(
            "run",
            "PreToolUse",
            hooks_dir=str(CLIENT_DIR),
            root_dir=str(tmp_path),
            stdin_data=stdin,
        )
        assert result.returncode == 0
        output = json.loads(result.stdout)
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_e2e_031_pretooluse_allow_no_output(self, tmp_path: Path) -> None:
        stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hello"}})
        result = run_cli(
            "run",
            "PreToolUse",
            hooks_dir=str(CLIENT_DIR),
            root_dir=str(tmp_path),
            stdin_data=stdin,
        )
        assert result.returncode == 0

    def test_e2e_032_stop_event_dispatch(self, tmp_path: Path) -> None:
        stdin = json.dumps({})
        result = run_cli(
            "run",
            "Stop",
            hooks_dir=str(CLIENT_DIR),
            root_dir=str(tmp_path),
            stdin_data=stdin,
        )
        assert result.returncode == 0

    def test_e2e_033_user_prompt_submit_dispatch(self, tmp_path: Path) -> None:
        stdin = json.dumps({"prompt": "hello world"})
        result = run_cli(
            "run",
            "UserPromptSubmit",
            hooks_dir=str(CLIENT_DIR),
            root_dir=str(tmp_path),
            stdin_data=stdin,
        )
        assert result.returncode == 0

    def test_e2e_034_subagent_stop_dispatch(self, tmp_path: Path) -> None:
        stdin = json.dumps({"agent_type": "worker", "agent_id": "abc"})
        result = run_cli(
            "run",
            "SubagentStop",
            hooks_dir=str(CLIENT_DIR),
            root_dir=str(tmp_path),
            stdin_data=stdin,
        )
        assert result.returncode == 0

    def test_e2e_035_subagent_start_dispatch(self, tmp_path: Path) -> None:
        stdin = json.dumps({"agent_type": "cleanup", "agent_id": "abc"})
        result = run_cli(
            "run",
            "SubagentStart",
            hooks_dir=str(CLIENT_DIR),
            root_dir=str(tmp_path),
            stdin_data=stdin,
        )
        assert result.returncode == 0

    def test_e2e_036_post_tool_use_dispatch(self, tmp_path: Path) -> None:
        stdin = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}, "tool_response": "hi"})
        result = run_cli(
            "run",
            "PostToolUse",
            hooks_dir=str(CLIENT_DIR),
            root_dir=str(tmp_path),
            stdin_data=stdin,
        )
        assert result.returncode == 0

    def test_e2e_037_post_tool_use_failure_dispatch(self, tmp_path: Path) -> None:
        stdin = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "false"},
                "error": "command failed",
            }
        )
        result = run_cli(
            "run",
            "PostToolUseFailure",
            hooks_dir=str(CLIENT_DIR),
            root_dir=str(tmp_path),
            stdin_data=stdin,
        )
        assert result.returncode == 0

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


class TestGenerateSettings:
    def test_e2e_050_generate_settings_valid_json(self, tmp_path: Path) -> None:
        result = run_cli(
            "generate-settings",
            hooks_dir=str(CLIENT_DIR),
            root_dir=str(tmp_path),
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert "hooks" in data
        assert isinstance(data["hooks"], dict)

    def test_e2e_051_generate_settings_has_expected_events(self, tmp_path: Path) -> None:
        result = run_cli(
            "generate-settings",
            hooks_dir=str(CLIENT_DIR),
            root_dir=str(tmp_path),
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        events = set(data["hooks"].keys())
        assert "PreToolUse" in events
        assert "Stop" in events

    def test_e2e_052_generate_settings_commands_have_run(self, tmp_path: Path) -> None:
        result = run_cli(
            "generate-settings",
            "--run-command",
            "bin/hooks",
            hooks_dir=str(CLIENT_DIR),
            root_dir=str(tmp_path),
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        raw = json.dumps(data)
        assert "bin/hooks run" in raw


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

        discover_hooks(str(CLIENT_DIR))

        agents_mod = sys.modules["hooks.agents"]

        Snapshot = agents_mod.Snapshot
        RunStatus = agents_mod.RunStatus
        CleanupScope = agents_mod.CleanupScope
        ReviewerOutput = agents_mod.ReviewerOutput

        store = SessionStore(tmp_path)

        store[Snapshot].set(Snapshot(op_id="op123"))
        assert store[Snapshot].get() is not None
        assert store[Snapshot].get().op_id == "op123"  # type: ignore[union-attr]

        store[RunStatus].set(RunStatus(status="running"))
        assert store[RunStatus].get() is not None
        assert store[RunStatus].get().status == "running"  # type: ignore[union-attr]

        store[CleanupScope].set(
            CleanupScope(
                source_files=["a.py", "b.py"],
                test_files=["test_a.py"],
                skipped=[],
            )
        )
        loaded_scope = store[CleanupScope].get()
        assert loaded_scope is not None
        assert loaded_scope.source_files == ["a.py", "b.py"]

        store[ReviewerOutput].set(ReviewerOutput(tracks={"code", "test"}))
        loaded_ro = store[ReviewerOutput].get()
        assert loaded_ro is not None
        assert loaded_ro.tracks == {"code", "test"}


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
        assert check_condition(RanCommand(r"uv run mtest"), evt) is False

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
