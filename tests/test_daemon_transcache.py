from __future__ import annotations

from pathlib import Path

import pytest
from cc_transcript.parser import parse_events_from_bytes
from cc_transcript.query import Session

from captain_hook.daemon import transcache
from captain_hook.transcripts import load_transcript

FIXTURE = Path(__file__).parent / "fixtures" / "hook_fires" / "fire-stop.jsonl"


@pytest.fixture(autouse=True)
def clear_transcache():
    transcache.cache_clear()
    yield
    transcache.cache_clear()


@pytest.fixture
def lines() -> list[bytes]:
    raw = FIXTURE.read_bytes()
    assert raw.endswith(b"\n")
    return [line + b"\n" for line in raw.rstrip(b"\n").split(b"\n")]


def write(path: Path, chunk: bytes) -> Path:
    path.write_bytes(chunk)
    return path


class TestEventsFor:
    def test_full_parse_matches_cold(self, tmp_path: Path) -> None:
        raw = FIXTURE.read_bytes()
        target = write(tmp_path / "t.jsonl", raw)
        assert transcache._events_for(target) == parse_events_from_bytes(raw)

    def test_unchanged_read_returns_cached_events(self, tmp_path: Path) -> None:
        target = write(tmp_path / "t.jsonl", FIXTURE.read_bytes())
        first = transcache._events_for(target)
        assert transcache._events_for(target) is first

    def test_append_growth_reuses_prior_parse(self, tmp_path: Path, lines: list[bytes]) -> None:
        target = write(tmp_path / "t.jsonl", b"".join(lines[:10]))
        before = transcache._events_for(target)
        write(target, b"".join(lines))
        after = transcache._events_for(target)
        assert after == parse_events_from_bytes(b"".join(lines))
        assert len(after) > len(before)
        assert all(a is b for a, b in zip(before, after, strict=False))

    def test_shrink_triggers_full_reparse(self, tmp_path: Path, lines: list[bytes]) -> None:
        target = write(tmp_path / "t.jsonl", b"".join(lines))
        transcache._events_for(target)
        smaller = b"".join(lines[:5])
        write(target, smaller)
        assert transcache._events_for(target) == parse_events_from_bytes(smaller)

    def test_in_place_change_same_size_reparses(self, tmp_path: Path) -> None:
        a = b'{"type":"queue-operation","operation":"enqueue","sessionId":"s","content":"aaa"}\n'
        b = b'{"type":"queue-operation","operation":"enqueue","sessionId":"s","content":"bbb"}\n'
        assert len(a) == len(b)
        target = write(tmp_path / "t.jsonl", a)
        first = transcache._events_for(target)
        write(target, b)
        second = transcache._events_for(target)
        assert second == parse_events_from_bytes(b)
        assert second != first

    def test_partial_trailing_line_matches_full_parse(self, tmp_path: Path, lines: list[bytes]) -> None:
        # A mid-write file whose last line lacks a newline: the daemon must drop the partial line
        # exactly as the cold full parse does.
        raw = b"".join(lines[:8]) + lines[8].rstrip(b"\n")
        target = write(tmp_path / "t.jsonl", raw)
        assert transcache._events_for(target) == parse_events_from_bytes(raw)

    def test_cache_clear_forces_reparse(self, tmp_path: Path) -> None:
        target = write(tmp_path / "t.jsonl", FIXTURE.read_bytes())
        first = transcache._events_for(target)
        transcache.cache_clear()
        assert transcache._events_for(target) is not first


class TestLoad:
    def test_missing_path_yields_empty_session(self, tmp_path: Path) -> None:
        assert list(transcache.load(tmp_path / "nope.jsonl").events) == []
        assert list(transcache.load(None).events) == []

    def test_load_matches_cold_session_events(self, tmp_path: Path) -> None:
        target = write(tmp_path / "t.jsonl", FIXTURE.read_bytes())
        assert isinstance(transcache.load(target), Session)
        assert len(list(transcache.load(target).events)) == len(list(load_transcript(target).events))
