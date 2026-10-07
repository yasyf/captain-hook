from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from filelock import FileLock
from spawnllm import JEV, BinaryAnswer, DecideError, Decision, Refused

from captain_hook import Annotated, Confirm, Event, hook
from captain_hook.app import _state
from captain_hook.confirm import PROTECTED, ConfirmDecisions, ConfirmNotes
from captain_hook.dispatch import OFFLOAD_THREADS, execute_hook
from captain_hook.events import BaseHookEvent, PreToolUseEvent
from captain_hook.types import Action, HookResult, HookSpec, RegisteredHook
from tests.helpers import make_ctx

RULE = "A push that lands on a branch the merge queue already admitted."
MESSAGE = "The merge queue holds `feat` and drops anything pushed after admission. Ship a stacked PR instead."


def entry(confirm: Confirm, **spec: Any) -> RegisteredHook:
    return RegisteredHook(
        spec=HookSpec(events=Event.PreToolUse, message=MESSAGE, block=True, confirm=confirm, **spec), name="queued_push"
    )


def push(ctx: Any, command: str = "git push origin feat") -> PreToolUseEvent:
    return PreToolUseEvent(_raw={"tool_name": "Bash", "tool_input": {"command": command}}, ctx=ctx)


def answering(tmp_path: Path, answer: Any) -> Any:
    ctx = make_ctx(tmp_path)
    ctx.decide = MagicMock(return_value=Decision({"protected": answer}, "jev-1.13.0", 0, 0.0))  # type: ignore[method-assign]
    return ctx


def protected(p_yes: float) -> BinaryAnswer:
    return BinaryAnswer(p_yes=p_yes, confidence=abs(2 * p_yes - 1))


MATCH = protected(0.9)
NO_MATCH = protected(0.05)


def test_a_likely_match_blocks(tmp_path: Path) -> None:
    result = execute_hook(entry(Confirm(rule=RULE)), push(answering(tmp_path, MATCH)))
    assert result == HookResult(action=Action.block, message=MESSAGE)


@pytest.mark.parametrize("answer", [NO_MATCH, protected(PROTECTED - 0.01), Refused()])
def test_anything_short_of_a_likely_match_allows_silently(tmp_path: Path, answer: Any) -> None:
    assert execute_hook(entry(Confirm(rule=RULE)), push(answering(tmp_path, answer))) is None


