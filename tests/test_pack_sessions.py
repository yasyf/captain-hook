from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

import captain_hook
from captain_hook.app import on
from captain_hook.context import HookContext
from captain_hook.dispatch import SYNC_DEADLINE_MARGIN_SECONDS, dispatch
from captain_hook.events import PreToolUseEvent
from captain_hook.loader import discover_pack
from captain_hook.session import SessionStore
from captain_hook.snapshots.client import EvidenceIncomplete
from captain_hook.testing.helpers import input_to_event
from captain_hook.testing.types import Input
from captain_hook.transcripts import lazy_transcript
from captain_hook.types import CustomCondition, Event
from captain_hook.util import proc, reqenv
from captain_hook.util.proc import ProcessTable
from tests.test_dispatch_snapshot_leases import LeasedFixture
from tests.test_proc import row, table

PACKS_DIR = Path(captain_hook.__file__).parent / "builtin_packs"
SESSIONS_MODULE = "captain_hook.builtin_packs.general.hooks.sessions"
HOOK_SHELL = 27103

OWN_SLEEP = row(31337, 27200, "sleep 60", started="2026-09-30T06:30:00")
MAC = table(
    row(1, 0, "/sbin/launchd", uid=0, started="2026-09-30T05:54:44"),
    row(
        900,
        1,
        "/Users/yasyf/Applications/Captain Hook.app/Contents/Helpers/capt-hookd serve",
        started="2026-09-30T05:55:00",
    ),
    row(1445, 1, "/Applications/Orca.app/Contents/MacOS/Orca", started="2026-09-30T05:58:20"),
    row(
        1743,
        1445,
        "/Applications/Orca.app/Contents/Frameworks/Orca Helper.app/Contents/MacOS/Orca Helper "
        "/Applications/Orca.app/Contents/Resources/app.asar.unpacked/out/main/daemon-entry.js --socket x.sock",
        started="2026-09-30T05:58:30",
    ),
    row(
        14545,
        1743,
        "/usr/bin/login -flpq yasyf /bin/bash --noprofile --norc -p -c orca-tcc-login",
        uid=0,
        started="2026-09-30T06:01:00",
    ),
    row(14550, 14545, "-/opt/homebrew/bin/fish -l", started="2026-09-30T06:01:01"),
    row(14575, 14550, "claude --dangerously-skip-permissions --effort xhigh", started="2026-09-30T06:01:05"),
    row(HOOK_SHELL, 14575, "/opt/homebrew/bin/zsh -c capt-hook run PreToolUse", started="2026-09-30T06:20:00"),
    row(27200, 14575, "/opt/homebrew/bin/zsh -c source snapshot.sh && eval 'sleep 60'", started="2026-09-30T06:29:59"),
    OWN_SLEEP,
    row(
        5988,
        1743,
        "/usr/bin/login -flpq yasyf /bin/bash --noprofile --norc -p -c orca-tcc-login",
        uid=0,
        started="2026-09-30T05:59:00",
    ),
    row(
        14462, 5988, "claude --allow-dangerously-skip-permissions --permission-mode plan", started="2026-09-30T05:59:10"
    ),
    row(115, 14462, "/opt/homebrew/bin/zsh -c source snapshot.sh && eval 'sleep 5'", started="2026-09-30T06:25:00"),
    row(4242, 115, "sleep 5", started="2026-09-30T06:25:01"),
    row(
        8300,
        1743,
        "/usr/bin/login -flpq yasyf /bin/bash --noprofile --norc -p -c orca-tcc-login",
        uid=0,
        started="2026-09-30T06:04:00",
    ),
    row(8311, 8300, "codex --dangerously-bypass-approvals-and-sandbox", started="2026-09-30T06:05:00"),
    row(7777, 1, "node server.js", started="2026-09-30T06:10:00"),
)
REUSED_PID = table(*(entry for entry in MAC.rows.values() if entry.pid != OWN_SLEEP.pid), row(31337, 115, "sleep 30"))
NESTED = table(
    *MAC.rows.values(),
    row(40000, 27200, "claude -p --output-format stream-json", started="2026-09-30T06:40:00"),
    row(40001, 40000, "sleep 30", started="2026-09-30T06:40:01"),
    row(27300, 14575, "/opt/homebrew/bin/zsh -c source snapshot.sh && eval 'sleep 30'", started="2026-09-30T06:41:00"),
    row(40002, 27300, "sleep 30", started="2026-09-30T06:41:01"),
)


def overrides(client_ppid: int = HOOK_SHELL) -> reqenv.RequestOverrides:
    return reqenv.RequestOverrides(env={}, cwd="/w", client_ppid=client_ppid, session_id="s1")


@pytest.fixture
def fake_table(monkeypatch: pytest.MonkeyPatch) -> dict[str, ProcessTable | None]:
    holder: dict[str, ProcessTable | None] = {"table": MAC}
    monkeypatch.setattr(proc, "process_table", lambda **kw: holder["table"])
    return holder


@pytest.fixture
def general_pack(isolate_modules: None) -> None:
    discover_pack("general", PACKS_DIR / "general" / "hooks")


