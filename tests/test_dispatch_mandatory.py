from __future__ import annotations

import json
import shutil
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from captain_hook import app
from captain_hook import dispatch as dispatch_module
from captain_hook.app import on
from captain_hook.cli import dispatch_event
from captain_hook.context import HookContext
from captain_hook.dispatch import (
    SYNC_DEADLINE_MARGIN_SECONDS,
    MandatoryBound,
    MandatoryDeadlinePassed,
    completion_key,
    dispatch,
)
from captain_hook.events import PreToolUseEvent
from captain_hook.loader import discover_pack
from captain_hook.session import SessionStore
from captain_hook.snapshots.client import CURRENT_CLIENT, MANDATORY_WORK_SECONDS, EvidenceIncomplete, SnapshotClient
from captain_hook.transcripts import lazy_transcript
from captain_hook.types import CustomCondition, Event, SkipPermissions
from captain_hook.util import proc, reqenv
from captain_hook.worker.runtime import ProductRuntime
from tests.test_dispatch import FROZEN_NOW, bounded_request, pinch_pool
from tests.test_pack_sessions import HOOK_SHELL, MAC, PACKS_DIR
from tests.test_worker_runtime import FakeRegistry, request

DESTRUCTIVE = {"session_id": "s1", "cwd": "/w", "tool_name": "Bash", "tool_input": {"command": "pkill -x sleep"}}
HEALTHY = {"session_id": "s1", "cwd": "/w", "tool_name": "Bash", "tool_input": {"command": "orca terminal list --json"}}


def decision(envelope: dict[str, Any] | None) -> str | None:
    if envelope is None:
        return None
    return envelope["hookSpecificOutput"].get("permissionDecision")


def reason(envelope: dict[str, Any] | None) -> str:
    assert envelope is not None
    return envelope["hookSpecificOutput"]["permissionDecisionReason"]


def note(envelope: dict[str, Any]) -> str:
    return envelope["systemMessage"]


def replied(response: Any) -> dict[str, Any]:
    return json.loads(response.stdout.splitlines()[-1])


