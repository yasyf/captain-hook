from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from captain_hook.dispatch import execute_hook
from captain_hook.events import PreToolUseEvent
from captain_hook.session import SessionStore
from captain_hook.state import HookState, PrimitiveState
from captain_hook.types import Action, Event, HookResult, HookSpec, RegisteredHook


def edit_evt() -> PreToolUseEvent:
    return PreToolUseEvent(
        _raw={"tool_name": "Edit", "tool_input": {"file_path": "a.py", "old_string": "x", "new_string": "y"}},
        ctx=MagicMock(),
    )


class TestDispatchFireRace:
    def test_max_fires_not_exceeded_under_thread_race(self, tmp_path: Path) -> None:
        """N threads racing one capped hook must deliver at most ``max_fires`` fires.

        Counts DELIVERED (non-None) results, not the persisted ``fire_count`` — last-write-wins
        converges the persisted count so a persisted-count assertion stays green against the bug.
        """
        n = 20
        barrier = threading.Barrier(n)

        def handler(_evt: object) -> HookResult:
            time.sleep(0.01)
            return HookResult(action=Action.warn, message="fired")

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse, max_fires=3),
            handler=handler,
            name="race_hook",
        )

        delivered: list[HookResult] = []
        guard = threading.Lock()

        def worker() -> None:
            barrier.wait()
            if (result := execute_hook(entry, edit_evt(), tmp_path)) is not None:
                with guard:
                    delivered.append(result)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(delivered) == 3, f"delivered {len(delivered)} fires, expected exactly 3 (max_fires=3)"


class TestSessionSlotMutateRace:
    def test_hookstate_mutate_no_lost_update(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)

        def worker() -> None:
            with store[HookState].mutate() as hs:
                time.sleep(0.003)
                hs.fire_count += 1

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert store[HookState].get(HookState()).fire_count == 20

    def test_primitive_consumed_mutate_no_lost_update(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)

        def worker(i: int) -> None:
            with store[PrimitiveState].mutate() as ps:
                time.sleep(0.003)
                ps.consumed_for("hook").add(f"h{i}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert store[PrimitiveState].get(PrimitiveState()).consumed["hook"] == {f"h{i}" for i in range(20)}


class TestReserveThenRelease:
    def test_falsy_handler_releases_reservation(self, tmp_path: Path) -> None:
        calls = {"n": 0}

        def handler(_evt: object) -> HookResult | None:
            calls["n"] += 1
            return None if calls["n"] == 1 else HookResult(action=Action.warn, message="ok")

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse, max_fires=1),
            handler=handler,
            name="release_falsy",
        )

        assert execute_hook(entry, edit_evt(), tmp_path) is None
        r = execute_hook(entry, edit_evt(), tmp_path)
        assert r is not None and r.message == "ok"
        assert execute_hook(entry, edit_evt(), tmp_path) is None

    def test_base_exception_handler_releases_reservation(self, tmp_path: Path) -> None:
        """A handler raising ``BaseException`` (``SystemExit``/``KeyboardInterrupt``) must release
        the reserved slot and re-raise — ``run_handler`` only swallows ``Exception``, so the abnormal
        exit would otherwise leak the reservation and permanently suppress the capped hook."""
        calls = {"n": 0}

        def handler(_evt: object) -> HookResult:
            calls["n"] += 1
            if calls["n"] == 1:
                raise SystemExit("abort")
            return HookResult(action=Action.warn, message="ok")

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse, max_fires=1),
            handler=handler,
            name="release_base",
        )

        with pytest.raises(SystemExit):
            execute_hook(entry, edit_evt(), tmp_path)
        r = execute_hook(entry, edit_evt(), tmp_path)
        assert r is not None and r.message == "ok", "reservation leaked: the released slot must let the retry fire"

    def test_raising_handler_releases_reservation(self, tmp_path: Path) -> None:
        calls = {"n": 0}

        def handler(_evt: object) -> HookResult:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return HookResult(action=Action.warn, message="ok")

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse, max_fires=1),
            handler=handler,
            name="release_raise",
        )

        assert execute_hook(entry, edit_evt(), tmp_path) is None
        r = execute_hook(entry, edit_evt(), tmp_path)
        assert r is not None and r.message == "ok"


class FakeSynset:
    def __init__(self, *lemmas: str) -> None:
        self._lemmas = lemmas

    def lemmas(self) -> tuple[str, ...]:
        return self._lemmas


class FakeWn:
    """A lexicon stand-in that records how many readers are inside a query at once."""

    def __init__(self) -> None:
        self.inside = 0
        self.peak = 0

    def synsets(self, term: str, pos: str) -> list[FakeSynset]:
        self.inside += 1
        self.peak = max(self.peak, self.inside)
        time.sleep(0.002)
        self.inside -= 1
        return [FakeSynset(f"{term}_one", f"{term}_two")]


class TestWnConnectionRace:
    def test_lemma_reads_admit_one_thread_at_a_time(self) -> None:
        """Concurrent lemma reads must never overlap inside the lexicon.

        wn pools one process-global sqlite connection, and `ensure_wn_lexicon` turns off Python's
        same-thread assertion. Serialized sqlite protects the C handle but not the connection's
        prepared-statement cache, so two threads running one query is `SQLITE_MISUSE` — an
        `InterfaceError`, or a short row that reads like a real answer.

        Asserts the observed peak, not the returned lemmas: the unlocked version returns the right
        answer most of the time, so a result assertion stays green against the bug.
        """
        from captain_hook.state import NlpResources

        resources = NlpResources()
        fake = FakeWn()
        resources.__dict__["wn"] = fake

        threads = [threading.Thread(target=lambda i=i: resources.wn_lemmas((f"term{i}",), "n")) for i in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert fake.peak == 1

    def test_lemma_reads_return_every_lemma_and_the_terms(self) -> None:
        from captain_hook.state import NlpResources

        resources = NlpResources()
        resources.__dict__["wn"] = FakeWn()

        assert resources.wn_lemmas(("alpha", "beta"), "n") == {
            "alpha one",
            "alpha two",
            "beta one",
            "beta two",
        }
