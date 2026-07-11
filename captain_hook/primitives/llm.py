from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger
from pydantic import BaseModel

from captain_hook.app import on
from captain_hook.contexts import apply_contexts, with_defaults
from captain_hook.primitives.nudge import DEFAULT_FIRES
from captain_hook.prompt import Prompt
from captain_hook.signals import extract_signal_context, resolve_signals, transcript_texts
from captain_hook.state import PrimitiveState, fired_this_turn, hook_name, record_fire
from captain_hook.types import (
    Action,
    Event,
    HookResult,
    InlineTests,
    Signal,
    Signals,
    TCondition,
    Waiting,
)
from captain_hook.util.paths import resolve_cache_dir

if TYPE_CHECKING:
    from spawnllm import TModel, TSpecialty

    from captain_hook.contexts import PromptContext
    from captain_hook.events import BaseHookEvent
    from captain_hook.signals.nlp import NlpSignal


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
    hook: str,
    signals: Sequence[Signal | NlpSignal] | Signals | None = None,
    when: Callable[[BaseHookEvent], bool] | None = None,
    contexts: Sequence[PromptContext] = (),
    max_context: int = 2000,
    specialty: TSpecialty = "review",
    model: TModel = "small",
    agent: bool = False,
    transcript: bool | int | Literal["recent", "full"] = False,
    diff: bool | str = False,
) -> M | None:
    from cc_transcript.render import clip

    if fired_this_turn(evt):
        return None
    if when is not None and not when(evt):
        return None

    if sig := resolve_signals(signals):
        ps = evt.ctx.s[PrimitiveState].get(PrimitiveState())
        texts = ps.unechoed_candidates(transcript_texts(evt, sig.window, sig.origin))
        if not (contributing_texts := ps.match_signals(sig, texts, hook)):
            return None
    elif contexts and when is None:
        contributing_texts = []
    else:
        contributing_texts = transcript_texts(evt, 5)

    context = clip(
        "\n".join(
            [line for text in contributing_texts for line in extract_signal_context(sig.patterns, text)]
            if sig
            else contributing_texts
        ),
        max_context,
    )

    base = Prompt().system(prompt).context("context", context or None)
    if (built := apply_contexts(base, evt, with_defaults(contexts), max_len=max_context)) is None:
        return None

    diff_text = evt.ctx.diff("uncommitted" if diff is True else diff) if diff else None
    if diff and not (diff_text or "").strip():
        return None

    try:
        return evt.ctx.call_llm(
            built.context("diff", diff_text),
            specialty=specialty,
            model=model,
            agent=agent,
            transcript=transcript,
            response_model=response_model,
        )
    except Exception:
        logger.bind(prompt=prompt).opt(exception=True).warning("llm evaluate failed")
        return None


def consume_signals(evt: BaseHookEvent, sig: Signals | None, hook: str) -> list[str] | None:
    """Re-match and consume ``sig`` under the state lock, returning the contributing texts or None.

    Consumption is the authoritative claim: the locked re-match runs the same candidate filter as
    the pre-gate, so when a concurrent process has already consumed the signal (or a quote/veto
    absorbs it) the re-match finds no contributors and returns None — the caller must then abort the
    fire rather than deliver a signal it did not actually claim.
    """
    if not sig:
        return None
    texts = transcript_texts(evt, sig.window, sig.origin)
    with evt.ctx.s[PrimitiveState].mutate() as ps:
        return ps.match_signals(sig, ps.unechoed_candidates(texts), hook)


