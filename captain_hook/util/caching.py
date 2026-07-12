"""Small caching primitives the resident daemon needs beyond ``functools.cache``.

``functools.cache`` never evicts and never expires — fine for a short-lived cold process, a
leak in a long-lived daemon. ``LRUDict`` bounds an ad-hoc cache to its most-recent entries;
``ttl_cache`` memoizes a callable's result for a fixed window. Both expose ``cache_clear`` so
the daemon (and the test suite) can drop them on demand.
"""

from __future__ import annotations

import time
from collections import OrderedDict
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
        if len(self) > self.maxsize:
            self.popitem(last=False)

    def cache_clear(self) -> None:
        self.clear()


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
