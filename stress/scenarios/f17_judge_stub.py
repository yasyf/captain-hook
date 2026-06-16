"""F17 — judge mechanics under the deterministic claude stub: failures, caps, idempotence, tier flips."""

from __future__ import annotations

from typing import TYPE_CHECKING

from stress import corpus
from stress.db import count, one, query
from stress.drivers.proc import capt_hook, review_run, wait_drained
from stress.scenarios.base import Scenario, ScenarioResult, Tier, expect
from tests.test_review_scan import write_transcript

if TYPE_CHECKING:
    from pathlib import Path
    from subprocess import CompletedProcess
    from typing import Any

    from stress.drivers.proc import SpawnReportLine
    from stress.sandbox import Sandbox

FAMILY = "judgestub"
ENV = {"HOOKS_REVIEW_MAX_OPEN_PRS": "0"}
CAP_ENV = ENV | {"HOOKS_REVIEW_MAX_JUDGE_CALLS_PER_SESSION": "3"}
SECOND_CORRECTION = "always run the unit suite before claiming a fix works [[judge:workflow_rule]]"
LIVE = corpus.DURABLE_CORRECTION_LIVE
VERDICT_COUNT = "SELECT COUNT(*) FROM verdicts"


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


def wait_and_drain(sandbox: Sandbox, *, run: int) -> SpawnReportLine:
    return wait_drained(sandbox, count=run, timeout=120)[run - 1]


def correction_pairs(texts: list[str], *, session: str) -> list[dict[str, Any]]:
    return [entry for text in texts for entry in corpus.correction_turns(text, session=session)]


def write_live_corrections(sandbox: Sandbox, slug: str, *, n: int) -> list[Path]:
    return [
        corpus.write(
            corpus.durable_correction(f"{slug}-{i}", session=f"stress-{slug}-{i}", text=f"{LIVE} ({slug} rule {i})"),
            sandbox.transcripts,
            cwd=sandbox.repo,
        )
        for i in range(1, n + 1)
    ]


def fail_stays_pending(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    first_path = corpus.write(corpus.durable_correction("judge-fail-a"), sandbox.transcripts)
    review_run(sandbox, first_path, env=sandbox.env(STRESS_JUDGE_STUB="fail"))
    first = wait_and_drain(sandbox, run=1)
    key = str(one(sandbox.review_db, "SELECT dedup_key FROM feedback_events")["dedup_key"])
    verdicts_while_failed = count(sandbox.review_db, "SELECT COUNT(*) FROM verdicts WHERE dedup_key = ?", (key,))
    second_path = corpus.write(
        corpus.durable_correction("judge-fail-b", session="stress-sess-2", text=SECOND_CORRECTION),
        sandbox.transcripts,
    )
    review_run(sandbox, second_path)
    second = wait_and_drain(sandbox, run=2)
    verdict = one(sandbox.review_db, "SELECT category, accepted FROM verdicts WHERE dedup_key = ?", (key,))
    return ScenarioResult(
        (
            expect("failing stub (judged, failed)", (first.judged, first.failed), (0, 1)),
            expect("verdict rows for the failed key", verdicts_while_failed, 0),
            expect("next pass retries the failed row (judged, failed)", (second.judged, second.failed), (2, 0)),
            expect("retried verdict", verdict, {"category": "tooling_rule", "accepted": 1}),
        )
    )


def garbage_output(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    path = corpus.write(corpus.durable_correction("judge-garbage"), sandbox.transcripts)
    review_run(sandbox, path, env=sandbox.env(STRESS_JUDGE_STUB="garbage"))
    report = wait_and_drain(sandbox, run=1)
    return ScenarioResult(
        (
            expect("garbage stub (judged, failed)", (report.judged, report.failed), (0, 1)),
            expect("verdict rows", count(sandbox.review_db, VERDICT_COUNT), 0),
            expect("tracebacks in spawn.log", sandbox.spawn_log_text().count("Traceback"), 0),
        )
    )


def cap_respected(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    stub_log = sandbox.root / "judge-calls.log"
    env = sandbox.env(STRESS_JUDGE_STUB_LOG=str(stub_log))
    big = write_transcript(
        sandbox.transcripts / "cap-seven.jsonl",
        correction_pairs([f"{LIVE} (case {n})" for n in range(7)], session="stress-cap-1"),
    )
    review_run(sandbox, big, env=env)
    first = wait_and_drain(sandbox, run=1)
    first_calls = len(stub_log.read_text().splitlines())
    extra = write_transcript(
        sandbox.transcripts / "cap-extra.jsonl", correction_pairs([f"{LIVE} (case 7)"], session="stress-cap-2")
    )
    review_run(sandbox, extra, env=env)
    second = wait_and_drain(sandbox, run=2)
    return ScenarioResult(
        (
            expect("first pass (scanned, inserted, judged)", (first.scanned, first.inserted, first.judged), (1, 7, 3)),
            expect("stub calls after first pass", first_calls, 3),
            expect("second pass judges the cap again", second.judged, 3),
            expect("stub calls after second pass", len(stub_log.read_text().splitlines()), 6),
        )
    )


def triage_idempotent(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    paths = write_live_corrections(sandbox, "triage", n=2)
    review_cli(sandbox, "scan", *[arg for path in paths for arg in ("--transcript", str(path))])
    first = review_cli(sandbox, "triage")
    second = review_cli(sandbox, "triage")
    return ScenarioResult(
        (
            expect("first triage", first.stdout.strip(), "judged 2, failed 0, pending 0"),
            expect("triage re-run is a no-op", second.stdout.strip(), "judged 0, failed 0, pending 0"),
            expect("verdict rows", count(sandbox.review_db, VERDICT_COUNT), 2),
        )
    )


def tier_switch_rejudges(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    paths = write_live_corrections(sandbox, "tier", n=3)
    review_cli(sandbox, "scan", *[arg for path in paths for arg in ("--transcript", str(path))])
    baseline = review_cli(sandbox, "triage")
    rejudged = review_cli(sandbox, "triage", HOOKS_REVIEW_JUDGE_TIER="large")
    models = sorted({str(row["model"]) for row in query(sandbox.review_db, "SELECT DISTINCT model FROM verdicts")})
    return ScenarioResult(
        (
            expect("baseline triage", baseline.stdout.strip(), "judged 3, failed 0, pending 0"),
            expect("tier-switch triage re-judges every row", rejudged.stdout.strip(), "judged 3, failed 0, pending 0"),
            expect("verdict rows doubled across models", count(sandbox.review_db, VERDICT_COUNT), 6),
            expect("distinct verdict models", len(models), 2),
        ),
        finding=(
            "flipping HOOKS_REVIEW_JUDGE_TIER re-judges the entire stored corpus: verdicts key on the resolved "
            "model string, so a tier change re-runs one LLM call per already-judged row (cost amplification) "
            "instead of reusing existing verdicts"
        ),
    )


def scenarios() -> tuple[Scenario, ...]:
    return tuple(
        Scenario(name=name, family=FAMILY, tier=Tier.OFFLINE, run=run, env_overrides=dict(env))
        for name, run, env in (
            ("fail-stays-pending", fail_stays_pending, ENV),
            ("garbage-output", garbage_output, ENV),
            ("cap-respected", cap_respected, CAP_ENV),
            ("triage-idempotent", triage_idempotent, ENV),
            ("tier-switch-rejudges", tier_switch_rejudges, ENV),
        )
    )
