"""Confirm-step latency per small-model backend: ``uv run python -m bench.confirm [--calls 20]``.

Times the exact structured call :mod:`captain_hook.confirm` makes, one realistic confirm prompt at a
time, against every backend spawnllm offers for a small model. The confirm step waits three seconds,
so a backend's p95 against that wall is the number that picks the default.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from statistics import quantiles
from typing import TYPE_CHECKING

import click
from spawnllm import (
    AppleBackend,
    BackendReady,
    ClaudeCliBackend,
    ClaudeSdkBackend,
    CodexCliBackend,
    OpenAiEndpointBackend,
    extract_sync,
)

from captain_hook.confirm import ConfirmVerdict, confirm_prompt

if TYPE_CHECKING:
    from spawnllm import LlmBackend, TReasoningEffort

WALL_S = 3.0
CEREBRAS = "https://api.cerebras.ai/v1"
ANTHROPIC = "https://api.anthropic.com/v1"
RULE = (
    "A push that lands on a branch whose pull request the Graphite merge queue already admitted, "
    "so the queue merges the older commit and silently drops the push."
)
MESSAGE = (
    "The Graphite merge queue holds `api-retry-fix` and drops anything pushed after admission. "
    "Ship the change as a stacked PR: run `ccx vcs stack new <name>` from that branch."
)
EVIDENCE = """\
$ ccx vcs pr status 28895
#28895 api: 🐛 retry the sandsql handoff on a stale lease  queue: queued  enqueued: 4be1c09  checks: 41/41 green
$ git log --oneline -3
9d0e1a2 api: address review — clamp the retry budget
4be1c09 api: 🐛 retry the sandsql handoff on a stale lease
c31f7aa api: ♻️ extract the lease reader
$ git status --short
 M api/src/modules/sandsql/handoff.ts
"""
SHIP = {"command": 'ccx vcs ship -m "api: clamp the retry budget" --tip-only', "description": "Ship the review fix"}
STATUS = {"command": "ccx vcs status --refresh", "description": "Check stack state"}
CASES = (
    (json.dumps(SHIP), True),
    (json.dumps(STATUS), False),
)


@dataclass(frozen=True, slots=True)
class Lane:
    name: str
    backend: LlmBackend | None
    skipped: str | None = None


@dataclass(frozen=True, slots=True)
class Result:
    name: str
    calls: int
    failures: int
    correct: int
    p50_ms: float | None
    p95_ms: float | None
    max_ms: float | None
    first_ms: float | None
    within_wall: int
    skipped: str | None
    errors: list[str]


def cerebras(model: str, effort: TReasoningEffort) -> Lane:
    key = os.environ.get("CEREBRAS_API_KEY")
    backend = OpenAiEndpointBackend(CEREBRAS, model, api_key=key, reasoning_effort=effort) if key else None
    return Lane(f"cerebras {model} ({effort})", backend, None if key else "CEREBRAS_API_KEY unset")


def anthropic() -> Lane:
    key = os.environ.get("ANTHROPIC_API_KEY")
    backend = OpenAiEndpointBackend(ANTHROPIC, "claude-haiku-4-5", api_key=key) if key else None
    return Lane("anthropic api haiku", backend, None if key else "no ANTHROPIC_API_KEY on this machine")


def lanes() -> list[Lane]:
    return [
        Lane("codex gpt-5.6-luna:low", CodexCliBackend()),
        Lane("claude-sdk haiku", ClaudeSdkBackend()),
        Lane("claude -p haiku", ClaudeCliBackend()),
        Lane("apple on-device", AppleBackend()),
        cerebras("qwen-3.8-27b", "none"),
        cerebras("gpt-oss-120b", "low"),
        anthropic(),
    ]


def measure(lane: Lane, calls: int) -> Result:
    if lane.backend is None or not isinstance(status := lane.backend.check_status(timeout=15), BackendReady):
        reason = lane.skipped or f"backend not ready: {type(status).__name__}"
        return Result(lane.name, 0, 0, 0, None, None, None, None, 0, reason, [])
    timings: list[float] = []
    errors: list[str] = []
    correct = 0
    for index in range(calls):
        tool_input, expected = CASES[index % len(CASES)]
        prompt = str(confirm_prompt(RULE, MESSAGE, tool_input, EVIDENCE))
        started = time.perf_counter()
        try:
            verdict = extract_sync(prompt, ConfirmVerdict, backend=lane.backend, model="small", timeout=60)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {str(exc).strip().splitlines()[-1:] or ['']}"[:200])
            continue
        finally:
            timings.append((time.perf_counter() - started) * 1000)
        correct += verdict.block == expected and verdict.confident
    cuts = quantiles(timings, n=20, method="inclusive")
    return Result(
        lane.name,
        calls,
        len(errors),
        correct,
        round(cuts[9]),
        round(cuts[18]),
        round(max(timings)),
        round(timings[0]),
        sum(ms <= WALL_S * 1000 for ms in timings),
        None,
        errors,
    )


@click.command()
@click.option("--calls", default=20, help="Calls per backend.")
@click.option("--out", type=click.Path(), default=None, help="Write the results as JSON here.")
@click.option("--only", default="", help="Measure only the backends whose name contains this text.")
def main(calls: int, out: str | None, only: str) -> None:
    """Time the confirm step's structured call against every small-model backend."""
    results = []
    for lane in (lane for lane in lanes() if only in lane.name):
        results.append(result := measure(lane, calls))
        click.echo(json.dumps(asdict(result)), err=False)
    if out:
        with open(out, "w") as handle:
            json.dump([asdict(result) for result in results], handle, indent=2)


if __name__ == "__main__":
    main()
