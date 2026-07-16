from __future__ import annotations

import importlib.resources
import json
from typing import Any

from captain_hook.cli import DEFAULT_PREFIX, run_command
from captain_hook.types import Event


def load_plugin_hooks() -> dict[str, Any]:
    """Read the shipped plugin ``hooks/hooks.json`` from the installed package data."""
    resource = importlib.resources.files("captain_hook") / "hooks" / "hooks.json"
    return json.loads(resource.read_text())


def expected_command_set(name: str) -> set[str]:
    """The commands the shipped hooks.json must register for ``name`` — derived, not hardcoded.

    Every event registers both dispatch variants (sync + async) and nothing else: the reviewer and
    the throttled sweep now ride the async ``run <Event>`` dispatch natively, so no ``review run`` or
    ``review sweep`` entry is wired. Everything flows from ``DEFAULT_PREFIX``, so a prefix change is a
    one-line edit.
    """
    return {run_command(name, async_=False), run_command(name, async_=True)}


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
            # The command SET is fully derived from DEFAULT_PREFIX — sync + async, nothing else.
            assert set(by_command) == expected_command_set(name)
            # Sync dispatcher foreground (restores additionalContext hooks); async is background.
            assert "async" not in by_command[run_command(name, async_=False)]
            assert by_command[run_command(name, async_=True)]["async"] is True

    def test_ships_no_raw_review_or_sweep_entries(self) -> None:
        # The reviewer and sweep are native `run <Event>` dispatch now; no raw entry survives.
        commands = [
            entry["command"]
            for group in (grp for groups in load_plugin_hooks()["hooks"].values() for grp in groups)
            for entry in group["hooks"]
        ]
        assert not any("review run" in c or "review sweep" in c for c in commands)

    def test_canonical_prefix_is_uvx_isolated_capt_hook(self) -> None:
        assert DEFAULT_PREFIX == "uvx --isolated capt-hook"
        assert run_command("PreToolUse", async_=False) == "uvx --isolated capt-hook run PreToolUse"
        assert run_command("Stop", async_=True) == "uvx --isolated capt-hook run Stop --async"
