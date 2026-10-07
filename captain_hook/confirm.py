"""The confirm step: a deterministic block lands only once a fast classifier confirms the match."""

from __future__ import annotations

import contextvars
import json
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
from spawnllm import Binary, BinaryAnswer

if TYPE_CHECKING:
    from collections.abc import Callable

    from captain_hook.events import BaseHookEvent
    from captain_hook.types import HookResult

CONFIRM_QUESTIONS = {
    "protected": Binary(
        "A deterministic hook matched this pending tool call and wants to block it. Is the call really what the rule "
        "protects against?",
        yes="The call is exactly the case the rule exists to stop.",
        no="The pattern matched a call the rule was never meant to stop, or an exception the rule names covers it.",
    ),
}
PROTECTED = 0.35
CONFIRMED = {"protected": BinaryAnswer(p_yes=0.9, confidence=0.8)}
"""Inline-test answers that confirm a ``Confirm`` block, as in ``Input(..., decide=CONFIRMED): Block()``."""
UNCONFIRMED = {"protected": BinaryAnswer(p_yes=0.05, confidence=0.9)}
"""Inline-test answers that clear a ``Confirm`` block, as in ``Input(..., decide=UNCONFIRMED): Allow()``."""
EVIDENCE_WINDOW = 8
EVIDENCE_CHARS = 3000
INPUT_CHARS = 4000
REMEMBERED_VERDICTS = 256


@dataclass(frozen=True, slots=True)
class Confirm:
    """Send a deterministic block through TypeSafe Jev before it lands.

    The block stands only when Jev's probability that the call is what ``rule`` protects against
    reaches :data:`PROTECTED`, the threshold measured against reference labels. A lower answer, or a
    question Jev declines, allows the call without ``additionalContext``.

    Timeouts and errors allow with one-line notes, once per distinct note per hook per session.
    A busy note-ledger lock still surfaces the note. Claude Code's permission flow decides as usual.

    Jev sees ``rule``, the tool input, the hook's message, and the last few tool results;
    the framework stops waiting after ``timeout_s`` wall-clock seconds.

    A verdict is remembered for the session per hook and exact input, so a retried identical call
    never pays twice.

    Pair it with ``skip_if=[Annotated(...)]``: an annotation that settles the call skips the hook,
    and the classifier with it.

    The key comes from ``TYPESAFE_API_KEY`` or the Keychain item ``spawnllm key set jev`` writes.
    Without it, every confirm allows; its failure note follows the same once-per-session rule.

    Attributes:
        rule: What the block protects against, the one sentence the classifier judges the call by.
        timeout_s: Seconds the framework waits for the verdict before it allows the call.

    Example:
        >>> hook(Event.PreToolUse, only_if=[Runs("git", "push")], block=True,
        ...      message="Pushing to a queued PR drops the push. Ship a stacked PR instead.",
        ...      confirm=Confirm(rule="A push that lands on a branch the merge queue already admitted."))
    """

    rule: str
    timeout_s: float = 3.0


class ConfirmDecisions(BaseModel):
    blocks: dict[str, bool] = Field(default_factory=dict)


class ConfirmNotes(BaseModel):
    noted: set[str] = Field(default_factory=set)


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


def ask(evt: BaseHookEvent, message: str, confirm: Confirm) -> bool | None:
    from cc_transcript.render import Budget, clip

    evidence = evt.ctx.transcript_text(
        window=EVIDENCE_WINDOW, tool_results=True, budget=Budget(turn_chars=300, tool_chars=600)
    )
    state = {
        "rule": confirm.rule,
        "hook_message": message,
        "tool_input": clip(json.dumps(dict(evt.input.raw), default=str), INPUT_CHARS),
        "recent_tool_results": clip(evidence, EVIDENCE_CHARS),
    }
    if (decision := evt.decide(state, CONFIRM_QUESTIONS, timeout=confirm.timeout_s)) is None:
        return None
    match decision.answers["protected"]:
        case BinaryAnswer(p_yes=p_yes):
            return p_yes >= PROTECTED
        case _:
            return False


def remember(evt: BaseHookEvent, key: str, blocks: bool, *, deadline: float) -> None:
    try:
        with evt.ctx.session[ConfirmDecisions].mutate(timeout=max(0.0, deadline - time.monotonic())) as remembered:
            remembered.blocks = dict(list({**remembered.blocks, key: blocks}.items())[-REMEMBERED_VERDICTS:])
    except filelock.Timeout:
        logger.bind(key=key).info("confirm verdict cache busy past the deadline; verdict not remembered")


def judged(evt: BaseHookEvent, hook: str, message: str, confirm: Confirm) -> bool | str:
    deadline = time.monotonic() + confirm.timeout_s
    key = verdict_key(hook, evt, message)
    if (known := evt.ctx.session[ConfirmDecisions].get(ConfirmDecisions()).blocks.get(key)) is not None:
        return known
    future = detached(partial(ask, evt, message, confirm), name=f"capt-hook-confirm-{hook}")
    if not wait([future], timeout=confirm.timeout_s).done:
        return f"could not confirm in {confirm.timeout_s:g} s"
    if (failure := future.exception()) is not None:
        logger.bind(hook=hook).opt(exception=failure).warning("confirm step failed; allowing")
        return f"the confirm step failed ({type(failure).__name__})"
    if (blocks := future.result()) is None:
        return "the confirm step got no answer from Jev"
    remember(evt, key, blocks, deadline=deadline)
    return blocks


def noted_once(evt: BaseHookEvent, note: str) -> HookResult | None:
    try:
        with evt.ctx.session[ConfirmNotes].mutate(timeout=0) as notes:
            fresh = note not in notes.noted
            notes.noted.add(note)
    except filelock.Timeout:
        fresh = True
    return evt.context(note) if fresh else None


def confirmed(evt: BaseHookEvent, hook: str, result: HookResult, confirm: Confirm) -> HookResult | None:
    """Settle a block that asked for confirmation: the block itself on a confident match, else let the call through."""
    match judged(evt, hook, result.message or "", confirm):
        case True:
            return replace(result, confirm=None)
        case False:
            return None
        case str() as why:
            return noted_once(evt, f"{hook}: allowed, {why}")
