"""The reviewer's LLM judge over both candidate kinds: durable corrections and confirmed misfires.

The deterministic scan is tuned for recall; this module supplies the precision.
The :func:`cc_transcript.judge.run_verdicts` mechanism fans a structured judge
over every stored row lacking a verdict at its taxonomy's bound prompt version
(the store's :class:`~captain_hook.review.store.PromptVersions`) — under the
CREATE taxonomy ("is this correction durable enough to encode as a hook?") for
user-correction rows and the FIX taxonomy (``misfire_confirmed`` /
``compliance`` / ``ambient_mention``) for ``hook_complaint`` rows — and each
verdict persists idempotently through
:meth:`~captain_hook.review.store.ReviewStore.record_verdict`. Prompts render
each row's :class:`~cc_transcript.context.ContextWindow` at full fidelity while
the transcript lives and fall back to the labeled summary previews once it
expires; each verdict records the fidelity it was judged at. Rows whose
heuristic confidence sits below :data:`~cc_transcript.mining.NOISE_FLOOR`
never reach the LLM, and each pass is capped so verdicts amortize per session.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Self, get_args

from cc_transcript.context import ContextWindow, HydratedWindow
from cc_transcript.judge.llm import resolved_model, structured_judge
from cc_transcript.judge.verdicts import SLUG_PATTERN, JudgeError, canonical_slug, run_verdicts
from cc_transcript.mining.candidates import DedupKey
from cc_transcript.mining.sourcekind import QUESTION_ANSWER
from cc_transcript.render import Budget
from pydantic import BaseModel, Field, field_validator, model_validator

from captain_hook.review.fix import HOOK_COMPLAINT
from captain_hook.review.prompts import CREATE_TEMPLATE, FIX_TEMPLATE, Category
from captain_hook.review.store import judge_worthy

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from cc_transcript.activity import Turn
    from cc_transcript.context import Fidelity
    from cc_transcript.judge.similar import Suggestion

    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

JUDGE_ROLE = "judge"
TRIGGER_BUDGET = Budget(turn_chars=2000, tool_chars=6000)
CONTEXT_BUDGET = Budget()

DURABLE_CATEGORIES = frozenset({"durable_style_rule", "workflow_rule", "tooling_rule", "safety_guard"})
assert DURABLE_CATEGORIES <= frozenset(get_args(Category))


class ReviewVerdict(BaseModel):
    """One judge verdict on a stored correction.

    Attributes:
        category: The single best-fitting durable or non-durable category.
        summary: One neutral sentence naming the rule the feedback implies.
        confidence: The model's probability that its durable-vs-not call is right.
        rationale: One short clause explaining the call.
        rule_slug: The durable rule's canonical kebab-case key, or ``None`` for a
            non-durable category. A supplied string is normalized through
            :func:`~cc_transcript.judge.canonical_slug` and must then match
            :data:`~cc_transcript.judge.SLUG_PATTERN`; every durable category
            requires one and every non-durable category must omit it.
    """

    category: Category
    summary: str
    confidence: float = Field(ge=0, le=1)
    rationale: str
    rule_slug: str | None = None

    @field_validator("rule_slug", mode="before")
    @classmethod
    def normalize_slug(cls, value: object) -> str | None:
        if value is None:
            return None
        if SLUG_PATTERN.fullmatch(slug := canonical_slug(str(value))):
            return slug
        raise ValueError(f"rule_slug {value!r} does not normalize to a canonical slug")

    @model_validator(mode="after")
    def enforce_slug_durability(self) -> Self:
        durable = self.category in DURABLE_CATEGORIES
        if durable and self.rule_slug is None:
            raise ValueError(f"durable category {self.category!r} requires a rule_slug")
        if not durable and self.rule_slug is not None:
            raise ValueError(f"non-durable category {self.category!r} must not carry rule_slug {self.rule_slug!r}")
        return self

    @property
    def accepted(self) -> bool:
        """Whether the category marks a durable correction or a confirmed misfire."""
        return self.category in DURABLE_CATEGORIES | {"misfire_confirmed"}

    @property
    def canonical_key(self) -> str | None:
        """The durable rule's canonical key — :attr:`rule_slug` under the alias ``record_verdict`` reads."""
        return self.rule_slug


