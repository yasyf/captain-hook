from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from cc_transcript import keep
from cc_transcript.context import ContextWindow
from cc_transcript.ids import EventRef, EventUuid, SessionId
from cc_transcript.mining.candidates import DedupKey, FeedbackCandidate, dedup_key
from cc_transcript.mining.signals import CandidateSignal, MiningSignal

from captain_hook.review.scan import (
    REVIEWER_MARKER,
    STRICT_USER,
    ScanReport,
    collapse_cross_detector,
    detect,
    parts,
    rule_parts,
    scan,
    scan_transcript,
)
from captain_hook.review.settings import ReviewSettings
from captain_hook.review.store import ReviewStore
from tests.review_helpers import (
    CORRECTION,
    PROMPT_VERSION,
    REPO,
    Verdict,
    assistant_text,
    assistant_tool_use,
    correction_entries,
    parse,
    tool_result,
    user_text,
    write_transcript,
)

if TYPE_CHECKING:
    from pathlib import Path

WHEN = datetime(2026, 6, 1, tzinfo=UTC)
PLACEHOLDER_REF = EventRef(SessionId("s1"), EventUuid("u0"))
PLACEHOLDER_CANDIDATE = FeedbackCandidate(
    dedup_key=DedupKey("k"),
    source_kind="transcript_message",
    occurred_at=WHEN,
    text="t",
    window=ContextWindow(anchor=PLACEHOLDER_REF, before=(), trigger=None, after=(), fidelity="full", preview_chars=0),
    ref=PLACEHOLDER_REF,
    signal=CandidateSignal(0.9),
)


def signal_pair(
    detector: str, text: str, *, event_uuid: EventUuid | None = EventUuid("u1"), session: str = "s1"
) -> tuple[MiningSignal, FeedbackCandidate]:
    return (
        MiningSignal(
            kind="transcript_message",
            detector=detector,
            session_id=SessionId(session),
            event_index=0,
            event_uuid=event_uuid,
            occurred_at=WHEN,
            text=text,
            cc_version=None,
            trigger_index=0,
            signal=CandidateSignal(0.9),
        ),
        PLACEHOLDER_CANDIDATE,
    )


async def rows(store: ReviewStore, query: str) -> list[dict[str, Any]]:
    cur = await store.store.conn.execute(query)
    return [dict(row) async for row in cur]


async def judge(store: ReviewStore, key: str) -> None:
    await store.record_verdict(
        DedupKey(key), Verdict(), role="judge", prompt_version=PROMPT_VERSION, model="m1", fidelity="full"
    )


class TestDedupDesign:
    def test_parts_scope_per_session_and_rule_parts_do_not(self) -> None:
        events = parse(correction_entries(session="s9"))
        [sig] = [s for s in detect(events) if s.detector == "transcript_message"]
        assert parts(sig) == ("transcript_message", "s9", CORRECTION)
        assert rule_parts(sig) == ("transcript_message", CORRECTION)

    async def test_correction_across_three_sessions_groups_under_one_candidate(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path
    ) -> None:
        sessions = [
            ("s1", "2026-06-01T10:00:00+00:00"),
            ("s2", "2026-06-01T15:00:00+00:00"),
            ("s3", "2026-06-02T10:00:00+00:00"),
        ]
        for session, timestamp in sessions:
            path = write_transcript(
                tmp_path / f"{session}.jsonl", correction_entries(session=session, timestamp=timestamp)
            )
            report = await scan_transcript(store, path, settings=settings, repo_key=REPO)
            assert report == ScanReport(scanned=1, inserted=1)

        [candidate] = await rows(store, "SELECT * FROM candidates")
        assert (candidate["repo_key"], candidate["candidate_kind"], candidate["status"]) == (REPO, "create", "watching")
        assert candidate["source_kind"] == "transcript_message"
        assert candidate["rule"] == dedup_key("transcript_message", CORRECTION)

        observations = await rows(store, "SELECT * FROM candidate_observations")
        assert {row["candidate_id"] for row in observations} == {candidate["id"]}
        assert {row["dedup_key"] for row in observations} == {
            dedup_key("transcript_message", session, CORRECTION) for session, _ in sessions
        }
        assert len(await rows(store, "SELECT * FROM feedback_events")) == 3

        await store.enable(REPO)
        for row in observations:
            await judge(store, str(row["dedup_key"]))
        status = await store.threshold_status(int(candidate["id"]), settings=settings, prompt_version=PROMPT_VERSION)
        assert (status.sessions, status.days) == (3, 2)
        assert await store.eligible(int(candidate["id"]), settings=settings, prompt_version=PROMPT_VERSION) is True

    async def test_same_correction_twice_in_one_session_is_one_observation(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path
    ) -> None:
        entries = [
            assistant_text("first attempt"),
            user_text(CORRECTION),
            assistant_text("second attempt"),
            user_text(CORRECTION),
        ]
        path = write_transcript(tmp_path / "s.jsonl", entries)
        report = await scan_transcript(store, path, settings=settings, repo_key=REPO)
        assert report == ScanReport(scanned=1, inserted=1)
        assert len(await rows(store, "SELECT * FROM feedback_events")) == 1
        assert len(await rows(store, "SELECT * FROM candidates")) == 1
        assert len(await rows(store, "SELECT * FROM candidate_observations")) == 1


