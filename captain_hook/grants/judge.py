"""The one grant judge every hook shares: evidence and the proposed action in, a cited verdict out."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from loguru import logger
from pydantic import BaseModel, Field
from spawnllm import Binary, BinaryAnswer, Label, LabelAnswer

from captain_hook.grants import store
from captain_hook.grants.evidence import tree_of
from captain_hook.prompt import Prompt, dedent_text
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from spawnllm import Decision, TModel, TSpecialty

    from captain_hook.contexts import PromptContext
    from captain_hook.events import BaseHookEvent
    from captain_hook.grants.records import Evidence, Proposal
    from captain_hook.grants.rules import Ruling

FRAME = """
    You decide whether the human owner of this coding agent permitted one pending action, in their own
    words. <grant_rules> are the hook's own rules for this kind of action; follow them. <evidence> lists
    the owner's words that may permit it, each under an id: only those count as the owner speaking.
    <proposed_action> is the action, and <rules_evaluated> the deterministic rules already run against it.

    Cite in relied_on the ids of every evidence item your verdict rests on, copied exactly as they
    appear in square brackets (for example "ccn:543e865" or "words:1a2b3c4d5e6f"); an allow that cites
    none is refused. Set standing to the owner's
    exact words, copied verbatim from one evidence item, only when those words permit more than this
    one action (for example "reply in that thread without asking"); otherwise leave it empty. When
    those words name how many such actions they permit ("send these three replies"), also set uses
    to that number; leave uses empty when they set no limit.
    When <widenable_scope> is present, the owner's words may cover more than this action's own scope:
    standing words for every thread of a channel, a ruling for a class of destinations, or one approval
    for several named places. Set scope to the values those words cover for the keys it lists, each a
    list of the exact values the words name, or "*" for every value of that key; keys you leave out keep
    this action's value. Widen only as far as the words reach, and leave scope empty when they name only
    this action's own scope.
    When you refuse, set refusal to one plain sentence for the agent, under 150 characters, with no
    quotation marks, ids, dates, or times: what the owner's words permit and what this action does
    beyond them.
    Reason first, quoting the owner words you relied on, then set allow.
"""
UNPERMITTED = "none"
PERMITTED = 0.8
STANDING = 0.2
JEV_UNSETTLED = "Jev found no single owner approval that permits exactly this action."
STANDING_QUESTION = Binary(
    "Do any of the owner's words in <evidence> permit more than the one action in <proposed_action>: further actions "
    "of its kind, every reply in a thread or channel, a stated number of them, or other places?",
    yes="Some evidence item permits more than this one action.",
    no="No evidence item permits anything beyond this one action.",
)


class GrantVerdict(BaseModel):
    """The judge's answer: whether the owner permitted the action, why, and the evidence it rests on."""

    reason: str
    allow: bool
    relied_on: list[str] = Field(default_factory=list[str])
    standing: str | None = None
    uses: int | None = Field(default=None, ge=1)
    scope: dict[str, str | list[str]] | None = None
    refusal: str = ""

    @property
    def explained(self) -> str:
        """Why the judge refused, in the words meant for the agent."""
        return self.refusal or self.reason


class JudgeFailed(Exception):
    """The judge gave no valid verdict in time; a grant it cannot judge never allows unless its declaration fails open.

    Attributes:
        cause: What stopped the verdict, such as ``TimeoutError``.
    """

    def __init__(self, cause: str) -> None:
        super().__init__(f"the judge gave no verdict ({cause})")
        self.cause = cause


def render_evidence(items: Sequence[Evidence]) -> str | None:
    blocks: list[str] = []
    for item in items:
        said = f" said {store.stamp(item.said_at)}" if item.said_at else ""
        detail = f"\n{item.detail}" if item.detail else ""
        blocks.append(f"[{item.id}] {item.source}{said}{detail}\nowner's words: {item.quote}")
    return "\n\n".join(blocks) or None


def render_action(action: Proposal) -> str:
    payload = json.dumps(dict(action.payload), indent=1, default=str, ensure_ascii=False)
    return f"{action.summary}\nscope: {dict(action.scope)}\npayload: {payload}"


def permits_question(items: Sequence[Evidence]) -> Label:
    return Label(
        "Which one <evidence> item, in the owner's own words and under <grant_rules>, permits exactly the action in "
        "<proposed_action>? Pick none when no single item does, when the action goes beyond the item's words, or when "
        "unsure.",
        {UNPERMITTED: "No single evidence item permits this action.", **dict.fromkeys(item.id for item in items)},
    )


def quick_verdict(decision: Decision) -> GrantVerdict | None:
    """An allow citing the item Jev picked when it is sure that item permits only this action, else ``None``."""
    match decision.answers["permits"], decision.answers["standing"]:
        case LabelAnswer(choice=choice, probabilities=probabilities), BinaryAnswer(p_yes=standing) if (
            choice != UNPERMITTED and probabilities[choice] >= PERMITTED and standing < STANDING
        ):
            return GrantVerdict(
                reason=f"{decision.model} read {choice} as permitting exactly this action",
                allow=True,
                relied_on=[choice],
            )
        case _:
            return None


