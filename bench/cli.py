"""The benchmark runner: ``uv run python -m bench.cli run [--record]``.

Run it from an otherwise idle machine. The exec counter is system-wide, so a
concurrent Claude Code session — which fires this very chain on every tool call —
is the loudest neighbour there is, and it shows up as a nonzero noise floor and a
scenario whose count comes back ``unquiet`` rather than as a wrong number.
"""

from __future__ import annotations

import click

from bench.dispatch import deployment, roots, scenarios
from bench.measure import measure
from bench.report import Record, record, table, write_baseline


@click.group()
def cli() -> None:
    """Dispatch-latency and execve-count benchmarks for the capt-hook hot path."""


@cli.command()
@click.option("--budget", default=20.0, help="Seconds a scenario may sample for; a cold one is capped lower.")
@click.option("--record", "persist", is_flag=True, default=False, help="Overwrite bench/baseline.json with this run.")
def run(budget: float, persist: bool) -> None:
    """Measure every scenario and print the table, optionally recording it as the baseline."""
    deployed = deployment()
    click.echo(f"build {deployed.build} via {deployed.client}")
    with roots() as (warm, cold):
        records: list[Record] = []
        for scenario in scenarios(deployed, warm, cold, budget_s=budget):
            measurement = measure(
                scenario.label,
                scenario.command,
                budget_s=scenario.budget_s,
                minimum_samples=scenario.minimum_samples,
            )
            records.append(record(measurement))
            click.echo(f"  {scenario.label:20s} {records[-1].execs} execs  {records[-1].ms_min} ms")
    click.echo(table(records))
    if persist:
        click.echo(f"recorded -> {write_baseline(records, build=deployed.build)}")


if __name__ == "__main__":
    cli()