class TestSweepIngestRace:
    async def test_ingest_pair_survives_concurrent_regroup_sweep(
        self, settings: ReviewSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "review.db"
        path = write_transcript(tmp_path / "s.jsonl", correction_entries())
        async with await ReviewStore.open(db) as ingesting, await ReviewStore.open(db) as sweeping:
            # A short busy_timeout so the fix's held write lock fails the sweep's
            # BEGIN IMMEDIATE fast instead of stalling on the default 5s timeout.
            await sweeping.store.conn.execute("PRAGMA busy_timeout = 200")
            record = ingesting.record_observation
            fired = False

            async def racing_record(*args: Any, **kwargs: Any) -> None:
                # Fire a concurrent regroup sweep in the exact window between the
                # ingest loop's ensure_candidate and record_observation — the
                # cross-process interleaving the per-pair transaction must defeat.
                nonlocal fired
                if not fired:
                    fired = True
                    with contextlib.suppress(sqlite3.OperationalError):
                        await sweeping.regroup_create(prompt_version=PROMPT_VERSION)
                await record(*args, **kwargs)

            monkeypatch.setattr(ingesting, "record_observation", racing_record)
            report = await scan_transcript(ingesting, path, settings=settings, repo_key=REPO)

            assert fired
            assert report == ScanReport(scanned=1, inserted=1)
            [candidate] = await rows(ingesting, "SELECT * FROM candidates")
            observations = await rows(ingesting, "SELECT * FROM candidate_observations")
            assert len(observations) == 1
            assert observations[0]["candidate_id"] == candidate["id"]


class TestStrictUser:
    def test_prefilter_drops_acks_and_structural_noise(self) -> None:
        [ack] = parse([user_text("ok")])
        [noise] = parse([user_text("<system-reminder>be good</system-reminder>")])
        [correction] = parse([user_text(CORRECTION)])
        assert keep(ack, STRICT_USER) is False
        assert keep(noise, STRICT_USER) is False
        assert keep(correction, STRICT_USER) is True

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("ok", id="trivial-ack"),
            pytest.param("<system-reminder>be good</system-reminder>", id="structural-noise"),
            pytest.param("use uv", id="short-control-message"),
        ],
    )
    async def test_prefiltered_messages_never_persist(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, text: str
    ) -> None:
        path = write_transcript(tmp_path / "s.jsonl", [assistant_text("done"), user_text(text)])
        report = await scan_transcript(store, path, settings=settings, repo_key=REPO)
        assert report == ScanReport(scanned=1, inserted=0)
        assert await rows(store, "SELECT * FROM feedback_events") == []
        assert await rows(store, "SELECT * FROM candidates") == []

    async def test_noise_band_interrupt_correction_dropped_by_confidence_floor(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path
    ) -> None:
        entries = [
            assistant_tool_use("t1", "Bash", {"command": "rm -rf build"}),
            tool_result("t1", "[Request interrupted by user]", is_error=True),
            user_text("use uv"),
        ]
        path = write_transcript(tmp_path / "s.jsonl", entries)
        report = await scan_transcript(store, path, settings=settings, repo_key=REPO)
        assert report == ScanReport(scanned=1, inserted=0)
        assert await rows(store, "SELECT * FROM feedback_events") == []
        assert await rows(store, "SELECT * FROM candidate_observations") == []

    async def test_triggerless_transcript_message_dropped(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path
    ) -> None:
        path = write_transcript(tmp_path / "s.jsonl", [user_text(CORRECTION)])
        report = await scan_transcript(store, path, settings=settings, repo_key=REPO)
        assert report == ScanReport(scanned=1, inserted=0)
        assert await rows(store, "SELECT * FROM feedback_events") == []


