"""Watch-gating scenarios: the unwatched no-op path, enable/disable wiring, and the watch-free scan CLI."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from stress.corpus import durable_correction, write
from stress.db import count, one, query
from stress.drivers.proc import capt_hook, review_run, wait_for_report
from stress.scenarios.base import Scenario, ScenarioResult, Tier, check, expect

if TYPE_CHECKING:
    from pathlib import Path

    from stress.sandbox import Sandbox

FAMILY = "gating"
WIRED_COMMAND = "capt-hook review run"


def cli_env(sandbox: Sandbox, **overrides: str) -> dict[str, str]:
    return {key: value for key, value in sandbox.env(**overrides).items() if key != "CLAUDE_PROJECT_DIR"}


def expected_repo(sandbox: Sandbox) -> str:
    return f"github.com/capt-hook-stress/{sandbox.root.name}"


def settings_path(sandbox: Sandbox) -> Path:
    return sandbox.repo / ".claude" / "settings.json"


def wired_commands(sandbox: Sandbox) -> list[str]:
    hooks = json.loads(settings_path(sandbox).read_text()).get("hooks") or {}
    return [
        command
        for group in hooks.get("SessionEnd") or []
        for entry in group.get("hooks") or []
        if WIRED_COMMAND in (command := str(entry.get("command") or ""))
    ]


def settings_digest(sandbox: Sandbox) -> str:
    return hashlib.sha256(settings_path(sandbox).read_bytes()).hexdigest()


def unwatched_noop(sandbox: Sandbox) -> ScenarioResult:
    transcript = write(durable_correction("gating-unwatched"), sandbox.transcripts)
    proc = review_run(sandbox, transcript)
    reports = wait_for_report(sandbox, count=1, timeout=120)
    report = reports[0] if reports else None
    return ScenarioResult(
        checks=(
            check(
                "unwatched spawn bails with watching=False scanned=0",
                report is not None
                and report.repo == expected_repo(sandbox)
                and not report.watching
                and report.scanned == 0,
                f"rc={proc.returncode} report={report} log={sandbox.spawn_log_text()[:300]!r}",
            ),
            expect("feedback_events rows", count(sandbox.review_db, "SELECT COUNT(*) FROM feedback_events"), 0),
        )
    )


def enable_disable_lifecycle(sandbox: Sandbox) -> ScenarioResult:
    enable_first = capt_hook("review", "enable", sandbox=sandbox, cwd=sandbox.repo, env=cli_env(sandbox))
    repos_enabled = one(sandbox.review_db, "SELECT repo_key, watching FROM repos")
    first_commands = wired_commands(sandbox)
    first_digest = settings_digest(sandbox)
    enable_again = capt_hook("review", "enable", sandbox=sandbox, cwd=sandbox.repo, env=cli_env(sandbox))
    second_digest = settings_digest(sandbox)
    second_commands = wired_commands(sandbox)

    watched = write(durable_correction("gating-watched"), sandbox.transcripts)
    review_run(sandbox, watched)
    watched_report = wait_for_report(sandbox, count=1, timeout=120)[0]
    before_rows = query(sandbox.review_db, "SELECT id, repo_key, candidate_kind, status FROM candidates ORDER BY id")

    disable = capt_hook("review", "disable", sandbox=sandbox, cwd=sandbox.repo, env=cli_env(sandbox))
    repos_disabled = one(sandbox.review_db, "SELECT repo_key, watching FROM repos")
    after_disable = write(durable_correction("gating-after-disable", day=2), sandbox.transcripts)
    review_run(sandbox, after_disable)
    disabled_report = wait_for_report(sandbox, count=2, timeout=120)[-1]
    after_rows = query(sandbox.review_db, "SELECT id, repo_key, candidate_kind, status FROM candidates ORDER BY id")

    return ScenarioResult(
        checks=(
            check(
                "enable marks repo watching=1",
                enable_first.returncode == 0 and repos_enabled == {"repo_key": expected_repo(sandbox), "watching": 1},
                f"rc={enable_first.returncode} stdout={enable_first.stdout!r} row={repos_enabled}",
            ),
            check(
                "SessionEnd wired exactly once",
                first_commands == [f"uvx {WIRED_COMMAND}"],
                f"commands={first_commands} stdout={enable_first.stdout!r}",
            ),
            check(
                "re-enable leaves settings byte-identical",
                enable_again.returncode == 0 and first_digest == second_digest and len(second_commands) == 1,
                f"digests=({first_digest[:12]}, {second_digest[:12]}) commands={second_commands} "
                f"stdout={enable_again.stdout!r}",
            ),
            check(
                "watched run scans and inserts",
                watched_report.watching and watched_report.scanned == 1 and watched_report.inserted == 1,
                watched_report,
            ),
            check(
                "disable marks repo watching=0",
                disable.returncode == 0 and repos_disabled == {"repo_key": expected_repo(sandbox), "watching": 0},
                f"rc={disable.returncode} stdout={disable.stdout!r} row={repos_disabled}",
            ),
            check(
                "post-disable run reports watching=False without scanning",
                not disabled_report.watching and disabled_report.scanned == 0,
                disabled_report,
            ),
            check(
                "candidates survive disable",
                after_rows == before_rows and len(after_rows) == 1,
                f"before={before_rows} after={after_rows}",
            ),
        )
    )


def scan_cli_unwatched(sandbox: Sandbox) -> ScenarioResult:
    transcript = write(durable_correction("gating-scan-cli"), sandbox.transcripts, cwd=sandbox.repo)
    proc = capt_hook("review", "scan", "--transcript", str(transcript), sandbox=sandbox)
    events = query(sandbox.review_db, "SELECT dedup_key, source_kind, session_id FROM feedback_events")
    candidates = query(sandbox.review_db, "SELECT id, repo_key, candidate_kind, status FROM candidates")
    return ScenarioResult(
        checks=(
            check(
                "scan exits 0 reporting 1 new correction",
                proc.returncode == 0 and "scanned 1 transcripts, 1 new corrections" in proc.stdout,
                f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr[:200]!r}",
            ),
            check(
                "feedback event inserted despite unwatched repo",
                len(events) == 1 and events[0]["source_kind"] == "transcript_message",
                events,
            ),
            check(
                "candidate created with no repos row",
                len(candidates) == 1
                and candidates[0]["repo_key"] == expected_repo(sandbox)
                and count(sandbox.review_db, "SELECT COUNT(*) FROM repos") == 0,
                f"candidates={candidates} repos={query(sandbox.review_db, 'SELECT * FROM repos')}",
            ),
        )
    )


def scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario(name="gating-unwatched-noop", family=FAMILY, tier=Tier.OFFLINE, run=unwatched_noop),
        Scenario(
            name="gating-enable-lifecycle",
            family=FAMILY,
            tier=Tier.OFFLINE,
            run=enable_disable_lifecycle,
            env_overrides={"HOOKS_REVIEW_MAX_OPEN_PRS": "0"},
        ),
        Scenario(name="gating-scan-cli-unwatched", family=FAMILY, tier=Tier.OFFLINE, run=scan_cli_unwatched),
    )
