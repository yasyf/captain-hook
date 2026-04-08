from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from captain_hook.dispatch import HookState, execute_hook
from captain_hook.session import SessionSlot, SessionStore
from captain_hook.types import Action, Event, HookResult, HookSpec, RegisteredHook

# ── Test models ──────────────────────────────────────────────────────────────


class MyModel(BaseModel):
    name: str
    value: int


class MyCustomModel(BaseModel):
    score: float


class AnotherModel(BaseModel):
    label: str


def make_pre_tool_event(ctx: MagicMock | None = None) -> MagicMock:
    from captain_hook.events import PreToolUseEvent

    evt = PreToolUseEvent(
        _raw={"tool_name": "Edit", "tool_input": {"file_path": "a.py", "old_string": "x", "new_string": "y"}},
        ctx=ctx or MagicMock(),
    )
    return evt


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-STATE: Class-Keyed State Store
# ═══════════════════════════════════════════════════════════════════════════════


class TestStateStore:
    # ── VAL-STATE-001: ctx.state[ModelClass] returns typed slot ───────────────

    def test_state_store_getitem_returns_session_slot(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        slot = store[MyModel]
        assert isinstance(slot, SessionSlot)

    # ── VAL-STATE-002: slot.get() returns None when no data persisted ────────

    def test_slot_get_returns_none_when_empty(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        assert store[MyModel].get() is None

    # ── VAL-STATE-003: slot.set(model) followed by slot.get() round-trips ────

    def test_slot_set_get_roundtrip(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        slot = store[MyModel]
        obj = MyModel(name="hello", value=42)
        slot.set(obj)
        assert slot.get() == obj

    # ── VAL-STATE-004: State file uses snake_case model name ─────────────────

    def test_state_file_snake_case_name(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        slot = store[MyCustomModel]
        assert slot.path is not None
        assert slot.path.name == "my_custom_model.json"

    # ── VAL-STATE-005: slot.delete() removes persisted state ─────────────────

    def test_slot_delete_removes_state(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        slot = store[MyModel]
        slot.set(MyModel(name="x", value=1))
        assert slot.get() is not None
        slot.delete()
        assert slot.get() is None

    # ── VAL-STATE-006: State store with no session dir is no-op ──────────────

    def test_state_store_none_dir_noop(self) -> None:
        store = SessionStore(None)
        slot = store[MyModel]
        assert slot.get() is None
        slot.set(MyModel(name="x", value=1))
        assert slot.get() is None

    # ── VAL-STATE-007: Multiple model classes share session directory ─────────

    def test_multiple_models_share_dir(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        store[MyModel].set(MyModel(name="a", value=1))
        store[AnotherModel].set(AnotherModel(label="b"))
        assert (tmp_path / "my_model.json").exists()
        assert (tmp_path / "another_model.json").exists()

    # ── VAL-STATE-008: Corrupted JSON file returns None on get ───────────────

    def test_corrupted_json_returns_none(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        slot = store[MyModel]
        slot.path.write_text("not valid json {{{")
        assert slot.get() is None

    # ── VAL-STATE-009: State store is properly generic with class keys ───────

    def test_generic_type(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        slot = store[MyModel]
        assert isinstance(slot, SessionSlot)
        slot.set(MyModel(name="test", value=1))
        result = slot.get()
        assert isinstance(result, MyModel)

    # ── VAL-STATE-010: SessionSlot.set creates parent directories ────────────

    def test_set_creates_parent_dirs(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested" / "dir"
        store = SessionStore(nested)
        slot = store[MyModel]
        slot.set(MyModel(name="deep", value=42))
        assert slot.get() == MyModel(name="deep", value=42)

    # ── VAL-STATE-011: SessionSlot.set with OSError silently caught ──────────

    def test_set_oserror_caught(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        store = SessionStore(tmp_path)
        slot = store[MyModel]
        monkeypatch.setattr(Path, "write_text", lambda *a, **kw: (_ for _ in ()).throw(OSError("no perms")))
        slot.set(MyModel(name="fail", value=0))

    # ── VAL-STATE-012: SessionSlot.delete on non-existent file is no-op ──────

    def test_delete_nonexistent_noop(self, tmp_path: Path) -> None:
        store = SessionStore(tmp_path)
        slot = store[MyModel]
        slot.delete()

    # ── VAL-STATE-013: slot.get(default) returns default when empty ───────────

    def test_get_default_when_empty(self, tmp_path: Path) -> None:
        assert SessionStore(tmp_path)[MyModel].get(MyModel(name="default", value=99)) == MyModel(
            name="default", value=99
        )

    # ── VAL-STATE-014: slot.get(default) returns stored value over default ────

    def test_get_default_returns_stored_over_default(self, tmp_path: Path) -> None:
        slot = SessionStore(tmp_path)[MyModel]
        slot.set(MyModel(name="stored", value=1))
        assert slot.get(MyModel(name="default", value=99)) == MyModel(name="stored", value=1)

    # ── VAL-STATE-015: slot.get(default) returns default on corrupt JSON ──────

    def test_get_default_on_corrupt_json(self, tmp_path: Path) -> None:
        slot = SessionStore(tmp_path)[MyModel]
        slot.path.write_text("not valid json {{{")
        assert slot.get(MyModel(name="fallback", value=0)) == MyModel(name="fallback", value=0)

    # ── VAL-STATE-016: slot.get() without default still returns None ──────────

    def test_get_no_default_still_returns_none(self, tmp_path: Path) -> None:
        assert SessionStore(tmp_path)[MyModel].get() is None

    # ── VAL-STATE-017: slot.get(default) with None session_dir returns default ─

    def test_get_default_with_none_session_dir(self) -> None:
        assert SessionStore(None)[MyModel].get(MyModel(name="fallback", value=7)) == MyModel(
            name="fallback", value=7
        )


# ═══════════════════════════════════════════════════════════════════════════════
# VAL-FIRE: Fire Counting
# ═══════════════════════════════════════════════════════════════════════════════


class TestFireCounting:
    # ── VAL-FIRE-001: max_fires enforcement: hook stops after N fires ────────

    def test_max_fires_enforcement(self, tmp_path: Path) -> None:
        def handler(evt: object) -> HookResult:
            return HookResult(action=Action.warn, message="fired")

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse, max_fires=2),
            handler=handler,
            name="fire_test",
        )

        r1 = execute_hook(entry, make_pre_tool_event(), tmp_path)
        assert r1 is not None
        r2 = execute_hook(entry, make_pre_tool_event(), tmp_path)
        assert r2 is not None
        r3 = execute_hook(entry, make_pre_tool_event(), tmp_path)
        assert r3 is None

    # ── VAL-FIRE-002: Fire count persists across dispatch calls via state ────

    def test_fire_count_persists_via_state(self, tmp_path: Path) -> None:
        def handler(evt: object) -> HookResult:
            return HookResult(action=Action.warn, message="fired")

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse, max_fires=2),
            handler=handler,
            name="persist_test",
        )

        execute_hook(entry, make_pre_tool_event(), tmp_path)
        hook_dir = tmp_path / "persist_test"
        store = SessionStore(hook_dir)
        state = store[HookState].get()
        assert state is not None
        assert state.fire_count == 1

    # ── VAL-FIRE-003: record_fire increments last_fired_at ───────────────────

    def test_record_fire_increments_last_fired_at(self, tmp_path: Path) -> None:
        from captain_hook.state import PrimitiveState, record_fire

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

    # ── VAL-FIRE-004: fired_this_turn True after fire in current turn ────────

    def test_fired_this_turn_true_after_fire(self, tmp_path: Path) -> None:
        from captain_hook.state import fired_this_turn, record_fire

        transcript = MagicMock()
        transcript.__len__ = lambda self: 15
        store = SessionStore(tmp_path)
        turn = MagicMock()
        turn.start_idx = 10
        ctx = MagicMock()
        ctx.s = store
        ctx.t = transcript
        ctx.turn = turn

        evt = MagicMock()
        evt.ctx = ctx

        record_fire(evt)
        assert fired_this_turn(evt) is True

    # ── VAL-FIRE-005: fired_this_turn False when no fire in current turn ─────

    def test_fired_this_turn_false_fresh(self, tmp_path: Path) -> None:
        from captain_hook.state import fired_this_turn

        store = SessionStore(tmp_path)
        turn = MagicMock()
        turn.start_idx = 10
        ctx = MagicMock()
        ctx.s = store
        ctx.turn = turn

        evt = MagicMock()
        evt.ctx = ctx

        assert fired_this_turn(evt) is False

    def test_fired_this_turn_false_prior_turn(self, tmp_path: Path) -> None:
        from captain_hook.state import fired_this_turn, record_fire

        transcript = MagicMock()
        transcript.__len__ = lambda self: 5
        store = SessionStore(tmp_path)
        turn_old = MagicMock()
        turn_old.start_idx = 0
        ctx = MagicMock()
        ctx.s = store
        ctx.t = transcript
        ctx.turn = turn_old

        evt = MagicMock()
        evt.ctx = ctx

        record_fire(evt)

        turn_new = MagicMock()
        turn_new.start_idx = 10
        ctx.turn = turn_new

        assert fired_this_turn(evt) is False

    # ── VAL-FIRE-006: Gate double-fire prevention within single turn ─────────

    def test_gate_double_fire_prevention(self, tmp_path: Path) -> None:
        from captain_hook.state import fired_this_turn, record_fire

        transcript = MagicMock()
        transcript.__len__ = lambda self: 15
        store = SessionStore(tmp_path)
        turn = MagicMock()
        turn.start_idx = 10
        ctx = MagicMock()
        ctx.s = store
        ctx.t = transcript
        ctx.turn = turn

        evt = MagicMock()
        evt.ctx = ctx

        assert fired_this_turn(evt) is False
        record_fire(evt)
        assert fired_this_turn(evt) is True

    # ── VAL-FIRE-007: max_fires=None means unlimited ────────────────────────

    def test_max_fires_none_unlimited(self, tmp_path: Path) -> None:
        def handler(evt: object) -> HookResult:
            return HookResult(action=Action.warn, message="fired")

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse, max_fires=None),
            handler=handler,
            name="unlimited_hook",
        )

        for _ in range(10):
            r = execute_hook(entry, make_pre_tool_event(), tmp_path)
            assert r is not None

    # ── VAL-FIRE-008: Default max_fires values per primitive type ────────────
    # NOTE: This assertion tests primitive defaults (nudge, gate, etc.) which
    # are implemented in the m2-nudge-gate feature. The mechanism is tested here;
    # the defaults are verified when primitives are available.

    def test_hookspec_max_fires_default_is_none(self) -> None:
        spec = HookSpec(events=Event.PreToolUse)
        assert spec.max_fires is None

    # ── VAL-FIRE-009: Explicit max_fires overrides defaults ──────────────────

    def test_explicit_max_fires_override(self) -> None:
        spec = HookSpec(events=Event.PreToolUse, max_fires=5)
        assert spec.max_fires == 5

    # ── VAL-FIRE-010: max_fires=0 means hook never fires ────────────────────

    def test_max_fires_zero_never_fires(self, tmp_path: Path) -> None:
        def handler(evt: object) -> HookResult:
            return HookResult(action=Action.warn, message="should not fire")

        entry = RegisteredHook(
            spec=HookSpec(events=Event.PreToolUse, max_fires=0),
            handler=handler,
            name="never_hook",
        )

        r = execute_hook(entry, make_pre_tool_event(), tmp_path)
        assert r is None

    # ── VAL-FIRE-011: HookState is Pydantic BaseModel with fire_count=0 ─────

    def test_hook_state_is_base_model(self) -> None:
        assert isinstance(HookState(), BaseModel)
        assert HookState().fire_count == 0

    # ── VAL-DISP-025: fire_count incremented only on non-None result ─────────

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

        r1 = execute_hook(entry, make_pre_tool_event(), tmp_path)
        assert r1 is None
        r2 = execute_hook(entry, make_pre_tool_event(), tmp_path)
        assert r2 is None
        r3 = execute_hook(entry, make_pre_tool_event(), tmp_path)
        assert r3 is not None
        r4 = execute_hook(entry, make_pre_tool_event(), tmp_path)
        assert r4 is None


# ═══════════════════════════════════════════════════════════════════════════════
# hook_name generation
# ═══════════════════════════════════════════════════════════════════════════════


class TestHookName:
    def test_hook_name_with_label(self) -> None:
        from captain_hook.state import hook_name

        name = hook_name("nudge", "My Cool Label", "some message")
        assert "nudge" in name
        assert "my_cool_label" in name

    def test_hook_name_without_label(self) -> None:
        from captain_hook.state import hook_name

        name = hook_name("gate", None, "some long message text")
        assert "gate" in name
        assert len(name) > 5

    def test_hook_name_deterministic(self) -> None:
        from captain_hook.state import hook_name

        a = hook_name("nudge", None, "same message")
        b = hook_name("nudge", None, "same message")
        assert a == b

    def test_hook_name_different_messages(self) -> None:
        from captain_hook.state import hook_name

        a = hook_name("nudge", None, "message A")
        b = hook_name("nudge", None, "message B")
        assert a != b


# ═══════════════════════════════════════════════════════════════════════════════
# text_hash
# ═══════════════════════════════════════════════════════════════════════════════


class TestTextHash:
    def test_text_hash_deterministic(self) -> None:
        from captain_hook.state import text_hash

        assert text_hash("hello") == text_hash("hello")

    def test_text_hash_different_inputs(self) -> None:
        from captain_hook.state import text_hash

        assert text_hash("hello") != text_hash("world")