def paused_before_publish(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    reached, release = threading.Event(), threading.Event()
    original = reqenv.publish

    def publish(fn: Callable[[], Any]) -> Any:
        reached.set()
        assert release.wait(timeout=5.0)
        return original(fn)

    monkeypatch.setattr(reqenv, "publish", publish)
    return reached, release


def closing_once(
    monkeypatch: pytest.MonkeyPatch, reached: threading.Event, *, then: Callable[[], None] | None = None
) -> None:
    original = dispatch_module.wait

    def wait_for_the_barrier(futures: Any, timeout: float | None = None) -> Any:
        assert reached.wait(timeout=5.0)
        if then is not None:
            then()
        return original(futures, timeout=0)

    monkeypatch.setattr(dispatch_module, "wait", wait_for_the_barrier)


def held_under_the_closure(monkeypatch: pytest.MonkeyPatch, name: str) -> tuple[threading.Event, threading.Event]:
    reached, release = threading.Event(), threading.Event()
    original = reqenv.note_mandatory_completed

    def note_then_hold(key: str) -> None:
        original(key)
        if key.startswith(f"{name}."):
            reached.set()
            assert release.wait(timeout=5.0)

    monkeypatch.setattr(reqenv, "note_mandatory_completed", note_then_hold)
    return reached, release


def held_after_settling(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    reached, release = threading.Event(), threading.Event()
    original = reqenv.Cutoff.publish

    def publish_then_hold[T](self: reqenv.Cutoff, fn: Callable[[], T]) -> T:
        def settle_then_hold() -> T:
            settled = fn()
            reached.set()
            assert release.wait(timeout=5.0)
            return settled

        return original(self, settle_then_hold)

    monkeypatch.setattr(reqenv.Cutoff, "publish", publish_then_hold)
    return reached, release


def held_at_closure_exit(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    reached, release = threading.Event(), threading.Event()
    original = reqenv.Cutoff.__init__

    def bound_with_a_held_exit(self: reqenv.Cutoff, deadline_unix_ms: int | None = None) -> None:
        original(self, deadline_unix_ms)
        if deadline_unix_ms is not None:
            self._closure = ClosureHeldOnExit(self._closure, reached, release)

    monkeypatch.setattr(reqenv.Cutoff, "__init__", bound_with_a_held_exit)
    return reached, release


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reqenv, "time", SimpleNamespace(time=lambda: FROZEN_NOW))


@pytest.fixture
def general_pack(isolate_modules: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc, "process_table", lambda **kw: MAC)
    monkeypatch.setattr("captain_hook.heartbeat.record_heartbeat", lambda *args: None)
    discover_pack("general", PACKS_DIR / "general" / "hooks")


def session_guards() -> list[str]:
    names = [hook.name for hook in app.get_mandatory_hooks(Event.PreToolUse)]
    assert {"kill_unverified_pid", "signal_by_criteria", "stop_unverified_task"} <= set(names)
    return names


def completed_names(overrides: reqenv.RequestOverrides) -> set[str]:
    return {key.split(".")[0] for key in overrides.mandatory_completed}


@pytest.fixture
def ticking_clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    """A deadline clock held at :data:`FROZEN_NOW` that a hook can advance by hand."""
    now = [FROZEN_NOW]
    monkeypatch.setattr(reqenv, "time", SimpleNamespace(time=lambda: now[0]))

    def advance(seconds: float) -> None:
        now[0] += seconds

    return advance


def inside_margin() -> reqenv.RequestOverrides:
    return replace(bounded_request(-1.0), client_ppid=HOOK_SHELL)


def outside_margin(seconds: float = 30.0) -> reqenv.RequestOverrides:
    return replace(bounded_request(seconds), client_ppid=HOOK_SHELL)


def past_deadline() -> reqenv.RequestOverrides:
    return replace(bounded_request(-SYNC_DEADLINE_MARGIN_SECONDS - 1.0), client_ppid=HOOK_SHELL)


class Exhausted(CustomCondition):
    def __init__(self, status: str = "deadline") -> None:
        self.status = status

    def check(self, evt: Any) -> bool:
        raise EvidenceIncomplete(self.status, "foreground transcript deadline exhausted")


@dataclass(frozen=True, slots=True)
class ClosureHeldOnExit:
    lock: threading.Lock
    reached: threading.Event
    release_gate: threading.Event

    def acquire(self, *, timeout: float = -1) -> bool:
        return self.lock.acquire(timeout=timeout)

    def release(self) -> None:
        self.lock.release()

    def __enter__(self) -> bool:
        return self.lock.acquire()

    def __exit__(self, *exc: object) -> None:
        self.reached.set()
        assert self.release_gate.wait(timeout=5.0)
        self.lock.release()


class TestDispatchEvent:
    def test_the_guard_denies_inside_the_margin_while_advisory_hooks_are_skipped(
        self, general_pack: None, frozen_clock: None, tmp_path: Path
    ) -> None:
        ran: list[str] = []

        @on(Event.PreToolUse)
        def advisory(evt: Any) -> None:
            ran.append("advisory")

        overrides = inside_margin()
        with reqenv.use_request(overrides):
            envelope, _ = dispatch_event(tmp_path, Event.PreToolUse, DESTRUCTIVE, session_dir=None)
        assert decision(envelope) == "deny"
        assert "pkill" in reason(envelope)
        assert ran == []
        assert completed_names(overrides) == set(session_guards())

    def test_a_healthy_guarded_call_records_completion_without_an_envelope(
        self, general_pack: None, frozen_clock: None, tmp_path: Path
    ) -> None:
        overrides = inside_margin()
        with reqenv.use_request(overrides):
            envelope, _ = dispatch_event(tmp_path, Event.PreToolUse, HEALTHY, session_dir=None)
        assert envelope is None
        assert completed_names(overrides) == set(session_guards())

    def test_an_advisory_deny_survives_the_guards_allow(
        self, general_pack: None, frozen_clock: None, tmp_path: Path
    ) -> None:
        @on(Event.PreToolUse)
        def advisory(evt: Any) -> Any:
            return evt.block("advisory says no")

        overrides = outside_margin()
        with reqenv.use_request(overrides):
            envelope, _ = dispatch_event(tmp_path, Event.PreToolUse, HEALTHY, session_dir=None)
        assert decision(envelope) == "deny"
        assert reason(envelope) == "advisory says no"
        assert completed_names(overrides) == set(session_guards())

    def test_the_guard_runs_with_no_fanout_permit_left(
        self, general_pack: None, frozen_clock: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ran: list[str] = []

        @on(Event.PreToolUse)
        def advisory(evt: Any) -> None:
            ran.append("advisory")

        overrides = outside_margin(0.2)
        with pinch_pool(monkeypatch, 0), reqenv.use_request(overrides):
            envelope, _ = dispatch_event(tmp_path, Event.PreToolUse, DESTRUCTIVE, session_dir=None)
        assert decision(envelope) == "deny"
        assert ran == []
        assert completed_names(overrides) == set(session_guards())


@pytest.mark.usefixtures("frozen_clock")
class TestMandatoryPhase:
    def test_a_fail_open_evidence_gap_records_no_completion(self, tmp_path: Path) -> None:
        @on(Event.PreToolUse | Event.PermissionRequest, only_if=[Exhausted()], mandatory=True)
        def starved(evt: Any) -> None:
            raise AssertionError("handler must not run")

        evt = PreToolUseEvent(_raw=DESTRUCTIVE, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))
        overrides = outside_margin()
        with reqenv.use_request(overrides):
            assert dispatch(Event.PreToolUse, evt) is None
        assert overrides.mandatory_completed == []
        assert overrides.evidence_gaps == []

    def test_a_mandatory_hook_reads_evidence_past_the_foreground_budget_and_hands_back_the_exhausted_one(
        self, tmp_path: Path
    ) -> None:
        seen: list[int | None] = []

        class Reads(CustomCondition):
            def check(self, evt: Any) -> bool:
                client = CURRENT_CLIENT.get()
                assert client is not None
                seen.append(client.foreground_deadline_unix_ms)
                return False

        @on(Event.PreToolUse, only_if=[Reads()], mandatory=True)
        def gate(evt: Any) -> None:
            raise AssertionError("handler must not run")

        client = SnapshotClient(lambda _: pytest.fail("no snapshot call expected"), foreground_seconds=0.75)
        client.foreground_deadline_unix_ms = 1
        evt = PreToolUseEvent(_raw=DESTRUCTIVE, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))
        started = time.time()
        token = CURRENT_CLIENT.set(client)
        try:
            with reqenv.use_request(outside_margin()):
                dispatch(Event.PreToolUse, evt)
        finally:
            CURRENT_CLIENT.reset(token)
        assert seen[0] is not None and seen[0] >= int((started + MANDATORY_WORK_SECONDS) * 1000)
        assert client.foreground_deadline_unix_ms == 1

    def test_invalid_evidence_is_the_whole_events_error(self, tmp_path: Path) -> None:
        @on(Event.PreToolUse, only_if=[Exhausted("invalid_request")], mandatory=True)
        def starved(evt: Any) -> None:
            raise AssertionError("handler must not run")

        evt = PreToolUseEvent(_raw=DESTRUCTIVE, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))
        with reqenv.use_request(outside_margin()), pytest.raises(EvidenceIncomplete):
            dispatch(Event.PreToolUse, evt)
        assert evt.ctx.transcript.pins.pending == 0

    def test_a_non_match_still_counts_as_completion(self, tmp_path: Path) -> None:
        class Never(CustomCondition):
            def check(self, evt: Any) -> bool:
                return False

        @on(Event.PreToolUse, only_if=[Never()], mandatory=True)
        def idle(evt: Any) -> None:
            raise AssertionError("handler must not run")

        evt = PreToolUseEvent(_raw=DESTRUCTIVE, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))
        overrides = outside_margin()
        with reqenv.use_request(overrides):
            assert dispatch(Event.PreToolUse, evt) is None
        assert overrides.mandatory_completed == [completion_key(app._state.hooks[0], 0)]

    def test_a_mandatory_block_dooms_only_the_handlers_registered_after_it(self, tmp_path: Path) -> None:
        ran: list[str] = []

        @on(Event.PreToolUse)
        def earlier(evt: Any) -> None:
            ran.append("earlier")

        @on(Event.PreToolUse, mandatory=True)
        def guard(evt: Any) -> Any:
            return evt.block("guard says no")

        @on(Event.PreToolUse)
        def later(evt: Any) -> None:
            ran.append("later")

        @on(Event.PreToolUse, advisory_on_deny=True)
        def rider(evt: Any) -> Any:
            ran.append("rider")
            return evt.warn("mind the rider")

        evt = PreToolUseEvent(_raw=DESTRUCTIVE, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))
        with reqenv.use_request(outside_margin()):
            envelope = dispatch(Event.PreToolUse, evt)
        assert decision(envelope) == "deny"
        assert reason(envelope).startswith("guard says no")
        assert "mind the rider" in reason(envelope)
        assert sorted(ran) == ["earlier", "rider"]
        assert evt.ctx.transcript.pins.pending == 0

    @pytest.mark.parametrize("mandatory", [False, True], ids=["advisory guard", "mandatory guard"])
    def test_an_advisory_registered_before_the_guard_keeps_the_deny_reason(
        self, tmp_path: Path, mandatory: bool
    ) -> None:
        ran: list[str] = []

        @on(Event.PreToolUse)
        def earlier(evt: Any) -> Any:
            ran.append("earlier")
            return evt.block("EARLIER advisory deny")

        @on(Event.PreToolUse, mandatory=mandatory)
        def guard(evt: Any) -> Any:
            ran.append("guard")
            return evt.block("GUARD deny")

        @on(Event.PreToolUse)
        def later(evt: Any) -> None:
            ran.append("later")

        evt = PreToolUseEvent(_raw=DESTRUCTIVE, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))
        with reqenv.use_request(outside_margin()):
            envelope = dispatch(Event.PreToolUse, evt)
        assert decision(envelope) == "deny"
        assert reason(envelope) == "EARLIER advisory deny"
        assert "earlier" in ran
        if mandatory:
            assert ran == ["guard", "earlier"]

    def test_a_raising_mandatory_handler_is_the_whole_events_error(self, tmp_path: Path) -> None:
        @on(Event.PreToolUse, mandatory=True)
        def broken(evt: Any) -> None:
            raise RuntimeError("guard crashed")

        evt = PreToolUseEvent(_raw=DESTRUCTIVE, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))
        overrides = outside_margin()
        with reqenv.use_request(overrides), pytest.raises(RuntimeError, match="guard crashed"):
            dispatch(Event.PreToolUse, evt)
        assert overrides.mandatory_completed == []
        assert evt.ctx.transcript.pins.pending == 0

    def test_a_gitignored_file_is_a_non_match_for_a_hook_that_respects_gitignore(self, tmp_path: Path) -> None:
        @on(Event.PreToolUse, mandatory=True)
        def guard(evt: Any) -> None:
            raise AssertionError("handler must not run")

        app._state.gitignore_patterns.append("*.log")
        write = {"tool_name": "Write", "tool_input": {"file_path": "/w/build.log", "content": "watch the script"}}
        evt = PreToolUseEvent(_raw=write, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))
        overrides = outside_margin()
        with reqenv.use_request(overrides):
            assert dispatch(Event.PreToolUse, evt) is None
        assert overrides.mandatory_completed == [completion_key(app._state.hooks[0], 0)]

    @pytest.mark.parametrize("advisory", [True, False])
    def test_every_transcript_branch_is_released(self, tmp_path: Path, advisory: bool) -> None:
        @on(Event.PreToolUse, mandatory=True)
        def guard(evt: Any) -> None:
            return None

        @on(Event.PreToolUse)
        def sibling(evt: Any) -> None:
            return None

        def never(path: Any) -> Any:
            raise AssertionError("evidence must not load")

        source = lazy_transcript("/fixture.jsonl", loader=never)
        evt = PreToolUseEvent(_raw=DESTRUCTIVE, ctx=HookContext(SessionStore(tmp_path), source, None))
        with reqenv.use_request(outside_margin()):
            assert dispatch(Event.PreToolUse, evt, advisory=advisory) is None
        assert source.released
        assert source.pins.pending == 0


