"""SIGKILL crash matrix over the detached reviewer child: torn commits, recovery, judge interruption."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tests.test_review_scan import assistant_text, user_text, write_transcript

from stress.corpus import DURABLE_CORRECTION, DURABLE_CORRECTION_LIVE
from stress.db import count, integrity_ok, one, query
from stress.drivers.proc import capt_hook, drain_spawn_children, kill_when, review_run, spawn_reports, wait_drained
from stress.scenarios.base import Scenario, ScenarioResult, Tier, check, expect

if TYPE_CHECKING:
    from pathlib import Path
    from subprocess import CompletedProcess

    from stress.sandbox import Sandbox

FAMILY = "crash"
NO_JUDGE_ENV = {"HOOKS_REVIEW_MAX_JUDGE_CALLS_PER_SESSION": "0", "HOOKS_REVIEW_MAX_OPEN_PRS": "0"}
SLOW_JUDGE_ENV = {"HOOKS_REVIEW_JUDGE_CONCURRENCY": "1", "HOOKS_REVIEW_MAX_OPEN_PRS": "0"}
TORN_FINDING = (
    "scan.py ingest(): record_file_scan commits the files mtime row and every feedback_event in one "
    "transaction BEFORE the ensure_candidate/record_observation loop runs (autocommit per statement); "
    "SIGKILL after that commit leaves candidate_observations torn, and because the mtime gate then reports "
    "scanned=0 on every rescan of the unchanged file, the lost observations are never backfilled."
)


@dataclass(frozen=True, slots=True)
class TornAttempt:
    path: Path
    session: str
    planted: int
    killed: tuple[int, ...]
    files_row: dict[str, Any] | None
    events: int
    observations: int

    @property
    def torn(self) -> bool:
        return bool(self.killed) and self.files_row is not None and self.observations < self.planted

    @property
    def summary(self) -> str:
        return (
            f"session={self.session} planted={self.planted} killed_pids={list(self.killed)} "
            f"files_row={self.files_row} events={self.events} observations={self.observations}"
        )


def repo_cli(sandbox: Sandbox, *args: str) -> CompletedProcess[str]:
    return capt_hook(*args, sandbox=sandbox, cwd=sandbox.repo, env=sandbox.env(CLAUDE_PROJECT_DIR=str(sandbox.repo)))


def enable(sandbox: Sandbox) -> None:
    if (proc := repo_cli(sandbox, "review", "enable")).returncode != 0:
        raise RuntimeError(f"review enable failed: {proc.stdout}{proc.stderr}")


def write_corrections(sandbox: Sandbox, name: str, n: int, *, session: str, marker: bool = False) -> Path:
    base = DURABLE_CORRECTION if marker else DURABLE_CORRECTION_LIVE
    return write_transcript(
        sandbox.transcripts / f"{name}.jsonl",
        [
            entry
            for index in range(n)
            for entry in (
                assistant_text(f"adding print {index} for debugging", sessionId=session),
                user_text(f"{base} ({name} case {index})", sessionId=session),
            )
        ],
    )


def stub_lines(log: Path) -> int:
    return len(log.read_text().splitlines()) if log.exists() else 0


def torn_attempt(sandbox: Sandbox, n: int, *, planted: int) -> TornAttempt:
    session = f"stress-torn-{n}"
    path = write_corrections(sandbox, f"stress-torn-{n}", planted, session=session)
    review_run(sandbox, path)
    killed = kill_when(
        sandbox,
        lambda: count(sandbox.review_db, "SELECT COUNT(*) FROM files WHERE path = ?", (str(path),)) > 0,
        timeout=120,
    )
    drain_spawn_children(sandbox, timeout=120)
    return TornAttempt(
        path=path,
        session=session,
        planted=planted,
        killed=tuple(killed),
        files_row=one(sandbox.review_db, "SELECT * FROM files WHERE path = ?", (str(path),)),
        events=count(sandbox.review_db, "SELECT COUNT(*) FROM feedback_events WHERE session_id = ?", (session,)),
        observations=count(
            sandbox.review_db, "SELECT COUNT(*) FROM candidate_observations WHERE session_id = ?", (session,)
        ),
    )


def run_torn_mtime_commit(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    attempts: list[TornAttempt] = []
    for n in range(1, 6):
        attempts.append(torn_attempt(sandbox, n, planted=150 if n <= 2 else 400))
        if attempts[-1].torn:
            break
    torn = attempts[-1]
    if not torn.torn:
        return ScenarioResult(
            checks=(
                check("torn state captured within 5 attempts", False, [attempt.summary for attempt in attempts]),
            )
        )
    before = len(spawn_reports(sandbox))
    review_run(sandbox, torn.path)
    reports = wait_drained(sandbox, count=before + 1, timeout=120)
    rescan = reports[-1] if len(reports) > before else None
    after = count(sandbox.review_db, "SELECT COUNT(*) FROM candidate_observations WHERE session_id = ?", (torn.session,))
    return ScenarioResult(
        checks=(
            check("files row committed before kill", torn.files_row is not None, torn.summary),
            expect("feedback_events committed atomically with files row", torn.events, torn.planted),
            check(
                f"candidate_observations torn ({torn.observations} < {torn.planted})",
                torn.observations < torn.planted,
                torn.summary,
            ),
            check("rescan of unchanged mtime reports scanned=0", rescan is not None and rescan.scanned == 0, rescan),
            check(
                "torn observations never backfilled (permanent loss)",
                after == torn.observations and after < torn.planted,
                f"observations_after_rescan={after} observations_at_kill={torn.observations} planted={torn.planted}",
            ),
        ),
        finding=TORN_FINDING,
    )


def run_integrity_after_kill_sweep(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    checks = []
    for index, delay in enumerate((0.05, 0.2, 1.0)):
        path = write_corrections(sandbox, f"stress-sweep-{index}", 80, session=f"stress-sweep-{index}")
        review_run(sandbox, path)
        start = time.monotonic()
        killed = kill_when(sandbox, lambda: time.monotonic() - start >= delay, timeout=10)
        drain_spawn_children(sandbox, timeout=120)
        checks.append(
            check(
                f"review.db integrity ok after SIGKILL at {delay}s",
                integrity_ok(sandbox.review_db),
                f"killed_pids={killed} integrity_check={integrity_ok(sandbox.review_db)}",
            )
        )
        before = len(spawn_reports(sandbox))
        recovery = write_corrections(sandbox, f"stress-sweep-recover-{index}", 1, session=f"stress-sweep-recover-{index}")
        review_run(sandbox, recovery)
        reports = wait_drained(sandbox, count=before + 1, timeout=120)
        fresh = reports[-1] if len(reports) > before else None
        checks.append(
            check(
                f"fresh transcript scans normally after SIGKILL at {delay}s",
                fresh is not None and fresh.scanned == 1 and fresh.inserted == 1,
                f"report={fresh} killed_pids={killed}",
            )
        )
    return ScenarioResult(checks=tuple(checks))


def run_kill_mid_judge(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    partial, events_at_kill, killed, calls = 0, 0, [], 0
    for attempt in range(1, 4):
        log = sandbox.root / f"judge-stub-{attempt}.log"
        path = write_corrections(
            sandbox, f"stress-judge-kill-{attempt}", 8, session=f"stress-judge-kill-{attempt}", marker=True
        )
        review_run(sandbox, path, env=sandbox.env(STRESS_JUDGE_STUB_LOG=str(log)))
        killed = kill_when(sandbox, lambda: stub_lines(log) >= 2, timeout=120)
        drain_spawn_children(sandbox, timeout=120)
        partial = count(sandbox.review_db, "SELECT COUNT(*) FROM verdicts WHERE role = 'judge'")
        events_at_kill = count(sandbox.review_db, "SELECT COUNT(*) FROM feedback_events")
        calls = stub_lines(log)
        if killed and 0 < partial < events_at_kill:
            break
    before = len(spawn_reports(sandbox))
    followup_log = sandbox.root / "judge-stub-followup.log"
    followup = write_corrections(sandbox, "stress-judge-follow", 1, session="stress-judge-follow", marker=True)
    review_run(sandbox, followup, env=sandbox.env(STRESS_JUDGE_STUB_LOG=str(followup_log)))
    reports = wait_drained(sandbox, count=before + 1, timeout=120)
    verdicts = query(sandbox.review_db, "SELECT dedup_key, category, confidence FROM verdicts WHERE role = 'judge'")
    events_total = count(sandbox.review_db, "SELECT COUNT(*) FROM feedback_events")
    return ScenarioResult(
        checks=(
            check(
                "SIGKILL landed mid-judge with partial verdicts persisted",
                bool(killed) and 0 < partial < events_at_kill,
                f"killed_pids={killed} stub_calls_at_kill={calls} verdicts_at_kill={partial} events={events_at_kill}",
            ),
            check(
                "follow-up session completes and reports",
                len(reports) > before,
                f"report={reports[-1] if reports else None} followup_judge_calls={stub_lines(followup_log)}",
            ),
            expect("verdict rows converge to one per worthy feedback event", len(verdicts), events_total),
            check("review.db integrity ok after judge kill", integrity_ok(sandbox.review_db), verdicts[:10]),
        ),
    )


def run_orphan_candidate_window(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    attempt = torn_attempt(sandbox, 1, planted=150)
    orphans = query(
        sandbox.review_db,
        "SELECT c.id FROM candidates c "
        "WHERE NOT EXISTS (SELECT 1 FROM candidate_observations o WHERE o.candidate_id = c.id)",
    )
    verdicts = [repo_cli(sandbox, "review", "threshold-check", str(row["id"])) for row in orphans]
    evidence = (
        [
            f"#{row['id']}: rc={proc.returncode} {proc.stdout.strip()}"
            for row, proc in zip(orphans, verdicts, strict=True)
        ]
        if orphans
        else f"no zero-observation candidate in torn state; {attempt.summary}"
    )
    return ScenarioResult(
        checks=(
            check("review.db integrity ok after kill attempt", integrity_ok(sandbox.review_db), attempt.summary),
            check(
                "zero-observation candidates are never eligible",
                all(proc.returncode == 0 and "eligible=False" in proc.stdout for proc in verdicts),
                evidence,
            ),
        ),
    )


def scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario(
            name="torn-mtime-commit",
            family=FAMILY,
            tier=Tier.OFFLINE,
            run=run_torn_mtime_commit,
            env_overrides=dict(NO_JUDGE_ENV),
        ),
        Scenario(
            name="integrity-after-kill-sweep",
            family=FAMILY,
            tier=Tier.OFFLINE,
            run=run_integrity_after_kill_sweep,
            env_overrides=dict(NO_JUDGE_ENV),
        ),
        Scenario(
            name="kill-mid-judge",
            family=FAMILY,
            tier=Tier.OFFLINE,
            run=run_kill_mid_judge,
            env_overrides=dict(SLOW_JUDGE_ENV),
        ),
        Scenario(
            name="orphan-candidate-window",
            family=FAMILY,
            tier=Tier.OFFLINE,
            run=run_orphan_candidate_window,
            env_overrides=dict(NO_JUDGE_ENV),
        ),
    )
