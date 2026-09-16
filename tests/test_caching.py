from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from captain_hook.util import caching
from captain_hook.util.caching import LRUDict, StampedCache, ttl_cache


class TestLRUDict:
    def test_evicts_least_recently_used_on_overflow(self) -> None:
        d: LRUDict[str, int] = LRUDict(2)
        d["a"], d["b"] = 1, 2
        d["c"] = 3
        assert "a" not in d
        assert dict(d) == {"b": 2, "c": 3}

    def test_read_marks_entry_most_recent(self) -> None:
        d: LRUDict[str, int] = LRUDict(2)
        d["a"], d["b"] = 1, 2
        assert d["a"] == 1  # touch a → b is now least-recent
        d["c"] = 3
        assert "b" not in d
        assert dict(d) == {"a": 1, "c": 3}

    def test_overwrite_marks_entry_most_recent(self) -> None:
        d: LRUDict[str, int] = LRUDict(2)
        d["a"], d["b"] = 1, 2
        d["a"] = 10  # overwrite a → b is now least-recent
        d["c"] = 3
        assert "b" not in d
        assert d["a"] == 10

    def test_cache_clear_empties(self) -> None:
        d: LRUDict[str, int] = LRUDict(4)
        d["a"], d["b"] = 1, 2
        d.cache_clear()
        assert len(d) == 0


class TestTtlCache:
    @pytest.fixture
    def clock(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        now = [1000.0]
        monkeypatch.setattr(caching.time, "monotonic", lambda: now[0])
        return now

    def test_memoizes_within_ttl(self, clock: list[float]) -> None:
        calls: list[int] = []

        @ttl_cache(10)
        def f(x: int) -> int:
            calls.append(x)
            return x * 2

        assert f(3) == 6
        clock[0] += 9
        assert f(3) == 6
        assert calls == [3]

    def test_recomputes_after_expiry(self, clock: list[float]) -> None:
        calls: list[int] = []

        @ttl_cache(10)
        def f(x: int) -> int:
            calls.append(x)
            return x

        f(3)
        clock[0] += 11
        f(3)
        assert calls == [3, 3]

    def test_keys_by_arguments(self, clock: list[float]) -> None:
        calls: list[int] = []

        @ttl_cache(10)
        def f(x: int) -> int:
            calls.append(x)
            return x

        f(1)
        f(2)
        f(1)
        assert calls == [1, 2]

    def test_cache_clear_forces_recompute(self, clock: list[float]) -> None:
        calls: list[int] = []

        @ttl_cache(10)
        def f() -> int:
            calls.append(1)
            return 1

        f()
        f.cache_clear()
        f()
        assert calls == [1, 1]


class TestStampedCache:
    @pytest.fixture
    def clock(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        now = [1000.0]
        monkeypatch.setattr(caching.time, "monotonic", lambda: now[0])
        return now

    @pytest.fixture
    def calls(self) -> list[int]:
        return []

    @pytest.fixture
    def compute(self, calls: list[int]):
        def compute() -> int:
            calls.append(1)
            return len(calls)

        return compute

    def test_holds_while_stamp_matches_within_ttl(self, clock: list[float], calls: list[int], compute) -> None:
        cache: StampedCache[str, int, int] = StampedCache(4)
        assert cache.get("k", 1, 10, compute) == 1
        clock[0] += 9
        assert cache.get("k", 1, 10, compute) == 1
        assert calls == [1]

    def test_stamp_change_recomputes_at_once(self, clock: list[float], calls: list[int], compute) -> None:
        cache: StampedCache[str, int, int] = StampedCache(4)
        cache.get("k", 1, 10, compute)
        assert cache.get("k", 2, 10, compute) == 2

    def test_expiry_recomputes(self, clock: list[float], calls: list[int], compute) -> None:
        cache: StampedCache[str, int, int] = StampedCache(4)
        cache.get("k", 1, 10, compute)
        clock[0] += 10
        assert cache.get("k", 1, 10, compute) == 2

    def test_fresh_recomputes_and_restamps(self, clock: list[float], calls: list[int], compute) -> None:
        cache: StampedCache[str, int, int] = StampedCache(4)
        cache.get("k", 1, 10, compute)
        assert cache.get("k", 1, 10, compute, fresh=True) == 2
        assert cache.get("k", 1, 10, compute) == 2

    def test_cache_clear_forces_recompute(self, clock: list[float], calls: list[int], compute) -> None:
        cache: StampedCache[str, int, int] = StampedCache(4)
        cache.get("k", 1, 10, compute)
        cache.cache_clear()
        assert cache.get("k", 1, 10, compute) == 2

    def test_concurrent_stale_readers_share_one_refill(self) -> None:
        cache: StampedCache[str, int, int] = StampedCache(4)
        calls: list[int] = []

        def slow() -> int:
            calls.append(1)
            time.sleep(0.05)
            return len(calls)

        start = threading.Barrier(12)

        def reader() -> int:
            start.wait()
            return cache.get("k", 1, 10, slow)

        with ThreadPoolExecutor(max_workers=12) as pool:
            values = [f.result() for f in [pool.submit(reader) for _ in range(12)]]

        assert calls == [1]
        assert values == [1] * 12
