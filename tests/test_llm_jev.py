from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import MagicMock

import pytest
import spawnllm
from pydantic import BaseModel, Field
from spawnllm import (
    JEV,
    Binary,
    BinaryAnswer,
    DecideError,
    DecideKeyMissing,
    Decision,
    Label,
    LabelAnswer,
    Refused,
    Score,
    ScoreAnswer,
)

from captain_hook import faults
from captain_hook.dispatch import dispatch
from captain_hook.grants.judge import GrantVerdict
from captain_hook.primitives.llm import (
    GateVerdict,
    IntAnswer,
    NudgeVerdict,
    PromptCheckVerdict,
    llm_gate,
    llm_nudge,
    verdict_from,
    verdict_questions,
)
from captain_hook.testing.helpers import fixture_session
from captain_hook.types import Event
from tests.helpers import build_ctx, make_post_tool_event, make_stop_event, raw_text

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from spawnllm.decide import Answer, Question

    from captain_hook.context import HookContext


class Risk(BaseModel):
    risk: int = Field(ge=1, le=5)
    reasoning: str


class Triage(BaseModel):
    urgent: bool
    kind: Literal["none", "bug", "feature"]


class Aliased(BaseModel):
    block: bool = Field(alias="shouldBlock")
    reasoning: str


class ShortReason(BaseModel):
    block: bool
    reasoning: str = Field(max_length=10)


class OneKind(BaseModel):
    kind: Literal["ok"]


class EvenRisk(BaseModel):
    risk: int = Field(ge=1, le=5, multiple_of=2)


def decided(answers: Mapping[str, Answer]) -> Decision:
    return Decision(dict(answers), "jev-1.13.0", 120, 95.0)


def jev_calls(monkeypatch: pytest.MonkeyPatch, outcome: Decision | BaseException) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake(state: str, questions: Mapping[str, Question], **kwargs: Any) -> Decision:
        calls.append({"state": state, "questions": dict(questions)} | kwargs)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(spawnllm, "decide_sync", fake)
    return calls


def jev_ctx(tmp_path: Path, text: str = "the agent blamed the CI provider") -> HookContext:
    ctx = build_ctx(transcript=fixture_session([raw_text("assistant", text)]), session_dir=tmp_path)
    ctx.call_llm = MagicMock(side_effect=AssertionError("the LLM must not judge a Jev verdict"))  # type: ignore[method-assign]
    return ctx


def test_a_bool_field_asks_a_binary_named_by_the_field() -> None:
    assert verdict_questions("Is the agent making excuses?", GateVerdict) == {
        "block": Binary("Is the agent making excuses?", yes="block=true", no="block=false")
    }


def test_a_literal_field_asks_a_label_in_declared_order() -> None:
    match verdict_questions("Rate the command.", PromptCheckVerdict):
        case {"action": Label(instructions=instructions, options=options)}:
            assert instructions.startswith("Rate the command.")
            assert list(options) == ["ok", "warning", "block"]
        case other:
            pytest.fail(f"expected one action label, got {other}")


def test_a_bounded_int_field_asks_a_score_lowest_first() -> None:
    match verdict_questions("How risky?", Risk):
        case {"risk": Score(levels=levels)}:
            assert list(levels) == ["1", "2", "3", "4", "5"]
        case other:
            pytest.fail(f"expected one risk score, got {other}")


def test_several_categorical_fields_ask_one_question_each() -> None:
    assert set(verdict_questions("Triage it.", Triage) or {}) == {"urgent", "kind"}


@pytest.mark.parametrize(
    "model",
    [
        pytest.param(None, id="free-text-reply"),
        pytest.param(IntAnswer, id="unbounded-int"),
        pytest.param(GrantVerdict, id="citations-and-scope"),
        pytest.param(ShortReason, id="constrained-text"),
        pytest.param(OneKind, id="single-option-label"),
        pytest.param(EvenRisk, id="constrained-score"),
    ],
)
def test_a_model_jev_cannot_answer_asks_nothing(model: type[BaseModel] | None) -> None:
    assert verdict_questions("Judge it.", model) is None


