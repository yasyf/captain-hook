from __future__ import annotations

import itertools
import json
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from cc_transcript import keep, parse_events_from_bytes
from cc_transcript.mining.candidates import DedupKey, dedup_key

from captain_hook.review.repo import RepoKey
from captain_hook.review.scan import (
    REVIEWER_MARKER,
    STRICT_USER,
    ScanReport,
    detect,
    parts,
    rule_parts,
    scan,
    scan_transcript,
)
from captain_hook.review.settings import ReviewSettings
from captain_hook.review.store import ReviewStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from cc_transcript.models import TranscriptEvent

REPO = RepoKey("github.com/yasyf/captain-hook")
BASE_TS = "2026-06-01T12:00:00+00:00"
CORRECTION = "no, never use a bare except here, always catch the specific parser error"
PROMPT_VERSION = 1

counter = itertools.count()


@dataclass(frozen=True, slots=True)
class Verdict:
    accepted: bool = True
    confidence: float = 0.9
    category: str = "durable_correction"
    summary: str = "user corrected approach"
    rationale: str = "explicit correction"


def next_uuid() -> str:
    return f"uuid-{next(counter)}"


def envelope(entry_type: str, **overrides: Any) -> dict[str, Any]:
    return {
        "type": entry_type,
        "uuid": overrides.pop("uuid", next_uuid()),
        "parentUuid": None,
        "sessionId": overrides.pop("sessionId", "sess-1"),
        "timestamp": overrides.pop("timestamp", BASE_TS),
        "cwd": overrides.pop("cwd", "/repo"),
        "gitBranch": "main",
        "version": "1.2.3",
        "isSidechain": False,
        "isMeta": False,
        "entrypoint": "cli",
        **overrides,
    }


def user_text(text: str, **overrides: Any) -> dict[str, Any]:
    return envelope("user", message={"role": "user", "content": text}, **overrides)


def assistant_text(text: str, **overrides: Any) -> dict[str, Any]:
    return envelope(
        "assistant",
        message={"role": "assistant", "model": "claude", "content": [{"type": "text", "text": text}]},
        **overrides,
    )


def assistant_tool_use(tool_id: str, name: str, tool_input: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return envelope(
        "assistant",
        message={
            "role": "assistant",
            "model": "claude",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}],
        },
        **overrides,
    )


def tool_result(tool_id: str, content: str, *, is_error: bool = False, **overrides: Any) -> dict[str, Any]:
    return envelope(
        "user",
        message={
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": is_error}],
        },
        **overrides,
    )


def correction_entries(*, session: str = "sess-1", timestamp: str = BASE_TS, **overrides: Any) -> list[dict[str, Any]]:
    return [
        assistant_text("I'll wrap the parser in a bare except", sessionId=session, timestamp=timestamp, **overrides),
        user_text(CORRECTION, sessionId=session, timestamp=timestamp, **overrides),
    ]


def parse(entries: list[dict[str, Any]]) -> list[TranscriptEvent]:
    return parse_events_from_bytes("".join(json.dumps(entry) + "\n" for entry in entries).encode())


def write_transcript(path: Path, entries: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    return path


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[ReviewStore]:
    async with await ReviewStore.open(tmp_path / "review.db") as opened:
        yield opened


@pytest.fixture
def settings() -> ReviewSettings:
    return ReviewSettings()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:yasyf/scratch.git"], check=True)
    return repo


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
