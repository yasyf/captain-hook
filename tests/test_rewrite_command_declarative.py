from __future__ import annotations

from pathlib import Path

import pytest

from captain_hook.app import get_matching_hooks
from captain_hook.dispatch import execute_hook
from captain_hook.primitives.commands import rewrite_command
from captain_hook.tests.helpers import make_ctx, make_pre_tool_event
from captain_hook.types import Action, Command


def fire(tmp_path: Path, command: str):
    ctx = make_ctx(tmp_path)
    evt = make_pre_tool_event("Bash", {"command": command}, ctx)
    return [execute_hook(entry, evt, tmp_path) for entry in get_matching_hooks(evt)]


class TestConditionalRewrite:
    def test_rewrites_when_to_returns_command(self, tmp_path: Path) -> None:
        rewrite_command(
            only_if=[Command(r"^cat\s")],
            to=lambda evt: f"ccx read {evt.command.split()[1]} --full",
            note="Rewrote cat to ccx read",
        )
        result = next(r for r in fire(tmp_path, "cat foo.py") if r)
        assert result.action == Action.rewrite
        assert result.updated_input == {"command": "ccx read foo.py --full"}
        assert result.note == "Rewrote cat to ccx read"

    def test_blocks_when_to_returns_none_and_block_given(self, tmp_path: Path) -> None:
        rewrite_command(only_if=[Command(r"git\s+push")], to=lambda evt: None, block="Pushing is disabled")
        result = next(r for r in fire(tmp_path, "git push origin") if r)
        assert result.action == Action.block
        assert result.message == "Pushing is disabled"

    def test_passes_through_when_to_returns_none_without_block(self, tmp_path: Path) -> None:
        rewrite_command(only_if=[Command(r"echo")], to=lambda evt: None)
        assert fire(tmp_path, "echo hi") == [None]

    def test_callable_note_computed_from_event(self, tmp_path: Path) -> None:
        rewrite_command(
            only_if=[Command(r"^ls")],
            to=lambda evt: "ccx find '*'",
            note=lambda evt: f"was: {evt.command}",
        )
        result = next(r for r in fire(tmp_path, "ls -la") if r)
        assert result.action == Action.rewrite
        assert result.note == "was: ls -la"

    def test_only_bash_tool_matched(self, tmp_path: Path) -> None:
        rewrite_command(only_if=[Command(r"^cat\s")], to=lambda evt: "ccx read x --full")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Edit", {"file_path": "a.py", "old_string": "cat x", "new_string": "y"}, ctx)
        assert not get_matching_hooks(evt)


class TestRegexFormStillWorks:
    def test_regex_rewrite(self, tmp_path: Path) -> None:
        rewrite_command(r"^cat\s+(\S+)$", r"ccx read \1 --full", note="Rewrote cat to ccx read")
        result = next(r for r in fire(tmp_path, "cat bar.py") if r)
        assert result.action == Action.rewrite
        assert result.updated_input == {"command": "ccx read bar.py --full"}
        assert result.note == "Rewrote cat to ccx read"


class TestModeValidation:
    def test_neither_mode_raises(self) -> None:
        with pytest.raises(TypeError, match="either"):
            rewrite_command()

    def test_both_modes_raises(self) -> None:
        with pytest.raises(TypeError, match="either"):
            rewrite_command(r"^cat", r"ccx read", to=lambda evt: "x")
