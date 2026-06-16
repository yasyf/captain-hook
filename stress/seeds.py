"""Seed the sandbox's decision ledger and verdict table without LLM calls.

Decision rows mirror what :func:`captain_hook.decisions.record_decision` writes
when a hook fires; verdict injection mirrors the judge's ``persist_verdict``
exactly (same model class, role, and prompt version), so threshold scenarios
exercise the real eligibility SQL rather than a parallel implementation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cc_transcript.decisions import Decision, DecisionLog
from cc_transcript.ids import SessionId, ToolDigest, tool_digest
from cc_transcript.mining.candidates import DedupKey

from captain_hook.review.judge import REVIEW_PROMPT_VERSION, ReviewVerdict

if TYPE_CHECKING:
    from pathlib import Path

    from captain_hook.review.judge import Category
    from captain_hook.review.store import ReviewStore

PRIMITIVE_NUDGE = "/x/site-packages/captain_hook/primitives/nudge.py"
NUDGE_MESSAGE = "Remember to use the project's task tracker before running status checks."
GIT_STATUS_DIGEST = tool_digest("Bash", {"command": "git status"})
INJECTED_MODEL = "stress-injected"


def seed_decision(
    db_path: Path,
    *,
    ts_ms: int,
    session_id: str,
    kind: str = "status_nudge:nudge_c424798f",
    source_file: str = PRIMITIVE_NUDGE,
    event: str = "PreToolUse",
    action: str = "warn",
    message: str | None = NUDGE_MESSAGE,
    digest: ToolDigest | None = GIT_STATUS_DIGEST,
) -> None:
    DecisionLog.open(db_path).append(
        Decision(
            ts_ms=ts_ms,
            session_id=SessionId(session_id),
            source="captain-hook",
            kind=kind,
            source_file=source_file,
            event=event,
            action=action,
            message=message,
            tool_digest=digest,
        )
    )


async def inject_verdict(
    store: ReviewStore,
    dedup_key: str,
    *,
    category: Category,
    confidence: float = 0.9,
    model: str = INJECTED_MODEL,
    fidelity: str = "full",
) -> None:
    await store.record_verdict(
        DedupKey(dedup_key),
        ReviewVerdict(category=category, summary="injected", confidence=confidence, rationale="injected"),
        role="judge",
        prompt_version=REVIEW_PROMPT_VERSION,
        model=model,
        fidelity=fidelity,  # type: ignore[arg-type]
    )
