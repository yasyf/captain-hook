from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from captain_hook.app import (
    _state,
    reset,
)
from captain_hook.dispatch import dispatch
from captain_hook.packs.general.review import EditedSource
from captain_hook.packs.general.tombstones import TombstoneComments, is_marker, is_tombstone
from captain_hook.testing.helpers import fixture_session
from captain_hook.types import Action, Event, RanCommand, Signal, Tool, Waiting
from tests.helpers import (
    build_ctx,
    make_ctx,
    make_post_tool_event,
    make_pre_tool_event,
    make_stop_event,
    raw_tool_msg,
)


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


class TestLlmNudgeExitPlanModeGate:
    """Wiring for the band-aid-plan nudge: only_if=[Tool('ExitPlanMode')] on PostToolUse."""

    def test_fires_on_exitplanmode_when_fire_true(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import NudgeVerdict

        ctx = make_ctx(tmp_path, texts=["plan"], call_llm_return=NudgeVerdict(fire=True, reasoning="band-aid"))
        register_llm_nudge("Band-aid?", message="WARNING", only_if=[Tool("ExitPlanMode")], events=Event.PostToolUse)
        evt = make_post_tool_event(tool_name="ExitPlanMode", ctx=ctx)
        result = dispatch(Event.PostToolUse, evt, session_dir=tmp_path)

        assert result is not None
        assert "additionalContext" in result["hookSpecificOutput"]

    def test_silent_on_exitplanmode_when_fire_false(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import NudgeVerdict

        ctx = make_ctx(tmp_path, texts=["plan"], call_llm_return=NudgeVerdict(fire=False, reasoning="root cause"))
        register_llm_nudge("Band-aid?", message="WARNING", only_if=[Tool("ExitPlanMode")], events=Event.PostToolUse)
        evt = make_post_tool_event(tool_name="ExitPlanMode", ctx=ctx)

        assert dispatch(Event.PostToolUse, evt, session_dir=tmp_path) is None

    def test_skips_non_exitplanmode_tools(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import NudgeVerdict

        ctx = make_ctx(tmp_path, texts=["plan"], call_llm_return=NudgeVerdict(fire=True, reasoning="band-aid"))
        register_llm_nudge("Band-aid?", message="WARNING", only_if=[Tool("ExitPlanMode")], events=Event.PostToolUse)
        evt = make_post_tool_event(tool_name="Edit", ctx=ctx)

        assert dispatch(Event.PostToolUse, evt, session_dir=tmp_path) is None
        ctx.call_llm.assert_not_called()


class TestLlmGateWaitAwareDefault:
    def test_stop_llm_gate_without_skip_if_gets_waiting(self) -> None:
        register_llm_gate("Check this", message="BLOCKED", when=lambda evt: True)
        assert _state.hooks[0].spec.skip_if == (Waiting(),)

    def test_stop_llm_gate_skip_if_is_additive_with_waiting(self) -> None:
        register_llm_gate("Check this", message="BLOCKED", when=lambda evt: True, skip_if=[RanCommand("pytest")])
        assert _state.hooks[0].spec.skip_if == (Waiting(), RanCommand("pytest"))

    def test_posttooluse_llm_gate_is_not_wait_aware(self) -> None:
        register_llm_gate("Check this", message="BLOCKED", when=lambda evt: True, events=Event.PostToolUse)
        assert _state.hooks[0].spec.skip_if == ()

    def test_llm_nudge_is_not_wait_aware(self) -> None:
        register_llm_nudge("Check this", message="WARNING", when=lambda evt: True)
        assert _state.hooks[0].spec.skip_if == ()


class TestLlmGateDefaultMaxFires:
    def test_llm_gate_defaults_to_unlimited_fires(self, tmp_path: Path) -> None:
        register_llm_gate("Check this", message="BLOCKED", when=lambda evt: True)

        # A gate must keep enforcing across turns, so it defaults to unlimited (once per turn).
        assert _state.hooks[0].spec.max_fires is None


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
        from captain_hook.prompt import Prompt

        ctx = make_ctx(
            tmp_path,
            texts=["I decided to do X because Y"],
            call_llm_return=PromptCheckVerdict(action="block", reason="bad reasoning"),
        )

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
        assert isinstance(prompt_arg, Prompt)
        assert "agent_reasoning" in str(prompt_arg)
        assert "I decided to do X because Y" in str(prompt_arg)


class TestPromptBuilderUsed:
    def test_llm_gate_uses_prompt_builder(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import GateVerdict
        from captain_hook.prompt import Prompt

        ctx = make_ctx(tmp_path, texts=["some context"], call_llm_return=GateVerdict(block=True, reasoning="bad"))

        register_llm_gate("Check this", message="BLOCKED", when=lambda evt: True)
        evt = make_stop_event(ctx=ctx)
        dispatch(Event.Stop, evt, session_dir=tmp_path)

        ctx.call_llm.assert_called_once()
        call_args = ctx.call_llm.call_args
        prompt_arg = call_args[0][0] if call_args[0] else ""
        assert isinstance(prompt_arg, Prompt)
        rendered = str(prompt_arg)
        assert "Check this" in rendered
        assert "<context>" in rendered

    def test_prompt_check_uses_prompt_builder(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import PromptCheckVerdict, prompt_check
        from captain_hook.prompt import Prompt

        ctx = make_ctx(tmp_path, call_llm_return=PromptCheckVerdict(action="ok", reason="fine"))

        evt = make_stop_event(ctx=ctx)
        prompt_check(evt, "Check {item}", {"item": "things"}, prefix="STYLE")

        ctx.call_llm.assert_called_once()
        prompt_arg = ctx.call_llm.call_args[0][0]
        assert isinstance(prompt_arg, Prompt)


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
        from captain_hook.prompt import Prompt

        verdict = GateVerdict(block=True, reasoning="bad")
        ctx = make_ctx(tmp_path, texts=['{"key": "value"}'], call_llm_return=verdict)

        register_llm_gate("Check this code", message="BLOCKED", when=lambda evt: True)
        evt = make_stop_event(ctx=ctx)
        dispatch(Event.Stop, evt, session_dir=tmp_path)

        ctx.call_llm.assert_called_once()
        prompt_arg = ctx.call_llm.call_args[0][0]
        assert isinstance(prompt_arg, Prompt)
        assert '{"key": "value"}' in str(prompt_arg)

    def test_llm_nudge_passes_prompt_message_not_str(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import NudgeVerdict
        from captain_hook.prompt import Prompt

        verdict = NudgeVerdict(fire=True, reasoning="issue")
        ctx = make_ctx(tmp_path, texts=["def foo(): return {x: 1}"], call_llm_return=verdict)

        register_llm_nudge("Check this", message="WARNING", when=lambda evt: True)
        evt = make_post_tool_event(ctx=ctx)
        dispatch(Event.PostToolUse, evt, session_dir=tmp_path)

        ctx.call_llm.assert_called_once()
        prompt_arg = ctx.call_llm.call_args[0][0]
        assert isinstance(prompt_arg, Prompt)
        assert "{x: 1}" in str(prompt_arg)


# Regression: fire state should update only after verdict confirms action


class TestFireStateTiming:
    def test_llm_gate_no_fire_state_on_false_verdict_with_signals(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import GateVerdict
        from captain_hook.state import PrimitiveState

        ctx = make_ctx(
            tmp_path, texts=["critical error found"], call_llm_return=GateVerdict(block=False, reasoning="ok")
        )

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

        ctx = make_ctx(
            tmp_path, texts=["critical error found"], call_llm_return=NudgeVerdict(fire=False, reasoning="ok")
        )

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

        ctx = make_ctx(
            tmp_path, texts=["critical error found"], call_llm_return=GateVerdict(block=True, reasoning="bad")
        )

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

        ctx = make_ctx(
            tmp_path, texts=["critical error found"], call_llm_return=GateVerdict(block=False, reasoning="ok")
        )

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


class TestDefaultAgentTranscript:
    def test_llm_gate_defaults_agent_and_transcript_to_true(self) -> None:
        from inspect import signature

        from captain_hook.primitives.llm import llm_gate

        params = signature(llm_gate).parameters
        assert params["agent"].default is True
        assert params["transcript"].default is True

    def test_llm_nudge_defaults_agent_and_transcript_to_true(self) -> None:
        from inspect import signature

        from captain_hook.primitives.llm import llm_nudge

        params = signature(llm_nudge).parameters
        assert params["agent"].default is True
        assert params["transcript"].default is True

    def test_llm_evaluate_keeps_old_defaults(self) -> None:
        from inspect import signature

        from captain_hook.primitives.llm import llm_evaluate

        params = signature(llm_evaluate).parameters
        assert params["agent"].default is False
        assert params["transcript"].default is False

    def test_primitives_default_diff_to_false(self) -> None:
        from inspect import signature

        from captain_hook.primitives.llm import llm_evaluate, llm_gate, llm_nudge, prompt_check

        for fn in (llm_gate, llm_nudge, llm_evaluate, prompt_check):
            assert signature(fn).parameters["diff"].default is False


class TestReviewGateDiff:
    def _register(self) -> None:
        from captain_hook.primitives.llm import llm_gate

        llm_gate(
            "review the diff",
            message=lambda r: f"issue: {r.reasoning}",
            diff=True,
            only_if=[EditedSource()],
            events=Event.Stop,
        )

    def _edited_ctx(self, session_dir: Path, *, file_path: str, block: bool) -> Any:
        from captain_hook.primitives.llm import GateVerdict

        ctx = build_ctx(
            transcript=fixture_session(
                [raw_tool_msg("Edit", {"file_path": file_path, "old_string": "a", "new_string": "b"})]
            ),
            session_dir=session_dir,
        )
        ctx.call_llm = MagicMock(return_value=GateVerdict(block=block, reasoning="r"))
        return ctx

    def test_blocks_on_bad_diff(self, tmp_path: Path) -> None:
        ctx = self._edited_ctx(tmp_path, file_path="src/app.py", block=True)
        self._register()
        result = dispatch(Event.Stop, make_stop_event(ctx=ctx), session_dir=tmp_path)
        assert result is not None
        assert result["decision"] == "block"

    def test_allows_on_clean_diff(self, tmp_path: Path) -> None:
        ctx = self._edited_ctx(tmp_path, file_path="src/app.py", block=False)
        self._register()
        result = dispatch(Event.Stop, make_stop_event(ctx=ctx), session_dir=tmp_path)
        assert result is None

    def test_skips_non_source_edit_before_llm(self, tmp_path: Path) -> None:
        ctx = self._edited_ctx(tmp_path, file_path="README.md", block=True)
        self._register()
        result = dispatch(Event.Stop, make_stop_event(ctx=ctx), session_dir=tmp_path)
        assert result is None
        ctx.call_llm.assert_not_called()


class TestLlmContexts:
    def _edit(self, ctx: Any, *, new: str) -> Any:
        return make_pre_tool_event("Edit", {"file_path": "a.py", "old_string": "x = 1\n", "new_string": new}, ctx=ctx)

    def _fire_ctx(self, tmp_path: Path, *, texts: list[str] | None = None) -> Any:
        from captain_hook.primitives.llm import NudgeVerdict

        return make_ctx(
            tmp_path, texts=texts or ["turn text"], call_llm_return=NudgeVerdict(fire=True, reasoning="bad")
        )

    def test_required_empty_context_skips_llm_and_consumes_no_fire(self, tmp_path: Path) -> None:
        from captain_hook.contexts import Introduced

        ctx = self._fire_ctx(tmp_path)
        register_llm_nudge("Check", message="WARNING", events=Event.PreToolUse, contexts=[Introduced(kind="comment")])

        assert dispatch(Event.PreToolUse, self._edit(ctx, new="x = 2\n"), session_dir=tmp_path) is None
        ctx.call_llm.assert_not_called()

        result = dispatch(Event.PreToolUse, self._edit(ctx, new="# removed it\nx = 2\n"), session_dir=tmp_path)
        assert result is not None
        ctx.call_llm.assert_called_once()

    def test_llm_gate_required_empty_context_skips(self, tmp_path: Path) -> None:
        from captain_hook.contexts import Introduced
        from captain_hook.primitives.llm import GateVerdict

        ctx = make_ctx(tmp_path, texts=["turn text"], call_llm_return=GateVerdict(block=True, reasoning="bad"))
        register_llm_gate("Check", message="BLOCKED", events=Event.PreToolUse, contexts=[Introduced(kind="comment")])

        assert dispatch(Event.PreToolUse, self._edit(ctx, new="x = 2\n"), session_dir=tmp_path) is None
        ctx.call_llm.assert_not_called()

    def test_context_blocks_ordered_and_transcript_fallback_suppressed(self, tmp_path: Path) -> None:
        from captain_hook.contexts import Introduced

        ctx = self._fire_ctx(tmp_path, texts=["transcript evidence"])
        register_llm_nudge(
            "Check",
            message="WARNING",
            events=Event.PreToolUse,
            contexts=[Introduced(pattern="print($$$)", tag="prints", required=False), Introduced(kind="comment")],
        )

        evt = self._edit(ctx, new="# note\nprint('hi')\nx = 2\n")
        assert dispatch(Event.PreToolUse, evt, session_dir=tmp_path) is not None

        prompt = str(ctx.call_llm.call_args[0][0])
        assert "<context>" not in prompt
        assert "transcript evidence" not in prompt
        positions = [prompt.index(tag) for tag in ("<prints>", "<introduced>", "<before_edit>", "<after_edit>")]
        assert positions == sorted(positions)

    def test_when_predicate_keeps_transcript_fallback(self, tmp_path: Path) -> None:
        from captain_hook.contexts import Introduced

        ctx = self._fire_ctx(tmp_path, texts=["transcript evidence"])
        register_llm_nudge(
            "Check",
            message="WARNING",
            events=Event.PreToolUse,
            when=lambda evt: True,
            contexts=[Introduced(kind="comment")],
        )

        assert dispatch(Event.PreToolUse, self._edit(ctx, new="# note\nx = 2\n"), session_dir=tmp_path) is not None

        prompt = str(ctx.call_llm.call_args[0][0])
        assert "<context>" in prompt
        assert "transcript evidence" in prompt

    def test_signals_and_contexts_compose(self, tmp_path: Path) -> None:
        from captain_hook.contexts import Introduced

        ctx = self._fire_ctx(tmp_path, texts=["critical error found"])
        register_llm_nudge(
            "Check",
            message="WARNING",
            events=Event.PreToolUse,
            signals=[Signal(pattern=r"critical", weight=1)],
            contexts=[Introduced(kind="comment")],
        )

        assert dispatch(Event.PreToolUse, self._edit(ctx, new="# note\nx = 2\n"), session_dir=tmp_path) is not None

        prompt = str(ctx.call_llm.call_args[0][0])
        assert "critical error found" in prompt
        assert prompt.index("<context>") < prompt.index("<introduced>")

    def test_default_contexts_attach_without_suppressing_fallback(self, tmp_path: Path) -> None:
        ctx = self._fire_ctx(tmp_path, texts=["transcript evidence"])
        register_llm_nudge("Check", message="WARNING", events=Event.PreToolUse, when=lambda evt: True)

        assert dispatch(Event.PreToolUse, self._edit(ctx, new="x = 2\n"), session_dir=tmp_path) is not None

        prompt = str(ctx.call_llm.call_args[0][0])
        assert "<context>" in prompt
        assert prompt.count("<before_edit>") == 1
        assert prompt.count("<after_edit>") == 1

    def test_user_before_edit_gates_and_replaces_default(self, tmp_path: Path) -> None:
        from captain_hook.contexts import BeforeEdit

        ctx = self._fire_ctx(tmp_path)
        register_llm_nudge("Check", message="WARNING", events=Event.PreToolUse, contexts=[BeforeEdit(required=True)])

        bash = make_pre_tool_event("Bash", {"command": "ls"}, ctx=ctx)
        assert dispatch(Event.PreToolUse, bash, session_dir=tmp_path) is None
        ctx.call_llm.assert_not_called()

        assert dispatch(Event.PreToolUse, self._edit(ctx, new="x = 2\n"), session_dir=tmp_path) is not None
        prompt = str(ctx.call_llm.call_args[0][0])
        assert prompt.count("<before_edit>") == 1


class TestTombstones:
    def _register(self) -> None:
        from captain_hook.primitives.llm import llm_nudge

        llm_nudge(
            "judge the flagged comments",
            message=lambda r: f"Tombstone comment: {r.reasoning}",
            contexts=[TombstoneComments()],
            events=Event.PreToolUse,
            only_if=[Tool("Edit", "Write", "MultiEdit")],
            agent=False,
            transcript=False,
        )

    def _ctx(self, session_dir: Path, *, fire: bool) -> Any:
        from captain_hook.primitives.llm import NudgeVerdict

        return make_ctx(
            session_dir, texts=["turn"], call_llm_return=NudgeVerdict(fire=fire, reasoning="narrates the edit")
        )

    def _edit(self, ctx: Any, *, old: str, new: str, file: str = "src/app.py") -> Any:
        return make_pre_tool_event("Edit", {"file_path": file, "old_string": old, "new_string": new}, ctx=ctx)

    def test_warns_on_confirmed_tombstone(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path, fire=True)
        self._register()
        evt = self._edit(ctx, old="retry(fetch, attempts=3)\n", new="# removed the retry logic\nfetch()\n")
        result = dispatch(Event.PreToolUse, evt, session_dir=tmp_path)
        assert result is not None
        message = result["hookSpecificOutput"]["additionalContext"]
        assert "Tombstone comment" in message
        assert "narrates the edit" in message

    def test_silent_on_false_verdict_for_migration_guidance(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path, fire=False)
        self._register()
        evt = self._edit(
            ctx, old="def fetch(): ...\n", new="# removed in API v2 — use fetch_v2() instead\ndef fetch_v2(): ...\n"
        )
        assert dispatch(Event.PreToolUse, evt, session_dir=tmp_path) is None
        ctx.call_llm.assert_called_once()

    def test_clean_edit_skips_llm(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path, fire=True)
        self._register()
        evt = self._edit(ctx, old="a = 1\n", new="a = 2\n")
        assert dispatch(Event.PreToolUse, evt, session_dir=tmp_path) is None
        ctx.call_llm.assert_not_called()

    def test_preexisting_comment_skips_llm(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path, fire=True)
        self._register()
        evt = self._edit(ctx, old="# removed the retry logic\nx()\n", new="# removed the retry logic\ny()\n")
        assert dispatch(Event.PreToolUse, evt, session_dir=tmp_path) is None
        ctx.call_llm.assert_not_called()

    def test_write_with_comment_already_on_disk_skips_llm(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path, fire=True)
        self._register()
        path = tmp_path / "mod.py"
        path.write_text("# no longer needed\nx = 1\n")
        evt = make_pre_tool_event("Write", {"file_path": str(path), "content": "# no longer needed\nx = 2\n"}, ctx=ctx)
        assert dispatch(Event.PreToolUse, evt, session_dir=tmp_path) is None
        ctx.call_llm.assert_not_called()

    def test_prompt_contains_suspect_comment(self, tmp_path: Path) -> None:
        ctx = self._ctx(tmp_path, fire=True)
        self._register()
        evt = self._edit(ctx, old="retry(fetch, attempts=3)\n", new="# removed the retry logic\nfetch()\n")
        assert dispatch(Event.PreToolUse, evt, session_dir=tmp_path) is not None
        prompt = str(ctx.call_llm.call_args[0][0])
        assert "<tombstone_comments>" in prompt
        assert "# removed the retry logic" in prompt

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("removed the retry logic", True, id="removed-retry-logic"),
            pytest.param("was previously here", True, id="was-previously-here"),
            pytest.param("used to be handled here", True, id="used-to-be-handled-here"),
            pytest.param("no longer needed", True, id="no-longer-needed"),
            pytest.param("# retry logic has been moved to utils.py", True, id="perfect-passive-moved"),
            pytest.param("# this function has been removed", True, id="perfect-passive-removed"),
            pytest.param("# the fallback path has been deleted", True, id="perfect-passive-deleted"),
            pytest.param("# config moved to settings.py", True, id="elliptical-config-moved"),
            pytest.param("# helpers moved to utils.py", True, id="elliptical-helpers-moved"),
            pytest.param("# validation logic extracted to validators.py", True, id="elliptical-logic-extracted"),
            pytest.param("# retry handling migrated to backoff.py", True, id="elliptical-handling-migrated"),
            pytest.param("# old handler removed", True, id="elliptical-handler-removed"),
            pytest.param("# retry logic removed", True, id="elliptical-logic-removed"),
            pytest.param("# logic migrated to the worker", True, id="elliptical-logic-migrated"),
            pytest.param("remove the node from the queue", False, id="imperative-remove"),
            pytest.param("skips removed entries", False, id="participle-as-modifier"),
            pytest.param("handles removed entries", False, id="participle-as-modifier-handles"),
            pytest.param("the parser removed the node", False, id="nominal-subject-with-object"),
            pytest.param("the node is removed when it expires", False, id="present-passive"),
            pytest.param("will be moved to utils.py later", False, id="modal-future"),
            pytest.param("removal policy", False, id="nominal-removal"),
            pytest.param("waits no longer than 30 seconds", False, id="no-longer-than-guard"),
            pytest.param("# this key is used to sign requests", False, id="passive-purpose-used-to"),
            pytest.param("# use the helper to normalize paths", False, id="imperative-use-to"),
            pytest.param("# we used the cache to avoid refetching", False, id="past-use-with-object"),
            pytest.param("the key used to sign requests", False, id="reduced-relative-used-to"),
        ],
    )
    def test_is_tombstone(self, text: str, expected: bool) -> None:
        assert is_tombstone(text) is expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("# TODO: remove after the June migration", True, id="todo"),
            pytest.param("// FIXME: handle unicode", True, id="fixme"),
            pytest.param("/* TODO(alice): tidy */", True, id="todo-block"),
            pytest.param("# XXX temporary", True, id="xxx"),
            pytest.param("// HACK around the cache", True, id="hack"),
            pytest.param("#!/usr/bin/env python", False, id="shebang"),
            pytest.param("# removed the retry logic", False, id="tombstone-text"),
        ],
    )
    def test_is_marker(self, text: str, expected: bool) -> None:
        assert is_marker(text) is expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param("# removed the retry logic", True, id="tombstone-kept"),
            pytest.param("# TODO: remove after the June migration", False, id="marker-vetoes"),
            pytest.param("# remove the node from the queue", False, id="imperative-dropped"),
        ],
    )
    def test_keep(self, text: str, expected: bool) -> None:
        assert TombstoneComments().keep(text) is expected


class TestMultiContributorConsume:
    """The eager-consume -> revert -> consume_signals dance round-trips across N contributors."""

    def test_round_trip_reconsumes_exactly_the_contributors(self, tmp_path: Path) -> None:
        from captain_hook.primitives.llm import GateVerdict, consume_signals, llm_evaluate
        from captain_hook.state import PrimitiveState, text_hash
        from captain_hook.types import Signals

        sig = Signals(
            patterns=[Signal(pattern=r"list", weight=2), Signal(pattern=r"feedback", weight=2)],
            threshold=4,
            window=10,
        )
        ctx = make_ctx(
            tmp_path,
            texts=["a list here", "some feedback"],
            call_llm_return=GateVerdict(block=True, reasoning="bad"),
        )
        evt = make_post_tool_event(ctx=ctx)

        result = llm_evaluate(evt, "check", GateVerdict, hook="rt", signals=sig)
        assert result is not None  # signals passed -> LLM consulted -> verdict returned
        # the eager consume is reverted: nothing stays consumed until the verdict confirms the fire
        assert (ps := evt.ctx.s[PrimitiveState].get()) is not None and ps.consumed == {}

        consume_signals(evt, sig, "rt")
        final = evt.ctx.s[PrimitiveState].get()
        assert final is not None
        assert final.consumed == {"rt": {text_hash("a list here"), text_hash("some feedback")}}
