"""Transcript pathology: malformed, truncated, oversized, and oddly-pathed inputs to the reviewer child."""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from tests.test_review_scan import assistant_text, user_text, write_transcript

from stress.corpus import (
    DURABLE_CORRECTION,
    durable_correction,
    write,
    write_binary_garbage,
    write_huge_line,
    write_perf_transcript,
    write_truncated,
    write_undecodable_splice,
)
from stress.db import count, one, query
from stress.drivers.proc import capt_hook, review_run, spawn_reports, wait_drained
from stress.scenarios.base import Scenario, ScenarioResult, Tier, check, expect

if TYPE_CHECKING:
    from pathlib import Path
    from subprocess import CompletedProcess

    from stress.drivers.proc import SpawnReportLine
    from stress.sandbox import Sandbox

FAMILY = "pathology"
NO_BRAIN_ENV = {"HOOKS_REVIEW_MAX_OPEN_PRS": "0"}


def repo_cli(sandbox: Sandbox, *args: str) -> CompletedProcess[str]:
    return capt_hook(*args, sandbox=sandbox, cwd=sandbox.repo, env=sandbox.env(CLAUDE_PROJECT_DIR=str(sandbox.repo)))


def enable(sandbox: Sandbox) -> None:
    if (proc := repo_cli(sandbox, "review", "enable")).returncode != 0:
        raise RuntimeError(f"review enable failed: {proc.stdout}{proc.stderr}")


def fire(sandbox: Sandbox, path: Path, *, timeout: float = 120) -> tuple[SpawnReportLine | None, float]:
    before = len(spawn_reports(sandbox))
    start = time.monotonic()
    review_run(sandbox, path)
    reports = wait_drained(sandbox, count=before + 1, timeout=timeout)
    elapsed = time.monotonic() - start
    return (reports[-1] if len(reports) > before else None, elapsed)


def files_row(sandbox: Sandbox, path: Path) -> dict[str, object] | None:
    return one(sandbox.review_db, "SELECT * FROM files WHERE path = ?", (str(path),))


def no_traceback(sandbox: Sandbox) -> tuple[bool, str]:
    log = sandbox.spawn_log_text()
    lines = "\n".join(line for line in log.splitlines() if "Traceback" in line or "Error" in line)
    return "Traceback" not in log, lines or f"(no Traceback/Error lines in {len(log)} chars of spawn.log)"


