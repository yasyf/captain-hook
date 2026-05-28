from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from captain_hook import state
from captain_hook.app import on
from captain_hook.primitives.audit import session_id_for
from captain_hook.prompt import Prompt, PromptMessage
from captain_hook.state import PrimitiveState, fired_this_turn, hook_name, record_fire
from captain_hook.types import (
    Action,
    Event,
    HookResult,
    InlineTests,
    Signal,
    Signals,
    TCondition,
)

logger = logging.getLogger(__name__)

FAILURE_ROOT = state.CACHE_ROOT / "failures"

if TYPE_CHECKING:
    from captain_hook._backends import TModel, TSpecialty
    from captain_hook.events import BaseHookEvent
    from captain_hook.signals.nlp import NlpSignal

from captain_hook.signals import extract_signal_context, resolve_signals, transcript_texts


class GateVerdict(BaseModel):
    """LLM response model for ``llm_gate``. The LLM sets ``block=True`` to deny."""

    block: bool
    reasoning: str


class NudgeVerdict(BaseModel):
    """LLM response model for ``llm_nudge``. The LLM sets ``fire=True`` to trigger the nudge."""

    fire: bool
    reasoning: str


class PromptCheckVerdict(BaseModel):
    """LLM response model for ``prompt_check``. Action is ``"ok"``, ``"warning"``, or ``"block"``."""

    action: Literal["ok", "warning", "block"]
    reason: str


def llm_evaluate[M: BaseModel](
    evt: BaseHookEvent,
    prompt: str,
    response_model: type[M],
    *,
    signals: Sequence[Signal | NlpSignal] | Signals | None = None,
    when: Callable[[BaseHookEvent], bool] | None = None,
    max_context: int = 2000,
    specialty: TSpecialty = "review",
    model: TModel = "small",
    agent: bool = False,
    transcript: bool = False,
) -> M | None:
    if fired_this_turn(evt):
        return None

    if sig := resolve_signals(signals):
        ps = evt.ctx.s[PrimitiveState].get(PrimitiveState())
        texts = transcript_texts(evt, sig.window)
        old_consumed = ps.consumed.copy()
        if not (contributing_texts := ps.match_signals(sig, texts)):
            evt.ctx.s[PrimitiveState].set(ps)
            return None
        ps.consumed = old_consumed
        evt.ctx.s[PrimitiveState].set(ps)
    elif when is not None and not when(evt):
        return None
    else:
        contributing_texts = transcript_texts(evt, 5)

    context = "\n".join(
        [line for text in contributing_texts for line in extract_signal_context(sig.patterns, text)]
        if sig
        else contributing_texts
    )[:max_context]

    built = Prompt().system(prompt).context("context", context or None)

    try:
        return evt.ctx.call_llm(
            built,
            specialty=specialty,
            model=model,
            agent=agent,
            transcript=transcript,
            response_model=response_model,
        )
    except Exception:
        logger.warning("LLM evaluate failed for prompt: %.100s", prompt, exc_info=True)
        return None


def consume_signals(evt: BaseHookEvent, sig: Signals | None) -> None:
    if not sig:
        return
    ps = evt.ctx.s[PrimitiveState].get(PrimitiveState())
    texts = transcript_texts(evt, sig.window)
    ps.match_signals(sig, texts)
    evt.ctx.s[PrimitiveState].set(ps)


def llm_primitive[M: BaseModel](
    prompt: str,
    *,
    action: Action,
    label: str,
    message: str | Callable[[M], str],
    response_model: type[M],
    verdict: Callable[[M], bool],
    default_events: Event,
    default_max_fires: int,
    signals: Sequence[Signal | NlpSignal] | Signals | None = None,
    when: Callable[[BaseHookEvent], bool] | None = None,
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    events: Event | None = None,
    max_fires: int | None = None,
    tests: InlineTests | None = None,
    async_: bool = False,
    max_context: int = 2000,
    specialty: TSpecialty = "review",
    model: TModel = "small",
    agent: bool = False,
    transcript: bool = False,
) -> None:
    sig = resolve_signals(signals)

    def handler(evt: BaseHookEvent) -> HookResult | None:
        if not (
            result := llm_evaluate(
                evt,
                prompt,
                response_model,
                signals=signals,
                when=when,
                max_context=max_context,
                specialty=specialty,
                model=model,
                agent=agent,
                transcript=transcript,
            )
        ):
            return None
        if not verdict(result):
            return None
        consume_signals(evt, sig)
        record_fire(evt)
        return HookResult(
            action=action,
            message=message(result) if callable(message) else message,
        )

    handler.__name__ = handler.__qualname__ = hook_name(label, None, prompt)

    on(
        events or default_events,
        only_if=only_if,
        skip_if=skip_if,
        max_fires=max_fires if max_fires is not None else default_max_fires,
        tests=tests,
        async_=async_,
    )(handler)


