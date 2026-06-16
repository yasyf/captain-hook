"""F5 — incremental scan semantics: the files-table mtime gate and parse-failure retry."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from stress import corpus
from stress.db import count, one, query
from stress.drivers.proc import capt_hook, review_run, wait_drained
from stress.scenarios.base import Scenario, ScenarioResult, Tier, expect
from tests.test_review_scan import write_transcript

if TYPE_CHECKING:
    from pathlib import Path

    from stress.drivers.proc import SpawnReportLine
    from stress.sandbox import Sandbox

FAMILY = "mtime"
ENV = {"HOOKS_REVIEW_MAX_OPEN_PRS": "0"}
SECOND_CORRECTION = "always run the unit suite before claiming a fix works [[judge:workflow_rule]]"
EVENT_COUNT = "SELECT COUNT(*) FROM feedback_events"
OBSERVATION_COUNT = "SELECT COUNT(*) FROM candidate_observations"
FILE_ROW_COUNT = "SELECT COUNT(*) FROM files WHERE path = ?"


def enable(sandbox: Sandbox) -> None:
    proc = capt_hook(
        "review",
        "enable",
        sandbox=sandbox,
        cwd=sandbox.repo,
        env=sandbox.env(CLAUDE_PROJECT_DIR=str(sandbox.repo)),
    )
    assert proc.returncode == 0, proc.stderr


def run_and_report(sandbox: Sandbox, path: Path, *, run: int) -> SpawnReportLine:
    review_run(sandbox, path)
    return wait_drained(sandbox, count=run, timeout=120)[run - 1]


def append_correction(path: Path, text: str) -> None:
    with path.open("a") as fh:
        fh.writelines(json.dumps(entry) + "\n" for entry in corpus.correction_turns(text))


def recorded_mtime(sandbox: Sandbox, path: Path) -> float:
    return float(one(sandbox.review_db, "SELECT mtime FROM files WHERE path = ?", (str(path),))["mtime"])


def db_counts(sandbox: Sandbox) -> tuple[int, int]:
    return (count(sandbox.review_db, EVENT_COUNT), count(sandbox.review_db, OBSERVATION_COUNT))


def rescan_noop(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    path = corpus.write(corpus.durable_correction("rescan-noop"), sandbox.transcripts)
    first = run_and_report(sandbox, path, run=1)
    before = db_counts(sandbox)
    second = run_and_report(sandbox, path, run=2)
    return ScenarioResult(
        (
            expect("first scan (scanned, inserted)", (first.scanned, first.inserted), (1, 1)),
            expect("rescan (scanned, inserted)", (second.scanned, second.inserted), (0, 0)),
            expect("(events, observations) after first scan", before, (1, 1)),
            expect("(events, observations) after rescan", db_counts(sandbox), before),
        )
    )


def append_newer_mtime(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    path = corpus.write(corpus.durable_correction("append-newer"), sandbox.transcripts)
    first = run_and_report(sandbox, path, run=1)
    append_correction(path, SECOND_CORRECTION)
    os.utime(path, (path.stat().st_atime, recorded_mtime(sandbox, path) + 10))
    second = run_and_report(sandbox, path, run=2)
    texts = [row["text"] for row in query(sandbox.review_db, "SELECT text FROM feedback_events ORDER BY id")]
    return ScenarioResult(
        (
            expect("first scan (scanned, inserted)", (first.scanned, first.inserted), (1, 1)),
            expect("rescan after newer mtime (scanned, inserted)", (second.scanned, second.inserted), (1, 1)),
            expect("feedback_events texts", texts, [corpus.DURABLE_CORRECTION, SECOND_CORRECTION]),
        )
    )


def same_mtime_rewrite(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    path = corpus.write(corpus.durable_correction("same-mtime"), sandbox.transcripts)
    first = run_and_report(sandbox, path, run=1)
    recorded = recorded_mtime(sandbox, path)
    append_correction(path, SECOND_CORRECTION)
    os.utime(path, (path.stat().st_atime, recorded))
    second = run_and_report(sandbox, path, run=2)
    return ScenarioResult(
        (
            expect("first scan (scanned, inserted)", (first.scanned, first.inserted), (1, 1)),
            expect("rewrite at recorded mtime (scanned, inserted)", (second.scanned, second.inserted), (0, 0)),
            expect("appended correction never ingested: events", count(sandbox.review_db, EVENT_COUNT), 1),
        ),
        finding=(
            "mtime is the only change detector: a transcript rewritten in place with its mtime restored to the "
            "recorded value rescans as scanned=0, so the appended correction is silently never ingested"
        ),
    )


def older_mtime(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    path = corpus.write(corpus.durable_correction("older-mtime"), sandbox.transcripts)
    first = run_and_report(sandbox, path, run=1)
    recorded = recorded_mtime(sandbox, path)
    append_correction(path, SECOND_CORRECTION)
    os.utime(path, (path.stat().st_atime, recorded - 100))
    second = run_and_report(sandbox, path, run=2)
    return ScenarioResult(
        (
            expect("first scan (scanned, inserted)", (first.scanned, first.inserted), (1, 1)),
            expect("rescan with older mtime (scanned, inserted)", (second.scanned, second.inserted), (0, 0)),
            expect("events after older-mtime rescan", count(sandbox.review_db, EVENT_COUNT), 1),
        )
    )


def parse_failure_retry(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    entries = [dict(entry) for entry in corpus.durable_correction("parse-retry").entries]
    broken = {key: value for key, value in entries[1].items() if key != "uuid"}
    path = write_transcript(sandbox.transcripts / "parse-retry.jsonl", [entries[0], broken])
    first = run_and_report(sandbox, path, run=1)
    rows_while_broken = count(sandbox.review_db, FILE_ROW_COUNT, (str(path),))
    write_transcript(path, entries)
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 10))
    second = run_and_report(sandbox, path, run=2)
    return ScenarioResult(
        (
            expect("broken transcript (scanned, inserted)", (first.scanned, first.inserted), (0, 0)),
            expect("files rows while broken", rows_while_broken, 0),
            expect("fixed transcript (scanned, inserted)", (second.scanned, second.inserted), (1, 1)),
            expect("files rows after fix", count(sandbox.review_db, FILE_ROW_COUNT, (str(path),)), 1),
        )
    )


def scenarios() -> tuple[Scenario, ...]:
    return tuple(
        Scenario(name=name, family=FAMILY, tier=Tier.OFFLINE, run=run, env_overrides=dict(ENV))
        for name, run in (
            ("rescan-noop", rescan_noop),
            ("append-newer-mtime", append_newer_mtime),
            ("same-mtime-rewrite", same_mtime_rewrite),
            ("older-mtime", older_mtime),
            ("parse-failure-retry", parse_failure_retry),
        )
    )
