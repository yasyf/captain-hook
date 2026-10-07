from __future__ import annotations

import builtins
import json
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from itertools import count
from typing import TYPE_CHECKING, Any, Literal, get_args, get_origin

from annotated_types import Ge, Le
from loguru import logger
from pydantic import BaseModel, ValidationError

from captain_hook.app import on
from captain_hook.context import decision_prompt, is_unsupported_model
from captain_hook.contexts import apply_contexts, with_defaults
from captain_hook.primitives.nudge import DEFAULT_FIRES
from captain_hook.prompt import Prompt, render_template
from captain_hook.signals import extract_signal_context, resolve_signals, transcript_texts
from captain_hook.snapshots.client import EvidenceIncomplete
from captain_hook.state import PrimitiveState, fired_this_turn, hook_name, record_fire
from captain_hook.types import (
    TOOL_EVENTS,
    Action,
    Event,
    HookResult,
    InlineTests,
    Signal,
    Signals,
    TCondition,
    Waiting,
)
from captain_hook.util import reqenv
from captain_hook.util.paths import resolve_cache_dir

if TYPE_CHECKING:
    from cc_transcript.render import Budget
    from pydantic.fields import FieldInfo
    from spawnllm import Decision, TModel, TSpecialty
    from spawnllm.decide import Answer, Question

    from captain_hook.contexts import PromptContext
    from captain_hook.events import BaseHookEvent
    from captain_hook.signals.nlp import NlpSignal

LLM_RETRY_FLOOR_SECONDS = 5.0
VERDICT_STATE_CHARS = 64_000
MAX_SCORE_LEVELS = 10
MAX_LABEL_OPTIONS = 255

TJudge = Literal["jev", "llm"]
"""Who answers an LLM primitive: TypeSafe Jev (``jev``) for a categorical verdict, or the LLM (``llm``)."""


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


class BoolAnswer(BaseModel):
    """LLM response model for ``evt.llm(..., bool)``: a single yes/no ``answer``."""

    answer: bool


class IntAnswer(BaseModel):
    """LLM response model for ``evt.llm(..., int)``: a single integer ``answer``."""

    answer: int


def score_levels(info: FieldInfo) -> range | None:
    match info.metadata:
        case [Ge(ge=int() as low), Le(le=int() as high)] | [Le(le=int() as high), Ge(ge=int() as low)] if (
            2 <= high - low + 1 <= MAX_SCORE_LEVELS
        ):
            return range(low, high + 1)
        case _:
            return None


def field_question(instructions: str, name: str, info: FieldInfo) -> Question | None:
    from spawnllm import Binary, Label, Score

    match info.annotation:
        case builtins.bool if not info.metadata:
            return Binary(instructions, yes=f"{name}=true", no=f"{name}=false")
        case builtins.int if (levels := score_levels(info)) is not None:
            return Score(
                f"{instructions}\n\nRate {name} from {levels[0]} to {levels[-1]}.", dict.fromkeys(map(str, levels))
            )
        case annotation if (
            get_origin(annotation) is Literal
            and not info.metadata
            and 2 <= len(values := get_args(annotation)) <= MAX_LABEL_OPTIONS
            and all(isinstance(v, str) for v in values)
        ):
            return Label(f"{instructions}\n\nPick the value of {name}.", dict.fromkeys(get_args(annotation)))
        case _:
            return None


def free_text(info: FieldInfo) -> bool:
    return info.annotation is str and not info.metadata


def verdict_questions(
    instructions: str, response_model: type[BaseModel] | None, *, defaults: bool = False
) -> dict[str, Question] | None:
    """One Jev question per categorical field of ``response_model``, or ``None`` when Jev cannot answer it.

    A ``bool`` field becomes a ``Binary``, a ``Literal`` of strings a ``Label`` in declared order, and an
    ``int`` bounded by ``ge`` and ``le`` alone to 2 to 10 values a ``Score``. A ``str`` field asks nothing:
    :func:`verdict_from` fills it with a summary of the answers. A model with no categorical field, with
    any other field, or with a field constraint Jev's answer could break, such as a ``max_length``, needs the LLM.
    ``defaults=True`` also lets through any other field that has a default, which a Jev verdict keeps:
    the first stage of a two-stage call, where only an escalated verdict reaches the LLM for those fields.
    """
    fields: dict[str, FieldInfo] = response_model.model_fields if response_model is not None else {}
    asked = {name: question for name, info in fields.items() if (question := field_question(instructions, name, info))}
    free = fields.keys() - asked.keys()
    return (
        asked
        if asked and all(free_text(fields[name]) or (defaults and not fields[name].is_required()) for name in free)
        else None
    )


