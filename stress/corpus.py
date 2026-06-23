"""Labeled synthetic transcripts: every planted entry carries its expected outcome.

Builders are lifted from the unit suite (``tests/test_review_scan.py`` and
``tests/test_review_fix.py``) rather than re-implemented; this module adds the
labels (expected source kinds, insert counts, judge categories) and the
pathological generators. Judge-stub routing rides inside the planted text as a
``[[judge:<category>]]`` marker the offline ``claude`` shim echoes back; live
corpus variants drop the marker.

Every synthetic session id starts with ``stress-`` and every transcript lives
under the sandbox — the fingerprints the real-state leak guard queries for.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tests.test_review_fix import (
    HEDGED_COMPLAINT,
    NUDGE_MESSAGE,
    STRONG_COMPLAINT,
    nudge_attachment,
)
from tests.test_review_scan import (
    assistant_text,
    assistant_tool_use,
    tool_result,
    user_text,
    write_transcript,
)

if TYPE_CHECKING:
    from pathlib import Path

BASE_TS = "2026-06-01T12:00:00+00:00"
SESSION = "stress-sess-1"
DURABLE_CORRECTION = "never log with print in this repo, always use the loguru logger [[judge:tooling_rule]]"
DURABLE_CORRECTION_LIVE = "never log with print in this repo, always use the loguru logger"
ONE_OFF_CORRECTION = "no, rename just this helper to parse_events [[judge:one_off_correction]]"
HEDGED_CORRECTION = "maybe we should possibly use a frozen dataclass here? up to you [[judge:preference_unclear]]"
REVIEWER_MARKER_TEXT = "[capt-hook-session-reviewer] reviewing eligible candidates"
COMPLIANCE_REMARK = "Good catch - I'll follow the task tracker reminder before the next status check."


@dataclass(frozen=True, slots=True)
class Planted:
    """One synthetic transcript with its expected scan outcome.

    Attributes:
        name: Scenario-unique slug; becomes the transcript filename.
        entries: The raw JSONL entry dicts, in order.
        expected_kinds: The ``source_kind`` values that must appear in
            ``feedback_events`` after a scan — exactly these, no others.
        expected_inserted: The exact ``inserted`` count the scan must report.
    """

    name: str
    entries: tuple[dict[str, Any], ...]
    expected_kinds: frozenset[str] = frozenset()
    expected_inserted: int = 0


def turn_ts(minute: int, *, day: int = 1) -> str:
    return f"2026-06-{day:02d}T12:{minute:02d}:00+00:00"


def correction_turns(
    text: str, *, session: str = SESSION, day: int = 1, cwd: str | None = None
) -> list[dict[str, Any]]:
    extra = {"cwd": cwd} if cwd else {}
    return [
        assistant_text(
            "I'll add a print statement for debugging", sessionId=session, timestamp=turn_ts(0, day=day), **extra
        ),
        user_text(text, sessionId=session, timestamp=turn_ts(1, day=day), **extra),
    ]


def complaint_turns(complaint: str, *, session: str = SESSION, day: int = 1) -> list[dict[str, Any]]:
    return [
        user_text("run a status check, then commit", sessionId=session, timestamp=turn_ts(0, day=day)),
        assistant_tool_use("t1", "Bash", {"command": "git status"}, sessionId=session, timestamp=turn_ts(1, day=day)),
        nudge_attachment(NUDGE_MESSAGE, sessionId=session, timestamp=turn_ts(1, day=day)),
        tool_result("t1", "clean", sessionId=session, timestamp=turn_ts(1, day=day)),
        assistant_text(complaint, sessionId=session, timestamp=turn_ts(2, day=day)),
    ]


def durable_correction(name: str, *, session: str = SESSION, day: int = 1, text: str = DURABLE_CORRECTION) -> Planted:
    return Planted(
        name,
        tuple(correction_turns(text, session=session, day=day)),
        expected_kinds=frozenset({"transcript_message"}),
        expected_inserted=1,
    )


def noise_trap(name: str, entries: list[dict[str, Any]]) -> Planted:
    return Planted(name, tuple(entries))


def noise_traps() -> tuple[Planted, ...]:
    return (
        noise_trap("trivial-ack", correction_turns("ok")[:1] + [user_text("ok", sessionId=SESSION)]),
        noise_trap("short-thanks", correction_turns("thanks")[:1] + [user_text("thanks", sessionId=SESSION)]),
        noise_trap("sidechain", [entry | {"isSidechain": True} for entry in correction_turns(DURABLE_CORRECTION)]),
        noise_trap(
            "meta-flagged",
            correction_turns(DURABLE_CORRECTION)[:1]
            + [user_text(DURABLE_CORRECTION, sessionId=SESSION) | {"isMeta": True}],
        ),
        noise_trap("no-assistant-trigger", [user_text(DURABLE_CORRECTION, sessionId=SESSION)]),
        noise_trap(
            "reviewer-marker",
            [user_text(REVIEWER_MARKER_TEXT, sessionId=SESSION), *correction_turns(DURABLE_CORRECTION)],
        ),
    )


def fix_strong(*, session: str = "stress-fix-strong") -> Planted:
    return Planted(
        "fix-strong-complaint",
        tuple(complaint_turns(STRONG_COMPLAINT, session=session)),
        expected_kinds=frozenset({"hook_complaint"}),
        expected_inserted=1,
    )


def fix_hedged(*, session: str = "stress-fix-hedged") -> Planted:
    return Planted(
        "fix-hedged-complaint",
        tuple(complaint_turns(HEDGED_COMPLAINT, session=session)),
        expected_kinds=frozenset({"hook_complaint"}),
        expected_inserted=1,
    )


def fix_compliance(*, session: str = "stress-fix-compliance") -> Planted:
    return Planted("fix-compliance-only", tuple(complaint_turns(COMPLIANCE_REMARK, session=session)))


def write(planted: Planted, directory: Path, *, cwd: Path | None = None) -> Path:
    entries = [entry | {"cwd": str(cwd)} if cwd else entry for entry in planted.entries]
    return write_transcript(directory / f"{planted.name}.jsonl", entries)


def write_truncated(planted: Planted, directory: Path) -> Path:
    path = write(planted, directory)
    raw = path.read_bytes()
    path.write_bytes(raw[: int(len(raw) * 0.6)])
    return path


def write_binary_garbage(directory: Path, name: str = "binary-garbage") -> Path:
    path = directory / f"{name}.jsonl"
    path.write_bytes(os.urandom(4096))
    return path


def write_undecodable_splice(planted: Planted, directory: Path) -> Path:
    path = write(planted, directory)
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join([*lines[:1], b'\xff\xfe{"broken": tru\n', *lines[1:]]))
    return path


def write_huge_line(directory: Path, *, megabytes: int = 10) -> Path:
    blob = "x" * (megabytes * 1024 * 1024)
    return write_transcript(
        directory / "huge-line.jsonl",
        [assistant_text("working", sessionId=SESSION), user_text(blob, sessionId=SESSION)],
    )


def write_perf_transcript(directory: Path, *, events: int = 100_000, corrections: int = 20) -> Path:
    stride = events // corrections
    path = directory / "perf-100k.jsonl"
    with path.open("w") as fh:
        for index in range(events):
            entry = (
                user_text(f"{DURABLE_CORRECTION_LIVE} (case {index // stride})", sessionId=SESSION)
                if index % stride == stride - 1
                else assistant_text(f"step {index}", sessionId=SESSION)
            )
            fh.write(json.dumps(entry) + "\n")
    return path


def denial_content(said: str) -> str:
    return (
        "The user doesn't want to proceed with this tool use. The tool use was rejected.\n"
        f"To tell you how to proceed, the user said:\n{said}\nNote: The user's next message will follow."
    )


def plan_rejection(
    name: str, *, session: str = SESSION, said: str = "the plan skips the data migration step"
) -> Planted:
    return Planted(
        name,
        (
            assistant_tool_use("t1", "ExitPlanMode", {"plan": "## Plan\n1. rewrite the parser"}, sessionId=session),
            tool_result("t1", denial_content(said), is_error=True, sessionId=session),
        ),
        expected_kinds=frozenset({"plan_review"}),
        expected_inserted=1,
    )


def plan_reentry(
    name: str, *, session: str = SESSION, text: str = "reconsider the plan, the parser rewrite is wrong"
) -> Planted:
    return Planted(
        name,
        (
            assistant_tool_use(
                "e1", "Edit", {"file_path": "/repo/x.py", "old_string": "a", "new_string": "b"}, sessionId=session
            ),
            {"type": "mode", "sessionId": session, "mode": "plan"},
            user_text(text, sessionId=session),
        ),
        expected_kinds=frozenset({"plan_review", "transcript_message"}),
        expected_inserted=2,
    )


def tool_denial(
    name: str, *, session: str = SESSION, said: str = "use the storage adapter instead of raw sql"
) -> Planted:
    return Planted(
        name,
        (
            assistant_tool_use("t1", "Bash", {"command": "rm -rf build"}, sessionId=session),
            tool_result("t1", denial_content(said), is_error=True, sessionId=session),
        ),
        expected_kinds=frozenset({"interrupt_rejection"}),
        expected_inserted=1,
    )


def interrupt_followup(
    name: str, *, session: str = SESSION, text: str = "actually gate the delete behind the dry-run flag"
) -> Planted:
    return Planted(
        name,
        (
            assistant_tool_use("t1", "Bash", {"command": "rm -rf build"}, sessionId=session),
            tool_result("t1", "[Request interrupted by user]", is_error=True, sessionId=session),
            user_text(text, sessionId=session),
        ),
        expected_kinds=frozenset({"transcript_message"}),
        expected_inserted=1,
    )


def review_comment_inline(name: str, *, session: str = SESSION) -> Planted:
    return Planted(
        name,
        (
            assistant_text("applied the guard change", sessionId=session),
            user_text(
                "In captain_hook/review/scan.py:L127-129: this guard also needs to drop sidechain turns first.",
                sessionId=session,
            ),
        ),
        expected_kinds=frozenset({"review_comment", "transcript_message"}),
        expected_inserted=2,
    )


def review_comment_finding(name: str, *, session: str = SESSION) -> Planted:
    return Planted(
        name,
        (
            assistant_text("applied the formats change", sessionId=session),
            user_text(
                "- file: captain_hook/review/formats.py:53\n- theme: comment assembly\n"
                "- claim: joining claim and suggestion with a space loses the boundary\n"
                "- suggestion: join with '; ' so the clauses stay readable",
                sessionId=session,
            ),
        ),
        expected_kinds=frozenset({"review_comment", "transcript_message"}),
        expected_inserted=2,
    )


def review_comment_workstream(name: str, *, session: str = SESSION) -> Planted:
    return Planted(
        name,
        (
            assistant_text("split the work into streams", sessionId=session),
            user_text(
                "### WS-1 [FIX] — Tighten the STRICT_USER prefilter\n"
                "FIX: gate review comments on drop_sidechain before the keep call.\n"
                "Tests: add a sidechain user entry fixture and assert it is dropped.",
                sessionId=session,
            ),
        ),
        expected_kinds=frozenset({"review_comment", "transcript_message"}),
        expected_inserted=2,
    )


def multi_session(name: str, *, sessions: int = 2) -> Planted:
    entries = [
        entry
        for index in range(sessions)
        for entry in correction_turns(DURABLE_CORRECTION, session=f"stress-multi-{index}")
    ]
    return Planted(
        name,
        tuple(entries),
        expected_kinds=frozenset({"transcript_message"}),
        expected_inserted=sessions,
    )
