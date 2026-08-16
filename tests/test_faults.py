from __future__ import annotations

from typing import TYPE_CHECKING

from captain_hook import faults

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_record_drains_as_one_announcement_line() -> None:
    faults.record("async review dispatch", ModuleNotFoundError("No module named 'wasmtime'"))

    (line,) = faults.drain()
    assert line.startswith(faults.ANNOUNCE_PREFIX)
    assert "async review dispatch" in line
    assert "ModuleNotFoundError: No module named 'wasmtime'" in line


def test_one_failure_per_event_collapses_to_one_line() -> None:
    for _ in range(20):
        faults.record("hook announce_pr_status", RuntimeError("boom"))

    assert len(faults.drain()) == 1


def test_distinct_failures_each_survive() -> None:
    faults.record("hook a", RuntimeError("boom"))
    faults.record("hook b", RuntimeError("boom"))
    faults.record("hook a", ValueError("boom"))

    assert len(faults.drain()) == 3


def test_the_store_is_bounded() -> None:
    # A machine-wide store that drains only at a session start, fed error texts that key differently
    # every event, would otherwise announce a wall nobody reads.
    for i in range(faults.MAX_RECORDS + 10):
        faults.record("hook a", RuntimeError(f"boom {i}"))

    assert len(faults.drain()) == faults.MAX_RECORDS


def test_a_bounded_store_still_re_records_a_known_fault() -> None:
    for i in range(faults.MAX_RECORDS):
        faults.record("hook a", RuntimeError(f"boom {i}"))
    faults.record("hook a", RuntimeError("boom 0"))

    assert len(faults.drain()) == faults.MAX_RECORDS


def test_drain_clears_the_store() -> None:
    faults.record("hook a", RuntimeError("boom"))

    assert faults.drain()
    assert faults.drain() == []


def test_unwritable_state_dir_drops_the_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # record() runs inside except blocks: an unwritable state dir must not replace the failure
    # being reported with a second one.
    (blocked := tmp_path / "state").mkdir()
    blocked.chmod(0o500)
    monkeypatch.setenv("CAPTAIN_HOOK_STATE_DIR", str(blocked))
    try:
        faults.record("hook a", RuntimeError("boom"))
        assert faults.drain() == []
    finally:
        blocked.chmod(0o700)


def test_session_start_announces_recorded_faults(tmp_path: Path) -> None:
    from captain_hook.dispatch import dispatch
    from captain_hook.events import SessionStartEvent
    from captain_hook.loader import register_fault_announcements
    from captain_hook.types import Event
    from tests.helpers import build_ctx

    faults.record("async review dispatch", ModuleNotFoundError("No module named 'wasmtime'"))
    register_fault_announcements()

    evt = SessionStartEvent(_raw={"source": "startup"}, ctx=build_ctx(project_root=tmp_path))
    output = dispatch(Event.SessionStart, evt, session_dir=tmp_path / "session")

    assert output is not None
    assert "wasmtime" in output["hookSpecificOutput"]["additionalContext"]
    assert faults.drain() == []  # announced exactly once


def test_session_start_stays_silent_without_faults(tmp_path: Path) -> None:
    from captain_hook.dispatch import dispatch
    from captain_hook.events import SessionStartEvent
    from captain_hook.loader import register_fault_announcements
    from captain_hook.types import Event
    from tests.helpers import build_ctx

    register_fault_announcements()
    evt = SessionStartEvent(_raw={"source": "startup"}, ctx=build_ctx(project_root=tmp_path))

    assert dispatch(Event.SessionStart, evt, session_dir=tmp_path / "session") is None


def test_discover_registers_the_fault_announcer(tmp_path: Path) -> None:
    from captain_hook.app import _state
    from captain_hook.cli import CliState
    from captain_hook.types import Event

    (hooks := tmp_path / ".claude" / "hooks").mkdir(parents=True)
    (hooks / "__init__.py").write_text("")
    CliState(root=tmp_path, hooks=str(hooks)).discover()

    assert any(h.name == "announce_faults" and Event.SessionStart in h.spec.events for h in _state.hooks)


def test_swallowed_hook_handler_failure_is_recorded(tmp_path: Path) -> None:
    # The blindness fdebd37 survived: a handler that raises returns a clean hook response, so the
    # only trace was a daemon log line nobody reads.
    from captain_hook.app import on
    from captain_hook.dispatch import dispatch
    from captain_hook.events import SessionStartEvent
    from captain_hook.types import Event
    from tests.helpers import build_ctx

    @on(Event.SessionStart)
    def explode(evt: SessionStartEvent) -> None:
        raise ModuleNotFoundError("No module named 'wasmtime'")

    evt = SessionStartEvent(_raw={"source": "startup"}, ctx=build_ctx(project_root=tmp_path))
    assert dispatch(Event.SessionStart, evt, session_dir=tmp_path / "session") is None

    (line,) = faults.drain()
    assert "hook explode" in line and "wasmtime" in line


def test_a_record_is_only_read_by_the_project_that_owns_it() -> None:
    # The store is machine-wide but a raw exception text carries the failing project's paths, and a
    # session start injects the announcement into that session's model context.
    faults.record("hook secret_guard", RuntimeError("token leaked for /private/a"), "/private/a")

    assert faults.drain("/other/project") == []
    (line,) = faults.drain("/private/a")
    assert "/private/a" in line


def test_an_unowned_record_reaches_every_project() -> None:
    faults.record("login shell PATH probe", RuntimeError("boom"))

    (line,) = faults.drain("/any/project")
    assert "login shell PATH probe" in line


def test_first_seen_is_the_first_occurrence_not_the_latest() -> None:
    faults.record("hook flaky", RuntimeError("boom"))
    (first,) = faults.drain()
    faults.record("hook flaky", RuntimeError("boom"))
    faults.record("hook flaky", RuntimeError("boom"))

    (again,) = faults.drain()
    assert first.rsplit("first seen ", 1)[1] != ""
    assert again.count("first seen") == 1


def test_the_cap_makes_room_for_the_newest_fault() -> None:
    # A hook whose error text carries a changing path would otherwise fill the store and mask every
    # later failure, including the one worth reading.
    for i in range(faults.MAX_RECORDS + 8):
        faults.record("hook noisy", RuntimeError(f"boom {i}"))
    faults.record("async review dispatch", ModuleNotFoundError("No module named 'wasmtime'"))

    lines = faults.drain()
    assert len(lines) <= faults.MAX_RECORDS
    assert any("async review dispatch" in line for line in lines)