@dataclass(frozen=True, slots=True)
class JudgeReport:
    """The outcome of one judge pass.

    Attributes:
        judged: How many rows received a verdict this pass.
        failed: How many rows failed (timeout, parse error) and stay pending.
        pending: How many judge-worthy rows remain unjudged after this pass.
        merged: How many observations the closing regroup re-parented onto their
            durable slug candidate.
        retired: How many watching create candidates the closing regroup rejected
            (every observation judged, none accepted).
        reopened: How many accepted fix candidates the closing pass returned to
            watching because their merged fix misfired again.
    """

    judged: int
    failed: int
    pending: int
    merged: int
    retired: int
    reopened: int


def section(window: ContextWindow, label: str, turns: tuple[Turn, ...], budget: Budget) -> str:
    return f"=== {label} ===\n" + (HydratedWindow(window=window, turns=turns).render(budget=budget) or "(none)")


def render_context(window: ContextWindow) -> tuple[str, Fidelity]:
    """Renders a row's window for a prompt, at the best fidelity available.

    While the transcript lives, the window hydrates and renders at full fidelity —
    the trigger turn under the generous :data:`TRIGGER_BUDGET`, the surrounding
    turns under the moderate :data:`CONTEXT_BUDGET`. Once it expires (or any ref
    was compacted away), the persisted previews render instead, led by the
    built-in summary-fidelity label.

    Returns:
        The rendered context and the fidelity it was rendered at.
    """
    if (hydrated := window.hydrate()) is None:
        return replace(window, fidelity="summary").render_preview(budget=CONTEXT_BUDGET), "summary"
    split = len(window.before)
    end = split + (window.trigger is not None)
    return (
        "\n".join(
            (
                section(window, "conversation before", hydrated.turns[:split], CONTEXT_BUDGET),
                section(window, "the turn the feedback arrived in", hydrated.turns[split:end], TRIGGER_BUDGET),
                section(window, "conversation after", hydrated.turns[end:], CONTEXT_BUDGET),
            )
        ),
        "full",
    )


def question_answer_block(row: Mapping[str, object]) -> str:
    if str(row["source_kind"]) != QUESTION_ANSWER:
        return ""
    payload: dict[str, object] = json.loads(str(row["payload_json"]))
    match payload["picked_labels"]:
        case []:
            resolved = "The developer picked none of the offered options and wrote a freeform answer."
        case [*labels]:
            choice = "options" if payload["multi_select"] else "option"
            standing = (
                "which is the option the assistant marked (Recommended)"
                if payload["recommended_pick"]
                else "which is not the recommended option"
            )
            resolved = f"The answer resolves to the {choice} {'; '.join(map(str, labels))} — {standing}."
    return (
        f"=== QUESTION THE ASSISTANT ASKED ===\n{payload['question']}\n{resolved}\n"
        "The feedback below is the developer's answer to that question.\n"
    )


def render_suggestions(suggestions: Sequence[Suggestion]) -> str:
    return (
        "\n".join(f'- {s.canonical_key} ({s.score:.2f}) — "{s.sentences[0]}"' for s in suggestions) or "(none similar)"
    )


def build_create_prompt(row: Mapping[str, object], context: str, suggestions: Sequence[Suggestion] = ()) -> str:
    return CREATE_TEMPLATE.format(
        suggestions=render_suggestions(suggestions),
        source_kind=row["source_kind"],
        context=context,
        question_answer=question_answer_block(row),
        text=row["text"],
    )


def build_fix_prompt(row: Mapping[str, object], context: str) -> str:
    payload: dict[str, object] = json.loads(str(row["payload_json"]))
    return FIX_TEMPLATE.format(
        target_hook_name=payload["target_hook_name"],
        event=payload["event"],
        action=payload["action"],
        fire_message=payload["fire_message"],
        context=context,
        text=row["text"],
    )


async def build_prompt(row: Mapping[str, object], *, suggestions: Sequence[Suggestion] = ()) -> tuple[str, Fidelity]:
    """Builds one row's judge prompt, hydrating its context window first.

    Returns:
        The prompt under the row's taxonomy (FIX for ``hook_complaint`` rows,
        CREATE otherwise, the latter carrying the suggested slugs) and the
        fidelity its context rendered at.
    """
    context, fidelity = render_context(ContextWindow.from_json(str(row["context_json"])))
    if str(row["source_kind"]) == HOOK_COMPLAINT:
        return build_fix_prompt(row, context), fidelity
    return build_create_prompt(row, context, suggestions), fidelity


