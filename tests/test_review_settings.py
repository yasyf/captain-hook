from __future__ import annotations

from pathlib import Path

import pytest
from cc_transcript.mining import MEDIUM, VERY_HIGH

from captain_hook.review.settings import ReviewSettings


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        pytest.param("min_sessions", 3, id="min_sessions"),
        pytest.param("min_days", 2, id="min_days"),
        pytest.param("max_open_prs", 2, id="max_open_prs"),
        pytest.param("stale_after_days", 30, id="stale_after_days"),
        pytest.param("min_confidence", MEDIUM, id="min_confidence"),
        pytest.param("min_sessions_fix", 2, id="min_sessions_fix"),
        pytest.param("min_days_fix", 0, id="min_days_fix"),
        pytest.param("min_confidence_fix", MEDIUM, id="min_confidence_fix"),
        pytest.param("min_confidence_fix_single", VERY_HIGH, id="min_confidence_fix_single"),
        pytest.param("judge_tier", "small", id="judge_tier"),
        pytest.param("judge_concurrency", 8, id="judge_concurrency"),
        pytest.param("judge_timeout", 180, id="judge_timeout"),
        pytest.param("min_judge_confidence", 0.6, id="min_judge_confidence"),
        pytest.param("max_judge_calls_per_session", 40, id="max_judge_calls_per_session"),
        pytest.param("brain_max_turns", 80, id="brain_max_turns"),
        pytest.param("brain_max_budget_usd", 5.0, id="brain_max_budget_usd"),
    ],
)
def test_defaults(field: str, expected: object) -> None:
    assert getattr(ReviewSettings(), field) == expected


def test_subclasses_hooks_settings() -> None:
    from captain_hook.settings import HooksSettings

    assert issubclass(ReviewSettings, HooksSettings)
    assert "Explore" in ReviewSettings().planning_agents


@pytest.mark.parametrize(
    ("env", "value", "field", "expected"),
    [
        pytest.param("HOOKS_REVIEW_MIN_SESSIONS", "5", "min_sessions", 5, id="int-knob"),
        pytest.param("HOOKS_REVIEW_MIN_JUDGE_CONFIDENCE", "0.8", "min_judge_confidence", 0.8, id="float-knob"),
        pytest.param("HOOKS_REVIEW_MIN_CONFIDENCE", "0.95", "min_confidence", 0.95, id="confidence-knob"),
        pytest.param("HOOKS_REVIEW_JUDGE_TIER", "large", "judge_tier", "large", id="str-knob"),
    ],
)
def test_env_prefix_overrides(
    monkeypatch: pytest.MonkeyPatch, env: str, value: str, field: str, expected: object
) -> None:
    monkeypatch.setenv(env, value)
    assert getattr(ReviewSettings(), field) == expected


def test_unprefixed_env_does_not_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOOKS_MIN_SESSIONS", "9")
    assert ReviewSettings().min_sessions == 3


def test_db_path_defaults_under_state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CAPTAIN_HOOK_STATE_DIR", str(tmp_path))
    assert ReviewSettings().db_path == tmp_path / "review" / "review.db"
