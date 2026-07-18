"""F13 — timestamps drive everything: UTC day counting, tz normalization, and PR staleness."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from captain_hook.review.store import ReviewStore
from stress import corpus
from stress.db import one, query
from stress.drivers.proc import capt_hook
from stress.scenarios.base import Scenario, ScenarioResult, Tier, check, expect
from stress.seeds import inject_verdict
from tests.test_review_scan import assistant_text, user_text, write_transcript

if TYPE_CHECKING:
    from pathlib import Path
    from subprocess import CompletedProcess

    from captain_hook.review.judge import Category
    from stress.sandbox import Sandbox

FAMILY = "clock"
TEXT_A = corpus.DURABLE_CORRECTION_LIVE
TEXT_B = "never push directly to main, open a pull request instead"


def review_cli(sandbox: Sandbox, *args: str, **env_overrides: str) -> CompletedProcess[str]:
    return capt_hook(
        "review",
        *args,
        sandbox=sandbox,
        cwd=sandbox.repo,
        env=sandbox.env(CLAUDE_PROJECT_DIR=str(sandbox.repo), **env_overrides),
    )


def enable(sandbox: Sandbox) -> None:
    proc = review_cli(sandbox, "enable")
    assert proc.returncode == 0, proc.stderr


def scan_transcripts(sandbox: Sandbox, *paths: Path) -> CompletedProcess[str]:
    return review_cli(sandbox, "scan", *[arg for path in paths for arg in ("--transcript", str(path))])


def write_correction(sandbox: Sandbox, name: str, *, session: str, ts: str, text: str = TEXT_A) -> Path:
    cwd = str(sandbox.repo)
    return write_transcript(
        sandbox.transcripts / f"{name}.jsonl",
        [
            assistant_text("I'll add a print statement for debugging", sessionId=session, timestamp=ts, cwd=cwd),
            user_text(text, sessionId=session, timestamp=ts, cwd=cwd),
        ],
    )


def inject_all(sandbox: Sandbox, *, category: Category, confidence: float = 0.9) -> list[str]:
    keys = [str(row["dedup_key"]) for row in query(sandbox.review_db, "SELECT dedup_key FROM feedback_events")]

    async def go() -> None:
        with ReviewStore.open(sandbox.review_db) as store:
            for key in keys:
                await inject_verdict(store, key, category=category, confidence=confidence)

    asyncio.run(go())
    return keys


def candidate_id(sandbox: Sandbox, text: str) -> int:
    row = one(
        sandbox.review_db,
        "SELECT o.candidate_id AS id FROM candidate_observations o "
        "JOIN feedback_events e ON e.dedup_key = o.dedup_key WHERE e.text = ? LIMIT 1",
        (text,),
    )
    return int(row["id"])


def threshold_line(sandbox: Sandbox, cid: int) -> str:
    return review_cli(sandbox, "threshold-check", str(cid)).stdout.strip()


def set_pr_opened_at(sandbox: Sandbox, cid: int, *, days_ago: int) -> str:
    stamp = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    with sqlite3.connect(sandbox.review_db) as conn:
        conn.execute("UPDATE candidates SET pr_opened_at = ? WHERE id = ?", (stamp, cid))
    return stamp


def utc_day_boundary(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    spread = [
        write_correction(sandbox, f"spread-{n}", session=f"stress-clk-a{n}", ts=ts)
        for n, ts in enumerate(
            ("2026-06-01T23:59:00+00:00", "2026-06-02T00:01:00+00:00", "2026-06-02T12:00:00+00:00"), 1
        )
    ]
    sameday = [
        write_correction(sandbox, f"sameday-{n}", session=f"stress-clk-b{n}", ts=ts, text=TEXT_B)
        for n, ts in enumerate(
            ("2026-06-01T09:00:00+00:00", "2026-06-01T15:00:00+00:00", "2026-06-01T23:59:00+00:00"), 1
        )
    ]
    scan_transcripts(sandbox, *spread, *sameday)
    inject_all(sandbox, category="tooling_rule")
    cid_spread, cid_sameday = candidate_id(sandbox, TEXT_A), candidate_id(sandbox, TEXT_B)
    return ScenarioResult(
        (
            expect(
                "3 sessions straddling UTC midnight count 2 days",
                threshold_line(sandbox, cid_spread),
                f"#{cid_spread} eligible=True sessions=3/3 days=2/2 open_prs=0/2 watching=True",
            ),
            expect(
                "3 sessions inside one UTC day count 1 day",
                threshold_line(sandbox, cid_sameday),
                f"#{cid_sameday} eligible=False sessions=3/3 days=1/2 open_prs=0/2 watching=True",
            ),
        )
    )


def tz_normalization(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    scan_transcripts(
        sandbox,
        write_correction(sandbox, "tz-tokyo", session="stress-clk-tz1", ts="2026-06-02T08:59:00+09:00"),
        write_correction(sandbox, "tz-utc", session="stress-clk-tz2", ts="2026-06-02T00:01:00+00:00"),
    )
    inject_all(sandbox, category="tooling_rule")
    stored = [
        str(row["occurred_at"])
        for row in query(sandbox.review_db, "SELECT occurred_at FROM candidate_observations ORDER BY session_id")
    ]
    cid = candidate_id(sandbox, TEXT_A)
    return ScenarioResult(
        (
            expect(
                "occurred_at stored UTC-normalized",
                stored,
                ["2026-06-01T23:59:00+00:00", "2026-06-02T00:01:00+00:00"],
            ),
            expect(
                "+09:00 observation lands on UTC day 06-01",
                threshold_line(sandbox, cid),
                f"#{cid} eligible=False sessions=2/3 days=2/2 open_prs=0/2 watching=True",
            ),
        )
    )


def stale_frees_slot(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    scan_transcripts(
        sandbox,
        write_correction(sandbox, "slot-pr", session="stress-clk-s1", ts="2026-06-01T12:00:00+00:00"),
        write_correction(sandbox, "slot-watch", session="stress-clk-s2", ts="2026-06-01T13:00:00+00:00", text=TEXT_B),
    )
    cid_pr, cid_watch = candidate_id(sandbox, TEXT_A), candidate_id(sandbox, TEXT_B)
    review_cli(sandbox, "update", str(cid_pr), "pr_open", "--pr-url", sandbox.pr_url(1))
    old_stamp = set_pr_opened_at(sandbox, cid_pr, days_ago=31)
    aged = threshold_line(sandbox, cid_watch)
    fresh_stamp = set_pr_opened_at(sandbox, cid_pr, days_ago=29)
    return ScenarioResult(
        (
            expect(
                "31-day-old PR frees the slot",
                aged,
                f"#{cid_watch} eligible=False sessions=0/3 days=0/2 open_prs=0/2 watching=True",
            ),
            expect(
                "29-day-old PR still holds the slot",
                threshold_line(sandbox, cid_watch),
                f"#{cid_watch} eligible=False sessions=0/3 days=0/2 open_prs=1/2 watching=True",
            ),
            check("pr_opened_at time travel applied", old_stamp < fresh_stamp, f"old={old_stamp} fresh={fresh_stamp}"),
        )
    )


def sync_stale_transition(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    scan_transcripts(
        sandbox,
        write_correction(sandbox, "sync-old", session="stress-clk-y1", ts="2026-06-01T12:00:00+00:00"),
        write_correction(sandbox, "sync-new", session="stress-clk-y2", ts="2026-06-01T13:00:00+00:00", text=TEXT_B),
    )
    cid_old, cid_new = candidate_id(sandbox, TEXT_A), candidate_id(sandbox, TEXT_B)
    urls = {cid_old: sandbox.pr_url(1), cid_new: sandbox.pr_url(2)}
    for cid, url in urls.items():
        review_cli(sandbox, "update", str(cid), "pr_open", "--pr-url", url)
    set_pr_opened_at(sandbox, cid_old, days_ago=31)
    set_pr_opened_at(sandbox, cid_new, days_ago=29)
    config = sandbox.root / "gh-stub.json"
    config.write_text(json.dumps(dict.fromkeys(urls.values(), "OPEN")))
    synced = review_cli(sandbox, "sync-prs", STRESS_GH_STUB_CONFIG=str(config))
    statuses = {row["id"]: row["status"] for row in query(sandbox.review_db, "SELECT id, status FROM candidates")}
    return ScenarioResult(
        (
            expect("sync-prs stdout", synced.stdout.strip(), "accepted 0, rejected 0, stale 1, unreachable 0"),
            expect(
                "31d OPEN PR goes stale, 29d stays pr_open",
                (statuses[cid_old], statuses[cid_new]),
                ("stale", "pr_open"),
            ),
        )
    )


def scenarios() -> tuple[Scenario, ...]:
    return tuple(
        Scenario(name=name, family=FAMILY, tier=Tier.OFFLINE, run=run)
        for name, run in (
            ("utc-day-boundary", utc_day_boundary),
            ("tz-normalization", tz_normalization),
            ("stale-frees-slot", stale_frees_slot),
            ("sync-stale-transition", sync_stale_transition),
        )
    )
