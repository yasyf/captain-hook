from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

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
