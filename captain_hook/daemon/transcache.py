from __future__ import annotations

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

MAX_SOURCE_BYTES = 128 * 1024 * 1024


@dataclass(slots=True)
class _Entry:
    size: int
    mtime_ns: int
    ctime_ns: int
    consumed: int
    committed: list[TranscriptEvent]
    events: list[TranscriptEvent]
    lifts: dict[UserClassifier, ActivityLift] = field(default_factory=dict)
    lifted: dict[int, tuple[UserClassifier, Session]] = field(default_factory=dict)


_CACHE: WeightedLRUDict[Path, _Entry] = WeightedLRUDict(MAX_SOURCE_BYTES, weigh=attrgetter("size"))
_LOCK = threading.Lock()


def load(path: str | Path | None) -> Session:
    from cc_transcript.query import Session

    if not path or not (resolved := Path(path)).exists():
        return Session(())
    entry = _entry_for(resolved)
    classifier = user_classifier(entry.events, path=resolved)
    if (held := entry.lifted.get(id(classifier))) is not None:
        return held[1]
    return entry.lifted.setdefault(id(classifier), (classifier, _lift(entry, classifier, resolved)))[1]


def cache_clear() -> None:
    with _LOCK:
        _CACHE.cache_clear()


def _entry_for(path: Path) -> _Entry:
    st = path.stat()
    size, mtime_ns, ctime_ns = st.st_size, st.st_mtime_ns, st.st_ctime_ns
    with _LOCK:
        entry = _CACHE.get(path)
    match entry:
        case _Entry(size=cached, mtime_ns=mstamp, ctime_ns=cstamp) if (
            cached == size and mstamp == mtime_ns and cstamp == ctime_ns
        ):
            return _store(path, entry)
        case _Entry(size=cached) if size > cached:
            try:
                return _store(path, _grow(entry, path, path.read_bytes(), size, mtime_ns, ctime_ns))
            except Exception:
                pass
    return _store(path, _full(path.read_bytes(), size, mtime_ns, ctime_ns))


def _lift(entry: _Entry, classifier: UserClassifier, path: Path) -> Session:
    from cc_transcript.activity import ActivityLift
    from cc_transcript.query import Session

    with _LOCK:
        activity = None if (lift := entry.lifts.get(classifier)) is None else lift.activity
    if activity is None:
        lift = ActivityLift(transcript_session_id(entry.events, path=path), user_classifier=classifier)
        activity = lift.extend(entry.events)
        with _LOCK:
            entry.lifts.setdefault(classifier, lift)
    return Session.from_activity(activity, path=path)


def _store(path: Path, entry: _Entry) -> _Entry:
    with _LOCK:
        _CACHE[path] = entry
    return entry


def _grow(entry: _Entry, path: Path, raw: bytes, size: int, mtime_ns: int, ctime_ns: int) -> _Entry:
    from cc_transcript.parser import parse_events_from_bytes

    consumed = raw.rfind(b"\n") + 1
    committed = entry.committed + parse_events_from_bytes(raw[entry.consumed : consumed])
    grown = _Entry(size, mtime_ns, ctime_ns, consumed, committed, committed + parse_events_from_bytes(raw[consumed:]))
    if len(entry.events) == len(entry.committed):
        with _LOCK:
            lifts, entry.lifts = entry.lifts, {}
        session_id = transcript_session_id(grown.events, path=path)
        grown.lifts = {
            classifier: lift
            for classifier, lift in lifts.items()
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
