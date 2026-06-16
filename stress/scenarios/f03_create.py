"""F3 — CREATE detector matrix: coverage, noise immunity, dedup scoping, grouping, hedging."""

from __future__ import annotations

from typing import TYPE_CHECKING

from stress.corpus import (
    DURABLE_CORRECTION,
    HEDGED_CORRECTION,
    Planted,
    correction_turns,
    durable_correction,
    interrupt_followup,
    multi_session,
    noise_traps,
    plan_reentry,
    plan_rejection,
    review_comment_finding,
    review_comment_inline,
    review_comment_workstream,
    tool_denial,
    write,
)
from stress.db import count, query
from stress.drivers.proc import capt_hook, review_run, wait_for_report
from stress.scenarios.base import Scenario, ScenarioResult, Tier, check, expect

if TYPE_CHECKING:
    from collections.abc import Callable

    from stress.sandbox import Sandbox
    from stress.scenarios.base import Check

FAMILY = "create"
NO_BRAIN = {"HOOKS_REVIEW_MAX_OPEN_PRS": "0"}
CROSS_SESSIONS = ("stress-cross-a", "stress-cross-b", "stress-cross-c")
COVERAGE_BUILDERS: tuple[tuple[Callable[..., Planted], str], ...] = (
    (durable_correction, "cov-durable"),
    (plan_rejection, "cov-plan-rejection"),
    (plan_reentry, "cov-plan-reentry"),
    (tool_denial, "cov-tool-denial"),
    (interrupt_followup, "cov-interrupt-followup"),
    (review_comment_inline, "cov-review-inline"),
    (review_comment_finding, "cov-review-finding"),
    (review_comment_workstream, "cov-review-workstream"),
)


def repo_cli(sandbox: Sandbox, *args: str) -> str:
    proc = capt_hook(
        "review",
        *args,
        sandbox=sandbox,
        cwd=sandbox.repo,
        env=sandbox.env(CLAUDE_PROJECT_DIR=str(sandbox.repo)),
    )
    return proc.stdout + proc.stderr


def run_detector_coverage(sandbox: Sandbox) -> ScenarioResult:
    repo_cli(sandbox, "enable")
    checks: list[Check] = []
    for nth, (build, name) in enumerate(COVERAGE_BUILDERS, 1):
        planted = build(name, session=f"stress-{name}")
        review_run(sandbox, write(planted, sandbox.transcripts))
        report = wait_for_report(sandbox, count=nth)[nth - 1]
        rows = query(
            sandbox.review_db,
            "SELECT source_kind, text FROM feedback_events WHERE session_id = ?",
            (f"stress-{name}",),
        )
        checks.append(expect(f"{name} inserted", report.inserted, planted.expected_inserted))
        checks.append(
            check(
                f"{name} kinds == {sorted(planted.expected_kinds)}",
                {row["source_kind"] for row in rows} == set(planted.expected_kinds),
                rows,
            )
        )
    return ScenarioResult(tuple(checks))


def run_noise_sweep(sandbox: Sandbox) -> ScenarioResult:
    repo_cli(sandbox, "enable")
    checks: list[Check] = []
    traps = noise_traps()
    for nth, planted in enumerate(traps, 1):
        review_run(sandbox, write(planted, sandbox.transcripts))
        report = wait_for_report(sandbox, count=nth)[nth - 1]
        checks.append(expect(f"{planted.name} inserted", report.inserted, 0))
    (nongit := sandbox.root / "nongit").mkdir()
    stray = write(durable_correction("nongit-correction", session="stress-nongit"), sandbox.transcripts, cwd=nongit)
    review_run(sandbox, stray, cwd=nongit)
    report = wait_for_report(sandbox, count=len(traps) + 1)[len(traps)]
    checks.append(check("non-git cwd bails with repo=None", report.repo is None and report.scanned == 0, report))
    checks.append(expect("feedback rows", count(sandbox.review_db, "SELECT COUNT(*) FROM feedback_events"), 0))
    checks.append(expect("candidate rows", count(sandbox.review_db, "SELECT COUNT(*) FROM candidates"), 0))
    return ScenarioResult(tuple(checks))


def run_within_session_dedup(sandbox: Sandbox) -> ScenarioResult:
    repo_cli(sandbox, "enable")
    entries = tuple(entry for _ in range(3) for entry in correction_turns(DURABLE_CORRECTION, session="stress-repeat"))
    review_run(sandbox, write(Planted("repeat-correction", entries), sandbox.transcripts))
    report = wait_for_report(sandbox)[0]
    events = query(sandbox.review_db, "SELECT dedup_key, source_kind, session_id FROM feedback_events")
    observations = query(sandbox.review_db, "SELECT candidate_id, dedup_key, session_id FROM candidate_observations")
    return ScenarioResult(
        (
            expect("3x repeated correction inserted once", report.inserted, 1),
            check(
                "one feedback event for the session",
                len(events) == 1 and events[0]["session_id"] == "stress-repeat",
                events,
            ),
            expect("candidate rows", count(sandbox.review_db, "SELECT COUNT(*) FROM candidates"), 1),
            check("one observation", len(observations) == 1, observations),
        )
    )


