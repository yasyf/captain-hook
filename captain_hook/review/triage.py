"""The cheap LLM junk-triage over surviving create candidates, run before the judge pass.

The deterministic scan prefilter — :data:`~captain_hook.review.scan.JUNK_CREATE_GROUPS`
and :func:`~captain_hook.review.scan.is_paste_only` — drops the obvious junk-create leads
at ingest. This pass runs a cheap structured LLM over the survivors (the create feedback
events a watching create candidate still evidences) to catch the junk the regexes can't,
before the judge spends a full verdict call on it. It runs inside the detached reviewer
spawn — never the SessionEnd hook process — records one verdict per dedup key so nothing
re-triages across runs, and rejects any candidate all of whose evidence junk-triaged
without a judge call.

The verdict is biased to keep: a false junk call silently loses real feedback, while a
false keep only defers to the judge, which stays the backstop for everything kept.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cc_transcript.judge.llm import structured_judge
from cc_transcript.judge.verdicts import run_verdicts
from cc_transcript.mining.candidates import DedupKey
from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore


class TriageVerdict(BaseModel):
    """One junk-triage verdict on a surviving create candidate.

    Attributes:
        junk: Whether the text is not actionable developer feedback and should reject
            without a judge call. Biased to ``False`` — only clear junk is dropped.
        reason: One short clause naming why, kept for the spawn log, not persisted.
    """

    junk: bool
    reason: str


@dataclass(frozen=True, slots=True)
class TriageReport:
    """The outcome of one junk-triage pass.

    Attributes:
        triaged: How many events received a triage verdict this pass.
        junk: How many of those verdicts were junk.
        rejected: How many watching create candidates the pass retired (all evidence junk).
    """

    triaged: int
    junk: int
    rejected: int


async def triage_prompt(row: Mapping[str, object]) -> str:
    return (
        "You are pre-screening one message from a Claude Code session to decide whether it is "
        "genuine, actionable developer feedback worth turning into a durable rule, or junk.\n\n"
        "Junk is control chatter and pasted noise that carries no correction, for example: a plan "
        "approval or 'go ahead / begin' directive; a 'continue, limits reset' resume nudge; an agent "
        "lifecycle banner ('Another Claude session sent a message', 'N background agents were "
        "stopped'); a file handoff ('@plan.md pick up where we left off'); or a verbatim paste of "
        "prior output, a command, or a log with no comment of the user's own.\n\n"
        "Genuine feedback states a correction, preference, or instruction the developer wants "
        "followed — even tersely, even riding a quoted lead, even with typos.\n\n"
        "Answer junk=true ONLY when you are confident the text carries no actionable feedback. When "
        "in any doubt, answer junk=false: a downstream judge screens everything you keep, but nothing "
        "recovers what you wrongly drop.\n\n"
        f"MESSAGE:\n{row['text']}"
    )


def structured_triage(settings: ReviewSettings) -> Callable[[str], Awaitable[TriageVerdict]]:
    return structured_judge(TriageVerdict, tier=settings.triage_tier, timeout=settings.judge_timeout)


async def triage_pass(store: ReviewStore, *, settings: ReviewSettings) -> TriageReport:
    """Junk-triages the surviving create feedback events, rejecting the all-junk candidates.

    Fetches up to ``settings.max_triage_calls_per_session`` un-triaged create events still
    evidencing a watching candidate, classifies each on the cheap ``settings.triage_tier``
    backend, records the verdict per dedup key (idempotent, so a re-run never re-triages),
    then retires every candidate all of whose evidence junk-triaged. A failed classify
    call leaves its event un-triaged to retry next pass, and the judge still judges every
    kept event.

    Args:
        store: The open review store.
        settings: The reviewer settings — the triage tier, the per-session cap, and the
            concurrency and timeout it shares with the judge pass.

    Returns:
        The pass's :class:`TriageReport`.
    """
    rows = await store.untriaged_create_events(limit=settings.max_triage_calls_per_session)

    async def persist(row: Mapping[str, object], verdict: TriageVerdict) -> None:
        await store.record_triage(DedupKey(str(row["dedup_key"])), junk=verdict.junk)

    triaged, _ = await run_verdicts(
        rows,
        triage_prompt,
        structured_triage(settings),
        persist,
        concurrency=settings.judge_concurrency,
    )
    junk = len({str(row["dedup_key"]) for row in rows} & await store.junk_triaged_keys())
    return TriageReport(triaged=triaged, junk=junk, rejected=await store.reject_junk_triaged())
