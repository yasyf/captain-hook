"""Settings for the SessionEnd session reviewer."""

from __future__ import annotations

from pathlib import Path

from cc_transcript.mining.confidence import MEDIUM, VERY_HIGH, Confidence
from pydantic import Field
from pydantic_settings import SettingsConfigDict
from spawnllm import TModel

from captain_hook.settings import HooksSettings, resolve_state_dir


def resolve_review_db_path() -> Path:
    return resolve_state_dir() / "review" / "review.db"


class ReviewSettings(HooksSettings):
    """Session-reviewer settings, backed by environment variables with ``HOOKS_REVIEW_`` prefix.

    The threshold knobs gate when a candidate becomes PR-eligible (the ``*_fix``
    variants apply to hook-misfire fix candidates), the judge knobs bound the
    per-session LLM verdict pass, and ``brain_max_turns``/``brain_max_budget_usd``
    cap the headless PR-drafting agent.
    """

    model_config = SettingsConfigDict(env_prefix="HOOKS_REVIEW_")

    min_sessions: int = 3
    min_days: int = 2
    max_open_prs: int = 2
    stale_after_days: int = 30
    min_confidence: Confidence = MEDIUM
    min_sessions_fix: int = 2
    min_days_fix: int = 0
    min_confidence_fix: Confidence = MEDIUM
    min_confidence_fix_single: Confidence = VERY_HIGH
    judge_tier: TModel = "medium"
    judge_concurrency: int = 8
    judge_timeout: int = 180
    min_judge_confidence: float = 0.6
    max_judge_calls_per_session: int = 40
    brain_max_turns: int = 80
    brain_max_budget_usd: float = 5.0
    db_path: Path = Field(default_factory=resolve_review_db_path)