class TestTranscriptGates:
    async def test_reviewer_marker_transcript_skipped(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path
    ) -> None:
        entries = [
            user_text(f"Run the {REVIEWER_MARKER} pass over session abc123"),
            *correction_entries(),
        ]
        path = write_transcript(tmp_path / "s.jsonl", entries)
        report = await scan_transcript(store, path, settings=settings, repo_key=REPO)
        assert report == ScanReport(scanned=1, inserted=0)
        assert await rows(store, "SELECT * FROM feedback_events") == []
        assert await rows(store, "SELECT * FROM candidates") == []

    async def test_non_git_cwd_dropped(self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        path = write_transcript(tmp_path / "s.jsonl", correction_entries(cwd=str(plain)))
        report = await scan_transcript(store, path, settings=settings)
        assert report == ScanReport(scanned=1, inserted=0)
        assert await rows(store, "SELECT * FROM feedback_events") == []
        assert await rows(store, "SELECT * FROM candidates") == []

    async def test_git_cwd_resolves_repo_key(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, git_repo: Path
    ) -> None:
        path = write_transcript(tmp_path / "s.jsonl", correction_entries(cwd=str(git_repo)))
        report = await scan_transcript(store, path, settings=settings)
        assert report == ScanReport(scanned=1, inserted=1)
        [candidate] = await rows(store, "SELECT * FROM candidates")
        assert candidate["repo_key"] == "github.com/yasyf/scratch"


class TestIncrementalScan:
    async def test_rescan_of_unchanged_transcripts_adds_nothing(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, git_repo: Path
    ) -> None:
        write_transcript(tmp_path / "proj" / "s.jsonl", correction_entries(cwd=str(git_repo)))
        first = await scan(store, settings=settings, transcripts=[tmp_path / "proj"])
        assert first == ScanReport(scanned=1, inserted=1)
        second = await scan(store, settings=settings, transcripts=[tmp_path / "proj"])
        assert second == ScanReport(scanned=0, inserted=0)
        assert len(await rows(store, "SELECT * FROM feedback_events")) == 1
        assert len(await rows(store, "SELECT * FROM candidate_observations")) == 1

    async def test_scan_transcript_skips_unchanged_file(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path
    ) -> None:
        path = write_transcript(tmp_path / "s.jsonl", correction_entries())
        await scan_transcript(store, path, settings=settings, repo_key=REPO)
        assert await scan_transcript(store, path, settings=settings, repo_key=REPO) == ScanReport(0, 0)

    async def test_scan_accepts_explicit_file_paths(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path, git_repo: Path
    ) -> None:
        path = write_transcript(tmp_path / "s.jsonl", correction_entries(cwd=str(git_repo)))
        report = await scan(store, settings=settings, transcripts=[path])
        assert report == ScanReport(scanned=1, inserted=1)

    async def test_missing_transcript_is_a_clean_noop(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path
    ) -> None:
        report = await scan_transcript(store, tmp_path / "gone.jsonl", settings=settings, repo_key=REPO)
        assert report == ScanReport(scanned=0, inserted=0)


class TestReviewCommentFormats:
    async def test_superset_inline_comment_extracted_and_persisted(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path
    ) -> None:
        body = "In src/foo.py:L10: use a frozen dataclass here instead"
        path = write_transcript(tmp_path / "s.jsonl", [assistant_text("rewrote the parser"), user_text(body)])
        await scan_transcript(store, path, settings=settings, repo_key=REPO)

        [event] = await rows(store, "SELECT * FROM feedback_events WHERE source_kind = 'review_comment'")
        assert event["text"] == "use a frozen dataclass here instead"
        payload = json.loads(str(event["payload_json"]))
        assert (payload["format"], payload["file"], payload["line_start"], payload["line_end"]) == (
            "superset-inline",
            "src/foo.py",
            10,
            None,
        )

        [candidate] = await rows(store, "SELECT * FROM candidates WHERE source_kind = 'review_comment'")
        assert candidate["rule"] == dedup_key(
            "review_comment", "src/foo.py", "10", "", "use a frozen dataclass here instead"
        )
        [observation] = await rows(
            store,
            f"SELECT * FROM candidate_observations WHERE candidate_id = {int(candidate['id'])}",
        )
        assert observation["dedup_key"] == dedup_key(
            "review_comment", "sess-1", "src/foo.py", "10", "", "use a frozen dataclass here instead"
        )


class TestCollapseCrossDetector:
    @pytest.mark.parametrize(
        "detector",
        ["exit_plan_rejection", "plan_reentry", "denial", "interrupt", "review_comment"],
    )
    def test_equal_text_same_event_drops_transcript_message(self, detector: str) -> None:
        kept = [signal_pair(detector, CORRECTION), signal_pair("transcript_message", CORRECTION)]
        assert [sig.detector for sig, _ in collapse_cross_detector(kept)] == [detector]

    def test_different_text_keeps_both(self) -> None:
        kept = [
            signal_pair("plan_reentry", CORRECTION),
            signal_pair("transcript_message", "always run the linter before you push"),
        ]
        assert [sig.detector for sig, _ in collapse_cross_detector(kept)] == ["plan_reentry", "transcript_message"]

    def test_different_event_uuid_keeps_both(self) -> None:
        kept = [
            signal_pair("plan_reentry", CORRECTION, event_uuid=EventUuid("ua")),
            signal_pair("transcript_message", CORRECTION, event_uuid=EventUuid("ub")),
        ]
        assert [sig.detector for sig, _ in collapse_cross_detector(kept)] == ["plan_reentry", "transcript_message"]

    def test_null_event_uuid_keeps_both(self) -> None:
        kept = [
            signal_pair("plan_reentry", CORRECTION, event_uuid=None),
            signal_pair("transcript_message", CORRECTION, event_uuid=None),
        ]
        assert [sig.detector for sig, _ in collapse_cross_detector(kept)] == ["plan_reentry", "transcript_message"]

    def test_transcript_message_alone_kept(self) -> None:
        kept = [signal_pair("transcript_message", CORRECTION)]
        assert [sig.detector for sig, _ in collapse_cross_detector(kept)] == ["transcript_message"]

    @pytest.mark.parametrize("detector", ["ask_user_question", "hook_complaint"])
    def test_question_answer_and_hook_complaint_never_shadow(self, detector: str) -> None:
        kept = [signal_pair(detector, CORRECTION), signal_pair("transcript_message", CORRECTION)]
        assert [sig.detector for sig, _ in collapse_cross_detector(kept)] == [detector, "transcript_message"]


class TestCrossDetectorCollapseIngest:
    async def test_plan_reentry_shadows_transcript_message_to_one_candidate(
        self, store: ReviewStore, settings: ReviewSettings, tmp_path: Path
    ) -> None:
        entries = [
            assistant_tool_use("t1", "Edit", {"file_path": "foo.py", "old_string": "a", "new_string": "b"}),
            {"type": "mode", "sessionId": "sess-1", "mode": "plan"},
            user_text(CORRECTION),
        ]
        path = write_transcript(tmp_path / "s.jsonl", entries)
        report = await scan_transcript(store, path, settings=settings, repo_key=REPO)
        assert report == ScanReport(scanned=1, inserted=1)

        [candidate] = await rows(store, "SELECT * FROM candidates")
        assert candidate["source_kind"] == "plan_review"
        assert candidate["rule"] == dedup_key("plan_review", "plan_reentry", CORRECTION)
        assert len(await rows(store, "SELECT * FROM feedback_events")) == 1