@pytest.mark.usefixtures("frozen_clock")
class TestAncestryDeadline:
    def test_a_mandatory_hook_records_no_completion_past_the_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(proc, "process_rows", lambda pids: pytest.fail("no probe past the deadline"))

        @on(Event.PreToolUse, only_if=[SkipPermissions()], mandatory=True)
        def gate(evt: Any) -> None:
            raise AssertionError("handler must not run")

        evt = PreToolUseEvent(_raw=DESTRUCTIVE, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))
        overrides = past_deadline()
        with reqenv.use_request(overrides), pytest.raises(MandatoryDeadlinePassed, match="deadline"):
            dispatch(Event.PreToolUse, evt)
        assert overrides.mandatory_completed == []
        assert overrides.evidence_gaps == []

    def test_an_advisory_hook_is_skipped_with_the_deadline_cause(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(proc, "process_rows", lambda pids: pytest.fail("no probe past the deadline"))

        @on(Event.PreToolUse, only_if=[SkipPermissions()])
        def advisory(evt: Any) -> None:
            raise AssertionError("handler must not run")

        evt = PreToolUseEvent(_raw=DESTRUCTIVE, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))
        overrides = past_deadline()
        with reqenv.use_request(overrides):
            assert dispatch(Event.PreToolUse, evt) is None
        assert overrides.evidence_gaps == [
            f"{app._state.hooks[0].name}: deadline: process ancestry walk reached the caller deadline"
        ]


