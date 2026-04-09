from __future__ import annotations

import pytest
from pathlib import Path
from typing import Any
from captain_hook.app import (
    _state,
    reset,
)
from captain_hook.dispatch import dispatch
from captain_hook.events import PostToolUseEvent, StopEvent, SubagentStopEvent
from captain_hook.types import Action, Event, Signal
from captain_hook.tests.helpers import make_ctx


def make_stop_event(ctx: Any = None) -> StopEvent:
    return StopEvent(_raw={}, ctx=ctx or make_ctx())


def make_subagent_stop_event(ctx: Any = None) -> SubagentStopEvent:
    return SubagentStopEvent(_raw={}, ctx=ctx or make_ctx())


def make_post_tool_event(
    tool_name: str = "Bash",
    tool_input: dict[str, Any] | None = None,
    ctx: Any = None,
) -> PostToolUseEvent:
    raw: dict[str, Any] = {"tool_name": tool_name}
    if tool_input is not None:
        raw["tool_input"] = tool_input
    return PostToolUseEvent(_raw=raw, ctx=ctx or make_ctx())


def register_llm_gate(
        prompt: str,
    *,
    message: str | Any = "BLOCKED",
    response_model: Any = None,
    verdict: Any = None,
    signals: Any = None,
    when: Any = None,
    events: Event | None = None,
    max_fires: int | None = None,
    only_if: Any = (),
    skip_if: Any = (),
    max_context: int = 2000,
    **kwargs: Any,
) -> None:
    from captain_hook.primitives.llm import llm_gate

    kw: dict[str, Any] = {"message": message}
    if response_model is not None:
        kw["response_model"] = response_model
    if verdict is not None:
        kw["verdict"] = verdict
    if signals is not None:
        kw["signals"] = signals
    if when is not None:
        kw["when"] = when
    if events is not None:
        kw["events"] = events
    if max_fires is not None:
        kw["max_fires"] = max_fires
    if only_if:
        kw["only_if"] = only_if
    if skip_if:
        kw["skip_if"] = skip_if
    if max_context != 2000:
        kw["max_context"] = max_context
    kw.update(kwargs)

    llm_gate(prompt, **kw)
def register_llm_nudge(
        prompt: str,
    *,
    message: str | Any = "WARNING",
    response_model: Any = None,
    verdict: Any = None,
    signals: Any = None,
    when: Any = None,
    events: Event | None = None,
    max_fires: int | None = None,
    async_: bool = False,
    max_context: int = 2000,
    **kwargs: Any,
) -> None:
    from captain_hook.primitives.llm import llm_nudge

    kw: dict[str, Any] = {"message": message}
    if response_model is not None:
        kw["response_model"] = response_model
    if verdict is not None:
        kw["verdict"] = verdict
    if signals is not None:
        kw["signals"] = signals
    if when is not None:
        kw["when"] = when
    if events is not None:
        kw["events"] = events
    if max_fires is not None:
        kw["max_fires"] = max_fires
    kw["async_"] = async_
    if max_context != 2000:
        kw["max_context"] = max_context
    kw.update(kwargs)

    from captain_hook.primitives.llm import llm_nudge

    llm_nudge(prompt, **kw)


