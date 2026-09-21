from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from operator import attrgetter
from pathlib import Path
from typing import TYPE_CHECKING

from captain_hook.transcripts import transcript_session_id, user_classifier
from captain_hook.util.caching import WeightedLRUDict

if TYPE_CHECKING:
    from cc_transcript.activity import ActivityLift, UserClassifier
    from cc_transcript.models import TranscriptEvent
    from cc_transcript.query import Session


@dataclass(slots=True)
class _Entry:
    size: int
    mtime_ns: int
    ctime_ns: int
    consumed: int
    committed: list[TranscriptEvent]
    events: list[TranscriptEvent]
    lifts: dict[int, ActivityLift] = field(default_factory=dict)
    lifted: dict[int, tuple[UserClassifier, Session]] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)


_CACHE: WeightedLRUDict[Path, _Entry] = WeightedLRUDict(128 * 1024 * 1024, weigh=attrgetter("size"))
_LOCK = threading.Lock()
_REFILL_LOCK = threading.Lock()


def load(path: str | Path | None) -> Session:
    from cc_transcript.query import Session

    if not path or not (resolved := Path(path)).exists():
        return Session(())
    entry = _entry_for(resolved)
    classifier = user_classifier(entry.events, path=resolved)
    with entry.lock:
        if (held := entry.lifted.get(id(classifier))) is not None:
            return held[1]
        session = _lift(entry, classifier, resolved)
        entry.lifted[id(classifier)] = (classifier, session)
        return session


def cache_clear() -> None:
    with _LOCK:
        _CACHE.cache_clear()


def _entry_for(path: Path) -> _Entry:
    st = path.stat()
    entry = _lookup(path)
    if _current(entry, st):
        return _store(path, entry)
    with _REFILL_LOCK:
        st = path.stat()
        entry = _lookup(path)
        if _current(entry, st):
            return _store(path, entry)
        return _refill(path, st, entry)


def _lookup(path: Path) -> _Entry | None:
    with _LOCK:
        return _CACHE.get(path)


def _current(entry: _Entry | None, st: os.stat_result) -> bool:
    return entry is not None and (entry.size, entry.mtime_ns, entry.ctime_ns) == (
        st.st_size,
        st.st_mtime_ns,
        st.st_ctime_ns,
    )


def _refill(path: Path, st: os.stat_result, entry: _Entry | None) -> _Entry:
    size, mtime_ns, ctime_ns = st.st_size, st.st_mtime_ns, st.st_ctime_ns
    match entry:
        case _Entry(size=cached) if size > cached:
            try:
                return _store(path, _grow(entry, path, _appended(path, entry.consumed, st), size, mtime_ns, ctime_ns))
            except Exception:
                pass
    with path.open("rb") as fh:
        before = os.fstat(fh.fileno())
        raw = fh.read(before.st_size)
        settled = _stamp(os.fstat(fh.fileno())) == _stamp(before)
    full = _full(raw, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    return _store(path, full) if settled else full


def _lift(entry: _Entry, classifier: UserClassifier, path: Path) -> Session:
    from cc_transcript.activity import ActivityLift
    from cc_transcript.query import Session

    with entry.lock:
        activity = None if (lift := entry.lifts.get(id(classifier))) is None else lift.activity
        if activity is None:
            lift = ActivityLift(transcript_session_id(entry.events, path=path), user_classifier=classifier)
            activity = lift.extend(entry.events)
            entry.lifts[id(classifier)] = lift
    return Session.from_activity(activity, path=path)


def _store(path: Path, entry: _Entry) -> _Entry:
    with _LOCK:
        _CACHE[path] = entry
    return entry


def _appended(path: Path, offset: int, st: os.stat_result) -> bytes:
    with path.open("rb") as fh:
        fh.seek(offset)
        appended = fh.read(st.st_size - offset)
        if _stamp(os.fstat(fh.fileno())) != _stamp(st):
            raise _ChangedUnderRead(path)
    return appended


def _stamp(st: os.stat_result) -> tuple[int, int, int, int]:
    return st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


class _ChangedUnderRead(Exception):
    pass


def _grow(entry: _Entry, path: Path, appended: bytes, size: int, mtime_ns: int, ctime_ns: int) -> _Entry:
    from cc_transcript.parser import parse_events_from_bytes

    cut = appended.rfind(b"\n") + 1
    committed = entry.committed + parse_events_from_bytes(appended[:cut])
    grown = _Entry(
        size,
        mtime_ns,
        ctime_ns,
        entry.consumed + cut,
        committed,
        committed + parse_events_from_bytes(appended[cut:]),
    )
    if len(entry.events) == len(entry.committed):
        with entry.lock:
            lifts, entry.lifts = entry.lifts, {}
        session_id = transcript_session_id(grown.events, path=path)
        grown.lifts = {
            key: lift
            for key, lift in lifts.items()
            if lift.session_id == session_id and _fed(lift) == len(entry.events)
        }
        appended = grown.events[len(entry.events) :]
        for lift in grown.lifts.values():
            lift.extend(appended)
    return grown


def _fed(lift: ActivityLift) -> int:
    return sum(len(turn.events) for turn in lift.activity.turns)


def _full(raw: bytes, size: int, mtime_ns: int, ctime_ns: int) -> _Entry:
    from cc_transcript.parser import parse_events_from_bytes

    consumed = raw.rfind(b"\n") + 1
    committed = parse_events_from_bytes(raw[:consumed])
    return _Entry(size, mtime_ns, ctime_ns, consumed, committed, committed + parse_events_from_bytes(raw[consumed:]))
