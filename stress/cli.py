"""The harness runner: ``uv run python -m stress.cli run --live {none,judge,brain}``.

Phases run cheapest-first with gates between them; the real-state leak guard
runs after every invocation and fails the run loudly on any hit. Sandboxes are
destroyed after each scenario unless ``--keep-sandbox``. Before any scenario the
runner stabilizes the interpreter's signature so the suite's many subprocess
spawns do not storm ``syspolicyd`` on macOS Tahoe.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import click
from loguru import logger

from stress.report import RunRecord, write_report
from stress.sandbox import RUN_ROOT, create_sandbox, real_state_leaks
from stress.scenarios import all_scenarios
from stress.scenarios.base import ScenarioResult, Tier, check
from stress.signing import ensure_stable_signatures

TIER_ORDER = {Tier.OFFLINE: 0, Tier.JUDGE: 1, Tier.BRAIN: 2}


def included(tier: Tier, live: str) -> bool:
    return TIER_ORDER[tier] <= TIER_ORDER[Tier(live)] if live != "none" else tier == Tier.OFFLINE


@click.group()
def cli() -> None:
    """Stress-test harness for the SessionEnd reviewer pipeline."""


@cli.command(name="list")
def list_() -> None:
    """List registered scenarios."""
    for scenario in all_scenarios():
        click.echo(f"{scenario.tier:8} {scenario.family:12} {scenario.name}")


@cli.command()
def clean() -> None:
    """Remove all leftover sandbox run dirs under the run root."""
    import shutil

    run_dirs = [path for path in RUN_ROOT.glob("*") if path.is_dir()]
    for path in run_dirs:
        shutil.rmtree(path, ignore_errors=True)
    click.echo(f"removed {len(run_dirs)} run dir(s) under {RUN_ROOT}")


@cli.command(name="nuke-github")
@click.option("--i-know", is_flag=True, required=True, help="Confirm deletion of all capt-hook-stress-* repos")
def nuke_github(i_know: bool) -> None:
    """Close PRs on and delete every leftover capt-hook-stress-* throwaway repo."""
    from stress.drivers.github import ThrowawayRepo, delete_throwaway, gh, gh_login

    login = gh_login()
    listing = gh("repo", "list", login, "--json", "name", "--jq", ".[].name")
    names = [name for name in listing.stdout.split() if name.startswith("capt-hook-stress-")]
    for name in names:
        repo = ThrowawayRepo(name=f"{login}/{name}", url=f"https://github.com/{login}/{name}")
        click.echo(f"{'deleted' if delete_throwaway(repo) else 'FAILED to delete'} {repo.name}")
    if not names:
        click.echo("no capt-hook-stress-* repos found")


@cli.command()
@click.option("--live", type=click.Choice(["none", "judge", "brain"]), default="none")
@click.option("--only", default=None, help="Run only scenarios whose family or name contains this substring")
@click.option(
    "--keep-sandbox",
    is_flag=True,
    default=False,
    help="Keep sandbox dirs for debugging (default: destroy each after its scenario)",
)
@click.option("--run-dir", type=click.Path(path_type=Path), default=None)
def run(live: str, only: str | None, keep_sandbox: bool, run_dir: Path | None) -> None:
    """Run all scenarios at or below the chosen live tier, then write the report."""
    run_dir = run_dir or RUN_ROOT / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for stabilized in ensure_stable_signatures():
        logger.info("stabilized signature: {}", stabilized)
    selected = [
        scenario
        for scenario in sorted(all_scenarios(), key=lambda s: TIER_ORDER[s.tier])
        if included(scenario.tier, live)
        if only is None or only in scenario.family or only in scenario.name
    ]
    records: list[RunRecord] = []
    for scenario in selected:
        sandbox = create_sandbox(run_dir, scenario.name, env_overrides=dict(scenario.env_overrides))
        started = time.monotonic()
        try:
            result = scenario.run(sandbox)
        except Exception as exc:
            result = ScenarioResult(checks=(check("scenario raised", False, f"{type(exc).__name__}: {exc}"),))
        record = RunRecord(scenario, result, time.monotonic() - started)
        records.append(record)
        logger.info("{} {} ({:.1f}s)", "PASS" if result.passed else "FAIL", scenario.name, record.duration_s)
        if leaks := real_state_leaks():
            raise click.ClickException(f"REAL-STATE LEAK after {scenario.name}: {leaks}")
        if not keep_sandbox:
            sandbox.destroy()
    report = write_report(records, tier=live)
    passed = sum(record.result.passed for record in records)
    click.echo(f"{passed}/{len(records)} passed -> {report}")
    if passed < len(records):
        raise SystemExit(1)


if __name__ == "__main__":
    cli()
