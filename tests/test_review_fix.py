from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from cc_transcript import parse_events_from_bytes
from cc_transcript.domains.mining.candidates import DedupKey, dedup_key
from cc_transcript.domains.mining.confidence import HIGH, MEDIUM, VERY_HIGH
from cc_transcript.domains.mining.context import ContextSnapshot, ContextTurn
from cc_transcript.domains.mining.verdicts import GoldenRow, golden_result

from captain_hook.fire_log import FireLog, FireRow
from captain_hook.review.fix import (
    COMPLIANCE_RE,
    HOOK_COMPLAINT,
    classify_marker,
    fire_message,
    iter_hook_complaint_signals,
    resolve_target,
)
from captain_hook.review.judge import REVIEW_PROMPT_VERSION, ReviewVerdict, build_prompt
from captain_hook.review.repo import RepoKey
from captain_hook.review.scan import ScanReport, scan_transcript
from captain_hook.review.settings import ReviewSettings
from captain_hook.review.store import ReviewStore
from captain_hook.session import session_hash
from tests.test_review_scan import (
    assistant_text,
    assistant_tool_use,
    envelope,
    tool_result,
    user_text,
    write_transcript,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from cc_transcript.models import TranscriptEvent

FIXTURES = Path(__file__).parent / "fixtures" / "hook_fires"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text())
MISFIRE_FIXTURE = "fire-misfire-complaint.jsonl"
REPO = RepoKey("github.com/yasyf/scratch")
BASE_TS = "2026-06-01T12:00:00+00:00"
BASE_EPOCH = datetime.fromisoformat(BASE_TS).timestamp()
PRIMITIVE_NUDGE = "/x/site-packages/captain_hook/primitives/nudge.py"
NUDGE_MESSAGE = "Remember to use the project's task tracker before running status checks."
STRONG_COMPLAINT = "**Note**: The task tracker reminder re-fired on a sequence I already completed - ignoring it."
HEDGED_COMPLAINT = "The lint reminder seems to have misfired here - the file is generated output, not source."


@dataclass(frozen=True, slots=True)
class Verdict:
    accepted: bool = True
    confidence: float = 0.9
    category: str = "misfire_confirmed"
    summary: str = "claude dismissed the fire"
    rationale: str = "explicit dismissal"


def fixture_events(name: str) -> list[TranscriptEvent]:
    return parse_events_from_bytes((FIXTURES / name).read_bytes())


def seed_fixture_fires(firelog: FireLog, name: str, path: Path) -> None:
    for row in MANIFEST["files"][name]["fire_log_rows"]:
        firelog.append(
            ts=row["ts"],
            session_id=session_hash(path),
            claude_session_id=row["claude_session_id"],
            repo_key=row["repo_key"],
            hook_name=row["hook_name"],
            source_file=row["source_file"],
            event=row["event"],
            action=row["action"],
            message=row["message"],
        )


def nudge_attachment(content: str, **overrides: Any) -> dict[str, Any]:
    return envelope(
        "attachment",
        attachment={
            "type": "hook_additional_context",
            "content": [content],
            "hookName": "PreToolUse:Bash",
            "toolUseID": "t1",
            "hookEvent": "PreToolUse",
        },
        **overrides,
    )


def complaint_entries(complaint: str, *, session: str = "sess-1") -> list[dict[str, Any]]:
    return [
        user_text("run a status check, then commit", sessionId=session),
        assistant_tool_use("t1", "Bash", {"command": "git status"}, sessionId=session),
        nudge_attachment(NUDGE_MESSAGE, sessionId=session),
        tool_result("t1", "clean", sessionId=session),
        assistant_text(complaint, sessionId=session),
    ]


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[ReviewStore]:
    async with await ReviewStore.open(tmp_path / "review.db") as opened:
        yield opened


@pytest.fixture
def settings(tmp_path: Path) -> ReviewSettings:
    return ReviewSettings(db_path=tmp_path / "review.db", fire_log_path=tmp_path / "fires.db")


