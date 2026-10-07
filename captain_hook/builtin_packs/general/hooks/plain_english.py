from __future__ import annotations

import contextvars
import re
import time
from concurrent.futures import wait
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from captain_hook import Action, Event, HookResult, Prompt, faults, on
from captain_hook.dispatch import offload_pool
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from captain_hook.events import MessageDisplayEvent

REWRITE_TIMEOUT_SECONDS = 6
ASSEMBLY_DEADLINE_SECONDS = 2.0
WRAPPING_FENCE = re.compile(r"```[^\n]*\n((?:(?!```).)*)\n```", re.DOTALL)
URL = re.compile(r"https?://\S+")
CODE_SPAN = re.compile(r"`[^`\n]+`")
LIST_ITEM = re.compile(r"^\s*(?:[-*]|\d+[.)])\s", re.MULTILINE)
MIN_KEPT_RATIO = 0.6
REWRITE_RULES = str(Prompt.load("plain_english_rules"))


class PlainEnglishBuffer(BaseModel):
    messages: dict[str, dict[int, str]] = Field(default_factory=dict)
    finalized: list[str] = Field(default_factory=list)


def assembled(evt: MessageDisplayEvent) -> str:
    slot = evt.ctx.session[PlainEnglishBuffer]
    deadline = time.monotonic() + ASSEMBLY_DEADLINE_SECONDS
    while True:
        with slot.mutate() as buffer:
            chunks = buffer.messages[evt.message_id]
            if all(i in chunks for i in range(evt.index + 1)) or time.monotonic() >= deadline:
                del buffer.messages[evt.message_id]
                buffer.finalized = [*buffer.finalized, evt.message_id][-64:]
                return "".join(chunks[i] for i in sorted(chunks))
        time.sleep(0.05)


def visible_length(text: str) -> int:
    return len(re.sub(r"\s", "", text))


def loss(text: str, rewritten: str) -> str | None:
    urls = [url.rstrip(".,;:!?") for url in URL.findall(text)]
    spans = CODE_SPAN.findall(text)
    items, kept_items = len(LIST_ITEM.findall(text)), len(LIST_ITEM.findall(rewritten))
    if dropped := [url for url in urls if url not in rewritten]:
        return f"rewrite dropped {len(dropped)} of {len(urls)} URLs"
    if dropped := [span for span in spans if span not in rewritten]:
        return f"rewrite dropped {len(dropped)} of {len(spans)} code spans"
    if kept_items < items:
        return f"rewrite cut list items from {items} to {kept_items}"
    if visible_length(rewritten) < MIN_KEPT_RATIO * visible_length(text):
        return f"rewrite kept under {MIN_KEPT_RATIO:.0%} of the text"
    return None


def is_prose(text: str) -> bool:
    return visible_length(re.sub(r"```.*?(?:```|\Z)", "", text, flags=re.DOTALL)) >= 200


def rewrite_prompt(evt: MessageDisplayEvent, text: str) -> Prompt:
    from captain_hook.snapshots.client import RemoteSession

    transcript = evt.ctx.transcript
    question = (
        next(iter(transcript.prompts(selection="last", count=1)), None)
        if isinstance(transcript, RemoteSession)
        else next((turn.prompt for turn in reversed(transcript.turns) if turn.prompt), None)
    )
    context = (
        [
            f'For context, the user asked the assistant: "{question[:800]}". Use this only to understand '
            "the message. Do NOT rewrite, answer, or repeat the user's question — rewrite only the assistant's "
            "message that follows."
        ]
        if question
        else []
    )
    return Prompt(system_text="\n\n".join([REWRITE_RULES, *context, text]))


def unwrapped(answer: str, text: str) -> str:
    stripped = (answer if "</think>" in text else answer.rpartition("</think>")[2]).strip()
    return match.group(1).strip() if (match := WRAPPING_FENCE.fullmatch(stripped)) else stripped


def rewrite(evt: MessageDisplayEvent, text: str, api_key: str, timeout: int) -> str:
    from spawnllm import OpenAiEndpointBackend

    return evt.ctx.call_llm(
        rewrite_prompt(evt, text),
        backend=OpenAiEndpointBackend(
            "https://api.cerebras.ai/v1", "qwen-3.8-27b", api_key=api_key, reasoning_effort="none"
        ),
        timeout=timeout,
    )


def plain_english(evt: MessageDisplayEvent, text: str, api_key: str) -> str:
    if not is_prose(text):
        return text
    left = reqenv.seconds_left()
    budget = REWRITE_TIMEOUT_SECONDS if left is None else min(REWRITE_TIMEOUT_SECONDS, left - 3.0)
    future = offload_pool().submit(contextvars.copy_context().run, rewrite, evt, text, api_key, max(1, int(budget)))
    finished = bool(wait([future], timeout=max(0.0, budget)).done)
    future.cancel()
    root = str(evt.cwd) if evt.cwd else None
    if (failure := future.exception() if finished else TimeoutError()) is not None:
        faults.record("plain_english rewrite", failure, root)
        return text
    rewritten = unwrapped(future.result(), text)
    if rewritten and (reason := loss(text, rewritten)):
        faults.record("plain_english rewrite", ValueError(reason), root)
        return text
    return rewritten or text


@on(Event.MessageDisplay)
def rewrite_plain_english(evt: MessageDisplayEvent) -> HookResult | None:
    if not (api_key := reqenv.getenv("CEREBRAS_API_KEY")):
        return None
    slot = evt.ctx.session[PlainEnglishBuffer]
    if slot.path is None:
        return None
    with slot.mutate() as buffer:
        if evt.message_id in buffer.finalized:
            return None
        buffer.messages.setdefault(evt.message_id, {})[evt.index] = evt.delta
    if not evt.final:
        return HookResult(action=Action.rewrite, message="")
    return HookResult(action=Action.rewrite, message=plain_english(evt, assembled(evt), api_key))
