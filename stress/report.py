"""Markdown run reports: scenario table, findings ledger, efficacy metrics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stress.scenarios.base import Scenario, ScenarioResult

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


@dataclass(frozen=True, slots=True)
class RunRecord:
    scenario: Scenario
    result: ScenarioResult
    duration_s: float


def failed_checks(record: RunRecord) -> str:
    return "; ".join(f"{c.name}: {c.evidence}" for c in record.result.checks if not c.ok) or "-"


def scenario_table(records: list[RunRecord]) -> str:
    rows = [
        f"| {r.scenario.family} | {r.scenario.name} | {r.scenario.tier} | "
        f"{'PASS' if r.result.passed else 'FAIL'} | {sum(c.ok for c in r.result.checks)}/{len(r.result.checks)} | "
        f"{r.duration_s:.1f}s | {failed_checks(r)} |"
        for r in records
    ]
    return "\n".join(
        ["| family | scenario | tier | status | checks | time | failed checks |", "|---|---|---|---|---|---|---|", *rows]
    )


def findings_section(records: list[RunRecord]) -> str:
    findings = [
        f"- **{r.scenario.name}**: {r.result.finding}" for r in records if r.result.finding
    ]
    return "\n".join(findings) or "_none_"


def evidence_section(records: list[RunRecord]) -> str:
    blocks = [
        f"### {r.scenario.name}\n\n"
        + "\n".join(f"- [{'x' if c.ok else ' '}] {c.name}\n  - `{c.evidence}`" for c in r.result.checks)
        for r in records
    ]
    return "\n\n".join(blocks)


def write_report(records: list[RunRecord], *, tier: str, extra_sections: dict[str, str] | None = None) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    passed = sum(r.result.passed for r in records)
    sections = [
        f"# Stress run {stamp} (--live {tier})",
        f"**{passed}/{len(records)} scenarios passed**",
        "## Scenarios\n\n" + scenario_table(records),
        "## Findings (pipeline behavior, not harness failures)\n\n" + findings_section(records),
        *(f"## {title}\n\n{body}" for title, body in (extra_sections or {}).items()),
        "## Evidence\n\n" + evidence_section(records),
    ]
    path = REPORTS_DIR / f"{stamp}-{tier}.md"
    path.write_text("\n\n".join(sections) + "\n")
    return path
