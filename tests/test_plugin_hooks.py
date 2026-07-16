from __future__ import annotations

import importlib.resources
import json
from typing import Any

from captain_hook.cli import DEFAULT_PREFIX, review_command, run_command
from captain_hook.types import Event

# Both SessionStart and SessionEnd additionally register the always-on session reviewer.
REVIEW_EVENTS = {"SessionStart", "SessionEnd"}
# Stop additionally registers the throttled repo-wide reviewer sweep.
SWEEP_EVENTS = {"Stop"}


def review_sweep_command() -> str:
    """The shipped Stop-sweep command, derived from ``DEFAULT_PREFIX`` like the run/dispatch commands."""
    return f"{DEFAULT_PREFIX} review sweep"


def load_plugin_hooks() -> dict[str, Any]:
    """Read the shipped plugin ``hooks/hooks.json`` from the installed package data."""
    resource = importlib.resources.files("captain_hook") / "hooks" / "hooks.json"
    return json.loads(resource.read_text())


def expected_command_set(name: str) -> set[str]:
    """The commands the shipped hooks.json must register for ``name`` — derived, not hardcoded.

    Every event registers both dispatch variants (sync + async); the review-run events add the
    reviewer pass, and Stop adds the throttled sweep. Everything flows from ``DEFAULT_PREFIX``, so a
    prefix change is a one-line edit.
    """
    commands = {run_command(name, async_=False), run_command(name, async_=True)}
    if name in REVIEW_EVENTS:
        commands |= {review_command()}
    if name in SWEEP_EVENTS:
        commands |= {review_sweep_command()}
    return commands


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
            # The command SET is fully derived from DEFAULT_PREFIX (+ the review-run special cases).
            assert set(by_command) == expected_command_set(name)
            # The sync dispatcher runs foreground (restores additionalContext SessionStart hooks —
            # the announcer, context injection — that 9.0.0's async-only registration silenced); the
            # async dispatcher and the review-run sweep are background.
            assert "async" not in by_command[run_command(name, async_=False)]
            assert by_command[run_command(name, async_=True)]["async"] is True
            if name in REVIEW_EVENTS:
                assert by_command[review_command()]["async"] is True
            if name in SWEEP_EVENTS:
                assert by_command[review_sweep_command()]["async"] is True

    def test_stop_block_ships_the_async_sweep_entry(self) -> None:
        [group] = load_plugin_hooks()["hooks"]["Stop"]
        entries = {entry["command"]: entry for entry in group["hooks"]}
        assert review_sweep_command() in entries
        assert entries[review_sweep_command()] == {
            "type": "command",
            "command": "uvx --isolated capt-hook review sweep",
            "async": True,
        }

    def test_canonical_prefix_is_uvx_isolated_capt_hook(self) -> None:
        assert DEFAULT_PREFIX == "uvx --isolated capt-hook"
        assert run_command("PreToolUse", async_=False) == "uvx --isolated capt-hook run PreToolUse"
        assert review_command() == "uvx --isolated capt-hook review run"
        assert review_sweep_command() == "uvx --isolated capt-hook review sweep"
