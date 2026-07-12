from __future__ import annotations

import importlib.resources
import json
from typing import Any

from captain_hook.cli import DEFAULT_PREFIX, review_command, run_command
from captain_hook.types import Event

# Events the plugin registers under `run --async` rather than a sync `run`. REVIEW_EVENT
# (SessionEnd) carries two async entries: the always-on session reviewer (`review run`) and
# a `run SessionEnd --async` so async_=True SessionEnd handlers still reach dispatch().
ASYNC_RUN_EVENTS = {"SessionStart"}
REVIEW_EVENT = "SessionEnd"


def load_plugin_hooks() -> dict[str, Any]:
    """Read the shipped plugin ``hooks/hooks.json`` from the installed package data."""
    resource = importlib.resources.files("captain_hook") / "hooks" / "hooks.json"
    return json.loads(resource.read_text())


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
        for name, groups in load_plugin_hooks()["hooks"].items():
            [group] = groups
            entries = group["hooks"]
            assert all(entry["type"] == "command" for entry in entries)
            if name == REVIEW_EVENT:
                assert {entry["command"] for entry in entries} == {
                    review_command(),
                    run_command("SessionEnd", async_=True),
                }
                assert all(entry["async"] is True for entry in entries)
            elif name in ASYNC_RUN_EVENTS:
                [entry] = entries
                assert entry["command"] == run_command(name, async_=True)
                assert entry["async"] is True
            else:
                [entry] = entries
                assert entry["command"] == run_command(name, async_=False)
                assert "async" not in entry

    def test_canonical_prefix_is_uvx_capt_hook(self) -> None:
        assert DEFAULT_PREFIX == "uvx capt-hook"
        assert run_command("PreToolUse", async_=False) == "uvx capt-hook run PreToolUse"
        assert review_command() == "uvx capt-hook review run"
