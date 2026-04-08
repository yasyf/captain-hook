from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from captain_hook.app import HookApp, _current_app
from captain_hook.dispatch import execute_hook
from captain_hook.events import PostToolUseEvent, PreToolUseEvent
from captain_hook.primitives.commands import block_command, warn_command
from captain_hook.session import SessionStore
from captain_hook.types import (
    Action,
    Event,
    tokens_to_regex,
)


def make_ctx(tmp_path: Path | None = None, transcript_len: int = 10) -> MagicMock:
    ctx = MagicMock()
    ctx.transcript = MagicMock()
    ctx.transcript.__len__ = MagicMock(return_value=transcript_len)
    ctx.transcript.has_skill = MagicMock(return_value=False)
    ctx.transcript.has_read = MagicMock(return_value=False)
    ctx.transcript.has_edit_to = MagicMock(return_value=False)
    ctx.transcript.has_command = MagicMock(return_value=False)
    ctx.transcript.count_tools = MagicMock(return_value=0)
    ctx.t = ctx.transcript
    store = SessionStore(tmp_path)
    ctx.session = store
    ctx.s = store
    turn = MagicMock()
    turn.start_idx = 5
    ctx.turn = turn
    return ctx


def make_pre_tool_event(
    tool_name: str = "Bash",
    tool_input: dict[str, Any] | None = None,
    ctx: Any = None,
) -> PreToolUseEvent:
    raw: dict[str, Any] = {"tool_name": tool_name}
    if tool_input is not None:
        raw["tool_input"] = tool_input
    return PreToolUseEvent(_raw=raw, ctx=ctx or make_ctx())


def make_post_tool_event(
    tool_name: str = "Bash",
    tool_input: dict[str, Any] | None = None,
    ctx: Any = None,
) -> PostToolUseEvent:
    raw: dict[str, Any] = {"tool_name": tool_name}
    if tool_input is not None:
        raw["tool_input"] = tool_input
    return PostToolUseEvent(_raw=raw, ctx=ctx or make_ctx())


@pytest.fixture
def app(tmp_path: Path) -> HookApp:
    a = HookApp()
    token = _current_app.set(a)
    yield a
    _current_app.reset(token)
    a.reset()


class TestBlockCommandRegex:
    def test_blocks_matching_command(self, app: HookApp, tmp_path: Path) -> None:
        block_command(r"git\s+stash", reason="No stashing allowed")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git stash pop"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.block for r in results)

    def test_non_matching_command_passes(self, app: HookApp, tmp_path: Path) -> None:
        block_command(r"git\s+stash", reason="No stashing")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git commit -m test"}, ctx)
        matching = app.get_matching_hooks(evt)
        assert not matching


class TestBlockCommandTokenList:
    def test_token_list_blocks_matching(self, app: HookApp, tmp_path: Path) -> None:
        block_command(["git", "stash"], reason="No stashing")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git stash pop"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.block for r in results)


class TestBlockCommandMessage:
    def test_message_includes_reason(self, app: HookApp, tmp_path: Path) -> None:
        block_command(r"git\s+stash", reason="No stashing allowed")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git stash"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in app.get_matching_hooks(evt)]
        result = next(r for r in results if r)
        assert "BLOCKED: No stashing allowed." in result.message

    def test_message_includes_hint_when_provided(self, app: HookApp, tmp_path: Path) -> None:
        block_command(r"git\s+stash", reason="No stashing", hint="Use git commit instead")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git stash"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in app.get_matching_hooks(evt)]
        result = next(r for r in results if r)
        assert "BLOCKED: No stashing." in result.message
        assert "Use git commit instead." in result.message

    def test_message_without_hint(self, app: HookApp, tmp_path: Path) -> None:
        block_command(r"git\s+stash", reason="No stashing")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git stash"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in app.get_matching_hooks(evt)]
        result = next(r for r in results if r)
        assert result.message == "BLOCKED: No stashing."


