from __future__ import annotations

import threading
from collections import Counter
from contextvars import copy_context
from pathlib import Path

import filelock
import pytest
from pydantic import BaseModel, Field

from captain_hook import app, session
from captain_hook.dispatch import dispatch
from captain_hook.session import ABSENT, SessionSlot, SessionStore, batched_state, merge_json
from captain_hook.state import HookState, PrimitiveState
from captain_hook.types import Action, Event, HookResult, HookSpec, RegisteredHook
from tests.helpers import make_ctx, make_pre_tool_event


class Ledger(BaseModel):
    count: int = 0
    seen: dict[str, int] = Field(default_factory=dict)
    names: list[str] = Field(default_factory=list)


def firing_hook(index: int, session_dir: Path) -> RegisteredHook:
    def handler(_evt: object) -> HookResult:
        with SessionStore(session_dir)[PrimitiveState].mutate() as state:
            state.consumed_for(f"hook_{index}").add("signal")
        return HookResult(action=Action.warn, message=f"fired {index}")

    return RegisteredHook(
        spec=HookSpec(events=Event.PreToolUse, max_fires=1),
        handler=handler,
        name=f"hook_{index}",
        source_file=f"/hooks/hook_{index}.py",
    )


@pytest.mark.parametrize("hooks", [1, 40])
def test_a_dispatch_writes_each_file_once_however_many_hooks_fired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hooks: int
) -> None:
    writes: Counter[str] = Counter()
    write = session.atomic_write

    def counted(path: Path, text: str) -> None:
        writes[path.name] += 1
        write(path, text)

    monkeypatch.setattr(session, "atomic_write", counted)
    state = app.State()
    state.hooks.extend(firing_hook(index, tmp_path) for index in range(hooks))
    with app.use_state(state):
        dispatch(Event.PreToolUse, make_pre_tool_event(ctx=make_ctx(tmp_path)), session_dir=tmp_path)

    assert writes == {"hook_state.json": 1, "primitive_state.json": 1}
    store = SessionStore(tmp_path)
    assert len(store[HookState].get(HookState()).fires) == hooks
    assert len(store[PrimitiveState].get(PrimitiveState()).consumed) == hooks


def test_a_dispatch_sees_its_own_staged_writes_before_the_commit(tmp_path: Path) -> None:
    slot = SessionSlot(tmp_path, Ledger)
    with batched_state():
        with slot.mutate() as ledger:
            ledger.count = 1
        assert not (tmp_path / "ledger.json").exists()
        assert slot.get(Ledger()).count == 1
        with slot.mutate() as ledger:
            ledger.count += 1
        assert slot.get(Ledger()).count == 2
    assert SessionSlot(tmp_path, Ledger).get(Ledger()).count == 2


def test_the_commit_merges_with_what_another_process_committed_meanwhile(tmp_path: Path) -> None:
    slot = SessionSlot(tmp_path, Ledger)
    slot.set(Ledger(seen={"base": 1}, names=["base"]))

    def other_process() -> None:
        with SessionSlot(tmp_path, Ledger).mutate() as ledger:
            ledger.seen["theirs"] = 1
            ledger.names.append("theirs")

    with batched_state():
        with slot.mutate() as ledger:
            ledger.seen["ours"] = 1
            ledger.names.append("ours")
        thread = threading.Thread(target=other_process)
        thread.start()
        thread.join()

    merged = slot.get(Ledger())
    assert merged.seen == {"base": 1, "ours": 1, "theirs": 1}
    assert merged.names == ["base", "theirs", "ours"]


def test_staged_set_and_delete_replace_whatever_is_on_disk_at_the_commit(tmp_path: Path) -> None:
    slot = SessionSlot(tmp_path, Ledger)
    slot.set(Ledger(count=1))
    with batched_state():
        slot.set(Ledger(count=5))
        assert (tmp_path / "ledger.json").read_text() == Ledger(count=1).model_dump_json()
    assert slot.get(Ledger()).count == 5
    with batched_state():
        slot.delete()
        assert slot.get() is None
        assert (tmp_path / "ledger.json").exists()
    assert not (tmp_path / "ledger.json").exists()


def test_an_unchanged_mutation_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    slot = SessionSlot(tmp_path, Ledger)
    slot.set(Ledger(count=1))

    def refuse(path: Path, text: str) -> None:
        raise AssertionError(f"wrote {path}: {text}")

    monkeypatch.setattr(session, "atomic_write", refuse)
    with batched_state(), slot.mutate() as ledger:
        assert ledger.count == 1


def test_a_hook_abandoned_past_the_commit_still_lands_its_write(tmp_path: Path) -> None:
    slot = SessionSlot(tmp_path, Ledger)
    with batched_state():
        with slot.mutate() as ledger:
            ledger.seen["on time"] = 1
        abandoned = copy_context()

    def late() -> None:
        with slot.mutate() as ledger:
            ledger.seen["late"] = 1

    abandoned.run(late)
    assert slot.get(Ledger()).seen == {"on time": 1, "late": 1}


def test_a_hook_racing_the_slot_in_its_own_dispatch_can_still_ask_not_to_wait(tmp_path: Path) -> None:
    slot = SessionSlot(tmp_path, Ledger)
    with batched_state(), slot.mutate():
        contender = copy_context()
        raised: list[BaseException] = []

        def try_now() -> None:
            try:
                with slot.mutate(timeout=0):
                    pass
            except filelock.Timeout as exc:
                raised.append(exc)

        thread = threading.Thread(target=contender.run, args=(try_now,))
        thread.start()
        thread.join()
    assert len(raised) == 1


def test_a_locked_slot_file_drops_the_write_instead_of_raising_into_the_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session, "COMMIT_LOCK_SECONDS", 0.05)
    slot = SessionSlot(tmp_path, Ledger)
    slot.set(Ledger(count=1))
    with filelock.FileLock(str(tmp_path / "ledger.json.lock")), batched_state(), slot.mutate() as ledger:
        ledger.count = 2
    assert slot.get(Ledger()).count == 1


@pytest.mark.parametrize(
    ("base", "ours", "theirs", "merged"),
    [
        ({"a": 1}, {"a": 1}, {"a": 2}, {"a": 2}),
        ({"a": 1}, {"a": 2}, {"a": 1}, {"a": 2}),
        ({"a": 1}, {"a": 2}, {"a": 3}, {"a": 2}),
        ({}, {"a": 1}, {"b": 2}, {"a": 1, "b": 2}),
        ({"a": 1, "b": 1}, {"b": 1}, {"a": 1, "b": 2}, {"b": 2}),
        ({"a": {"x": 1}}, {"a": {"x": 1, "y": 2}}, {"a": {"x": 1, "z": 3}}, {"a": {"x": 1, "z": 3, "y": 2}}),
        ([1, 2], [1, 2, 3], [1, 2, 4], [1, 2, 4, 3]),
        ([1, 2], [2], [1, 2, 3], [2, 3]),
        (ABSENT, [1], [2], [2, 1]),
    ],
)
def test_merge_keeps_both_writers_changes_and_ours_on_a_conflict(
    base: object, ours: object, theirs: object, merged: object
) -> None:
    assert merge_json(base, ours, theirs) == merged
