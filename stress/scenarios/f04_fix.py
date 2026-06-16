"""F4 — FIX fold: misfire complaints attributed through the decision ledger."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING

from tests.test_review_fix import (
    STOP_COMPLAINT,
    STOP_MESSAGE,
    STRONG_COMPLAINT,
    nudge_attachment,
    stop_feedback_turn,
)
from tests.test_review_scan import assistant_text, assistant_tool_use, tool_result, user_text

from stress.corpus import Planted, fix_compliance, fix_hedged, fix_strong, turn_ts, write
from stress.db import count, query
from stress.drivers.proc import capt_hook, review_run, wait_for_report
from stress.scenarios.base import Scenario, ScenarioResult, Tier, check, expect
from stress.seeds import NUDGE_MESSAGE, seed_decision

if TYPE_CHECKING:
    from typing import Any

    from stress.drivers.proc import SpawnReportLine
    from stress.sandbox import Sandbox

FAMILY = "fix"
BASE_MS = int(datetime.fromisoformat(turn_ts(1)).timestamp() * 1000)
STATUS_NUDGE_CANDIDATE = {
    "candidate_kind": "fix",
    "status": "watching",
    "target_source_file": ".claude/hooks/status_nudge.py",
    "target_hook_name": "status_nudge:nudge_c424798f",
    "misfire_class": "refire",
}
AMBIGUOUS_SESSION = "stress-fix-ambiguous"
STOP_SESSION = "stress-fix-stop"
GROUP_SESSIONS = ("stress-fix-g1", "stress-fix-g2")
CANDIDATE_COLUMNS = "candidate_kind, status, target_source_file, target_hook_name, misfire_class"


def enable(sandbox: Sandbox) -> None:
    capt_hook(
        "review",
        "enable",
        sandbox=sandbox,
        cwd=sandbox.repo,
        env=sandbox.env(CLAUDE_PROJECT_DIR=str(sandbox.repo)),
    )


def run_planted(sandbox: Sandbox, planted: Planted, *, nth: int = 1) -> SpawnReportLine:
    review_run(sandbox, write(planted, sandbox.transcripts))
    return wait_for_report(sandbox, count=nth)[nth - 1]


def fix_events(sandbox: Sandbox) -> list[dict[str, Any]]:
    return query(sandbox.review_db, "SELECT source_kind, session_id, text, payload_json FROM feedback_events")


def fix_candidates(sandbox: Sandbox) -> list[dict[str, Any]]:
    return query(sandbox.review_db, f"SELECT {CANDIDATE_COLUMNS} FROM candidates")


def ambiguous_planted() -> Planted:
    session = AMBIGUOUS_SESSION
    return Planted(
        "fix-ambiguous-two-targets",
        (
            user_text("run a status check, then commit", sessionId=session, timestamp=turn_ts(0)),
            assistant_tool_use("t1", "Bash", {"command": "git status"}, sessionId=session, timestamp=turn_ts(1)),
            nudge_attachment(NUDGE_MESSAGE, sessionId=session, timestamp=turn_ts(1)),
            tool_result("t1", "clean", sessionId=session, timestamp=turn_ts(1)),
            stop_feedback_turn(STOP_MESSAGE, sessionId=session, timestamp=turn_ts(1)),
            assistant_text(STRONG_COMPLAINT, sessionId=session, timestamp=turn_ts(2)),
        ),
    )


def stop_planted() -> Planted:
    session = STOP_SESSION
    return Planted(
        "fix-stop-feedback",
        (
            user_text("wrap up the change", sessionId=session, timestamp=turn_ts(0)),
            assistant_text("done with the work", sessionId=session, timestamp=turn_ts(1)),
            stop_feedback_turn(STOP_MESSAGE, sessionId=session, timestamp=turn_ts(1)),
            assistant_text(STOP_COMPLAINT, sessionId=session, timestamp=turn_ts(2)),
        ),
    )


def run_strong_complaint(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    seed_decision(sandbox.decisions_db, ts_ms=BASE_MS, session_id="stress-fix-strong")
    report = run_planted(sandbox, fix_strong())
    events, candidates = fix_events(sandbox), fix_candidates(sandbox)
    return ScenarioResult(
        (
            expect("strong complaint inserted", report.inserted, 1),
            check("one hook_complaint row", [row["source_kind"] for row in events] == ["hook_complaint"], events),
            check(
                "primitive fire remapped to the user hook target",
                candidates == [STATUS_NUDGE_CANDIDATE],
                candidates,
            ),
        )
    )


def run_hedged_complaint(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    seed_decision(sandbox.decisions_db, ts_ms=BASE_MS, session_id="stress-fix-hedged")
    report = run_planted(sandbox, fix_hedged())
    events, candidates = fix_events(sandbox), fix_candidates(sandbox)
    confidences = [json.loads(str(row["payload_json"]))["signal"]["confidence"] for row in events]
    return ScenarioResult(
        (
            expect("hedged complaint inserted", report.inserted, 1),
            check("hedged confidence 0.75 clears the 0.5 fix floor", confidences == [0.75], events),
            check("fix candidate exists", [row["candidate_kind"] for row in candidates] == ["fix"], candidates),
        )
    )


def run_compliance_suppressed(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    seed_decision(sandbox.decisions_db, ts_ms=BASE_MS, session_id="stress-fix-compliance")
    report = run_planted(sandbox, fix_compliance())
    return ScenarioResult(
        (
            expect("compliance remark inserted nothing", report.inserted, 0),
            expect(
                "hook_complaint rows",
                count(sandbox.review_db, "SELECT COUNT(*) FROM feedback_events WHERE source_kind = 'hook_complaint'"),
                0,
            ),
            expect("candidate rows", count(sandbox.review_db, "SELECT COUNT(*) FROM candidates"), 0),
        )
    )


def run_attribution_miss(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    report = run_planted(sandbox, fix_strong(session="stress-fix-unseeded"))
    decisions = query(sandbox.decisions_db, "SELECT * FROM decisions_v1")
    return ScenarioResult(
        (
            check("decision ledger empty", decisions == [], decisions),
            expect("unattributable complaint inserted nothing", report.inserted, 0),
            expect("feedback rows", count(sandbox.review_db, "SELECT COUNT(*) FROM feedback_events"), 0),
        )
    )


def run_ambiguity_drop(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    seed_decision(
        sandbox.decisions_db,
        ts_ms=BASE_MS,
        session_id=AMBIGUOUS_SESSION,
        kind="guard_a:nudge_aaaa0001",
        source_file="/repo/.claude/hooks/a.py",
    )
    seed_decision(
        sandbox.decisions_db,
        ts_ms=BASE_MS,
        session_id=AMBIGUOUS_SESSION,
        kind="guard_b:gate_bbbb0002",
        source_file="/repo/.claude/hooks/b.py",
        event="Stop",
        action="block",
        message=STOP_MESSAGE,
        digest=None,
    )
    report = run_planted(sandbox, ambiguous_planted())
    decisions = query(sandbox.decisions_db, "SELECT kind, source_file, event, message FROM decisions_v1")
    return ScenarioResult(
        (
            check(
                "two competing decisions seeded",
                {row["source_file"] for row in decisions} == {"/repo/.claude/hooks/a.py", "/repo/.claude/hooks/b.py"},
                decisions,
            ),
            expect("two-target ambiguity inserted nothing", report.inserted, 0),
            expect(
                "hook_complaint rows",
                count(sandbox.review_db, "SELECT COUNT(*) FROM feedback_events WHERE source_kind = 'hook_complaint'"),
                0,
            ),
        )
    )


def run_non_primitive_target(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    seed_decision(
        sandbox.decisions_db,
        ts_ms=BASE_MS,
        session_id="stress-fix-custom",
        kind="custom_guard:hook_1",
        source_file=".claude/hooks/custom_guard.py",
    )
    report = run_planted(sandbox, replace(fix_strong(session="stress-fix-custom"), name="fix-custom-guard"))
    candidates = fix_candidates(sandbox)
    return ScenarioResult(
        (
            expect("complaint inserted", report.inserted, 1),
            check(
                "non-primitive source_file passes through unmapped",
                candidates
                == [
                    STATUS_NUDGE_CANDIDATE
                    | {"target_source_file": ".claude/hooks/custom_guard.py", "target_hook_name": "custom_guard:hook_1"}
                ],
                candidates,
            ),
        )
    )


def run_fix_grouping(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    reports: list[SpawnReportLine] = []
    for nth, session in enumerate(GROUP_SESSIONS, 1):
        seed_decision(sandbox.decisions_db, ts_ms=BASE_MS, session_id=session)
        reports.append(run_planted(sandbox, replace(fix_strong(session=session), name=f"fix-group-{nth}"), nth=nth))
    observations = query(sandbox.review_db, "SELECT candidate_id, session_id FROM candidate_observations")
    return ScenarioResult(
        (
            check("each session inserted one complaint", [report.inserted for report in reports] == [1, 1], reports),
            expect("one grouped fix candidate", count(sandbox.review_db, "SELECT COUNT(*) FROM candidates"), 1),
            check(
                "two observations across two sessions on one candidate",
                len(observations) == 2
                and {row["session_id"] for row in observations} == set(GROUP_SESSIONS)
                and len({row["candidate_id"] for row in observations}) == 1,
                observations,
            ),
        )
    )


def run_stop_feedback_fingerprint(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    seed_decision(
        sandbox.decisions_db,
        ts_ms=BASE_MS,
        session_id=STOP_SESSION,
        kind="stop_reminder:gate_e76ccd07",
        event="Stop",
        action="block",
        message=STOP_MESSAGE,
        digest=None,
    )
    report = run_planted(sandbox, stop_planted())
    events, candidates = fix_events(sandbox), fix_candidates(sandbox)
    return ScenarioResult(
        (
            expect("stop complaint inserted", report.inserted, 1),
            check(
                "hook_complaint row attributed via attribute_nearest",
                [row["source_kind"] for row in events] == ["hook_complaint"],
                events,
            ),
            check(
                "stop gate resolves to .claude/hooks/stop_reminder.py",
                candidates
                == [
                    STATUS_NUDGE_CANDIDATE
                    | {
                        "target_source_file": ".claude/hooks/stop_reminder.py",
                        "target_hook_name": "stop_reminder:gate_e76ccd07",
                        "misfire_class": "already_addressed",
                    }
                ],
                candidates,
            ),
        )
    )


def scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario("fix-strong-complaint", FAMILY, Tier.OFFLINE, run_strong_complaint),
        Scenario("fix-hedged-complaint", FAMILY, Tier.OFFLINE, run_hedged_complaint),
        Scenario("fix-compliance-suppressed", FAMILY, Tier.OFFLINE, run_compliance_suppressed),
        Scenario("fix-attribution-miss", FAMILY, Tier.OFFLINE, run_attribution_miss),
        Scenario("fix-ambiguity-drop", FAMILY, Tier.OFFLINE, run_ambiguity_drop),
        Scenario("fix-non-primitive-target", FAMILY, Tier.OFFLINE, run_non_primitive_target),
        Scenario("fix-grouping", FAMILY, Tier.OFFLINE, run_fix_grouping),
        Scenario("fix-stop-feedback-fingerprint", FAMILY, Tier.OFFLINE, run_stop_feedback_fingerprint),
    )