def reason(envelope: dict[str, Any] | None) -> str | None:
    if envelope is None:
        return None
    output = envelope["hookSpecificOutput"]
    if "permissionDecision" in output:
        return output["permissionDecisionReason"] if output["permissionDecision"] == "deny" else None
    return output["decision"]["message"] if output["decision"]["behavior"] == "deny" else None


def decide(
    command: str, tmp_path: Path, *, event: Event = Event.PreToolUse, client_ppid: int = HOOK_SHELL
) -> str | None:
    evt = input_to_event(event, Input(command=command, cwd="/w", session_id="s1"))
    with reqenv.use_request(overrides(client_ppid)):
        return reason(dispatch(event, evt, session_dir=tmp_path))


class TestUnprovenChildren:
    @pytest.mark.parametrize(
        "command",
        [
            "kill 31337",
            "kill -TERM 31337",
            "kill -STOP 31337",
            "kill -9 31337",
            "renice -n 5 -p 31337",
            "timeout 5 kill 31337",
            "nohup kill 31337",
            "env kill -TERM 31337",
            "sudo kill -9 31337",
            "command kill 31337",
        ],
    )
    def test_a_child_under_this_session_has_no_creation_proof(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path, command: str
    ) -> None:
        message = decide(command, tmp_path)
        assert message is not None
        assert "pid 31337 (`sleep 60`) runs under claude 14575" in message
        assert "no recorded per-task creation identity ties it to this task" in message
        assert "Stop a background task you started with the harness's stop tool" in message

    @pytest.mark.parametrize(
        ("command", "holder"),
        [
            pytest.param("kill 40001", "under claude 40000", id="nested_agent_child"),
            pytest.param("kill 40002", "under claude 14575", id="sibling_worker_child"),
        ],
    )
    def test_ancestry_under_this_session_is_not_creation_proof(
        self,
        general_pack: None,
        fake_table: dict[str, ProcessTable | None],
        tmp_path: Path,
        command: str,
        holder: str,
    ) -> None:
        fake_table["table"] = NESTED
        message = decide(command, tmp_path)
        assert message is not None
        assert f"runs {holder}, and no recorded per-task creation identity ties it to this task" in message
        assert "Stop a background task you started with the harness's stop tool" in message

    def test_a_protected_process_inside_this_session_stays_protected(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        fake_table["table"] = table(
            *MAC.rows.values(),
            row(40000, 27200, "codex exec --full-auto review", started="2026-09-30T06:40:00"),
            row(40001, 27200, "tmux new-session -d -s scratch", started="2026-09-30T06:41:00"),
        )
        assert "is an agent session, which no session may signal" in (decide("kill 40000", tmp_path) or "")
        assert "is a terminal multiplexer, which no session may signal" in (decide("kill 40001", tmp_path) or "")
        assert "is an ancestor of a protected process" in (decide("kill 27200", tmp_path) or "")

    def test_another_sessions_child_names_its_agent(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        message = decide("kill 4242", tmp_path)
        assert message is not None
        assert "pid 4242 (`sleep 5`) runs under claude 14462" in message
        assert "no recorded per-task creation identity" in message

    def test_a_detached_process_has_no_provable_owner(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        message = decide("kill 7777", tmp_path)
        assert message is not None
        assert "no agent ancestor to vouch for it" in message

    def test_an_absent_pid_is_stale(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        message = decide("kill 99999", tmp_path)
        assert message is not None
        assert "not in the current process table" in message

    def test_a_reused_pid_now_under_another_session_names_that_session(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        assert "runs under claude 14575" in (decide("kill 31337", tmp_path) or "")
        fake_table["table"] = REUSED_PID
        message = decide("kill 31337", tmp_path)
        assert message is not None
        assert "runs under claude 14462" in message


class TestProtectedHosts:
    @pytest.mark.parametrize(
        ("command", "label"),
        [
            ("kill 14575", "an agent session"),
            ("kill 14550", "an ancestor of a protected process"),
            ("kill 14545", "a terminal host"),
            ("kill 1743", "the Orca PTY daemon"),
            ("kill 1445", "the Orca app"),
            ("kill 900", "the Captain Hook host"),
            ("kill 8311", "an agent session"),
            ("kill 14462", "an agent session"),
            ("kill 1", "the system service manager"),
            ("renice -n 19 -p 14575", "an agent session"),
        ],
    )
    def test_names_the_class(
        self,
        general_pack: None,
        fake_table: dict[str, ProcessTable | None],
        tmp_path: Path,
        command: str,
        label: str,
    ) -> None:
        message = decide(command, tmp_path)
        assert message is not None
        assert f"is {label}, which no session may signal" in message

    def test_a_negative_pgid_is_never_resolved(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        message = decide("kill -9 -14575", tmp_path)
        assert message is not None
        assert "negative process group" in message


class TestFailClosed:
    def test_an_unreadable_process_table_denies(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        fake_table["table"] = None
        message = decide("kill 31337", tmp_path)
        assert message is not None
        assert "the process table could not be read" in message

    def test_an_unresolvable_caller_denies(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        message = decide("kill 31337", tmp_path, client_ppid=424242)
        assert message is not None
        assert "cannot resolve this session's own agent process" in message

    def test_a_crashing_verdict_never_allows(
        self,
        general_pack: None,
        fake_table: dict[str, ProcessTable | None],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def explode(**kw: Any) -> ProcessTable:
            raise RuntimeError("ps exploded")

        monkeypatch.setattr(proc, "process_table", explode)
        with pytest.raises(RuntimeError, match="ps exploded"):
            decide("kill 31337", tmp_path)

    def test_permission_request_denies_too(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        message = decide("pkill -x sleep", tmp_path, event=Event.PermissionRequest)
        assert message is not None
        assert "`pkill` signals every process matching a name" in message
        assert "no recorded per-task creation identity" in (
            decide("kill 31337", tmp_path, event=Event.PermissionRequest) or ""
        )


class TestSnapshotBudget:
    @pytest.mark.parametrize(
        ("command", "calls"),
        [("kill 31337 31337; renice -n 5 -p 31337", 1), ("orca status", 0), ("pkill -x sleep", 0), ("git status", 0)],
    )
    def test_one_snapshot_per_event(
        self, general_pack: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str, calls: int
    ) -> None:
        seen: list[float] = []
        monkeypatch.setattr(proc, "process_table", lambda **kw: (seen.append(kw["timeout"]), MAC)[1])
        decide(command, tmp_path)
        assert seen == [2.0] * calls

    @pytest.mark.parametrize(
        ("left", "timeout"),
        [(SYNC_DEADLINE_MARGIN_SECONDS + 4.0, 2.0), (SYNC_DEADLINE_MARGIN_SECONDS + 1.0, 0.75)],
    )
    def test_ps_finishes_before_dispatch_abandons_the_verdict(
        self, general_pack: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, left: float, timeout: float
    ) -> None:
        seen: list[float] = []
        monkeypatch.setattr(proc, "process_table", lambda **kw: (seen.append(kw["timeout"]), MAC)[1])
        evt = input_to_event(Event.PreToolUse, Input(command="kill 31337", cwd="/w", session_id="s1"))
        with reqenv.use_request(overrides()), reqenv.deadline_in(left):
            message = reason(dispatch(Event.PreToolUse, evt, session_dir=tmp_path))
        assert message is not None
        assert "no recorded per-task creation identity" in message
        assert len(seen) == 1
        assert seen[0] == pytest.approx(timeout, abs=0.05)

    def test_a_deadline_too_close_for_ps_denies_without_running_it(
        self, general_pack: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def never(**kw: Any) -> ProcessTable:
            raise AssertionError("ps must not run")

        monkeypatch.setattr(proc, "process_table", never)
        evt = input_to_event(Event.PreToolUse, Input(command="kill 31337", cwd="/w", session_id="s1"))
        with reqenv.use_request(overrides()), reqenv.deadline_in(SYNC_DEADLINE_MARGIN_SECONDS + 0.3):
            message = reason(dispatch(Event.PreToolUse, evt, session_dir=tmp_path))
        assert message is not None
        assert "the caller deadline is too close to read the process table" in message


@pytest.fixture
def leased_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[PreToolUseEvent, list[str], list[str]]:
    released: list[str] = []
    loaded: list[str] = []
    monkeypatch.setattr("captain_hook.snapshots.client.RemoteSession", LeasedFixture)

    def load(path: str) -> LeasedFixture:
        loaded.append(path)
        return LeasedFixture(released)

    source = lazy_transcript("/fixture.jsonl", loader=load)
    evt = PreToolUseEvent(
        _raw={"tool_name": "Bash", "tool_input": {"command": "pkill -x sleep"}, "cwd": "/w", "session_id": "s1"},
        ctx=HookContext(SessionStore(tmp_path), source, None),
    )
    return evt, loaded, released


def load_guard() -> None:
    if (module := sys.modules.get(SESSIONS_MODULE)) is not None:
        importlib.reload(module)
    else:
        importlib.import_module(SESSIONS_MODULE)


def test_denies_without_touching_transcript_evidence(
    leased_event: tuple[PreToolUseEvent, list[str], list[str]], fake_table: dict[str, ProcessTable | None]
) -> None:
    evt, loaded, released = leased_event
    load_guard()

    class Exhausted(CustomCondition):
        def check(self, evt: Any) -> bool:
            raise EvidenceIncomplete("deadline", "foreground transcript deadline exhausted")

    @on(Event.PreToolUse, only_if=[Exhausted()])
    def starved_sibling(evt: Any) -> None:
        raise AssertionError("handler must not run")

    request = overrides()
    with reqenv.use_request(request):
        envelope = dispatch(Event.PreToolUse, evt)
    assert envelope is not None
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "pkill" in envelope["hookSpecificOutput"]["permissionDecisionReason"]
    assert loaded == released == []
    assert request.evidence_gaps == ["starved_sibling: deadline: foreground transcript deadline exhausted"]
    assert evt.ctx.transcript.pins.pending == 0
