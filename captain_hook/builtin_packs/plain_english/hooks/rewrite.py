from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from captain_hook import Action, Event, HookResult, Prompt, faults, on
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from captain_hook.events import MessageDisplayEvent

CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"
CEREBRAS_MODEL = "qwen-3.8-27b"
REWRITE_TIMEOUT_SECONDS = 20
ASSEMBLY_DEADLINE_SECONDS = 2.0
ASSEMBLY_POLL_SECONDS = 0.05
MIN_PROSE_CHARS = 200
QUESTION_CHARS = 800
FENCED_BLOCK = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
WHITESPACE = re.compile(r"\s")
WRAPPING_FENCE = re.compile(r"```[^\n]*\n((?:(?!```).)*)\n```", re.DOTALL)
REWRITE_RULES = str(Prompt.load("rewrite_rules"))


class PlainEnglishBuffer(BaseModel):
    messages: dict[str, dict[int, str]] = Field(default_factory=dict)


def assembled(evt: MessageDisplayEvent) -> str:
    slot = evt.ctx.session[PlainEnglishBuffer]
    deadline = time.monotonic() + ASSEMBLY_DEADLINE_SECONDS
    while True:
        with slot.mutate() as buffer:
            chunks = buffer.messages[evt.message_id]
            if all(i in chunks for i in range(evt.index + 1)) or time.monotonic() >= deadline:
                del buffer.messages[evt.message_id]
                return "".join(chunks[i] for i in sorted(chunks))
        time.sleep(ASSEMBLY_POLL_SECONDS)


def is_prose(text: str) -> bool:
    return len(WHITESPACE.sub("", FENCED_BLOCK.sub("", text))) >= MIN_PROSE_CHARS


def rewrite_prompt(evt: MessageDisplayEvent, text: str) -> Prompt:
    question = next((turn.prompt for turn in reversed(evt.ctx.transcript.turns) if turn.prompt), None)
    context = (
        [
            f'For context, the user asked the assistant: "{question[:QUESTION_CHARS]}". Use this only to understand '
            "the message. Do NOT rewrite, answer, or repeat the user's question — rewrite only the assistant's "
            "message that follows."
        ]
        if question
        else []
    )
    return Prompt(system_text="\n\n".join([REWRITE_RULES, *context, text]))


def unwrapped(answer: str) -> str:
    stripped = answer.rpartition("</think>")[2].strip()
    return match.group(1).strip() if (match := WRAPPING_FENCE.fullmatch(stripped)) else stripped


def plain_english(evt: MessageDisplayEvent, text: str, api_key: str) -> str:
    from spawnllm import OpenAiEndpointBackend

    if not is_prose(text):
        return text
    try:
        answer = evt.ctx.call_llm(
            rewrite_prompt(evt, text),
            backend=OpenAiEndpointBackend(CEREBRAS_BASE_URL, CEREBRAS_MODEL, api_key=api_key),
            timeout=REWRITE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        faults.record("plain_english rewrite", exc, str(evt.cwd) if evt.cwd else None)
        return text
    return unwrapped(answer) or text


@on(Event.MessageDisplay)
def rewrite_plain_english(evt: MessageDisplayEvent) -> HookResult | None:
    if not (api_key := reqenv.getenv("CEREBRAS_API_KEY")):
        return None
    with evt.ctx.session[PlainEnglishBuffer].mutate() as buffer:
        buffer.messages.setdefault(evt.message_id, {})[evt.index] = evt.delta
    if not evt.final:
        return HookResult(action=Action.rewrite, message="")
    return HookResult(action=Action.rewrite, message=plain_english(evt, assembled(evt), api_key))
