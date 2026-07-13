from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel

import captain_hook
from captain_hook import app
from captain_hook.dispatch import HookState, execute_hook
from captain_hook.durable import DurableStore
from captain_hook.loader import import_pack_module
from captain_hook.session import SessionSlot, SessionStore, session_state
from captain_hook.state import (
    PrimitiveState,
    SeenKeys,
    fired_this_turn,
    hook_name,
    package_aware_stem,
    record_fire,
)
from captain_hook.types import Action, Event, HookResult, HookSpec, RegisteredHook

PACKS_DIR = Path(captain_hook.__file__).resolve().parent / "packs"


class MyModel(BaseModel):
    name: str
    value: int


class MyCustomModel(BaseModel):
    score: float


class AnotherModel(BaseModel):
    label: str


class DefaultModel(BaseModel):
    name: str = "default"
    value: int = 0


def mock_edit_event(ctx: MagicMock | None = None) -> MagicMock:
    from captain_hook.events import PreToolUseEvent

    evt = PreToolUseEvent(
        _raw={"tool_name": "Edit", "tool_input": {"file_path": "a.py", "old_string": "x", "new_string": "y"}},
        ctx=ctx or MagicMock(),
    )
    return evt


class TestStateStore:
    def test_state_store_getitem_returns_session_slot(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        slot = store[MyModel]
        assert isinstance(slot, SessionSlot)

    def test_slot_get_returns_none_when_empty(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        assert store[MyModel].get() is None

    def test_slot_set_get_roundtrip(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        slot = store[MyModel]
        obj = MyModel(name="hello", value=42)
        slot.set(obj)
        assert slot.get() == obj

    def test_state_file_snake_case_name(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        slot = store[MyCustomModel]
        assert slot.path is not None
        assert slot.path.name == "my_custom_model.json"

    def test_slot_delete_removes_state(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        slot = store[MyModel]
        slot.set(MyModel(name="x", value=1))
        assert slot.get() == MyModel(name="x", value=1)
        slot.delete()
        assert slot.get() is None

    def test_state_store_none_dir_noop(self) -> None:
        store = SessionStore(None)
        slot = store[MyModel]
        assert slot.get() is None
        slot.set(MyModel(name="x", value=1))
        assert slot.get() is None

    def test_multiple_models_share_dir(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        store[MyModel].set(MyModel(name="a", value=1))
        store[AnotherModel].set(AnotherModel(label="b"))
        assert (tmp_path / "my_model.json").exists()
        assert (tmp_path / "another_model.json").exists()

    def test_corrupted_json_returns_none(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        slot = store[MyModel]
        slot.path.write_text("not valid json {{{")
        assert slot.get() is None

    def test_stale_flat_consumed_shape_falls_back_to_default(self, tmp_path: Path) -> None:
        # A primitive_state.json written before `consumed` became a per-hook dict carries the old
        # flat list shape; SessionSlot.get must swallow the ValidationError and fall back, never
        # crash or silently coerce (sessions are throwaway state, so a fresh default is correct).
        from captain_hook.state import PrimitiveState

        store = SessionStore(tmp_path)
        slot = store[PrimitiveState]
        slot.path.write_text('{"consumed": ["abc123"], "last_fired_at": 3, "echo_lemmas": [], "echo_window_end": 0}')
        assert slot.get() is None
        assert slot.get(PrimitiveState()) == PrimitiveState()
        assert store.load(PrimitiveState) == PrimitiveState()

    def test_generic_type(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        slot = store[MyModel]
        assert isinstance(slot, SessionSlot)
        slot.set(MyModel(name="test", value=1))
        result = slot.get()
        assert isinstance(result, MyModel)

    def test_set_creates_parent_dirs(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested" / "dir"
        store = SessionStore(nested)
        slot = store[MyModel]
        slot.set(MyModel(name="deep", value=42))
        assert slot.get() == MyModel(name="deep", value=42)

    def test_set_oserror_caught(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        store = SessionStore(tmp_path)
        slot = store[MyModel]
        monkeypatch.setattr(Path, "write_text", lambda *a, **kw: (_ for _ in ()).throw(OSError("no perms")))
        slot.set(MyModel(name="fail", value=0))

    def test_delete_nonexistent_noop(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        slot = store[MyModel]
        slot.delete()

    def test_get_default_when_empty(self, tmp_path: Path) -> None:
        assert SessionStore(tmp_path)[MyModel].get(MyModel(name="default", value=99)) == MyModel(
            name="default", value=99
        )

    def test_get_default_returns_stored_over_default(self, tmp_path: Path) -> None:
        slot = SessionStore(tmp_path)[MyModel]
        slot.set(MyModel(name="stored", value=1))
        assert slot.get(MyModel(name="default", value=99)) == MyModel(name="stored", value=1)

    def test_get_default_on_corrupt_json(self, tmp_path: Path) -> None:
        slot = SessionStore(tmp_path)[MyModel]
        slot.path.write_text("not valid json {{{")
        assert slot.get(MyModel(name="fallback", value=0)) == MyModel(name="fallback", value=0)

    def test_get_no_default_still_returns_none(self, tmp_path: Path) -> None:
        assert SessionStore(tmp_path)[MyModel].get() is None

    def test_get_default_with_none_session_dir(self) -> None:
        assert SessionStore(None)[MyModel].get(MyModel(name="fallback", value=7)) == MyModel(name="fallback", value=7)

    def test_load_returns_fresh_default_when_empty(self, tmp_path: Path) -> None:
        assert SessionStore(tmp_path).load(DefaultModel) == DefaultModel()

    def test_load_returns_stored_instance(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        store[DefaultModel].set(DefaultModel(name="x", value=5))
        assert store.load(DefaultModel) == DefaultModel(name="x", value=5)


class TestFireCounting:
    def test_max_fires_enforcement(self, tmp_path: Path) -> None:
        def handler(evt: object) -> HookResult:
            return HookResult(action=Action.warn, message="fired")

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse, max_fires=2),
            handler=handler,
            name="fire_test",
        )

        r1 = execute_hook(entry, mock_edit_event(), tmp_path)
        assert r1 is not None and r1.action == Action.warn
        r2 = execute_hook(entry, mock_edit_event(), tmp_path)
        assert r2 is not None and r2.action == Action.warn
        r3 = execute_hook(entry, mock_edit_event(), tmp_path)
        assert r3 is None

    def test_fire_count_persists_via_state(self, tmp_path: Path) -> None:
        def handler(evt: object) -> HookResult:
            return HookResult(action=Action.warn, message="fired")

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse, max_fires=2),
            handler=handler,
            name="persist_test",
        )

        execute_hook(entry, mock_edit_event(), tmp_path)
        hook_dir = tmp_path / entry.state_key / "main"
        store = SessionStore(hook_dir)
        state = store[HookState].get()
        assert state is not None
        assert state.fire_count == 1

    def test_record_fire_increments_last_fired_at(self, tmp_path: Path) -> None:

        transcript = MagicMock()
        transcript.__len__ = lambda self: 10
        store = SessionStore(tmp_path)
        ctx = MagicMock()
        ctx.s = store
        ctx.t = transcript

        evt = MagicMock()
        evt.ctx = ctx

        record_fire(evt)
        ps = store[PrimitiveState].get()
        assert ps is not None
        assert ps.last_fired_at == 10

    def test_fired_this_turn_true_after_fire(self, tmp_path: Path) -> None:

        transcript = MagicMock()
        transcript.__len__ = lambda self: 15
        store = SessionStore(tmp_path)
        turn = MagicMock()
        turn.__len__ = lambda self: 5
        ctx = MagicMock()
        ctx.s = store
        ctx.t = transcript
        ctx.turn = turn

        evt = MagicMock()
        evt.ctx = ctx

        record_fire(evt)
        assert fired_this_turn(evt) is True

    def test_fired_this_turn_false_fresh(self, tmp_path: Path) -> None:

        store = SessionStore(tmp_path)
        turn = MagicMock()
        turn.__len__ = lambda self: 5
        ctx = MagicMock()
        ctx.s = store
        ctx.turn = turn

        evt = MagicMock()
        evt.ctx = ctx

        assert fired_this_turn(evt) is False

    def test_fired_this_turn_false_prior_turn(self, tmp_path: Path) -> None:

        transcript = MagicMock()
        transcript.__len__ = lambda self: 5
        store = SessionStore(tmp_path)
        turn_old = MagicMock()
        turn_old.__len__ = lambda self: 5
        ctx = MagicMock()
        ctx.s = store
        ctx.t = transcript
        ctx.turn = turn_old

        evt = MagicMock()
        evt.ctx = ctx

        record_fire(evt)

        transcript.__len__ = lambda self: 15
        turn_new = MagicMock()
        turn_new.__len__ = lambda self: 5
        ctx.turn = turn_new

        assert fired_this_turn(evt) is False

    def test_gate_double_fire_prevention(self, tmp_path: Path) -> None:

        transcript = MagicMock()
        transcript.__len__ = lambda self: 15
        store = SessionStore(tmp_path)
        turn = MagicMock()
        turn.__len__ = lambda self: 5
        ctx = MagicMock()
        ctx.s = store
        ctx.t = transcript
        ctx.turn = turn

        evt = MagicMock()
        evt.ctx = ctx

        assert fired_this_turn(evt) is False
        record_fire(evt)
        assert fired_this_turn(evt) is True

    def test_max_fires_none_unlimited(self, tmp_path: Path) -> None:
        def handler(evt: object) -> HookResult:
            return HookResult(action=Action.warn, message="fired")

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse, max_fires=None),
            handler=handler,
            name="unlimited_hook",
        )

        for _ in range(10):
            r = execute_hook(entry, mock_edit_event(), tmp_path)
            assert r is not None and r.action == Action.warn

    # NOTE: This assertion tests primitive defaults (nudge, gate, etc.) which
    # are implemented in the m2-nudge-gate feature. The mechanism is tested here;
    # the defaults are verified when primitives are available.

    @pytest.mark.parametrize(
        ("spec", "max_fires"),
        [
            pytest.param(HookSpec(events=Event.PreToolUse), None, id="hookspec_max_fires_default_is_none"),
            pytest.param(HookSpec(events=Event.PreToolUse, max_fires=5), 5, id="explicit_max_fires_override"),
        ],
    )
    def test_max_fires_spec_value(self, spec: HookSpec, max_fires: int | None) -> None:
        assert spec.max_fires == max_fires

    def test_max_fires_zero_never_fires(self, tmp_path: Path) -> None:
        def handler(evt: object) -> HookResult:
            return HookResult(action=Action.warn, message="should not fire")

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse, max_fires=0),
            handler=handler,
            name="never_hook",
        )

        r = execute_hook(entry, mock_edit_event(), tmp_path)
        assert r is None

    def test_hook_state_is_base_model(self) -> None:
        assert isinstance(HookState(), BaseModel)
        assert HookState().fire_count == 0

    def test_fire_count_only_on_non_none(self, tmp_path: Path) -> None:
        call_count = 0

        def handler(evt: object) -> HookResult | None:
            nonlocal call_count
            call_count += 1
            return None if call_count <= 2 else HookResult(action=Action.warn, message="now")

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse, max_fires=1),
            handler=handler,
            name="counter_hook",
        )

        r1 = execute_hook(entry, mock_edit_event(), tmp_path)
        assert r1 is None
        r2 = execute_hook(entry, mock_edit_event(), tmp_path)
        assert r2 is None
        r3 = execute_hook(entry, mock_edit_event(), tmp_path)
        assert r3 is not None and r3.action == Action.warn and r3.message == "now"
        r4 = execute_hook(entry, mock_edit_event(), tmp_path)
        assert r4 is None


# hook_name generation


class TestHookName:
    def test_hook_name_with_label(self) -> None:

        name = hook_name("nudge", "My Cool Label", "some message")
        assert "nudge" in name
        assert "my_cool_label" in name

    def test_hook_name_without_label(self) -> None:

        name = hook_name("gate", None, "some long message text")
        assert "gate" in name
        assert len(name) > 5

    def test_pack_module_hook_name_is_pack_qualified(self, isolate_modules: None) -> None:
        import_pack_module("captain_hook._packs.general.docs", PACKS_DIR / "general" / "docs.py")
        names = sorted(entry.name for entry in app._state.hooks)
        assert re.fullmatch(r"general\.docs:llm_gate_[0-9a-f]{8}", names[0])
        assert re.fullmatch(r"general\.docs:nudge_[0-9a-f]{8}", names[1])

    def test_pack_namespace_module_is_pack_qualified_off_disk(self, tmp_path: Path, isolate_modules: None) -> None:
        # An external pack module loaded from the cache (off PACKS_DIR) is pack-qualified by its
        # captain_hook._packs.<pack>.<stem> __name__, not the bare file stem package_aware_stem would give.
        (tmp_path / "my_hook.py").write_text("from captain_hook import nudge\n\nnudge('remember the tests')\n")
        import_pack_module("captain_hook._packs.local.my_hook", tmp_path / "my_hook.py")
        [entry] = app._state.hooks
        assert re.fullmatch(r"local\.my_hook:nudge_[0-9a-f]{8}", entry.name)


class TestPackageAwareStem:
    def test_builtin_pack_file_is_pack_qualified(self) -> None:
        assert package_aware_stem(PACKS_DIR / "general" / "docs.py") == "general.docs"

    def test_framework_file_keeps_bare_stem(self) -> None:
        assert package_aware_stem(PACKS_DIR.parent / "primitives" / "nudge.py") == "nudge"

    def test_loose_file_keeps_bare_stem(self, tmp_path: Path) -> None:
        assert package_aware_stem(tmp_path / "guard.py") == "guard"

    def test_packaged_user_file_is_package_qualified(self, tmp_path: Path) -> None:
        pkg = tmp_path / "hooks"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("x = 1\n")
        assert package_aware_stem(pkg / "tasks.py") == "hooks.tasks"


class TestSessionStateTracking:
    def test_decorator_registers_class_and_returns_unchanged(self) -> None:
        before = set(SessionStore.tracked_models())

        @session_state
        class Tracked(BaseModel):
            x: int = 0

        assert Tracked in SessionStore.tracked_models()
        assert Tracked not in before
        assert Tracked.model_validate({"x": 1}).x == 1

    def test_tracked_models_returns_immutable_view(self) -> None:
        snapshot = SessionStore.tracked_models()
        assert isinstance(snapshot, tuple)
        with pytest.raises((TypeError, AttributeError)):
            snapshot.append(MyModel)  # type: ignore[attr-defined]

    def test_builtin_models_auto_tracked(self) -> None:
        models = SessionStore.tracked_models()
        assert HookState in models
        assert PrimitiveState in models

    def test_tracked_paths_returns_paths_for_session_dir(self, tmp_path: Path) -> None:
        @session_state
        class Tracked(BaseModel):
            v: int = 0

        store = SessionStore(tmp_path)
        paths = store.tracked_paths()
        assert "Tracked" in paths
        assert paths["Tracked"] == store[Tracked].path

    def test_tracked_paths_omits_models_without_session_dir(self) -> None:
        @session_state
        class Tracked(BaseModel):
            v: int = 0

        assert "Tracked" not in SessionStore(None).tracked_paths()

    def test_tracked_paths_keyed_by_class_name(self, tmp_path: Path) -> None:
        @session_state
        class CustomScope(BaseModel):
            n: int = 0

        paths = SessionStore(tmp_path).tracked_paths()
        assert "CustomScope" in paths
        assert paths["CustomScope"].name == "custom_scope.json"

    def test_no_test_local_models_leak_into_tracked_registries(self) -> None:
        # The autouse isolate_tracked_models fixture snapshots and restores TRACKED per test, so a
        # throwaway model defined inside a test body never survives into another test's registry.
        leaked = [
            f"{m.__module__}.{m.__qualname__}"
            for m in (*SessionStore.TRACKED, *DurableStore.TRACKED)
            if "<locals>" in m.__qualname__
        ]
        assert leaked == [], f"test-local models leaked into TRACKED: {leaked}"


class TestSeenKeys:
    def test_seen_keys_is_tracked(self) -> None:
        assert SeenKeys in SessionStore.tracked_models()

    def test_once_first_sight_true_then_false(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        assert store.once("k") is True
        assert store.once("k") is False
        assert store.once("k") is False

    def test_once_distinct_keys_each_first_sight_true(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        assert store.once("a") is True
        assert store.once("b") is True
        assert store.once("a") is False
        assert store.once("b") is False

    def test_once_scopes_are_independent(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        assert store.once("k", scope="a") is True
        assert store.once("k", scope="b") is True
        assert store.once("k", scope="a") is False
        assert store.once("k", scope="b") is False

    def test_once_records_under_scope(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        store.once("k", scope="a")
        store.once("j", scope="a")
        assert store.load(SeenKeys).seen == {"a": ["k", "j"]}

    def test_once_already_seen_no_rewrite(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        store.once("k")
        path = store[SeenKeys].path
        assert path is not None
        mtime_before = path.stat().st_mtime_ns
        assert store.once("k") is False
        assert path.stat().st_mtime_ns == mtime_before

    def test_unseen_returns_fresh_subset_and_marks_all(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        assert store.unseen(["a", "b"]) == ["a", "b"]
        assert store.unseen(["a", "b", "c"]) == ["c"]
        assert store.unseen(["a", "b", "c"]) == []

    def test_unseen_dedups_within_batch_order_preserving(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        assert store.unseen(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]
        assert store.load(SeenKeys).seen == {"": ["b", "a", "c"]}

    def test_unseen_marks_all_in_one_persist(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        with patch("captain_hook.session.os.replace", wraps=os.replace) as persist:
            store.unseen(["x", "y", "z"])
        assert persist.call_count == 1
        assert store.load(SeenKeys).seen == {"": ["x", "y", "z"]}
        assert store.unseen(["x", "y", "z"]) == []

    def test_unseen_empty_returns_empty_and_no_write(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        assert store.unseen([]) == []
        assert store[SeenKeys].path is not None
        assert not store[SeenKeys].path.exists()

    def test_unseen_no_fresh_keys_no_write(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        store.unseen(["a"])
        path = store[SeenKeys].path
        assert path is not None
        mtime_before = path.stat().st_mtime_ns
        assert store.unseen(["a"]) == []
        assert path.stat().st_mtime_ns == mtime_before

    def test_unseen_scopes_are_independent(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        assert store.unseen(["k"], scope="a") == ["k"]
        assert store.unseen(["k"], scope="b") == ["k"]
        assert store.unseen(["k"], scope="a") == []

    def test_persistence_round_trip_fresh_store(self, tmp_path: Path) -> None:
        first = SessionStore(tmp_path)
        first.once("k", scope="s")
        first.unseen(["a", "b"], scope="s")

        second = SessionStore(tmp_path)
        assert second.once("k", scope="s") is False
        assert second.unseen(["a", "b", "c"], scope="s") == ["c"]
        assert second.load(SeenKeys).seen == {"s": ["k", "a", "b", "c"]}