def llm_primitive[M: BaseModel](
    prompt: str | Prompt,
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
    contexts: Sequence[PromptContext] = (),
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    events: Event | None = None,
    max_fires: int | None = DEFAULT_FIRES,
    tests: InlineTests | None = None,
    async_: bool = False,
    max_context: int = 2000,
    specialty: TSpecialty = "review",
    model: TModel = "small",
    agent: bool = False,
    transcript: bool | int | Literal["recent", "full"] = False,
    diff: bool | str = False,
) -> None:
    prompt = str(prompt)
    sig = resolve_signals(signals)
    name = hook_name(label, None, prompt)

    def handler(evt: BaseHookEvent) -> HookResult | None:
        if not (
            result := llm_evaluate(
                evt,
                prompt,
                response_model,
                hook=name,
                signals=signals,
                when=when,
                contexts=contexts,
                max_context=max_context,
                specialty=specialty,
                model=model,
                agent=agent,
                transcript=transcript,
                diff=diff,
            )
        ):
            return None
        if not verdict(result):
            return None
        if sig and consume_signals(evt, sig, name) is None:
            return None
        record_fire(evt)
        return HookResult(
            action=action,
            message=message(result) if callable(message) else message,
        )

    handler.__name__ = handler.__qualname__ = name

    resolved = events or default_events
    guards_waiting = action is Action.block and bool(resolved & (Event.Stop | Event.SubagentStop))
    on(
        resolved,
        only_if=only_if,
        skip_if=(Waiting(), *skip_if) if guards_waiting else tuple(skip_if),
        max_fires=(None if action is Action.block else default_max_fires) if max_fires == DEFAULT_FIRES else max_fires,
        tests=tests,
        async_=async_,
    )(handler)


def llm_gate(
    prompt: str | Prompt,
    *,
    message: str | Callable[[GateVerdict], str],
    response_model: type[GateVerdict] = GateVerdict,
    verdict: Callable[[GateVerdict], bool] = lambda r: r.block,
    signals: Sequence[Signal | NlpSignal] | Signals | None = None,
    when: Callable[[BaseHookEvent], bool] | None = None,
    contexts: Sequence[PromptContext] = (),
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    events: Event | None = None,
    max_fires: int | None = DEFAULT_FIRES,
    tests: InlineTests | None = None,
    max_context: int = 2000,
    specialty: TSpecialty = "review",
    model: TModel = "small",
    agent: bool = True,
    transcript: bool | int | Literal["recent", "full"] = True,
    diff: bool | str = False,
) -> None:
    """Register an LLM-powered blocking gate.

    Defaults are tuned for the common case: ``agent=True`` and ``transcript=True``
    so the gate has tool access and a recent transcript window (the path lets the agent
    read full history). Pass ``diff=True`` to attach a compact working-tree diff as a
    ``<diff>`` block, or ``agent=False, transcript=False`` for cheap, stateless yes/no checks.
    An empty diff (or no repo) skips the LLM call entirely, consuming no fire.

    ``contexts`` attaches declarative evidence blocks
    (:class:`~captain_hook.contexts.PromptContext`), each rendered as an XML block
    after ``<context>`` in array order; a ``required`` context with empty content
    skips the LLM call entirely, consuming no fire. Passing your own ``contexts``
    with no ``signals``/``when`` suppresses the implicit transcript ``<context>``
    block — you own context assembly. The ambient defaults
    (:class:`~captain_hook.contexts.BeforeEdit`/:class:`~captain_hook.contexts.AfterEdit`)
    attach to every gate without suppressing it, carrying the pending edit's
    before/after text on edit-shaped events and nothing elsewhere. A Write's
    pre-image is only knowable at ``PreToolUse``, so contexts reading it over Writes
    (``Introduced``, ``BeforeEdit(required=True)``) need ``events=Event.PreToolUse``.

    Example:
        >>> llm_gate("Is the agent making excuses?",
        ...          message=lambda r: f"Excuse detected: {r.reasoning}",
        ...          signals=Signals([Signal(r"external.*service", weight=2)], threshold=2))
        >>> llm_gate("Does the new code hardcode a secret?",
        ...          message=lambda r: f"Secret detected: {r.reasoning}",
        ...          contexts=[Introduced(pattern='os.environ[$KEY] = $VALUE')],
        ...          events=Event.PreToolUse, only_if=[Tool("Edit", "Write", "MultiEdit")])
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
        contexts=contexts,
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
        diff=diff,
    )


def llm_nudge(
    prompt: str | Prompt,
    *,
    message: str | Callable[[NudgeVerdict], str],
    response_model: type[NudgeVerdict] = NudgeVerdict,
    verdict: Callable[[NudgeVerdict], bool] = lambda r: r.fire,
    signals: Sequence[Signal | NlpSignal] | Signals | None = None,
    when: Callable[[BaseHookEvent], bool] | None = None,
    contexts: Sequence[PromptContext] = (),
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    events: Event | None = None,
    max_fires: int | None = DEFAULT_FIRES,
    tests: InlineTests | None = None,
    async_: bool = False,
    max_context: int = 2000,
    specialty: TSpecialty = "review",
    model: TModel = "small",
    agent: bool = True,
    transcript: bool | int | Literal["recent", "full"] = True,
    diff: bool | str = False,
) -> None:
    """Register an LLM-powered advisory nudge.

    Defaults are tuned for the common case: ``agent=True`` and ``transcript=True``
    so the nudge has tool access and a recent transcript window (the path lets the agent
    read full history). Pass ``diff=True`` to attach a compact working-tree diff as a
    ``<diff>`` block, or ``agent=False, transcript=False`` for cheap, stateless yes/no checks.
    An empty diff (or no repo) skips the LLM call entirely, consuming no fire.

    ``contexts`` attaches declarative evidence blocks
    (:class:`~captain_hook.contexts.PromptContext`), each rendered as an XML block
    after ``<context>`` in array order; a ``required`` context with empty content
    skips the LLM call entirely, consuming no fire. Passing your own ``contexts``
    with no ``signals``/``when`` suppresses the implicit transcript ``<context>``
    block — you own context assembly. The ambient defaults
    (:class:`~captain_hook.contexts.BeforeEdit`/:class:`~captain_hook.contexts.AfterEdit`)
    attach to every nudge without suppressing it, carrying the pending edit's
    before/after text on edit-shaped events and nothing elsewhere. A Write's
    pre-image is only knowable at ``PreToolUse``, so contexts reading it over Writes
    (``Introduced``, ``BeforeEdit(required=True)``) need ``events=Event.PreToolUse``
    — the nudge default of ``PostToolUse`` leaves them empty on Writes.

    Example:
        >>> llm_nudge("Is the agent speculating instead of observing?",
        ...           message="Observe, don't infer -- check traces first",
        ...           signals=Signals([Signal(r"should contain", weight=2)], threshold=3))
        >>> llm_nudge("Does any newly introduced comment narrate the edit itself?",
        ...           message=lambda r: f"Tombstone comment: {r.reasoning}",
        ...           contexts=[Introduced(kind=COMMENT_TYPES)],
        ...           events=Event.PreToolUse, only_if=[Tool("Edit", "Write", "MultiEdit")])
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
        contexts=contexts,
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
        diff=diff,
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

    failure_path = (
        resolve_cache_dir() / "failures" / (p.stem if (p := evt.ctx.t.path) else "unknown") / f"{timestamp}.json"
    )
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

    logger.opt(exception=exc).warning(
        f"prompt_check failed for {prefix}\n"
        f"  argv: {argv}\n"
        f"  exit_code: {exit_code}\n"
        f"  stderr: {stderr}\n"
        f"  stdout (tail 4KB): {stdout[-4096:]}\n"
        f"  prompt (tail 1KB): {prompt[-1024:]}\n"
        f"  failure_record: {failure_path}",
    )


