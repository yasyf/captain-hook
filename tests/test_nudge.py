from __future__ import annotations

import pytest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from captain_hook.app import (
    _state,
    hook as register_hook,
    on,
    register,
    reset,
)
from captain_hook.dispatch import dispatch
from captain_hook.events import (
    PostToolUseEvent,
    PreToolUseEvent,
    StopEvent,
    SubagentStopEvent,
)
from captain_hook.session import SessionStore
from captain_hook.types import Event, Tool


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
    tool_response: str | None = None,
    ctx: Any = None,
) -> PostToolUseEvent:
    raw: dict[str, Any] = {"tool_name": tool_name}
    if tool_input is not None:
        raw["tool_input"] = tool_input
    if tool_response is not None:
        raw["tool_response"] = tool_response
    return PostToolUseEvent(_raw=raw, ctx=ctx or make_ctx())


def make_stop_event(ctx: Any = None) -> StopEvent:
    return StopEvent(_raw={}, ctx=ctx or make_ctx())


def make_subagent_stop_event(ctx: Any = None) -> SubagentStopEvent:
    return SubagentStopEvent(_raw={}, ctx=ctx or make_ctx())


def register_nudge(
        message: str,
    *,
    when: Any = None,
    signals: Any = None,
    only_if: Any = (),
    skip_if: Any = (),
    block: bool = False,
    events: Event | None = None,
    max_fires: int | None = None,
    tests: Any = None,
    async_: bool = False,
) -> None:
    from captain_hook.primitives.nudge import nudge

    nudge(
        message,
        when=when,
        signals=signals,
        only_if=only_if,
        skip_if=skip_if,
        block=block,
        events=events,
        max_fires=max_fires,
        tests=tests,
        async_=async_,
    )
def register_gate(
        message: str,
    **kwargs: Any,
) -> None:
    from captain_hook.primitives.nudge import gate

    gate(message, **kwargs)
# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NUDGE-001 — nudge() with when predicate fires warning when predicate True
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _clean_state():
    reset()
    yield
    reset()



class TestNudgeWhenTrue:
    def test_nudge_when_true_produces_warn(self, tmp_path: Path) -> None:
        register_nudge("Watch out!", when=lambda evt: True)

        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event(ctx=ctx)
        result = dispatch(Event.PreToolUse, evt, session_dir=tmp_path)

        assert result is not None
        assert result["hookSpecificOutput"]["additionalContext"] == "Watch out!"
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NUDGE-002 — nudge() with when predicate skips when predicate is False
# ═══════════════════════════════════════════════════════════════════════════════


class TestNudgeWhenFalse:
    def test_nudge_when_false_returns_none(self, tmp_path: Path) -> None:
        register_nudge("Watch out!", when=lambda evt: False)

        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event(ctx=ctx)
        result = dispatch(Event.PreToolUse, evt, session_dir=tmp_path)

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NUDGE-003 — nudge() with signals fires when score meets threshold
# ═══════════════════════════════════════════════════════════════════════════════


class TestNudgeSignalsFire:
    def test_nudge_with_matching_signals_fires(self, tmp_path: Path) -> None:
        from captain_hook.types import Signal

        register_nudge(
            "Detected risky pattern",
            signals=[Signal(pattern=r"git\s+push", weight=2)],
        )

        ctx = make_ctx(tmp_path)
        ctx.transcript.recent = MagicMock(
            return_value=MagicMock(
                messages=[MagicMock(text="running git push --force")],
            )
        )
        evt = make_post_tool_event(ctx=ctx)
        result = dispatch(Event.PostToolUse, evt, session_dir=tmp_path)

        assert result is not None
        assert "Detected risky pattern" in result["hookSpecificOutput"]["additionalContext"]


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NUDGE-004 — nudge() with signals skips when score < threshold
# ═══════════════════════════════════════════════════════════════════════════════


