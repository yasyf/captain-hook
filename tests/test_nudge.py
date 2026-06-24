from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from captain_hook.app import _state
from captain_hook.dispatch import dispatch
from captain_hook.tests.helpers import (
    build_ctx,
    make_ctx,
    make_post_tool_event,
    make_pre_tool_event,
    make_stop_event,
    make_transcript,
    workflow_launch,
)
from captain_hook.types import Event, Signal, Signals, Tool, Waiting


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


class TestNudgeWhenTrue:
    def test_nudge_when_true_produces_warn(self, tmp_path: Path) -> None:
        register_nudge("Watch out!", when=lambda evt: True)

        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event(ctx=ctx)
        result = dispatch(Event.PreToolUse, evt, session_dir=tmp_path)

        assert result is not None
        assert result["hookSpecificOutput"]["additionalContext"] == "Watch out!"
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


class TestNudgeWhenFalse:
    def test_nudge_when_false_returns_none(self, tmp_path: Path) -> None:
        register_nudge("Watch out!", when=lambda evt: False)

        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event(ctx=ctx)
        result = dispatch(Event.PreToolUse, evt, session_dir=tmp_path)

        assert result is None


class TestNudgeSignalsFire:
    def test_nudge_with_matching_signals_fires(self, tmp_path: Path) -> None:
        register_nudge(
            "Detected risky pattern",
            signals=[Signal(pattern=r"git\s+push", weight=2)],
        )

        ctx = make_ctx(tmp_path, texts=["running git push --force"])
        evt = make_post_tool_event(ctx=ctx)
        result = dispatch(Event.PostToolUse, evt, session_dir=tmp_path)

        assert result is not None
        assert "Detected risky pattern" in result["hookSpecificOutput"]["additionalContext"]


class TestNudgeSignalsSkip:
    def test_nudge_with_below_threshold_signals_skips(self, tmp_path: Path) -> None:
        register_nudge(
            "Detected risky pattern",
            signals=Signals(patterns=[Signal(pattern=r"git\s+push", weight=2)], threshold=3),
        )

        ctx = make_ctx(tmp_path, texts=["running git push --force"])
        evt = make_post_tool_event(ctx=ctx)
        result = dispatch(Event.PostToolUse, evt, session_dir=tmp_path)

        assert result is None


class TestGateBlock:
    def test_gate_produces_deny(self, tmp_path: Path) -> None:
        register_gate("You must stop!")

        ctx = make_ctx(tmp_path)
        evt = make_stop_event(ctx=ctx)
        result = dispatch(Event.Stop, evt, session_dir=tmp_path)

        assert result is not None
        assert result["decision"] == "block"
        assert result["reason"] == "You must stop!"


class TestGateOncePerTurn:
    def test_gate_blocks_only_once_per_turn(self, tmp_path: Path) -> None:
        register_gate("Stop here!", max_fires=5)

        ctx = make_ctx(tmp_path, n_messages=10)
        evt1 = make_stop_event(ctx=ctx)
        result1 = dispatch(Event.Stop, evt1, session_dir=tmp_path)
        assert result1 is not None
        assert result1["decision"] == "block"

        evt2 = make_stop_event(ctx=ctx)
        result2 = dispatch(Event.Stop, evt2, session_dir=tmp_path)
        assert result2 is None


#                 PostToolUse with signals


class TestNudgeDefaultEvents:
    @pytest.mark.parametrize(
        ("signals", "events", "expected"),
        [
            pytest.param(None, None, Event.PreToolUse, id="nudge_without_signals_default_pretooluse"),
            pytest.param(
                [Signal(pattern=r"test", weight=1)],
                None,
                Event.PostToolUse,
                id="nudge_with_signals_default_posttooluse",
            ),
            pytest.param(None, Event.UserPromptSubmit, Event.UserPromptSubmit, id="nudge_custom_events"),
        ],
    )
    def test_nudge_events(self, signals: Any, events: Event | None, expected: Event) -> None:
        register_nudge("nudge", signals=signals, events=events)
        assert _state.hooks[-1].spec.events == expected


