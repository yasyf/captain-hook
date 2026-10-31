"""``Decide``: the one judge every ticket declaration shares, reading the user's words over an action."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from importlib.metadata import version
from time import perf_counter
from typing import TYPE_CHECKING, Literal

from loguru import logger
from pydantic import BaseModel, Field
from spawnllm import Label, LabelAnswer

from captain_hook.conditions import UserSaid
from captain_hook.prompt import Prompt, dedent_text
from captain_hook.tickets import store
from captain_hook.tickets.conditions import CustomTicketCondition, UserAnswered, found
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from loguru import Logger
    from spawnllm import Decision, TModel, TSpecialty

    from captain_hook.contexts import PromptContext
    from captain_hook.tickets.context import Context
    from captain_hook.tickets.records import Evidence, Proposal
    from captain_hook.types import TCondition

FRAME = """
    You decide whether the human user of this coding agent permitted one pending action, in their own
    words. <rules> are the hook's own rules for this kind of action; follow them. <evidence> lists the
    user's words that may permit it, each under an id, with the agent message each answers: only those
    count as the user speaking. <action> is the action.

    Cite in relied_on the ids of every evidence item your verdict rests on, copied exactly from the
    square brackets in <evidence>; an allow that cites none of them is refused.
    When you refuse, set refusal to one plain sentence for the agent, under 150 characters, with no
    quotation marks, ids, dates, or times: what the user's words permit and what this action does
    beyond them. Reason first, quoting the user words you relied on, then set allow.
"""
UNPERMITTED = "none"
PERMITTED = 0.8
JEV_UNSETTLED = "Jev found no single user approval that permits exactly this action."
DETAIL_SHOWN = 600
RETRY_DETAIL_SHOWN = 200


class Verdict(BaseModel):
    """The judge's answer: whether the user permitted the action, why, and the evidence it rests on."""

    reason: str
    allow: bool
    relied_on: list[str] = Field(default_factory=list[str])
    refusal: str = ""

    @property
    def explained(self) -> str:
        return self.refusal or self.reason


class Undecided(Exception):
    """The judge gave no verdict in time; an action it cannot judge never goes ahead on its say."""

    def __init__(self, cause: str) -> None:
        super().__init__(f"the judge gave no verdict ({cause})")
        self.cause = cause


def render_evidence(items: Sequence[Evidence], shown: int = DETAIL_SHOWN) -> str | None:
    blocks: list[str] = []
    for item in items:
        said = f" said {store.stamp(item.said_at)}" if item.said_at else ""
        detail = f"\n{item.detail[:shown]}" if item.detail else ""
        blocks.append(f"[{item.id}] {item.source}{said}{detail}\nuser's words: {item.quote}")
    return "\n\n".join(blocks) or None


def render_action(action: Proposal) -> str:
    payload = json.dumps(dict(action.payload), indent=1, default=str, ensure_ascii=False)
    return f"{action.summary}\nplace: {dict(action.place)}\npayload: {payload}"


def permits_question(items: Sequence[Evidence]) -> Label:
    return Label(
        "Which one <evidence> item, in the user's own words and under <rules>, permits exactly the action in "
        "<action>? Pick none when no single item does, when the action goes beyond the item's words, or when unsure.",
        {UNPERMITTED: "No single evidence item permits this action.", **dict.fromkeys(item.id for item in items)},
    )


def quick_verdict(decision: Decision) -> Verdict | None:
    """An allow citing the item Jev picked when it is sure that item permits this action, else ``None``."""
    match decision.answers["permits"]:
        case LabelAnswer(choice=choice, probabilities=probabilities) if (
            choice != UNPERMITTED and probabilities[choice] >= PERMITTED
        ):
            return Verdict(
                reason=f"{decision.model} read {choice} as permitting exactly this action",
                allow=True,
                relied_on=[choice],
            )
        case _:
            return None


def tail(ident: str) -> str:
    return ident.rpartition(":")[2]


QUOTED_RUN = 20


def quotes(ident: str, item: Evidence) -> bool:
    """Whether the citation *ident* carries the item's words, or is a long run of them, instead of its id."""
    return bool(item.quote) and (item.quote in ident or (len(ident) >= QUOTED_RUN and ident in item.quote))