@pytest.mark.parametrize(
    ("p_yes", "block"),
    [
        pytest.param(0.93, True, id="likely"),
        pytest.param(0.5, False, id="even-odds"),
        pytest.param(0.1, False, id="unlikely"),
    ],
)
def test_a_binary_is_true_only_above_even_odds(p_yes: float, block: bool) -> None:
    questions = verdict_questions("Block?", GateVerdict) or {}
    verdict = verdict_from(GateVerdict, questions, decided({"block": BinaryAnswer(p_yes, abs(2 * p_yes - 1))}))
    assert verdict == GateVerdict(block=block, reasoning=f"jev-1.13.0 decided block={block}")


def test_a_label_takes_its_choice_and_a_score_rounds_to_a_level() -> None:
    label = verdict_questions("Rate the command.", PromptCheckVerdict) or {}
    chosen = LabelAnswer("warning", {"ok": 0.2, "warning": 0.7, "block": 0.1}, 0.55)
    assert verdict_from(PromptCheckVerdict, label, decided({"action": chosen})) == PromptCheckVerdict(
        action="warning", reason="jev-1.13.0 decided action=warning"
    )
    score = verdict_questions("How risky?", Risk) or {}
    rated = ScoreAnswer(2.6, dict.fromkeys(["1", "2", "3", "4", "5"], 0.2), 0.4)
    assert verdict_from(Risk, score, decided({"risk": rated})) == Risk(risk=4, reasoning="jev-1.13.0 decided risk=4")


def test_an_aliased_field_takes_its_answer_by_name() -> None:
    questions = verdict_questions("Block?", Aliased) or {}
    verdict = verdict_from(Aliased, questions, decided({"block": BinaryAnswer(0.99, 0.98)}))
    assert verdict is not None
    assert verdict.block


def test_a_refused_question_yields_no_verdict() -> None:
    questions = verdict_questions("Triage it.", Triage) or {}
    answers = {"urgent": BinaryAnswer(0.9, 0.8), "kind": Refused()}
    assert verdict_from(Triage, questions, decided(answers)) is None


def test_a_gate_asks_jev_with_its_evidence_as_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = jev_calls(monkeypatch, decided({"block": BinaryAnswer(0.97, 0.94)}))
    llm_gate("Is the agent blaming an external service?", message="Fix it yourself.", when=lambda evt: True)

    result = dispatch(Event.Stop, make_stop_event(ctx=jev_ctx(tmp_path)), session_dir=tmp_path)

    assert result is not None
    assert (result["decision"], result["reason"]) == ("block", "Fix it yourself.")
    (call,) = calls
    assert call["provider"] == JEV
    assert call["questions"] == {
        "block": Binary("Is the agent blaming an external service?", yes="block=true", no="block=false")
    }
    assert "<context>\nthe agent blamed the CI provider\n</context>" in call["state"]
    assert "<transcript>" in call["state"]
    assert "Is the agent blaming" not in call["state"]


def test_a_gate_allows_when_jev_says_no(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jev_calls(monkeypatch, decided({"block": BinaryAnswer(0.08, 0.84)}))
    llm_gate("Is the agent blaming an external service?", message="Fix it yourself.", when=lambda evt: True)

    assert dispatch(Event.Stop, make_stop_event(ctx=jev_ctx(tmp_path)), session_dir=tmp_path) is None


@pytest.mark.parametrize(
    ("failure", "recorded"),
    [
        pytest.param(TimeoutError("no jev decision within 5s"), False, id="timeout"),
        pytest.param(DecideError(500, "upstream overloaded"), True, id="rejected"),
        pytest.param(DecideKeyMissing("no jev API key"), True, id="missing-key"),
    ],
)
def test_a_jev_failure_skips_a_gate_and_a_nudge_like_a_failed_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException, recorded: bool
) -> None:
    calls = jev_calls(monkeypatch, failure)
    monkeypatch.setattr(faults, "faults_dir", lambda: tmp_path / "faults")
    llm_gate("Block?", message="BLOCKED", when=lambda evt: True, on_incomplete="Judge it by hand.")
    llm_nudge("Warn?", message="WARNED", when=lambda evt: True)

    assert dispatch(Event.Stop, make_stop_event(ctx=jev_ctx(tmp_path)), session_dir=tmp_path) is None
    assert dispatch(Event.PostToolUse, make_post_tool_event(ctx=jev_ctx(tmp_path)), session_dir=tmp_path) is None
    assert [set(call["questions"]) for call in calls] == [{"block"}, {"fire"}]
    assert bool(drained := faults.drain(None)) is recorded
    assert not any("upstream overloaded" in line for line in drained)


