from __future__ import annotations

import threading
from dataclasses import dataclass
from operator import attrgetter
from pathlib import Path
from typing import TYPE_CHECKING

from captain_hook.transcripts import lift_session
from captain_hook.util.caching import WeightedLRUDict

if TYPE_CHECKING:
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


_CACHE: WeightedLRUDict[Path, _Entry] = WeightedLRUDict(MAX_SOURCE_BYTES, weigh=attrgetter("size"))
_LOCK = threading.Lock()


def load(path: str | Path | None) -> Session:
    from cc_transcript.query import Session

    if not path or not (resolved := Path(path)).exists():
        return Session(())
    return lift_session(_events_for(resolved), path=resolved)


def cache_clear() -> None:
    with _LOCK:
        _CACHE.cache_clear()


def _events_for(path: Path) -> list[TranscriptEvent]:
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
                return _store(path, _grow(entry, path.read_bytes(), size, mtime_ns, ctime_ns))
            except Exception:
                pass
    return _store(path, _full(path.read_bytes(), size, mtime_ns, ctime_ns))


def _store(path: Path, entry: _Entry) -> list[TranscriptEvent]:
    with _LOCK:
        _CACHE[path] = entry
    return entry.events


def _grow(entry: _Entry, raw: bytes, size: int, mtime_ns: int, ctime_ns: int) -> _Entry:
    from cc_transcript.parser import parse_events_from_bytes

    consumed = raw.rfind(b"\n") + 1
    committed = entry.committed + parse_events_from_bytes(raw[entry.consumed : consumed])
    return _Entry(size, mtime_ns, ctime_ns, consumed, committed, committed + parse_events_from_bytes(raw[consumed:]))


def _full(raw: bytes, size: int, mtime_ns: int, ctime_ns: int) -> _Entry:
    from cc_transcript.parser import parse_events_from_bytes

    consumed = raw.rfind(b"\n") + 1
    committed = parse_events_from_bytes(raw[:consumed])
    return _Entry(size, mtime_ns, ctime_ns, consumed, committed, committed + parse_events_from_bytes(raw[consumed:]))
