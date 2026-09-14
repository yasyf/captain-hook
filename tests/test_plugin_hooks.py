from __future__ import annotations

import importlib.resources
import json
from typing import Any

from captain_hook.types import Event

PLUGIN_PREFIX = '"${CLAUDE_PLUGIN_ROOT}/bin/hook"'
SHIM_PREFIX = '"${CLAUDE_PLUGIN_ROOT}/bin/hook-shim"'
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


def prefix_for(name: str) -> str:
    return SHIM_PREFIX if name == Event.SessionStart.name else PLUGIN_PREFIX


def expected_command_set(name: str) -> set[str]:
    """The commands the shipped hooks.json must register for ``name`` — derived, not hardcoded.

    Every event registers both dispatch variants (sync + async) and nothing else: the reviewer and
    the throttled sweep now ride the async ``run <Event>`` dispatch natively, so no ``review run`` or
    ``review sweep`` entry is wired. SessionStart enters the Python shim, every other event the signed host.
    """
    command = f"{prefix_for(name)} run {name}"
    return {command, f"{command} --async"}


class TestPluginHooksJson:
    def test_parses_to_a_hooks_mapping(self) -> None:
        assert isinstance(load_plugin_hooks()["hooks"], dict)

    def test_ships_in_the_installed_package(self) -> None:
        # importlib.resources resolves against the installed package, so this only passes
        # when hooks/hooks.json is packaged as data alongside the module.
        assert (importlib.resources.files("captain_hook") / "hooks" / "hooks.json").is_file()

    def test_covers_every_event_enum_member(self) -> None:
        assert set(load_plugin_hooks()["hooks"]) == {name for e in Event if (name := e.name)}

    def test_every_command_is_canonical(self) -> None:
        # Drive off the Event enum, not the file's keys, so the shipped set must MATCH the
        # derivation (a dropped event fails here as well as in test_covers_every_event_enum_member).
        hooks = load_plugin_hooks()["hooks"]
        for name in (n for e in Event if (n := e.name)):
            [group] = hooks[name]
            entries = group["hooks"]
            assert all(entry["type"] == "command" for entry in entries)
            by_command = {entry["command"]: entry for entry in entries}
            sync_command = f"{prefix_for(name)} run {name}"
            async_command = f"{sync_command} --async"
            assert set(by_command) == expected_command_set(name)
            # Sync dispatcher foreground (restores additionalContext hooks); async is background.
            assert "async" not in by_command[sync_command]
            assert by_command[async_command]["async"] is True

    def test_ships_no_raw_review_or_sweep_entries(self) -> None:
        # The reviewer and sweep are native `run <Event>` dispatch now; no raw entry survives.
        commands = [
            entry["command"]
            for group in (grp for groups in load_plugin_hooks()["hooks"].values() for grp in groups)
            for entry in group["hooks"]
        ]
        assert not any("review run" in c or "review sweep" in c for c in commands)

    def test_session_start_dispatches_through_the_python_shim(self) -> None:
        """PIN: SessionStart carries the auto-updater, so it must dispatch on an app of any version.

        An app older than the Go hook grammar exits 2 on ``run <Event>``; the shim spells the flag
        grammar every app accepts, so the updater still runs and brings the app forward.
        """
        hooks = load_plugin_hooks()["hooks"]
        commands = {entry["command"] for group in hooks["SessionStart"] for entry in group["hooks"]}
        assert commands == {f"{SHIM_PREFIX} run SessionStart", f"{SHIM_PREFIX} run SessionStart --async"}
        others = {
            entry["command"]
            for name, groups in hooks.items()
            if name != "SessionStart"
            for group in groups
            for entry in group["hooks"]
        }
        assert all(command.startswith(f"{PLUGIN_PREFIX} run ") for command in others)

    def test_canonical_prefix_enters_the_signed_host_shim(self) -> None:
        assert PLUGIN_PREFIX == '"${CLAUDE_PLUGIN_ROOT}/bin/hook"'
        assert expected_command_set("PreToolUse") == {
            '"${CLAUDE_PLUGIN_ROOT}/bin/hook" run PreToolUse',
            '"${CLAUDE_PLUGIN_ROOT}/bin/hook" run PreToolUse --async',
        }


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