class TestGateDefaultEvents:
    def test_gate_default_stop_events(self) -> None:
        register_gate("gate message")
        assert _state.hooks[-1].spec.events == (Event.Stop | Event.SubagentStop)


class TestGateWaitAwareDefault:
    @pytest.mark.parametrize(
        ("skip_if", "expected"),
        [
            pytest.param((), (Waiting(),), id="stop_gate_without_skip_if_gets_waiting"),
            pytest.param([Tool("Bash")], (Tool("Bash"),), id="stop_gate_with_skip_if_is_left_untouched"),
        ],
    )
    def test_gate_skip_if(self, skip_if: Any, expected: tuple[Any, ...]) -> None:
        register_gate("gate message", skip_if=skip_if)
        assert _state.hooks[-1].spec.skip_if == expected

    @pytest.mark.parametrize(
        ("kwargs"),
        [
            pytest.param({"block": True, "events": Event.PreToolUse}, id="pretooluse_block_gate_is_not_wait_aware"),
            pytest.param({}, id="plain_warn_nudge_is_not_wait_aware"),
        ],
    )
    def test_nudge_not_wait_aware(self, kwargs: dict[str, Any]) -> None:
        register_nudge("nudge", **kwargs)
        assert _state.hooks[-1].spec.skip_if == ()

    def test_stop_gate_skips_while_waiting(self, tmp_path: Path) -> None:
        register_gate("You must stop!")

        ctx = build_ctx(transcript=make_transcript(workflow_launch(id="toolu_wf")), session_dir=tmp_path)
        evt = make_stop_event(ctx=ctx)
        result = dispatch(Event.Stop, evt, session_dir=tmp_path)

        assert result is None


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


class TestNudgeConditions:
    def test_nudge_only_if_matches(self, tmp_path: Path) -> None:
        register_nudge(
            "Edit warning",
            only_if=[Tool("Edit")],
            when=lambda evt: True,
            events=Event.PreToolUse,
        )

        ctx = make_ctx(tmp_path)
        evt_match = make_pre_tool_event("Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"}, ctx=ctx)
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


class TestNudgeMaxFiresDefault:
    @pytest.mark.parametrize(
        ("signals", "expected"),
        [
            pytest.param(None, 1, id="nudge_without_signals_max_fires_1"),
            pytest.param([Signal(pattern=r"test", weight=1)], 3, id="nudge_with_signals_max_fires_3"),
        ],
    )
    def test_nudge_max_fires(self, signals: Any, expected: int) -> None:
        register_nudge("nudge", signals=signals)
        assert _state.hooks[-1].spec.max_fires == expected


class TestSignalCitation:
    def test_signal_triggered_nudge_cites_context(self, tmp_path: Path) -> None:
        register_nudge(
            "Watch out for force push",
            signals=[Signal(pattern=r"force.push", weight=1)],
        )

        ctx = make_ctx(tmp_path, texts=["about to force push to main branch"])
        evt = make_post_tool_event(ctx=ctx)
        result = dispatch(Event.PostToolUse, evt, session_dir=tmp_path)

        assert result is not None
        msg = result["hookSpecificOutput"]["additionalContext"]
        assert "Triggered by:" in msg
        assert "force push" in msg


class TestNudgeSignalsPrecedence:
    def test_nudge_with_both_when_and_signals_ignores_when(self, tmp_path: Path) -> None:
        register_nudge(
            "msg",
            when=lambda evt: 1 / 0,
            signals=[Signal(pattern=r"no_match_xyz", weight=1)],
        )

        ctx = make_ctx(tmp_path, texts=["safe text without match"])
        evt = make_post_tool_event(ctx=ctx)

        result = dispatch(Event.PostToolUse, evt, session_dir=tmp_path)
        assert result is None


class TestNudgeUnconditional:
    def test_nudge_bare_fires_unconditionally(self, tmp_path: Path) -> None:
        register_nudge("Always fires")

        ctx = make_ctx(tmp_path)
        evt = make_pre_tool_event(ctx=ctx)
        result = dispatch(Event.PreToolUse, evt, session_dir=tmp_path)

        assert result is not None
        assert result["hookSpecificOutput"]["additionalContext"] == "Always fires"