@pytest.fixture
def firelog(settings: ReviewSettings) -> FireLog:
    return FireLog.open(settings.fire_log_path)


def seed_nudge_fire(firelog: FireLog, path: Path, *, source_file: str = PRIMITIVE_NUDGE) -> None:
    firelog.append(
        ts=BASE_EPOCH - 10,
        session_id=session_hash(path),
        claude_session_id="sess-1",
        repo_key=str(REPO),
        hook_name="status_nudge:nudge_c424798f",
        source_file=source_file,
        event="PreToolUse",
        action="warn",
        message=NUDGE_MESSAGE,
    )


async def rows(store: ReviewStore, query: str) -> list[dict[str, Any]]:
    cur = await store.store.conn.execute(query)
    return [dict(row) async for row in cur]


class TestMarkers:
    @pytest.mark.parametrize(
        ("text", "strength", "misfire_class"),
        [
            pytest.param("the hook re-fired on my own earlier text - ignoring it", "strong", "refire", id="refire"),
            pytest.param("this nudge is a false positive", "strong", "false_positive", id="false-positive"),
            pytest.param(
                "that stop gate shouldn't have fired - I had already addressed every open task",
                "strong",
                "already_addressed",
                id="already-addressed",
            ),
            pytest.param(
                "that stop gate shouldn't have fired at all here",
                "strong",
                "should_not_have_fired",
                id="should-not-have-fired",
            ),
            pytest.param("this reminder is spurious; the tests already pass", "strong", "spurious", id="spurious"),
            pytest.param(
                "noted, but this reminder re-fired on text I already fixed - ignoring it",
                "strong",
                "refire",
                id="strong-dismissal-overrides-compliance",
            ),
            pytest.param(
                "I think the hook may be a false positive here", "hedged", "false_positive", id="hedged-known-class"
            ),
            pytest.param("the gate seems to have misfired", "hedged", "misfire", id="hedged-misfire"),
        ],
    )
    def test_marker_classification(self, text: str, strength: str, misfire_class: str) -> None:
        marker = classify_marker(text)
        assert marker is not None
        assert (marker.strength, marker.misfire_class) == (strength, misfire_class)

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("I'll fix the hook config now", id="compliance-ill"),
            pytest.param("good catch - the reminder is right, running the tests now", id="compliance-good-catch"),
            pytest.param("let me re-run the linter before the hook complains again", id="compliance-let-me"),
            pytest.param("noted - the gate wants a summary line", id="compliance-noted"),
            pytest.param("the user pointed out a false positive in my analysis", id="no-hook-vocabulary"),
            pytest.param("the policy hook blocked the force-delete, so I removed files individually", id="ambient"),
            pytest.param("the hook reminder about the task tracker has fired again", id="fired-again-not-refired"),
        ],
    )
    def test_non_complaints_yield_no_marker(self, text: str) -> None:
        assert classify_marker(text) is None


class TestFingerprints:
    @pytest.mark.parametrize("name", sorted(MANIFEST["files"]))
    def test_manifest_fingerprints_match_parser_view(self, name: str) -> None:
        events = fixture_events(name)
        for fp in MANIFEST["files"][name]["fingerprints"]:
            extracted = fire_message(events[fp["line"] - 1])
            match fp["kind"]:
                case "hook_additional_context":
                    assert extracted == "\n".join(fp["content"])
                case "hook_blocking_error":
                    assert extracted == fp["blockingError"]["blockingError"]
                case "stop_hook_feedback_meta_turn":
                    assert extracted == fp["content"].removeprefix("Stop hook feedback:\n")
                case "is_error_tool_result":
                    assert extracted == fp["content"]
                case "hook_success":
                    assert extracted is None
                case unknown:
                    raise AssertionError(unknown)

    def test_ordinary_events_carry_no_fingerprint(self) -> None:
        events = parse_events_from_bytes(
            (
                json.dumps(user_text("hello"))
                + "\n"
                + json.dumps(assistant_text("hi"))
                + "\n"
                + json.dumps(tool_result("t1", "ok"))
            ).encode()
        )
        assert [fire_message(event) for event in events] == [None, None, None]


