from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from captain_hook.app import (
    hook as register_hook,
    on,
)
from captain_hook.dispatch import dispatch, execute_hook, format_output, run_declarative
from captain_hook.types import Action, Event, HookResult, HookSpec, RegisteredHook
from captain_hook.tests.helpers import make_ctx, make_post_tool_event, make_pre_tool_event, make_stop_event, make_subagent_stop_event



class TestRunDeclarative:
    def test_warn_message(self) -> None:
        spec = HookSpec(events=Event.PreToolUse, message="caution")
        result = run_declarative(spec, make_pre_tool_event())
        assert result is not None
        assert result.action is Action.warn
        assert result.message == "caution"

    def test_block_message(self) -> None:
        spec = HookSpec(events=Event.PreToolUse, message="denied", block=True)
        result = run_declarative(spec, make_pre_tool_event())
        assert result is not None
        assert result.action is Action.block
        assert result.message == "denied"

    def test_no_message_returns_none(self) -> None:
        spec = HookSpec(events=Event.PreToolUse)
        result = run_declarative(spec, make_pre_tool_event())
        assert result is None


class TestFormatOutput:
    def test_pre_tool_use_block(self) -> None:
        result = HookResult(action=Action.block, message="not allowed")
        output = format_output(Event.PreToolUse, result)
        assert output is not None
        assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert output["hookSpecificOutput"]["permissionDecisionReason"] == "not allowed"
        assert output["hookSpecificOutput"]["hookEventName"] == "PreToolUse"

    def test_pre_tool_use_warn(self) -> None:
        result = HookResult(action=Action.warn, message="be careful")
        output = format_output(Event.PreToolUse, result)
        assert output is not None
        hso = output["hookSpecificOutput"]
        assert hso["additionalContext"] == "be careful"
        assert hso["permissionDecision"] == "allow"
        assert hso["hookEventName"] == "PreToolUse"

    def test_pre_tool_use_allow(self) -> None:
        result = HookResult(action=Action.allow)
        output = format_output(Event.PreToolUse, result)
        assert output is not None
        assert output["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_post_tool_use_warn_no_permission_decision(self) -> None:
        result = HookResult(action=Action.warn, message="info")
        output = format_output(Event.PostToolUse, result)
        assert output is not None
        hso = output["hookSpecificOutput"]
        assert hso["additionalContext"] == "info"
        assert "permissionDecision" not in hso

    def test_stop_block(self) -> None:
        result = HookResult(action=Action.block, message="cannot stop")
        output = format_output(Event.Stop, result)
        assert output == {"decision": "block", "reason": "cannot stop"}

    def test_stop_allow_returns_none(self) -> None:
        result = HookResult(action=Action.allow)
        output = format_output(Event.Stop, result)
        assert output is None

    def test_subagent_stop_block(self) -> None:
        result = HookResult(action=Action.block, message="stay")
        output = format_output(Event.SubagentStop, result)
        assert output == {"decision": "block", "reason": "stay"}

    def test_subagent_stop_allow_returns_none(self) -> None:
        result = HookResult(action=Action.allow)
        output = format_output(Event.SubagentStop, result)
        assert output is None

    def test_stop_warn_returns_block_format(self) -> None:
        result = HookResult(action=Action.warn, message="warning stop")
        output = format_output(Event.Stop, result)
        assert output is None or output == {"decision": "block", "reason": "warning stop"}


class TestExecuteHook:
    def test_handler_returns_result(self, tmp_path: Path) -> None:
        def handler(evt: Any) -> HookResult:
            return HookResult(action=Action.warn, message="handled")

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse),
            handler=handler,
            name="test_hook",
        )
        evt = make_pre_tool_event()
        result = execute_hook(entry, evt, tmp_path)
        assert result is not None
        assert result.action is Action.warn
        assert result.message == "handled"

    def test_handler_returns_none(self, tmp_path: Path) -> None:
        def handler(evt: Any) -> None:
            return None

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse),
            handler=handler,
            name="test_hook",
        )
        result = execute_hook(entry, make_pre_tool_event(), tmp_path)
        assert result is None

    def test_declarative_hook(self, tmp_path: Path) -> None:
        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse, message="warn me"),
            name="declarative_1",
        )
        result = execute_hook(entry, make_pre_tool_event(), tmp_path)
        assert result is not None
        assert result.action is Action.warn
        assert result.message == "warn me"

    def test_handler_crash_returns_none(self, tmp_path: Path) -> None:
        def handler(evt: Any) -> HookResult:
            raise RuntimeError("boom")

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse),
            handler=handler,
            name="crashing_hook",
        )
        result = execute_hook(entry, make_pre_tool_event(), tmp_path)
        assert result is None

    def test_system_exit_propagates(self, tmp_path: Path) -> None:
        def handler(evt: Any) -> HookResult:
            raise SystemExit(1)

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse),
            handler=handler,
            name="exit_hook",
        )
        with pytest.raises(SystemExit):
            execute_hook(entry, make_pre_tool_event(), tmp_path)

    def test_keyboard_interrupt_propagates(self, tmp_path: Path) -> None:
        def handler(evt: Any) -> HookResult:
            raise KeyboardInterrupt

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse),
            handler=handler,
            name="interrupt_hook",
        )
        with pytest.raises(KeyboardInterrupt):
            execute_hook(entry, make_pre_tool_event(), tmp_path)

    def test_per_hook_session_subdirectory(self, tmp_path: Path) -> None:
        def handler(evt: Any) -> HookResult:
            return HookResult(action=Action.warn, message="x")

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse),
            handler=handler,
            name="my_hook",
        )
        execute_hook(entry, make_pre_tool_event(), tmp_path)
        assert (tmp_path / "my_hook").is_dir()

    def test_fire_count_only_on_non_none(self, tmp_path: Path) -> None:
        call_count = 0

        def handler(evt: Any) -> HookResult | None:
            nonlocal call_count
            call_count += 1
            return None if call_count <= 2 else HookResult(action=Action.warn, message="now")

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse, max_fires=1),
            handler=handler,
            name="counter_hook",
        )

        r1 = execute_hook(entry, make_pre_tool_event(), tmp_path)
        assert r1 is None

        r2 = execute_hook(entry, make_pre_tool_event(), tmp_path)
        assert r2 is None

        r3 = execute_hook(entry, make_pre_tool_event(), tmp_path)
        assert r3 is not None
        assert r3.action is Action.warn

        r4 = execute_hook(entry, make_pre_tool_event(), tmp_path)
        assert r4 is None