@dataclass(frozen=True, slots=True)
class Judge:
    """A check that the owner's words permit the action, shared by every grant declaration.

    TypeSafe Jev judges first, citing the one evidence item that permits exactly this action. When Jev is
    sure of that item, no grant rests on it yet, and no item's words reach past this one action, the
    action goes ahead on that citation. Any other answer, a refusal, or a Jev failure asks the LLM, which
    writes the citations, standing words, scope, and refusal, and may still allow. With ``llm`` off, Jev
    alone judges: any other answer refuses, and a Jev failure raises :class:`JudgeFailed`.

    Attributes:
        rules: The hook's rules for this kind of action, in prose.
        contexts: Extra prompt contexts the hook renders, such as a preview diff.
        model: spawnllm model tier; capt-hook's small judge model by default.
        specialty: spawnllm specialty for the call.
        deadline: Seconds the judge may take before it fails.
        transcript: Recent-session window the judge reads, as ``llm_evaluate`` takes it.
        root_transcript: The spawning session's window, for a lane.
        root_excerpt: Maps the event to needles whose root-session mentions the judge reads.
        tool_results: Whether the transcript windows carry tool output.
        llm: Whether an action Jev does not settle goes to the LLM; off, it is refused.
    """

    rules: str
    contexts: Sequence[PromptContext] = ()
    model: TModel = "small"
    specialty: TSpecialty = "review"
    deadline: float = 20
    transcript: bool | int | Literal["recent", "full"] = False
    root_transcript: bool | int | Literal["recent", "full"] = False
    root_excerpt: Callable[[BaseHookEvent], Sequence[str]] | None = None
    tool_results: bool = False
    llm: bool = True

    def __call__(
        self,
        evt: BaseHookEvent,
        *,
        hook: str,
        action: Proposal,
        evidence: Sequence[Evidence],
        rulings: Sequence[Ruling] = (),
        widen: Sequence[str] = (),
    ) -> GrantVerdict:
        from captain_hook.primitives.llm import llm_evaluate
        from captain_hook.snapshots.client import EvidenceIncomplete

        prompt = (
            Prompt()
            .system(dedent_text(FRAME))
            .context("grant_rules", self.rules)
            .context("evidence", render_evidence(evidence))
            .context("proposed_action", render_action(action))
            .context("rules_evaluated", "\n".join(ruling.line() for ruling in rulings) or None)
            .context("widenable_scope", ", ".join(widen) or None)
        )
        left = reqenv.seconds_left()
        try:
            with reqenv.deadline_in(self.deadline if left is None else min(self.deadline, left)):
                if (quick := self.quick(evt, prompt, evidence)) is not None:
                    return quick
                if not self.llm:
                    return GrantVerdict(reason=JEV_UNSETTLED, allow=False)
                verdict = llm_evaluate(
                    evt,
                    prompt,
                    GrantVerdict,
                    hook=hook,
                    contexts=self.contexts,
                    specialty=self.specialty,
                    model=self.model,
                    transcript=self.transcript,
                    root_transcript=self.root_transcript,
                    root_excerpt=self.root_excerpt,
                    tool_results=self.tool_results,
                    once_per_turn=False,
                    evidence=False,
                    backend="llm",
                )
        except EvidenceIncomplete:
            raise
        except Exception as exc:
            detail = str(exc).partition("\n")[0]
            raise JudgeFailed(f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__) from exc
        if not isinstance(verdict, GrantVerdict):
            raise JudgeFailed(f"{type(verdict).__name__} instead of a verdict")
        return verdict

    def quick(self, evt: BaseHookEvent, prompt: Prompt, evidence: Sequence[Evidence]) -> GrantVerdict | None:
        """Jev's allow on one evidence item no grant rests on yet, or ``None`` when the LLM must judge."""
        from spawnllm import JEV, DecideError, DecideKeyMissing

        from captain_hook.context import VERDICT_TIMEOUT_SECONDS, record_decide_failure
        from captain_hook.contexts import apply_contexts, with_defaults
        from captain_hook.primitives.llm import VERDICT_STATE_CHARS, verdict_state
        from captain_hook.snapshots.client import EvidenceIncomplete

        cited = {item.id for grant in store.grants(tree=tree_of(evt)) for item in grant.evidence}
        if not (fresh := [item for item in evidence if item.id not in cited]):
            return None
        built = apply_contexts(prompt, evt, with_defaults(self.contexts))
        if built is None or len(str(Prompt(contexts=built.contexts))) >= VERDICT_STATE_CHARS:
            return None
        try:
            state = verdict_state(
                evt.ctx.transcript_evidence(
                    transcript=self.transcript,
                    tool_results=self.tool_results,
                    root_transcript=self.root_transcript,
                    root_excerpt=self.root_excerpt(evt) if self.root_excerpt else (),
                ),
                built,
            )
            decision = evt.ctx.decide(
                state,
                {"permits": permits_question(fresh), "standing": STANDING_QUESTION},
                provider=JEV,
                timeout=VERDICT_TIMEOUT_SECONDS,
            )
        except EvidenceIncomplete:
            raise
        except (DecideError, DecideKeyMissing) as exc:
            record_decide_failure("jev", exc, str(evt.cwd) if evt.cwd else None)
            if not self.llm:
                raise
            return None
        except Exception:
            if not self.llm:
                raise
            logger.opt(exception=True).warning("jev gave no grant verdict; asking the llm")
            return None
        return quick_verdict(decision)
