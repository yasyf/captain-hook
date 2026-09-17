from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable


class LRUDict[K, V](OrderedDict[K, V]):
    """A dict bounded to ``maxsize`` entries, evicting the least-recently-used on overflow.

    Reads and writes mark an entry most-recent, so a hot key survives eviction. Access
    through ``[]``; ``get`` does not update recency.
    """

    def __init__(self, maxsize: int) -> None:
        super().__init__()
        self.maxsize = maxsize

    def __getitem__(self, key: K) -> V:
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value

    def __setitem__(self, key: K, value: V) -> None:
        super().__setitem__(key, value)
        self.move_to_end(key)
        while self.overflowing():
            self.popitem(last=False)

    def overflowing(self) -> bool:
        return len(self) > self.maxsize

    def cache_clear(self) -> None:
        self.clear()


class WeightedLRUDict[K, V](LRUDict[K, V]):
    def __init__(self, maxweight: int, weigh: Callable[[V], int]) -> None:
        super().__init__(maxweight)
        self.weigh = weigh

    def overflowing(self) -> bool:
        return len(self) > 1 and sum(map(self.weigh, self.values())) > self.maxsize


def ttl_cache[**P, R](ttl: float) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Memoize a callable's result for ``ttl`` seconds, keyed by its arguments.

    A hit returns the cached value; a miss (never called, or expired) recomputes and restamps.
    The wrapped callable carries ``cache_clear()`` to drop every entry.
    """

    def decorate(fn: Callable[P, R]) -> Callable[P, R]:
        store: dict[Hashable, tuple[float, R]] = {}

        @wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            key = (args, tuple(sorted(kwargs.items())))
            if (hit := store.get(key)) is not None and time.monotonic() - hit[0] < ttl:
                return hit[1]
            store[key] = (time.monotonic(), result := fn(*args, **kwargs))
            return result

        wrapper.cache_clear = store.clear
        return wrapper

    return decorate


@dataclass(frozen=True, slots=True)
class Stamped[S, V]:
    stamp: S
    computed_at: float
    value: V


class StampedCache[K, S, V]:
    def __init__(self, maxsize: int) -> None:
        self._entries: LRUDict[K, Stamped[S, V]] = LRUDict(maxsize)
        self._entries_lock = threading.Lock()
        self._refill_lock = threading.Lock()

    def get(self, key: K, stamp: S, ttl: float, compute: Callable[[], V], *, fresh: bool = False) -> V:
        if not fresh and (hit := self._valid(key, stamp, ttl)) is not None:
            return hit.value
        with self._refill_lock:
            if not fresh and (hit := self._valid(key, stamp, ttl)) is not None:
                return hit.value
            computed_at = time.monotonic()
            value = compute()
            with self._entries_lock:
                self._entries[key] = Stamped(stamp, computed_at, value)
            return value

    def cache_clear(self) -> None:
        with self._entries_lock:
            self._entries.cache_clear()

    def _valid(self, key: K, stamp: S, ttl: float) -> Stamped[S, V] | None:
        with self._entries_lock:
            entry = self._entries.get(key)
        if entry is None or entry.stamp != stamp or time.monotonic() - entry.computed_at >= ttl:
            return None
        return entry


def once[R](fn: Callable[[], R]) -> Callable[[], R]:
    guard = threading.Lock()
    held: list[R] = []

    @wraps(fn)
    def wrapper() -> R:
        if held:
            return held[0]
        with guard:
            if not held:
                held.append(fn())
            return held[0]

    wrapper.cache_clear = held.clear
    return wrapper
