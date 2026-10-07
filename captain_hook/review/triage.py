"""The cheap junk-triage over surviving create candidates, run before the judge pass.

The deterministic scan prefilter — :data:`~captain_hook.review.scan.JUNK_CREATE_GROUPS`
and :func:`~captain_hook.review.scan.is_paste_only` — drops the obvious junk-create leads
at ingest. This pass asks TypeSafe Jev one ``Label`` question per survivor (the create
feedback events a watching create candidate still evidences) to catch the junk the regexes
can't, before the judge spends a full verdict call on it. It runs inside the detached
reviewer spawn — never the SessionEnd hook process — records one verdict per dedup key so
nothing re-triages across runs, and rejects any candidate all of whose evidence
junk-triaged without a judge call.

The verdict is biased to keep: a false junk call silently loses real feedback, while a
false keep only defers to the judge, which stays the backstop for everything kept. Only a
message Jev places below even odds of being feedback is junk, and a refused question keeps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from cc_transcript.judge.verdicts import JudgeError, run_verdicts
from cc_transcript.mining.candidates import DedupKey
from spawnllm import JEV, DecideError, DecideKeyMissing, Label, LabelAnswer, decide

if TYPE_CHECKING:
    from collections.abc import Mapping

    from captain_hook.review.settings import ReviewSettings
    from captain_hook.review.store import ReviewStore

FEEDBACK = "feedback"
JUNK_BELOW = 0.5
KIND = Label(
    "What is this message a developer typed into a coding agent session?",
    {
        FEEDBACK: "The developer's own words correct the agent, complain about how it works, or say how to do "
        "the work: a method, tool, branch, order, scope, or constraint",
        "go_ahead": "Only approves, picks an option, or says go, begin, continue, merge, or deploy, adding nothing "
        "about how to do it",
        "status": "Only asks for or reports status",
        "paste": "Pasted output, logs, alerts, commands, or bot text with no comment of the developer's own",
        "banner": "An agent lifecycle banner, a resume nudge after a limit reset, or a file handoff",
    },
)


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


async def message(row: Mapping[str, object]) -> str:
    return str(row["text"])


async def is_junk(text: str) -> bool:
    try:
        decision = await decide(text, {"kind": KIND}, provider=JEV)
    except (DecideError, DecideKeyMissing, TimeoutError) as exc:
        raise JudgeError(str(exc)) from exc
    match decision.answers["kind"]:
        case LabelAnswer(probabilities=probabilities):
            return probabilities[FEEDBACK] < JUNK_BELOW
        case _:
            return False


async def triage_pass(store: ReviewStore, *, settings: ReviewSettings) -> TriageReport:
    """Junk-triages the surviving create feedback events, rejecting the all-junk candidates.

    Fetches up to ``settings.max_triage_calls_per_session`` un-triaged create events still
    evidencing a watching candidate, asks Jev what kind of message each is, records the
    verdict per dedup key (idempotent, so a re-run never re-triages), then retires every
    candidate all of whose evidence junk-triaged. A failed decision call leaves its event
    un-triaged to retry next pass, and the judge still judges every kept event.

    Args:
        store: The open review store.
        settings: The reviewer settings — the per-session cap and the concurrency it shares
            with the judge pass.

    Returns:
        The pass's :class:`TriageReport`.
    """
    rows = await store.untriaged_create_events(limit=settings.max_triage_calls_per_session)

    async def persist(row: Mapping[str, object], junk: bool) -> None:
        await store.record_triage(DedupKey(str(row["dedup_key"])), junk=junk)

    triaged, _ = await run_verdicts(rows, message, is_junk, persist, concurrency=settings.judge_concurrency)
    junk = len({str(row["dedup_key"]) for row in rows} & await store.junk_triaged_keys())
    return TriageReport(triaged=triaged, junk=junk, rejected=await store.reject_junk_triaged())
