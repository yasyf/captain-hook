from __future__ import annotations

import importlib.resources
import json
import subprocess
from pathlib import Path
from typing import Any

from captain_hook.types import Event

CLIENT = 'c="$HOME/.daemonkit/bin/capt-hookd"; '
BUNDLE_HOST = '"$HOME/Applications/Captain Hook.app/Contents/Helpers/capt-hookd"'
MCP_SERVER_NAME = "capt-hook"
MCP_COMMAND = "${CLAUDE_PLUGIN_ROOT}/bin/capt-hook"
MCP_ARGS = ["mcp"]


def load_plugin_hooks() -> dict[str, Any]:
    """Read the shipped plugin ``hooks/hooks.json`` from the installed package data."""
    resource = importlib.resources.files("captain_hook") / "hooks" / "hooks.json"
    return json.loads(resource.read_text())


def load_mcp_config() -> dict[str, Any]:
    """Read the shipped ``.mcp.json`` from the installed package data."""
    resource = importlib.resources.files("captain_hook") / ".mcp.json"
    return json.loads(resource.read_text())


def expected_command(name: str) -> str:
    if name == Event.SessionStart.name:
        return f'{CLIENT}[ -x "$c" ] && exec "$c" run SessionStart; exec {BUNDLE_HOST} run SessionStart --async'
    return f'{CLIENT}[ -x "$c" ] || exit 0; exec "$c" run {name}'


class TestPluginHooksJson:
    def test_parses_to_a_hooks_mapping(self) -> None:
        assert isinstance(load_plugin_hooks()["hooks"], dict)

    def test_ships_in_the_installed_package(self) -> None:
        # importlib.resources resolves against the installed package, so this only passes
        # when hooks/hooks.json is packaged as data alongside the module.
        assert (importlib.resources.files("captain_hook") / "hooks" / "hooks.json").is_file()

    def test_covers_every_event_enum_member(self) -> None:
        assert set(load_plugin_hooks()["hooks"]) == {name for e in Event if (name := e.name)}

    def test_every_event_registers_exactly_the_plain_client(self) -> None:
        hooks = load_plugin_hooks()["hooks"]
        for name in (n for e in Event if (n := e.name)):
            [group] = hooks[name]
            assert group["hooks"] == [{"type": "command", "command": expected_command(name)}]

    def test_ships_no_raw_review_or_sweep_entries(self) -> None:
        # The reviewer and sweep are native `run <Event>` dispatch now; no raw entry survives.
        commands = [
            entry["command"]
            for group in (grp for groups in load_plugin_hooks()["hooks"].values() for grp in groups)
            for entry in group["hooks"]
        ]
        assert not any("review run" in c or "review sweep" in c for c in commands)


class TestMcpJson:
    def test_ships_in_the_installed_package(self) -> None:
        assert (importlib.resources.files("captain_hook") / ".mcp.json").is_file()

    def test_registers_only_the_capt_hook_server(self) -> None:
        assert set(load_mcp_config()["mcpServers"]) == {MCP_SERVER_NAME}

    def test_command_is_the_plugin_binrun_wrapper(self) -> None:
        server = load_mcp_config()["mcpServers"][MCP_SERVER_NAME]
        assert server["command"] == MCP_COMMAND
        assert server["args"] == MCP_ARGS

    def test_ships_no_unpinned_uvx_launcher(self) -> None:
        server = load_mcp_config()["mcpServers"][MCP_SERVER_NAME]
        assert server["command"] == "${CLAUDE_PLUGIN_ROOT}/bin/capt-hook"
        assert "uvx" not in {server["command"], *server["args"]}


def run_hook_command(name: str, home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", "-c", expected_command(name)],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def fake_executable(path: Path, label: str) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(f'#!/bin/sh\necho "{label} $*"\n')
    path.chmod(0o755)


class TestHookCommandShell:
    def test_runs_the_installed_client(self, tmp_path: Path) -> None:
        fake_executable(tmp_path / ".daemonkit/bin/capt-hookd", "client")
        assert run_hook_command("PreToolUse", tmp_path).stdout == "client run PreToolUse\n"
        assert run_hook_command("SessionStart", tmp_path).stdout == "client run SessionStart\n"

    def test_a_missing_client_skips_the_event_without_blocking(self, tmp_path: Path) -> None:
        fake_executable(tmp_path / "Applications/Captain Hook.app/Contents/Helpers/capt-hookd", "bundle")
        result = run_hook_command("PreToolUse", tmp_path)
        assert (result.returncode, result.stdout) == (0, "")

    def test_session_start_without_a_client_runs_the_bundles_async_pass(self, tmp_path: Path) -> None:
        fake_executable(tmp_path / "Applications/Captain Hook.app/Contents/Helpers/capt-hookd", "bundle")
        assert run_hook_command("SessionStart", tmp_path).stdout == "bundle run SessionStart --async\n"
