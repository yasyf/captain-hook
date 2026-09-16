from __future__ import annotations

import os
from pathlib import Path

import pytest
from cc_transcript.parser import parse_events_from_bytes
from cc_transcript.query import Session

from captain_hook.app import State, use_state
from captain_hook.daemon import transcache
from captain_hook.transcripts import load_transcript
from captain_hook.util import reqenv
from captain_hook.util.reqenv import RequestOverrides

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
        assert transcache._entry_for(target).events == parse_events_from_bytes(raw)

    def test_unchanged_read_returns_cached_events(self, tmp_path: Path) -> None:
        target = write(tmp_path / "t.jsonl", FIXTURE.read_bytes())
        first = transcache._entry_for(target).events
        assert transcache._entry_for(target).events is first

    def test_append_growth_reuses_prior_parse(self, tmp_path: Path, lines: list[bytes]) -> None:
        target = write(tmp_path / "t.jsonl", b"".join(lines[:10]))
        before = transcache._entry_for(target).events
        write(target, b"".join(lines))
        after = transcache._entry_for(target).events
        assert after == parse_events_from_bytes(b"".join(lines))
        assert len(after) > len(before)
        assert all(a is b for a, b in zip(before, after, strict=False))

    def test_shrink_triggers_full_reparse(self, tmp_path: Path, lines: list[bytes]) -> None:
        target = write(tmp_path / "t.jsonl", b"".join(lines))
        transcache._entry_for(target).events
        smaller = b"".join(lines[:5])
        write(target, smaller)
        assert transcache._entry_for(target).events == parse_events_from_bytes(smaller)

    def test_in_place_change_same_size_reparses(self, tmp_path: Path) -> None:
        a = b'{"type":"queue-operation","operation":"enqueue","sessionId":"s","content":"aaa"}\n'
        b = b'{"type":"queue-operation","operation":"enqueue","sessionId":"s","content":"bbb"}\n'
        assert len(a) == len(b)
        target = write(tmp_path / "t.jsonl", a)
        first = transcache._entry_for(target).events
        write(target, b)
        second = transcache._entry_for(target).events
        assert second == parse_events_from_bytes(b)
        assert second != first

    def test_mtime_preserved_rewrite_reparses(self, tmp_path: Path) -> None:
        # FINDER-4: a same-size, mtime-preserved in-place rewrite still moves ctime, so the cache must
        # reparse rather than return the stale prior events.
        a = b'{"type":"queue-operation","operation":"enqueue","sessionId":"s","content":"aaa"}\n'
        b = b'{"type":"queue-operation","operation":"enqueue","sessionId":"s","content":"bbb"}\n'
        assert len(a) == len(b)
        target = write(tmp_path / "t.jsonl", a)
        original = target.stat()
        first = transcache._entry_for(target).events
        write(target, b)
        os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))  # restore mtime; ctime still moved
        after = target.stat()
        assert after.st_size == original.st_size and after.st_mtime_ns == original.st_mtime_ns
        second = transcache._entry_for(target).events
        assert second == parse_events_from_bytes(b)
        assert second != first

    def test_partial_trailing_line_matches_full_parse(self, tmp_path: Path, lines: list[bytes]) -> None:
        # A mid-write file whose last line lacks a newline: the daemon must drop the partial line
        # exactly as the cold full parse does.
        raw = b"".join(lines[:8]) + lines[8].rstrip(b"\n")
        target = write(tmp_path / "t.jsonl", raw)
        assert transcache._entry_for(target).events == parse_events_from_bytes(raw)

    def test_cache_clear_forces_reparse(self, tmp_path: Path) -> None:
        target = write(tmp_path / "t.jsonl", FIXTURE.read_bytes())
        first = transcache._entry_for(target).events
        transcache.cache_clear()
        assert transcache._entry_for(target).events is not first


def request(project_dir: str) -> RequestOverrides:
    return RequestOverrides(env={"CLAUDE_PROJECT_DIR": project_dir}, cwd=project_dir, client_ppid=1, session_id="s")


class TestLoad:
    def test_missing_path_yields_empty_session(self, tmp_path: Path) -> None:
        assert list(transcache.load(tmp_path / "nope.jsonl").events) == []
        assert list(transcache.load(None).events) == []

    def test_load_matches_cold_session_events(self, tmp_path: Path) -> None:
        target = write(tmp_path / "t.jsonl", FIXTURE.read_bytes())
        assert isinstance(transcache.load(target), Session)
        assert len(list(transcache.load(target).events)) == len(list(load_transcript(target).events))

    def test_unchanged_transcript_shares_the_lifted_session(self, tmp_path: Path) -> None:
        target = write(tmp_path / "t.jsonl", FIXTURE.read_bytes())
        assert transcache.load(target) is transcache.load(target)

    def test_requests_with_different_classifiers_get_different_sessions(self, tmp_path: Path) -> None:
        target = write(tmp_path / "t.jsonl", FIXTURE.read_bytes())
        with reqenv.use_request(request("/Users/someone/conductor/workspaces/repo/lane")):
            conductor = transcache.load(target)
            assert transcache.load(target) is conductor
            assert conductor.turns == load_transcript(target).turns
        with reqenv.use_request(request(str(tmp_path))):
            native = transcache.load(target)
            assert native.turns == load_transcript(target).turns
        with use_state(State(classifier=lambda event: False)):
            silent = transcache.load(target)
            assert silent.turns == load_transcript(target).turns
        assert len({id(conductor), id(native), id(silent)}) == 3
        (entry,) = transcache._CACHE.values()
        assert len(entry.lifted) == 3

    def test_growth_lifts_a_fresh_session(self, tmp_path: Path, lines: list[bytes]) -> None:
        target = write(tmp_path / "t.jsonl", b"".join(lines[:10]))
        before = transcache.load(target)
        write(target, b"".join(lines))
        after = transcache.load(target)
        assert after is not before
        assert after.turns == load_transcript(target).turns
        assert len(after) > len(before)


class TestSourceByteBudget:
    def test_evicts_least_recent_transcripts_past_the_budget(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        raw = FIXTURE.read_bytes()
        monkeypatch.setattr(transcache._CACHE, "maxsize", 2 * len(raw))
        a, b, c = [write(tmp_path / f"{name}.jsonl", raw) for name in "abc"]
        first = transcache._entry_for(a).events
        transcache._entry_for(b).events
        transcache._entry_for(c).events
        assert list(transcache._CACHE) == [b, c]
        again = transcache._entry_for(a).events
        assert again is not first
        assert again == parse_events_from_bytes(raw)
        assert list(transcache._CACHE) == [c, a]

    def test_growth_past_the_budget_evicts_others_and_matches_the_cold_parse(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lines: list[bytes]
    ) -> None:
        head, full = b"".join(lines[:10]), b"".join(lines)
        monkeypatch.setattr(transcache._CACHE, "maxsize", 2 * len(head))
        other = write(tmp_path / "other.jsonl", head)
        target = write(tmp_path / "t.jsonl", head)
        transcache._entry_for(other).events
        before = transcache._entry_for(target).events
        write(target, full)
        after = transcache._entry_for(target).events
        assert list(transcache._CACHE) == [target]
        assert after == parse_events_from_bytes(full)
        assert all(a is b for a, b in zip(before, after, strict=False))
