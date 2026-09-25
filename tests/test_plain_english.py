from __future__ import annotations

import importlib
import importlib.resources
import json
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from spawnllm import OpenAiEndpointBackend

from captain_hook import Prompt, faults
from captain_hook.app import State, use_state
from captain_hook.builtin_packs.general.hooks import plain_english
from captain_hook.context import HookContext
from captain_hook.dispatch import dispatch, offload_pool
from captain_hook.events import MessageDisplayEvent
from captain_hook.loader import import_pack_module
from captain_hook.session import SessionStore
from captain_hook.testing.helpers import fixture_session
from captain_hook.types import Event

if TYPE_CHECKING:
    from collections.abc import Iterator

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
    delay: float = 0.0
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def call_llm(self, template: str | Prompt, *args: Any, **kwargs: Any) -> str:
        self.calls.append(
            (self.assemble_prompt(template, args, {}, transcript=False, tool_results=False, diff_text=None), kwargs)
        )
        time.sleep(self.delay)
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
def registered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-key")
    importlib.reload(plain_english)


def shown(text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "MessageDisplay", "displayContent": text}}


def chunk(ctx: HookContext, index: int, delta: str, *, final: bool = False) -> dict[str, Any] | None:
    return dispatch(
        Event.MessageDisplay,
        MessageDisplayEvent(
            _raw={"message_id": "msg_1", "index": index, "final": final, "delta": delta, "session_id": "s"},
            ctx=ctx,
        ),
    )


def stream(ctx: HookContext, text: str, *, size: int = 60) -> dict[str, Any] | None:
    parts = [text[i : i + size] for i in range(0, len(text), size)]
    results = [chunk(ctx, i, part, final=i == len(parts) - 1) for i, part in enumerate(parts)]
    assert results[:-1] == [shown("")] * (len(parts) - 1)
    return results[-1]


