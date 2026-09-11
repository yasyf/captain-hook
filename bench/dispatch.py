"""The dispatch chain under test, and the roots that make a dispatch cold or warm.

The daemon caches one Python worker per ``{root, python, build, environment}`` tuple
and retires the entry the moment its dispatch finishes when the root lives under the
system temp directory (``ephemeralRoot``, ``internal/hookd/manager.go``). A scratch
root there is therefore cold on every dispatch and a root outside it is warm from the
second one on, which is the whole cold/warm control this harness needs — no worker
restart, and nothing touched in the live daemon that its own idle sweep will not reclaim.

Both benchmark roots are empty, so what the scenarios time is the framework's dispatch
overhead rather than the bodies of whichever hooks a real project happens to declare.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from bench.measure import Command
from capt_hook_client.client import HOST

BENCH_ROOT = Path.home() / ".cache" / "capt-hook-bench"
COLD_BUDGET_S = 60.0
EVENT = "PostToolUse"


class DeploymentMissing(RuntimeError):
    """No `hook` on PATH is the deployment the signed host will serve."""


@dataclass(frozen=True, slots=True)
class Deployment:
    client: Path
    interpreter: Path
    build: str


@dataclass(frozen=True, slots=True)
class Scenario:
    label: str
    command: Command
    budget_s: float
    minimum_samples: int


def host_build() -> str:
    return json.loads(subprocess.run([HOST, "version"], capture_output=True, text=True, check=True).stdout)["build"]


def shebang(script: Path) -> Path:
    return Path(script.read_text().splitlines()[0].removeprefix("#!").strip())


def dist_version(interpreter: Path) -> str:
    probe = "import importlib.metadata; print(importlib.metadata.version('capt-hook'))"
    return subprocess.run([interpreter, "-c", probe], capture_output=True, text=True).stdout.strip()


def clients() -> list[Path]:
    return [script for directory in os.get_exec_path() if (script := Path(directory) / "hook").is_file()]


def deployment() -> Deployment:
    """The `hook` console script whose distribution is the exact build the signed host serves."""
    build = host_build()
    for client in clients():
        if dist_version(interpreter := shebang(client)) == build:
            return Deployment(client=client, interpreter=interpreter, build=build)
    raise DeploymentMissing(f"no `hook` on PATH carries build {build}; searched {[str(c) for c in clients()]}")


def payload(root: Path) -> bytes:
    return json.dumps(
        {
            "session_id": "bench",
            "transcript_path": "/dev/null",
            "cwd": str(root),
            "hook_event_name": EVENT,
            "tool_name": "Bash",
            "tool_input": {"command": "true"},
            "tool_response": {},
        }
    ).encode()


def chain(deployed: Deployment, root: Path) -> Command:
    return Command((str(deployed.client), "--root", str(root), "run", EVENT), payload(root))


def host(deployed: Deployment, root: Path) -> Command:
    return Command(
        (
            HOST,
            "run",
            "--event",
            EVENT,
            "--root",
            str(root),
            "--cwd",
            os.getcwd(),
            "--python",
            str(deployed.interpreter),
            "--build",
            deployed.build,
        ),
        payload(root),
    )


@contextmanager
def roots() -> Iterator[tuple[Path, Path]]:
    warm = BENCH_ROOT / "warm"
    warm.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="capt-hook-bench-cold-") as cold:
        yield warm, Path(cold)


def scenarios(deployed: Deployment, warm: Path, cold: Path, *, budget_s: float) -> tuple[Scenario, ...]:
    interpreter = str(deployed.interpreter)
    return (
        Scenario("chain-warm", chain(deployed, warm), budget_s, 30),
        Scenario("chain-cold", chain(deployed, cold), min(budget_s, COLD_BUDGET_S), 10),
        Scenario("host-warm", host(deployed, warm), budget_s, 30),
        Scenario("host-cold", host(deployed, cold), min(budget_s, COLD_BUDGET_S), 10),
        Scenario("interpreter-bare", Command((interpreter, "-c", "pass")), budget_s, 30),
        Scenario("interpreter-client", Command((interpreter, "-c", "import capt_hook_client.client")), budget_s, 30),
        Scenario("host-version", Command((HOST, "version")), budget_s, 30),
    )
