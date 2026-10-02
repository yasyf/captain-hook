"""The confirm step: a deterministic block lands only once a small model confirms the match."""

from __future__ import annotations

import contextvars
import json
import math
import threading
import time
from concurrent.futures import Future, wait
from dataclasses import dataclass, replace
from functools import partial
from hashlib import sha256
from typing import TYPE_CHECKING

import filelock
from loguru import logger
from pydantic import BaseModel, Field

from captain_hook.prompt import Prompt
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from collections.abc import Callable

    from spawnllm import LlmBackend

    from captain_hook.events import BaseHookEvent
    from captain_hook.types import HookResult

CONFIRM_RULES = """\
A deterministic hook matched the pending tool call and wants to block it. Its pattern match is cheap and
sometimes fires on calls the rule was never meant to stop. Judge whether this call really is what the rule
protects against, from the rule, the hook's message, the tool input, and the recent tool results.

Set block=true only when the call is what the rule protects against, and confident=true only when the
evidence leaves no reasonable doubt either way. Keep reasoning to one sentence.
"""
CONFIRM_ENDPOINT = "https://api.cerebras.ai/v1"
CONFIRM_MODEL = "gpt-oss-120b"
EVIDENCE_WINDOW = 8
EVIDENCE_CHARS = 3000
INPUT_CHARS = 4000
REMEMBERED_VERDICTS = 256


@dataclass(frozen=True, slots=True)
class Confirm:
    """Send a deterministic block through a small model before it lands.

    The block stands only when the model confidently confirms the call is what ``rule`` protects against.
    A timeout, an error, an unsure answer, or a no-match allows the call with a one-line ``additionalContext``
    note naming the hook and why. The model sees ``rule``, the tool input, the hook's message, and the last
    few tool results; the framework stops waiting after ``timeout_s`` wall-clock seconds. A verdict is
    remembered for the session per hook and exact input, so a retried identical call never pays twice.
    Pair it with ``skip_if=[Annotated(...)]``: an annotation that settles the call skips the hook, and the
    model with it. The model is Cerebras ``gpt-oss-120b``, the fastest small model measured against the
    three-second wall (``bench/confirm.py``); it reads ``CEREBRAS_API_KEY``, and without one every confirm
    allows with a failure note.

    Attributes:
        rule: What the block protects against, the one sentence the model judges the call by.
        timeout_s: Seconds the framework waits for the verdict before it allows the call.

    Example:
        >>> hook(Event.PreToolUse, only_if=[Runs("git", "push")], block=True,
        ...      message="Pushing to a queued PR drops the push. Ship a stacked PR instead.",
        ...      confirm=Confirm(rule="A push that lands on a branch the merge queue already admitted."))
    """

    rule: str
    timeout_s: float = 3.0


class ConfirmVerdict(BaseModel):
    """The confirm step's model answer: whether the call matches the rule, and whether the model is sure."""

    block: bool
    confident: bool
    reasoning: str


class ConfirmVerdicts(BaseModel):
    verdicts: dict[str, ConfirmVerdict] = Field(default_factory=dict)


def confirm_backend() -> LlmBackend:
    from spawnllm import OpenAiEndpointBackend

    return OpenAiEndpointBackend(
        CONFIRM_ENDPOINT, CONFIRM_MODEL, api_key=reqenv.getenv("CEREBRAS_API_KEY") or "", reasoning_effort="low"
    )


def confirm_prompt(rule: str, message: str, tool_input: str, evidence: str) -> Prompt:
    return (
        Prompt()
        .system(CONFIRM_RULES)
        .context("rule", rule)
        .context("hook_message", message)
        .context("tool_input", tool_input)
        .context("recent_tool_results", evidence)
    )


def verdict_key(hook: str, evt: BaseHookEvent, message: str) -> str:
    material = json.dumps([dict(evt.input.raw), message], sort_keys=True, default=str)
    return f"{hook}:{sha256(material.encode()).hexdigest()[:16]}"


def detached[T](work: Callable[[], T], *, name: str) -> Future[T]:
    """Run ``work`` on a daemon thread of its own, so a call nobody waits for any more holds no shared worker."""
    future: Future[T] = Future()

    def run() -> None:
        try:
            future.set_result(work())
        except Exception as exc:
            future.set_exception(exc)

    threading.Thread(target=contextvars.copy_context().run, args=(run,), name=name, daemon=True).start()
    return future


def ask(evt: BaseHookEvent, message: str, confirm: Confirm) -> ConfirmVerdict:
    from cc_transcript.render import Budget, clip

    evidence = evt.ctx.transcript_text(
        window=EVIDENCE_WINDOW, tool_results=True, budget=Budget(turn_chars=300, tool_chars=600)
    )
    return evt.ctx.call_llm(
        confirm_prompt(
            confirm.rule,
            message,
            clip(json.dumps(dict(evt.input.raw), default=str), INPUT_CHARS),
            clip(evidence, EVIDENCE_CHARS),
        ),
        backend=confirm_backend(),
        model="small",
        timeout=math.ceil(confirm.timeout_s),
        response_model=ConfirmVerdict,
    )


def remember(evt: BaseHookEvent, key: str, verdict: ConfirmVerdict, *, deadline: float) -> None:
    try:
        with evt.ctx.session[ConfirmVerdicts].mutate(timeout=max(0.0, deadline - time.monotonic())) as remembered:
            remembered.verdicts = dict(list({**remembered.verdicts, key: verdict}.items())[-REMEMBERED_VERDICTS:])
    except filelock.Timeout:
        logger.bind(key=key).info("confirm verdict cache busy past the deadline; verdict not remembered")


def judged(evt: BaseHookEvent, hook: str, message: str, confirm: Confirm) -> ConfirmVerdict | str:
    deadline = time.monotonic() + confirm.timeout_s
    key = verdict_key(hook, evt, message)
    if (known := evt.ctx.session[ConfirmVerdicts].get(ConfirmVerdicts()).verdicts.get(key)) is not None:
        return known
    future = detached(partial(ask, evt, message, confirm), name=f"capt-hook-confirm-{hook}")
    if not wait([future], timeout=confirm.timeout_s).done:
        return f"could not confirm in {confirm.timeout_s:g} s"
    if (failure := future.exception()) is not None:
        logger.bind(hook=hook).opt(exception=failure).warning("confirm step failed; allowing")
        return f"the confirm step failed ({type(failure).__name__})"
    remember(evt, key, verdict := future.result(), deadline=deadline)
    return verdict


def confirmed(evt: BaseHookEvent, hook: str, result: HookResult, confirm: Confirm) -> HookResult:
    """Settle a block that asked for confirmation: the block itself on a confident match, else an allow note."""
    match judged(evt, hook, result.message or "", confirm):
        case ConfirmVerdict(block=True, confident=True):
            return replace(result, confirm=None)
        case ConfirmVerdict(block=True):
            return evt.context(f"{hook}: allowed, the model could not confirm the match with confidence")
        case ConfirmVerdict():
            return evt.context(f"{hook}: allowed, the model found the call outside the rule")
        case str() as why:
            return evt.context(f"{hook}: allowed, {why}")