class TestResolveTarget:
    @pytest.mark.parametrize(
        ("source_file", "hook_name", "expected"),
        [
            pytest.param(
                PRIMITIVE_NUDGE,
                "status_nudge:nudge_c424798f",
                (".claude/hooks/status_nudge.py", "status_nudge:nudge_c424798f"),
                id="primitive-resolves-from-module-stem",
            ),
            pytest.param(
                PRIMITIVE_NUDGE,
                "hooks.tasks:nudge_9cf8ea99",
                (".claude/hooks/tasks.py", "hooks.tasks:nudge_9cf8ea99"),
                id="primitive-strips-package-prefix",
            ),
            pytest.param(PRIMITIVE_NUDGE, "declarative_1", None, id="primitive-anonymous-unresolvable"),
            pytest.param(
                "/repo/.claude/hooks/guard.py",
                "guard:warn_deadbeef",
                ("/repo/.claude/hooks/guard.py", "guard:warn_deadbeef"),
                id="user-file-passes-through",
            ),
        ],
    )
    def test_resolve_target(self, source_file: str, hook_name: str, expected: tuple[str, str] | None) -> None:
        row = FireRow(
            id=1,
            ts=BASE_EPOCH,
            session_id="s",
            claude_session_id=None,
            repo_key=None,
            hook_name=hook_name,
            source_file=source_file,
            event="PreToolUse",
            action="warn",
            message="m",
        )
        assert resolve_target(row) == expected


