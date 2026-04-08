from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from captain_hook.app import get_current_app
from captain_hook.prompt import Prompt
from captain_hook.state import PrimitiveState, fired_this_turn, hook_name, record_fire
from captain_hook.types import (
    Action,
    Event,
    HookResult,
    Signal,
    Signals,
    TCondition,
    TTest,
)

if TYPE_CHECKING:
    from captain_hook.context import TModel, TSpecialty
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
    """Evaluate an LLM prompt against transcript context with signal pre-filtering.

    Checks signals or ``when`` predicate first. If triggered, builds a prompt
    with signal context and calls the LLM backend, returning a parsed response model.

    Args:
        evt: The current hook event.
        prompt: System prompt for the LLM.
        response_model: Pydantic model to parse the LLM response into.
        signals: Signal patterns for pre-filtering transcript text.
        when: Predicate fallback when no signals are provided.
        max_context: Maximum characters of context to include.
        specialty: LLM backend selection (``"review"``, ``"debugging"``, ``"general"``).
        model: Model size (``"small"``, ``"medium"``, ``"large"``).
        agent: If True, runs the LLM in agent mode.
        transcript: If True, includes the full transcript in the prompt.

    Returns:
        Parsed response model instance, or None if signals don't match or LLM fails.
    """
    if fired_this_turn(evt):
        return None

    if sig := resolve_signals(signals):
        ps = evt.ctx.s[PrimitiveState].get() or PrimitiveState()
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
        return None


def consume_signals(evt: BaseHookEvent, sig: Signals | None) -> None:
    if not sig:
        return
    ps = evt.ctx.s[PrimitiveState].get() or PrimitiveState()
    texts = transcript_texts(evt, sig.window)
    ps.match_signals(sig, texts)
    evt.ctx.s[PrimitiveState].set(ps)


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
    tests: TTest | None = None,
    max_context: int = 2000,
    specialty: TSpecialty = "review",
    model: TModel = "small",
    agent: bool = False,
    transcript: bool = False,
) -> None:
    """Register an LLM-powered blocking gate.

    Pre-filters via signals, then asks the LLM for a ``GateVerdict``.
    Blocks when ``verdict(result)`` returns True.

    Args:
        prompt: System prompt describing what the LLM should evaluate.
        message: Block message string, or callable receiving the verdict.
        response_model: Pydantic model for LLM response (default ``GateVerdict``).
        verdict: Predicate on the response to decide whether to block.
        signals: Signal patterns for transcript pre-filtering.
        when: Predicate fallback when no signals are provided.
        only_if: Conditions that must all match.
        skip_if: Conditions that suppress the gate if any match.
        events: Override default event targeting (default ``Stop | SubagentStop``).
        max_fires: Limit fires per session (default 1).
        tests: Inline test dict for ``run_inline_tests``.
        max_context: Maximum characters of context for the LLM.
        specialty: LLM backend (default ``"review"``).
        model: Model size (default ``"small"``).
        agent: If True, runs the LLM in agent mode.
        transcript: If True, includes the full transcript.

    Example:
        >>> llm_gate("Is the agent making excuses?",
        ...          message=lambda r: f"Excuse detected: {r.reasoning}",
        ...          signals=Signals([Signal(r"external.*service", weight=2)], threshold=2))
    """
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
            action=Action.block,
            message=message(result) if callable(message) else message,
        )

    handler.__name__ = handler.__qualname__ = hook_name("llm_gate", None, prompt)

    app = get_current_app()
    app.on(
        events or (Event.Stop | Event.SubagentStop),
        only_if=only_if,
        skip_if=skip_if,
        max_fires=max_fires if max_fires is not None else 1,
        tests=tests,
    )(handler)


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
    tests: TTest | None = None,
    async_: bool = False,
    max_context: int = 2000,
    specialty: TSpecialty = "review",
    model: TModel = "small",
    agent: bool = False,
    transcript: bool = False,
) -> None:
    """Register an LLM-powered advisory nudge.

    Pre-filters via signals, then asks the LLM for a ``NudgeVerdict``.
    Warns when ``verdict(result)`` returns True.

    Args:
        prompt: System prompt describing what the LLM should evaluate.
        message: Warning message string, or callable receiving the verdict.
        response_model: Pydantic model for LLM response (default ``NudgeVerdict``).
        verdict: Predicate on the response to decide whether to warn.
        signals: Signal patterns for transcript pre-filtering.
        when: Predicate fallback when no signals are provided.
        only_if: Conditions that must all match.
        skip_if: Conditions that suppress the nudge if any match.
        events: Override default event targeting (default ``PostToolUse``).
        max_fires: Limit fires per session (default 3).
        tests: Inline test dict for ``run_inline_tests``.
        async_: If True, runs in the async dispatch pass.
        max_context: Maximum characters of context for the LLM.
        specialty: LLM backend (default ``"review"``).
        model: Model size (default ``"small"``).
        agent: If True, runs the LLM in agent mode.
        transcript: If True, includes the full transcript.

    Example:
        >>> llm_nudge("Is the agent speculating instead of observing?",
        ...           message="Observe, don't infer — check traces first",
        ...           signals=Signals([Signal(r"should contain", weight=2)], threshold=3))
    """
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
            action=Action.warn,
            message=message(result) if callable(message) else message,
        )

    handler.__name__ = handler.__qualname__ = hook_name("llm_nudge", None, prompt)

    app = get_current_app()
    app.on(
        events or Event.PostToolUse,
        only_if=only_if,
        skip_if=skip_if,
        max_fires=max_fires if max_fires is not None else 3,
        tests=tests,
        async_=async_,
    )(handler)


def prompt_check(
    evt: BaseHookEvent,
    template: str,
    fmt: dict[str, Any],
    *,
    prefix: str,
    suffix: str = "",
    timeout: int = 45,
    include_reasoning: bool = True,
    response_model: type[PromptCheckVerdict] = PromptCheckVerdict,
) -> HookResult | None:
    """Run an LLM check with a formatted prompt and return block/warn/None.

    Used by handler hooks that need per-event LLM evaluation (e.g. test
    integrity checks). The ``template`` is formatted with ``fmt``, recent
    assistant reasoning is appended as context, and the LLM returns a
    ``PromptCheckVerdict``.

    Args:
        evt: The current hook event.
        template: Prompt template string with ``{key}`` placeholders.
        fmt: Dict of values to format into the template.
        prefix: Prefix prepended to the verdict message (e.g. ``"TEST QUALITY"``).
        suffix: Suffix appended to the verdict message.
        timeout: LLM call timeout in seconds.
        include_reasoning: If True, includes recent assistant text as context.
        response_model: Pydantic model for parsing the verdict.

    Returns:
        ``HookResult`` with block/warn action, or None if the LLM says ``"ok"``.
    """
    reasoning = ""
    if include_reasoning:
        reasoning = evt.ctx.t.recent(50).assistant_text() if hasattr(evt.ctx.t, "recent") else ""

    built = Prompt().system(template.format(**fmt)).context("agent_reasoning", reasoning or None)

    try:
        verdict = evt.ctx.call_llm(
            built,
            timeout=timeout,
            response_model=response_model,
        )
    except Exception:
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
