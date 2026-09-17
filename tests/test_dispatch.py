from __future__ import annotations

import threading
import time
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from captain_hook import dispatch as dispatch_module
from captain_hook.app import (
    HookHandler,
    on,
)
from captain_hook.app import (
    hook as register_hook,
)
from captain_hook.dispatch import (
    ADVISORY_SEPARATOR,
    ASYNC_HOOK_TIMEOUT_SECONDS,
    SYNC_DEADLINE_MARGIN_SECONDS,
    FirstBlock,
    dispatch,
    dispatch_async,
    doomed_by_block,
    execute_hook,
    format_output,
    run_declarative,
)
from captain_hook.events import MessageDisplayEvent, PermissionRequestEvent
from captain_hook.primitives.nudge import nudge
from captain_hook.session import SessionStore
from captain_hook.types import Action, CustomCondition, Event, HookResult, HookSpec, RegisteredHook
from captain_hook.util import reqenv
from tests.helpers import (
    make_ctx,
    make_post_tool_event,
    make_pre_tool_event,
    make_stop_event,
    make_subagent_stop_event,
)

HOOK_SLEEP_SECONDS = 0.3


def make_permission_request_event() -> PermissionRequestEvent:
    return PermissionRequestEvent(_raw={"tool_name": "Bash", "tool_input": {"command": "ls"}}, ctx=make_ctx())


def make_message_display_event() -> MessageDisplayEvent:
    return MessageDisplayEvent(
        _raw={"message_id": "msg_1", "index": 0, "final": False, "delta": "Refactored the parser."}, ctx=make_ctx()
    )


def distinct_hook(name: str, body: Callable[[str], HookResult | None]) -> HookHandler:
    """A handler carrying its own ``__name__``, so each registration lands in its own state-key group."""

    def handler(evt: Any) -> HookResult | None:
        return body(name)

    handler.__name__ = name
    return handler


@contextmanager
def pinch_pool(monkeypatch: pytest.MonkeyPatch, width: int) -> Generator[None]:
    """Cap each event's fan-out at ``width`` threads, so queueing order is deterministic."""
    with monkeypatch.context() as patch:
        patch.setattr(dispatch_module, "HOOK_FANOUT_THREADS", width)
        yield


def fanout_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name.startswith("capt-hook-hook")]


FROZEN_NOW = 1_000.0


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold the deadline clock still, so a hook is abandoned after its collection wait and never skipped at its start.

    Against the live clock a hook whose thread starts late on a loaded runner finds the deadline
    already inside the margin and is skipped, and the test never sees the straggler it set up.
    """
    monkeypatch.setattr(reqenv, "time", SimpleNamespace(time=lambda: FROZEN_NOW))


def bounded_request(seconds: float) -> reqenv.RequestOverrides:
    """A request under :func:`frozen_clock` whose hooks' verdicts are each waited on for *seconds*."""
    deadline_ms = int((FROZEN_NOW + SYNC_DEADLINE_MARGIN_SECONDS + seconds) * 1000)
    return reqenv.RequestOverrides(env={}, cwd="/w", client_ppid=1, session_id="s", deadline_unix_ms=deadline_ms)


