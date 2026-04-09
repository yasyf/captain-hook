from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from captain_hook.app import _state, discover_hooks, get_matching_hooks
from captain_hook.dispatch import dispatch
from captain_hook.testing.helpers import mock_tool_event
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

    def test_creates_bin_script(self, project_dir: Path) -> None:
        script = project_dir / ".claude" / "bin" / "captain-hook"
        assert script.exists()
        import os
        assert os.access(script, os.X_OK)

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

    def test_settings_commands_reference_bin(self, project_dir: Path) -> None:
        settings = project_dir / ".claude" / "settings.local.json"
        raw = settings.read_text()
        assert ".claude/bin/captain-hook" in raw


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
