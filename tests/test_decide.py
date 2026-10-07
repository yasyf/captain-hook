from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
import spawnllm
from spawnllm import JEV, OPENAI, Binary, BinaryAnswer, DecideError, DecideKeyMissing, Decision, Label, LabelAnswer

from captain_hook import actor, faults
from captain_hook.context import DECIDE_MARGIN_SECONDS, HookContext
from captain_hook.session import SessionStore
from captain_hook.testing.helpers import input_to_event, isolated_state_root
from captain_hook.testing.types import Input
from captain_hook.types import Event
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from spawnllm.decide import Question

    from captain_hook.events import BaseHookEvent

QUESTIONS = {"rollback": Binary("Does the user ask to roll back a release?")}
ANSWERED = Decision({"rollback": BinaryAnswer(p_yes=0.97, confidence=0.94)}, "jev-1.13.0", 300, 110.0)
KEY = "ts-synthetic-decide-key-5f1a"


@pytest.fixture(autouse=True)
def clean_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in actor.CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(actor, "ACTOR", None)


def provider_calls(monkeypatch: pytest.MonkeyPatch, outcome: Decision | BaseException) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake(state: str, questions: Mapping[str, Question], **kwargs: Any) -> Decision:
        calls.append({"state": state, "questions": questions} | kwargs)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(spawnllm, "decide_sync", fake)
    return calls


def context() -> HookContext:
    return HookContext(session=SessionStore(None), transcript=MagicMock(), settings=None)


def request(deadline_in: float | None) -> reqenv.RequestOverrides:
    return reqenv.RequestOverrides(
        env={},
        cwd="/w",
        client_ppid=1,
        session_id="s",
        deadline_unix_ms=0 if deadline_in is None else int((time.time() + deadline_in) * 1000),
    )


def test_a_cold_call_keeps_the_callers_timeout_and_lets_spawnllm_find_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = provider_calls(monkeypatch, ANSWERED)

    assert context().decide("roll it back", QUESTIONS, provider=JEV, timeout=3.0) == ANSWERED
    assert calls == [
        {"state": "roll it back", "questions": QUESTIONS, "provider": JEV, "timeout": 3.0, "api_key": None}
    ]


def test_the_request_deadline_cuts_the_timeout_short(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = provider_calls(monkeypatch, ANSWERED)

    with reqenv.use_request(request(deadline_in=1.5)):
        context().decide("x", QUESTIONS, provider=JEV, timeout=3.0)

    assert 0 < calls[0]["timeout"] <= 1.5 - DECIDE_MARGIN_SECONDS


def test_a_spent_deadline_raises_without_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = provider_calls(monkeypatch, ANSWERED)

    with reqenv.use_request(request(deadline_in=0.2)), pytest.raises(TimeoutError):
        context().decide("x", QUESTIONS, provider=JEV, timeout=3.0)
    assert calls == []


@pytest.mark.parametrize(
    ("provider", "env", "key"),
    [
        pytest.param(JEV, {"OPENAI_API_KEY": "sk-agent", "TYPESAFE_API_KEY": KEY}, KEY, id="jev-reads-typesafe"),
        pytest.param(
            OPENAI, {"OPENAI_API_KEY": "sk-agent", "TYPESAFE_API_KEY": KEY}, "sk-agent", id="openai-reads-codex"
        ),
        pytest.param(JEV, {"OPENAI_API_KEY": "sk-agent"}, None, id="jev-without-its-key"),
    ],
)
def test_an_api_actor_passes_the_key_it_captured(
    monkeypatch: pytest.MonkeyPatch, provider: spawnllm.Provider, env: dict[str, str], key: str | None
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    actor.capture("codex")
    calls = provider_calls(monkeypatch, ANSWERED)

    context().decide("x", QUESTIONS, provider=provider, timeout=3.0)

    assert calls[0]["api_key"] == key
    assert "TYPESAFE_API_KEY" not in os.environ


def event(**decide: Any) -> BaseHookEvent:
    return input_to_event(Event.UserPromptSubmit, Input(prompt="roll it back", cwd="/w", decide=decide or None))


def test_evt_decide_maps_the_provider_name(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = provider_calls(monkeypatch, ANSWERED)
    evt = input_to_event(Event.UserPromptSubmit, Input(prompt="roll it back"))
    evt.ctx = context()

    assert evt.decide("roll it back", QUESTIONS, provider="openai", timeout=2.0) == ANSWERED
    assert (calls[0]["provider"], calls[0]["timeout"]) == (OPENAI, 2.0)


@pytest.mark.parametrize(
    ("failure", "recorded"),
    [
        pytest.param(DecideError(401, "Incorrect API key provided"), True, id="rejected-records-a-fault"),
        pytest.param(DecideKeyMissing("no jev API key"), True, id="missing-key-records-a-fault"),
        pytest.param(TimeoutError("no jev decision within 3s"), False, id="timeout-only-logs"),
    ],
)
def test_evt_decide_fails_open(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException, recorded: bool, tmp_path: Path
) -> None:
    provider_calls(monkeypatch, failure)
    monkeypatch.setattr(faults, "faults_dir", lambda: tmp_path)
    evt = input_to_event(Event.UserPromptSubmit, Input(prompt="roll it back", cwd="/w"))
    evt.ctx = context()

    assert evt.decide("roll it back", QUESTIONS) is None
    assert bool(faults.drain("/w")) is recorded


def test_inline_tests_answer_from_the_stub_and_never_reach_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = provider_calls(monkeypatch, ANSWERED)
    answer = LabelAnswer(choice="rollback", probabilities={"none": 0.1, "rollback": 0.9}, confidence=0.8)
    questions = {"action": Label("Which action?", {"none": None, "rollback": None})}

    with isolated_state_root():
        decision = event(action=answer).decide("x", questions)
        timed_out = event(error=TimeoutError()).decide("x", questions)
        unstubbed = event(other=answer).decide

        assert decision is not None
        assert decision.answers == {"action": answer}
        assert timed_out is None
        with pytest.raises(KeyError):
            unstubbed("x", questions)
    assert calls == []