def answer_value(question: Question, answer: Answer) -> bool | str | int | None:
    from spawnllm import BinaryAnswer, LabelAnswer, Refused, Score, ScoreAnswer

    match question, answer:
        case _, BinaryAnswer(p_yes=p_yes):
            return p_yes > 0.5
        case _, LabelAnswer(choice=choice):
            return choice
        case Score(levels=levels), ScoreAnswer(score=score):
            return int(list(levels)[round(score)])
        case _, Refused():
            return None
    raise ValueError(f"{type(answer).__name__} does not answer {type(question).__name__}")


def verdict_from[M: BaseModel](
    response_model: type[M], questions: Mapping[str, Question], decision: Decision
) -> M | None:
    """Build ``response_model`` from a Jev ``decision`` on :func:`verdict_questions`, or ``None`` when Jev refused one.

    A ``Binary`` is true above even odds, a ``Label`` takes its choice, and a ``Score`` rounds to the
    nearest level. Every ``str`` field carries one line naming the model and its answers, and every
    other field keeps its default.
    """
    answers = {name: answer_value(question, decision.answers[name]) for name, question in questions.items()}
    if any(value is None for value in answers.values()):
        return None
    summary = f"{decision.model} decided " + ", ".join(f"{name}={value}" for name, value in answers.items())
    texts = [name for name, info in response_model.model_fields.items() if name not in answers and free_text(info)]
    return response_model.model_validate(answers | dict.fromkeys(texts, summary), by_name=True)


def verdict_state(transcript: str, evidence: Prompt) -> str:
    state = "\n\n".join(part for part in (transcript, str(Prompt(contexts=evidence.contexts))) if part)
    if len(state) <= VERDICT_STATE_CHARS:
        return state
    return f"…(-{len(state) - VERDICT_STATE_CHARS}ch){state[-VERDICT_STATE_CHARS:]}"


def needs_text(message: str | Callable[[Any], str], response_model: type[BaseModel]) -> bool:
    return isinstance(message, str) and any(
        f"{{{name}}}" in message for name, info in response_model.model_fields.items() if info.annotation is str
    )


