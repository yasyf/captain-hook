"""max_fires cross-process reservation: N concurrent ``capt-hook run`` processes must not over-fire a capped hook.

Each hook event is a separate ``capt-hook run`` process, so a plain read-check-write of the fire
count lets batched parallel tool calls all read the pre-increment count and over-fire. This scenario
fires ``PROCS`` concurrent dispatches of one capped nudge sharing a session and asserts at most
``MAX_FIRES`` deliveries — the reserve-then-release protocol's cross-process contract, which threads
in one process cannot fully exercise.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from stress.drivers.proc import CAPT_HOOK_BIN
from stress.scenarios.base import Scenario, ScenarioResult, Tier, check, expect

if TYPE_CHECKING:
    from stress.sandbox import Sandbox

FAMILY = "fire-race"
SESSION_ID = "stress-fire-race"
MAX_FIRES = 3
PROCS = 4
WARN = "capped race warn"
HOOK_SRC = f"from captain_hook import nudge\n\nnudge({WARN!r}, when=lambda evt: True, max_fires={MAX_FIRES})\n"


def hooks_dir(sandbox: Sandbox) -> Path:
    directory = sandbox.repo / ".claude" / "hooks"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "race_hook.py").write_text(HOOK_SRC)
    return directory


def pre_tool_payload(sandbox: Sandbox) -> str:
    transcript = sandbox.transcripts / f"{SESSION_ID}.jsonl"
    transcript.write_text("")
    return json.dumps(
        {
            "session_id": SESSION_ID,
            "hook_event_name": "PreToolUse",
            "transcript_path": str(transcript),
            "cwd": str(sandbox.repo),
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
        }
    )


def fire_concurrently(sandbox: Sandbox, directory: Path, n: int) -> list[tuple[int, str, str]]:
    payload = pre_tool_payload(sandbox)
    procs = [
        subprocess.Popen(
            [str(CAPT_HOOK_BIN), "--hooks", str(directory), "--root", str(sandbox.repo), "run", "PreToolUse"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=sandbox.env(),
            cwd=str(sandbox.repo),
            text=True,
        )
        for _ in range(n)
    ]
    for proc in procs:
        proc.stdin.write(payload)
        proc.stdin.close()
    for proc in procs:
        proc.wait(timeout=60)
    return [(proc.returncode, proc.stdout.read(), proc.stderr.read()) for proc in procs]


def persisted_fire_count(sandbox: Sandbox) -> int:
    session_dir = sandbox.state_dir / "hooks" / "sessions" / SESSION_ID
    return sum(json.loads(path.read_text()).get("fire_count", 0) for path in session_dir.glob("**/hook_state.json"))


def run_fire_race(sandbox: Sandbox) -> ScenarioResult:
    outcomes = fire_concurrently(sandbox, hooks_dir(sandbox), PROCS)
    delivered = sum(1 for _rc, out, _err in outcomes if WARN in out)
    tracebacks = [err for _rc, _out, err in outcomes if "Traceback" in err]
    return ScenarioResult(
        checks=(
            expect(
                f"exactly {MAX_FIRES} of {PROCS} concurrent dispatches deliver the capped warn",
                delivered,
                MAX_FIRES,
            ),
            check(
                "every dispatch exits 0",
                all(rc == 0 for rc, _out, _err in outcomes),
                [rc for rc, _out, _err in outcomes],
            ),
            check("no Traceback in any dispatch stderr", not tracebacks, tracebacks[:1]),
            expect("persisted fire_count equals the reservation ceiling", persisted_fire_count(sandbox), MAX_FIRES),
        ),
    )


def scenarios() -> tuple[Scenario, ...]:
    return (Scenario(name="fire-race-max-fires", family=FAMILY, tier=Tier.OFFLINE, run=run_fire_race),)