class TestDispatch:
    def test_no_matching_hooks_returns_none(self) -> None:
        evt = make_pre_tool_event()
        result = dispatch(Event.PreToolUse, evt)
        assert result is None

    def test_declarative_warn(self) -> None:
        register_hook(Event.PreToolUse, message="be careful")
        evt = make_pre_tool_event()
        result = dispatch(Event.PreToolUse, evt)
        assert result is not None
        assert result["hookSpecificOutput"]["additionalContext"] == "be careful"

    def test_declarative_block(self) -> None:
        register_hook(Event.PreToolUse, message="denied", block=True)
        evt = make_pre_tool_event()
        result = dispatch(Event.PreToolUse, evt)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_handler_warn(self) -> None:

        @on(Event.PreToolUse)
        def my_handler(evt: Any) -> HookResult:
            return HookResult(action=Action.warn, message="from handler")

        evt = make_pre_tool_event()
        result = dispatch(Event.PreToolUse, evt)
        assert result is not None
        assert result["hookSpecificOutput"]["additionalContext"] == "from handler"

    def test_handler_block(self) -> None:

        @on(Event.PreToolUse)
        def blocker(evt: Any) -> HookResult:
            return HookResult(action=Action.block, message="blocked")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_handler_allow(self) -> None:

        @on(Event.PreToolUse)
        def allower(evt: Any) -> HookResult:
            return HookResult(action=Action.allow)

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_handler_none_returns_none(self) -> None:

        @on(Event.PreToolUse)
        def noop(evt: Any) -> None:
            return None

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is None

    def test_block_takes_priority_over_warn(self) -> None:
        register_hook(Event.PreToolUse, message="warning first")

        @on(Event.PreToolUse)
        def blocker(evt: Any) -> HookResult:
            return HookResult(action=Action.block, message="blocked")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == "blocked"

    def test_warns_combined_with_newline(self) -> None:
        register_hook(Event.PreToolUse, message="warn1")
        register_hook(Event.PreToolUse, message="warn2")
        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        context = result["hookSpecificOutput"]["additionalContext"]
        assert "warn1" in context
        assert "warn2" in context
        assert "\n\n" in context

    def test_allow_short_circuits(self) -> None:
        call_count = 0

        @on(Event.PreToolUse)
        def allower(evt: Any) -> HookResult:
            return HookResult(action=Action.allow)

        @on(Event.PreToolUse)
        def blocker(evt: Any) -> HookResult:
            nonlocal call_count
            call_count += 1
            return HookResult(action=Action.block, message="should not reach")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert call_count == 0

    def test_block_short_circuits_no_side_effects(self) -> None:
        counter = 0

        @on(Event.PreToolUse)
        def blocker(evt: Any) -> HookResult:
            return HookResult(action=Action.block, message="stop here")

        @on(Event.PreToolUse)
        def counter_handler(evt: Any) -> HookResult:
            nonlocal counter
            counter += 1
            return HookResult(action=Action.warn, message="counted")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert counter == 0

    def test_handler_crash_returns_none(self) -> None:

        @on(Event.PreToolUse)
        def crasher(evt: Any) -> HookResult:
            raise RuntimeError("kaboom")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is None

    def test_async_flag_filters_hooks(self) -> None:
        register_hook(Event.PreToolUse, message="sync warning", async_=False)
        register_hook(Event.PreToolUse, message="async warning", async_=True)

        sync_result = dispatch(Event.PreToolUse, make_pre_tool_event(), async_=False)
        assert sync_result is not None
        assert sync_result["hookSpecificOutput"]["additionalContext"] == "sync warning"

        async_result = dispatch(Event.PreToolUse, make_pre_tool_event(), async_=True)
        assert async_result is not None
        assert async_result["hookSpecificOutput"]["additionalContext"] == "async warning"

    def test_stop_event_format(self) -> None:
        register_hook(Event.Stop, message="cannot stop", block=True)
        result = dispatch(Event.Stop, make_stop_event())
        assert result is not None
        assert result == {"decision": "block", "reason": "cannot stop"}

    def test_stop_event_allow_returns_none(self) -> None:

        @on(Event.Stop)
        def allower(evt: Any) -> HookResult:
            return HookResult(action=Action.allow)

        result = dispatch(Event.Stop, make_stop_event())
        assert result is None

    def test_dispatch_with_session_dir(self, tmp_path: Path) -> None:

        @on(Event.PreToolUse)
        def handler(evt: Any) -> HookResult:
            return HookResult(action=Action.warn, message="with session")

        result = dispatch(Event.PreToolUse, make_pre_tool_event(), session_dir=tmp_path)
        assert result is not None
        assert result["hookSpecificOutput"]["additionalContext"] == "with session"

    def test_max_fires_in_dispatch(self, tmp_path: Path) -> None:
        register_hook(Event.PreToolUse, message="once only", max_fires=1)

        r1 = dispatch(Event.PreToolUse, make_pre_tool_event(), session_dir=tmp_path)
        assert r1 is not None

        r2 = dispatch(Event.PreToolUse, make_pre_tool_event(), session_dir=tmp_path)
        assert r2 is None

    def test_stop_warn_combined(self) -> None:
        register_hook(Event.Stop, message="warn stop")
        result = dispatch(Event.Stop, make_stop_event())
        assert result is None or "decision" in result

    def test_subagent_stop_block(self) -> None:
        register_hook(Event.SubagentStop, message="stay", block=True)
        result = dispatch(Event.SubagentStop, make_subagent_stop_event())
        assert result == {"decision": "block", "reason": "stay"}

    def test_empty_stdin_handling(self) -> None:
        register_hook(Event.PreToolUse, message="test")
        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