def prompt_check(
    evt: BaseHookEvent,
    template: str | Prompt,
    fmt: dict[str, Any] | None = None,
    *,
    prefix: str,
    suffix: str = "",
    timeout: int = 45,
    include_reasoning: bool = True,
    diff: bool | str = False,
    response_model: type[PromptCheckVerdict] = PromptCheckVerdict,
) -> HookResult | None:
    """Run an LLM check with a formatted prompt and return block/warn/None."""
    reasoning = evt.ctx.t.recent(50).assistant_text() if include_reasoning else ""

    base = template if isinstance(template, Prompt) else Prompt().system(template.format(**(fmt or {})))
    built = base.context("agent_reasoning", reasoning or None)
    prompt_str = str(built)

    try:
        verdict = evt.ctx.call_llm(
            built,
            timeout=timeout,
            diff=diff,
            response_model=response_model,
        )
    except Exception as exc:
        record_prompt_check_failure(evt, prefix, prompt_str, exc)
        return None

    if not verdict:
        return None

    match verdict.action:
        case "block":
            return HookResult(action=Action.block, message=f"{prefix}: {verdict.reason}{suffix}")
        case "warning":
            return HookResult(action=Action.warn, message=f"{prefix}: {verdict.reason}{suffix}")
        case _:
            return None