class TestLlmGateBlocks:
    def test_llm_gate_blocks_on_true_verdict(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import GateVerdict

        ctx = make_ctx(tmp_path, texts=["some context"], call_llm_return=GateVerdict(block=True, reasoning="bad"))

        register_llm_gate("Check this", message="BLOCKED", when=lambda evt: True)
        evt = make_stop_event(ctx=ctx)
        result = dispatch(Event.Stop, evt, session_dir=tmp_path)

        assert result is not None
        assert result["decision"] == "block"


class TestLlmGateAllows:
    def test_llm_gate_allows_on_false_verdict(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import GateVerdict

        ctx = make_ctx(tmp_path, texts=["some context"], call_llm_return=GateVerdict(block=False, reasoning="ok"))

        register_llm_gate("Check this", message="BLOCKED", when=lambda evt: True)
        evt = make_stop_event(ctx=ctx)
        result = dispatch(Event.Stop, evt, session_dir=tmp_path)

        assert result is None


class TestLlmGateNoSignalMatch:
    def test_llm_gate_skips_when_signals_dont_match(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path, texts=["unrelated text"])

        register_llm_gate(
            "Check this",
            message="BLOCKED",
            signals=[Signal(pattern=r"critical_error", weight=1)],
        )
        evt = make_stop_event(ctx=ctx)
        result = dispatch(Event.Stop, evt, session_dir=tmp_path)

        assert result is None
        ctx.call_llm.assert_not_called()


class TestLlmGateCallableMessage:
    def test_llm_gate_callable_message(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import GateVerdict

        verdict = GateVerdict(block=True, reasoning="very bad")
        ctx = make_ctx(tmp_path, texts=["some context"], call_llm_return=verdict)

        register_llm_gate(
            "Check this",
            message=lambda r: f"BLOCKED: {r.reasoning}",
            when=lambda evt: True,
        )
        evt = make_stop_event(ctx=ctx)
        result = dispatch(Event.Stop, evt, session_dir=tmp_path)

        assert result is not None
        assert result["decision"] == "block"
        assert "very bad" in result["reason"]


class TestLlmGateNoneResponse:
    def test_llm_gate_returns_none_on_llm_none(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path, texts=["some context"], call_llm_return=None)

        register_llm_gate("Check this", message="BLOCKED", when=lambda evt: True)
        evt = make_stop_event(ctx=ctx)
        result = dispatch(Event.Stop, evt, session_dir=tmp_path)

        assert result is None


class TestLlmGateCustomVerdict:
    def test_llm_gate_custom_verdict_inverts(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import GateVerdict

        ctx = make_ctx(tmp_path, texts=["some context"], call_llm_return=GateVerdict(block=False, reasoning="ok"))

        register_llm_gate(
            "Check this",
            message="INVERTED BLOCK",
            verdict=lambda r: not r.block,
            when=lambda evt: True,
        )
        evt = make_stop_event(ctx=ctx)
        result = dispatch(Event.Stop, evt, session_dir=tmp_path)

        assert result is not None
        assert result["decision"] == "block"


class TestLlmGateCustomModel:
    def test_llm_gate_custom_response_model(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import GateVerdict

        class CustomGateVerdict(GateVerdict):
            severity: str = "low"

        verdict = CustomGateVerdict(block=True, reasoning="bad", severity="high")
        ctx = make_ctx(tmp_path, texts=["some context"], call_llm_return=verdict)

        register_llm_gate(
            "Check this",
            message=lambda r: f"BLOCKED (severity={r.severity})",
            response_model=CustomGateVerdict,
            when=lambda evt: True,
        )
        evt = make_stop_event(ctx=ctx)
        result = dispatch(Event.Stop, evt, session_dir=tmp_path)

        assert result is not None
        assert "severity=high" in result["reason"]


class TestLlmNudgeWarns:
    def test_llm_nudge_warns_on_fire_true(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import NudgeVerdict

        ctx = make_ctx(tmp_path, texts=["some context"], call_llm_return=NudgeVerdict(fire=True, reasoning="issue"))

        register_llm_nudge("Check this", message="WARNING", when=lambda evt: True)
        evt = make_post_tool_event(ctx=ctx)
        result = dispatch(Event.PostToolUse, evt, session_dir=tmp_path)

        assert result is not None
        assert "additionalContext" in result["hookSpecificOutput"]


class TestLlmNudgeAllows:
    def test_llm_nudge_allows_on_fire_false(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import NudgeVerdict

        ctx = make_ctx(tmp_path, texts=["some context"], call_llm_return=NudgeVerdict(fire=False, reasoning="ok"))

        register_llm_nudge("Check this", message="WARNING", when=lambda evt: True)
        evt = make_post_tool_event(ctx=ctx)
        result = dispatch(Event.PostToolUse, evt, session_dir=tmp_path)

        assert result is None


class TestLlmNudgeAsyncSkippedInSync:
    def test_llm_nudge_async_skipped_in_sync(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import NudgeVerdict

        ctx = make_ctx(tmp_path, texts=["some context"], call_llm_return=NudgeVerdict(fire=True, reasoning="issue"))

        register_llm_nudge(
            "Check this",
            message="WARNING",
            when=lambda evt: True,
            async_=True,
        )
        evt = make_post_tool_event(ctx=ctx)
        result = dispatch(Event.PostToolUse, evt, session_dir=tmp_path, async_=False)

        assert result is None


class TestLlmNudgeAsyncDispatched:
    def test_llm_nudge_async_dispatched_in_async_mode(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import NudgeVerdict

        ctx = make_ctx(tmp_path, texts=["some context"], call_llm_return=NudgeVerdict(fire=True, reasoning="issue"))

        register_llm_nudge(
            "Check this",
            message="WARNING",
            when=lambda evt: True,
            async_=True,
        )
        evt = make_post_tool_event(ctx=ctx)
        result = dispatch(Event.PostToolUse, evt, session_dir=tmp_path, async_=True)

        assert result is not None
        assert "additionalContext" in result["hookSpecificOutput"]


class TestLlmGateDefaultEvents:
    def test_llm_gate_default_events(self, tmp_path: Path) -> None:
        register_llm_gate("Check this", message="BLOCKED", when=lambda evt: True)

        assert len(_state.hooks) == 1
        assert _state.hooks[0].spec.events == (Event.Stop | Event.SubagentStop)


class TestLlmNudgeDefaultEvents:
    def test_llm_nudge_default_events(self, tmp_path: Path) -> None:
        register_llm_nudge("Check this", message="WARNING", when=lambda evt: True)

        assert len(_state.hooks) == 1
        assert _state.hooks[0].spec.events == Event.PostToolUse


class TestLlmGateDefaultMaxFires:
    def test_llm_gate_default_max_fires(self, tmp_path: Path) -> None:
        register_llm_gate("Check this", message="BLOCKED", when=lambda evt: True)

        assert _state.hooks[0].spec.max_fires == 1


class TestLlmNudgeDefaultMaxFires:
    def test_llm_nudge_default_max_fires(self, tmp_path: Path) -> None:
        register_llm_nudge("Check this", message="WARNING", when=lambda evt: True)

        assert _state.hooks[0].spec.max_fires == 3


class TestLlmEvaluateFiredThisTurn:
    def test_llm_evaluate_skips_when_fired_this_turn(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import GateVerdict

        verdict = GateVerdict(block=True, reasoning="bad")
        ctx = make_ctx(tmp_path, texts=["some context"], call_llm_return=verdict)

        register_llm_gate("Check this", message="BLOCKED", when=lambda evt: True)

        # First dispatch fires
        evt1 = make_stop_event(ctx=ctx)
        result1 = dispatch(Event.Stop, evt1, session_dir=tmp_path)
        assert result1 is not None

        # Second dispatch in same turn should skip (fired_this_turn=True)
        ctx.call_llm.reset_mock()
        evt2 = make_stop_event(ctx=ctx)
        result2 = dispatch(Event.Stop, evt2, session_dir=tmp_path)
        assert result2 is None
        ctx.call_llm.assert_not_called()


class TestLlmEvaluateWhenPredicate:
    def test_llm_evaluate_when_true_triggers_llm(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import GateVerdict

        ctx = make_ctx(tmp_path, texts=["context text"], call_llm_return=GateVerdict(block=True, reasoning="bad"))

        register_llm_gate("Check this", message="BLOCKED", when=lambda evt: True)
        evt = make_stop_event(ctx=ctx)
        result = dispatch(Event.Stop, evt, session_dir=tmp_path)

        assert result is not None
        ctx.call_llm.assert_called_once()

    def test_llm_evaluate_when_false_skips_llm(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path, texts=["context text"])

        register_llm_gate("Check this", message="BLOCKED", when=lambda evt: False)
        evt = make_stop_event(ctx=ctx)
        result = dispatch(Event.Stop, evt, session_dir=tmp_path)

        assert result is None
        ctx.call_llm.assert_not_called()


class TestLlmEvaluateMaxContext:
    def test_llm_evaluate_truncates_context(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import GateVerdict

        long_text = "x" * 5000
        ctx = make_ctx(tmp_path, texts=[long_text], call_llm_return=GateVerdict(block=True, reasoning="bad"))

        register_llm_gate(
            "Check this",
            message="BLOCKED",
            when=lambda evt: True,
            max_context=100,
        )
        evt = make_stop_event(ctx=ctx)
        dispatch(Event.Stop, evt, session_dir=tmp_path)

        ctx.call_llm.assert_called_once()
        call_args = ctx.call_llm.call_args
        prompt_arg = call_args[0][0] if call_args[0] else call_args[1].get("template", "")
        prompt_str = str(prompt_arg)
        assert len(prompt_str) <= 5000 + 200


class TestPromptCheckBlock:
    def test_prompt_check_returns_block(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import PromptCheckVerdict, prompt_check

        ctx = make_ctx(tmp_path, call_llm_return=PromptCheckVerdict(action="block", reason="stop that"))

        evt = make_stop_event(ctx=ctx)
        result = prompt_check(evt, "Check {item}", {"item": "things"}, prefix="STYLE")

        assert result is not None
        assert result.action is Action.block
        assert "STYLE" in result.message
        assert "stop that" in result.message


class TestPromptCheckWarn:
    def test_prompt_check_returns_warn(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import PromptCheckVerdict, prompt_check

        ctx = make_ctx(tmp_path, call_llm_return=PromptCheckVerdict(action="warning", reason="minor issue"))

        evt = make_stop_event(ctx=ctx)
        result = prompt_check(evt, "Check {item}", {"item": "things"}, prefix="STYLE")

        assert result is not None
        assert result.action is Action.warn
        assert "STYLE" in result.message
        assert "minor issue" in result.message


class TestPromptCheckOk:
    def test_prompt_check_returns_none_on_ok(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import PromptCheckVerdict, prompt_check

        ctx = make_ctx(tmp_path, call_llm_return=PromptCheckVerdict(action="ok", reason="looks good"))

        evt = make_stop_event(ctx=ctx)
        result = prompt_check(evt, "Check {item}", {"item": "things"}, prefix="STYLE")

        assert result is None


class TestPromptCheckLlmNone:
    def test_prompt_check_returns_none_when_llm_returns_none(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import prompt_check

        ctx = make_ctx(tmp_path, call_llm_return=None)

        evt = make_stop_event(ctx=ctx)
        result = prompt_check(evt, "Check {item}", {"item": "things"}, prefix="STYLE")

        assert result is None


class TestPromptCheckReasoning:
    def test_prompt_check_includes_reasoning(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import PromptCheckVerdict, prompt_check
        from captain_hook.prompt import PromptMessage

        ctx = make_ctx(tmp_path, texts=["I decided to do X because Y"], call_llm_return=PromptCheckVerdict(action="block", reason="bad reasoning"))

        evt = make_stop_event(ctx=ctx)
        prompt_check(
            evt,
            "Check {item}",
            {"item": "things"},
            prefix="STYLE",
            include_reasoning=True,
        )

        ctx.call_llm.assert_called_once()
        call_args = ctx.call_llm.call_args
        prompt_arg = call_args[0][0]
        assert isinstance(prompt_arg, PromptMessage)
        assert "agent_reasoning" in str(prompt_arg)
        assert "I decided to do X because Y" in str(prompt_arg)


class TestPromptBuilderUsed:
    def test_llm_gate_uses_prompt_builder(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import GateVerdict
        from captain_hook.prompt import PromptMessage

        ctx = make_ctx(tmp_path, texts=["some context"], call_llm_return=GateVerdict(block=True, reasoning="bad"))

        register_llm_gate("Check this", message="BLOCKED", when=lambda evt: True)
        evt = make_stop_event(ctx=ctx)
        dispatch(Event.Stop, evt, session_dir=tmp_path)

        ctx.call_llm.assert_called_once()
        call_args = ctx.call_llm.call_args
        prompt_arg = call_args[0][0] if call_args[0] else ""
        assert isinstance(prompt_arg, PromptMessage)
        rendered = str(prompt_arg)
        assert "Check this" in rendered
        assert "<context>" in rendered

    def test_prompt_check_uses_prompt_builder(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import PromptCheckVerdict, prompt_check
        from captain_hook.prompt import PromptMessage

        ctx = make_ctx(tmp_path, call_llm_return=PromptCheckVerdict(action="ok", reason="fine"))

        evt = make_stop_event(ctx=ctx)
        prompt_check(evt, "Check {item}", {"item": "things"}, prefix="STYLE")

        ctx.call_llm.assert_called_once()
        prompt_arg = ctx.call_llm.call_args[0][0]
        assert isinstance(prompt_arg, PromptMessage)


class TestLlmEvaluateBothSignalsAndWhen:
    def test_llm_evaluate_ignores_when_if_signals_present(self, tmp_path: Path) -> None:
        ctx = make_ctx(tmp_path, texts=["no match"])

        register_llm_gate(
            "Check this",
            message="BLOCKED",
            signals=[Signal(pattern=r"zzz_nomatch", weight=1)],
            when=lambda evt: 1 / 0,  # would raise if called
        )
        evt = make_stop_event(ctx=ctx)
        result = dispatch(Event.Stop, evt, session_dir=tmp_path)

        assert result is None  # signals don't match, when not called


class TestLlmNudgeDefaultAsync:
    def test_llm_nudge_defaults_async_false(self, tmp_path: Path) -> None:
        register_llm_nudge("Check this", message="WARNING", when=lambda evt: True)

        assert _state.hooks[0].spec.async_ is False


# Regression: braces in prompt context should not cause double-formatting


class TestLlmBracesInPrompt:
    def test_llm_gate_passes_prompt_message_not_str(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import GateVerdict
        from captain_hook.prompt import PromptMessage

        verdict = GateVerdict(block=True, reasoning="bad")
        ctx = make_ctx(tmp_path, texts=['{"key": "value"}'], call_llm_return=verdict)

        register_llm_gate("Check this code", message="BLOCKED", when=lambda evt: True)
        evt = make_stop_event(ctx=ctx)
        dispatch(Event.Stop, evt, session_dir=tmp_path)

        ctx.call_llm.assert_called_once()
        prompt_arg = ctx.call_llm.call_args[0][0]
        assert isinstance(prompt_arg, PromptMessage)
        assert '{"key": "value"}' in str(prompt_arg)

    def test_llm_nudge_passes_prompt_message_not_str(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import NudgeVerdict
        from captain_hook.prompt import PromptMessage

        verdict = NudgeVerdict(fire=True, reasoning="issue")
        ctx = make_ctx(tmp_path, texts=["def foo(): return {x: 1}"], call_llm_return=verdict)

        register_llm_nudge("Check this", message="WARNING", when=lambda evt: True)
        evt = make_post_tool_event(ctx=ctx)
        dispatch(Event.PostToolUse, evt, session_dir=tmp_path)

        ctx.call_llm.assert_called_once()
        prompt_arg = ctx.call_llm.call_args[0][0]
        assert isinstance(prompt_arg, PromptMessage)
        assert "{x: 1}" in str(prompt_arg)


# Regression: fire state should update only after verdict confirms action


class TestFireStateTiming:
    def test_llm_gate_no_fire_state_on_false_verdict_with_signals(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import GateVerdict
        from captain_hook.state import PrimitiveState

        ctx = make_ctx(tmp_path, texts=["critical error found"], call_llm_return=GateVerdict(block=False, reasoning="ok"))

        register_llm_gate(
            "Check this",
            message="BLOCKED",
            signals=[Signal(pattern=r"critical", weight=1)],
            max_fires=5,
        )
        evt = make_stop_event(ctx=ctx)
        dispatch(Event.Stop, evt, session_dir=tmp_path)

        ps = ctx.s[PrimitiveState].get()
        assert ps is None or ps.last_fired_at == 0

    def test_llm_nudge_no_fire_state_on_false_verdict_with_signals(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import NudgeVerdict
        from captain_hook.state import PrimitiveState

        ctx = make_ctx(tmp_path, texts=["critical error found"], call_llm_return=NudgeVerdict(fire=False, reasoning="ok"))

        register_llm_nudge(
            "Check this",
            message="WARNING",
            signals=[Signal(pattern=r"critical", weight=1)],
            max_fires=5,
        )
        evt = make_post_tool_event(ctx=ctx)
        dispatch(Event.PostToolUse, evt, session_dir=tmp_path)

        ps = ctx.s[PrimitiveState].get()
        assert ps is None or ps.last_fired_at == 0

    def test_llm_gate_fire_state_on_true_verdict(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import GateVerdict
        from captain_hook.state import PrimitiveState

        ctx = make_ctx(tmp_path, texts=["critical error found"], call_llm_return=GateVerdict(block=True, reasoning="bad"))

        register_llm_gate(
            "Check this",
            message="BLOCKED",
            signals=[Signal(pattern=r"critical", weight=1)],
            max_fires=5,
        )
        evt = make_stop_event(ctx=ctx)
        dispatch(Event.Stop, evt, session_dir=tmp_path)

        ps = ctx.s[PrimitiveState].get()
        assert ps is not None
        assert ps.last_fired_at > 0


# Regression: signal-driven LLM hook suppression bug
# Two signal-driven LLM hooks in the same turn: first returns no-action,
# second should still fire (consumed hashes should not suppress later hooks).


class TestSignalConsumptionNotSuppressLaterHooks:
    def test_two_llm_gates_first_no_action_second_fires(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import GateVerdict

        ctx = make_ctx(tmp_path, texts=["critical error found in module"])

        call_count = 0

        def mock_llm(*args: Any, **kwargs: Any) -> GateVerdict:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return GateVerdict(block=False, reasoning="not a real issue")
            return GateVerdict(block=True, reasoning="actual problem")

        ctx.call_llm = mock_llm

        register_llm_gate(
            "First gate check",
            message="GATE1",
            signals=[Signal(pattern=r"critical", weight=1)],
            max_fires=5,
        )
        register_llm_gate(
            "Second gate check",
            message="GATE2",
            signals=[Signal(pattern=r"critical", weight=1)],
            max_fires=5,
        )

        evt = make_stop_event(ctx=ctx)
        result = dispatch(Event.Stop, evt, session_dir=tmp_path)

        assert call_count == 2, f"Expected both LLM hooks to be called, but only {call_count} were"
        assert result is not None, "Second gate should have blocked"
        assert result["decision"] == "block"
        assert "GATE2" in result["reason"]

    def test_two_llm_nudges_first_no_action_second_fires(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import NudgeVerdict

        ctx = make_ctx(tmp_path, texts=["critical error found in module"])

        call_count = 0

        def mock_llm(*args: Any, **kwargs: Any) -> NudgeVerdict:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return NudgeVerdict(fire=False, reasoning="not relevant")
            return NudgeVerdict(fire=True, reasoning="needs attention")

        ctx.call_llm = mock_llm

        register_llm_nudge(
            "First nudge check",
            message="NUDGE1",
            signals=[Signal(pattern=r"critical", weight=1)],
            max_fires=5,
        )
        register_llm_nudge(
            "Second nudge check",
            message="NUDGE2",
            signals=[Signal(pattern=r"critical", weight=1)],
            max_fires=5,
        )

        evt = make_post_tool_event(ctx=ctx)
        result = dispatch(Event.PostToolUse, evt, session_dir=tmp_path)

        assert call_count == 2, f"Expected both LLM hooks to be called, but only {call_count} were"
        assert result is not None, "Second nudge should have warned"
        assert "NUDGE2" in result["hookSpecificOutput"]["additionalContext"]

    def test_llm_gate_no_action_does_not_consume_hashes(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import GateVerdict
        from captain_hook.state import PrimitiveState

        ctx = make_ctx(tmp_path, texts=["critical error found"], call_llm_return=GateVerdict(block=False, reasoning="ok"))

        register_llm_gate(
            "Gate check",
            message="BLOCKED",
            signals=[Signal(pattern=r"critical", weight=1)],
            max_fires=5,
        )

        evt = make_stop_event(ctx=ctx)
        dispatch(Event.Stop, evt, session_dir=tmp_path)

        ps = ctx.s[PrimitiveState].get()
        consumed = ps.consumed if ps else set()
        assert len(consumed) == 0, f"Consumed hashes should be empty after no-action verdict, got {consumed}"


class TestLlmPrimitiveHelper:
    def test_gate_and_nudge_produce_identical_handler_structure(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import GateVerdict, NudgeVerdict

        gate_dir = tmp_path / "gate"
        gate_dir.mkdir()
        gate_verdict = GateVerdict(block=True, reasoning="bad")
        gate_ctx = make_ctx(gate_dir, texts=["trigger"], call_llm_return=gate_verdict)
        register_llm_gate("Gate prompt", message="BLOCKED", when=lambda evt: True)
        gate_evt = make_stop_event(ctx=gate_ctx)
        gate_result = dispatch(Event.Stop, gate_evt, session_dir=gate_dir)
        assert gate_result is not None
        assert gate_result["decision"] == "block"

        reset()

        nudge_dir = tmp_path / "nudge"
        nudge_dir.mkdir()
        nudge_verdict = NudgeVerdict(fire=True, reasoning="issue")
        nudge_ctx = make_ctx(nudge_dir, texts=["trigger"], call_llm_return=nudge_verdict)
        register_llm_nudge("Nudge prompt", message="WARNING", when=lambda evt: True)
        nudge_evt = make_post_tool_event(ctx=nudge_ctx)
        nudge_result = dispatch(Event.PostToolUse, nudge_evt, session_dir=nudge_dir)
        assert nudge_result is not None
        assert "additionalContext" in nudge_result["hookSpecificOutput"]

    def test_helper_respects_async_parameter(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import NudgeVerdict

        ctx = make_ctx(tmp_path, texts=["trigger"], call_llm_return=NudgeVerdict(fire=True, reasoning="issue"))
        register_llm_nudge("Async nudge", message="WARNING", when=lambda evt: True, async_=True)

        assert _state.hooks[-1].spec.async_ is True

        evt = make_post_tool_event(ctx=ctx)
        sync_result = dispatch(Event.PostToolUse, evt, session_dir=tmp_path, async_=False)
        assert sync_result is None

        async_result = dispatch(Event.PostToolUse, evt, session_dir=tmp_path, async_=True)
        assert async_result is not None