class TestDetector:
    def test_real_misfire_complaint_attributes_to_user_hook_not_primitive(self, firelog: FireLog) -> None:
        path = FIXTURES / MISFIRE_FIXTURE
        seed_fixture_fires(firelog, MISFIRE_FIXTURE, path)
        [sig] = iter_hook_complaint_signals(
            fixture_events(MISFIRE_FIXTURE), firelog=firelog, session_key=session_hash(path)
        )
        assert sig.kind == HOOK_COMPLAINT
        assert "re-fired unnecessarily and I am ignoring the repeats" in sig.text
        assert sig.evidence["target_source_file"] == ".claude/hooks/status_nudge.py"
        assert sig.evidence["target_hook_name"] == "status_nudge:nudge_c424798f"
        assert str(sig.evidence["source_file"]).endswith("captain_hook/primitives/nudge.py")
        assert (sig.evidence["event"], sig.evidence["action"]) == ("PreToolUse", "warn")
        assert sig.evidence["fire_message"] == NUDGE_MESSAGE
        assert sig.evidence["fire_ts"] == 1781224517.348702
        assert (sig.evidence["marker"], sig.evidence["misfire_class"]) == ("re-fired", "refire")
        assert sig.signal is not None
        assert sig.signal.confidence == VERY_HIGH
        assert sig.signal.reasons == ("strong_marker", "refire")

    @pytest.mark.parametrize("name", ["fire-compliance.jsonl", "fire-block.jsonl", "fire-stop.jsonl"])
    def test_compliance_and_working_fire_fixtures_yield_nothing(self, firelog: FireLog, name: str) -> None:
        path = FIXTURES / name
        seed_fixture_fires(firelog, name, path)
        events = fixture_events(name)
        assert list(iter_hook_complaint_signals(events, firelog=firelog, session_key=session_hash(path))) == []

    def test_complaint_with_no_preceding_fingerprint_yields_nothing(self, firelog: FireLog, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        entries = [
            user_text("run a status check"),
            assistant_text("checking now"),
            assistant_text(STRONG_COMPLAINT),
        ]
        write_transcript(path, entries)
        seed_nudge_fire(firelog, path)
        events = parse_events_from_bytes(path.read_bytes())
        assert list(iter_hook_complaint_signals(events, firelog=firelog, session_key=session_hash(path))) == []

    def test_complaint_with_no_fire_log_row_yields_nothing(self, firelog: FireLog) -> None:
        path = FIXTURES / MISFIRE_FIXTURE
        events = fixture_events(MISFIRE_FIXTURE)
        assert list(iter_hook_complaint_signals(events, firelog=firelog, session_key=session_hash(path))) == []

    def test_ambiguous_attribution_across_source_files_drops(self, firelog: FireLog) -> None:
        path = FIXTURES / MISFIRE_FIXTURE
        seed_fixture_fires(firelog, MISFIRE_FIXTURE, path)
        seed_nudge_fire(firelog, path, source_file="/repo/.claude/hooks/other.py")
        events = fixture_events(MISFIRE_FIXTURE)
        assert list(iter_hook_complaint_signals(events, firelog=firelog, session_key=session_hash(path))) == []

    def test_anonymous_declarative_fire_drops(self, firelog: FireLog, tmp_path: Path) -> None:
        deny = "BLOCKED: recursive force-delete (rm -rf) is forbidden in this repo."
        path = tmp_path / "s.jsonl"
        entries = [
            assistant_tool_use("t1", "Bash", {"command": "rm -rf scratch"}),
            tool_result("t1", deny, is_error=True),
            assistant_text(f"That rm guard misfired - the path is a scratch dir. {deny}"),
        ]
        write_transcript(path, entries)
        firelog.append(
            ts=BASE_EPOCH - 10,
            session_id=session_hash(path),
            claude_session_id="sess-1",
            repo_key=str(REPO),
            hook_name="declarative_1",
            source_file="",
            event="PreToolUse",
            action="block",
            message=deny,
        )
        events = parse_events_from_bytes(path.read_bytes())
        assert list(iter_hook_complaint_signals(events, firelog=firelog, session_key=session_hash(path))) == []

    def test_tight_proximity_bumps_hedged_to_high(self, firelog: FireLog, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        entries = [
            user_text("run a status check"),
            assistant_tool_use("t1", "Bash", {"command": "git status"}),
            nudge_attachment(NUDGE_MESSAGE),
            assistant_text(HEDGED_COMPLAINT),
        ]
        write_transcript(path, entries)
        seed_nudge_fire(firelog, path)
        events = parse_events_from_bytes(path.read_bytes())
        [sig] = iter_hook_complaint_signals(events, firelog=firelog, session_key=session_hash(path))
        assert sig.signal is not None
        assert sig.signal.confidence == MEDIUM + 0.25
        assert sig.signal.reasons == ("hedged_marker", "misfire", "tight_proximity")


class TestStrictFixPartition:
    def hedged_entries(self) -> list[dict[str, Any]]:
        return [
            user_text("run a status check"),
            nudge_attachment(NUDGE_MESSAGE),
            assistant_tool_use("t1", "Bash", {"command": "git status"}),
            tool_result("t1", "clean"),
            assistant_text(HEDGED_COMPLAINT),
        ]

    async def test_hedged_complaint_passes_the_default_fix_floor(
        self, store: ReviewStore, settings: ReviewSettings, firelog: FireLog, tmp_path: Path
    ) -> None:
        path = write_transcript(tmp_path / "s.jsonl", self.hedged_entries())
        seed_nudge_fire(firelog, path)
        assert await scan_transcript(store, path, settings=settings, repo_key=REPO) == ScanReport(scanned=1, inserted=1)
        [event] = await rows(store, "SELECT * FROM feedback_events")
        assert event["source_kind"] == HOOK_COMPLAINT
        assert json.loads(str(event["payload_json"]))["signal"]["confidence"] == MEDIUM

    async def test_hedged_complaint_drops_under_a_raised_fix_floor(
        self, store: ReviewStore, settings: ReviewSettings, firelog: FireLog, tmp_path: Path
    ) -> None:
        raised = ReviewSettings(db_path=settings.db_path, fire_log_path=settings.fire_log_path, min_confidence_fix=HIGH)
        path = write_transcript(tmp_path / "s.jsonl", self.hedged_entries())
        seed_nudge_fire(firelog, path)
        assert await scan_transcript(store, path, settings=raised, repo_key=REPO) == ScanReport(scanned=1, inserted=0)
        assert await rows(store, "SELECT * FROM feedback_events") == []
        assert await rows(store, "SELECT * FROM candidates") == []


class TestFixGroupingAndStore:
    async def test_end_to_end_fixture_scan_creates_fix_candidate_with_target(
        self, store: ReviewStore, settings: ReviewSettings, firelog: FireLog
    ) -> None:
        path = FIXTURES / MISFIRE_FIXTURE
        seed_fixture_fires(firelog, MISFIRE_FIXTURE, path)
        assert await scan_transcript(store, path, settings=settings, repo_key=REPO) == ScanReport(scanned=1, inserted=1)

        [event] = await rows(store, "SELECT * FROM feedback_events")
        assert event["source_kind"] == HOOK_COMPLAINT
        payload = json.loads(str(event["payload_json"]))
        assert payload["target_source_file"] == ".claude/hooks/status_nudge.py"
        assert payload["signal"]["confidence"] == VERY_HIGH

        [candidate] = await rows(store, "SELECT * FROM candidates")
        assert (candidate["candidate_kind"], candidate["status"]) == ("fix", "watching")
        assert candidate["target_source_file"] == ".claude/hooks/status_nudge.py"
        assert candidate["target_hook_name"] == "status_nudge:nudge_c424798f"
        assert candidate["misfire_class"] == "refire"
        assert candidate["rule"] == dedup_key(
            "hook_complaint", "status_nudge:nudge_c424798f", ".claude/hooks/status_nudge.py"
        )

        await store.enable(REPO)
        [observation] = await rows(store, "SELECT * FROM candidate_observations")
        await store.record_verdict(
            DedupKey(str(observation["dedup_key"])),
            Verdict(),
            role="judge",
            prompt_version=REVIEW_PROMPT_VERSION,
            model="m1",
        )
        status = await store.threshold_status(
            int(candidate["id"]), settings=settings, prompt_version=REVIEW_PROMPT_VERSION
        )
        assert (status.sessions, status.single_observation) == (1, True)
        assert await store.eligible(int(candidate["id"]), settings=settings, prompt_version=REVIEW_PROMPT_VERSION)

    async def test_two_sessions_complaints_about_one_hook_group_under_one_candidate(
        self, store: ReviewStore, settings: ReviewSettings, firelog: FireLog, tmp_path: Path
    ) -> None:
        for session in ("s1", "s2"):
            path = write_transcript(tmp_path / f"{session}.jsonl", complaint_entries(STRONG_COMPLAINT, session=session))
            seed_nudge_fire(firelog, path)
            assert await scan_transcript(store, path, settings=settings, repo_key=REPO) == ScanReport(
                scanned=1, inserted=1
            )

        [candidate] = await rows(store, "SELECT * FROM candidates")
        assert candidate["candidate_kind"] == "fix"
        observations = await rows(store, "SELECT * FROM candidate_observations")
        assert {row["candidate_id"] for row in observations} == {candidate["id"]}
        assert {row["session_id"] for row in observations} == {"s1", "s2"}

        await store.enable(REPO)
        for observation in observations:
            await store.record_verdict(
                DedupKey(str(observation["dedup_key"])),
                Verdict(),
                role="judge",
                prompt_version=REVIEW_PROMPT_VERSION,
                model="m1",
            )
        status = await store.threshold_status(
            int(candidate["id"]), settings=settings, prompt_version=REVIEW_PROMPT_VERSION
        )
        assert status.sessions == 2
        assert await store.eligible(int(candidate["id"]), settings=settings, prompt_version=REVIEW_PROMPT_VERSION)


class TestFixJudge:
    @pytest.mark.parametrize(
        ("category", "accepted"),
        [
            pytest.param("misfire_confirmed", True, id="misfire-accepted"),
            pytest.param("compliance", False, id="compliance-rejected"),
            pytest.param("ambient_mention", False, id="ambient-rejected"),
        ],
    )
    def test_fix_categories_drive_acceptance(self, category: Any, accepted: bool) -> None:
        verdict = ReviewVerdict(category=category, summary="s", confidence=0.5, rationale="r")
        assert verdict.accepted is accepted

    def test_hook_complaint_rows_get_the_fix_prompt(self) -> None:
        snapshot = ContextSnapshot(
            before=(ContextTurn(role="assistant", text="running git status"),),
            trigger=None,
            after=(),
        )
        row = {
            "source_kind": "hook_complaint",
            "context_json": snapshot.to_json(),
            "text": STRONG_COMPLAINT,
            "payload_json": json.dumps(
                {
                    "target_hook_name": "status_nudge:nudge_c424798f",
                    "event": "PreToolUse",
                    "action": "warn",
                    "fire_message": NUDGE_MESSAGE,
                }
            ),
        }
        prompt = build_prompt(row)
        assert "misfire_confirmed" in prompt
        assert "[hook: status_nudge:nudge_c424798f (PreToolUse/warn)]" in prompt
        assert NUDGE_MESSAGE in prompt
        assert STRONG_COMPLAINT in prompt
        assert "running git status" in prompt

    def test_create_rows_keep_the_create_prompt(self) -> None:
        snapshot = ContextSnapshot(before=(), trigger=None, after=())
        row = {"source_kind": "transcript_message", "context_json": snapshot.to_json(), "text": "never do X"}
        assert "DURABLE correction worth encoding as an" in build_prompt(row)


class TestGoldenReview:
    def load(self) -> tuple[list[GoldenRow], str]:
        raw = (FIXTURES / "golden_review.json").read_bytes()
        golden = [
            GoldenRow(
                dedup_key=row["dedup_key"],
                source_kind=row["source_kind"],
                text=row["text"],
                expected=row["expected"] == "accepted",
                note=row["note"],
            )
            for row in json.loads(raw)
        ]
        return golden, hashlib.sha256(raw).hexdigest()

    def fake_judge(self, text: str) -> ReviewVerdict:
        if (marker := classify_marker(text)) is not None:
            return ReviewVerdict(
                category="misfire_confirmed",
                summary="claims the hook fired wrongly",
                confidence=0.9 if marker.strength == "strong" else 0.7,
                rationale=marker.misfire_class,
            )
        if COMPLIANCE_RE.search(text):
            return ReviewVerdict(category="compliance", summary="follows the hook", confidence=0.8, rationale="c")
        return ReviewVerdict(category="ambient_mention", summary="mentions a hook", confidence=0.8, rationale="a")

    def test_fixture_shape(self) -> None:
        golden, _ = self.load()
        assert len(golden) == 14
        assert sum(row.expected for row in golden) == 4
        assert {row.source_kind for row in golden} == {"hook_complaint"}
        assert len({row.dedup_key for row in golden}) == len(golden)

    def test_golden_gate_passes_against_the_fake_judge(self) -> None:
        golden, sha = self.load()
        verdicts = {
            row.dedup_key: {
                "accepted": (verdict := self.fake_judge(row.text)).accepted,
                "category": verdict.category,
                "rationale": verdict.rationale,
            }
            for row in golden
        }
        result = golden_result(golden, {row.dedup_key for row in golden}, verdicts, sha)
        assert result.failures == ()
        assert (result.passed, result.total, result.sha256) == (14, 14, sha)

    def test_golden_gate_flags_a_regressed_judge(self) -> None:
        golden, sha = self.load()
        verdicts = {
            row.dedup_key: {"accepted": True, "category": "misfire_confirmed", "rationale": "r"} for row in golden
        }
        result = golden_result(golden, {row.dedup_key for row in golden}, verdicts, sha)
        assert result.passed == 4
        assert {failure.expected for failure in result.failures} == {False}