def cited(items: Sequence[Evidence], ids: Sequence[str]) -> list[Evidence]:
    """The items *ids* name: an id copied whole or inside a longer line, one whose tail after the last colon
    survived a mangled prefix, or one whose words the citation quotes."""
    named = {ident.strip().strip("[]") for ident in ids}
    tails = {tail(ident) for ident in named}
    return [
        item
        for item in items
        if any(item.id in ident or quotes(ident, item) for ident in named)
        or item.id.partition(":")[2] in named
        or tail(item.id) in tails
    ]


@cache
def decision_build() -> str:
    return version("capt-hook")


def decision_log() -> Logger:
    from captain_hook.snapshots.client import CURRENT_CLIENT

    client = CURRENT_CLIENT.get()
    return logger.bind(
        capt_build=decision_build(), worker_pid=os.getpid(),
        request_prefix=client.request_prefix if client is not None else None,
    )


@contextmanager
def decision_stage(kind: str, stage: str) -> Iterator[None]:
    started = perf_counter()
    try:
        yield
    finally:
        elapsed_ms = round((perf_counter() - started) * 1000, 3)
        decision_log().bind(kind=kind, stage=stage, elapsed_ms=elapsed_ms).info(
            "ticket decision stage"
        )


@dataclass(frozen=True, slots=True)
class Decide(CustomTicketCondition):
    """A judge's reading of the user's words: the evidence it cites as permitting the action, if any.

    TypeSafe Jev judges first, citing the one evidence item that permits exactly this action; sure of it,
    the action goes ahead on that citation. Any other answer, a refusal, or a Jev failure asks the LLM,
    which cites and may still allow; one LLM refusal is asked again on the evidence alone, and the action
    is refused only when both askings refuse. With ``llm`` off, Jev alone judges, and an unsettled answer
    refuses.
    A judge that gives no verdict in time raises :class:`Undecided`, which the declaration reports as a
    rule that found nothing.

    Attributes:
        rules: The hook's rules for this kind of action, in prose.
        over: The conditions whose evidence the judge reads; the user's words and answers by default.
        contexts: Extra prompt contexts the hook renders, such as the destination.
        model: spawnllm model tier; capt-hook's small judge model by default.
        specialty: spawnllm specialty for the call.
        deadline: Seconds the judge may take before it fails.
        transcript: Recent-session window the judge reads, as ``llm_evaluate`` takes it.
        root_transcript: The spawning session's window, for a lane.
        tool_results: Whether the transcript windows carry tool output.
        llm: Whether an action Jev does not settle goes to the LLM; off, it is refused.
        second_opinion: Whether one LLM refusal is asked again, on the evidence alone; the action is refused
            only when both askings refuse.
    """

    rules: str
    over: tuple[TCondition, ...] = (UserSaid(), UserAnswered(approved=False))
    contexts: Sequence[PromptContext] = ()
    model: TModel = "small"
    specialty: TSpecialty = "review"
    deadline: float = 20
    transcript: bool | int | Literal["recent", "full"] = False
    root_transcript: bool | int | Literal["recent", "full"] = False
    tool_results: bool = False
    llm: bool = True
    second_opinion: bool = True

    def items(self, ctx: Context) -> list[Evidence]:
        return [item for condition in self.over if (part := found(condition, ctx)) for item in part]

    def evidence(self, ctx: Context) -> list[Evidence] | None:
        if ctx.evt is None:
            raise Undecided("the context carries no event to judge")
        with decision_stage(ctx.kind, "evidence"):
            items = self.items(ctx)
        if not items:
            return None
        verdict = self.verdict(ctx, items)
        if not verdict.allow:
            logger.bind(kind=ctx.kind, reason=verdict.reason).info("the judge refused")
            return None
        return cited(items, verdict.relied_on) or None

    def verdict(self, ctx: Context, items: Sequence[Evidence]) -> Verdict:
        from captain_hook.context import is_context_overflow
        from captain_hook.snapshots.client import EvidenceIncomplete

        assert ctx.evt is not None
        prompt = self.framed(ctx, items, DETAIL_SHOWN)
        left = reqenv.seconds_left()
        try:
            with reqenv.deadline_in(self.deadline if left is None else min(self.deadline, left)):
                if (quick := self.quick(ctx, prompt, items)) is not None:
                    return quick
                if not self.llm:
                    return Verdict(reason=JEV_UNSETTLED, allow=False)
                try:
                    verdict = self.ask(ctx, prompt, transcript=self.transcript, root_transcript=self.root_transcript)
                except Exception as exc:
                    if not is_context_overflow(exc):
                        raise
                    logger.info("the judge's window overflowed; asking again without the transcript")
                    prompt = self.framed(ctx, items, RETRY_DETAIL_SHOWN)
                    verdict = self.ask(ctx, prompt, transcript=False, root_transcript=False)
                if isinstance(verdict, Verdict) and not verdict.allow and self.second_opinion:
                    logger.bind(reason=verdict.reason).info("the judge refused once; asking a second time")
                    verdict = self.ask(ctx, prompt, transcript=False, root_transcript=False)
        except EvidenceIncomplete:
            raise
        except Exception as exc:
            detail = str(exc).partition("\n")[0]
            raise Undecided(f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__) from exc
        if not isinstance(verdict, Verdict):
            raise Undecided(f"{type(verdict).__name__} instead of a verdict")
        return verdict

    def framed(self, ctx: Context, items: Sequence[Evidence], shown: int) -> Prompt:
        return (
            Prompt()
            .system(dedent_text(FRAME))
            .context("rules", self.rules)
            .context("evidence", render_evidence(items, shown))
            .context("action", render_action(ctx.action))
        )

    def ask(
        self,
        ctx: Context,
        prompt: Prompt,
        *,
        transcript: bool | int | Literal["recent", "full"],
        root_transcript: bool | int | Literal["recent", "full"],
    ) -> object:
        from captain_hook.primitives.llm import llm_evaluate

        assert ctx.evt is not None
        with decision_stage(ctx.kind, "llm-with-context"):
            return llm_evaluate(
                ctx.evt,
                prompt,
                Verdict,
                hook=f"tickets:{ctx.kind}",
                contexts=self.contexts,
                specialty=self.specialty,
                model=self.model,
                transcript=transcript,
                root_transcript=root_transcript,
                tool_results=self.tool_results,
                once_per_turn=False,
                evidence=False,
                backend="llm",
            )

    def quick(self, ctx: Context, prompt: Prompt, items: Sequence[Evidence]) -> Verdict | None:
        """Jev's allow on one evidence item, else ``None`` for the LLM."""
        from spawnllm import JEV, DecideError, DecideKeyMissing

        from captain_hook.context import VERDICT_TIMEOUT_SECONDS, record_decide_failure
        from captain_hook.contexts import apply_contexts, with_defaults
        from captain_hook.primitives.llm import VERDICT_STATE_CHARS, verdict_state
        from captain_hook.snapshots.client import EvidenceIncomplete

        assert ctx.evt is not None
        with decision_stage(ctx.kind, "contexts"):
            built = apply_contexts(prompt, ctx.evt, with_defaults(self.contexts))
        if built is None or len(str(Prompt(contexts=built.contexts))) >= VERDICT_STATE_CHARS:
            return None
        try:
            with decision_stage(ctx.kind, "transcript"):
                state = verdict_state(
                    ctx.evt.ctx.transcript_evidence(
                        transcript=self.transcript, tool_results=self.tool_results, root_transcript=self.root_transcript
                    ),
                    built,
                )
            with decision_stage(ctx.kind, "jev"):
                decision = ctx.evt.ctx.decide(
                    state, {"permits": permits_question(items)}, provider=JEV, timeout=VERDICT_TIMEOUT_SECONDS
                )
        except EvidenceIncomplete:
            raise
        except (DecideError, DecideKeyMissing) as exc:
            record_decide_failure("jev", exc, str(ctx.evt.cwd) if ctx.evt.cwd else None)
            if not self.llm:
                raise
            return None
        except Exception:
            if not self.llm:
                raise
            logger.opt(exception=True).warning("jev gave no ticket verdict; asking the llm")
            return None
        return quick_verdict(decision)