class TestPools:
    @pytest.mark.parametrize("getter", ["background_pool", "offload_pool"])
    def test_concurrent_first_calls_build_one_executor(self, monkeypatch: pytest.MonkeyPatch, getter: str) -> None:
        pool_getter = getattr(dispatch_module, getter)
        built: list[ThreadPoolExecutor] = []

        def slow_executor(**kwargs: Any) -> ThreadPoolExecutor:
            time.sleep(0.05)
            built.append(executor := ThreadPoolExecutor(**kwargs))
            return executor

        monkeypatch.setattr(dispatch_module, "ThreadPoolExecutor", slow_executor)
        pool_getter.cache_clear()
        barrier = threading.Barrier(8)
        seen: list[ThreadPoolExecutor] = []

        def first_call() -> None:
            barrier.wait()
            seen.append(pool_getter())

        callers = [threading.Thread(target=first_call) for _ in range(8)]
        try:
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join()
            assert len(built) == 1
            assert len(seen) == 8
            assert all(pool is built[0] for pool in seen)
        finally:
            pool_getter.cache_clear()
            for executor in built:
                executor.shutdown(wait=True)


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

    def test_pre_tool_use_context_omits_permission_decision(self) -> None:
        # A context result (warn with approve=False) surfaces its message as pure
        # additionalContext and drops the PreToolUse allow rider a plain warn carries.
        result = HookResult(action=Action.warn, message="just context", approve=False)
        output = format_output(Event.PreToolUse, result)
        assert output is not None
        hso = output["hookSpecificOutput"]
        assert hso["additionalContext"] == "just context"
        assert "permissionDecision" not in hso

    def test_post_tool_use_context_unchanged(self) -> None:
        # Non-PreToolUse warns never carried the rider; approve=False leaves them identical.
        result = HookResult(action=Action.warn, message="ctx", approve=False)
        output = format_output(Event.PostToolUse, result)
        assert output is not None
        hso = output["hookSpecificOutput"]
        assert hso["additionalContext"] == "ctx"
        assert "permissionDecision" not in hso

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

    @pytest.mark.parametrize(
        "message",
        [pytest.param("Fixed the parser.", id="replacement"), pytest.param("", id="blanked-chunk")],
    )
    def test_message_display_rewrite_emits_display_content(self, message: str) -> None:
        output = format_output(Event.MessageDisplay, HookResult(action=Action.rewrite, message=message))
        assert output == {"hookSpecificOutput": {"hookEventName": "MessageDisplay", "displayContent": message}}

    @pytest.mark.parametrize(
        "result",
        [
            pytest.param(HookResult(action=Action.allow), id="allow"),
            pytest.param(HookResult(action=Action.warn, message="careful"), id="warn"),
            pytest.param(HookResult(action=Action.block, message="no"), id="block"),
        ],
    )
    def test_message_display_non_rewrite_leaves_the_chunk_displayed(self, result: HookResult) -> None:
        assert format_output(Event.MessageDisplay, result) is None


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

    def test_rewrite_beats_earlier_plain_allow(self) -> None:
        # A broad approve firing first must not drop a later hook's corrected input:
        # composition precedence is block > rewrite > allow.

        @on(Event.PreToolUse)
        def allower(evt: Any) -> HookResult:
            return HookResult(action=Action.allow)

        @on(Event.PreToolUse)
        def rewriter(evt: Any) -> HookResult:
            return HookResult(action=Action.rewrite, updated_input={"command": "echo sanitized"})

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert hso["updatedInput"] == {"command": "echo sanitized"}

    def test_block_beats_rewrite(self) -> None:

        @on(Event.PreToolUse)
        def rewriter(evt: Any) -> HookResult:
            return HookResult(action=Action.rewrite, updated_input={"command": "echo sanitized"})

        @on(Event.PreToolUse)
        def blocker(evt: Any) -> HookResult:
            return HookResult(action=Action.block, message="blocked")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "updatedInput" not in result["hookSpecificOutput"]

    def test_first_rewrite_wins_among_rewrites(self) -> None:

        @on(Event.PreToolUse)
        def first(evt: Any) -> HookResult:
            return HookResult(action=Action.rewrite, updated_input={"command": "first"})

        @on(Event.PreToolUse)
        def second(evt: Any) -> HookResult:
            return HookResult(action=Action.rewrite, updated_input={"command": "second"})

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert result["hookSpecificOutput"]["updatedInput"] == {"command": "first"}

    def test_warn_rides_along_on_winning_rewrite(self) -> None:
        # Deny-wins scans every matching hook (no short-circuit on an approval), and a
        # warn is never lost to the winner: it surfaces as the rewrite's advisory context.
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
        assert result["hookSpecificOutput"]["additionalContext"] == "counted"
        assert counter == 1

    def test_warn_joins_rewrite_note_in_advisory_context(self) -> None:

        @on(Event.PreToolUse)
        def rewriter(evt: Any) -> HookResult:
            return HookResult(action=Action.rewrite, updated_input={"command": "ccx find **"}, note="rewrote")

        @on(Event.PreToolUse)
        def warner(evt: Any) -> HookResult:
            return HookResult(action=Action.warn, message="counted")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert result["hookSpecificOutput"]["additionalContext"] == "rewrote\n\ncounted"

    def test_warn_rides_along_on_winning_allow(self) -> None:

        @on(Event.PreToolUse)
        def allower(evt: Any) -> HookResult:
            return HookResult(action=Action.allow)

        @on(Event.PreToolUse)
        def warner(evt: Any) -> HookResult:
            return HookResult(action=Action.warn, message="advisory")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert hso["additionalContext"] == "advisory"

    def test_warn_then_block_denies_with_both(self) -> None:
        # A (declarative) warn that fired before a block rides along on the deny, behind an advisory
        # separator — block text first, then the warns.
        register_hook(Event.PreToolUse, message="warning first", advisory_on_deny=True)

        @on(Event.PreToolUse)
        def blocker(evt: Any) -> HookResult:
            return HookResult(action=Action.block, message="blocked")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert (
            result["hookSpecificOutput"]["permissionDecisionReason"]
            == f"blocked\n\n{ADVISORY_SEPARATOR}\n\nwarning first"
        )

    def test_default_nudge_does_not_ride_along_on_block(self) -> None:
        nudge("command will still run", events=Event.PreToolUse, max_fires=None)
        register_hook(Event.PreToolUse, message="blocked", block=True)

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "command will still run" not in reason

    def test_opted_in_nudge_rides_along_on_block(self) -> None:
        nudge(
            "retry with safer arguments",
            events=Event.PreToolUse,
            max_fires=None,
            advisory_on_deny=True,
        )
        register_hook(Event.PreToolUse, message="blocked", block=True)

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert (
            result["hookSpecificOutput"]["permissionDecisionReason"]
            == f"blocked\n\n{ADVISORY_SEPARATOR}\n\nretry with safer arguments"
        )

    def test_opted_in_nudge_after_block_rides_along(self) -> None:
        register_hook(Event.PreToolUse, message="blocked", block=True)
        nudge(
            "retry with safer arguments",
            events=Event.PreToolUse,
            max_fires=None,
            advisory_on_deny=True,
        )

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert (
            result["hookSpecificOutput"]["permissionDecisionReason"]
            == f"blocked\n\n{ADVISORY_SEPARATOR}\n\nretry with safer arguments"
        )

    def test_message_less_block_keeps_advisory_out_of_deny_reason(self) -> None:
        nudge(
            "retry with safer arguments",
            events=Event.PreToolUse,
            max_fires=None,
            advisory_on_deny=True,
        )

        @on(Event.PreToolUse)
        def blocker(evt: Any) -> HookResult:
            return HookResult(action=Action.block)

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert (
            result["hookSpecificOutput"]["permissionDecisionReason"]
            == f"{ADVISORY_SEPARATOR}\n\nretry with safer arguments"
        )

    def test_warns_combined_with_newline(self) -> None:
        register_hook(Event.PreToolUse, message="warn1")
        register_hook(Event.PreToolUse, message="warn2")
        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        context = result["hookSpecificOutput"]["additionalContext"]
        assert "warn1" in context
        assert "warn2" in context
        assert "\n\n" in context

    def test_context_only_drops_pretooluse_rider(self) -> None:
        # A lone evt.context result surfaces as advisory context without pre-approving the tool.
        @on(Event.PreToolUse)
        def contexter(evt: Any) -> HookResult:
            return evt.context("advice one")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["additionalContext"] == "advice one"
        assert "permissionDecision" not in hso

    def test_two_context_only_stay_rider_free(self) -> None:
        # The context-only merge path rebuilds the warn; approve stays False so no rider regains.
        @on(Event.PreToolUse)
        def c1(evt: Any) -> HookResult:
            return evt.context("a")

        @on(Event.PreToolUse)
        def c2(evt: Any) -> HookResult:
            return evt.context("b")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["additionalContext"] == "a\n\nb"
        assert "permissionDecision" not in hso

    def test_warn_plus_context_keeps_rider(self) -> None:
        # any(contributing warns' approve): a plain warn in the merge keeps the allow rider.
        @on(Event.PreToolUse)
        def warner(evt: Any) -> HookResult:
            return evt.warn("plain warn")

        @on(Event.PreToolUse)
        def contexter(evt: Any) -> HookResult:
            return evt.context("extra context")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow"
        assert "plain warn" in hso["additionalContext"]
        assert "extra context" in hso["additionalContext"]

    def test_context_injects_text_verbatim_end_to_end(self) -> None:
        # Agent-inject shape: a context result's text reaches additionalContext verbatim.
        injected = "You are reviewing PR #42.\nFocus on the auth changes."

        @on(Event.PreToolUse)
        def inject(evt: Any) -> HookResult:
            return evt.context(injected)

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        hso = result["hookSpecificOutput"]
        assert hso["additionalContext"] == injected
        assert "permissionDecision" not in hso

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

    def test_handler_backed_hook_after_block_is_dropped(self) -> None:
        # A block earlier in registration order suppresses a later handler-backed hook: its verdict
        # never reaches the envelope, whether it was skipped before it started or folded away after.

        register_hook(Event.PreToolUse, message="stop here", block=True)

        @on(Event.PreToolUse)
        def second_blocker(evt: Any) -> HookResult:
            return HookResult(action=Action.block, message="also denied")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == "stop here"

    def test_handler_backed_hook_queued_behind_a_block_never_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A hook still queued when the block fires is not invoked at all — it would burn API cost
        # and max_fires on a doomed call.
        counter = 0

        register_hook(Event.PreToolUse, message="stop here", block=True)

        @on(Event.PreToolUse)
        def counter_handler(evt: Any) -> HookResult:
            nonlocal counter
            counter += 1
            return HookResult(action=Action.warn, message="counted")

        with pinch_pool(monkeypatch, 1):
            result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == "stop here"
        assert counter == 0

    def test_opted_in_declarative_warn_after_block_rides_along(self) -> None:
        # A message-only declarative warn still runs after a block and can opt into the deny.
        register_hook(Event.PreToolUse, message="stop here", block=True)
        register_hook(Event.PreToolUse, message="advisory note", advisory_on_deny=True)

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert (
            result["hookSpecificOutput"]["permissionDecisionReason"]
            == f"stop here\n\n{ADVISORY_SEPARATOR}\n\nadvisory note"
        )

    def test_handler_crash_returns_none(self) -> None:

        @on(Event.PreToolUse)
        def crasher(evt: Any) -> HookResult:
            raise RuntimeError("kaboom")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is None

    def test_dispatch_runs_sync_hooks_and_dispatch_async_runs_the_rest(self) -> None:
        ran: list[str] = []
        register_hook(Event.PostToolUse, message="sync warning")

        @on(Event.PostToolUse, async_=True)
        def background(evt: Any) -> HookResult:
            ran.append("async")
            return HookResult(action=Action.warn, message="async warning")

        evt = make_post_tool_event()
        sync_result = dispatch(Event.PostToolUse, evt)
        assert sync_result is not None
        assert sync_result["hookSpecificOutput"]["additionalContext"] == "sync warning"
        assert ran == []

        dispatch_async(evt)
        assert ran == ["async"]

    def test_each_pass_evaluates_only_its_own_hooks_conditions(self) -> None:
        checked: list[str] = []

        class Recorded(CustomCondition):
            def __init__(self, label: str) -> None:
                self.label = label

            def check(self, evt: Any) -> bool:
                checked.append(self.label)
                return True

        register_hook(Event.PostToolUse, message="sync warning", only_if=[Recorded("sync")])
        register_hook(Event.PostToolUse, message="async warning", only_if=[Recorded("async")], async_=True)

        evt = make_post_tool_event()
        dispatch(Event.PostToolUse, evt)
        assert checked == ["sync"]

        dispatch_async(evt)
        assert checked == ["sync", "async"]

    def test_each_async_hook_gets_its_own_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = {"now": 100.0}
        monkeypatch.setattr(reqenv, "time", SimpleNamespace(time=lambda: clock["now"]))
        deadlines: list[int] = []

        def observe(evt: Any) -> None:
            assert (current := reqenv.current()) is not None
            deadlines.append(current.deadline_unix_ms)
            clock["now"] += 60.0

        on(Event.PostToolUse, async_=True)(observe)
        on(Event.PostToolUse, async_=True)(observe)

        overrides = reqenv.RequestOverrides(env={}, cwd="/w", client_ppid=1, session_id="s", deadline_unix_ms=101_000)
        with pinch_pool(monkeypatch, 1), reqenv.use_request(overrides):
            dispatch_async(make_post_tool_event())

        assert deadlines == [
            int((100.0 + ASYNC_HOOK_TIMEOUT_SECONDS) * 1000),
            int((160.0 + ASYNC_HOOK_TIMEOUT_SECONDS) * 1000),
        ]

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

    def test_queued_hooks_stop_once_the_caller_deadline_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = {"now": 0.0}
        monkeypatch.setattr(reqenv, "time", SimpleNamespace(time=lambda: clock["now"]))
        ran: list[str] = []

        @on(Event.PreToolUse)
        def slow(evt: Any) -> HookResult:
            ran.append("slow")
            clock["now"] = 60.0
            return HookResult(action=Action.warn, message="from slow")

        @on(Event.PreToolUse)
        def late(evt: Any) -> HookResult:
            ran.append("late")
            return HookResult(action=Action.block, message="never reached")

        overrides = reqenv.RequestOverrides(env={}, cwd="/w", client_ppid=1, session_id="s", deadline_unix_ms=60_000)
        with pinch_pool(monkeypatch, 1), reqenv.use_request(overrides):
            result = dispatch(Event.PreToolUse, make_pre_tool_event())

        assert ran == ["slow"]
        assert result is not None
        assert result["hookSpecificOutput"]["additionalContext"] == "from slow"

    def test_queued_hooks_stop_inside_the_deadline_margin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = {"now": 0.0}
        monkeypatch.setattr(reqenv, "time", SimpleNamespace(time=lambda: clock["now"]))
        ran: list[str] = []

        @on(Event.PostToolUse)
        def first(evt: Any) -> HookResult:
            ran.append("first")
            clock["now"] = 30.0 - SYNC_DEADLINE_MARGIN_SECONDS + 1
            return HookResult(action=Action.warn, message="from first")

        @on(Event.PostToolUse)
        def second(evt: Any) -> HookResult:
            ran.append("second")
            return HookResult(action=Action.warn, message="from second")

        overrides = reqenv.RequestOverrides(env={}, cwd="/w", client_ppid=1, session_id="s", deadline_unix_ms=30_000)
        with pinch_pool(monkeypatch, 1), reqenv.use_request(overrides):
            result = dispatch(Event.PostToolUse, make_post_tool_event())

        assert ran == ["first"]
        assert result is not None
        assert result["hookSpecificOutput"]["additionalContext"].startswith("from first")

    def test_no_hook_starts_inside_the_deadline_margin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(reqenv, "time", SimpleNamespace(time=lambda: 0.0))
        ran: list[str] = []

        def recorder(name: str) -> HookHandler:
            def observer(evt: Any) -> HookResult:
                ran.append(name)
                return HookResult(action=Action.warn, message=name)

            return observer

        for name in ("first", "second", "third"):
            on(Event.PostToolUse)(recorder(name))

        margin_ms = int(SYNC_DEADLINE_MARGIN_SECONDS * 1000)
        overrides = reqenv.RequestOverrides(env={}, cwd="/w", client_ppid=1, session_id="s", deadline_unix_ms=margin_ms)
        with reqenv.use_request(overrides):
            result = dispatch(Event.PostToolUse, make_post_tool_event())

        assert ran == []
        assert result is None

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

    def test_message_display_dispatch_keeps_an_empty_rewrite(self) -> None:

        @on(Event.MessageDisplay)
        def blanker(evt: Any) -> HookResult:
            return HookResult.of(Action.rewrite, "")

        result = dispatch(Event.MessageDisplay, make_message_display_event())
        assert result == {"hookSpecificOutput": {"hookEventName": "MessageDisplay", "displayContent": ""}}

    def test_subagent_stop_block(self) -> None:
        register_hook(Event.SubagentStop, message="stay", block=True)
        result = dispatch(Event.SubagentStop, make_subagent_stop_event())
        assert result == {"decision": "block", "reason": "stay"}

    def test_empty_stdin_handling(self) -> None:
        register_hook(Event.PreToolUse, message="test")
        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None


