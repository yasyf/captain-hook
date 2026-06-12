"""The reviewer's LLM judge: is a stored correction durable enough to encode as a hook?

The deterministic scan is tuned for recall; this module supplies the precision.
The lifted :func:`cc_transcript.domains.mining.run_verdicts` mechanism fans a
structured judge over every stored correction lacking a verdict at the current
prompt version, and each verdict persists idempotently through
:meth:`~captain_hook.review.store.ReviewStore.record_verdict`. Rows whose
heuristic confidence sits below :data:`~cc_transcript.domains.mining.NOISE_FLOOR`
never reach the LLM, and each pass is capped so verdicts amortize per session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cc_transcript.domains.mining.candidates import DedupKey
from cc_transcript.domains.mining.confidence import NOISE_FLOOR
from cc_transcript.domains.mining.context import ContextSnapshot, render_turn, render_turns
from cc_transcript.domains.mining.llm import resolved_model, structured_judge
from cc_transcript.domains.mining.verdicts import run_verdicts
from pydantic import BaseModel, Field

from captain_hook.review.store import signal_confidence

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

REVIEW_PROMPT_VERSION = 1
JUDGE_ROLE = "judge"
TRIGGER_TEXT_LIMIT = 2000

DURABLE_CATEGORIES = frozenset({"durable_style_rule", "workflow_rule", "tooling_rule", "safety_guard"})

Category = Literal[
    "durable_style_rule",
    "workflow_rule",
    "tooling_rule",
    "safety_guard",
    "one_off_correction",
    "task_specific",
    "preference_unclear",
    "ambient_noise",
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
=== conversation before ===
{before}
=== assistant action under review ===
{trigger}
=== FEEDBACK TO CLASSIFY ===
{text}
=== conversation after ===
{after}"""


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
        """Whether the category marks a durable, hook-worthy correction."""
        return self.category in DURABLE_CATEGORIES


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


def build_prompt(row: Mapping[str, object]) -> str:
    ctx = ContextSnapshot.from_json(str(row["context_json"]))
    return JUDGE_PROMPT.format(
        source_kind=row["source_kind"],
        before=render_turns(ctx.before),
        trigger=render_turn(ctx.trigger, TRIGGER_TEXT_LIMIT) if ctx.trigger else "(unknown)",
        text=row["text"],
        after=render_turns(ctx.after),
    )


def persist_verdict(
    store: ReviewStore, *, model: str
) -> Callable[[Mapping[str, object], ReviewVerdict], Awaitable[None]]:
    async def persist(row: Mapping[str, object], verdict: ReviewVerdict) -> None:
        await store.record_verdict(
            DedupKey(str(row["dedup_key"])), verdict, role=JUDGE_ROLE, prompt_version=REVIEW_PROMPT_VERSION, model=model
        )

    return persist


async def judge_pass(store: ReviewStore, *, settings: ReviewSettings, limit: int | None = None) -> JudgeReport:
    """Judges stored corrections lacking a verdict at :data:`REVIEW_PROMPT_VERSION`.

    Incremental and idempotent: each verdict persists as soon as its call
    completes, a failed row stays unjudged and is retried on the next pass, and
    re-running over a fully judged corpus is a no-op. Rows whose heuristic
    :func:`~cc_transcript.domains.mining.effective_confidence` sits below
    :data:`~cc_transcript.domains.mining.NOISE_FLOOR` are never sent.

    Args:
        store: The open review store.
        settings: The judge knobs — tier, timeout, concurrency, and the
            per-session call cap.
        limit: When set, overrides ``settings.max_judge_calls_per_session``
            as this pass's call cap (the manual-backfill path).

    Returns:
        The pass's judged/failed/pending counts over judge-worthy rows.
    """
    model = resolved_model(settings.judge_tier)
    rows = await store.unjudged(role=JUDGE_ROLE, prompt_version=REVIEW_PROMPT_VERSION, model=model)
    worthy = [row for row in rows if signal_confidence(row["payload_json"]) >= NOISE_FLOOR]
    judged, failed = await run_verdicts(
        worthy[: limit if limit is not None else settings.max_judge_calls_per_session],
        build_prompt,
        structured_judge(ReviewVerdict, tier=settings.judge_tier, timeout=settings.judge_timeout),
        persist_verdict(store, model=model),
        concurrency=settings.judge_concurrency,
    )
    return JudgeReport(judged=judged, failed=failed, pending=len(worthy) - judged)