def test_a_refused_question_skips_the_nudge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jev_calls(monkeypatch, decided({"fire": Refused()}))
    llm_nudge("Warn?", message="WARNED", when=lambda evt: True)

    assert dispatch(Event.PostToolUse, make_post_tool_event(ctx=jev_ctx(tmp_path)), session_dir=tmp_path) is None


def test_a_callable_message_reads_the_jev_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jev_calls(monkeypatch, decided({"fire": BinaryAnswer(0.9, 0.8)}))
    llm_nudge("Warn?", message=lambda r: f"why: {r.reasoning}", when=lambda evt: True)

    result = dispatch(Event.PostToolUse, make_post_tool_event(ctx=jev_ctx(tmp_path)), session_dir=tmp_path)

    assert result is not None
    assert result["hookSpecificOutput"]["additionalContext"].endswith("why: jev-1.13.0 decided fire=True")


@pytest.mark.parametrize(
    ("message", "backend"),
    [
        pytest.param("Do not widen: {reasoning}.", "jev", id="reasoning-template"),
        pytest.param("BLOCKED", "llm", id="explicit-opt-out"),
    ],
)
def test_free_text_asks_the_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str, backend: Literal["jev", "llm"]
) -> None:
    calls = jev_calls(monkeypatch, decided({"block": BinaryAnswer(0.9, 0.8)}))
    ctx = jev_ctx(tmp_path)
    ctx.call_llm = MagicMock(return_value=GateVerdict(block=True, reasoning="use a Protocol"))  # type: ignore[method-assign]
    llm_gate("Block?", message=message, when=lambda evt: True, backend=backend)

    result = dispatch(Event.Stop, make_stop_event(ctx=ctx), session_dir=tmp_path)

    assert result is not None
    assert result["decision"] == "block"
    ctx.call_llm.assert_called_once()
    assert calls == []


def test_evt_llm_asks_jev_for_a_bool_and_the_llm_for_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = jev_calls(monkeypatch, decided({"answer": BinaryAnswer(0.88, 0.76)}))
    ctx = jev_ctx(tmp_path)
    ctx.call_llm = MagicMock(return_value="a summary")  # type: ignore[method-assign]
    evt = make_post_tool_event(ctx=ctx)

    assert evt.llm("Is this a debug print?", bool) is True
    assert evt.llm("Summarize the call.") == "a summary"
    assert [set(call["questions"]) for call in calls] == [{"answer"}]
    assert ctx.call_llm.call_args.kwargs["response_model"] is None


def test_evt_llm_raises_when_jev_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    jev_calls(monkeypatch, TimeoutError("no jev decision within 5s"))

    with pytest.raises(TimeoutError):
        make_post_tool_event(ctx=jev_ctx(tmp_path)).llm("Is this a debug print?", bool)


def test_inline_tests_stub_a_jev_verdict_from_the_llm_stub() -> None:
    from captain_hook.testing.helpers import StubbedContext

    ctx = StubbedContext.wrapping(build_ctx(), llm={"fire": False})

    assert ctx.decide_verdict("Warn?", "state", NudgeVerdict, root=None) == NudgeVerdict(
        fire=False, reasoning="inline test stub"
    )
