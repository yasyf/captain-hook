"""The scenario registry: every family module contributes its ``scenarios()`` tuple."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stress.scenarios.base import Scenario


def all_scenarios() -> tuple[Scenario, ...]:
    from stress.scenarios import (
        f01_guard,
        f02_gating,
        f03_create,
        f04_fix,
        f05_mtime,
        f07_judge_live,
        f08_thresholds,
        f09_env,
        f10_e2e_live,
        f11_crash,
        f12_concurrency,
        f13_clock,
        f15_pathology,
        f16_selfref,
        f17_judge_stub,
    )

    modules = (
        f01_guard,
        f02_gating,
        f03_create,
        f04_fix,
        f05_mtime,
        f07_judge_live,
        f08_thresholds,
        f09_env,
        f10_e2e_live,
        f11_crash,
        f12_concurrency,
        f13_clock,
        f15_pathology,
        f16_selfref,
        f17_judge_stub,
    )
    return tuple(scenario for module in modules for scenario in module.scenarios())