def llm_evaluate[M: BaseModel](
    evt: BaseHookEvent,
    prompt: str | Prompt,
    response_model: type[M] | None,
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
    root_transcript: bool | int | Literal["recent", "full"] = False,
    root_excerpt: Callable[[BaseHookEvent], Sequence[str]] | None = None,
    tool_results: bool = False,
    budget: Budget | None = None,
    diff: bool | str = False,
    retries: int = 2,
    once_per_turn: bool = True,
    evidence: bool = True,
    backend: TJudge = "jev",
    escalate: Callable[[M], bool] | None = None,
) -> M | str | None:
    """Run one throttled, context-aware LLM evaluation for ``evt`` and return the validated verdict.

    Skips once ``hook`` has fired this turn unless ``once_per_turn`` is False, then applies
    signals/when gating, renders ``contexts`` (a ``required`` context with no content skips the call),
    attaches the transcript window and optional diff, then calls the backend — retrying up
    to ``retries`` times, feeding a schema validation failure back to the model on re-ask. Returns
    ``None`` on a skip; raises when the call still fails after the final retry, at once when the
    backend rejects the model itself, and at once when the caller's deadline is inside
    :data:`LLM_RETRY_FLOOR_SECONDS`, since a retry clamped to the seconds left cannot finish.
    ``backend="jev"``, the default, sends a categorical ``response_model`` (see
    :func:`verdict_questions`) to TypeSafe Jev instead: the prompt becomes each question's
    instructions and the transcript windows, contexts, and diff its state, with no tools, no
    validation retries, and ``specialty``/``model``/``agent`` unused. Jev's timeout, rejection, or
    missing key raises like a failed LLM call, and a question Jev refuses returns ``None`` like a
    skip. ``backend="llm"`` always asks the LLM, as does any ``response_model`` Jev cannot answer.
    ``escalate`` judges in two stages: Jev decides every categorical field first (see
    :func:`verdict_questions` with ``defaults=True``, so any other field needs a default), and only a
    verdict ``escalate`` returns true for goes on to the LLM, whose prompt carries Jev's answer as a
    ``<quick_verdict>`` block and whose verdict, in the full ``response_model``, is the result. Any
    other Jev verdict returns at once, each ``str`` field holding the one-line summary and every other
    field its default. A Jev timeout, error, or refusal goes on to the LLM without a quick verdict.
    ``evidence=False`` keeps the event's transcripts open after the call, for a caller that judges
    again within the same event.
    ``root_transcript`` takes a window like ``transcript`` and, when the event fires inside a subagent
    or teammate lane, adds that window of the root session that spawned the lane as
    ``<root_transcript>``, so a judge can read the user's words a lane never saw. ``root_excerpt``
    maps the event to needles (a quote, a thread, the text being judged) and adds every event
    in the last 16 MiB of the root transcript that mentions one as ``<root_excerpt>``.
    """
    from cc_transcript.render import clip

    if once_per_turn and fired_this_turn(evt):
        return None
    if when is not None and not when(evt):
        return None

    if sig := resolve_signals(signals):
        if not (contributing_texts := matched_signals(evt, sig, hook)):
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

    base = (prompt if isinstance(prompt, Prompt) else Prompt().system(prompt)).context("context", context or None)
    if (built := apply_contexts(base, evt, with_defaults(contexts, budget), max_len=max_context)) is None:
        return None

    diff_text = evt.ctx.diff("uncommitted" if diff is True else diff) if diff else None
    if diff and not (diff_text or "").strip():
        return None

    instructions = str(Prompt(system_text=built.system_text, ask_text=built.ask_text))
    excerpt = root_excerpt(evt) if root_excerpt else ()

    def ask(asked: Prompt) -> M | str | None:
        dispatched = evt.ctx.assemble_prompt(
            asked.context("diff", diff_text),
            (),
            {},
            transcript=transcript,
            tool_results=tool_results,
            budget=budget,
            diff_text=None,
            root_transcript=root_transcript,
            root_excerpt=excerpt,
        )
        current = dispatched
        for attempt in count():
            try:
                return evt.ctx.call_llm(
                    Prompt(system_text=current),
                    specialty=specialty,
                    model=model,
                    agent=agent,
                    evidence=evidence,
                    response_model=response_model,
                )
            except ValidationError as e:
                if attempt >= retries or not retry_affordable():
                    raise
                current = str(
                    Prompt(system_text=dispatched).context(
                        "validation_error",
                        f"{e}\nYour previous reply failed validation; answer again conforming to the schema.",
                    )
                )
                logger.bind(attempt=attempt).opt(exception=True).warning("llm output failed validation; retrying")
            except EvidenceIncomplete:
                raise
            except Exception as e:
                if attempt >= retries or is_unsupported_model(e) or not retry_affordable():
                    raise
                logger.bind(attempt=attempt).opt(exception=True).warning("llm call failed; retrying")

    if (
        backend == "jev"
        and response_model is not None
        and verdict_questions(instructions, response_model, defaults=escalate is not None) is not None
    ):
        state = verdict_state(
            evt.ctx.transcript_evidence(
                transcript=transcript,
                tool_results=tool_results,
                budget=budget,
                root_transcript=root_transcript,
                root_excerpt=excerpt,
            ),
            built.context("diff", diff_text),
        )
        if escalate is None:
            return evt.ctx.decide_verdict(
                instructions, state, response_model, root=str(evt.cwd) if evt.cwd else None, evidence=evidence
            )
        return staged_verdict(
            evt,
            instructions,
            state,
            response_model,
            escalate,
            lambda note: ask(built.context("quick_verdict", note)),
            evidence=evidence,
        )
    return ask(built)


def quick_note(verdict: BaseModel, fields: Iterable[str]) -> str:
    answered = ", ".join(f"{name}={getattr(verdict, name)}" for name in fields)
    return (
        f"A fast classifier read the same evidence and answered {answered}. It can be wrong: judge the evidence "
        "yourself, then fill in every field."
    )