def run_cross_session_grouping(sandbox: Sandbox) -> ScenarioResult:
    repo_cli(sandbox, "enable")
    for day, session in enumerate(CROSS_SESSIONS, 1):
        review_run(sandbox, write(durable_correction(f"cross-day{day}", session=session, day=day), sandbox.transcripts))
        wait_for_report(sandbox, count=day)
    reports = wait_for_report(sandbox, count=3)
    candidates = query(sandbox.review_db, "SELECT id, candidate_kind, rule, status FROM candidates")
    observations = query(sandbox.review_db, "SELECT candidate_id, session_id, occurred_at FROM candidate_observations")
    listing = repo_cli(sandbox, "list")
    thresholds = repo_cli(sandbox, "threshold-check")
    return ScenarioResult(
        (
            expect("candidate rows", len(candidates), 1),
            check(
                "3 observations across 3 distinct sessions",
                len(observations) == 3 and {row["session_id"] for row in observations} == set(CROSS_SESSIONS),
                observations,
            ),
            check("review list shows x3", "x3:" in listing, listing),
            check(
                "thresholds crossed but PR slots zeroed",
                "sessions=3/3" in thresholds and "eligible=False" in thresholds,
                thresholds,
            ),
            check("brain never spawned", not any(report.brain for report in reports), reports),
        )
    )


def run_hedged_still_candidate(sandbox: Sandbox) -> ScenarioResult:
    repo_cli(sandbox, "enable")
    planted = durable_correction("hedged-correction", session="stress-hedged", text=HEDGED_CORRECTION)
    review_run(sandbox, write(planted, sandbox.transcripts))
    report = wait_for_report(sandbox)[0]
    verdicts = query(sandbox.review_db, "SELECT dedup_key, category, accepted, confidence FROM verdicts")
    thresholds = repo_cli(sandbox, "threshold-check")
    return ScenarioResult(
        (
            expect("hedged correction still inserted", report.inserted, 1),
            expect("candidate rows", count(sandbox.review_db, "SELECT COUNT(*) FROM candidates"), 1),
            check(
                "judge rejected as preference_unclear",
                [(row["category"], row["accepted"]) for row in verdicts] == [("preference_unclear", 0)],
                verdicts,
            ),
            check(
                "accepted sessions stay 0",
                "sessions=0/3" in thresholds and "eligible=False" in thresholds,
                thresholds,
            ),
        )
    )


def run_multi_session_file(sandbox: Sandbox) -> ScenarioResult:
    repo_cli(sandbox, "enable")
    review_run(sandbox, write(multi_session("multi-two-sessions"), sandbox.transcripts))
    report = wait_for_report(sandbox)[0]
    observations = query(sandbox.review_db, "SELECT candidate_id, session_id FROM candidate_observations")
    return ScenarioResult(
        (
            expect("two sessions inserted from one file", report.inserted, 2),
            expect("candidate rows", count(sandbox.review_db, "SELECT COUNT(*) FROM candidates"), 1),
            check(
                "2 distinct sessions observed on one candidate",
                len(observations) == 2
                and len({row["session_id"] for row in observations}) == 2
                and len({row["candidate_id"] for row in observations}) == 1,
                observations,
            ),
        ),
        finding=(
            "one transcript file can contribute multiple distinct session_ids to a single candidate: "
            "eligibility counts distinct sessions, not files, so min_sessions can be satisfied by a "
            "single file carrying several session ids (e.g. a resumed/concatenated transcript)"
        ),
    )


def scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario("create-detector-coverage", FAMILY, Tier.OFFLINE, run_detector_coverage),
        Scenario("create-noise-sweep", FAMILY, Tier.OFFLINE, run_noise_sweep),
        Scenario("create-within-session-dedup", FAMILY, Tier.OFFLINE, run_within_session_dedup),
        Scenario(
            "create-cross-session-grouping",
            FAMILY,
            Tier.OFFLINE,
            run_cross_session_grouping,
            env_overrides=dict(NO_BRAIN),
        ),
        Scenario("create-hedged-still-candidate", FAMILY, Tier.OFFLINE, run_hedged_still_candidate),
        Scenario("create-multi-session-file", FAMILY, Tier.OFFLINE, run_multi_session_file),
    )
