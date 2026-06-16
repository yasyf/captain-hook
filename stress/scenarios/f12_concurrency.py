"""Concurrent SessionEnd deliveries: racing children, duplicate payloads, CLI writes against a live child."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tests.test_review_scan import assistant_text, user_text, write_transcript

from stress.db import count, integrity_ok, query
from stress.drivers.proc import (
    CAPT_HOOK_BIN,
    capt_hook,
    drain_spawn_children,
    hammer,
    review_run,
    session_end_payload,
    spawn_reports,
    wait_for_report,
)
from stress.sandbox import create_sandbox
from stress.scenarios.base import Scenario, ScenarioResult, Tier, check, expect

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from subprocess import CompletedProcess

    from stress.drivers.proc import SpawnReportLine
    from stress.sandbox import Sandbox

FAMILY = "concurrency"
NO_BRAIN_ENV = {"HOOKS_REVIEW_MAX_OPEN_PRS": "0"}
SLOW_JUDGE_ENV = {"HOOKS_REVIEW_JUDGE_CONCURRENCY": "1", "HOOKS_REVIEW_MAX_OPEN_PRS": "0"}
CORRECTION = "never log with print in this repo, always use the loguru logger [[judge:tooling_rule]]"
PR_URL = "https://github.com/capt-hook-stress/race/pull/1"
LOCKED_ERROR = "sqlite3.OperationalError: database is locked"
LOCKED_FINDING = (
    "concurrent SessionEnd deliveries race DecisionLog.open on a fresh decisions.db: cc_transcript/decisions.py "
    "runs `PRAGMA journal_mode = WAL` BEFORE `PRAGMA busy_timeout = 2000`, so when two reviewer children open "
    "the ledger simultaneously one crashes with sqlite3.OperationalError 'database is locked' (observed ~40% of "
    "paired deliveries); the crash happens in scan.py ingest() before record_file_scan, so the crashed session "
    "gets no SpawnReport, no files row, and its corrections are silently never scanned"
)
HAMMER_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("review", "list"),
    ("review", "update", "1", "pr_open", "--pr-url", PR_URL),
)


def repo_cli(sandbox: Sandbox, *args: str) -> CompletedProcess[str]:
    return capt_hook(*args, sandbox=sandbox, cwd=sandbox.repo, env=sandbox.env(CLAUDE_PROJECT_DIR=str(sandbox.repo)))


def cli_outcome(sandbox: Sandbox, *args: str) -> tuple[str, int, str]:
    proc = repo_cli(sandbox, *args)
    return (" ".join(args), proc.returncode, (proc.stdout + proc.stderr).strip()[-120:])


def enable(sandbox: Sandbox) -> None:
    if (proc := repo_cli(sandbox, "review", "enable")).returncode != 0:
        raise RuntimeError(f"review enable failed: {proc.stdout}{proc.stderr}")


def write_corrections(sandbox: Sandbox, name: str, n: int, *, session: str) -> Path:
    return write_transcript(
        sandbox.transcripts / f"{name}.jsonl",
        [
            entry
            for index in range(n)
            for entry in (
                assistant_text(f"adding print {index} for debugging", sessionId=session),
                user_text(f"{CORRECTION} ({name} case {index})", sessionId=session),
            )
        ],
    )


def fire_concurrently(sandbox: Sandbox, transcripts: Sequence[Path]) -> list[subprocess.Popen[str]]:
    procs = [
        subprocess.Popen(
            [str(CAPT_HOOK_BIN), "review", "run"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=sandbox.env(),
            cwd=str(sandbox.root),
            text=True,
        )
        for _ in transcripts
    ]
    for proc, transcript in zip(procs, transcripts, strict=True):
        proc.stdin.write(session_end_payload(transcript, cwd=sandbox.repo))
        proc.stdin.close()
    for proc in procs:
        proc.wait(timeout=60)
    return procs


def log_excerpt(text: str, needle: str) -> str:
    return "\n".join(line for line in text.splitlines() if needle in line) or (
        f"(no {needle!r} lines in {len(text)} chars of spawn.log)"
    )


def warm_stores(sandbox: Sandbox) -> None:
    review_run(sandbox, write_corrections(sandbox, "stress-warmup", 1, session="stress-warmup"))
    drain_spawn_children(sandbox, timeout=120)


@dataclass(frozen=True, slots=True)
class RaceAttempt:
    rcs: tuple[int, ...]
    crashes: int
    reports: tuple[SpawnReportLine, ...]
    recorded: frozenset[str]
    planted: frozenset[str]
    events: int
    integrity: bool
    log: str

    @property
    def summary(self) -> str:
        return (
            f"rcs={list(self.rcs)} crashes={self.crashes} reports={len(self.reports)} "
            f"recorded={sorted(self.recorded)} events={self.events} integrity={self.integrity}"
        )


def race_attempt(sandbox: Sandbox, n: int) -> RaceAttempt:
    sub = create_sandbox(sandbox.root, f"sub-race-{n}", env_overrides=dict(sandbox.env_overrides))
    enable(sub)
    paths = (
        write_corrections(sub, "stress-race-a", 1, session="stress-race-a"),
        write_corrections(sub, "stress-race-b", 1, session="stress-race-b"),
    )
    procs = fire_concurrently(sub, paths)
    drain_spawn_children(sub, timeout=120)
    log = sub.spawn_log_text()
    return RaceAttempt(
        rcs=tuple(proc.returncode for proc in procs),
        crashes=log.count(LOCKED_ERROR),
        reports=tuple(spawn_reports(sub)),
        recorded=frozenset(str(row["path"]) for row in query(sub.review_db, "SELECT path FROM files")),
        planted=frozenset(str(path) for path in paths),
        events=count(sub.review_db, "SELECT COUNT(*) FROM feedback_events"),
        integrity=integrity_ok(sub.review_db),
        log=log,
    )


def run_two_transcripts_race(sandbox: Sandbox) -> ScenarioResult:
    attempts: list[RaceAttempt] = []
    for n in range(1, 13):
        attempts.append(race_attempt(sandbox, n))
        if attempts[-1].crashes:
            break
    raced = attempts[-1]
    if not raced.crashes:
        return ScenarioResult(
            checks=(
                check(
                    "decisions.db WAL-open crash reproduced within 12 attempts",
                    False,
                    [attempt.summary for attempt in attempts],
                ),
            )
        )
    return ScenarioResult(
        checks=(
            expect("review run exit codes (hook side stays silent)", list(raced.rcs), [0, 0]),
            check(
                "one child crashed on the decisions.db WAL-open race",
                raced.crashes >= 1 and "PRAGMA journal_mode = WAL" in raced.log and "decisions.py" in raced.log,
                f"attempt={len(attempts)} {log_excerpt(raced.log, 'locked')}\n{log_excerpt(raced.log, 'PRAGMA')}",
            ),
            check(
                "only surviving children report",
                len(raced.reports) == 2 - raced.crashes,
                f"{raced.summary} reports={raced.reports}",
            ),
            check(
                "crashed session left unrecorded: files rows and events only for survivors",
                len(raced.recorded) == 2 - raced.crashes
                and raced.recorded < raced.planted
                and raced.events == 2 - raced.crashes,
                raced.summary,
            ),
            check("review.db integrity ok despite crash", raced.integrity, raced.summary),
        ),
        finding=LOCKED_FINDING,
    )


def run_duplicate_delivery_race(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    warm_stores(sandbox)
    before = len(spawn_reports(sandbox))
    path = write_corrections(sandbox, "stress-dup", 1, session="stress-dup")
    fire_concurrently(sandbox, (path, path))
    drain_spawn_children(sandbox, timeout=120)
    reports = spawn_reports(sandbox)[before:]
    total_inserted = sum(report.inserted for report in reports)
    files = query(sandbox.review_db, "SELECT * FROM files WHERE path = ?", (str(path),))
    log = sandbox.spawn_log_text()
    return ScenarioResult(
        checks=(
            check("both children report", len(reports) == 2, reports),
            expect("files rows for the duplicated path", len(files), 1),
            expect(
                "feedback_events deduped",
                count(sandbox.review_db, "SELECT COUNT(*) FROM feedback_events WHERE session_id = ?", ("stress-dup",)),
                1,
            ),
            expect(
                "candidate_observations deduped",
                count(
                    sandbox.review_db,
                    "SELECT COUNT(*) FROM candidate_observations WHERE session_id = ?",
                    ("stress-dup",),
                ),
                1,
            ),
            expect(
                "candidates deduped",
                count(
                    sandbox.review_db,
                    "SELECT COUNT(DISTINCT candidate_id) FROM candidate_observations WHERE session_id = ?",
                    ("stress-dup",),
                ),
                1,
            ),
            check("reports' inserted total <= 2", total_inserted <= 2, f"inserted_total={total_inserted} {reports}"),
            check("no Traceback in spawn.log", "Traceback" not in log, log_excerpt(log, "Traceback")),
        ),
        finding=(
            "duplicate concurrent delivery double-counts inserted across SpawnReports "
            f"(sum={total_inserted}) even though the db end-state stays deduped"
            if total_inserted > 1
            else None
        ),
    )


def run_update_races_judge(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    path = write_corrections(sandbox, "stress-update-race", 10, session="stress-update-race")
    before = len(spawn_reports(sandbox))
    review = capt_hook("review", "run", sandbox=sandbox, stdin=session_end_payload(path, cwd=sandbox.repo))
    outcomes = hammer(lambda: [cli_outcome(sandbox, *args) for args in HAMMER_COMMANDS], window=2)
    reports = wait_for_report(sandbox, count=before + 1, timeout=120)
    drain_spawn_children(sandbox, timeout=120)
    log = sandbox.spawn_log_text()
    return ScenarioResult(
        checks=(
            expect("review run exit code", review.returncode, 0),
            check(
                "hammered CLI calls all graceful (rc 0/1, no Traceback)",
                all(rc in (0, 1) and "Traceback" not in out for _, rc, out in outcomes),
                outcomes,
            ),
            check("child completes and reports", len(reports) > before, reports[-1] if reports else None),
            check("no Traceback in spawn.log", "Traceback" not in log, log_excerpt(log, "Traceback")),
            check("review.db integrity ok", integrity_ok(sandbox.review_db), f"hammer_calls={len(outcomes)}"),
        ),
    )


def run_log_interleave_tolerance(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    warm_stores(sandbox)
    before = len(spawn_reports(sandbox))
    paths = [
        write_corrections(sandbox, f"stress-interleave-{index}", 1, session=f"stress-interleave-{index}")
        for index in range(4)
    ]
    fire_concurrently(sandbox, paths)
    reports = wait_for_report(sandbox, count=before + 4, timeout=180)[before:]
    drain_spawn_children(sandbox, timeout=120)
    log = sandbox.spawn_log_text()
    return ScenarioResult(
        checks=(
            check(
                "parser recovers >= 4 reports from interleaved log",
                len(reports) >= 4,
                f"parsed={len(reports)} raw_occurrences={log.count('SpawnReport(')} log_lines={len(log.splitlines())}",
            ),
            expect(
                "each child scanned its own transcript",
                sorted((report.scanned, report.inserted) for report in reports),
                [(1, 1)] * 4,
            ),
            check("no Traceback in spawn.log", "Traceback" not in log, log_excerpt(log, "Traceback")),
        ),
    )


def scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario(
            name="two-transcripts-race",
            family=FAMILY,
            tier=Tier.OFFLINE,
            run=run_two_transcripts_race,
            env_overrides=dict(NO_BRAIN_ENV),
        ),
        Scenario(
            name="duplicate-delivery-race",
            family=FAMILY,
            tier=Tier.OFFLINE,
            run=run_duplicate_delivery_race,
            env_overrides=dict(NO_BRAIN_ENV),
        ),
        Scenario(
            name="update-races-judge",
            family=FAMILY,
            tier=Tier.OFFLINE,
            run=run_update_races_judge,
            env_overrides=dict(SLOW_JUDGE_ENV),
        ),
        Scenario(
            name="log-interleave-tolerance",
            family=FAMILY,
            tier=Tier.OFFLINE,
            run=run_log_interleave_tolerance,
            env_overrides=dict(NO_BRAIN_ENV),
        ),
    )
