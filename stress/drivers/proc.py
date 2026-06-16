"""Process driving: invoke the local checkout's capt-hook, watch and kill the detached reviewer.

Invocations go straight at ``.venv/bin/capt-hook`` — no ``uv run`` in the hot
path, so concurrent scenarios never serialize on uv's project lock, and the
detached child (spawned via ``sys.executable -m captain_hook``) stays inside
the same venv.
"""

from __future__ import annotations

import json
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from stress.sandbox import CHECKOUT

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from stress.sandbox import Sandbox

CAPT_HOOK_BIN = CHECKOUT / ".venv" / "bin" / "capt-hook"
SPAWN_PATTERN = "captain_hook review spawn"
SPAWN_REPORT_RE = re.compile(
    r"SpawnReport\(repo=(?P<repo>None|'[^']*'), watching=(?P<watching>\w+), scanned=(?P<scanned>\d+), "
    r"inserted=(?P<inserted>\d+), judged=(?P<judged>\d+), failed=(?P<failed>\d+), "
    r"eligible=\((?P<eligible>[^)]*)\), brain=(?P<brain>\w+)\)"
)


@dataclass(frozen=True, slots=True)
class SpawnReportLine:
    repo: str | None
    watching: bool
    scanned: int
    inserted: int
    judged: int
    failed: int
    eligible: tuple[int, ...]
    brain: bool


def capt_hook(
    *args: str,
    sandbox: Sandbox,
    cwd: Path | None = None,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CAPT_HOOK_BIN), *args],
        input=stdin,
        env=env or sandbox.env(),
        cwd=str(cwd or sandbox.root),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def session_end_payload(transcript: Path, *, cwd: Path | None) -> str:
    return json.dumps(
        {"hook_event_name": "SessionEnd", "transcript_path": str(transcript)}
        | ({"cwd": str(cwd)} if cwd else {})
    )


def review_run(sandbox: Sandbox, transcript: Path, *, cwd: Path | None = None, **kwargs: object) -> subprocess.CompletedProcess[str]:
    payload = session_end_payload(transcript, cwd=cwd if cwd is not None else sandbox.repo)
    return capt_hook("review", "run", sandbox=sandbox, stdin=payload, **kwargs)  # type: ignore[arg-type]


def spawn_pids(sandbox: Sandbox) -> list[int]:
    roots = {str(sandbox.root), str(sandbox.root.resolve())}
    pids: set[int] = set()
    for root in roots:
        proc = subprocess.run(["pgrep", "-f", f"{SPAWN_PATTERN}.*{root}"], capture_output=True, text=True)
        pids |= {int(line) for line in proc.stdout.split() if line.isdigit()}
    return sorted(pids)


def wait_for[T](predicate: Callable[[], T | None], *, timeout: float, interval: float = 0.05) -> T | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (value := predicate()) is not None:
            return value
        time.sleep(interval)
    return None


def hammer[T](action: Callable[[], Iterable[T]], *, window: float) -> list[T]:
    deadline = time.monotonic() + window
    results: list[T] = []
    while time.monotonic() < deadline:
        results.extend(action())
    return results


def drain_spawn_children(sandbox: Sandbox, *, timeout: float = 300) -> bool:
    return wait_for(lambda: True if not spawn_pids(sandbox) else None, timeout=timeout) is True


def kill_when(
    sandbox: Sandbox, predicate: Callable[[], bool], *, timeout: float = 30, interval: float = 0.001
) -> list[int]:
    fired = wait_for(lambda: True if predicate() else None, timeout=timeout, interval=interval)
    if fired is None:
        return []
    pids = spawn_pids(sandbox)
    for pid in pids:
        try:
            subprocess.run(["kill", f"-{signal.SIGKILL.value}", str(pid)], capture_output=True)
        except OSError:
            pass
    return pids


def spawn_reports(sandbox: Sandbox) -> list[SpawnReportLine]:
    return [
        SpawnReportLine(
            repo=None if match["repo"] == "None" else match["repo"].strip("'"),
            watching=match["watching"] == "True",
            scanned=int(match["scanned"]),
            inserted=int(match["inserted"]),
            judged=int(match["judged"]),
            failed=int(match["failed"]),
            eligible=tuple(int(part) for part in match["eligible"].split(",") if part.strip()),
            brain=match["brain"] == "True",
        )
        for match in SPAWN_REPORT_RE.finditer(sandbox.spawn_log_text())
    ]


def wait_for_report(sandbox: Sandbox, *, count: int = 1, timeout: float = 300) -> list[SpawnReportLine]:
    reports = wait_for(
        lambda: found if len(found := spawn_reports(sandbox)) >= count else None, timeout=timeout, interval=0.2
    )
    return reports or spawn_reports(sandbox)


def wait_drained(sandbox: Sandbox, *, count: int = 1, timeout: float = 300) -> list[SpawnReportLine]:
    reports = wait_for_report(sandbox, count=count, timeout=timeout)
    drain_spawn_children(sandbox, timeout=timeout)
    return reports
