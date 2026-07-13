from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from captain_hook.app import (
    hook as register_hook,
)
from captain_hook.app import (
    on,
)
from captain_hook.dispatch import ADVISORY_SEPARATOR, dispatch, execute_hook, format_output, run_declarative
from captain_hook.events import PermissionRequestEvent
from captain_hook.types import Action, Event, HookResult, HookSpec, RegisteredHook
from tests.helpers import (
    make_ctx,
    make_post_tool_event,
    make_pre_tool_event,
    make_stop_event,
    make_subagent_stop_event,
)


def make_permission_request_event() -> PermissionRequestEvent:
    return PermissionRequestEvent(_raw={"tool_name": "Bash", "tool_input": {"command": "ls"}}, ctx=make_ctx())


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

    def test_pre_tool_use_rewrite_emits_updated_input(self) -> None:
        result = HookResult(
            action=Action.rewrite,
            updated_input={"command": "ccx read x --full"},
            note="ran ccx",
        )
        output = format_output(Event.PreToolUse, result)
        assert output is not None
        hso = output["hookSpecificOutput"]
        assert hso["hookEventName"] == "PreToolUse"
        assert hso["permissionDecision"] == "allow"
        assert hso["updatedInput"] == {"command": "ccx read x --full"}
        assert hso["additionalContext"] == "ran ccx"

    def test_pre_tool_use_rewrite_without_note_omits_additional_context(self) -> None:
        result = HookResult(action=Action.rewrite, updated_input={"command": "ccx find **"})
        output = format_output(Event.PreToolUse, result)
        assert output is not None
        hso = output["hookSpecificOutput"]
        assert hso["updatedInput"] == {"command": "ccx find **"}
        assert "additionalContext" not in hso

    def test_post_tool_use_warn_no_permission_decision(self) -> None:
        result = HookResult(action=Action.warn, message="info")
        output = format_output(Event.PostToolUse, result)
        assert output is not None
        hso = output["hookSpecificOutput"]
        assert hso["additionalContext"] == "info"
        assert "permissionDecision" not in hso

    @pytest.mark.parametrize(
        ("event", "message"),
        [
            pytest.param(Event.Stop, "cannot stop", id="stop_block"),
            pytest.param(Event.SubagentStop, "stay", id="subagent_stop_block"),
        ],
    )
    def test_block_uses_decision_format(self, event: Event, message: str) -> None:
        output = format_output(event, HookResult(action=Action.block, message=message))
        assert output == {"decision": "block", "reason": message}

    @pytest.mark.parametrize(
        "event",
        [
            pytest.param(Event.Stop, id="stop_allow_returns_none"),
            pytest.param(Event.SubagentStop, id="subagent_stop_allow_returns_none"),
        ],
    )
    def test_allow_returns_none(self, event: Event) -> None:
        output = format_output(event, HookResult(action=Action.allow))
        assert output is None

    def test_stop_warn_returns_block_format(self) -> None:
        result = HookResult(action=Action.warn, message="warning stop")
        output = format_output(Event.Stop, result)
        assert output is None or output == {"decision": "block", "reason": "warning stop"}

    def test_session_start_warn_uses_additional_context(self) -> None:
        result = HookResult(action=Action.warn, message="resources provisioned")
        output = format_output(Event.SessionStart, result)
        assert output is not None
        assert "decision" not in output
        hso = output["hookSpecificOutput"]
        assert hso["hookEventName"] == "SessionStart"
        assert hso["additionalContext"] == "resources provisioned"
        assert "permissionDecision" not in hso

    def test_session_end_warn_uses_additional_context(self) -> None:
        result = HookResult(action=Action.warn, message="session over")
        output = format_output(Event.SessionEnd, result)
        assert output is not None
        assert "decision" not in output
        hso = output["hookSpecificOutput"]
        assert hso["hookEventName"] == "SessionEnd"
        assert hso["additionalContext"] == "session over"
        assert "permissionDecision" not in hso

    def test_permission_request_allow_full_envelope(self) -> None:
        output = format_output(Event.PermissionRequest, HookResult(action=Action.allow))
        assert output == {
            "hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}
        }

    def test_permission_request_block_puts_message_inside_decision(self) -> None:
        output = format_output(Event.PermissionRequest, HookResult(action=Action.block, message="teammate denied"))
        assert output == {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "deny", "message": "teammate denied"},
            }
        }

    def test_permission_request_block_without_message_omits_message_key(self) -> None:
        output = format_output(Event.PermissionRequest, HookResult(action=Action.block))
        assert output == {
            "hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "deny"}}
        }

    def test_permission_request_rewrite_emits_updated_input_and_drops_note(self) -> None:
        result = HookResult(action=Action.rewrite, updated_input={"command": "ccx read x --full"}, note="ran ccx")
        output = format_output(Event.PermissionRequest, result)
        assert output == {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow", "updatedInput": {"command": "ccx read x --full"}},
            }
        }

    def test_permission_request_warn_returns_none_so_dialog_shows(self) -> None:
        assert format_output(Event.PermissionRequest, HookResult(action=Action.warn, message="careful")) is None


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
        assert (tmp_path / entry.state_key).is_dir()

    def test_same_name_different_source_no_shared_counter(self, tmp_path: Path) -> None:
        # Two packs each register an @on handler named "check"; only source_file differs. A
        # bare-name state key would let pack A's single fire suppress pack B's; the source-file
        # namespacing gives each its own max_fires counter.
        def make(source_file: str) -> RegisteredHook:
            def check(evt: Any) -> HookResult:
                return HookResult(action=Action.warn, message="fired")

            return RegisteredHook(
                spec=HookSpec(events=Event.PreToolUse, max_fires=1),
                handler=check,
                name="check",
                source_file=source_file,
            )

        a, b = make("/packs/alpha/check.py"), make("/packs/beta/check.py")
        assert a.state_key != b.state_key

        assert execute_hook(a, make_pre_tool_event(), tmp_path) is not None  # a fires (1/1)
        assert execute_hook(a, make_pre_tool_event(), tmp_path) is None  # a exhausted
        assert execute_hook(b, make_pre_tool_event(), tmp_path) is not None  # b keeps its own slot

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

    def test_handler_rewrite(self) -> None:

        @on(Event.PreToolUse)
        def rewriter(evt: Any) -> HookResult:
            return HookResult(action=Action.rewrite, updated_input={"command": "ccx read x --full"}, note="n")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert hso["updatedInput"] == {"command": "ccx read x --full"}
        assert hso["additionalContext"] == "n"

    def test_approval_beats_later_warn(self) -> None:
        # Deny-wins scans every matching hook (no short-circuit on an approval), but an
        # approval still wins over a warn: the later warn runs yet never surfaces.
        counter = 0

        @on(Event.PreToolUse)
        def rewriter(evt: Any) -> HookResult:
            return HookResult(action=Action.rewrite, updated_input={"command": "ccx find **"})

        @on(Event.PreToolUse)
        def counter_handler(evt: Any) -> HookResult:
            nonlocal counter
            counter += 1
            return HookResult(action=Action.warn, message="counted")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert result["hookSpecificOutput"]["updatedInput"] == {"command": "ccx find **"}
        assert "additionalContext" not in result["hookSpecificOutput"]
        assert counter == 1

    def test_warn_then_block_denies_with_both(self) -> None:
        # A (declarative) warn that fired before a block rides along on the deny, behind an advisory
        # separator — block text first, then the warns.
        register_hook(Event.PreToolUse, message="warning first")

        @on(Event.PreToolUse)
        def blocker(evt: Any) -> HookResult:
            return HookResult(action=Action.block, message="blocked")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == f"blocked\n\n{ADVISORY_SEPARATOR}\n\nwarning first"

    def test_warns_combined_with_newline(self) -> None:
        register_hook(Event.PreToolUse, message="warn1")
        register_hook(Event.PreToolUse, message="warn2")
        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        context = result["hookSpecificOutput"]["additionalContext"]
        assert "warn1" in context
        assert "warn2" in context
        assert "\n\n" in context

    def test_block_wins_over_earlier_allow(self) -> None:
        # Deny-wins: a block beats an allow that ran before it (CC's deny > allow), so an
        # earlier approval — e.g. the fixes pack's teammate-bash allow — can never suppress
        # a later block such as the general pack's `jj undo` guard.
        call_count = 0

        @on(Event.PreToolUse)
        def allower(evt: Any) -> HookResult:
            return HookResult(action=Action.allow)

        @on(Event.PreToolUse)
        def blocker(evt: Any) -> HookResult:
            nonlocal call_count
            call_count += 1
            return HookResult(action=Action.block, message="denied")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == "denied"
        assert call_count == 1

    def test_handler_backed_hook_skipped_after_block(self) -> None:
        # Once a block fires, a later handler-backed hook (an LLM nudge, an async handler) is not
        # invoked — it would burn API cost and max_fires on a doomed call.
        counter = 0

        register_hook(Event.PreToolUse, message="stop here", block=True)

        @on(Event.PreToolUse)
        def counter_handler(evt: Any) -> HookResult:
            nonlocal counter
            counter += 1
            return HookResult(action=Action.warn, message="counted")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == "stop here"
        assert counter == 0

    def test_declarative_warn_after_block_rides_along(self) -> None:
        # A message-only declarative warn still runs after a block and lands on the deny.
        register_hook(Event.PreToolUse, message="stop here", block=True)
        register_hook(Event.PreToolUse, message="advisory note")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == f"stop here\n\n{ADVISORY_SEPARATOR}\n\nadvisory note"

    def test_handler_crash_returns_none(self) -> None:

        @on(Event.PreToolUse)
        def crasher(evt: Any) -> HookResult:
            raise RuntimeError("kaboom")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is None

    def test_async_flag_filters_hooks(self) -> None:
        # PostToolUse (not a decision event) so async_=True is a legal registration; the
        # async-flag filtering under test is independent of the event.
        register_hook(Event.PostToolUse, message="sync warning", async_=False)
        register_hook(Event.PostToolUse, message="async warning", async_=True)

        sync_result = dispatch(Event.PostToolUse, make_post_tool_event(), async_=False)
        assert sync_result is not None
        assert sync_result["hookSpecificOutput"]["additionalContext"] == "sync warning"

        async_result = dispatch(Event.PostToolUse, make_post_tool_event(), async_=True)
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

    def test_permission_request_dispatch_emits_decision_envelope(self) -> None:

        @on(Event.PermissionRequest)
        def approver(evt: Any) -> HookResult:
            return HookResult(action=Action.allow)

        result = dispatch(Event.PermissionRequest, make_permission_request_event())
        assert result == {
            "hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}
        }

    def test_subagent_stop_block(self) -> None:
        register_hook(Event.SubagentStop, message="stay", block=True)
        result = dispatch(Event.SubagentStop, make_subagent_stop_event())
        assert result == {"decision": "block", "reason": "stay"}

    def test_empty_stdin_handling(self) -> None:
        register_hook(Event.PreToolUse, message="test")
        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
