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
Each pass closes by sweeping verdicts a lane was bumped past through
:meth:`~captain_hook.review.store.ReviewStore.purge_stale_verdicts`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, Self

import anyio.to_thread
from cc_transcript.context import ContextWindow, HydratedWindow
from cc_transcript.judge.llm import resolved_model, structured_judge
from cc_transcript.judge.verdicts import SLUG_PATTERN, JudgeError, canonical_slug, run_verdicts
from cc_transcript.mining.candidates import DedupKey
from cc_transcript.mining.sourcekind import QUESTION_ANSWER
from cc_transcript.render import Budget
from pydantic import BaseModel, Field, field_validator, model_validator

from captain_hook.review.fix import HOOK_COMPLAINT
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
FIX_CATEGORIES = frozenset({"misfire_confirmed", "compliance", "ambient_mention"})

Category = Literal[
    "durable_style_rule",
    "workflow_rule",
    "tooling_rule",
    "safety_guard",
    "one_off_correction",
    "task_specific",
    "preference_unclear",
    "ambient_noise",
    "misfire_confirmed",
    "compliance",
    "ambient_mention",
]


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
        purged: How many stale-version verdict rows the closing sweep deleted —
            rows recorded at a version their lane no longer runs.
    """

    judged: int
    failed: int
    pending: int
    merged: int
    retired: int
    purged: int


def section(window: ContextWindow, label: str, turns: tuple[Turn, ...], budget: Budget) -> str:
    return f"=== {label} ===\n" + (HydratedWindow(window=window, turns=turns).render(budget=budget) or "(none)")


async def render_context(window: ContextWindow) -> tuple[str, Fidelity]:
    """Renders a row's window for a prompt, at the best fidelity available.

    While the transcript lives, the window hydrates and renders at full fidelity —
    the trigger turn under the generous :data:`TRIGGER_BUDGET`, the surrounding
    turns under the moderate :data:`CONTEXT_BUDGET`. Once it expires (or any ref
    was compacted away), the persisted previews render instead, led by the
    built-in summary-fidelity label.

    Returns:
        The rendered context and the fidelity it was rendered at.
    """
    if (hydrated := await window.hydrate()) is None:
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
    return f"""\
You are auditing one piece of feedback a developer gave an AI coding assistant
(Claude), deciding whether it is a DURABLE correction worth encoding as an
automated hook — a rule that should fire in every future session of this
repository — or feedback that only mattered in the moment.

