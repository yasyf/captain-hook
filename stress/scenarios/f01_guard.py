"""Guard-path scenarios: ``review run`` must exit 0 silently on every input and never wedge a session."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from stress.corpus import durable_correction, write
from stress.drivers.proc import (
    capt_hook,
    drain_spawn_children,
    review_run,
    session_end_payload,
    spawn_pids,
    spawn_reports,
    wait_for,
    wait_for_report,
)
from stress.scenarios.base import Check, Scenario, ScenarioResult, Tier, check, expect

if TYPE_CHECKING:
    import subprocess

    from stress.sandbox import Sandbox

FAMILY = "guard"
NO_SPAWN_WINDOW = 1.5
MALFORMED_CASES: tuple[tuple[str, str], ...] = (
    ("empty-stdin", ""),
    ("not-json", "not json"),
    ("json-array", "[]"),
    ("json-empty-object", "{}"),
    ("non-string-path", '{"transcript_path": 42}'),
    ("missing-file", '{"transcript_path": "/nonexistent/x.jsonl"}'),
    ("nul-in-path", json.dumps({"transcript_path": "/tmp/capt-stress/\x00bad.jsonl"})),
)


def lingering_spawn(sandbox: Sandbox) -> list[int]:
    return wait_for(lambda: pids if (pids := spawn_pids(sandbox)) else None, timeout=NO_SPAWN_WINDOW) or []


def silent_no_spawn(sandbox: Sandbox, label: str, proc: subprocess.CompletedProcess[str], before: str) -> Check:
    pids = lingering_spawn(sandbox)
    after = sandbox.spawn_log_text()
    return check(
        f"{label}: exit 0, empty stdout, spawn.log unchanged, no child",
        proc.returncode == 0 and proc.stdout == "" and after == before and not pids,
        f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr[:200]!r} "
        f"log_delta={after[len(before) :]!r} pids={pids}",
    )


def sweep_case(sandbox: Sandbox, label: str, payload: str) -> Check:
    before = sandbox.spawn_log_text()
    return silent_no_spawn(sandbox, label, capt_hook("review", "run", sandbox=sandbox, stdin=payload), before)


def malformed_stdin(sandbox: Sandbox) -> ScenarioResult:
    return ScenarioResult(checks=tuple(sweep_case(sandbox, label, payload) for label, payload in MALFORMED_CASES))


def valid_payload_spawns_once(sandbox: Sandbox) -> ScenarioResult:
    transcript = write(durable_correction("guard-valid"), sandbox.transcripts)
    started = time.monotonic()
    proc = review_run(sandbox, transcript)
    elapsed = time.monotonic() - started
    reports = wait_for_report(sandbox, count=1, timeout=120)
    drained = drain_spawn_children(sandbox, timeout=120)
    final = spawn_reports(sandbox)
    return ScenarioResult(
        checks=(
            check(
                "parent exits 0 in under 5s",
                proc.returncode == 0 and elapsed < 5.0,
                f"rc={proc.returncode} elapsed={elapsed:.2f}s stdout={proc.stdout!r} stderr={proc.stderr[:200]!r}",
            ),
            check(
                "exactly one spawn report appears",
                len(final) == 1 and len(reports) == 1,
                f"reports={final} drained={drained} log={sandbox.spawn_log_text()[:400]!r}",
            ),
            expect("unwatched spawn watching flag", reports[0].watching if reports else None, False),
        )
    )


def spawned_env_skips(sandbox: Sandbox) -> ScenarioResult:
    transcript = write(durable_correction("guard-spawned"), sandbox.transcripts)
    before = sandbox.spawn_log_text()
    proc = capt_hook(
        "review",
        "run",
        sandbox=sandbox,
        stdin=session_end_payload(transcript, cwd=sandbox.repo),
        env=sandbox.env(CAPT_HOOK_SPAWNED="1"),
    )
    return ScenarioResult(
        checks=(
            silent_no_spawn(sandbox, "spawned-env", proc, before),
            expect("spawn.log bytes after run", len(sandbox.spawn_log_text()), len(before)),
        )
    )


def directory_transcript(sandbox: Sandbox) -> ScenarioResult:
    before = sandbox.spawn_log_text()
    payload = session_end_payload(sandbox.transcripts, cwd=sandbox.repo)
    proc = capt_hook("review", "run", sandbox=sandbox, stdin=payload)
    return ScenarioResult(
        checks=(
            check("payload path is a directory", sandbox.transcripts.is_dir(), payload),
            silent_no_spawn(sandbox, "directory-transcript", proc, before),
        )
    )


def readonly_state_dir(sandbox: Sandbox) -> ScenarioResult:
    transcript = write(durable_correction("guard-readonly"), sandbox.transcripts)
    review_dir = sandbox.spawn_log.parent
    review_dir.chmod(0o500)
    try:
        before = sandbox.spawn_log_text()
        proc = review_run(sandbox, transcript)
        no_spawn = silent_no_spawn(sandbox, "readonly-state", proc, before)
        log_exists = sandbox.spawn_log.exists()
    finally:
        review_dir.chmod(0o755)
    return ScenarioResult(
        checks=(
            no_spawn,
            check("spawn.log never created under 0o500 dir", not log_exists, f"exists={log_exists} dir_mode=0o500"),
        )
    )


def scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario(name="guard-malformed-stdin", family=FAMILY, tier=Tier.OFFLINE, run=malformed_stdin),
        Scenario(name="guard-valid-spawn", family=FAMILY, tier=Tier.OFFLINE, run=valid_payload_spawns_once),
        Scenario(name="guard-spawned-skip", family=FAMILY, tier=Tier.OFFLINE, run=spawned_env_skips),
        Scenario(name="guard-directory-transcript", family=FAMILY, tier=Tier.OFFLINE, run=directory_transcript),
        Scenario(name="guard-readonly-state", family=FAMILY, tier=Tier.OFFLINE, run=readonly_state_dir),
    )