def run_empty_file(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    path = sandbox.transcripts / "stress-empty.jsonl"
    path.write_text("")
    report, _ = fire(sandbox, path)
    clean, excerpt = no_traceback(sandbox)
    return ScenarioResult(
        checks=(
            check(
                "empty file recorded as scanned with zero candidates",
                report is not None and report.scanned == 1 and report.inserted == 0,
                f"report={report} files_row={files_row(sandbox, path)}",
            ),
            check("files row recorded for the empty file", files_row(sandbox, path) is not None, files_row(sandbox, path)),
            check("no Traceback in spawn.log", clean, excerpt),
        ),
    )


def run_truncated_mid_line(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    planted = durable_correction("stress-truncated", session="stress-trunc-1")
    path = write_truncated(planted, sandbox.transcripts)
    first, _ = fire(sandbox, path)
    first_row = files_row(sandbox, path)
    full = write(planted, sandbox.transcripts)
    recorded = float(first_row["mtime"]) if first_row else 0.0
    os.utime(full, (time.time(), max(time.time(), recorded + 5)))
    second, _ = fire(sandbox, full)
    clean, excerpt = no_traceback(sandbox)
    return ScenarioResult(
        checks=(
            check(
                "truncated pass commits a files row with zero insertions",
                first is not None and first.scanned == 1 and first.inserted == 0 and first_row is not None,
                f"first_report={first} files_row={first_row}",
            ),
            check(
                "full rewrite with newer mtime rescans and inserts",
                second is not None and second.scanned == 1 and second.inserted == 1,
                f"second_report={second} files_row={files_row(sandbox, full)}",
            ),
            expect("feedback_events after retry", count(sandbox.review_db, "SELECT COUNT(*) FROM feedback_events"), 1),
            check("no Traceback in spawn.log", clean, excerpt),
        ),
        finding=(
            "a transcript truncated mid-line does not fail to parse: the surviving prefix is committed as "
            "scanned (files row, inserted=0), so the cut-off correction is recovered only when a later rewrite "
            "bumps the mtime; an unchanged-mtime completion of the file would be skipped by the gate forever"
        ),
    )


def run_binary_garbage(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    path = write_binary_garbage(sandbox.transcripts, "stress-binary-garbage")
    report, _ = fire(sandbox, path)
    row = files_row(sandbox, path)
    clean, excerpt = no_traceback(sandbox)
    return ScenarioResult(
        checks=(
            check(
                "binary garbage recorded-with-zero (files row, scanned=1, inserted=0)",
                report is not None and report.scanned == 1 and report.inserted == 0 and row is not None,
                f"report={report} files_row={row}",
            ),
            expect("feedback_events", count(sandbox.review_db, "SELECT COUNT(*) FROM feedback_events"), 0),
            check("no Traceback in spawn.log", clean, excerpt),
        ),
    )


def run_undecodable_splice(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    planted = durable_correction("stress-splice", session="stress-splice-1")
    path = write_undecodable_splice(planted, sandbox.transcripts)
    report, _ = fire(sandbox, path)
    clean, excerpt = no_traceback(sandbox)
    return ScenarioResult(
        checks=(
            check(
                "spliced garbage line tolerated: surviving correction inserted",
                report is not None and report.scanned == 1 and report.inserted == 1,
                f"report={report} files_row={files_row(sandbox, path)}",
            ),
            expect("feedback_events", count(sandbox.review_db, "SELECT COUNT(*) FROM feedback_events"), 1),
            check("no Traceback in spawn.log", clean, excerpt),
        ),
    )


def run_huge_line(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    path = write_huge_line(sandbox.transcripts, megabytes=10)
    report, elapsed = fire(sandbox, path, timeout=60)
    clean, excerpt = no_traceback(sandbox)
    return ScenarioResult(
        checks=(
            check(
                "10MB single line completes < 60s",
                report is not None and elapsed < 60,
                f"elapsed={elapsed:.2f}s report={report}",
            ),
            check("no Traceback in spawn.log", clean, excerpt),
        ),
    )


def run_missing_fields_line(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    session = "stress-missing-1"
    entries = [
        assistant_text("I'll add a print statement for debugging", sessionId=session),
        user_text(DURABLE_CORRECTION, sessionId=session),
    ]
    entries[1].pop("uuid")
    path = write_transcript(sandbox.transcripts / "stress-missing-fields.jsonl", entries)
    report, _ = fire(sandbox, path)
    clean, excerpt = no_traceback(sandbox)
    return ScenarioResult(
        checks=(
            check(
                "missing-uuid file dropped whole: report scanned=0, no files row, nothing inserted",
                report is not None
                and report.scanned == 0
                and report.inserted == 0
                and files_row(sandbox, path) is None
                and count(sandbox.review_db, "SELECT COUNT(*) FROM feedback_events") == 0,
                f"report={report} files_row={files_row(sandbox, path)}",
            ),
            check("no Traceback in spawn.log", clean, excerpt),
        ),
    )


def run_symlink_transcript(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    real = write(durable_correction("stress-symlink-real", session="stress-symlink-1"), sandbox.transcripts)
    link = sandbox.transcripts / "stress-symlink-link.jsonl"
    link.symlink_to(real)
    report, _ = fire(sandbox, link)
    rows = query(sandbox.review_db, "SELECT * FROM files")
    return ScenarioResult(
        checks=(
            check(
                "symlinked transcript scans and inserts",
                report is not None and report.scanned == 1 and report.inserted == 1,
                f"report={report}",
            ),
            check(
                "files row records the symlink path string, not the target",
                [str(row["path"]) for row in rows] == [str(link)],
                f"files_rows={rows} link={link} real={real}",
            ),
        ),
    )


def run_spaces_in_path(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    path = write(durable_correction("stress has spaces", session="stress-spaces-1"), sandbox.transcripts)
    report, _ = fire(sandbox, path)
    return ScenarioResult(
        checks=(
            check(
                "transcript path with spaces scans end-to-end",
                report is not None and report.scanned == 1 and report.inserted == 1,
                f"report={report} path={path}",
            ),
            check("files row holds the spaced path", files_row(sandbox, path) is not None, files_row(sandbox, path)),
        ),
    )


def run_perf_100k(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    path = write_perf_transcript(sandbox.transcripts)
    report, elapsed = fire(sandbox, path, timeout=240)
    log = sandbox.spawn_log_text()
    clean, excerpt = no_traceback(sandbox)
    return ScenarioResult(
        checks=(
            check(
                "100k-event transcript completes < 180s",
                report is not None and elapsed < 180,
                f"elapsed={elapsed:.2f}s report={report}",
            ),
            check(
                "exactly 20 planted corrections inserted",
                report is not None and report.inserted == 20,
                f"report={report} events={count(sandbox.review_db, 'SELECT COUNT(*) FROM feedback_events')}",
            ),
            check(
                "no Traceback or MemoryError in spawn.log",
                clean and "MemoryError" not in log,
                excerpt,
            ),
        ),
    )


def scenarios() -> tuple[Scenario, ...]:
    cases = (
        ("empty-file", run_empty_file, NO_BRAIN_ENV),
        ("truncated-mid-line", run_truncated_mid_line, NO_BRAIN_ENV),
        ("binary-garbage", run_binary_garbage, NO_BRAIN_ENV),
        ("undecodable-splice", run_undecodable_splice, NO_BRAIN_ENV),
        ("huge-line", run_huge_line, NO_BRAIN_ENV),
        ("missing-fields-line", run_missing_fields_line, NO_BRAIN_ENV),
        ("symlink-transcript", run_symlink_transcript, NO_BRAIN_ENV),
        ("spaces-in-path", run_spaces_in_path, NO_BRAIN_ENV),
        ("perf-100k", run_perf_100k, NO_BRAIN_ENV),
    )
    return tuple(
        Scenario(name=name, family=FAMILY, tier=Tier.OFFLINE, run=run, env_overrides=dict(env))
        for name, run, env in cases
    )