def test_no_api_key_leaves_chunks_displayed(ctx: CerebrasStub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CEREBRAS_API_KEY")

    assert chunk(ctx, 0, "partial") is None
    assert chunk(ctx, 1, PROSE, final=True) is None
    assert ctx.session[plain_english.PlainEnglishBuffer].path is not None
    assert not ctx.session[plain_english.PlainEnglishBuffer].path.exists()
    assert ctx.calls == []


def test_non_final_chunk_is_blanked_and_buffered(ctx: CerebrasStub) -> None:
    assert chunk(ctx, 0, "I traced") == shown("")
    assert ctx.session.load(plain_english.PlainEnglishBuffer).messages == {"msg_1": {0: "I traced"}}


def test_streamed_message_is_rewritten_once(ctx: CerebrasStub) -> None:
    assert stream(ctx, PROSE + BRACED) == shown("Plain rewrite.")

    ((prompt, kwargs),) = ctx.calls
    assert prompt == "\n\n".join(
        [
            plain_english.REWRITE_RULES,
            f'For context, the user asked the assistant: "{QUESTION}". Use this only to understand the message. '
            "Do NOT rewrite, answer, or repeat the user's question — rewrite only the assistant's message that "
            "follows.",
            PROSE + BRACED,
        ]
    )
    backend = kwargs["backend"]
    assert isinstance(backend, OpenAiEndpointBackend)
    assert (backend.base_url, backend.model, backend.api_key, backend.reasoning_effort) == (
        "https://api.cerebras.ai/v1",
        "qwen-3.8-27b",
        "test-key",
        "none",
    )
    assert kwargs["timeout"] == 6
    assert ctx.session.load(plain_english.PlainEnglishBuffer).messages == {}


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

    assert chunk(ctx, 2, "!", final=True) == shown("Plain rewrite.")
    late.join()

    ((prompt, _),) = ctx.calls
    assert prompt.endswith(f"\n\n{PROSE}!")


def test_final_chunk_joins_what_arrived_by_the_deadline(ctx: CerebrasStub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plain_english, "ASSEMBLY_DEADLINE_SECONDS", 0.1)
    ctx.answer = ""
    chunk(ctx, 1, PROSE)

    assert chunk(ctx, 3, "!", final=True) == shown(f"{PROSE}!")
    assert ctx.session.load(plain_english.PlainEnglishBuffer).messages == {}


def test_chunk_after_assembly_stays_displayed(ctx: CerebrasStub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plain_english, "ASSEMBLY_DEADLINE_SECONDS", 0.1)
    ctx.answer = ""
    chunk(ctx, 0, PROSE)
    chunk(ctx, 2, "!", final=True)

    assert chunk(ctx, 1, " late") is None
    assert ctx.session.load(plain_english.PlainEnglishBuffer).messages == {}


def test_without_session_storage_chunks_stay_displayed(ctx: CerebrasStub) -> None:
    ctx.session = SessionStore(None)

    assert chunk(ctx, 0, "partial") is None
    assert chunk(ctx, 1, PROSE, final=True) is None
    assert ctx.calls == []


def test_slow_rewrite_is_abandoned_before_the_caller_deadline(
    ctx: CerebrasStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plain_english, "REWRITE_TIMEOUT_SECONDS", 0.2)
    ctx.delay = 2.0

    started = time.monotonic()
    assert stream(ctx, PROSE) == shown(PROSE)
    assert time.monotonic() - started < 1.0
    (line,) = faults.drain()
    assert "plain_english rewrite" in line


@pytest.fixture
def one_offload_thread(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr("captain_hook.dispatch.OFFLOAD_THREADS", 1)
    offload_pool.cache_clear()
    yield
    offload_pool().shutdown(wait=True)
    offload_pool.cache_clear()


def final_event(ctx: CerebrasStub) -> MessageDisplayEvent:
    return MessageDisplayEvent(
        _raw={"message_id": "msg_1", "index": 0, "final": True, "delta": PROSE, "session_id": "s"}, ctx=ctx
    )


@pytest.mark.usefixtures("one_offload_thread")
def test_rewrite_queued_behind_a_slow_one_is_cancelled_on_timeout(
    ctx: CerebrasStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plain_english, "REWRITE_TIMEOUT_SECONDS", 0.2)
    ctx.delay = 1.0

    assert plain_english.plain_english(final_event(ctx), PROSE, "test-key") == PROSE
    assert plain_english.plain_english(final_event(ctx), PROSE, "test-key") == PROSE
    offload_pool().submit(time.sleep, 0).result()

    assert len(ctx.calls) == 1
    (line,) = faults.drain()
    assert "TimeoutError" in line


@pytest.mark.usefixtures("one_offload_thread")
def test_rediscovered_hook_modules_share_the_bounded_pool(ctx: CerebrasStub, monkeypatch: pytest.MonkeyPatch) -> None:
    fqn = "captain_hook._packs.general.plain_english"
    monkeypatch.delitem(sys.modules, fqn, raising=False)
    modules = []
    for _ in range(2):
        with use_state(State()):
            modules.append(import_pack_module(fqn, Path(plain_english.__file__)))
        monkeypatch.setattr(modules[-1], "REWRITE_TIMEOUT_SECONDS", 0.2)
    ctx.delay = 1.0

    assert [module.plain_english(final_event(ctx), PROSE, "test-key") for module in modules] == [PROSE, PROSE]
    offload_pool().submit(time.sleep, 0).result()

    assert modules[0].offload_pool is modules[1].offload_pool is offload_pool
    assert len(ctx.calls) == 1


def test_rewrite_budget_leaves_margin_before_the_caller_deadline(
    ctx: CerebrasStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plain_english.reqenv, "seconds_left", lambda: 8.0)

    stream(ctx, PROSE)

    ((_, kwargs),) = ctx.calls
    assert kwargs["timeout"] == 5


def test_final_chunk_settles_before_claude_code_cancels_the_hook() -> None:
    hooks = json.loads((importlib.resources.files("captain_hook") / "hooks" / "hooks.json").read_text())
    [group] = hooks["hooks"]["MessageDisplay"]
    [entry] = group["hooks"]

    assert plain_english.ASSEMBLY_DEADLINE_SECONDS + plain_english.REWRITE_TIMEOUT_SECONDS + 1 < entry["timeout"] <= 10


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Done. The test passes now.", id="short"),
        pytest.param("Run this:\n\n```python\n" + "x = compute(1, 2)\n" * 40 + "```\n", id="code-only"),
    ],
)
def test_message_with_little_prose_shows_the_original(ctx: CerebrasStub, text: str) -> None:
    assert stream(ctx, text) == shown(text)
    assert ctx.calls == []


def test_llm_failure_shows_the_original_and_records_a_fault(ctx: CerebrasStub) -> None:
    ctx.answer = RuntimeError("cerebras 503")

    assert stream(ctx, PROSE) == shown(PROSE)
    (line,) = faults.drain()
    assert "plain_english rewrite" in line
    assert "RuntimeError: cerebras 503" in line


@pytest.mark.parametrize(
    ("answer", "display"),
    [
        pytest.param(
            "<think>Split the sentences.</think>\n\nShort one. Short two.", "Short one. Short two.", id="think"
        ),
        pytest.param("Split them.</think>Short one.", "Short one.", id="unopened-think"),
        pytest.param("\n\nShort one. Short two.\n", "Short one. Short two.", id="surrounding-whitespace"),
        pytest.param("```markdown\nShort one.\n\nShort two.\n```", "Short one.\n\nShort two.", id="wrapping-fence"),
        pytest.param(
            "```sh\nls\n```\nShort one.\n```sh\npwd\n```",
            "```sh\nls\n```\nShort one.\n```sh\npwd\n```",
            id="inner-fences-kept",
        ),
        pytest.param("<think>nothing to say</think>\n", PROSE, id="empty-falls-back"),
    ],
)
def test_answer_is_cleaned_before_display(ctx: CerebrasStub, answer: str, display: str) -> None:
    ctx.answer = answer

    assert stream(ctx, PROSE) == shown(display)


def test_literal_think_tag_in_the_message_is_kept(ctx: CerebrasStub) -> None:
    text = PROSE + '\n\n```python\nclosing_tag = "</think>"\n```\n'
    ctx.answer = text

    assert stream(ctx, text) == shown(text.strip())