class TestConcurrentDispatch:
    def test_hooks_of_one_event_run_concurrently(self) -> None:
        def sleep_then_warn(name: str) -> HookResult:
            time.sleep(HOOK_SLEEP_SECONDS)
            return HookResult(action=Action.warn, message=name)

        for name in ("a", "b", "c", "d", "e"):
            on(Event.PostToolUse)(distinct_hook(name, sleep_then_warn))

        start = time.perf_counter()
        result = dispatch(Event.PostToolUse, make_post_tool_event())
        elapsed = time.perf_counter() - start

        assert result is not None
        assert result["hookSpecificOutput"]["additionalContext"] == "a\n\nb\n\nc\n\nd\n\ne"
        assert elapsed < HOOK_SLEEP_SECONDS * 2.5, f"five {HOOK_SLEEP_SECONDS}s hooks took {elapsed:.2f}s"

    def test_messages_join_in_registration_order_not_completion_order(self) -> None:
        @on(Event.PostToolUse)
        def slowest(evt: Any) -> HookResult:
            time.sleep(HOOK_SLEEP_SECONDS)
            return HookResult(action=Action.warn, message="registered first")

        @on(Event.PostToolUse)
        def fastest(evt: Any) -> HookResult:
            return HookResult(action=Action.warn, message="registered second")

        result = dispatch(Event.PostToolUse, make_post_tool_event())
        assert result is not None
        assert result["hookSpecificOutput"]["additionalContext"] == "registered first\n\nregistered second"

    def test_block_wins_when_the_blocker_finishes_last(self) -> None:
        @on(Event.PreToolUse)
        def allower(evt: Any) -> HookResult:
            return HookResult(action=Action.allow)

        @on(Event.PreToolUse)
        def blocker(evt: Any) -> HookResult:
            time.sleep(HOOK_SLEEP_SECONDS)
            return HookResult(action=Action.block, message="denied late")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == "denied late"

    def test_advisory_warn_rides_along_on_a_block_that_finished_first(self) -> None:
        register_hook(Event.PreToolUse, message="stop here", block=True)

        @on(Event.PreToolUse, advisory_on_deny=True)
        def advisor(evt: Any) -> HookResult:
            time.sleep(HOOK_SLEEP_SECONDS)
            return HookResult(action=Action.warn, message="advisory note")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert (
            result["hookSpecificOutput"]["permissionDecisionReason"]
            == f"stop here\n\n{ADVISORY_SEPARATOR}\n\nadvisory note"
        )

    def test_a_straggler_is_abandoned_and_the_reply_still_lands(self) -> None:
        release = threading.Event()

        @on(Event.PostToolUse)
        def prompt(evt: Any) -> HookResult:
            return HookResult(action=Action.warn, message="in time")

        @on(Event.PostToolUse)
        def straggler(evt: Any) -> HookResult:
            release.wait(30)
            return HookResult(action=Action.warn, message="too late")

        deadline_ms = int((time.time() + SYNC_DEADLINE_MARGIN_SECONDS + HOOK_SLEEP_SECONDS) * 1000)
        overrides = reqenv.RequestOverrides(
            env={}, cwd="/w", client_ppid=1, session_id="s", deadline_unix_ms=deadline_ms
        )
        try:
            start = time.perf_counter()
            with reqenv.use_request(overrides):
                result = dispatch(Event.PostToolUse, make_post_tool_event())
            elapsed = time.perf_counter() - start
        finally:
            release.set()

        assert result is not None
        assert result["hookSpecificOutput"]["additionalContext"] == "in time"
        assert elapsed < HOOK_SLEEP_SECONDS * 5, f"the reply waited {elapsed:.2f}s on an abandoned hook"

    @pytest.mark.usefixtures("frozen_clock")
    def test_an_abandoned_hook_does_not_delay_the_next_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        started = threading.Event()
        release = threading.Event()
        stragglers = count()

        @on(Event.PostToolUse)
        def straggler(evt: Any) -> HookResult:
            if next(stragglers) == 0:
                started.set()
                release.wait(30)
            return HookResult(action=Action.warn, message="answered")

        try:
            with pinch_pool(monkeypatch, 1):
                with reqenv.use_request(overrides := bounded_request(HOOK_SLEEP_SECONDS)):
                    abandoned = dispatch(Event.PostToolUse, make_post_tool_event())
                assert started.wait(30)
                start = time.perf_counter()
                with reqenv.use_request(bounded_request(30)):
                    prompt = dispatch(Event.PostToolUse, make_post_tool_event())
                elapsed = time.perf_counter() - start
        finally:
            release.set()

        assert abandoned is None
        assert overrides.abandoned == ["straggler"]
        assert prompt is not None
        assert prompt["hookSpecificOutput"]["additionalContext"] == "answered"
        assert elapsed < HOOK_SLEEP_SECONDS * 5, f"the next event waited {elapsed:.2f}s behind an abandoned hook"

    @pytest.mark.usefixtures("frozen_clock")
    def test_an_abandoned_hook_stops_at_its_next_checkpoint_and_frees_its_thread(self) -> None:
        started = threading.Event()
        release = threading.Event()
        stopped = threading.Event()
        outlived: list[str] = []

        @on(Event.PostToolUse)
        def straggler(evt: Any) -> None:
            started.set()
            release.wait(30)
            try:
                reqenv.checkpoint()
            except reqenv.Abandoned:
                stopped.set()
                raise
            outlived.append("ran past its checkpoint")

        with reqenv.use_request(bounded_request(HOOK_SLEEP_SECONDS)):
            dispatch(Event.PostToolUse, make_post_tool_event())
        assert started.wait(30)
        release.set()

        assert stopped.wait(30)
        for thread in fanout_threads():
            thread.join(30)
        assert outlived == []
        assert fanout_threads() == []

    @pytest.mark.usefixtures("frozen_clock")
    def test_a_hook_whose_verdict_was_collected_never_sees_a_checkpoint_raise(self) -> None:
        @on(Event.PostToolUse)
        def careful(evt: Any) -> HookResult:
            reqenv.checkpoint()
            return HookResult(action=Action.warn, message="collected")

        with reqenv.use_request(bounded_request(30)):
            result = dispatch(Event.PostToolUse, make_post_tool_event())

        assert result is not None
        assert result["hookSpecificOutput"]["additionalContext"] == "collected"

    def test_a_hook_doomed_by_a_block_stops_at_its_next_checkpoint(self) -> None:
        started = threading.Event()
        release = threading.Event()
        stopped = threading.Event()

        @on(Event.PreToolUse)
        def blocker(evt: Any) -> HookResult:
            started.wait(30)
            return HookResult(action=Action.block, message="denied")

        @on(Event.PreToolUse)
        def doomed(evt: Any) -> HookResult:
            started.set()
            release.wait(30)
            try:
                reqenv.checkpoint()
            except reqenv.Abandoned:
                stopped.set()
                raise
            return HookResult(action=Action.warn, message="never delivered")

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        release.set()

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == "denied"
        assert stopped.wait(30)

    def test_concurrent_hooks_share_session_state_without_losing_writes(self, tmp_path: Path) -> None:
        from captain_hook.state import SeenKeys

        evt = make_post_tool_event(ctx=make_ctx(tmp_path))

        def mark(name: str) -> None:
            evt.ctx.session.once(name, scope="shared")

        for name in ("a", "b", "c", "d", "e"):
            on(Event.PostToolUse)(distinct_hook(name, mark))

        dispatch(Event.PostToolUse, evt, session_dir=tmp_path)

        assert sorted(SessionStore(tmp_path).load(SeenKeys).seen["shared"]) == ["a", "b", "c", "d", "e"]

    def test_max_fires_holds_across_concurrent_registrations_of_one_hook(self, tmp_path: Path) -> None:
        fired = 0
        guard = threading.Lock()

        def capped(evt: Any) -> HookResult:
            nonlocal fired
            with guard:
                fired += 1
            return HookResult(action=Action.warn, message="fired")

        for _ in range(10):
            on(Event.PostToolUse, max_fires=3)(capped)

        result = dispatch(Event.PostToolUse, make_post_tool_event(), session_dir=tmp_path)

        assert fired == 3, f"a capped hook fired {fired} times, expected 3"
        assert result is not None
        assert result["hookSpecificOutput"]["additionalContext"] == "fired\n\nfired\n\nfired"

    def test_each_hook_carries_the_request_context(self) -> None:
        seen: list[tuple[str, str]] = []
        guard = threading.Lock()

        for _ in range(5):

            @on(Event.PostToolUse)
            def observer(evt: Any) -> None:
                assert (current := reqenv.current()) is not None
                with guard:
                    seen.append((current.session_id, str(reqenv.cwd())))

        overrides = reqenv.RequestOverrides(
            env={}, cwd="/scoped", client_ppid=1, session_id="scoped-session", deadline_unix_ms=0
        )
        with reqenv.use_request(overrides):
            dispatch(Event.PostToolUse, make_post_tool_event())

        assert seen == [("scoped-session", "/scoped")] * 5

    def test_a_later_block_never_suppresses_an_earlier_hook(self) -> None:
        blocked = FirstBlock()
        blocked.record(3)
        entry = RegisteredHook(spec=HookSpec(events=Event.PreToolUse), handler=lambda evt: None, name="h")

        assert doomed_by_block(entry, 4, blocked)
        assert not doomed_by_block(entry, 3, blocked)
        assert not doomed_by_block(entry, 2, blocked)

    def test_a_hook_suppressed_by_a_block_never_raises_into_the_reply(self) -> None:
        from captain_hook.transcripts import TranscriptLoadError

        register_hook(Event.PreToolUse, message="stop here", block=True)

        @on(Event.PreToolUse)
        def raiser(evt: Any) -> HookResult:
            raise TranscriptLoadError(None)

        result = dispatch(Event.PreToolUse, make_pre_tool_event())
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == "stop here"

    def test_registrations_sharing_a_state_key_run_in_registration_order(self, tmp_path: Path) -> None:
        # A capped hook reserves its fire before running and releases it on a falsy result, so a
        # sibling registration under the same state key must not read the count mid-reservation.
        seen: list[int] = []
        counter = count()

        def capped(evt: Any) -> HookResult | None:
            seen.append(nth := next(counter))
            return None if nth == 0 else HookResult(action=Action.warn, message="fired")

        for _ in range(2):
            on(Event.PostToolUse, max_fires=1)(capped)

        result = dispatch(Event.PostToolUse, make_post_tool_event(), session_dir=tmp_path)

        assert seen == [0, 1]
        assert result is not None
        assert result["hookSpecificOutput"]["additionalContext"] == "fired"

    def test_a_busy_background_pool_does_not_delay_a_synchronous_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        release = threading.Event()
        pool = ThreadPoolExecutor(max_workers=1)
        monkeypatch.setattr(dispatch_module, "background_pool", lambda: pool)

        @on(Event.PostToolUse, async_=True)
        def slow_background(evt: Any) -> None:
            release.wait(30)

        @on(Event.PreToolUse)
        def gate(evt: Any) -> HookResult:
            return HookResult(action=Action.block, message="denied")

        occupier = threading.Thread(target=dispatch_async, args=(make_post_tool_event(),))
        occupier.start()
        try:
            start = time.perf_counter()
            result = dispatch(Event.PreToolUse, make_pre_tool_event())
            elapsed = time.perf_counter() - start
        finally:
            release.set()
            occupier.join(30)
            pool.shutdown(wait=True)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert elapsed < HOOK_SLEEP_SECONDS * 5, f"a blocking gate waited {elapsed:.2f}s on background work"

    def test_async_hooks_run_concurrently(self) -> None:
        def sleep_a_while(name: str) -> None:
            time.sleep(HOOK_SLEEP_SECONDS)

        for name in ("a", "b", "c", "d", "e"):
            on(Event.PostToolUse, async_=True)(distinct_hook(name, sleep_a_while))

        start = time.perf_counter()
        dispatch_async(make_post_tool_event())
        elapsed = time.perf_counter() - start

        assert elapsed < HOOK_SLEEP_SECONDS * 2.5, f"five {HOOK_SLEEP_SECONDS}s async hooks took {elapsed:.2f}s"
