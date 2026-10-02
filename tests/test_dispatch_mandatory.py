from __future__ import annotations

import shutil
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from captain_hook import app
from captain_hook.app import on
from captain_hook.cli import dispatch_event
from captain_hook.context import HookContext
from captain_hook.dispatch import dispatch
from captain_hook.events import PreToolUseEvent
from captain_hook.loader import discover_pack
from captain_hook.session import SessionStore
from captain_hook.snapshots.client import CURRENT_CLIENT, MANDATORY_WORK_SECONDS, EvidenceIncomplete, SnapshotClient
from captain_hook.transcripts import lazy_transcript
from captain_hook.types import CustomCondition, Event
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


def inside_margin() -> reqenv.RequestOverrides:
    return replace(bounded_request(-1.0), client_ppid=HOOK_SHELL)


def outside_margin(seconds: float = 30.0) -> reqenv.RequestOverrides:
    return replace(bounded_request(seconds), client_ppid=HOOK_SHELL)


class Exhausted(CustomCondition):
    def __init__(self, status: str = "deadline") -> None:
        self.status = status

    def check(self, evt: Any) -> bool:
        raise EvidenceIncomplete(self.status, "foreground transcript deadline exhausted")


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
        assert [key.split(".")[0] for key in overrides.mandatory_completed] == session_guards()

    def test_a_healthy_guarded_call_records_completion_without_an_envelope(
        self, general_pack: None, frozen_clock: None, tmp_path: Path
    ) -> None:
        overrides = inside_margin()
        with reqenv.use_request(overrides):
            envelope, _ = dispatch_event(tmp_path, Event.PreToolUse, HEALTHY, session_dir=None)
        assert envelope is None
        assert [key.split(".")[0] for key in overrides.mandatory_completed] == session_guards()

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
        assert [key.split(".")[0] for key in overrides.mandatory_completed] == session_guards()

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
        assert [key.split(".")[0] for key in overrides.mandatory_completed] == session_guards()


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
        assert overrides.mandatory_completed == [app._state.hooks[0].state_key]

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
        assert overrides.mandatory_completed == [app._state.hooks[0].state_key]

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


class TestGuardCompletion:
    PAYLOAD = '{"cwd":"/w","tool_name":"Bash","tool_input":{"command":"pkill -x sleep"}}'
    STOP_PAYLOAD = '{"cwd":"/w","tool_name":"TaskStop","tool_input":{"task_id":"wcn64vfub"}}'

    def respond(self, *, payload: str = PAYLOAD, mandatory: bool = True) -> Any:
        runtime = ProductRuntime(
            registry_factory=lambda _: FakeRegistry(app.current_state()),
            transcript_loader=lambda path: None,
            install_writer=False,
            nlp_warmer=lambda: None,
        )
        response, _ = runtime.dispatch(replace(request(payload_raw=payload, mandatory=mandatory), deadline_unix_ms=0))
        return response

    def test_the_general_pack_completes_the_guard(self, general_pack: None) -> None:
        response = self.respond()
        assert response.exit == 0
        assert response.guard == "completed"
        assert '"permissionDecision": "deny"' in response.stdout

    @pytest.mark.parametrize("event", ["PreToolUse", "PermissionRequest"])
    def test_the_general_pack_completes_the_guard_for_a_stop_tool(self, general_pack: None, event: str) -> None:
        runtime = ProductRuntime(
            registry_factory=lambda _: FakeRegistry(app.current_state()),
            transcript_loader=lambda path: None,
            install_writer=False,
            nlp_warmer=lambda: None,
        )
        response, _ = runtime.dispatch(
            replace(request(event=event, payload_raw=self.STOP_PAYLOAD, mandatory=True), deadline_unix_ms=0)
        )
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

    def test_a_crashed_mandatory_hook_is_a_worker_error_without_completion(self) -> None:
        @on(Event.PreToolUse, mandatory=True)
        def broken(evt: Any) -> None:
            raise RuntimeError("guard crashed")

        response = self.respond()
        assert response.exit == 1
        assert response.guard == ""
        assert "permissionDecision" not in response.stdout
        assert "RuntimeError: guard crashed" in response.stderr

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