@pytest.mark.usefixtures("frozen_clock")
class TestMandatoryLane:
    def test_the_guard_completes_while_a_slow_mandatory_hook_still_runs(
        self, general_pack: None, tmp_path: Path
    ) -> None:
        guards = set(session_guards())
        overrides = outside_margin()

        @on(Event.PreToolUse, only_if=[Exhausted()])
        def starved_advisory(evt: Any) -> None:
            raise AssertionError("handler must not run")

        @on(Event.PreToolUse, mandatory=True)
        def slow(evt: Any) -> None:
            patience = time.monotonic() + 5.0
            while not completed_names(overrides) >= guards:
                assert time.monotonic() < patience, "the guards waited behind the slow hook"
                time.sleep(0.01)

        with reqenv.use_request(overrides):
            envelope, _ = dispatch_event(tmp_path, Event.PreToolUse, HEALTHY, session_dir=None)
        assert envelope is None
        assert completed_names(overrides) == guards | {"slow"}
        assert overrides.evidence_gaps == ["starved_advisory: deadline: foreground transcript deadline exhausted"]

    def test_mandatory_hooks_run_side_by_side(self, tmp_path: Path) -> None:
        rendezvous = threading.Barrier(4, timeout=5.0)

        def register(index: int) -> None:
            def together(evt: Any) -> None:
                rendezvous.wait()

            together.__name__ = f"together_{index}"
            on(Event.PreToolUse, mandatory=True)(together)

        for index in range(4):
            register(index)

        evt = PreToolUseEvent(_raw=HEALTHY, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))
        overrides = outside_margin()
        with reqenv.use_request(overrides):
            assert dispatch(Event.PreToolUse, evt) is None
        assert len(overrides.mandatory_completed) == 4
        assert evt.ctx.transcript.pins.pending == 0

    @pytest.mark.parametrize(
        ("seconds", "clamped"),
        [
            pytest.param(30.0, 30, id="outside the margin: the deadline less the margin"),
            pytest.param(-1.0, 4, id="inside the margin: whatever remains"),
        ],
    )
    def test_an_inner_call_clamps_to_the_mandatory_budget(self, tmp_path: Path, seconds: float, clamped: int) -> None:
        seen: dict[str, float | int | None] = {}

        @on(Event.PreToolUse, mandatory=True)
        def guard(evt: Any) -> None:
            seen["timeout"] = reqenv.clamp_timeout(180)
            seen["left"] = reqenv.seconds_left()

        evt = PreToolUseEvent(_raw=HEALTHY, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))
        overrides = outside_margin(seconds)
        with reqenv.use_request(overrides):
            dispatch(Event.PreToolUse, evt)
        assert seen == {"timeout": clamped, "left": float(clamped)}
        assert reqenv.seconds_left() is None
        with reqenv.use_request(overrides):
            assert reqenv.seconds_left() == SYNC_DEADLINE_MARGIN_SECONDS + seconds

    def test_registrations_sharing_a_state_key_keep_the_later_deny(self, tmp_path: Path) -> None:
        def register(verdict: Callable[[Any], Any]) -> None:
            def guard(evt: Any) -> Any:
                return verdict(evt)

            on(Event.PreToolUse, mandatory=True, max_fires=1)(guard)

        register(lambda evt: None)
        register(lambda evt: evt.block("the second registration says no"))
        assert len({hook.state_key for hook in app._state.hooks}) == 1

        evt = PreToolUseEvent(_raw=HEALTHY, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))
        overrides = outside_margin()
        with reqenv.use_request(overrides):
            envelope = dispatch(Event.PreToolUse, evt, session_dir=tmp_path)
        assert decision(envelope) == "deny"
        assert reason(envelope) == "the second registration says no"
        assert len(overrides.mandatory_completed) == 2

    def test_a_hook_ignoring_its_budget_is_the_events_error_at_the_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = ThreadPoolExecutor(max_workers=1)
        monkeypatch.setattr(dispatch_module, "mandatory_pool", lambda: pool)
        ran: list[str] = []

        @on(Event.PreToolUse, mandatory=True)
        def stuck(evt: Any) -> None:
            time.sleep(1.0)

        @on(Event.PreToolUse, mandatory=True)
        def queued(evt: Any) -> None:
            ran.append("queued")

        evt = PreToolUseEvent(_raw=HEALTHY, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))
        overrides = outside_margin(-4.7)
        started = time.monotonic()
        with reqenv.use_request(overrides), pytest.raises(MandatoryDeadlinePassed, match="stuck: still running"):
            dispatch(Event.PreToolUse, evt)
        assert time.monotonic() - started < 1.0
        assert overrides.mandatory_completed == []
        pool.shutdown(wait=True)
        assert overrides.mandatory_completed == []
        assert ran == []
        assert evt.ctx.transcript.pins.pending == 0

    def test_a_late_verdict_records_no_completion_and_refunds_its_fire_slot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = ThreadPoolExecutor(max_workers=1)
        monkeypatch.setattr(dispatch_module, "mandatory_pool", lambda: pool)
        ran: list[str] = []

        @on(Event.PreToolUse, mandatory=True, max_fires=1)
        def stuck(evt: Any) -> Any:
            ran.append("stuck")
            time.sleep(0.6)
            return evt.block("too late")

        def event() -> PreToolUseEvent:
            return PreToolUseEvent(_raw=HEALTHY, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))

        overrides = outside_margin(-4.7)
        with reqenv.use_request(overrides), pytest.raises(MandatoryDeadlinePassed, match="stuck: still running"):
            dispatch(Event.PreToolUse, event(), session_dir=tmp_path)
        pool.shutdown(wait=True)
        assert overrides.mandatory_completed == []
        assert ran == ["stuck"]

        monkeypatch.setattr(dispatch_module, "mandatory_pool", lambda: ThreadPoolExecutor(max_workers=1))
        overrides = outside_margin()
        with reqenv.use_request(overrides):
            envelope = dispatch(Event.PreToolUse, event(), session_dir=tmp_path)
        assert ran == ["stuck", "stuck"]
        assert decision(envelope) == "deny"
        assert reason(envelope) == "too late"
        assert overrides.mandatory_completed == [completion_key(app._state.hooks[0], 0)]

    def test_a_verdict_racing_the_closure_is_refused_whole(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: list[str] = []
        monkeypatch.setattr(dispatch_module, "record_fire", lambda entry, evt, result: recorded.append(entry.name))
        ran: list[str] = []

        @on(Event.PreToolUse, mandatory=True, max_fires=1)
        def policy(evt: Any) -> Any:
            ran.append("policy")
            return evt.block("permission denied")

        def event() -> PreToolUseEvent:
            return PreToolUseEvent(_raw=HEALTHY, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))

        pool = ThreadPoolExecutor(max_workers=1)
        overrides = outside_margin()
        with monkeypatch.context() as racing:
            racing.setattr(dispatch_module, "mandatory_pool", lambda: pool)
            reached, release = paused_before_publish(racing)
            closing_once(racing, reached)
            with reqenv.use_request(overrides), pytest.raises(MandatoryDeadlinePassed, match="policy: still running"):
                dispatch(Event.PreToolUse, event(), session_dir=tmp_path)
            assert overrides.mandatory_phase.failed
            assert overrides.mandatory_completed == []
            release.set()
            pool.shutdown(wait=True)
        assert overrides.mandatory_completed == []
        assert recorded == []

        monkeypatch.setattr(dispatch_module, "mandatory_pool", lambda: ThreadPoolExecutor(max_workers=1))
        retried = outside_margin()
        with reqenv.use_request(retried):
            envelope = dispatch(Event.PreToolUse, event(), session_dir=tmp_path)
        assert ran == ["policy", "policy"]
        assert reason(envelope) == "permission denied"
        assert recorded == ["policy"]
        assert retried.mandatory_completed == [completion_key(app._state.hooks[0], 0)]
        assert retried.mandatory_phase.outcome == "settled"

    def test_a_verdict_published_before_the_closure_is_delivered_whole(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reached, release = threading.Event(), threading.Event()
        recorded: list[str] = []

        def recording_under_the_lock(entry: Any, evt: Any, result: Any) -> None:
            recorded.append(entry.name)
            reached.set()
            assert release.wait(timeout=5.0)

        ran: list[str] = []

        @on(Event.PreToolUse, mandatory=True, max_fires=1)
        def policy(evt: Any) -> Any:
            ran.append("policy")
            return evt.block("permission denied")

        def event() -> PreToolUseEvent:
            return PreToolUseEvent(_raw=HEALTHY, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))

        overrides = outside_margin()

        def dispatched() -> Any:
            with reqenv.use_request(overrides):
                return dispatch(Event.PreToolUse, event(), session_dir=tmp_path)

        pool = ThreadPoolExecutor(max_workers=1)
        with monkeypatch.context() as racing:
            racing.setattr(dispatch_module, "mandatory_pool", lambda: pool)
            racing.setattr(dispatch_module, "record_fire", recording_under_the_lock)
            closing_once(racing, reached)
            with ThreadPoolExecutor(max_workers=1) as collector:
                verdict = collector.submit(dispatched)
                assert reached.wait(timeout=5.0)
                release.set()
                envelope = verdict.result(timeout=5.0)
            pool.shutdown(wait=True)
        assert reason(envelope) == "permission denied"
        assert recorded == ["policy"]
        assert overrides.mandatory_completed == [completion_key(app._state.hooks[0], 0)]
        assert overrides.mandatory_phase.outcome == "settled"

        monkeypatch.setattr(dispatch_module, "mandatory_pool", lambda: ThreadPoolExecutor(max_workers=1))
        spent = outside_margin()
        with reqenv.use_request(spent):
            assert dispatch(Event.PreToolUse, event(), session_dir=tmp_path) is None
        assert ran == ["policy"]
        assert spent.mandatory_completed == [completion_key(app._state.hooks[0], 0)]

    def test_a_publisher_holding_the_closure_past_the_cutoff_never_holds_the_collector(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ticking_clock: Callable[[float], None]
    ) -> None:
        recorded: list[str] = []
        monkeypatch.setattr(dispatch_module, "record_fire", lambda entry, evt, result: recorded.append(entry.name))
        ran: list[str] = []
        pool = ThreadPoolExecutor(max_workers=2)
        overrides = outside_margin()

        def event() -> PreToolUseEvent:
            return PreToolUseEvent(_raw=HEALTHY, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))

        with monkeypatch.context() as racing:
            racing.setattr(dispatch_module, "mandatory_pool", lambda: pool)
            reached, release = held_under_the_closure(racing, "holder")
            closing_once(racing, reached, then=lambda: ticking_clock(31.0))

            @on(Event.PreToolUse, mandatory=True)
            def holder(evt: Any) -> None:
                ran.append("holder")

            @on(Event.PreToolUse, mandatory=True, max_fires=1)
            def policy(evt: Any) -> Any:
                ran.append("policy")
                assert reached.wait(timeout=5.0)
                return evt.block("permission denied")

            with reqenv.use_request(overrides), pytest.raises(MandatoryDeadlinePassed, match="holder: still running"):
                dispatch(Event.PreToolUse, event(), session_dir=tmp_path)
            assert overrides.mandatory_phase.failed
            release.set()
            pool.shutdown(wait=True)
        assert sorted(ran) == ["holder", "policy"]
        assert overrides.mandatory_completed == [completion_key(app._state.hooks[0], 0)]
        assert recorded == []

        ticking_clock(-31.0)
        monkeypatch.setattr(dispatch_module, "mandatory_pool", lambda: ThreadPoolExecutor(max_workers=2))
        retried = outside_margin()
        with reqenv.use_request(retried):
            envelope = dispatch(Event.PreToolUse, event(), session_dir=tmp_path)
        assert sorted(ran) == ["holder", "holder", "policy", "policy"]
        assert reason(envelope) == "permission denied"
        assert recorded == ["policy"]
        assert retried.mandatory_completed == [
            completion_key(hook, ordinal) for ordinal, hook in enumerate(app._state.hooks)
        ]
        assert retried.mandatory_phase.outcome == "settled"

    def test_a_verdict_accepted_before_the_cutoff_settles_while_its_row_is_still_in_flight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ticking_clock: Callable[[float], None]
    ) -> None:
        reached, release = threading.Event(), threading.Event()
        recorded: list[str] = []

        def recording_after_the_commit(entry: Any, evt: Any, result: Any) -> None:
            recorded.append(entry.name)
            reached.set()
            assert release.wait(timeout=5.0)

        ran: list[str] = []

        @on(Event.PreToolUse, mandatory=True, max_fires=1)
        def policy(evt: Any) -> Any:
            ran.append("policy")
            return evt.block("permission denied")

        def event() -> PreToolUseEvent:
            return PreToolUseEvent(_raw=HEALTHY, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))

        overrides = outside_margin()
        pool = ThreadPoolExecutor(max_workers=1)
        with monkeypatch.context() as racing:
            racing.setattr(dispatch_module, "mandatory_pool", lambda: pool)
            racing.setattr(dispatch_module, "record_fire", recording_after_the_commit)
            closing_once(racing, reached, then=lambda: ticking_clock(31.0))
            with reqenv.use_request(overrides):
                envelope = dispatch(Event.PreToolUse, event(), session_dir=tmp_path)
            assert recorded == ["policy"]
            assert overrides.mandatory_phase.outcome == "settled"
            release.set()
            pool.shutdown(wait=True)
        assert reason(envelope) == "permission denied"
        assert overrides.mandatory_completed == [completion_key(app._state.hooks[0], 0)]

        ticking_clock(-31.0)
        monkeypatch.setattr(dispatch_module, "mandatory_pool", lambda: ThreadPoolExecutor(max_workers=1))
        spent = outside_margin()
        with reqenv.use_request(spent):
            assert dispatch(Event.PreToolUse, event(), session_dir=tmp_path) is None
        assert ran == ["policy"]
        assert spent.mandatory_completed == [completion_key(app._state.hooks[0], 0)]

    @pytest.mark.parametrize(
        ("hold", "delayed_collector"),
        [
            pytest.param(held_after_settling, False, id="held-inside-the-closure-past-the-bound"),
            pytest.param(held_at_closure_exit, False, id="held-at-the-closure-exit-after-its-last-check"),
            pytest.param(
                lambda racing: held_under_the_closure(racing, "policy"),
                True,
                id="settled-past-the-cutoff-before-a-delayed-collector-closes",
            ),
        ],
    )
    def test_a_publication_settling_past_the_cutoff_fails_the_phase_and_keeps_no_row_or_slot(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        ticking_clock: Callable[[float], None],
        hold: Callable[[pytest.MonkeyPatch], tuple[threading.Event, threading.Event]],
        delayed_collector: bool,
    ) -> None:
        recorded: list[str] = []
        monkeypatch.setattr(dispatch_module, "record_fire", lambda entry, evt, result: recorded.append(entry.name))
        ran: list[str] = []

        @on(Event.PreToolUse, mandatory=True, max_fires=1)
        def policy(evt: Any) -> Any:
            ran.append("policy")
            return evt.block("permission denied")

        def event() -> PreToolUseEvent:
            return PreToolUseEvent(_raw=HEALTHY, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))

        overrides = outside_margin()
        pool = ThreadPoolExecutor(max_workers=1)
        with monkeypatch.context() as racing:
            racing.setattr(dispatch_module, "mandatory_pool", lambda: pool)
            reached, release = hold(racing)

            def cross_the_cutoff() -> None:
                ticking_clock(31.0)
                if delayed_collector:
                    release.set()
                    pool.submit(lambda: None).result(timeout=5.0)

            closing_once(racing, reached, then=cross_the_cutoff)
            with reqenv.use_request(overrides), pytest.raises(MandatoryDeadlinePassed, match="still publishing"):
                dispatch(Event.PreToolUse, event(), session_dir=tmp_path)
            release.set()
            pool.shutdown(wait=True)
        assert overrides.mandatory_phase.failed
        assert overrides.mandatory_completed == [completion_key(app._state.hooks[0], 0)]
        assert recorded == []

        ticking_clock(-31.0)
        monkeypatch.setattr(dispatch_module, "mandatory_pool", lambda: ThreadPoolExecutor(max_workers=1))
        retried = outside_margin()
        with reqenv.use_request(retried):
            envelope = dispatch(Event.PreToolUse, event(), session_dir=tmp_path)
        assert ran == ["policy", "policy"]
        assert reason(envelope) == "permission denied"
        assert recorded == ["policy"]
        assert retried.mandatory_phase.outcome == "settled"

    def test_a_verdict_reached_after_the_cutoff_is_the_events_error(
        self, tmp_path: Path, ticking_clock: Callable[[float], None]
    ) -> None:
        ran: list[str] = []

        @on(Event.PreToolUse, mandatory=True, max_fires=1)
        def slow(evt: Any) -> Any:
            ran.append("slow")
            ticking_clock(31.0 if len(ran) == 1 else 0.0)
            return evt.block("too late")

        def event() -> PreToolUseEvent:
            return PreToolUseEvent(_raw=HEALTHY, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))

        overrides = outside_margin()
        with reqenv.use_request(overrides), pytest.raises(MandatoryDeadlinePassed, match="slow: finished past"):
            dispatch(Event.PreToolUse, event(), session_dir=tmp_path)
        assert overrides.mandatory_completed == []

        ticking_clock(-31.0)
        with reqenv.use_request(overrides):
            envelope = dispatch(Event.PreToolUse, event(), session_dir=tmp_path)
        assert ran == ["slow", "slow"]
        assert decision(envelope) == "deny"

    @pytest.mark.parametrize(
        ("seconds", "inner"),
        [
            pytest.param(30.0, 30.0, id="outside the margin: the caller's deadline less the margin"),
            pytest.param(-1.0, 4.0, id="inside the margin: the caller's own deadline"),
        ],
    )
    def test_the_bound_is_absolute_however_the_clock_moves_between_reads(
        self, monkeypatch: pytest.MonkeyPatch, seconds: float, inner: float
    ) -> None:
        reads = iter(range(100))
        monkeypatch.setattr(reqenv, "time", SimpleNamespace(time=lambda: FROZEN_NOW + next(reads)))
        overrides = outside_margin(seconds)
        original = overrides.deadline_unix_ms
        with reqenv.use_request(overrides):
            bound = MandatoryBound.of(SYNC_DEADLINE_MARGIN_SECONDS)
            with bound.deadline():
                assert reqenv.current() is not None
                assert reqenv.current().deadline_unix_ms == bound.deadline_unix_ms
                first, second = reqenv.seconds_left(), reqenv.seconds_left()
                assert first is not None and second is not None and second == first - 1.0
                assert reqenv.current().deadline_unix_ms == bound.deadline_unix_ms
        assert next(reads) >= 3
        assert bound.deadline_unix_ms == int((FROZEN_NOW + inner) * 1000)
        assert bound.deadline_unix_ms <= original
        assert bound.cutoff.deadline_unix_ms == min(original, bound.deadline_unix_ms + 250)

    def test_registrations_sharing_a_state_key_each_owe_their_own_completion(self, tmp_path: Path) -> None:
        def register(only_if: list[Any]) -> None:
            def guard(evt: Any) -> None:
                return None

            on(Event.PreToolUse, mandatory=True, only_if=only_if)(guard)

        register([Exhausted()])
        register([])
        assert len({hook.state_key for hook in app._state.hooks}) == 1

        evt = PreToolUseEvent(_raw=HEALTHY, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))
        overrides = outside_margin()
        with reqenv.use_request(overrides):
            assert dispatch(Event.PreToolUse, evt, session_dir=tmp_path) is None
        assert overrides.mandatory_completed == [completion_key(app._state.hooks[1], 1)]

    def test_a_hook_queued_past_the_deadline_is_the_whole_events_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ticking_clock: Callable[[float], None]
    ) -> None:
        monkeypatch.setattr(dispatch_module, "mandatory_pool", lambda: ThreadPoolExecutor(max_workers=1))

        @on(Event.PreToolUse, mandatory=True)
        def first(evt: Any) -> None:
            ticking_clock(SYNC_DEADLINE_MARGIN_SECONDS + 31.0)

        @on(Event.PreToolUse, mandatory=True)
        def queued(evt: Any) -> None:
            raise AssertionError("handler must not run past the deadline")

        evt = PreToolUseEvent(_raw=HEALTHY, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))
        overrides = outside_margin()
        with reqenv.use_request(overrides), pytest.raises(MandatoryDeadlinePassed, match="first: finished past"):
            dispatch(Event.PreToolUse, evt)
        assert overrides.mandatory_completed == []
        assert evt.ctx.transcript.pins.pending == 0

    def test_a_cold_cli_run_stays_unbounded(self, tmp_path: Path) -> None:
        seen: list[float | None] = []

        @on(Event.PreToolUse, mandatory=True)
        def guard(evt: Any) -> None:
            seen.append(reqenv.seconds_left())

        evt = PreToolUseEvent(_raw=HEALTHY, ctx=HookContext(SessionStore(tmp_path), lazy_transcript(None), None))
        assert dispatch(Event.PreToolUse, evt) is None
        assert seen == [None]

    def test_a_timed_out_mandatory_hook_is_the_whole_events_error(
        self, general_pack: None, tmp_path: Path, ticking_clock: Callable[[float], None]
    ) -> None:
        guards = set(session_guards())

        @on(Event.PreToolUse, mandatory=True)
        def judged(evt: Any) -> None:
            timeout = reqenv.clamp_timeout(180)
            ticking_clock(timeout)
            raise TimeoutError(f"claude-sdk timed out after {timeout}s")

        overrides = outside_margin()
        with reqenv.use_request(overrides), pytest.raises(TimeoutError, match="timed out after 30s"):
            dispatch_event(tmp_path, Event.PreToolUse, HEALTHY, session_dir=None)
        assert completed_names(overrides) == guards


