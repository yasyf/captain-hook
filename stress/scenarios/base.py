"""Scenario primitives: a named check list with raw evidence, never prose."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from stress.sandbox import Sandbox


class Tier(StrEnum):
    OFFLINE = "offline"
    JUDGE = "judge"
    BRAIN = "brain"


@dataclass(frozen=True, slots=True)
class Check:
    """One machine-checked assertion.

    Attributes:
        name: What was checked.
        ok: Whether the observation matched the expectation.
        evidence: The raw observation — a row dump, log excerpt, or exit code.
    """

    name: str
    ok: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    checks: tuple[Check, ...]
    finding: str | None = None

    @property
    def passed(self) -> bool:
        return all(check.ok for check in self.checks)


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    family: str
    tier: Tier
    run: Callable[[Sandbox], ScenarioResult]
    env_overrides: dict[str, str] = field(default_factory=dict)


def check(name: str, ok: bool, evidence: object) -> Check:
    return Check(name, ok, str(evidence)[:2000])


def expect(name: str, actual: object, expected: object) -> Check:
    return check(f"{name} == {expected!r}", actual == expected, f"actual={actual!r}")