def llm_gate(
    prompt: str,
    *,
    message: str | Callable[[GateVerdict], str],
    response_model: type[GateVerdict] = GateVerdict,
    verdict: Callable[[GateVerdict], bool] = lambda r: r.block,
    signals: Sequence[Signal | NlpSignal] | Signals | None = None,
    when: Callable[[BaseHookEvent], bool] | None = None,
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    events: Event | None = None,
    max_fires: int | None = None,
    tests: InlineTests | None = None,
    max_context: int = 2000,
    specialty: TSpecialty = "review",
    model: TModel = "small",
    agent: bool = True,
    transcript: bool = True,
) -> None:
    """Register an LLM-powered blocking gate.

    Defaults are tuned for the common case: ``agent=True`` and ``transcript=True``
    so the gate has tool access and full transcript context. Pass
    ``agent=False, transcript=False`` for cheap, stateless yes/no checks.

    Example:
        >>> llm_gate("Is the agent making excuses?",
        ...          message=lambda r: f"Excuse detected: {r.reasoning}",
        ...          signals=Signals([Signal(r"external.*service", weight=2)], threshold=2))
    """
    llm_primitive(
        prompt,
        action=Action.block,
        label="llm_gate",
        message=message,
        response_model=response_model,
        verdict=verdict,
        default_events=Event.Stop | Event.SubagentStop,
        default_max_fires=1,
        signals=signals,
        when=when,
        only_if=only_if,
        skip_if=skip_if,
        events=events,
        max_fires=max_fires,
        tests=tests,
        max_context=max_context,
        specialty=specialty,
        model=model,
        agent=agent,
        transcript=transcript,
    )


def llm_nudge(
    prompt: str,
    *,
    message: str | Callable[[NudgeVerdict], str],
    response_model: type[NudgeVerdict] = NudgeVerdict,
    verdict: Callable[[NudgeVerdict], bool] = lambda r: r.fire,
    signals: Sequence[Signal | NlpSignal] | Signals | None = None,
    when: Callable[[BaseHookEvent], bool] | None = None,
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    events: Event | None = None,
    max_fires: int | None = None,
    tests: InlineTests | None = None,
    async_: bool = False,
    max_context: int = 2000,
    specialty: TSpecialty = "review",
    model: TModel = "small",
    agent: bool = True,
    transcript: bool = True,
) -> None:
    """Register an LLM-powered advisory nudge.

    Defaults are tuned for the common case: ``agent=True`` and ``transcript=True``
    so the nudge has tool access and full transcript context. Pass
    ``agent=False, transcript=False`` for cheap, stateless yes/no checks.

    Example:
        >>> llm_nudge("Is the agent speculating instead of observing?",
        ...           message="Observe, don't infer -- check traces first",
        ...           signals=Signals([Signal(r"should contain", weight=2)], threshold=3))
    """
    llm_primitive(
        prompt,
        action=Action.warn,
        label="llm_nudge",
        message=message,
        response_model=response_model,
        verdict=verdict,
        default_events=Event.PostToolUse,
        default_max_fires=3,
        signals=signals,
        when=when,
        only_if=only_if,
        skip_if=skip_if,
        events=events,
        max_fires=max_fires,
        tests=tests,
        async_=async_,
        max_context=max_context,
        specialty=specialty,
        model=model,
        agent=agent,
        transcript=transcript,
    )


def record_prompt_check_failure(
    evt: BaseHookEvent,
    prefix: str,
    prompt: str,
    exc: BaseException,
) -> None:
    timestamp = datetime.now(UTC).isoformat().replace(":", "-")
    match exc:
        case subprocess.CalledProcessError(cmd=cmd, returncode=rc, output=out, stderr=err):
            argv = list(cmd) if isinstance(cmd, list | tuple) else str(cmd)
            exit_code, stdout, stderr = rc, out or "", err or ""
        case _:
            argv, exit_code, stdout, stderr = None, None, "", ""

    failure_path = FAILURE_ROOT / (session_id_for(evt) or "unknown") / f"{timestamp}.json"
    failure_path.parent.mkdir(parents=True, exist_ok=True)
    failure_path.write_text(
        json.dumps(
            {
                "timestamp": timestamp,
                "prefix": prefix,
                "argv": argv,
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "prompt": prompt,
                "exception_type": type(exc).__name__,
                "exception_str": str(exc),
            },
            indent=2,
        )
    )

    logger.warning(
        "prompt_check failed for %s\n"
        "  argv: %s\n"
        "  exit_code: %s\n"
        "  stderr: %s\n"
        "  stdout (tail 4KB): %s\n"
        "  prompt (tail 1KB): %s\n"
        "  failure_record: %s",
        prefix,
        argv,
        exit_code,
        stderr,
        stdout[-4096:],
        prompt[-1024:],
        failure_path,
        exc_info=True,
    )


def prompt_check(
    evt: BaseHookEvent,
    template: str | PromptMessage,
    fmt: dict[str, Any] | None = None,
    *,
    prefix: str,
    suffix: str = "",
    timeout: int = 45,
    include_reasoning: bool = True,
    response_model: type[PromptCheckVerdict] = PromptCheckVerdict,
) -> HookResult | None:
    """Run an LLM check with a formatted prompt and return block/warn/None."""
    reasoning = ""
    if include_reasoning:
        reasoning = evt.ctx.t.recent(50).assistant_text() if hasattr(evt.ctx.t, "recent") else ""

    base = template if isinstance(template, PromptMessage) else Prompt().system(template.format(**(fmt or {})))
    built = base.context("agent_reasoning", reasoning or None)
    prompt_str = str(built)

    try:
        verdict = evt.ctx.call_llm(
            built,
            timeout=timeout,
            response_model=response_model,
        )
    except Exception as exc:
        record_prompt_check_failure(evt, prefix, prompt_str, exc)
        return None

    if not verdict:
        return None

    assert isinstance(verdict, response_model)
    match verdict.action:
        case "block":
            return HookResult(action=Action.block, message=f"{prefix}: {verdict.reason}{suffix}")
        case "warning":
            return HookResult(action=Action.warn, message=f"{prefix}: {verdict.reason}{suffix}")
        case _:
            return None