class TestNudgeSignalsSkip:
    def test_nudge_with_below_threshold_signals_skips(self, tmp_path: Path) -> None:
        from captain_hook.types import Signal, Signals

        register_nudge(
            "Detected risky pattern",
            signals=Signals(patterns=[Signal(pattern=r"git\s+push", weight=2)], threshold=3),
        )

        ctx = make_ctx(tmp_path)
        ctx.transcript.recent = MagicMock(
            return_value=MagicMock(
                messages=[MagicMock(text="running git push --force")],
            )
        )
        evt = make_post_tool_event(ctx=ctx)
        result = dispatch(Event.PostToolUse, evt, session_dir=tmp_path)

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NUDGE-005 — gate() produces block action
# ═══════════════════════════════════════════════════════════════════════════════


class TestGateBlock:
    def test_gate_produces_deny(self, tmp_path: Path) -> None:
        register_gate("You must stop!")

        ctx = make_ctx(tmp_path)
        evt = make_stop_event(ctx=ctx)
        result = dispatch(Event.Stop, evt, session_dir=tmp_path)

        assert result is not None
        assert result["decision"] == "block"
        assert result["reason"] == "You must stop!"


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NUDGE-006 — gate() blocks only once per turn
# ═══════════════════════════════════════════════════════════════════════════════


class TestGateOncePerTurn:
    def test_gate_blocks_only_once_per_turn(self, tmp_path: Path) -> None:
        register_gate("Stop here!", max_fires=5)

        ctx = make_ctx(tmp_path, transcript_len=10)
        evt1 = make_stop_event(ctx=ctx)
        result1 = dispatch(Event.Stop, evt1, session_dir=tmp_path)
        assert result1 is not None
        assert result1["decision"] == "block"

        evt2 = make_stop_event(ctx=ctx)
        result2 = dispatch(Event.Stop, evt2, session_dir=tmp_path)
        assert result2 is None


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NUDGE-007 — nudge() default events: PreToolUse without signals,
#                 PostToolUse with signals
# ═══════════════════════════════════════════════════════════════════════════════


class TestNudgeDefaultEvents:
    def test_nudge_without_signals_default_pretooluse(self) -> None:

        register_nudge("no signals nudge")
        assert _state.hooks[-1].spec.events == Event.PreToolUse

    def test_nudge_with_signals_default_posttooluse(self) -> None:
        from captain_hook.types import Signal

        register_nudge("signals nudge", signals=[Signal(pattern=r"test", weight=1)])
        assert _state.hooks[-1].spec.events == Event.PostToolUse


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NUDGE-008 — gate() default events: Stop | SubagentStop
# ═══════════════════════════════════════════════════════════════════════════════


class TestGateDefaultEvents:
    def test_gate_default_stop_events(self) -> None:
        register_gate("gate message")
        assert _state.hooks[-1].spec.events == (Event.Stop | Event.SubagentStop)


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NUDGE-009 — nudge() message is dedented and stripped
# ═══════════════════════════════════════════════════════════════════════════════


class TestNudgeMessageDedent:
    def test_nudge_dedents_and_strips_message(self, tmp_path: Path) -> None:
        register_nudge(
            """
            This is a multi-line
            indented message
        """,
            when=lambda evt: True,
        )

        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event(ctx=ctx)
        result = dispatch(Event.PreToolUse, evt, session_dir=tmp_path)

        assert result is not None
        msg = result["hookSpecificOutput"]["additionalContext"]
        assert not msg.startswith("\n")
        assert not msg.startswith(" ")
        assert msg == "This is a multi-line\nindented message"


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NUDGE-010 — nudge() with only_if / skip_if conditions
# ═══════════════════════════════════════════════════════════════════════════════


