from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest
from spawnllm import OpenAiEndpointBackend

from captain_hook import Prompt, faults
from captain_hook.builtin_packs.plain_english.hooks import rewrite
from captain_hook.context import HookContext
from captain_hook.events import MessageDisplayEvent
from captain_hook.session import SessionStore
from captain_hook.testing.helpers import fixture_session
from captain_hook.types import Action, HookResult

if TYPE_CHECKING:
    from pathlib import Path

PROSE = (
    "I traced the flaky test to a race between the writer and the reader, which both open the same session "
    "file without a lock, so a read can observe a half-written document and fail to parse it. "
    "You can reproduce it by running the suite with forty workers."
)
BRACED = "\n\n```python\nconfig = {'workers': 40}\nprint(f'{config}')\n```\n"
QUESTION = "why does test_state_race flake under xdist?"


@dataclass
class CerebrasStub(HookContext):
    answer: str | Exception = "Plain rewrite."
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def call_llm(self, template: str | Prompt, *args: Any, **kwargs: Any) -> str:
        self.calls.append((self.assemble_prompt(template, args, {}, transcript=False, diff_text=None), kwargs))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def ctx(session_dir: Path) -> CerebrasStub:
    return CerebrasStub(
        session=SessionStore(session_dir),
        transcript=fixture_session(
            [
                {"type": "user", "message": {"role": "user", "content": QUESTION}},
                {"type": "user", "isMeta": True, "message": {"role": "user", "content": "<meta>caveat</meta>"}},
            ]
        ),
        settings=None,
    )


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-key")


def chunk(ctx: HookContext, index: int, delta: str, *, final: bool = False) -> HookResult | None:
    return rewrite.rewrite_plain_english(
        MessageDisplayEvent(
            _raw={"message_id": "msg_1", "index": index, "final": final, "delta": delta, "session_id": "s"},
            ctx=ctx,
        )
    )


def stream(ctx: HookContext, text: str, *, size: int = 60) -> HookResult | None:
    parts = [text[i : i + size] for i in range(0, len(text), size)]
    results = [chunk(ctx, i, part, final=i == len(parts) - 1) for i, part in enumerate(parts)]
    assert results[:-1] == [HookResult(action=Action.rewrite, message="")] * (len(parts) - 1)
    return results[-1]


def test_no_api_key_leaves_chunks_displayed(ctx: CerebrasStub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CEREBRAS_API_KEY")

    assert chunk(ctx, 0, "partial") is None
    assert chunk(ctx, 1, PROSE, final=True) is None
    assert ctx.session[rewrite.PlainEnglishBuffer].path is not None
    assert not ctx.session[rewrite.PlainEnglishBuffer].path.exists()
    assert ctx.calls == []


def test_non_final_chunk_is_blanked_and_buffered(ctx: CerebrasStub) -> None:
    assert chunk(ctx, 0, "I traced") == HookResult(action=Action.rewrite, message="")
    assert ctx.session.load(rewrite.PlainEnglishBuffer).messages == {"msg_1": {0: "I traced"}}


def test_streamed_message_is_rewritten_once(ctx: CerebrasStub) -> None:
    assert stream(ctx, PROSE + BRACED) == HookResult(action=Action.rewrite, message="Plain rewrite.")

    ((prompt, kwargs),) = ctx.calls
    assert prompt == "\n\n".join(
        [
            rewrite.REWRITE_RULES,
            f'For context, the user asked the assistant: "{QUESTION}". Use this only to understand the message. '
            "Do NOT rewrite, answer, or repeat the user's question — rewrite only the assistant's message that "
            "follows.",
            PROSE + BRACED,
        ]
    )
    backend = kwargs["backend"]
    assert isinstance(backend, OpenAiEndpointBackend)
    assert (backend.base_url, backend.model, backend.api_key) == (
        "https://api.cerebras.ai/v1",
        "qwen-3.8-27b",
        "test-key",
    )
    assert kwargs["timeout"] == 45
    assert ctx.session.load(rewrite.PlainEnglishBuffer).messages == {}


def test_long_question_is_truncated(ctx: CerebrasStub) -> None:
    ctx.transcript = fixture_session([{"type": "user", "message": {"role": "user", "content": "q" * 2000}}])

    stream(ctx, PROSE)

    ((prompt, _),) = ctx.calls
    assert f'asked the assistant: "{"q" * 800}".' in prompt


def test_out_of_order_chunks_join_by_index(ctx: CerebrasStub) -> None:
    chunk(ctx, 1, PROSE[40:])
    chunk(ctx, 0, PROSE[:40])
    chunk(ctx, 2, "!", final=True)

    ((prompt, _),) = ctx.calls
    assert prompt.endswith(f"\n\n{PROSE}!")


def test_final_chunk_waits_for_a_late_chunk(ctx: CerebrasStub) -> None:
    chunk(ctx, 1, PROSE[40:])
    late = threading.Timer(0.2, chunk, args=(ctx, 0, PROSE[:40]))
    late.start()

    assert chunk(ctx, 2, "!", final=True) == HookResult(action=Action.rewrite, message="Plain rewrite.")
    late.join()

    ((prompt, _),) = ctx.calls
    assert prompt.endswith(f"\n\n{PROSE}!")


def test_final_chunk_joins_what_arrived_by_the_deadline(ctx: CerebrasStub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rewrite, "ASSEMBLY_DEADLINE_SECONDS", 0.1)
    ctx.answer = ""
    chunk(ctx, 1, PROSE)

    assert chunk(ctx, 3, "!", final=True) == HookResult(action=Action.rewrite, message=f"{PROSE}!")
    assert ctx.session.load(rewrite.PlainEnglishBuffer).messages == {}


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Done. The test passes now.", id="short"),
        pytest.param("Run this:\n\n```python\n" + "x = compute(1, 2)\n" * 40 + "```\n", id="code-only"),
    ],
)
def test_message_with_little_prose_shows_the_original(ctx: CerebrasStub, text: str) -> None:
    assert stream(ctx, text) == HookResult(action=Action.rewrite, message=text)
    assert ctx.calls == []


def test_llm_failure_shows_the_original_and_records_a_fault(ctx: CerebrasStub) -> None:
    ctx.answer = RuntimeError("cerebras 503")

    assert stream(ctx, PROSE) == HookResult(action=Action.rewrite, message=PROSE)
    (line,) = faults.drain()
    assert "plain_english rewrite" in line
    assert "RuntimeError: cerebras 503" in line


@pytest.mark.parametrize(
    ("answer", "shown"),
    [
        pytest.param(
            "<think>Split the sentences.</think>\n\nShort one. Short two.", "Short one. Short two.", id="think"
        ),
        pytest.param("Split them.</think>Short one.", "Short one.", id="unopened-think"),
        pytest.param("```markdown\nShort one.\n\nShort two.\n```", "Short one.\n\nShort two.", id="wrapping-fence"),
        pytest.param(
            "```sh\nls\n```\nShort one.\n```sh\npwd\n```",
            "```sh\nls\n```\nShort one.\n```sh\npwd\n```",
            id="inner-fences-kept",
        ),
        pytest.param("<think>nothing to say</think>\n", PROSE, id="empty-falls-back"),
    ],
)
def test_answer_is_cleaned_before_display(ctx: CerebrasStub, answer: str, shown: str) -> None:
    ctx.answer = answer

    assert stream(ctx, PROSE) == HookResult(action=Action.rewrite, message=shown)
