from __future__ import annotations

import importlib.resources
import json
from typing import Any

from captain_hook.cli import DEFAULT_PREFIX, review_command, run_command
from captain_hook.types import Event


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
            by_command = {entry["command"]: entry for entry in entries}
            match name:
                case "SessionStart":
                    # Sync dispatcher (restores additionalContext SessionStart hooks — the
                    # announcer, context injection — that 9.0.0's async-only registration
                    # silenced), async dispatcher, and the still-open-session review sweep.
                    assert set(by_command) == {
                        run_command(name, async_=False),
                        run_command(name, async_=True),
                        review_command(),
                    }
                    assert "async" not in by_command[run_command(name, async_=False)]
                    assert by_command[run_command(name, async_=True)]["async"] is True
                    assert by_command[review_command()]["async"] is True
                case "SessionEnd":
                    # Sync dispatcher (a default synchronous SessionEnd handler), async
                    # dispatcher (a pack's async_=True SessionEnd handler), and the
                    # always-on session reviewer (`review run`); both non-sync entries async.
                    assert set(by_command) == {
                        run_command(name, async_=False),
                        run_command(name, async_=True),
                        review_command(),
                    }
                    assert "async" not in by_command[run_command(name, async_=False)]
                    assert by_command[run_command(name, async_=True)]["async"] is True
                    assert by_command[review_command()]["async"] is True
                case _:
                    # Every other event registers both dispatch variants so a hook declared
                    # either synchronous or async_=True reaches dispatch() (which filters on
                    # the exact variant); the once-guard keys on the variant, so both fire.
                    assert set(by_command) == {run_command(name, async_=False), run_command(name, async_=True)}
                    assert "async" not in by_command[run_command(name, async_=False)]
                    assert by_command[run_command(name, async_=True)]["async"] is True

    def test_canonical_prefix_is_uvx_isolated_capt_hook(self) -> None:
        assert DEFAULT_PREFIX == "uvx --isolated capt-hook"
        assert run_command("PreToolUse", async_=False) == "uvx --isolated capt-hook run PreToolUse"
        assert review_command() == "uvx --isolated capt-hook review run"