class TestNudgeConditions:
    def test_nudge_only_if_matches(self, tmp_path: Path) -> None:
        register_nudge(
            "Edit warning",
            only_if=[Tool("Edit")],
            when=lambda evt: True,
            events=Event.PreToolUse,
        )

        ctx = make_ctx(tmp_path)
        evt_match = PreToolUseEvent(
            _raw={"tool_name": "Edit", "tool_input": {"file_path": "a.py", "old_string": "x", "new_string": "y"}},
            ctx=ctx,
        )
        result = dispatch(Event.PreToolUse, evt_match, session_dir=tmp_path)
        assert result is not None

    def test_nudge_only_if_no_match(self, tmp_path: Path) -> None:
        register_nudge(
            "Edit warning",
            only_if=[Tool("Edit")],
            when=lambda evt: True,
            events=Event.PreToolUse,
        )

        ctx = make_ctx(tmp_path)
        evt_no_match = make_pre_tool_event(tool_name="Bash", ctx=ctx)
        result = dispatch(Event.PreToolUse, evt_no_match, session_dir=tmp_path)
        assert result is None

    def test_nudge_skip_if_skips(self, tmp_path: Path) -> None:
        register_nudge(
            "Edit warning",
            skip_if=[Tool("Bash")],
            when=lambda evt: True,
            events=Event.PreToolUse,
        )

        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event(tool_name="Bash", ctx=ctx)
        result = dispatch(Event.PreToolUse, evt, session_dir=tmp_path)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NUDGE-011 — nudge() max_fires default: 1 without signals, 3 with signals
# ═══════════════════════════════════════════════════════════════════════════════


class TestNudgeMaxFiresDefault:
    def test_nudge_without_signals_max_fires_1(self) -> None:
        register_nudge("plain nudge")
        assert _state.hooks[-1].spec.max_fires == 1

    def test_nudge_with_signals_max_fires_3(self) -> None:
        from captain_hook.types import Signal

        register_nudge("signal nudge", signals=[Signal(pattern=r"test", weight=1)])
        assert _state.hooks[-1].spec.max_fires == 3


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NUDGE-012 — Signal-triggered nudge cites triggering context
# ═══════════════════════════════════════════════════════════════════════════════


class TestSignalCitation:
    def test_signal_triggered_nudge_cites_context(self, tmp_path: Path) -> None:
        from captain_hook.types import Signal

        register_nudge(
            "Watch out for force push",
            signals=[Signal(pattern=r"force.push", weight=1)],
        )

        ctx = make_ctx(tmp_path)
        ctx.transcript.recent = MagicMock(
            return_value=MagicMock(
                messages=[MagicMock(text="about to force push to main branch")],
            )
        )
        evt = make_post_tool_event(ctx=ctx)
        result = dispatch(Event.PostToolUse, evt, session_dir=tmp_path)

        assert result is not None
        msg = result["hookSpecificOutput"]["additionalContext"]
        assert "Triggered by:" in msg
        assert "force push" in msg


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NUDGE-013 — nudge() with events parameter overrides defaults
# ═══════════════════════════════════════════════════════════════════════════════


class TestNudgeEventsOverride:
    def test_nudge_custom_events(self) -> None:
        register_nudge("custom events", events=Event.UserPromptSubmit)
        assert _state.hooks[-1].spec.events == Event.UserPromptSubmit


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NUDGE-014 — nudge() with both when AND signals ignores when
# ═══════════════════════════════════════════════════════════════════════════════


class TestNudgeSignalsPrecedence:
    def test_nudge_with_both_when_and_signals_ignores_when(self, tmp_path: Path) -> None:
        from captain_hook.types import Signal

        register_nudge(
            "msg",
            when=lambda evt: 1 / 0,
            signals=[Signal(pattern=r"no_match_xyz", weight=1)],
        )

        ctx = make_ctx(tmp_path)
        ctx.transcript.recent = MagicMock(
            return_value=MagicMock(
                messages=[MagicMock(text="safe text without match")],
            )
        )
        evt = make_post_tool_event(ctx=ctx)

        result = dispatch(Event.PostToolUse, evt, session_dir=tmp_path)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-NUDGE-015 — nudge() with when=None and signals=None fires unconditionally
# ═══════════════════════════════════════════════════════════════════════════════


class TestNudgeUnconditional:
    def test_nudge_bare_fires_unconditionally(self, tmp_path: Path) -> None:
        register_nudge("Always fires")

        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event(ctx=ctx)
        result = dispatch(Event.PreToolUse, evt, session_dir=tmp_path)

        assert result is not None
        assert result["hookSpecificOutput"]["additionalContext"] == "Always fires"
