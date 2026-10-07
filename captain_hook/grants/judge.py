"""The one grant judge every hook shares: evidence and the proposed action in, a cited verdict out."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from captain_hook.grants.store import stamp
from captain_hook.prompt import Prompt, dedent_text
from captain_hook.util import reqenv

if TYPE_CHECKING:
    from spawnllm import TModel, TSpecialty

    from captain_hook.contexts import PromptContext
    from captain_hook.events import BaseHookEvent
    from captain_hook.grants.records import Evidence, Grant, Proposal
    from captain_hook.grants.rules import Ruling

FRAME = """
    You decide whether the human owner of this coding agent permitted one pending action, in their own
    words. <grant_rules> are the hook's own rules for this kind of action; follow them. <evidence> lists
    the owner's words that may permit it, each under an id: only those count as the owner speaking.
    <grant>, when present, is a permission already recorded from the owner's words; <owner_since_grant>
    lists what the owner said after it was recorded, which can narrow it. <proposed_action>
    is the action, and <rules_evaluated> the deterministic rules already run against it.

    Cite in relied_on the ids of every evidence item your verdict rests on, copied exactly as they
    appear in square brackets (for example "ccn:543e865" or "words:1a2b3c4d5e6f"); an allow that cites
    none is refused. Refuse when the owner's words in <owner_since_grant> narrow the recorded grant so
    that it no longer covers this action; only the owner revokes a grant. Set standing to the owner's
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
        said = f" said {stamp(item.said_at)}" if item.said_at else ""
        detail = f"\n{item.detail}" if item.detail else ""
        blocks.append(f"[{item.id}] {item.source}{said}{detail}\nowner's words: {item.quote}")
    return "\n\n".join(blocks) or None


def render_action(action: Proposal) -> str:
    payload = json.dumps(dict(action.payload), indent=1, default=str, ensure_ascii=False)
    return f"{action.summary}\nscope: {dict(action.scope)}\npayload: {payload}"


def render_grant(grant: Grant | None) -> str | None:
    if grant is None:
        return None
    uses = "unlimited" if grant.uses is None else f"{grant.uses} use(s)"
    approved = json.dumps(grant.approved, default=str, ensure_ascii=False) if grant.approved else "none"
    return f"grant {grant.id}, {uses}, scope {grant.scope}, approved payload: {approved}\n" + (
        render_evidence(grant.evidence) or ""
    )


@dataclass(frozen=True, slots=True)
class Judge:
    """An LLM check that the owner's words permit the action, shared by every grant declaration.

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

    def __call__(
        self,
        evt: BaseHookEvent,
        *,
        hook: str,
        action: Proposal,
        evidence: Sequence[Evidence],
        rulings: Sequence[Ruling],
        grant: Grant | None = None,
        widen: Sequence[str] = (),
    ) -> GrantVerdict:
        from captain_hook.primitives.llm import llm_evaluate
        from captain_hook.snapshots.client import EvidenceIncomplete

        prompt = (
            Prompt()
            .system(dedent_text(FRAME))
            .context("grant_rules", self.rules)
            .context("grant", render_grant(grant))
            .context("owner_since_grant" if grant is not None else "evidence", render_evidence(evidence))
            .context("proposed_action", render_action(action))
            .context("rules_evaluated", "\n".join(ruling.line() for ruling in rulings) or None)
            .context("widenable_scope", ", ".join(widen) or None)
        )
        left = reqenv.seconds_left()
        try:
            with reqenv.deadline_in(self.deadline if left is None else min(self.deadline, left)):
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
                )
        except EvidenceIncomplete:
            raise
        except Exception as exc:
            detail = str(exc).partition("\n")[0]
            raise JudgeFailed(f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__) from exc
        if not isinstance(verdict, GrantVerdict):
            raise JudgeFailed(f"{type(verdict).__name__} instead of a verdict")
        return verdict
