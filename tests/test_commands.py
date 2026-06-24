from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from captain_hook.app import (
    _state,
    get_matching_hooks,
)
from captain_hook.dispatch import execute_hook
from captain_hook.primitives.commands import block_command, block_command_pattern, warn_command
from captain_hook.tests.helpers import make_ctx, make_post_tool_event, make_pre_tool_event
from captain_hook.types import (
    Action,
    Event,
)


def block_stash() -> None:
    block_command(r"git\s+stash", reason="No stashing")


def run_hook(tmp_path: Path, tool: str, tool_input: dict[str, Any], *, post: bool = False) -> list:
    ctx = make_ctx(tmp_path)
    build = make_post_tool_event if post else make_pre_tool_event
    evt = build(tool, tool_input, ctx)
    return [execute_hook(entry, evt, tmp_path) for entry in get_matching_hooks(evt)]


def matching_hooks(tmp_path: Path, tool: str, tool_input: dict[str, Any]) -> list:
    return get_matching_hooks(make_pre_tool_event(tool, tool_input, make_ctx(tmp_path)))


class TestBlockCommandRegex:
    @pytest.mark.parametrize(
        ("register", "command", "post", "expected"),
        [
            pytest.param(
                lambda: block_command(r"git\s+stash", reason="No stashing allowed"),
                "git stash pop",
                False,
                Action.block,
                id="blocks_matching_command",
            ),
            pytest.param(
                lambda: block_command(["git", "stash"], reason="No stashing"),
                "git stash pop",
                False,
                Action.block,
                id="token_list_blocks_matching",
            ),
            pytest.param(
                lambda: warn_command(r"git\s+push", message="Be careful with push"),
                "git push origin",
                True,
                Action.warn,
                id="warns_matching_command",
            ),
            pytest.param(
                lambda: block_command(r"git\s+push", reason="No pushing"),
                "git push && echo done",
                False,
                Action.block,
                id="blocks_guarded_command_not_in_primary",
            ),
            pytest.param(
                block_stash,
                "git stash; git push",
                False,
                Action.block,
                id="blocks_guarded_command_in_semicolon_chain",
            ),
            pytest.param(
                lambda: block_command(r"rm\s+-rf", reason="No rm -rf"),
                "rm -rf /tmp/dir | tee log.txt",
                False,
                Action.block,
                id="blocks_guarded_command_in_pipe_chain",
            ),
            pytest.param(
                lambda: warn_command(r"git\s+push", message="Be careful", events=Event.PreToolUse),
                "git push && echo done",
                False,
                Action.warn,
                id="warns_guarded_command_not_in_primary",
            ),
        ],
    )
    def test_matching_command_triggers_action(
        self, tmp_path: Path, register: Callable[[], None], command: str, post: bool, expected: Action
    ) -> None:
        register()
        results = run_hook(tmp_path, "Bash", {"command": command}, post=post)
        assert any(r and r.action == expected for r in results)

    @pytest.mark.parametrize(
        ("register", "tool", "tool_input"),
        [
            pytest.param(
                block_stash,
                "Bash",
                {"command": "git commit -m test"},
                id="non_matching_command_passes",
            ),
            pytest.param(
                block_stash,
                "Edit",
                {"file_path": "a.py", "old_string": "git stash", "new_string": "x"},
                id="edit_tool_not_matched",
            ),
            pytest.param(
                lambda: block_command(r"git\s+push", reason="No pushing"),
                "Bash",
                {"command": "echo done && git commit"},
                id="non_matching_chain_passes",
            ),
        ],
    )
    def test_non_matching_does_not_match(
        self, tmp_path: Path, register: Callable[[], None], tool: str, tool_input: dict[str, Any]
    ) -> None:
        register()
        assert not matching_hooks(tmp_path, tool, tool_input)


class TestBlockCommandMessage:
    def test_message_includes_reason(self, tmp_path: Path) -> None:
        block_command(r"git\s+stash", reason="No stashing allowed")
        result = next(r for r in run_hook(tmp_path, "Bash", {"command": "git stash"}) if r)
        assert "BLOCKED: No stashing allowed." in result.message

    def test_message_includes_hint_when_provided(self, tmp_path: Path) -> None:
        block_command(r"git\s+stash", reason="No stashing", hint="Use git commit instead")
        result = next(r for r in run_hook(tmp_path, "Bash", {"command": "git stash"}) if r)
        assert "BLOCKED: No stashing." in result.message
        assert "Use git commit instead." in result.message

    def test_message_without_hint(self, tmp_path: Path) -> None:
        block_command(r"git\s+stash", reason="No stashing")
        result = next(r for r in run_hook(tmp_path, "Bash", {"command": "git stash"}) if r)
        assert result.message == "BLOCKED: No stashing."


class TestWarnCommandDefaultEvent:
    def test_default_event_is_post_tool_use(self) -> None:
        warn_command(r"git\s+push", message="Be careful")
        assert len(_state.hooks) == 1
        assert _state.hooks[-1].spec.events == Event.PostToolUse


class TestWarnCommandEventOverride:
    def test_event_override_to_pre_tool_use(self, tmp_path: Path) -> None:
        warn_command(r"git\s+push", message="Be careful", events=Event.PreToolUse)
        assert _state.hooks[-1].spec.events == Event.PreToolUse
        results = run_hook(tmp_path, "Bash", {"command": "git push origin"})
        assert any(r and r.action == Action.warn for r in results)


class TestBlockCommandOnlyBashExecute:
    def test_execute_tool_matched(self, tmp_path: Path) -> None:
        block_command(r"git\s+stash", reason="No stashing")
        assert len(matching_hooks(tmp_path, "Execute", {"command": "git stash pop"})) == 1


class TestTokensToRegexWildcardAlternation:
    def test_wildcard_becomes_non_whitespace(self) -> None:
        regex = block_command_pattern(["git", "push|pull", "*"])
        assert re.match(regex, "git push origin")
        assert re.match(regex, "git pull remote")
        assert not re.match(regex, "git commit message")

    def test_alternation_with_special_chars(self) -> None:
        regex = block_command_pattern(["git", "push|pull"])
        assert re.match(regex, "git push")
        assert re.match(regex, "git pull")

    def test_plain_tokens_escaped(self) -> None:
        regex = block_command_pattern(["git", "stash"])
        assert re.match(regex, "git stash pop")
        assert not re.match(regex, "git  commit")


class TestTestsDictPropagation:
    @pytest.mark.parametrize(
        ("register", "kwargs"),
        [
            (block_command, {"pattern": r"git\s+stash", "reason": "No stashing"}),
            (warn_command, {"pattern": r"git\s+push", "message": "Warning"}),
        ],
        ids=["block_command", "warn_command"],
    )
    def test_propagates_tests(self, register, kwargs) -> None:
        tests = {"input": "test", "expected": "result"}
        register(**kwargs, tests=tests)
        assert _state.hooks[-1].spec.tests is tests

    @pytest.mark.parametrize(
        ("register", "kwargs"),
        [
            (block_command, {"pattern": r"git\s+stash", "reason": "No stashing"}),
            (warn_command, {"pattern": r"git\s+push", "message": "Warning"}),
        ],
        ids=["block_command", "warn_command"],
    )
    def test_tests_none_by_default(self, register, kwargs) -> None:
        register(**kwargs)
        assert _state.hooks[-1].spec.tests is None
