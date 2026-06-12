"""The reviewer's LLM judge over both candidate kinds: durable corrections and confirmed misfires.

The deterministic scan is tuned for recall; this module supplies the precision.
The :func:`cc_transcript.judge.run_verdicts` mechanism fans a structured judge
over every stored row lacking a verdict at the current prompt version — under
the CREATE taxonomy ("is this correction durable enough to encode as a hook?")
for user-correction rows and the FIX taxonomy (``misfire_confirmed`` /
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

import json
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from cc_transcript.context import ContextWindow, HydratedWindow
from cc_transcript.judge.llm import resolved_model, structured_judge
from cc_transcript.judge.verdicts import run_verdicts
from cc_transcript.mining.candidates import DedupKey
from cc_transcript.mining.confidence import NOISE_FLOOR
from cc_transcript.render import Budget
from pydantic import BaseModel, Field

from captain_hook.review.fix import HOOK_COMPLAINT
from captain_hook.review.store import signal_confidence

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from cc_transcript.activity import Turn
    from cc_transcript.context import Fidelity

    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

REVIEW_PROMPT_VERSION = 2
JUDGE_ROLE = "judge"
TRIGGER_BUDGET = Budget(turn_chars=2000, tool_chars=6000)
CONTEXT_BUDGET = Budget()

DURABLE_CATEGORIES = frozenset({"durable_style_rule", "workflow_rule", "tooling_rule", "safety_guard"})
ACCEPTED_CATEGORIES = DURABLE_CATEGORIES | {"misfire_confirmed"}

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

JUDGE_PROMPT = """\
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

Respond with strict JSON matching the schema — no extra keys, no prose.

[source: {source_kind}]
{context}
=== FEEDBACK TO CLASSIFY ===
{text}"""

FIX_JUDGE_PROMPT = """\
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

[hook: {hook_name} ({event}/{action})]
=== the hook's fire message ===
{fire_message}
{context}
=== REMARK TO CLASSIFY ===
{text}"""


class ReviewVerdict(BaseModel):
    """One judge verdict on a stored correction.

    Attributes:
        category: The single best-fitting durable or non-durable category.
        summary: One neutral sentence naming the rule the feedback implies.
        confidence: The model's probability that its durable-vs-not call is right.
        rationale: One short clause explaining the call.
    """

    category: Category
    summary: str
    confidence: float = Field(ge=0, le=1)
    rationale: str

    @property
    def accepted(self) -> bool:
        """Whether the category marks a durable correction or a confirmed misfire."""
        return self.category in ACCEPTED_CATEGORIES


@dataclass(frozen=True, slots=True)
class JudgeReport:
    """The outcome of one judge pass.

    Attributes:
        judged: How many rows received a verdict this pass.
        failed: How many rows failed (timeout, parse error) and stay pending.
        pending: How many judge-worthy rows remain unjudged after this pass.
    """

    judged: int
    failed: int
    pending: int


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


def build_create_prompt(row: Mapping[str, object], context: str) -> str:
    return JUDGE_PROMPT.format(source_kind=row["source_kind"], context=context, text=row["text"])


def build_fix_prompt(row: Mapping[str, object], context: str) -> str:
    payload: dict[str, object] = json.loads(str(row["payload_json"]))
    return FIX_JUDGE_PROMPT.format(
        hook_name=payload["target_hook_name"],
        event=payload["event"],
        action=payload["action"],
        fire_message=payload["fire_message"],
        context=context,
        text=row["text"],
    )


async def build_prompt(row: Mapping[str, object]) -> tuple[str, Fidelity]:
    """Builds one row's judge prompt, hydrating its context window first.

    Returns:
        The prompt under the row's taxonomy (FIX for ``hook_complaint`` rows,
        CREATE otherwise) and the fidelity its context rendered at.
    """
    context, fidelity = await render_context(ContextWindow.from_json(str(row["context_json"])))
    builder = build_fix_prompt if str(row["source_kind"]) == HOOK_COMPLAINT else build_create_prompt
    return builder(row, context), fidelity


def prompt_builder(fidelities: dict[str, Fidelity]) -> Callable[[Mapping[str, object]], Awaitable[str]]:
    async def build(row: Mapping[str, object]) -> str:
        prompt, fidelity = await build_prompt(row)
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
            prompt_version=REVIEW_PROMPT_VERSION,
            model=model,
            fidelity=fidelities[str(row["dedup_key"])],
        )

    return persist


async def judge_pass(
    store: ReviewStore, *, settings: ReviewSettings, limit: int | None = None, refresh_summary: bool = False
) -> JudgeReport:
    """Judges stored corrections lacking a verdict at :data:`REVIEW_PROMPT_VERSION`.

    Incremental and idempotent: each verdict persists as soon as its call
    completes, a failed row stays unjudged and is retried on the next pass, and
    re-running over a fully judged corpus is a no-op. Rows whose heuristic
    signal confidence sits below :data:`~cc_transcript.mining.NOISE_FLOOR` are
    never sent.

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
        The pass's judged/failed/pending counts over judge-worthy rows.
    """
    model = resolved_model(settings.judge_tier)
    rows = await store.unjudged(
        role=JUDGE_ROLE, prompt_version=REVIEW_PROMPT_VERSION, model=model, refresh_summary=refresh_summary
    )
    worthy = [row for row in rows if signal_confidence(row["payload_json"]) >= NOISE_FLOOR]
    fidelities: dict[str, Fidelity] = {}
    judged, failed = await run_verdicts(
        worthy[: limit if limit is not None else settings.max_judge_calls_per_session],
        prompt_builder(fidelities),
        structured_judge(ReviewVerdict, tier=settings.judge_tier, timeout=settings.judge_timeout),
        persist_verdict(store, model=model, fidelities=fidelities),
        concurrency=settings.judge_concurrency,
    )
    return JudgeReport(judged=judged, failed=failed, pending=len(worthy) - judged)