def staged_verdict[M: BaseModel, R](
    evt: BaseHookEvent,
    instructions: str,
    state: str,
    response_model: type[M],
    escalate: Callable[[M], bool],
    ask: Callable[[str | None], R],
    *,
    evidence: bool,
) -> M | R:
    """Ask Jev for ``response_model`` first, and ``ask`` the LLM only when ``escalate`` holds for Jev's verdict.

    ``ask`` takes Jev's answer as one line for its prompt, or ``None`` when Jev cannot answer the model,
    timed out, failed, or refused, and returns the LLM's verdict. ``evidence`` records Jev's state for
    a verdict that does not escalate, as ``ask`` does for one that does.
    """
    if (questions := verdict_questions(instructions, response_model, defaults=True)) is None:
        return ask(None)
    try:
        quick = evt.ctx.decide_verdict(
            instructions,
            state,
            response_model,
            root=str(evt.cwd) if evt.cwd else None,
            evidence=False,
            defaults=True,
        )
    except EvidenceIncomplete:
        raise
    except Exception:
        logger.opt(exception=True).warning("jev gave no verdict; asking the llm")
        quick = None
    if quick is not None and not escalate(quick):
        if evidence:
            evt.ctx.release_preparation(decision_prompt(state, instructions))
        return quick
    return ask(None if quick is None else quick_note(quick, questions))


def retry_affordable() -> bool:
    return not reqenv.deadline_within(LLM_RETRY_FLOOR_SECONDS)


def matched_signals(evt: BaseHookEvent, sig: Signals, hook: str) -> list[str] | None:
    """The texts that would fire ``sig`` for ``hook`` right now, without consuming them."""
    ps = evt.ctx.s[PrimitiveState].get(PrimitiveState())
    texts = transcript_texts(evt, sig.window, sig.origin, sig.thinking)
    return ps.match_signals(sig, ps.unechoed_candidates(texts), hook)


def consume_signals(evt: BaseHookEvent, sig: Signals | None, hook: str) -> list[str] | None:
    """Re-match and consume ``sig`` under the state lock, returning the contributing texts or None.

    Consumption is the authoritative claim: the locked re-match runs the same candidate filter as
    the pre-gate, so when a concurrent process has already consumed the signal (or a quote/veto
    absorbs it) the re-match finds no contributors and returns None — the caller must then abort the
    fire rather than deliver a signal it did not actually claim.
    """
    if not sig:
        return None
    texts = transcript_texts(evt, sig.window, sig.origin, sig.thinking)
    with evt.ctx.s[PrimitiveState].mutate() as ps:
        return ps.match_signals(sig, ps.unechoed_candidates(texts), hook)


def llm_primitive[M: BaseModel](
    prompt: str | Prompt,
    *,
    action: Action,
    prefix: str,
    label: str | None = None,
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
    guards_waiting: bool | None = None,
    once_per_turn: bool | None = None,
    events: Event | None = None,
    max_fires: int | None = DEFAULT_FIRES,
    tests: InlineTests | None = None,
    async_: bool = False,
    advisory_on_deny: bool = False,
    max_context: int = 2000,
    specialty: TSpecialty = "review",
    model: TModel = "small",
    agent: bool = False,
    transcript: bool | int | Literal["recent", "full"] = False,
    tool_results: bool = False,
    budget: Budget | None = None,
    diff: bool | str = False,
    on_incomplete: str | None = None,
    backend: TJudge = "jev",
    escalate: Callable[[M], bool] | None = None,
) -> None:
    prompt = str(prompt)
    sig = resolve_signals(signals)
    name = hook_name(prefix, label, prompt)
    staged = escalate or (verdict if needs_text(message, response_model) else None)

    def handler(evt: BaseHookEvent) -> HookResult | None:
        try:
            result = llm_evaluate(
                evt,
                prompt,
                response_model,
                hook=name,
                once_per_turn=(action is not Action.block or not evt.event & TOOL_EVENTS)
                if once_per_turn is None
                else once_per_turn,
                signals=signals,
                when=when,
                contexts=contexts,
                max_context=max_context,
                specialty=specialty,
                model=model,
                agent=agent,
                transcript=transcript,
                tool_results=tool_results,
                budget=budget,
                diff=diff,
                backend=backend,
                escalate=staged,
            )
        except EvidenceIncomplete:
            raise
        except Exception:
            logger.bind(hook=name).opt(exception=True).warning("llm primitive failed")
            return None
        if not result:
            return None
        if not verdict(result):
            consume_signals(evt, sig, name)
            return None
        if sig and consume_signals(evt, sig, name) is None:
            return None
        record_fire(evt)
        return HookResult(
            action=action,
            message=message(result) if callable(message) else render_template(message, **result.model_dump()),
        )

    handler.__name__ = handler.__qualname__ = name

    resolved = events or default_events
    waiting_guarded = (
        action is Action.block and bool(resolved & (Event.Stop | Event.SubagentStop))
        if guards_waiting is None
        else guards_waiting
    )
    on(
        resolved,
        only_if=only_if,
        skip_if=(Waiting(), *skip_if) if waiting_guarded else tuple(skip_if),
        max_fires=(None if action is Action.block else default_max_fires) if max_fires == DEFAULT_FIRES else max_fires,
        tests=tests,
        async_=async_,
        skip_planning_agents=action is not Action.block,
        advisory_on_deny=advisory_on_deny,
        on_incomplete=on_incomplete,
    )(handler)