class TestWarnCommandRegex:
    def test_warns_matching_command(self, app: HookApp, tmp_path: Path) -> None:
        warn_command(r"git\s+push", message="Be careful with push")
        ctx = make_ctx(tmp_path)
        evt = make_post_tool_event("Bash", {"command": "git push origin"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn for r in results)


class TestWarnCommandDefaultEvent:
    def test_default_event_is_post_tool_use(self, app: HookApp) -> None:
        warn_command(r"git\s+push", message="Be careful")
        assert len(app.hooks) == 1
        assert app.hooks[-1].spec.events == Event.PostToolUse


class TestWarnCommandEventOverride:
    def test_event_override_to_pre_tool_use(self, app: HookApp, tmp_path: Path) -> None:
        warn_command(r"git\s+push", message="Be careful", events=Event.PreToolUse)
        assert app.hooks[-1].spec.events == Event.PreToolUse
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git push origin"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn for r in results)


class TestBlockCommandOnlyBashExecute:
    def test_edit_tool_not_matched(self, app: HookApp, tmp_path: Path) -> None:
        block_command(r"git\s+stash", reason="No stashing")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Edit", {"file_path": "a.py", "old_string": "git stash", "new_string": "x"}, ctx)
        matching = app.get_matching_hooks(evt)
        assert not matching

    def test_execute_tool_matched(self, app: HookApp, tmp_path: Path) -> None:
        block_command(r"git\s+stash", reason="No stashing")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Execute", {"command": "git stash pop"}, ctx)
        matching = app.get_matching_hooks(evt)
        assert len(matching) == 1


class TestTokensToRegexWildcardAlternation:
    def test_wildcard_becomes_non_whitespace(self) -> None:
        regex = tokens_to_regex(["git", "push|pull", "*"])
        assert re.match(regex, "git push origin")
        assert re.match(regex, "git pull remote")
        assert not re.match(regex, "git commit message")

    def test_alternation_with_special_chars(self) -> None:
        regex = tokens_to_regex(["git", "push|pull"])
        assert re.match(regex, "git push")
        assert re.match(regex, "git pull")

    def test_plain_tokens_escaped(self) -> None:
        regex = tokens_to_regex(["git", "stash"])
        assert re.match(regex, "git stash pop")
        assert not re.match(regex, "git  commit")


class TestBlockCommandChainedCommands:
    def test_blocks_guarded_command_not_in_primary(self, app: HookApp, tmp_path: Path) -> None:
        block_command(r"git\s+push", reason="No pushing")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git push && echo done"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.block for r in results)

    def test_blocks_guarded_command_in_semicolon_chain(self, app: HookApp, tmp_path: Path) -> None:
        block_command(r"git\s+stash", reason="No stashing")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git stash; git push"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.block for r in results)

    def test_blocks_guarded_command_in_pipe_chain(self, app: HookApp, tmp_path: Path) -> None:
        block_command(r"rm\s+-rf", reason="No rm -rf")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "rm -rf /tmp/dir | tee log.txt"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.block for r in results)

    def test_non_matching_chain_passes(self, app: HookApp, tmp_path: Path) -> None:
        block_command(r"git\s+push", reason="No pushing")
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "echo done && git commit"}, ctx)
        matching = app.get_matching_hooks(evt)
        assert not matching


class TestWarnCommandChainedCommands:
    def test_warns_guarded_command_not_in_primary(self, app: HookApp, tmp_path: Path) -> None:
        warn_command(r"git\s+push", message="Be careful", events=Event.PreToolUse)
        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event("Bash", {"command": "git push && echo done"}, ctx)
        results = [execute_hook(entry, evt, tmp_path) for entry in app.get_matching_hooks(evt)]
        assert any(r and r.action == Action.warn for r in results)


class TestTestsDictPropagation:
    def test_block_command_propagates_tests(self, app: HookApp) -> None:
        tests = {"input": "test", "expected": "block"}
        block_command(r"git\s+stash", reason="No stashing", tests=tests)
        assert app.hooks[-1].spec.tests is tests

    def test_warn_command_propagates_tests(self, app: HookApp) -> None:
        tests = {"input": "test", "expected": "warn"}
        warn_command(r"git\s+push", message="Warning", tests=tests)
        assert app.hooks[-1].spec.tests is tests

    def test_block_command_tests_none_by_default(self, app: HookApp) -> None:
        block_command(r"git\s+stash", reason="No stashing")
        assert app.hooks[-1].spec.tests is None

    def test_warn_command_tests_none_by_default(self, app: HookApp) -> None:
        warn_command(r"git\s+push", message="Warning")
        assert app.hooks[-1].spec.tests is None
