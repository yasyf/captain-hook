from __future__ import annotations

import importlib.resources
import json
from typing import Any

from captain_hook.types import Event

PLUGIN_PREFIX = '"${CLAUDE_PLUGIN_ROOT}/bin/hook"'
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
    return f"{PLUGIN_PREFIX} run {name}"


class TestPluginHooksJson:
    def test_parses_to_a_hooks_mapping(self) -> None:
        assert isinstance(load_plugin_hooks()["hooks"], dict)

    def test_ships_in_the_installed_package(self) -> None:
        # importlib.resources resolves against the installed package, so this only passes
        # when hooks/hooks.json is packaged as data alongside the module.
        assert (importlib.resources.files("captain_hook") / "hooks" / "hooks.json").is_file()

    def test_covers_every_event_enum_member(self) -> None:
        assert set(load_plugin_hooks()["hooks"]) == {name for e in Event if (name := e.name)}

    def test_every_event_registers_exactly_the_binrun_hook(self) -> None:
        hooks = load_plugin_hooks()["hooks"]
        for name in (n for e in Event if (n := e.name)):
            [group] = hooks[name]
            timeout = {"timeout": 10} if name == Event.MessageDisplay.name else {}
            assert group["hooks"] == [{"type": "command", "command": expected_command(name)} | timeout]

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