def llm_gate(
    prompt: str | Prompt,
    *,
    message: str | Callable[[GateVerdict], str],
    response_model: type[GateVerdict] = GateVerdict,
    verdict: Callable[[GateVerdict], bool] = lambda r: r.block,
    label: str | None = None,
    signals: Sequence[Signal | NlpSignal] | Signals | None = None,
    when: Callable[[BaseHookEvent], bool] | None = None,
    contexts: Sequence[PromptContext] = (),
    only_if: Sequence[TCondition] = (),
    skip_if: Sequence[TCondition] = (),
    guards_waiting: bool | None = None,
    once_per_turn: bool | None = None,
    events: Event | None = None,
    max_fires: int | None = DEFAULT_FIRES,
    tests: InlineTests | None = None,
    max_context: int = 2000,
    specialty: TSpecialty = "review",
    model: TModel = "small",
    agent: bool = True,
    transcript: bool | int | Literal["recent", "full"] = True,
    tool_results: bool = False,
    budget: Budget | None = None,
    diff: bool | str = False,
    on_incomplete: str | None = None,
    backend: TJudge = "jev",
    escalate: Callable[[GateVerdict], bool] | None = None,
) -> None:
    """Register an LLM-powered blocking gate.

    On a tool event the gate judges every call, so a call retried after a block is judged
    again rather than let through. On any other event a gate that blocked stays quiet for
    the rest of the turn, which keeps a Stop gate from looping.

    ``message`` may be a literal string, a ``{field}`` template with the verdict model's fields
    splatted in (same placeholder rules as :meth:`~captain_hook.Prompt.from_template`: only
    ``{identifier}`` substitutes, every other brace stays literal), or a callable taking the verdict.

    TypeSafe Jev answers by default, in about a tenth of a second and without tools: the prompt
    becomes the instructions of one question per verdict field, and the transcript window,
    contexts, and diff become its state. ``agent``, ``specialty``, and ``model`` apply only to
    the LLM. Under Jev, ``reasoning`` holds a one-line summary of the answer, so a ``message``
    that shows the model's own words needs the LLM. A ``{reasoning}`` template judges in two
    stages on its own: Jev decides, and only a verdict that fires goes on to the LLM, which reads
    Jev's answer and writes the verdict the message shows. A callable that reads ``reasoning``
    asks for the same with ``escalate=lambda r: r.block``, and ``backend="llm"`` skips Jev.

    Defaults are tuned for the common case: ``agent=True`` and ``transcript=True``
    so the gate has tool access and a recent transcript window (the path lets the agent
    read full history). Pass ``tool_results=True`` to render each tool result after its call
    in that window, as ``result:`` or ``failed:``, and ``budget=`` a cc-transcript ``Budget`` to
    widen what each prose chunk, tool call and answer preview keeps. Pass ``diff=True`` to attach a compact
    working-tree diff as a ``<diff>`` block, or ``agent=False, transcript=False`` for cheap,
    stateless yes/no checks.
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

    Args:
        label: Stable identity for this gate. When set, the hook name derives from
            ``label`` instead of the prompt hash, so review verdicts and fire state
            survive prompt edits; two registrations sharing a ``label`` within a module
            resolve to the same hook name. Uniqueness within the module is the author's
            responsibility. Omit it to derive the name from the prompt (the name then
            shifts whenever the prompt text changes).
        guards_waiting: Whether to skip while :class:`~captain_hook.types.Waiting`.
            ``None`` keeps the default — a blocking Stop/SubagentStop gate skips, anything
            else does not. Pass ``False`` for a gate whose subject *is* the turn that parks
            on background work rather than finishing.
        once_per_turn: Whether the gate stays quiet for the rest of the turn once any LLM hook
            has fired in it. ``None`` keeps the default — on everywhere except tool events.
            Pass ``False`` for a Stop gate that must judge every closing message: teammate and
            cross-session messages wake an orchestrator without opening a new turn, so one
            fire would otherwise silence the gate until the user next types.
        on_incomplete: Fail closed. When set, a call the gate cannot judge — its transcript
            evidence came back incomplete, or the caller's deadline left it unrun — is blocked
            with this remediation instead of skipped. Use it for permission gates, where a
            skipped judge lets the guarded action through.
        backend: Who judges. ``"jev"``, the default, asks TypeSafe Jev whenever the verdict
            model is categorical (see :func:`verdict_questions`); ``"llm"`` always asks the LLM.
            A Jev timeout or error skips the gate exactly as a failed LLM call does.
        escalate: Judge in two stages, as :func:`llm_evaluate` does: Jev first, then the LLM for
            a Jev verdict this returns true for, such as ``lambda r: r.block``. ``None`` keeps one
            stage, except for a ``message`` template that names a ``str`` field, which escalates
            the verdicts that block. A Jev timeout or error there asks the LLM instead of skipping.

    Example:
        >>> llm_gate("Is the agent making excuses?",
        ...          message="Fix the failure instead of blaming an external service. Rerun the failing step.",
        ...          signals=Signals([Signal(r"external.*service", weight=2)], threshold=2))
        >>> llm_gate("Does the new code hardcode a secret?",
        ...          message="Secrets come from the environment, never source. Read it with `os.environ`.",
        ...          contexts=[Introduced(pattern='os.environ[$KEY] = $VALUE')],
        ...          events=Event.PreToolUse, only_if=[Tool("Edit", "Write", "MultiEdit")])
    """
    llm_primitive(
        prompt,
        action=Action.block,
        prefix="llm_gate",
        label=label,
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
        guards_waiting=guards_waiting,
        once_per_turn=once_per_turn,
        events=events,
        max_fires=max_fires,
        tests=tests,
        max_context=max_context,
        specialty=specialty,
        model=model,
        agent=agent,
        transcript=transcript,
        tool_results=tool_results,
        budget=budget,
        diff=diff,
        on_incomplete=on_incomplete,
        backend=backend,
        escalate=escalate,
    )