Pick exactly one category:
- durable_style_rule: a standing code-style or API-design rule ("never use a
  bare except", "always frozen dataclasses") that future code must follow.
- workflow_rule: a standing rule about process — how to plan, commit, test,
  review, or communicate ("always run the tests before claiming done").
- tooling_rule: a standing rule about which tool or command to use ("use uv,
  not pip", "search with rg, not grep").
- safety_guard: a standing guard against a dangerous action ("never force-push
  to main", "never edit generated files").
- one_off_correction: fixes the assistant's current output without stating a
  reusable rule ("rename this one", "the test you broke is test_foo").
- task_specific: a rule scoped to the current task or file, not the repository
  ("for this migration keep both columns").
- preference_unclear: corrective in tone, but the underlying rule cannot be
  stated precisely enough to automate.
- ambient_noise: not corrective at all — status updates, questions, new tasks.

The first four categories are durable; the rest are not. A durable correction
states (or clearly implies) a rule that would be violated again and could be
checked mechanically. Words like "always", "never", or "stop doing X" are
strong durability signals; a rule that names one specific line, variable, or
test is task-scoped, not durable.

summary: ONE neutral sentence naming the rule the feedback implies (or what the
user reacted to when there is no rule). Write it for every category.
confidence: your probability (0 to 1) that your durable-vs-not call is correct.
rationale: one short clause.
rule_slug: a canonical kebab-case name for the rule, 2-6 words (e.g.
"never-bare-except", "prefer-uv-over-pip"). Reuse a suggested slug VERBATIM if
this feedback states the same underlying rule — even paraphrased, misspelled, or
captured by a different detector; mint a new slug only for a genuinely new rule;
if several fit, reuse the first listed. null for every non-durable category.

Suggested slugs (existing durable rules, most similar first):
{render_suggestions(suggestions)}

Respond with strict JSON matching the schema — no extra keys, no prose.

[source: {row["source_kind"]}]
{context}
{question_answer_block(row)}=== FEEDBACK TO CLASSIFY ===
{row["text"]}"""


def build_fix_prompt(row: Mapping[str, object], context: str) -> str:
    payload: dict[str, object] = json.loads(str(row["payload_json"]))
    return f"""\
You are auditing one remark an AI coding assistant (Claude) made about an
automated hook that fired during its session, deciding whether the remark
REPORTS A MISFIRE — the hook firing wrongly or redundantly — or something else.

Pick exactly one category:
- misfire_confirmed: the remark asserts the hook fired wrongly — it re-fired on
  content already addressed, flagged a false positive, or fired outside its
  intended scope — and the surrounding conversation is consistent with that
  claim.
- compliance: the remark acknowledges the hook's message and follows it (or
  promises to follow it going forward).
- ambient_mention: the hook is merely described, quoted, or referenced in
  passing, with no claim that it fired wrongly.

Only misfire_confirmed marks the hook as worth amending. A remark that both
complies and dismisses ("noted, but this re-fired on text I already fixed") is
misfire_confirmed — the dismissal is the signal. A remark that merely reports
the hook fired, or works around it without disputing it, is not.

summary: ONE neutral sentence naming what the hook did and what Claude claims
about it. Write it for every category.
confidence: your probability (0 to 1) that your misfire-vs-not call is correct.
rationale: one short clause.

Respond with strict JSON matching the schema — no extra keys, no prose.

[hook: {payload["target_hook_name"]} ({payload["event"]}/{payload["action"]})]
=== the hook's fire message ===
{payload["fire_message"]}
{context}
=== REMARK TO CLASSIFY ===
{row["text"]}"""


async def build_prompt(row: Mapping[str, object], *, suggestions: Sequence[Suggestion] = ()) -> tuple[str, Fidelity]:
    """Builds one row's judge prompt, hydrating its context window first.

    Returns:
        The prompt under the row's taxonomy (FIX for ``hook_complaint`` rows,
        CREATE otherwise, the latter carrying the suggested slugs) and the
        fidelity its context rendered at.
    """
    context, fidelity = await render_context(ContextWindow.from_json(str(row["context_json"])))
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
            ranked = await suggest_canonical_keys(store, str(row["text"]), prompt_version=store.versions.create, k=5)
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
        purged count from the closing
        :meth:`~captain_hook.review.store.ReviewStore.purge_stale_verdicts` sweep.
    """
    from cc_transcript.judge.similar import default_embedder

    model = resolved_model(settings.judge_tier)
    rows = await store.judge_queue(refresh_summary=refresh_summary)
    worthy = [row for row in rows if judge_worthy(row)]
    dispatch = worthy[: limit if limit is not None else settings.max_judge_calls_per_session]
    fidelities: dict[str, Fidelity] = {}
    suggesting = await store.has_verdict_evidence()
    if suggesting or any(str(row["source_kind"]) != HOOK_COMPLAINT for row in dispatch):
        await anyio.to_thread.run_sync(default_embedder)
    judged, failed = await run_verdicts(
        dispatch,
        prompt_builder(fidelities, store, suggesting=suggesting),
        structured_judge(ReviewVerdict, tier=settings.judge_tier, timeout=settings.judge_timeout),
        persist_verdict(store, model=model, fidelities=fidelities),
        concurrency=settings.judge_concurrency,
    )
    merged, retired = await store.regroup_create()
    purged = await store.purge_stale_verdicts()
    return JudgeReport(
        judged=judged, failed=failed, pending=len(worthy) - judged, merged=merged, retired=retired, purged=purged
    )