def prompt_builder(
    fidelities: dict[str, Fidelity], store: ReviewStore, *, suggesting: bool
) -> Callable[[Mapping[str, object]], Awaitable[str]]:
    from cc_transcript.judge.similar import suggest_canonical_keys

    async def suggestions_for(row: Mapping[str, object]) -> Sequence[Suggestion]:
        if not suggesting or str(row["source_kind"]) == HOOK_COMPLAINT:
            return ()
        try:
            ranked = await suggest_canonical_keys(store.db, str(row["text"]), prompt_version=store.versions.create, k=5)
        except Exception as exc:
            raise JudgeError(f"slug suggestion retrieval failed: {exc}") from exc
        return [suggestion for suggestion in ranked if SLUG_PATTERN.fullmatch(suggestion.canonical_key)]

    async def build(row: Mapping[str, object]) -> str:
        prompt, fidelity = await build_prompt(row, suggestions=await suggestions_for(row))
        fidelities[str(row["dedup_key"])] = fidelity
        return prompt

    return build


def persist_verdict(
    store: ReviewStore, *, model: str, fidelities: Mapping[str, Fidelity]
) -> Callable[[Mapping[str, object], ReviewVerdict], Awaitable[None]]:
    async def persist(row: Mapping[str, object], verdict: ReviewVerdict) -> None:
        await store.record_verdict(
            DedupKey(str(row["dedup_key"])),
            verdict,
            role=JUDGE_ROLE,
            prompt_version=store.versions.for_row(row),
            model=model,
            fidelity=fidelities[str(row["dedup_key"])],
        )

    return persist


async def judge_pass(
    store: ReviewStore, *, settings: ReviewSettings, limit: int | None = None, refresh_summary: bool = False
) -> JudgeReport:
    """Judges stored corrections lacking a verdict at their taxonomy's bound prompt version.

    The version comes from the store's
    :class:`~captain_hook.review.store.PromptVersions` — the create lane for
    user-correction rows, the fix lane for ``hook_complaint`` rows — fetched as
    one queue by :meth:`~captain_hook.review.store.ReviewStore.judge_queue`.

    Incremental and idempotent: each verdict persists as soon as its call
    completes, a failed row stays unjudged and is retried on the next pass, and
    re-running over a fully judged corpus is a no-op. Rows whose heuristic
    signal confidence sits below :data:`~cc_transcript.mining.NOISE_FLOOR` are
    never sent. Slug suggestions are drawn only when prior evidence exists, and a
    per-row retrieval failure surfaces as a
    :class:`~cc_transcript.judge.JudgeError`, counting the row failed for the next
    pass rather than cancelling this one. The static embedder pre-warms off the
    event loop whenever suggestions are drawn or the dispatched slice holds a
    create row that could yield a durable verdict — each such verdict embeds its
    evidence through the process-wide cached loader, which is not single-flight —
    so only a fix-only or empty pass skips it entirely.

    Args:
        store: The open review store.
        settings: The judge knobs — tier, timeout, concurrency, and the
            per-session call cap.
        limit: When set, overrides ``settings.max_judge_calls_per_session``
            as this pass's call cap (the manual-backfill path).
        refresh_summary: When True, also re-judge rows whose verdict was
            recorded at summary fidelity; a full-fidelity verdict replaces the
            summary one once the row's window hydrates again.

    Returns:
        The pass's judged/failed/pending counts over judge-worthy rows, the
        merged/retired counts from the closing
        :meth:`~captain_hook.review.store.ReviewStore.regroup_create`, and the
        reopened count from
        :meth:`~captain_hook.review.store.ReviewStore.reopen_recurrent_fixes`.
    """
    from cc_transcript.judge.similar import default_embedder

    model = resolved_model(settings.judge_tier)
    rows = await store.judge_queue(refresh_summary=refresh_summary)
    worthy = [row for row in rows if judge_worthy(row)]
    dispatch = worthy[: limit if limit is not None else settings.max_judge_calls_per_session]
    fidelities: dict[str, Fidelity] = {}
    suggesting = await store.has_verdict_evidence()
    if suggesting or any(str(row["source_kind"]) != HOOK_COMPLAINT for row in dispatch):
        await asyncio.to_thread(default_embedder)
    judged, failed = await run_verdicts(
        dispatch,
        prompt_builder(fidelities, store, suggesting=suggesting),
        structured_judge(ReviewVerdict, tier=settings.judge_tier, timeout=settings.judge_timeout),
        persist_verdict(store, model=model, fidelities=fidelities),
        concurrency=settings.judge_concurrency,
    )
    await store.revive_junk_rejected()
    merged, retired = await store.regroup_create()
    reopened = await store.reopen_recurrent_fixes()
    return JudgeReport(
        judged=judged, failed=failed, pending=len(worthy) - judged, merged=merged, retired=retired, reopened=reopened
    )
