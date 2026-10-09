from __future__ import annotations

import pytest

from captain_hook import Model
from captain_hook.context import HookContext
from captain_hook.session import SessionStore
from captain_hook.testing.helpers import fixture_session

QUESTION = {"type": "user", "message": {"role": "user", "content": "hi"}}


def context(*lines: dict) -> HookContext:
    return HookContext(session=SessionStore(None), transcript=fixture_session([QUESTION, *lines]), settings=None)


def reply(model: str) -> dict:
    return {
        "type": "assistant",
        "message": {"model": model, "role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    }


@pytest.mark.parametrize(
    ("id", "family", "version"),
    [
        pytest.param("claude-opus-5-5", "opus", (5, 5), id="opus-5-5"),
        pytest.param("claude-opus-6", "opus", (6, 0), id="major-only"),
        pytest.param("claude-sonnet-4-5-20250929", "sonnet", (4, 5), id="dated"),
        pytest.param("claude-opus-5-20260101", "opus", (5, 0), id="dated-major-only"),
        pytest.param("claude-fable-5-1[1m]", "fable", (5, 1), id="context-suffix"),
        pytest.param("claude-3-5-sonnet-20241022", None, None, id="legacy-claude"),
        pytest.param("gpt-6.1-sol", None, None, id="codex"),
    ],
)
def test_parse(id: str, family: str | None, version: tuple[int, int] | None) -> None:
    assert Model.parse(id) == Model(id, family, version)


@pytest.mark.parametrize(
    ("id", "family", "major", "minor", "expected"),
    [
        pytest.param("claude-opus-5-5", "opus", 5, 5, True, id="equal"),
        pytest.param("claude-opus-6", "opus", 5, 5, True, id="newer-major"),
        pytest.param("claude-opus-4-7", "opus", 5, 5, False, id="older"),
        pytest.param("claude-sonnet-5-5", "opus", 5, 5, False, id="other-family"),
        pytest.param("gpt-6.1-sol", "opus", 5, 0, False, id="unparsed"),
    ],
)
def test_at_least(id: str, family: str, major: int, minor: int, expected: bool) -> None:
    assert Model.parse(id).at_least(family, major, minor) is expected


def test_context_model_is_the_latest_real_reply() -> None:
    ctx = context(reply("claude-opus-4-7"), reply("claude-opus-5-5"), reply("<synthetic>"))

    assert ctx.model == Model("claude-opus-5-5", "opus", (5, 5))


def test_context_model_is_none_before_the_first_reply() -> None:
    assert context().model is None