def test_the_framework_stops_waiting_at_the_timeout(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    ctx.decide = MagicMock(side_effect=lambda *_, **__: time.sleep(2))  # type: ignore[method-assign]
    started = time.monotonic()
    result = execute_hook(entry(Confirm(rule=RULE, timeout_s=0.2)), push(ctx))
    assert time.monotonic() - started < 1.0
    assert result == HookResult(
        action=Action.warn, message="queued_push: allowed, could not confirm in 0.2 s", approve=False
    )


def test_a_failed_call_allows_with_a_note(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    ctx.decide = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    result = execute_hook(entry(Confirm(rule=RULE)), push(ctx))
    assert result == HookResult(
        action=Action.warn, message="queued_push: allowed, the confirm step failed (RuntimeError)", approve=False
    )


def test_a_rejected_request_allows_with_a_note(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    ctx.decide = MagicMock(side_effect=DecideError(401, "bad key"))  # type: ignore[method-assign]
    result = execute_hook(entry(Confirm(rule=RULE)), push(ctx))
    assert result == HookResult(
        action=Action.warn, message="queued_push: allowed, the confirm step got no answer from Jev", approve=False
    )


def test_a_failure_note_lands_once_per_session_and_hook(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    ctx.decide = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    guard = entry(Confirm(rule=RULE))
    assert execute_hook(guard, push(ctx, "git push origin feat")) is not None
    assert execute_hook(guard, push(ctx, "git push origin main")) is None
    other = RegisteredHook(spec=guard.spec, name="other_push")
    assert execute_hook(other, push(ctx)) == HookResult(
        action=Action.warn, message="other_push: allowed, the confirm step failed (RuntimeError)", approve=False
    )
    ctx.decide.side_effect = TimeoutError()
    assert execute_hook(guard, push(ctx)) == HookResult(
        action=Action.warn, message="queued_push: allowed, the confirm step got no answer from Jev", approve=False
    )
    assert ctx.decide.call_count == 4


def test_a_busy_note_ledger_still_surfaces_the_note(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    ctx.decide = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    with FileLock(f"{ctx.session[ConfirmNotes].path}.lock"):
        result = execute_hook(entry(Confirm(rule=RULE)), push(ctx))
    assert result == HookResult(
        action=Action.warn, message="queued_push: allowed, the confirm step failed (RuntimeError)", approve=False
    )


def test_a_retried_identical_call_reuses_the_verdict(tmp_path: Path) -> None:
    ctx = answering(tmp_path, MATCH)
    guard = entry(Confirm(rule=RULE))
    assert execute_hook(guard, push(ctx, "git push origin feat")).action is Action.block
    assert execute_hook(guard, push(ctx, "git push origin feat")).action is Action.block
    assert ctx.decide.call_count == 1


def test_inputs_that_differ_only_in_whitespace_pay_separately(tmp_path: Path) -> None:
    ctx = answering(tmp_path, NO_MATCH)
    guard = entry(Confirm(rule=RULE))
    execute_hook(guard, push(ctx, "printf allowed # printf blocked"))
    execute_hook(guard, push(ctx, "printf allowed #\nprintf blocked"))
    assert ctx.decide.call_count == 2


def test_stuck_calls_never_starve_a_later_confirm(tmp_path: Path) -> None:
    release = threading.Event()
    stuck = make_ctx(tmp_path / "stuck")
    stuck.decide = MagicMock(side_effect=lambda *_, **__: release.wait(5))  # type: ignore[method-assign]
    try:
        for index in range(OFFLOAD_THREADS + 1):
            execute_hook(entry(Confirm(rule=RULE, timeout_s=0.03)), push(stuck, f"git push origin stuck-{index}"))
        result = execute_hook(entry(Confirm(rule=RULE, timeout_s=1.0)), push(answering(tmp_path / "fast", MATCH)))
    finally:
        release.set()
    assert result == HookResult(action=Action.block, message=MESSAGE)


def test_a_busy_verdict_cache_never_outlasts_the_timeout(tmp_path: Path) -> None:
    ctx = answering(tmp_path, MATCH)
    lock = FileLock(f"{ctx.session[ConfirmDecisions].path}.lock")
    tmp_path.mkdir(exist_ok=True)
    with lock:
        started = time.monotonic()
        result = execute_hook(entry(Confirm(rule=RULE, timeout_s=0.05)), push(ctx))
        elapsed = time.monotonic() - started
    assert elapsed < 0.5
    assert result == HookResult(action=Action.block, message=MESSAGE)


def test_jev_sees_the_rule_message_and_input(tmp_path: Path) -> None:
    ctx = answering(tmp_path, MATCH)
    execute_hook(entry(Confirm(rule=RULE)), push(ctx))
    state, questions = ctx.decide.call_args.args
    assert state["rule"] == RULE
    assert "drops anything pushed after admission" in state["hook_message"]
    assert "git push origin feat" in state["tool_input"]
    assert list(questions) == ["protected"]
    assert ctx.decide.call_args.kwargs["provider"] == JEV


def test_a_settling_annotation_skips_the_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from captain_hook.conditions import matches_conditions

    monkeypatch.delenv("CAPT_HOOK_CCX_RAW", raising=False)
    ctx = answering(tmp_path, MATCH)
    guard = entry(Confirm(rule=RULE), skip_if=(Annotated("raw"),))
    assert not matches_conditions(guard.spec, push(ctx, "git push origin feat # ccx:raw"))
    assert matches_conditions(guard.spec, push(ctx, "git push origin feat"))
    ctx.decide.assert_not_called()


def test_a_handler_block_asks_for_confirmation(tmp_path: Path) -> None:
    def handler(evt: BaseHookEvent) -> HookResult:
        return evt.block(MESSAGE, confirm=Confirm(rule=RULE))

    guard = RegisteredHook(spec=HookSpec(events=Event.PreToolUse), handler=handler, name="queued_push")
    assert execute_hook(guard, push(answering(tmp_path, NO_MATCH))) is None


@pytest.mark.parametrize(
    ("events", "block"),
    [(Event.PreToolUse, False), (Event.Stop, True)],
)
def test_confirm_needs_a_block_on_a_tool_event(events: Event, block: bool) -> None:
    with pytest.raises(ValueError, match="confirm"):
        hook(events, MESSAGE, block=block, confirm=Confirm(rule=RULE))
    assert not _state.hooks
