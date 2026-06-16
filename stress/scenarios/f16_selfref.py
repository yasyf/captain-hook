"""F16 — self-reference: the scope of the reviewer marker's self-skip."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.test_review_scan import assistant_text, user_text

from stress.corpus import DURABLE_CORRECTION, REVIEWER_MARKER_TEXT, Planted, correction_turns, turn_ts, write
from stress.db import count, query
from stress.drivers.proc import capt_hook, review_run, wait_for_report
from stress.scenarios.base import Scenario, ScenarioResult, Tier, check, expect

if TYPE_CHECKING:
    from stress.sandbox import Sandbox

FAMILY = "selfref"
FIRST_SESSION = "stress-selfref-first"
MID_SESSION = "stress-selfref-mid"
ASSISTANT_SESSION = "stress-selfref-assistant"


def enable(sandbox: Sandbox) -> None:
    capt_hook(
        "review",
        "enable",
        sandbox=sandbox,
        cwd=sandbox.repo,
        env=sandbox.env(CLAUDE_PROJECT_DIR=str(sandbox.repo)),
    )


def first_marker_planted() -> Planted:
    return Planted(
        "selfref-first",
        (
            user_text(REVIEWER_MARKER_TEXT, sessionId=FIRST_SESSION, timestamp=turn_ts(0)),
            *correction_turns(DURABLE_CORRECTION, session=FIRST_SESSION),
        ),
    )


def mid_marker_planted() -> Planted:
    return Planted(
        "selfref-mid",
        (
            *correction_turns(DURABLE_CORRECTION, session=MID_SESSION),
            assistant_text("posting the reviewer summary now", sessionId=MID_SESSION, timestamp=turn_ts(2)),
            user_text(
                f"the reviewer session printed: {REVIEWER_MARKER_TEXT}",
                sessionId=MID_SESSION,
                timestamp=turn_ts(3),
            ),
        ),
    )


def assistant_marker_planted() -> Planted:
    return Planted(
        "selfref-assistant",
        (
            assistant_text(f"working as {REVIEWER_MARKER_TEXT}", sessionId=ASSISTANT_SESSION, timestamp=turn_ts(0)),
            user_text(DURABLE_CORRECTION, sessionId=ASSISTANT_SESSION, timestamp=turn_ts(1)),
        ),
    )


def run_marker_first_message(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    path = write(first_marker_planted(), sandbox.transcripts)
    review_run(sandbox, path)
    first = wait_for_report(sandbox)[0]
    files = query(sandbox.review_db, "SELECT path, mtime FROM files")
    review_run(sandbox, path)
    second = wait_for_report(sandbox, count=2)[1]
    return ScenarioResult(
        (
            check("marker session scanned, nothing inserted", first.scanned == 1 and first.inserted == 0, first),
            check("files row recorded for the marker transcript", [row["path"] for row in files] == [str(path)], files),
            expect("feedback rows", count(sandbox.review_db, "SELECT COUNT(*) FROM feedback_events"), 0),
            check("second pass skips by mtime", second.scanned == 0 and second.inserted == 0, second),
        )
    )


def run_marker_mid_conversation(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    review_run(sandbox, write(mid_marker_planted(), sandbox.transcripts))
    report = wait_for_report(sandbox)[0]
    events = query(sandbox.review_db, "SELECT source_kind, session_id, text FROM feedback_events")
    return ScenarioResult(
        (
            check("mid-conversation marker does not self-skip", report.scanned == 1 and report.inserted == 2, report),
            check(
                "correction AND the marker message both ingested",
                sorted(row["source_kind"] for row in events) == ["transcript_message", "transcript_message"]
                and any(REVIEWER_MARKER_TEXT in str(row["text"]) for row in events),
                events,
            ),
        ),
        finding=(
            "is_reviewer_session tests only the FIRST user message (scan.py:229-230 — next() over user "
            "events returns the first event's containment check, not any()), so a reviewer marker arriving "
            "mid-conversation does not self-skip: the session ingests normally and the marker-bearing user "
            "message itself becomes a transcript_message feedback event"
        ),
    )


def run_marker_in_assistant_text(sandbox: Sandbox) -> ScenarioResult:
    enable(sandbox)
    review_run(sandbox, write(assistant_marker_planted(), sandbox.transcripts))
    report = wait_for_report(sandbox)[0]
    events = query(sandbox.review_db, "SELECT source_kind, session_id, text FROM feedback_events")
    return ScenarioResult(
        (
            check("assistant-only marker does not self-skip", report.scanned == 1 and report.inserted == 1, report),
            check(
                "durable correction still ingested",
                [(row["source_kind"], row["session_id"]) for row in events]
                == [("transcript_message", ASSISTANT_SESSION)],
                events,
            ),
        )
    )


def scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario("selfref-marker-first-message", FAMILY, Tier.OFFLINE, run_marker_first_message),
        Scenario("selfref-marker-mid-conversation", FAMILY, Tier.OFFLINE, run_marker_mid_conversation),
        Scenario("selfref-marker-in-assistant-text", FAMILY, Tier.OFFLINE, run_marker_in_assistant_text),
    )
