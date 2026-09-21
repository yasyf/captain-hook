from __future__ import annotations

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

import pytest
from cc_transcript.activity import ActivityLift, native_user_classifier
from cc_transcript.parser import parse_events_from_bytes
from cc_transcript.query import Session

from captain_hook.app import State, use_state
from captain_hook.daemon import transcache
from captain_hook.transcripts import lift_classified, load_transcript
from captain_hook.util import reqenv
from captain_hook.util.reqenv import RequestOverrides

if TYPE_CHECKING:
    from collections.abc import Iterator

    from cc_transcript.activity import UserClassifier
    from cc_transcript.models import TranscriptEvent, UserEvent

FIXTURE = Path(__file__).parent / "fixtures" / "hook_fires" / "fire-stop.jsonl"
TOOL_HEAVY = Path(__file__).parent / "fixtures" / "hook_fires" / "fire-misfire-complaint.jsonl"


@pytest.fixture(autouse=True)
def clear_transcache():
    transcache.cache_clear()
    yield
    transcache.cache_clear()


@pytest.fixture
def lines() -> list[bytes]:
    raw = FIXTURE.read_bytes()
    assert raw.endswith(b"\n")
    return split_lines(raw)


def split_lines(raw: bytes) -> list[bytes]:
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

    def test_concurrent_cold_reads_share_one_parse(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = write(tmp_path / "t.jsonl", FIXTURE.read_bytes())
        original = transcache._full
        calls = 0

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            time.sleep(0.05)
            return original(*args, **kwargs)

        monkeypatch.setattr(transcache, "_full", counted)
        with ThreadPoolExecutor(max_workers=16) as pool:
            entries = list(pool.map(lambda _: transcache._entry_for(target), range(16)))

        assert calls == 1
        assert all(entry is entries[0] for entry in entries)


@dataclass
class PromptClassifier:
    def __call__(self, event: UserEvent) -> bool:
        return bool(event.text.strip())


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

    def test_unhashable_classifier_lifts_like_the_cold_loader(self, tmp_path: Path) -> None:
        target = write(tmp_path / "t.jsonl", FIXTURE.read_bytes())
        with use_state(State(classifier=PromptClassifier())):
            session = transcache.load(target)
            assert transcache.load(target) is session
            assert session.turns == load_transcript(target).turns

    def test_growth_lifts_a_fresh_session(self, tmp_path: Path, lines: list[bytes]) -> None:
        target = write(tmp_path / "t.jsonl", b"".join(lines[:10]))
        before = transcache.load(target)
        write(target, b"".join(lines))
        after = transcache.load(target)
        assert after is not before
        assert after.turns == load_transcript(target).turns
        assert len(after) > len(before)

    def test_concurrent_loads_share_one_lift(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = write(tmp_path / "t.jsonl", FIXTURE.read_bytes())
        original = transcache._lift
        calls = 0

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            time.sleep(0.05)
            return original(*args, **kwargs)

        monkeypatch.setattr(transcache, "_lift", counted)
        with ThreadPoolExecutor(max_workers=16) as pool:
            sessions = list(pool.map(lambda _: transcache.load(target), range(16)))

        assert calls == 1
        assert all(session is sessions[0] for session in sessions)


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


def no_prompts(event: UserEvent) -> bool:
    return False


@dataclass
class PromptsWithText:
    def __call__(self, event: UserEvent) -> bool:
        return bool(event.text.strip())


def cuts(lines: list[bytes], step: str) -> Iterator[bytes]:
    for i, line in enumerate(lines):
        prefix = b"".join(lines[:i])
        match step:
            case "line":
                yield prefix + line
            case "three-lines" if i % 3 == 2 or i == len(lines) - 1:
                yield prefix + line
            case "mid-line":
                yield prefix + line[: len(line) // 2]
                yield prefix + line[:-1]
                yield prefix + line


def cursor_of(target: Path, classifier: UserClassifier) -> object:
    return transcache._CACHE[target].lifts[id(classifier)]


class TestIncrementalLift:
    @pytest.mark.parametrize("step", ["line", "three-lines", "mid-line"])
    def test_growth_matches_a_cold_lift_at_every_step(self, tmp_path: Path, step: str) -> None:
        target = tmp_path / "t.jsonl"
        for raw in cuts(split_lines(TOOL_HEAVY.read_bytes()), step):
            write(target, raw)
            assert transcache.load(target) == load_transcript(target)
            assert transcache._lift(transcache._entry_for(target), no_prompts, target) == lift_classified(
                parse_events_from_bytes(raw), no_prompts, path=target
            )

    def test_line_growth_extends_one_cursor_across_a_split_tool_result(self, tmp_path: Path) -> None:
        lines = split_lines(TOOL_HEAVY.read_bytes())
        target = write(tmp_path / "t.jsonl", b"".join(lines[:9]))
        before = transcache.load(target)
        assert [use.result for use in before.tool_calls] == [None]
        cursor = cursor_of(target, native_user_classifier)
        for end in range(10, len(lines) + 1):
            write(target, b"".join(lines[:end]))
            assert transcache.load(target) == load_transcript(target)
            assert cursor_of(target, native_user_classifier) is cursor
        assert all(use.result is not None for use in transcache.load(target).tool_calls)

    def test_an_unterminated_line_that_growth_invalidates_leaves_the_lift(self, tmp_path: Path) -> None:
        lines = split_lines(TOOL_HEAVY.read_bytes())
        head = b"".join(lines[:12])
        target = write(tmp_path / "t.jsonl", head + lines[12][:-1])
        assert [use.result for use in transcache.load(target).tool_calls][-1] is not None
        write(target, head + lines[12][:-1] + b"x\n" + b"".join(lines[13:]))
        assert transcache.load(target) == load_transcript(target)

    def test_growth_feeds_each_cursor_exactly_the_events_after_its_last_feed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        feeds: dict[ActivityLift, list[TranscriptEvent]] = {}
        extend = ActivityLift.extend

        def recording(lift: ActivityLift, events: list[TranscriptEvent]) -> object:
            feeds.setdefault(lift, []).extend(events)
            return extend(lift, events)

        monkeypatch.setattr(ActivityLift, "extend", recording)
        target = tmp_path / "t.jsonl"
        for raw in cuts(split_lines(TOOL_HEAVY.read_bytes()), "mid-line"):
            write(target, raw)
            transcache.load(target)
            assert feeds[cursor_of(target, native_user_classifier)] == transcache._CACHE[target].events

    def test_a_cursor_fed_past_its_entry_is_not_inherited(self, tmp_path: Path) -> None:
        lines = split_lines(TOOL_HEAVY.read_bytes())
        target = write(tmp_path / "t.jsonl", b"".join(lines[:10]))
        entry = transcache._entry_for(target)
        transcache._lift(entry, native_user_classifier, target)
        overfed = entry.lifts[id(native_user_classifier)]
        overfed.extend(entry.events[-1:])
        write(target, b"".join(lines))
        assert id(native_user_classifier) not in transcache._entry_for(target).lifts
        assert transcache.load(target) == load_transcript(target)

    def test_a_session_id_learned_by_growth_starts_a_fresh_cursor(self, tmp_path: Path) -> None:
        lines = split_lines(TOOL_HEAVY.read_bytes())
        target = write(tmp_path / "t.jsonl", b"".join(lines[:2]))
        transcache.load(target)
        stem_cursor = cursor_of(target, native_user_classifier)
        write(target, b"".join(lines))
        assert transcache.load(target) == load_transcript(target)
        assert cursor_of(target, native_user_classifier) is not stem_cursor

    def test_full_reparse_starts_fresh_cursors(self, tmp_path: Path) -> None:
        lines = split_lines(TOOL_HEAVY.read_bytes())
        target = write(tmp_path / "t.jsonl", b"".join(lines))
        transcache.load(target)
        cursor = cursor_of(target, native_user_classifier)
        write(target, b"".join(lines[:20]))
        assert transcache.load(target) == load_transcript(target)
        assert cursor_of(target, native_user_classifier) is not cursor

    def test_each_classifier_extends_its_own_cursor(self, tmp_path: Path) -> None:
        lines = split_lines(TOOL_HEAVY.read_bytes())
        target = write(tmp_path / "t.jsonl", b"".join(lines[:20]))
        entry = transcache._entry_for(target)
        transcache._lift(entry, native_user_classifier, target)
        transcache._lift(entry, no_prompts, target)
        native, quiet = entry.lifts[id(native_user_classifier)], entry.lifts[id(no_prompts)]
        assert native is not quiet
        write(target, b"".join(lines))
        grown = transcache._entry_for(target)
        assert grown.lifts == {id(native_user_classifier): native, id(no_prompts): quiet}
        for classifier in (native_user_classifier, no_prompts):
            assert transcache._lift(grown, classifier, target) == lift_classified(grown.events, classifier, path=target)

    def test_an_unhashable_classifier_extends_its_own_cursor_through_growth(self, tmp_path: Path) -> None:
        classifier = PromptsWithText()
        with pytest.raises(TypeError):
            hash(classifier)
        lines = split_lines(TOOL_HEAVY.read_bytes())
        target = write(tmp_path / "t.jsonl", b"".join(lines[:20]))
        entry = transcache._entry_for(target)
        transcache._lift(entry, classifier, target)
        cursor = entry.lifts[id(classifier)]
        write(target, b"".join(lines))
        grown = transcache._entry_for(target)
        assert grown.lifts[id(classifier)] is cursor
        assert cursor.user_classifier is classifier
        assert transcache._lift(grown, classifier, target) == lift_classified(grown.events, classifier, path=target)


class TestTailRead:
    def test_growth_reads_only_the_appended_bytes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        lines = split_lines(TOOL_HEAVY.read_bytes())
        target = write(tmp_path / "t.jsonl", b"".join(lines[:40]))
        transcache.load(target)
        write(target, b"".join(lines))
        read: list[int] = []
        opened = Path.open

        class Counting:
            def __init__(self, fh: BinaryIO) -> None:
                self.fh = fh

            def __enter__(self) -> Counting:
                return self

            def __exit__(self, *exc: object) -> None:
                self.fh.close()

            def seek(self, offset: int) -> int:
                return self.fh.seek(offset)

            def fileno(self) -> int:
                return self.fh.fileno()

            def read(self, size: int = -1) -> bytes:
                chunk = self.fh.read(size)
                read.append(len(chunk))
                return chunk

        monkeypatch.setattr(Path, "open", lambda self, *args, **kwargs: Counting(opened(self, *args, **kwargs)))
        assert transcache.load(target) == lift_classified(
            parse_events_from_bytes(b"".join(lines)), native_user_classifier, path=target
        )
        assert read == [len(b"".join(lines[40:]))]

    @pytest.mark.parametrize(("start", "end"), [(0, 5), (1, 46)])
    def test_a_file_rewritten_shorter_between_stat_and_read_reparses_in_full(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, start: int, end: int
    ) -> None:
        lines = split_lines(TOOL_HEAVY.read_bytes())
        target = write(tmp_path / "t.jsonl", b"".join(lines[:40]))
        transcache.load(target)
        write(target, b"".join(lines))
        rewritten = b"".join(lines[start:end])
        assert len(rewritten) < target.stat().st_size
        opened = Path.open

        def rewrite_then_open(self: Path, *args: object, **kwargs: object) -> BinaryIO:
            monkeypatch.setattr(Path, "open", opened)
            write(target, rewritten)
            return opened(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", rewrite_then_open)
        assert transcache._entry_for(target).events == parse_events_from_bytes(rewritten)
        assert transcache.load(target) == load_transcript(target)
        assert transcache._entry_for(target).events == parse_events_from_bytes(rewritten)

    @pytest.mark.parametrize("longer", [False, True])
    def test_a_file_rewritten_equal_or_longer_between_stat_and_read_never_splices(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, longer: bool
    ) -> None:
        lines = split_lines(TOOL_HEAVY.read_bytes())
        target = write(tmp_path / "t.jsonl", b"".join(lines[:40]))
        transcache.load(target)
        grown = b"".join(lines[:50])
        write(target, grown)
        rewritten = b"".join(lines[1:50] + lines[:1] + (lines[50:] if longer else []))
        assert (len(rewritten) > len(grown)) is longer and len(rewritten) >= len(grown)
        opened = Path.open

        def rewrite_then_open(self: Path, *args: object, **kwargs: object) -> BinaryIO:
            monkeypatch.setattr(Path, "open", opened)
            write(target, rewritten)
            return opened(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", rewrite_then_open)
        assert transcache._entry_for(target).events == parse_events_from_bytes(rewritten)
        assert transcache.load(target) == load_transcript(target)

    def test_a_full_reparse_torn_by_a_rewrite_is_not_cached(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        lines = split_lines(TOOL_HEAVY.read_bytes())
        target = write(tmp_path / "t.jsonl", b"".join(lines[:40]))
        rewritten = b"".join(lines[1:40] + lines[:1] + lines[40:])
        opened = Path.open

        class RewrittenMidRead:
            def __init__(self, fh: BinaryIO) -> None:
                self.fh = fh

            def __enter__(self) -> RewrittenMidRead:
                return self

            def __exit__(self, *exc: object) -> None:
                self.fh.close()

            def fileno(self) -> int:
                return self.fh.fileno()

            def read(self, size: int = -1) -> bytes:
                chunk = self.fh.read(size // 2)
                with opened(target, "wb") as out:
                    out.write(rewritten)
                return chunk + self.fh.read(size - len(chunk))

        monkeypatch.setattr(Path, "open", lambda self, *args, **kwargs: RewrittenMidRead(opened(self, *args, **kwargs)))
        transcache._entry_for(target)
        assert target not in transcache._CACHE
        monkeypatch.setattr(Path, "open", opened)
        assert transcache._entry_for(target).events == parse_events_from_bytes(rewritten)
        assert transcache.load(target) == load_transcript(target)


class TestCursorOwnership:
    def test_a_stale_entry_hands_its_cursor_off_and_lifts_afresh(self, tmp_path: Path) -> None:
        lines = split_lines(TOOL_HEAVY.read_bytes())
        target = write(tmp_path / "t.jsonl", b"".join(lines[:10]))
        stale = transcache._entry_for(target)
        transcache._lift(stale, native_user_classifier, target)
        cursor = stale.lifts[id(native_user_classifier)]
        write(target, b"".join(lines[:30]))
        current = transcache._entry_for(target)
        assert current.lifts[id(native_user_classifier)] is cursor
        assert stale.lifts == {}
        assert transcache._lift(stale, native_user_classifier, target) == lift_classified(
            stale.events, native_user_classifier, path=target
        )
        assert stale.lifts[id(native_user_classifier)] is not cursor

    def test_a_second_growth_from_a_stale_entry_never_touches_the_current_cursor(self, tmp_path: Path) -> None:
        lines = split_lines(TOOL_HEAVY.read_bytes())
        target = write(tmp_path / "t.jsonl", b"".join(lines[:10]))
        stale = transcache._entry_for(target)
        transcache._lift(stale, native_user_classifier, target)
        write(target, b"".join(lines[:30]))
        current = transcache._entry_for(target)
        taken = current.lifts[id(native_user_classifier)].activity
        write(target, b"".join(lines))
        st = target.stat()
        rival = transcache._grow(
            stale, target, target.read_bytes()[stale.consumed :], st.st_size, st.st_mtime_ns, st.st_ctime_ns
        )
        assert rival.lifts == {}
        assert current.lifts[id(native_user_classifier)].activity is taken
        for entry in (current, rival):
            assert transcache._lift(entry, native_user_classifier, target) == lift_classified(
                entry.events, native_user_classifier, path=target
            )

    def test_a_reader_copies_the_activity_out_before_a_growth_can_take_the_cursor(self, tmp_path: Path) -> None:
        lines = split_lines(TOOL_HEAVY.read_bytes())
        target = write(tmp_path / "t.jsonl", b"".join(lines[:10]))
        entry = transcache._entry_for(target)
        transcache._lift(entry, native_user_classifier, target)
        cursor = entry.lifts[id(native_user_classifier)]
        write(target, b"".join(lines))
        growth = threading.Thread(target=transcache._entry_for, args=(target,))

        class GrowsMidRead(dict):
            def get(self, key: object, default: object = None) -> object:
                growth.start()
                growth.join(timeout=0.2)
                return super().get(key, default)

        entry.lifts = GrowsMidRead(entry.lifts)
        assert transcache._lift(entry, native_user_classifier, target) == lift_classified(
            entry.events, native_user_classifier, path=target
        )
        growth.join()
        assert transcache._CACHE[target].lifts[id(native_user_classifier)] is cursor

    def test_readers_racing_growth_see_exactly_their_entrys_lift(self, tmp_path: Path) -> None:
        lines = split_lines(TOOL_HEAVY.read_bytes())
        target = write(tmp_path / "t.jsonl", b"".join(lines[:3]))
        seen: list[tuple[transcache._Entry, UserClassifier, Session]] = []
        grown = threading.Event()

        def grow() -> None:
            with target.open("ab") as fh:
                for line in lines[3:]:
                    for part in (line[: len(line) // 2], line[len(line) // 2 :]):
                        fh.write(part)
                        fh.flush()
                        transcache._entry_for(target)
            grown.set()

        def read(classifier: UserClassifier) -> None:
            while not grown.is_set():
                entry = transcache._entry_for(target)
                seen.append((entry, classifier, transcache._lift(entry, classifier, target)))

        interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            readers = [threading.Thread(target=read, args=(c,)) for c in (native_user_classifier, no_prompts) * 6]
            for thread in readers:
                thread.start()
            grow()
            for thread in readers:
                thread.join()
        finally:
            sys.setswitchinterval(interval)

        cold: dict[tuple[int, UserClassifier], Session] = {}
        assert len({len(entry.events) for entry, _, _ in seen}) > len(lines) // 2
        for entry, classifier, session in seen:
            key = (len(entry.events), classifier)
            if key not in cold:
                cold[key] = lift_classified(entry.events, classifier, path=target)
            assert session == cold[key]
