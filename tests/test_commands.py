from __future__ import annotations

import re
from pathlib import Path

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


class TestBlockCommandRegex:
    def test_blocks_matching_command(self, tmp_path: Path) -> None:
        block_command(r"git\s+stash", reason="No stashing allowed")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git stash pop"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in get_matching_hooks(evt)]
        assert any(r and r.action == Action.block for r in results)

    def test_non_matching_command_passes(self, tmp_path: Path) -> None:
        block_command(r"git\s+stash", reason="No stashing")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git commit -m test"}, ctx)
        matching = get_matching_hooks(evt)
        assert not matching


class TestBlockCommandTokenList:
    def test_token_list_blocks_matching(self, tmp_path: Path) -> None:
        block_command(["git", "stash"], reason="No stashing")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git stash pop"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in get_matching_hooks(evt)]
        assert any(r and r.action == Action.block for r in results)


class TestBlockCommandMessage:
    def test_message_includes_reason(self, tmp_path: Path) -> None:
        block_command(r"git\s+stash", reason="No stashing allowed")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git stash"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in get_matching_hooks(evt)]
        result = next(r for r in results if r)
        assert "BLOCKED: No stashing allowed." in result.message

    def test_message_includes_hint_when_provided(self, tmp_path: Path) -> None:
        block_command(r"git\s+stash", reason="No stashing", hint="Use git commit instead")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git stash"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in get_matching_hooks(evt)]
        result = next(r for r in results if r)
        assert "BLOCKED: No stashing." in result.message
        assert "Use git commit instead." in result.message

    def test_message_without_hint(self, tmp_path: Path) -> None:
        block_command(r"git\s+stash", reason="No stashing")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git stash"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in get_matching_hooks(evt)]
        result = next(r for r in results if r)
        assert result.message == "BLOCKED: No stashing."


class TestWarnCommandRegex:
    def test_warns_matching_command(self, tmp_path: Path) -> None:
        warn_command(r"git\s+push", message="Be careful with push")
        ctx = make_ctx(tmp_path)
        evt = make_post_tool_event("Bash", {"command": "git push origin"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn for r in results)


class TestWarnCommandDefaultEvent:
    def test_default_event_is_post_tool_use(self) -> None:
        warn_command(r"git\s+push", message="Be careful")
        assert len(_state.hooks) == 1
        assert _state.hooks[-1].spec.events == Event.PostToolUse


class TestWarnCommandEventOverride:
    def test_event_override_to_pre_tool_use(self, tmp_path: Path) -> None:
        warn_command(r"git\s+push", message="Be careful", events=Event.PreToolUse)
        assert _state.hooks[-1].spec.events == Event.PreToolUse
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git push origin"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn for r in results)


class TestBlockCommandOnlyBashExecute:
    def test_edit_tool_not_matched(self, tmp_path: Path) -> None:
        block_command(r"git\s+stash", reason="No stashing")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Edit", {"file_path": "a.py", "old_string": "git stash", "new_string": "x"}, ctx)
        matching = get_matching_hooks(evt)
        assert not matching

    def test_execute_tool_matched(self, tmp_path: Path) -> None:
        block_command(r"git\s+stash", reason="No stashing")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Execute", {"command": "git stash pop"}, ctx)
        matching = get_matching_hooks(evt)
        assert len(matching) == 1


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


class TestBlockCommandChainedCommands:
    def test_blocks_guarded_command_not_in_primary(self, tmp_path: Path) -> None:
        block_command(r"git\s+push", reason="No pushing")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git push && echo done"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in get_matching_hooks(evt)]
        assert any(r and r.action == Action.block for r in results)

    def test_blocks_guarded_command_in_semicolon_chain(self, tmp_path: Path) -> None:
        block_command(r"git\s+stash", reason="No stashing")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git stash; git push"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in get_matching_hooks(evt)]
        assert any(r and r.action == Action.block for r in results)

    def test_blocks_guarded_command_in_pipe_chain(self, tmp_path: Path) -> None:
        block_command(r"rm\s+-rf", reason="No rm -rf")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "rm -rf /tmp/dir | tee log.txt"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in get_matching_hooks(evt)]
        assert any(r and r.action == Action.block for r in results)

    def test_non_matching_chain_passes(self, tmp_path: Path) -> None:
        block_command(r"git\s+push", reason="No pushing")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "echo done && git commit"}, ctx)
        matching = get_matching_hooks(evt)
        assert not matching


class TestWarnCommandChainedCommands:
    def test_warns_guarded_command_not_in_primary(self, tmp_path: Path) -> None:
        warn_command(r"git\s+push", message="Be careful", events=Event.PreToolUse)
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git push && echo done"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn for r in results)


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