def llm_nudge(
    prompt: str | Prompt,
    *,
    message: str | Callable[[NudgeVerdict], str],
    response_model: type[NudgeVerdict] = NudgeVerdict,
    verdict: Callable[[NudgeVerdict], bool] = lambda r: r.fire,
    label: str | None = None,
    advisory_on_deny: bool = False,
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
    tool_results: bool = False,
    budget: Budget | None = None,
    diff: bool | str = False,
    backend: TJudge = "jev",
    escalate: Callable[[NudgeVerdict], bool] | None = None,
) -> None:
    """Register an LLM-powered advisory nudge.

    ``message`` may be a literal string, a ``{field}`` template with the verdict model's fields
    splatted in (same placeholder rules as :meth:`~captain_hook.Prompt.from_template`: only
    ``{identifier}`` substitutes, every other brace stays literal), or a callable taking the verdict.

    TypeSafe Jev answers by default, in about a tenth of a second and without tools: the prompt
    becomes the instructions of one question per verdict field, and the transcript window,
    contexts, and diff become its state. ``agent``, ``specialty``, and ``model`` apply only to
    the LLM. Under Jev, ``reasoning`` holds a one-line summary of the answer, so a ``message``
    that shows the model's own words needs the LLM. A ``{reasoning}`` template judges in two
    stages on its own: Jev decides, and only a verdict that fires goes on to the LLM, which reads
    Jev's answer and writes the verdict the message shows. A callable that reads ``reasoning``
    asks for the same with ``escalate=lambda r: r.fire``, and ``backend="llm"`` skips Jev.

    Defaults are tuned for the common case: ``agent=True`` and ``transcript=True``
    so the nudge has tool access and a recent transcript window (the path lets the agent
    read full history). Pass ``tool_results=True`` to render each tool result after its call
    in that window, as ``result:`` or ``failed:``, and ``budget=`` a cc-transcript ``Budget`` to
    widen what each prose chunk, tool call and answer preview keeps. Pass ``diff=True`` to attach a compact
    working-tree diff as a ``<diff>`` block, or ``agent=False, transcript=False`` for cheap,
    stateless yes/no checks.
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

    Args:
        label: Stable identity for this nudge. When set, the hook name derives from
            ``label`` instead of the prompt hash, so review verdicts and fire state
            survive prompt edits; two registrations sharing a ``label`` within a module
            resolve to the same hook name. Uniqueness within the module is the author's
            responsibility. Omit it to derive the name from the prompt (the name then
            shifts whenever the prompt text changes).
        advisory_on_deny: Include this nudge after another hook's deny. Leave disabled
            when the message assumes the denied action ran.
        backend: Who judges. ``"jev"``, the default, asks TypeSafe Jev whenever the verdict
            model is categorical (see :func:`verdict_questions`); ``"llm"`` always asks the LLM.
            A Jev timeout or error skips the nudge exactly as a failed LLM call does.
        escalate: Judge in two stages, as :func:`llm_evaluate` does: Jev first, then the LLM for
            a Jev verdict this returns true for, such as ``lambda r: r.fire``. ``None`` keeps one
            stage, except for a ``message`` template that names a ``str`` field, which escalates
            the verdicts that fire. A Jev timeout or error there asks the LLM instead of skipping.

    Example:
        >>> llm_nudge("Is the agent speculating instead of observing?",
        ...           message="Observe, don't infer -- check traces first",
        ...           signals=Signals([Signal(r"should contain", weight=2)], threshold=3))
        >>> llm_nudge("Does any newly introduced comment narrate the edit itself?",
        ...           message="Comments never narrate the edit. Delete the comment.",
        ...           contexts=[Introduced(kind=COMMENT_TYPES)],
        ...           events=Event.PreToolUse, only_if=[Tool("Edit", "Write", "MultiEdit")])
    """
    llm_primitive(
        prompt,
        action=Action.warn,
        prefix="llm_nudge",
        label=label,
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
        advisory_on_deny=advisory_on_deny,
        max_context=max_context,
        specialty=specialty,
        model=model,
        agent=agent,
        transcript=transcript,
        tool_results=tool_results,
        budget=budget,
        diff=diff,
        backend=backend,
        escalate=escalate,
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
        resolve_cache_dir()
        / "failures"
        / (p.stem if (p := evt.ctx.transcript_path) else "unknown")
        / f"{timestamp}.json"
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
    """Run a two-stage check with a formatted prompt and return block/warn/None.

    TypeSafe Jev picks the action first; an ``"ok"`` returns ``None`` at once, and a ``"warning"``
    or ``"block"``, or a Jev failure, goes on to the LLM, which reads Jev's answer and writes the
    ``reason`` the message carries.
    """
    reasoning = evt.ctx.t.recent(50).assistant_text() if include_reasoning else ""

    base = template if isinstance(template, Prompt) else Prompt().system(template.format(**(fmt or {})))
    built = base.context("agent_reasoning", reasoning or None).context(
        "diff", evt.ctx.diff("uncommitted" if diff is True else diff) if diff else None
    )
    prompt_str = str(built)

    try:
        verdict = staged_verdict(
            evt,
            str(Prompt(system_text=built.system_text, ask_text=built.ask_text)),
            verdict_state("", built),
            response_model,
            lambda v: v.action != "ok",
            lambda note: evt.ctx.call_llm(
                built.context("quick_verdict", note), timeout=timeout, response_model=response_model
            ),
            evidence=True,
        )
    except EvidenceIncomplete:
        raise
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