class TestGuardCompletion:
    PAYLOAD = '{"cwd":"/w","tool_name":"Bash","tool_input":{"command":"pkill -x sleep"}}'
    STOP_PAYLOAD = '{"cwd":"/w","tool_name":"TaskStop","tool_input":{"task_id":"wcn64vfub"}}'

    def respond(
        self, *, payload: str = PAYLOAD, mandatory: bool = True, event: str = "PreToolUse", deadline_unix_ms: int = 0
    ) -> Any:
        runtime = ProductRuntime(
            registry_factory=lambda _: FakeRegistry(app.current_state()),
            transcript_loader=lambda path: None,
            install_writer=False,
            nlp_warmer=lambda: None,
        )
        response, _ = runtime.dispatch(
            replace(
                request(event=event, payload_raw=payload, mandatory=mandatory),
                client_ppid=HOOK_SHELL,
                deadline_unix_ms=deadline_unix_ms,
            )
        )
        return response

    def test_the_general_pack_completes_the_guard(self, general_pack: None) -> None:
        response = self.respond()
        assert response.exit == 0
        assert response.guard == "completed"
        assert '"permissionDecision": "deny"' in response.stdout

    @pytest.mark.parametrize("event", ["PreToolUse", "PermissionRequest"])
    def test_the_general_pack_completes_the_guard_for_a_stop_tool(self, general_pack: None, event: str) -> None:
        response = self.respond(payload=self.STOP_PAYLOAD, event=event)
        assert response.exit == 0
        assert response.guard == "completed"
        assert '"deny"' in response.stdout
        assert "`TaskStop` on task `wcn64vfub` cannot be verified" in response.stdout

    def test_a_request_the_client_did_not_flag_gets_the_verdict_without_the_completion(
        self, general_pack: None
    ) -> None:
        response = self.respond(mandatory=False)
        assert response.exit == 0
        assert response.guard == ""
        assert "guard" not in response.message()
        assert '"permissionDecision": "deny"' in response.stdout

    def test_a_protected_session_is_denied_while_advisory_evidence_is_slow(self, general_pack: None) -> None:
        @on(Event.PreToolUse, only_if=[Exhausted()])
        def starved_advisory(evt: Any) -> None:
            raise AssertionError("handler must not run")

        response = self.respond(payload='{"cwd":"/w","tool_name":"Bash","tool_input":{"command":"kill 14575"}}')
        assert response.exit == 0
        assert response.guard == "completed"
        assert '"permissionDecision": "deny"' in response.stdout
        assert "an agent session" in response.stdout

    def test_a_safe_call_is_permitted_beside_a_slow_mandatory_hook(self, general_pack: None) -> None:
        @on(Event.PreToolUse, mandatory=True)
        def slow(evt: Any) -> None:
            time.sleep(0.05)

        response = self.respond(payload='{"cwd":"/w","tool_name":"Bash","tool_input":{"command":"ls"}}')
        assert response.exit == 0
        assert response.guard == "completed"
        assert "permissionDecision" not in response.stdout

    def test_a_timed_out_mandatory_hook_is_skipped_with_a_note_and_no_completion(
        self, general_pack: None, ticking_clock: Callable[[float], None]
    ) -> None:
        @on(Event.PreToolUse, mandatory=True)
        def judged(evt: Any) -> None:
            timeout = reqenv.clamp_timeout(180)
            ticking_clock(timeout)
            raise TimeoutError(f"claude-sdk timed out after {timeout}s")

        response = self.respond(
            payload='{"cwd":"/w","tool_name":"Bash","tool_input":{"command":"ls"}}',
            deadline_unix_ms=int((FROZEN_NOW + 30.0) * 1000),
        )
        assert response.exit == 0
        assert response.guard == ""
        assert decision(replied(response)) is None
        assert "judged did not complete (TimeoutError: claude-sdk timed out after 25s)" in response.stdout
        assert "TimeoutError: claude-sdk timed out after 25s" in response.stderr

    def test_a_crashed_mandatory_hook_is_skipped_with_a_note_and_no_completion(self) -> None:
        @on(Event.PreToolUse, mandatory=True)
        def broken(evt: Any) -> None:
            raise RuntimeError("guard crashed")

        response = self.respond()
        assert response.exit == 0
        assert response.guard == ""
        assert decision(replied(response)) is None
        assert "broken did not complete (RuntimeError: guard crashed)" in response.stdout
        assert "RuntimeError: guard crashed" in response.stderr

    def test_a_failure_after_every_mandatory_hook_completed_stays_a_worker_error(self, general_pack: None) -> None:
        @on(Event.PreToolUse, only_if=[Exhausted("invalid_request")])
        def advisory(evt: Any) -> None:
            raise AssertionError("handler must not run")

        response = self.respond(payload='{"cwd":"/w","tool_name":"Bash","tool_input":{"command":"ls"}}')
        assert response.exit == 1
        assert response.guard == ""
        assert "permissionDecision" not in response.stdout

    def test_a_settled_block_stands_when_an_advisory_hook_then_fails(self, general_pack: None) -> None:
        @on(Event.PreToolUse, only_if=[Exhausted("invalid_request")])
        def advisory(evt: Any) -> None:
            raise AssertionError("handler must not run")

        response = self.respond()
        assert response.exit == 0
        assert response.guard == "completed"
        assert decision(replied(response)) == "deny"

    @pytest.mark.parametrize(
        ("broken", "payload"),
        [
            pytest.param("sessions.py", PAYLOAD, id="sessions"),
            pytest.param("stops.py", STOP_PAYLOAD, id="stops"),
        ],
    )
    def test_a_broken_guard_module_leaves_the_completion_empty(
        self, isolate_modules: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: str, payload: str
    ) -> None:
        monkeypatch.setattr("captain_hook.heartbeat.record_heartbeat", lambda *args: None)
        hooks = tmp_path / "hooks"
        shutil.copytree(PACKS_DIR / "general" / "hooks", hooks)
        with (hooks / broken).open("a") as source:
            source.write("\nraise ImportError('broken copy')\n")
        discover_pack("general", hooks)
        assert [error.source for error in app._state.load_errors] == [str(hooks / broken)]
        assert any(hook.spec.mandatory for hook in app._state.hooks)

        response = self.respond(payload=payload)
        assert response.exit == 0
        assert response.guard == ""
        assert "permissionDecision" not in response.stdout
        for surviving in (self.PAYLOAD, self.STOP_PAYLOAD):
            assert self.respond(payload=surviving).guard == ""

    def test_another_packs_guard_cannot_complete_for_a_general_pack_with_no_surviving_guard(
        self, isolate_modules: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("captain_hook.heartbeat.record_heartbeat", lambda *args: None)
        hooks = tmp_path / "hooks"
        shutil.copytree(PACKS_DIR / "general" / "hooks", hooks)
        guard_modules = ("sessions.py", "stops.py")
        for guard_module in guard_modules:
            with (hooks / guard_module).open("a") as source:
                source.write("\nraise ImportError('broken copy')\n")
        other = tmp_path / "other"
        other.mkdir()
        (other / "other_guard.py").write_text(
            "from captain_hook import Event, on\n\n\n"
            "@on(Event.PreToolUse | Event.PermissionRequest, mandatory=True)\n"
            "def other_guard(evt):\n"
            "    return None\n"
        )
        discover_pack("general", hooks)
        discover_pack("other", other)
        assert sorted(error.source for error in app._state.load_errors) == sorted(
            str(hooks / guard_module) for guard_module in guard_modules
        )
        assert [hook.pack_name for hook in app.get_mandatory_hooks(Event.PreToolUse)] == ["other"]

        for payload in (self.PAYLOAD, self.STOP_PAYLOAD):
            response = self.respond(payload=payload)
            assert response.exit == 0
            assert response.guard == ""
            assert "permissionDecision" not in response.stdout

    @pytest.mark.parametrize("event", ["PreToolUse", "PermissionRequest"])
    def test_a_pack_losing_one_of_two_mandatory_modules_leaves_the_completion_empty(
        self, general_pack: None, tmp_path: Path, event: str
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        for name in ("other_alpha", "other_beta"):
            (other / f"{name}.py").write_text(
                "from captain_hook import Event, on\n\n\n"
                "@on(Event.PreToolUse | Event.PermissionRequest, mandatory=True)\n"
                f"def {name}(evt):\n"
                "    return None\n"
            )
        with (other / "other_beta.py").open("a") as source:
            source.write("\nraise ImportError('broken copy')\n")
        discover_pack("other", other)
        assert [error.source for error in app._state.load_errors] == [str(other / "other_beta.py")]
        assert {hook.pack_name for hook in app.get_mandatory_hooks(Event.PreToolUse)} == {"general", "other"}

        healthy = self.respond(
            payload='{"cwd":"/w","tool_name":"Bash","tool_input":{"command":"orca terminal list --json"}}', event=event
        )
        assert (healthy.exit, healthy.guard, healthy.stdout) == (0, "", "discovered out\n")
        stop = self.respond(payload=self.STOP_PAYLOAD, event=event)
        assert (stop.exit, stop.guard) == (0, "")
        assert "`TaskStop` on task `wcn64vfub` cannot be verified" in stop.stdout


SLACK = '{"cwd":"/w","tool_name":"mcp__slack__send_message","tool_input":{"channel":"C1","text":"hello"}}'


@pytest.mark.usefixtures("frozen_clock")
class TestMandatorySkip:
    def respond(self, *, event: str = "PreToolUse", deadline_unix_ms: int = 0) -> Any:
        runtime = ProductRuntime(
            registry_factory=lambda _: FakeRegistry(app.current_state()),
            transcript_loader=lambda path: None,
            install_writer=False,
            nlp_warmer=lambda: None,
        )
        response, _ = runtime.dispatch(
            replace(
                request(event=event, payload_raw=SLACK, mandatory=False),
                client_ppid=HOOK_SHELL,
                deadline_unix_ms=deadline_unix_ms,
            )
        )
        assert response.guard == ""
        assert "guard" not in response.message()
        return response

    def test_a_hook_ignoring_its_budget_is_skipped_at_the_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        pool = ThreadPoolExecutor(max_workers=1)
        monkeypatch.setattr(dispatch_module, "mandatory_pool", lambda: pool)

        @on(Event.PreToolUse, mandatory=True)
        def slack_policy(evt: Any) -> None:
            time.sleep(0.6)

        response = self.respond(deadline_unix_ms=int((FROZEN_NOW + 0.3) * 1000))
        pool.shutdown(wait=True)
        assert response.exit == 0
        envelope = replied(response)
        assert decision(envelope) is None
        assert "slack_policy did not complete (MandatoryDeadlinePassed: slack_policy: still running" in note(envelope)

    def test_a_verdict_racing_the_closure_is_skipped_not_errored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[str] = []
        pool = ThreadPoolExecutor(max_workers=1)

        @on(Event.PreToolUse, mandatory=True)
        def slack_policy(evt: Any) -> Any:
            return evt.block("permission denied")

        with monkeypatch.context() as racing:
            racing.setattr(dispatch_module, "mandatory_pool", lambda: pool)
            racing.setattr(dispatch_module, "record_fire", lambda entry, evt, result: recorded.append(entry.name))
            reached, release = paused_before_publish(racing)
            closing_once(racing, reached)
            response = self.respond(deadline_unix_ms=int((FROZEN_NOW + 30.0) * 1000))
            release.set()
            pool.shutdown(wait=True)
        assert response.exit == 0
        envelope = replied(response)
        assert decision(envelope) is None
        assert "slack_policy did not complete (MandatoryDeadlinePassed: slack_policy: still running" in note(envelope)
        assert "permission denied" not in response.stdout
        assert recorded == []

    def test_a_publisher_holding_the_closure_past_the_deadline_is_skipped_in_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: list[str] = []
        pool = ThreadPoolExecutor(max_workers=2)

        with monkeypatch.context() as racing:
            racing.setattr(dispatch_module, "mandatory_pool", lambda: pool)
            racing.setattr(dispatch_module, "record_fire", lambda entry, evt, result: recorded.append(entry.name))
            reached, release = held_under_the_closure(racing, "holder")
            closing_once(
                racing,
                reached,
                then=lambda: racing.setattr(reqenv, "time", SimpleNamespace(time=lambda: FROZEN_NOW + 60.0)),
            )

            @on(Event.PreToolUse, mandatory=True)
            def holder(evt: Any) -> None:
                return None

            @on(Event.PreToolUse, mandatory=True)
            def slack_policy(evt: Any) -> Any:
                assert reached.wait(timeout=5.0)
                return evt.block("permission denied")

            response = self.respond(deadline_unix_ms=int((FROZEN_NOW + 30.0) * 1000))
            release.set()
            pool.shutdown(wait=True)
        assert response.exit == 0
        envelope = replied(response)
        assert decision(envelope) is None
        assert "did not complete (MandatoryDeadlinePassed: holder: still running" in note(envelope)
        assert "permission denied" not in response.stdout
        assert recorded == []

    def test_a_raising_hook_is_skipped_with_a_note(self) -> None:
        @on(Event.PreToolUse, mandatory=True)
        def slack_policy(evt: Any) -> None:
            raise RuntimeError("policy backend down")

        response = self.respond()
        assert response.exit == 0
        envelope = replied(response)
        assert decision(envelope) is None
        assert (
            note(envelope)
            == envelope["hookSpecificOutput"]["additionalContext"]
            == (
                "capt-hook: the mandatory hook slack_policy did not complete (RuntimeError: policy backend down) "
                "and did not check this call. Run `capt-hook logs` to see why."
            )
        )
        assert "RuntimeError: policy backend down" in response.stderr

    def test_a_hook_left_unrun_is_skipped_with_a_note(self) -> None:
        @on(Event.PreToolUse, only_if=[Exhausted()], mandatory=True)
        def slack_policy(evt: Any) -> None:
            raise AssertionError("handler must not run")

        response = self.respond()
        assert response.exit == 0
        envelope = replied(response)
        assert decision(envelope) is None
        assert "slack_policy did not complete (left unrun)" in note(envelope)
        assert "Traceback" not in response.stderr

    def test_a_permission_request_is_noted_without_a_decision(self) -> None:
        @on(Event.PermissionRequest, mandatory=True)
        def slack_policy(evt: Any) -> None:
            raise RuntimeError("policy backend down")

        response = self.respond(event="PermissionRequest")
        assert response.exit == 0
        envelope = replied(response)
        assert list(envelope) == ["systemMessage"]
        assert "slack_policy did not complete (RuntimeError: policy backend down)" in note(envelope)

    def test_same_key_registrations_with_one_unrun_are_skipped_with_a_note(self) -> None:
        def register(only_if: list[Any]) -> None:
            def slack_policy(evt: Any) -> None:
                return None

            on(Event.PreToolUse, mandatory=True, only_if=only_if)(slack_policy)

        register([Exhausted()])
        register([])
        response = self.respond()
        assert response.exit == 0
        envelope = replied(response)
        assert decision(envelope) is None
        assert "slack_policy did not complete (left unrun)" in note(envelope)

    def test_a_completed_block_stands_beside_a_hook_left_unrun(self) -> None:
        @on(Event.PreToolUse, mandatory=True)
        def slack_policy(evt: Any) -> Any:
            return evt.block("permission denied")

        @on(Event.PreToolUse, only_if=[Exhausted()], mandatory=True)
        def slack_audit(evt: Any) -> None:
            raise AssertionError("handler must not run")

        response = self.respond()
        assert response.exit == 0
        envelope = replied(response)
        assert decision(envelope) == "deny"
        assert "permission denied" in reason(envelope)
        assert "slack_audit did not complete (left unrun)" in note(envelope)

    def test_a_completed_block_stands_beside_a_raising_hook(self) -> None:
        @on(Event.PreToolUse, mandatory=True)
        def slack_policy(evt: Any) -> Any:
            return evt.block("permission denied")

        @on(Event.PreToolUse, mandatory=True)
        def slack_audit(evt: Any) -> None:
            raise RuntimeError("audit backend down")

        response = self.respond()
        assert response.exit == 0
        envelope = replied(response)
        assert decision(envelope) == "deny"
        assert "permission denied" in reason(envelope)
        assert "slack_audit did not complete (RuntimeError: audit backend down)" in note(envelope)

    def test_an_accepted_block_stands_when_a_sibling_holds_the_closure_past_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pool = ThreadPoolExecutor(max_workers=2)
        early_noted = threading.Event()

        with monkeypatch.context() as racing:
            racing.setattr(dispatch_module, "mandatory_pool", lambda: pool)
            reached, release = held_under_the_closure(racing, "holder")
            noting = reqenv.note_mandatory_completed

            def note_early(key: str) -> None:
                noting(key)
                if key.startswith("early_policy."):
                    early_noted.set()

            racing.setattr(reqenv, "note_mandatory_completed", note_early)
            closing_once(
                racing,
                reached,
                then=lambda: racing.setattr(reqenv, "time", SimpleNamespace(time=lambda: FROZEN_NOW + 60.0)),
            )

            @on(Event.PreToolUse, mandatory=True)
            def early_policy(evt: Any) -> Any:
                return evt.block("permission denied")

            @on(Event.PreToolUse, mandatory=True)
            def holder(evt: Any) -> None:
                assert early_noted.wait(timeout=5.0)

            response = self.respond(deadline_unix_ms=int((FROZEN_NOW + 30.0) * 1000))
            release.set()
            pool.shutdown(wait=True)
        assert response.exit == 0
        envelope = replied(response)
        assert decision(envelope) == "deny"
        assert "permission denied" in reason(envelope)
        assert "did not complete (MandatoryDeadlinePassed: holder: still running" in note(envelope)

    def test_a_completed_block_stands_beside_a_hook_that_exits(self) -> None:
        @on(Event.PreToolUse, mandatory=True)
        def slack_policy(evt: Any) -> Any:
            return evt.block("permission denied")

        @on(Event.PreToolUse, mandatory=True)
        def slack_audit(evt: Any) -> None:
            raise SystemExit(0)

        response = self.respond()
        assert response.exit == 0
        envelope = replied(response)
        assert decision(envelope) == "deny"
        assert "permission denied" in reason(envelope)
        assert "slack_audit did not complete (SystemExit: 0)" in note(envelope)

    @pytest.mark.parametrize("event", ["PreToolUse", "PermissionRequest"])
    def test_a_skip_never_carries_another_hooks_allow(self, event: str) -> None:
        @on(Event.PreToolUse | Event.PermissionRequest, only_if=[Exhausted()], mandatory=True)
        def slack_policy(evt: Any) -> None:
            raise AssertionError("handler must not run")

        @on(Event.PreToolUse | Event.PermissionRequest)
        def approver(evt: Any) -> Any:
            return evt.allow()

        response = self.respond(event=event)
        assert response.exit == 0
        envelope = replied(response)
        assert "slack_policy did not complete (left unrun)" in note(envelope)
        assert "permissionDecision" not in response.stdout
        assert '"decision"' not in response.stdout

    def test_a_completed_policy_lets_the_call_through(self) -> None:
        @on(Event.PreToolUse, mandatory=True)
        def slack_policy(evt: Any) -> None:
            return None

        response = self.respond()
        assert response.exit == 0
        assert "permissionDecision" not in response.stdout
