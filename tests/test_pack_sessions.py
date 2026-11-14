from __future__ import annotations

import gc
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import weakref
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from contextvars import copy_context
from copy import copy
from datetime import UTC, datetime, timedelta
from inspect import signature
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

import captain_hook
from captain_hook.app import on
from captain_hook.builtin_packs.general.hooks import _sessions
from captain_hook.builtin_packs.general.hooks._sessions import (
    INLINE_BUSY,
    INLINE_COMMANDS,
    INLINE_LOGIN,
    INLINE_OWNER_TERMINAL,
    INLINE_STARTED,
    INLINE_TRANSCRIPT,
    KILL_RULING_TAGS,
    RECORDED_SINCE,
    Scan,
    inline_class_rulings,
    inline_create,
    inline_ruling,
    inline_run,
    inline_spawn,
    inline_tab,
    inline_worker,
    programs,
)
from captain_hook.context import HookContext
from captain_hook.dispatch import SYNC_DEADLINE_MARGIN_SECONDS, dispatch
from captain_hook.events import BaseHookEvent, PreToolUseEvent
from captain_hook.loader import discover_pack
from captain_hook.session import SessionStore
from captain_hook.snapshots.client import EvidenceIncomplete
from captain_hook.testing.helpers import fixture_file, input_to_event, stubbed_commands
from captain_hook.testing.types import Input
from captain_hook.tickets import Reach, store
from captain_hook.tickets import context as context_module
from captain_hook.tickets import orca as orca_tickets
from captain_hook.tickets.context import said_evidence
from captain_hook.transcripts import lazy_transcript
from captain_hook.types import CustomCondition, Event
from captain_hook.util import proc, reqenv
from captain_hook.util.proc import ProcessRow, ProcessTable
from tests.test_dispatch_snapshot_leases import LeasedFixture
from tests.test_proc import row, table

if TYPE_CHECKING:
    from cc_transcript.command import CommandLine

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
TERMINALS = table(
    *MAC.rows.values(),
    row(15000, 1743, f"{INLINE_LOGIN} /opt/homebrew/bin/fish", uid=0, started="2026-09-30T06:50:00"),
    row(15001, 15000, "-/opt/homebrew/bin/fish -l", started="2026-09-30T06:50:01"),
)
AGENT_TERMINALS = table(
    *MAC.rows.values(),
    row(16000, 1743, f"{INLINE_LOGIN} /opt/homebrew/bin/fish", uid=0, started="2026-09-30T06:52:00"),
    row(16001, 16000, "-/opt/homebrew/bin/fish -l", started="2026-09-30T06:52:01"),
    row(16002, 16001, "claude --dangerously-skip-permissions --effort xhigh", started="2026-09-30T06:52:05"),
)
TWO_AGENT_TERMINALS = table(
    *AGENT_TERMINALS.rows.values(),
    row(17000, 1743, f"{INLINE_LOGIN} /opt/homebrew/bin/fish", uid=0, started="2026-09-30T06:53:00"),
    row(17001, 17000, "-/opt/homebrew/bin/fish -l", started="2026-09-30T06:53:01"),
    row(17002, 17001, "codex --dangerously-bypass-approvals-and-sandbox", started="2026-09-30T06:53:05"),
)
CODEX_HOOK = 8320
CODEX_WATCH = "ccx vcs pr watch Forge-AI/monorepo#51766 --until landed"
CODEX_DAEMON_HOOK = 28400
CODEX_LANE = table(
    *TWO_AGENT_TERMINALS.rows.values(),
    row(CODEX_HOOK, 8311, "/bin/zsh -c capt-hook run PreToolUse", started="2026-09-30T06:30:00"),
    row(8330, 8311, f"/bin/zsh -lc '{CODEX_WATCH}'", started="2026-09-30T06:31:00"),
    row(52209, 8330, CODEX_WATCH, started="2026-09-30T06:31:01"),
    row(17100, 17002, "sleep 30", started="2026-09-30T06:54:00"),
    row(28376, 1, "codex app-server --listen unix:// --managed-daemon", started="2026-09-30T06:00:00"),
    row(CODEX_DAEMON_HOOK, 28376, "/bin/zsh -c capt-hook run PreToolUse", started="2026-09-30T06:40:00"),
    row(28500, 28376, "sleep 30", started="2026-09-30T06:40:05"),
)
OWNER_CLOSE = f"orca terminal close --terminal {INLINE_OWNER_TERMINAL} --json"
AGENT_CLOSE = "orca terminal close --terminal term_agent --json"
IDLE_CLOSE = "orca terminal close --terminal term_idle --json"
SETTLED = INLINE_COMMANDS | inline_class_rulings() | inline_run("term_root")
ROOT = {"ORCA_TERMINAL_HANDLE": "term_root"}
SETTLED_WORKER = {"dispatchId": "ctx_settled", "dispatchStatus": "completed", "runId": "run_inline"}
CLASS_ALLOW = {"allow": True, "relied_on": ["ccn:c9b27c1"]}
ORCA_GC = "orca-reclaim.py"
CLOSE_FIX = (
    "Close only an idle terminal this session created, or ask the owner to name it in a cc-notes answer for a later "
    "session."
)
REAL_CCN_ANSWERS = context_module.ccn_answers


def overrides(client_ppid: int = HOOK_SHELL, env: dict[str, str] | None = None) -> reqenv.RequestOverrides:
    state = {"CAPTAIN_HOOK_STATE_DIR": os.environ["CAPTAIN_HOOK_STATE_DIR"], **(env or {})}
    return reqenv.RequestOverrides(env=state, cwd="/w", client_ppid=client_ppid, session_id="s1")


@pytest.fixture
def fake_table(monkeypatch: pytest.MonkeyPatch) -> dict[str, ProcessTable | None]:
    holder: dict[str, ProcessTable | None] = {"table": MAC}
    monkeypatch.setattr(proc, "process_table", lambda **kw: holder["table"])
    monkeypatch.setattr(proc, "environment", lambda row, **kw: None)
    return holder


@pytest.fixture
def agent_table(fake_table: dict[str, ProcessTable | None]) -> None:
    fake_table["table"] = AGENT_TERMINALS


@pytest.fixture
def environs(monkeypatch: pytest.MonkeyPatch, fake_table: dict[str, ProcessTable | None]) -> dict[int, str]:
    shown: dict[int, str] = {}
    monkeypatch.setattr(proc, "environment", lambda row, **kw: shown.get(row.pid))
    return shown


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LONG_RUNNING_STATE", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    return tmp_path / "home"


def write_drive(home: Path, name: str, roles: dict[str, str], handlers: dict[str, str | None] | None = None) -> Path:
    state = home / ".local" / "state" / "long-running" / name
    state.mkdir(parents=True)
    seats = {
        role: {"session": session, "cwd": f"/c/{name}", "terminal": f"term_{role}"} for role, session in roles.items()
    }
    held = {seat: {"session": session} for seat, session in (handlers or {}).items()}
    (state / "drive.json").write_text(
        json.dumps({"name": name, "orders": {}, "roles": seats, "subcoordinators": {}, "handlers": held})
    )
    return state.parent


def move_drive(home: Path, name: str, rows: dict[tuple[str, str], Any]) -> None:
    state = home / ".local" / "state" / "long-running" / name
    state.mkdir(parents=True)
    moved = {"coordinator": {"session": "r0", "cwd": f"/c/{name}"}}
    pointer = {"moved_to": "drive.db", "name": name, "roles": moved, "subcoordinators": {}, "handlers": {}}
    (state / "drive.json").write_text(json.dumps(pointer))
    with closing(sqlite3.connect(state / "drive.db")) as db, db:
        db.execute(
            "CREATE TABLE rows (collection TEXT NOT NULL, key TEXT NOT NULL, place INTEGER NOT NULL, body TEXT, "
            "PRIMARY KEY (collection, key)) WITHOUT ROWID"
        )
        db.executemany(
            "INSERT INTO rows VALUES (?, ?, 0, ?)",
            [(kind, key, json.dumps(body)) for (kind, key), body in rows.items()],
        )


@pytest.fixture(autouse=True)
def rulings(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    answers: dict[str, list[dict[str, Any]]] = {}
    monkeypatch.setattr(context_module, "ccn_answers", lambda evt, term: answers.get(term, []))
    return answers


def ruling(body: str, *, written: Any = INLINE_STARTED - timedelta(days=1)) -> list[dict[str, Any]]:
    return json.loads(inline_ruling(body, written=written))


def minted(kind: str) -> list[tuple[str, list[str]]]:
    return [(ticket.rule, [item.id for item in ticket.evidence]) for ticket in store.tickets(kind)]


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


def envelope_of(
    inp: Input,
    tmp_path: Path,
    *,
    event: Event = Event.PreToolUse,
    client_ppid: int = HOOK_SHELL,
    env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    evt = input_to_event(event, inp)
    with reqenv.use_request(overrides(client_ppid, env)):
        return dispatch(event, evt, session_dir=tmp_path)


def decide_input(
    inp: Input,
    tmp_path: Path,
    *,
    event: Event = Event.PreToolUse,
    client_ppid: int = HOOK_SHELL,
    env: dict[str, str] | None = None,
) -> str | None:
    return reason(envelope_of(inp, tmp_path, event=event, client_ppid=client_ppid, env=env))


def bash(command: str, **fields: Any) -> Input:
    return Input(command=command, cwd="/w", session_id="s1", **{"transcript": INLINE_TRANSCRIPT, **fields})


def decide(
    command: str, tmp_path: Path, *, event: Event = Event.PreToolUse, client_ppid: int = HOOK_SHELL
) -> str | None:
    return decide_input(bash(command), tmp_path, event=event, client_ppid=client_ppid)


def stop(tool_input: dict[str, Any], **fields: Any) -> Input:
    return Input(
        tool="TaskStop",
        tool_input=tool_input,
        cwd="/w",
        **{"session_id": "s1", "transcript": INLINE_TRANSCRIPT, **fields},
    )


def failing_run(monkeypatch: pytest.MonkeyPatch, program: str, error: BaseException) -> None:
    def run(args: Sequence[str], *pargs: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[0] == program:
            raise error
        raise AssertionError(f"unexpected subprocess {args!r}")

    monkeypatch.setattr(subprocess, "run", run)


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
        assert "Let it finish, or ask the owner to end it; only the long-running drive" in message

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
        assert "Let it finish, or ask the owner to end it; only the long-running drive" in message

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


class TestCodexSessionChildren:
    @pytest.fixture(autouse=True)
    def codex_lane(self, fake_table: dict[str, ProcessTable | None]) -> None:
        fake_table["table"] = CODEX_LANE

    def test_a_codex_lane_may_stop_a_child_its_own_session_started(self, general_pack: None, tmp_path: Path) -> None:
        assert decide("kill 52209", tmp_path, client_ppid=CODEX_HOOK) is None
        assert decide("kill -TERM 52209", tmp_path, client_ppid=CODEX_HOOK) is None

    @pytest.mark.parametrize(
        ("command", "client_ppid", "refusal"),
        [
            pytest.param("kill 17100", CODEX_HOOK, "runs under codex 17002", id="another_codex_session"),
            pytest.param("kill 52209", HOOK_SHELL, "runs under codex 8311", id="a_claude_caller"),
            pytest.param("kill 28500", CODEX_DAEMON_HOOK, "runs under codex 28376", id="a_shared_app_server"),
        ],
    )
    def test_every_other_codex_process_stays_refused(
        self, general_pack: None, tmp_path: Path, command: str, client_ppid: int, refusal: str
    ) -> None:
        assert refusal in (decide(command, tmp_path, client_ppid=client_ppid) or "")


class TestSessionStartedChildren:
    def test_a_child_carrying_this_sessions_id_may_be_killed(
        self, general_pack: None, environs: dict[int, str], tmp_path: Path
    ) -> None:
        environs[31337] = " HOME=/Users/dev CLAUDE_CODE_SESSION_ID=s1 CLAUDECODE=1"
        assert decide("kill 31337", tmp_path) is None
        assert decide("kill -9 31337", tmp_path) is None
        assert decide("renice -n 5 -p 31337", tmp_path) is None

    def test_a_detached_process_this_session_started_may_be_killed(
        self, general_pack: None, environs: dict[int, str], tmp_path: Path
    ) -> None:
        environs[7777] = " CLAUDE_CODE_SESSION_ID=s1"
        assert decide("kill 7777", tmp_path) is None
        assert decide("kill 7777", tmp_path, client_ppid=424242) is None

    @pytest.mark.parametrize(
        "shown",
        [
            pytest.param(" CLAUDE_CODE_SESSION_ID=s2", id="another_session"),
            pytest.param(" CLAUDE_CODE_SESSION_ID=s1 CLAUDE_CODE_SESSION_ID=s2", id="two_sessions"),
            pytest.param(" XCLAUDE_CODE_SESSION_ID=s1", id="another_variable"),
            pytest.param(" CLAUDE_CODE_SESSION_ID=s10", id="id_prefix"),
            pytest.param("", id="no_session"),
        ],
    )
    def test_any_other_environment_has_no_creation_proof(
        self, general_pack: None, environs: dict[int, str], tmp_path: Path, shown: str
    ) -> None:
        environs[31337] = shown
        assert "no recorded per-task creation identity" in (decide("kill 31337", tmp_path) or "")

    def test_a_recycled_or_unreadable_pid_has_no_creation_proof(
        self, general_pack: None, environs: dict[int, str], tmp_path: Path
    ) -> None:
        assert "no recorded per-task creation identity" in (decide("kill 31337", tmp_path) or "")

    def test_a_payload_without_a_session_id_has_no_creation_proof(
        self, general_pack: None, environs: dict[int, str], tmp_path: Path
    ) -> None:
        environs[31337] = " CLAUDE_CODE_SESSION_ID=s1"
        message = decide_input(Input(command="kill 31337", cwd="/w"), tmp_path)
        assert "no recorded per-task creation identity" in (message or "")

    def test_a_protected_process_carrying_this_sessions_id_stays_protected(
        self, general_pack: None, environs: dict[int, str], tmp_path: Path
    ) -> None:
        environs.update({14575: " CLAUDE_CODE_SESSION_ID=s1", 40000: " CLAUDE_CODE_SESSION_ID=s1"})
        assert "is an agent session" in (decide("kill 14575", tmp_path) or "")

    def test_every_target_must_be_proven(self, general_pack: None, environs: dict[int, str], tmp_path: Path) -> None:
        environs[31337] = " CLAUDE_CODE_SESSION_ID=s1"
        assert "runs under claude 14462" in (decide("kill 31337 4242", tmp_path) or "")
        assert "runs under claude 14462" in (decide("kill 4242 31337", tmp_path) or "")
        assert "runs under claude 14462" in (decide("renice -n 5 -p 31337 4242", tmp_path) or "")


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


class TestAppQuit:
    def test_quitting_slack_is_allowed(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        fake_table["table"] = table(
            *MAC.rows.values(),
            row(19000, 1, "/Applications/Slack.app/Contents/MacOS/Slack", started="2026-09-30T07:00:00"),
        )
        assert decide("osascript -e 'tell application \"Slack\" to quit'", tmp_path) is None

    def test_quitting_orca_is_blocked(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        message = decide("osascript -e 'tell application \"Orca\" to quit'", tmp_path)
        assert message is not None
        assert "quits Orca" in message

    def test_quitting_an_app_whose_tree_hosts_claude_is_blocked(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        fake_table["table"] = table(
            *MAC.rows.values(),
            row(19100, 1, "/Applications/Zed.app/Contents/MacOS/zed", started="2026-09-30T07:00:00"),
            row(19101, 19100, "/bin/zsh -l", started="2026-09-30T07:00:01"),
            row(19102, 19101, "claude --dangerously-skip-permissions", started="2026-09-30T07:00:02"),
        )
        message = decide("osascript -e 'tell application \"Zed\" to quit'", tmp_path)
        assert message is not None
        assert "pid 19102" in message


class TestFailClosed:
    def test_an_unreadable_process_table_denies(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        fake_table["table"] = None
        message = decide("kill 31337", tmp_path)
        assert message is not None
        assert "the process table could not be read" in message

    def test_an_unreadable_process_table_denies_an_app_quit(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        fake_table["table"] = None
        message = decide("osascript -e 'tell application \"Slack\" to quit'", tmp_path)
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


class TestRunTimeCommandNames:
    @pytest.mark.parametrize(
        "command",
        [
            'P="cci post --drive $D --lane root"; $P --kind decision --resolves 88115 --topic heal; ls ~/.orca',
            '"$CMD" --kind decision; ls ~/.orca',
            "read p; $p sleep; ls ~/.orca",
            "B=$(command -v biome) && $B check src; ls ~/.orca",
            'eval "$CMD"; ls ~/.orca',
            'bash -c "$CMD"; ls ~/.orca',
            'read -r K <<< kill; caffeinate "$K" 14575',
            "read -r K <<< kill; find /tmp -exec \"$K\" 14575 ';'",
        ],
    )
    def test_an_unreadable_command_name_passes_through(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path, command: str
    ) -> None:
        assert decide(command, tmp_path) is None

    @pytest.mark.parametrize("command", ["P=pkill; $P -x sleep", "eval 'pkill -x sleep'"])
    def test_a_name_that_resolves_to_pkill_still_blocks(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path, command: str
    ) -> None:
        assert "`pkill` signals every process matching a name" in (decide(command, tmp_path) or "")


LAUNCH = "nohup /w/_scratch-watch/watch.sh >/dev/null 2>&1 & echo $! > /w/watch.pid"
WATCH = "/bin/zsh /w/_scratch-watch/watch.sh"


def after_launch() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(minutes=1)


def orphan(pid: int, command: str = WATCH, *, pgid: int | None = None, started: datetime | None = None) -> ProcessRow:
    begun = started or after_launch()
    return ProcessRow(pid, 1, pgid or pid - 1, 501, begun, command)


def in_session(inp: Input, tmp_path: Path, session: Path, event: Event = Event.PreToolUse) -> dict[str, Any] | None:
    evt = input_to_event(event, inp)
    evt.ctx.session = SessionStore(session)
    evt._raw["tool_use_id"] = "toolu_launch"
    with reqenv.use_request(overrides()):
        return dispatch(event, evt, session_dir=tmp_path)


def launch(
    command: str,
    tmp_path: Path,
    fake_table: dict[str, ProcessTable | None],
    *spawned: ProcessRow,
    agent: str | None = None,
) -> Path:
    session = tmp_path / "session"
    session.mkdir()
    assert in_session(bash(command, agent_id=agent), tmp_path, session) is None
    fake_table["table"] = table(*MAC.rows.values(), *spawned)
    assert in_session(bash(command, agent_id=agent), tmp_path, session, Event.PostToolUse) is None
    return session


def killing(pid: int, tmp_path: Path, session: Path, *, agent: str | None = None) -> str | None:
    return reason(in_session(bash(f"kill {pid}", agent_id=agent), tmp_path, session))


class TestSpawnRecords:
    def test_an_orphaned_lane_child_may_be_killed_by_any_lane(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        session = launch(LAUNCH, tmp_path, fake_table, orphan(51159), agent="tooling-lane")
        assert killing(51159, tmp_path, session) is None
        assert killing(51159, tmp_path, session, agent="other-lane") is None

    def test_another_session_cannot_kill_the_recorded_orphan(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        launch(LAUNCH, tmp_path, fake_table, orphan(51159))
        other = tmp_path / "other-session"
        other.mkdir()
        assert "no agent ancestor to vouch for it" in (killing(51159, tmp_path, other) or "")

    def test_the_same_command_from_another_session_is_not_recorded(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        earlier = orphan(50001, started=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=5))
        session = launch(LAUNCH, tmp_path, fake_table, orphan(51159), earlier)
        assert killing(51159, tmp_path, session) is None
        assert "no agent ancestor to vouch for it" in (killing(50001, tmp_path, session) or "")

    def test_two_orphan_groups_in_one_window_record_neither(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        session = launch(LAUNCH, tmp_path, fake_table, orphan(51159), orphan(51170))
        assert "no agent ancestor to vouch for it" in (killing(51159, tmp_path, session) or "")
        assert "no agent ancestor to vouch for it" in (killing(51170, tmp_path, session) or "")

    def test_a_reused_pid_is_not_the_recorded_process(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        session = launch(LAUNCH, tmp_path, fake_table, recorded := orphan(51159))
        fake_table["table"] = table(*MAC.rows.values(), orphan(51159, started=recorded.started + timedelta(hours=1)))
        assert "no agent ancestor to vouch for it" in (killing(51159, tmp_path, session) or "")
        fake_table["table"] = table(*MAC.rows.values(), orphan(51159, "sleep 300", started=recorded.started))
        assert "no agent ancestor to vouch for it" in (killing(51159, tmp_path, session) or "")

    def test_an_unrelated_program_in_the_window_is_not_recorded(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        session = launch(LAUNCH, tmp_path, fake_table, orphan(51159), orphan(51170, "node server.js", pgid=51158))
        assert killing(51159, tmp_path, session) is None
        assert "no agent ancestor to vouch for it" in (killing(51170, tmp_path, session) or "")

    def test_a_sleep_in_a_live_or_older_group_does_not_hide_the_orphan(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
        loop = orphan(27213, "/bin/zsh /w/_scratch-old/loop.sh", pgid=27207, started=now - timedelta(hours=1))
        spawned = (
            orphan(51159, "sleep 3123"),
            ProcessRow(7801, 7777, 7777, 501, now, "sleep 1"),
            loop,
            ProcessRow(27300, 27213, 27207, 501, now, "sleep 0.5"),
        )
        session = launch("nohup sleep 3123 >/dev/null 2>&1 &", tmp_path, fake_table, *spawned)
        assert killing(51159, tmp_path, session) is None
        assert "no agent ancestor to vouch for it" in (killing(7801, tmp_path, session) or "")
        assert "no agent ancestor to vouch for it" in (killing(27300, tmp_path, session) or "")

    def test_a_recorded_child_still_under_this_sessions_agent_may_be_killed(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        now = after_launch()
        session = launch("sleep 300 &", tmp_path, fake_table, ProcessRow(4400, 27200, 27200, 501, now, "sleep 300"))
        assert killing(4400, tmp_path, session) is None
        assert "runs under claude 14575" in (killing(31337, tmp_path, session) or "")

    def test_a_child_under_another_sessions_agent_is_not_recorded(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        now = after_launch()
        session = launch("sleep 5 &", tmp_path, fake_table, ProcessRow(4300, 115, 4300, 501, now, "sleep 5"))
        assert "runs under claude 14462" in (killing(4300, tmp_path, session) or "")


class TestScratchOrphans:
    def scratch(self, started: datetime, ppid: int = 1) -> ProcessRow:
        script = Path.home() / ".claude" / "worktrees" / "cc-context" / "_scratch-cleanup-pause" / "watch.sh"
        return ProcessRow(51159, ppid, 51158, 501, started, f"/bin/zsh {script}")

    def test_a_scratch_orphan_from_before_spawn_records_may_be_killed(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        fake_table["table"] = table(*MAC.rows.values(), self.scratch(RECORDED_SINCE - timedelta(hours=8)))
        assert decide("kill 51159", tmp_path) is None

    @pytest.mark.parametrize(
        ("started", "ppid", "holder"),
        [
            pytest.param(RECORDED_SINCE, 1, "with no agent ancestor", id="started_after_records"),
            pytest.param(RECORDED_SINCE - timedelta(hours=8), 115, "under claude 14462", id="another_sessions_child"),
        ],
    )
    def test_any_other_scratch_process_still_refuses(
        self,
        general_pack: None,
        fake_table: dict[str, ProcessTable | None],
        tmp_path: Path,
        started: datetime,
        ppid: int,
        holder: str,
    ) -> None:
        fake_table["table"] = table(*MAC.rows.values(), self.scratch(started, ppid))
        assert holder in (decide("kill 51159", tmp_path) or "")

    def test_a_detached_process_outside_scratch_still_refuses(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        assert "no agent ancestor to vouch for it" in (decide("kill 7777", tmp_path) or "")


DRIVE_LEFTOVERS = table(
    *TERMINALS.rows.values(),
    row(52000, 1, "python3 desk-runner.py run --config desk.json --desk orca", started="2026-09-30T07:00:00"),
    row(52001, 52000, "python3 ledger.py report --ledger b29c27a", started="2026-09-30T07:00:05"),
    row(52002, 1, "git -C /w fetch origin", started="2026-09-30T07:01:00"),
    row(52003, 15001, "node buildDashboard.js --watch", started="2026-09-30T07:02:00"),
    row(52004, 1, "cc-slack serve", started="2026-09-30T07:03:00"),
    row(
        52005,
        1,
        "node /opt/node/bin/yarn tsx ./scripts/buildDashboard.ts --watch --serve",
        started="2026-09-30T07:04:00",
    ),
    row(
        52007,
        1,
        "python3 -I /cache/skills/long-running/1.0.459/bin/drive watch --loop --drive d1",
        started="2026-09-30T07:05:00",
    ),
    row(
        52006,
        52007,
        "python3 -I /cache/skills/long-running/1.0.459/bin/drive pr land https://github.com/o/r/pull/7 --drive d1",
        started="2026-09-30T07:06:00",
    ),
)


class TestDriveLeftovers:
    @pytest.fixture(autouse=True)
    def leftovers(self, fake_table: dict[str, ProcessTable | None]) -> None:
        fake_table["table"] = DRIVE_LEFTOVERS

    @pytest.fixture
    def registry(self, home: Path) -> Path:
        write_drive(home, "d1", {"coordinator": "r0", "meta": "s1"}, {"h1": "h-1", "h2": None})
        return write_drive(home, "d2", {"coordinator": "r9"})

    @pytest.mark.parametrize(
        ("pid", "shown"),
        [
            pytest.param(52000, " CLAUDE_CODE_SESSION_ID=r0", id="desk_runner_from_an_earlier_root"),
            pytest.param(52001, " CLAUDE_CODE_SESSION_ID=r0", id="live_desk_runner_child"),
            pytest.param(52002, " CLAUDE_CODE_SESSION_ID=w7 CLAUDE_LONG_RUNNING_DRIVE=d1", id="orca_worker_orphan"),
            pytest.param(52003, " CLAUDE_LONG_RUNNING_DRIVE=d1", id="dev_server_in_an_ended_lanes_terminal"),
            pytest.param(52005, " CLAUDE_CODE_SESSION_ID=h-1", id="handler_session"),
        ],
    )
    def test_a_leftover_the_drive_started_may_be_ended(
        self, general_pack: None, environs: dict[int, str], registry: Path, tmp_path: Path, pid: int, shown: str
    ) -> None:
        environs[pid] = shown
        assert decide(f"kill {pid}", tmp_path) is None
        assert decide(f"kill -9 {pid}", tmp_path, client_ppid=424242) is None
        assert decide(f"renice -n 5 -p {pid}", tmp_path) is None

    @pytest.mark.parametrize(
        "broken",
        [
            '{"name": "a-broken", "roles": {"meta": {"session": "s1"',
            '{"name": "a-broken", "roles": {"meta": "s1"}}',
            '{"name": "a-broken", "roles": {"meta": {"session": "s1"}}}',
        ],
    )
    def test_an_unreadable_drive_state_is_skipped_and_never_opens_the_guard(
        self, general_pack: None, environs: dict[int, str], registry: Path, tmp_path: Path, broken: str
    ) -> None:
        (registry / "a-broken").mkdir()
        (registry / "a-broken" / "drive.json").write_text(broken)
        environs[52002] = " CLAUDE_CODE_SESSION_ID=r0"
        assert decide("kill 52002", tmp_path) is None
        assert "is an agent session" in (decide("kill 52002 14575", tmp_path) or "")

    def test_an_orca_worker_of_the_drive_ends_its_leftovers(
        self, general_pack: None, environs: dict[int, str], registry: Path, tmp_path: Path
    ) -> None:
        environs[52002] = " CLAUDE_CODE_SESSION_ID=r0"
        worker = Input(command="kill 52002", cwd="/w", session_id="w7", transcript=INLINE_TRANSCRIPT)
        assert decide_input(worker, tmp_path, env={"CLAUDE_LONG_RUNNING_DRIVE": "d1"}) is None
        assert "only the long-running drive that started it" in (decide_input(worker, tmp_path) or "")

    @pytest.mark.parametrize(
        "shown",
        [
            pytest.param(" CLAUDE_CODE_SESSION_ID=r9", id="another_drives_root"),
            pytest.param(" CLAUDE_LONG_RUNNING_DRIVE=d2", id="another_drive"),
            pytest.param(" CLAUDE_CODE_SESSION_ID=r0 CLAUDE_LONG_RUNNING_DRIVE=d2", id="mixed"),
            pytest.param(" CLAUDE_CODE_SESSION_ID=r0 CLAUDE_CODE_SESSION_ID=r9", id="two_sessions"),
            pytest.param("", id="no_identity"),
        ],
    )
    def test_a_process_the_drive_did_not_start_stays_blocked(
        self, general_pack: None, environs: dict[int, str], registry: Path, tmp_path: Path, shown: str
    ) -> None:
        environs[52002] = shown
        message = decide("kill 52002", tmp_path)
        assert message is not None
        assert "with no agent ancestor to vouch for it" in message
        assert message.endswith(
            "Let it finish, or ask the owner to end it; a drive ends only a leftover whose environment names the drive."
        )

    def test_the_drives_meta_session_ends_a_stuck_verb_the_drive_detached(
        self, general_pack: None, environs: dict[int, str], registry: Path, tmp_path: Path
    ) -> None:
        environs[52006] = " CLAUDE_LONG_RUNNING_DRIVE=d1 CLAUDE_LONG_RUNNING_LANE=watch"
        assert decide("kill 52006", tmp_path) is None
        environs[52006] = " CLAUDE_LONG_RUNNING_DRIVE=d2 CLAUDE_LONG_RUNNING_LANE=watch"
        assert "a drive ends only a leftover whose environment names the drive" in (
            decide("kill 52006", tmp_path) or ""
        )

    def test_a_session_in_no_drive_claims_no_leftover(
        self, general_pack: None, environs: dict[int, str], home: Path, tmp_path: Path
    ) -> None:
        environs[52002] = " CLAUDE_CODE_SESSION_ID=r0"
        message = decide("kill 52002", tmp_path) or ""
        assert message.endswith(
            "ask the owner to end it; only the long-running drive that started it may end it unasked."
        )

    def test_a_process_a_live_agent_holds_is_no_leftover(
        self, general_pack: None, environs: dict[int, str], registry: Path, tmp_path: Path
    ) -> None:
        environs[4242] = " CLAUDE_LONG_RUNNING_DRIVE=d1"
        message = decide("kill 4242", tmp_path)
        assert message is not None
        assert "runs under claude 14462" in message
        assert "a drive ends only a leftover no live agent holds" in message

    @pytest.mark.parametrize(
        ("pid", "refusal"),
        [
            pytest.param(
                15001, "never ends an agent host or a direct child of a protected process", id="terminal_shell"
            ),
            pytest.param(52004, "never ends an agent host or a direct child of a protected process", id="agent_shim"),
            pytest.param(14575, "is an agent session, which no session may signal", id="agent"),
            pytest.param(15000, "is a terminal host, which no session may signal", id="terminal_host"),
            pytest.param(900, "is the Captain Hook host, which no session may signal", id="capt_hookd"),
        ],
    )
    def test_protected_processes_and_their_shells_stay_blocked(
        self, general_pack: None, environs: dict[int, str], registry: Path, tmp_path: Path, pid: int, refusal: str
    ) -> None:
        environs[pid] = " CLAUDE_CODE_SESSION_ID=r0 CLAUDE_LONG_RUNNING_DRIVE=d1"
        assert refusal in (decide(f"kill {pid}", tmp_path) or "")


def stuck_verb(agent: int) -> ProcessTable:
    sprite = "pool-hlnbmpe66lwtbf54"
    return table(
        *MAC.rows.values(),
        row(76822, agent, "/bin/zsh -c source snapshot.sh && eval 'drive job end j2198 2>&1 | tail -c 600'"),
        row(
            76824,
            76822,
            "/Users/dev/.local/share/uv/python/cpython-3.13.5-macos-aarch64-none/bin/python3.13 -I "
            "/Users/dev/.claude/plugins/cache/skills/long-running/1.0.606/bin/drive job end j2198",
        ),
        row(76825, 76822, "tail -c 600"),
        row(
            77014,
            76824,
            f"/usr/bin/python3 /Users/dev/.claude/plugins/cache/skills/orca/0.1.42/bin/orca-reclaim.py --save {sprite}",
        ),
        row(77069, 77014, f"/usr/bin/ssh -F /Users/dev/.cc-remote/ssh/{sprite}.ssh -o BatchMode=yes {sprite} bash -s"),
    )


class TestStuckDriveCommands:
    @pytest.fixture(autouse=True)
    def hidden_ssh(self, environs: dict[int, str]) -> None:
        environs[77069] = ""

    @pytest.fixture
    def registry(self, home: Path) -> Path:
        write_drive(home, "d1", {"coordinator": "r0", "meta": "s1", "platform": "p1"})
        return write_drive(home, "d2", {"coordinator": "r9"})

    @pytest.mark.parametrize("pid", [76824, 77014, 77069])
    def test_any_drive_session_ends_a_stuck_drive_command_and_what_runs_under_it(
        self,
        general_pack: None,
        fake_table: dict[str, ProcessTable | None],
        environs: dict[int, str],
        registry: Path,
        tmp_path: Path,
        pid: int,
    ) -> None:
        fake_table["table"] = stuck_verb(14462)
        environs[76824] = " CLAUDE_CODE_SESSION_ID=p1"
        assert decide(f"kill {pid}", tmp_path) is None

    @pytest.mark.parametrize("pid", [76822, 76825])
    def test_the_agents_shell_and_a_sibling_of_the_drive_command_stay_blocked(
        self,
        general_pack: None,
        fake_table: dict[str, ProcessTable | None],
        environs: dict[int, str],
        registry: Path,
        tmp_path: Path,
        pid: int,
    ) -> None:
        fake_table["table"] = stuck_verb(14462)
        environs.update({76822: " CLAUDE_CODE_SESSION_ID=p1", 76824: " CLAUDE_CODE_SESSION_ID=p1"})
        message = decide(f"kill {pid}", tmp_path) or ""
        assert "runs under claude 14462" in message
        assert message.endswith(
            "a drive ends only a leftover no live agent holds, or a drive command and what runs under it."
        )

    @pytest.mark.parametrize(
        "shown",
        [
            pytest.param(" CLAUDE_LONG_RUNNING_DRIVE=d2", id="another_drive"),
            pytest.param(" CLAUDE_CODE_SESSION_ID=r9", id="another_drives_root"),
            pytest.param("", id="hidden"),
        ],
    )
    def test_a_drive_command_the_drive_did_not_start_stays_blocked(
        self,
        general_pack: None,
        fake_table: dict[str, ProcessTable | None],
        environs: dict[int, str],
        registry: Path,
        tmp_path: Path,
        shown: str,
    ) -> None:
        fake_table["table"] = stuck_verb(14462)
        environs[76824] = shown
        assert (decide("kill 77069", tmp_path) or "").endswith(
            "a drive ends only a leftover whose environment names the drive."
        )

    def test_a_session_in_no_drive_never_ends_another_sessions_drive_command(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], environs: dict[int, str], tmp_path: Path
    ) -> None:
        fake_table["table"] = stuck_verb(14462)
        environs[76824] = " CLAUDE_CODE_SESSION_ID=p1"
        assert (decide("kill 77069", tmp_path) or "").endswith(
            "only the long-running drive that started it may end it unasked."
        )

    def test_the_starter_ends_its_own_stuck_ssh_from_the_drive_commands_environment(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], environs: dict[int, str], tmp_path: Path
    ) -> None:
        fake_table["table"] = stuck_verb(14575)
        environs[76824] = " CLAUDE_CODE_SESSION_ID=s1"
        assert decide("kill 77069", tmp_path) is None

    def test_the_starter_reads_no_environment_at_or_above_its_agent(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], environs: dict[int, str], tmp_path: Path
    ) -> None:
        fake_table["table"] = stuck_verb(14575)
        environs.update({76825: "", 14575: " CLAUDE_CODE_SESSION_ID=s1"})
        assert "no recorded per-task creation identity" in (decide("kill 76825", tmp_path) or "")


class TestMovedDrives:
    @pytest.mark.parametrize(
        "rows",
        [
            pytest.param(
                {
                    ("", "roles"): {"coordinator": {"session": "r0", "cwd": "/c/d1"}, "meta": {"session": "s1"}},
                    ("", "subcoordinators"): {"platform": {"session": "p1"}},
                },
                id="roles",
            ),
            pytest.param(
                {
                    ("", "driver"): {"session": "r0", "cwd": "/c/d1"},
                    ("handlers", "meta"): {"session": "s1"},
                    ("handlers", "platform"): {"session": "p1"},
                },
                id="driver_and_handlers",
            ),
        ],
    )
    def test_sessions_bound_after_the_drive_moved_into_drive_db_end_its_stuck_command(
        self,
        general_pack: None,
        fake_table: dict[str, ProcessTable | None],
        environs: dict[int, str],
        home: Path,
        tmp_path: Path,
        rows: dict[tuple[str, str], Any],
    ) -> None:
        move_drive(home, "d1", rows)
        fake_table["table"] = stuck_verb(14462)
        environs.update({76824: " CLAUDE_CODE_SESSION_ID=p1", 77069: ""})
        assert decide("kill 77069", tmp_path) is None

    def test_a_terminal_bound_after_the_drive_moved_into_drive_db_is_the_drives(self, home: Path) -> None:
        move_drive(
            home, "d1", {("", "roles"): {"coordinator": {"session": "r0", "cwd": "/c/d1", "terminal": "term_root"}}}
        )
        assert _sessions.drive_terminal("term_root")
        assert not _sessions.drive_terminal("term_agent")


class TestKillRulingPrograms:
    @pytest.mark.parametrize(
        ("command", "names"),
        [
            pytest.param(
                "node /opt/node/bin/yarn tsx ./scripts/buildDashboard.ts --watch --serve",
                ("buildDashboard", "buildDashboard.ts", "tsx", "yarn", "node"),
                id="yarn_tsx",
            ),
            pytest.param(
                "npm exec tsx ./scripts/buildDashboard.ts --serve",
                ("buildDashboard", "buildDashboard.ts", "tsx", "npm"),
                id="npm_exec",
            ),
            pytest.param(
                "/opt/node/bin/node --require /w/tsx/dist/preflight.cjs --import file:///w/tsx/dist/loader.mjs "
                "./scripts/buildDashboard.ts --serve",
                ("buildDashboard", "buildDashboard.ts", "loader.mjs", "preflight.cjs", "node"),
                id="loaders",
            ),
            pytest.param(
                "python3 desk-runner.py run --config runner.json",
                ("desk-runner", "desk-runner.py", "python3"),
                id="script_arguments",
            ),
            pytest.param("git -C /w fetch origin", ("git",), id="subcommand"),
            pytest.param("sleep 60", ("sleep",), id="plain"),
        ],
    )
    def test_every_name_in_the_host_chain_is_a_program(self, command: str, names: tuple[str, ...]) -> None:
        assert programs(row(1, 1, command)) == names

    @pytest.mark.parametrize("named", ["buildDashboard", "buildDashboard.ts", "tsx", "yarn", "node"])
    def test_a_ruling_naming_any_program_in_the_chain_lifts_the_kill(
        self,
        general_pack: None,
        fake_table: dict[str, ProcessTable | None],
        rulings: dict[str, list[dict[str, Any]]],
        tmp_path: Path,
        named: str,
    ) -> None:
        fake_table["table"] = DRIVE_LEFTOVERS
        rulings[named] = json.loads(inline_ruling(f"Owner: end leftover {named} dev servers.", tags=KILL_RULING_TAGS))
        assert decide("kill 52005", tmp_path) is None

    def test_a_ruling_naming_a_script_argument_keeps_the_block(
        self,
        general_pack: None,
        fake_table: dict[str, ProcessTable | None],
        rulings: dict[str, list[dict[str, Any]]],
        tmp_path: Path,
    ) -> None:
        fake_table["table"] = DRIVE_LEFTOVERS
        rulings["--serve"] = json.loads(inline_ruling("Owner: end --serve.", tags=KILL_RULING_TAGS))
        rulings["buildDashboard"] = json.loads(inline_ruling("Owner: end buildDashboard.tsx.", tags=KILL_RULING_TAGS))
        assert "no agent ancestor to vouch for it" in (decide("kill 52005", tmp_path) or "")


TAP_LANE = {"CLAUDE_LONG_RUNNING_DRIVE": "d1", "CLAUDE_LONG_RUNNING_LANE": "meta-desk-3"}
RECORDER = 52010
OTHER_RECORDER = 52011
TAP_LABELS = ("board-answer", "from:owner", "board-session:b1", "board-card:card-rec", "board-block:rec-choice")
TAP_WORDS = f"May I restart your recorder? meta-desk restarts pid {RECORDER}. Yes, restart it now No"
SCRIPT_WORDS = "May I restart your recorder? meta-desk restarts board_watch.py. Yes No"
TAP_ROW = {"id": "c8be9e0", "title": "May I restart your recorder?", "body": "picked", "tags": list(TAP_LABELS)}
TAP_TABLE = table(
    *DRIVE_LEFTOVERS.rows.values(),
    row(RECORDER, 1, "python3 board_watch.py start --session b1", started="2026-09-30T07:05:00"),
    row(OTHER_RECORDER, 1, "python3 board_watch.py start --session b2", started="2026-09-30T07:06:00"),
)


def tap_time(minutes_ago: float = 10) -> datetime:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).replace(microsecond=0)


def tap_answer(
    words: str = TAP_WORDS,
    picked: Sequence[str] = ("Yes, restart it now",),
    *,
    at: datetime | None = None,
    origin: str = "human",
    block: str = "rec-choice",
    kind: str = "choice.selected",
    **payload: Any,
) -> dict[str, Any]:
    return {
        "type": kind,
        "origin": origin,
        "at": (at or tap_time()).isoformat(),
        "round": 1,
        "payload": {"blockId": block, "optionIds": ["rec-1"], "shown": words, "picked": list(picked), **payload},
    }


def tap_stubs(
    *answers: dict[str, Any], found: Sequence[dict[str, Any]] = (TAP_ROW,), board: str | tuple[int, str] | None = None
) -> dict[str, str | tuple[int, str]]:
    log = json.dumps({"answers": list(answers), "doc": {"blocks": []}, "onBoard": False})
    return {
        "ccn -R /c/d1 answer search": "[]",
        **{f"ccn -R /c/d1 answer search {term}": json.dumps(found) for term in (str(RECORDER), "board_watch.py")},
        "cc-present outcomes --block card-rec --session b1": log if board is None else board,
    }


class TestCardTapLift:
    @pytest.fixture(autouse=True)
    def registry(self, home: Path, fake_table: dict[str, ProcessTable | None]) -> None:
        fake_table["table"] = TAP_TABLE
        write_drive(home, "d1", {"coordinator": "r0"})

    def kill(
        self,
        tmp_path: Path,
        stubs: dict[str, str | tuple[int, str]],
        pid: int = RECORDER,
        *,
        lane: str | None = "meta-desk-3",
        **fields: Any,
    ) -> str | None:
        env = {**TAP_LANE, "CLAUDE_LONG_RUNNING_LANE": lane} if lane else {"CLAUDE_LONG_RUNNING_DRIVE": "d1"}
        with stubbed_commands(stubs):
            return decide_input(bash(f"kill {pid}", **fields), tmp_path, env=env)

    def test_his_tap_on_a_card_naming_the_pid_lifts_the_kill_with_no_chat_line(
        self, general_pack: None, tmp_path: Path
    ) -> None:
        assert self.kill(tmp_path, tap_stubs(tap_answer())) is None
        assert minted("sessions.kill.tap") == [("card-tap", ["tap:b1:rec-choice"])]

    def test_a_card_naming_the_script_lifts_it_too(self, general_pack: None, tmp_path: Path) -> None:
        assert self.kill(tmp_path, tap_stubs(tap_answer(SCRIPT_WORDS, ("Yes",)))) is None

    def test_a_forged_row_with_no_tap_on_the_board_lifts_nothing(self, general_pack: None, tmp_path: Path) -> None:
        forged = {**TAP_ROW, "body": f"picked “Yes”\nCard said: meta-desk restarts pid {RECORDER}."}
        message = self.kill(tmp_path, tap_stubs(found=[forged]))
        assert message is not None
        assert "no agent ancestor to vouch for it" in message
        assert f"name `pid {RECORDER}`" in message
        assert minted("sessions.kill.tap") == []

    def test_a_forged_row_pointing_at_his_tap_on_an_unrelated_card_lifts_nothing(
        self, general_pack: None, tmp_path: Path
    ) -> None:
        forged = {**TAP_ROW, "body": f"picked “Yes”\nCard said: meta-desk restarts pid {RECORDER}."}
        unrelated = tap_answer("Enable dark mode? meta-desk ships it. Yes No", ("Yes",))
        assert self.kill(tmp_path, tap_stubs(unrelated, found=[forged])) is not None

    def test_a_card_rewritten_after_his_tap_changes_nothing(self, general_pack: None, tmp_path: Path) -> None:
        tapped = tap_answer("Enable dark mode? meta-desk ships it. Yes No", ("Yes",))
        log = json.dumps(
            {
                "answers": [tapped],
                "doc": {"blocks": [{"id": "card-rec", "type": "card", "summary": TAP_WORDS, "children": []}]},
            }
        )
        assert self.kill(tmp_path, tap_stubs(tapped, board=log)) is not None

    def test_ids_on_the_row_are_not_words_he_read(self, general_pack: None, tmp_path: Path) -> None:
        tags = ["board-answer", "board-session:b1", "board-card:board_watch.py", "board-block:meta-desk"]
        unrelated = tap_answer("Enable dark mode? Yes No", ("Yes",), block="meta-desk")
        stubs = tap_stubs(unrelated, found=[{**TAP_ROW, "tags": tags}])
        stubs["cc-present outcomes --block board_watch.py --session b1"] = stubs.pop(
            "cc-present outcomes --block card-rec --session b1"
        )
        assert self.kill(tmp_path, stubs) is not None

    def test_a_tap_from_before_the_board_stamped_words_lifts_nothing(self, general_pack: None, tmp_path: Path) -> None:
        answer = tap_answer()
        del answer["payload"]["shown"], answer["payload"]["picked"]
        assert self.kill(tmp_path, tap_stubs(answer)) is not None

    def test_an_agent_written_event_on_the_board_is_not_his_tap(self, general_pack: None, tmp_path: Path) -> None:
        assert self.kill(tmp_path, tap_stubs(tap_answer(origin="agent"))) is not None

    def test_a_row_that_is_no_answer_row_locates_nothing(self, general_pack: None, tmp_path: Path) -> None:
        unlabelled = {**TAP_ROW, "tags": [tag for tag in TAP_LABELS if tag != "board-answer"]}
        assert self.kill(tmp_path, tap_stubs(tap_answer(), found=[unlabelled])) is not None

    def test_his_newest_answer_decides(self, general_pack: None, tmp_path: Path) -> None:
        then_no = tap_answer(picked=("No",), at=tap_time(2))
        assert self.kill(tmp_path, tap_stubs(then_no, tap_answer())) is not None

    def test_another_blocks_answer_on_the_card_is_not_this_one(self, general_pack: None, tmp_path: Path) -> None:
        assert self.kill(tmp_path, tap_stubs(tap_answer(block="another-choice"))) is not None

    @pytest.mark.parametrize("minutes", [59, 61, 600])
    def test_a_tap_a_two_minute_retry_could_carry_past_the_hour_lifts_nothing(
        self, general_pack: None, tmp_path: Path, minutes: int
    ) -> None:
        assert self.kill(tmp_path, tap_stubs(tap_answer(at=tap_time(minutes)))) is not None

    @pytest.mark.parametrize(
        ("picked", "fields"),
        [
            pytest.param(("No, keep the old route",), {}, id="no"),
            pytest.param(("Leave it running",), {}, id="a_keep"),
            pytest.param(("Yes, but not yet",), {}, id="a_hedged_yes"),
            pytest.param(("Yes",), {"other": "actually wait"}, id="a_yes_with_a_hold_typed"),
            pytest.param((), {"other": "restart it"}, id="words_with_no_pick"),
            pytest.param(("Yes", "No"), {}, id="a_multi_select_with_a_no"),
        ],
    )
    def test_a_pick_that_is_no_go_lifts_nothing(
        self, general_pack: None, tmp_path: Path, picked: tuple[str, ...], fields: dict[str, str]
    ) -> None:
        assert self.kill(tmp_path, tap_stubs(tap_answer(picked=picked, **fields))) is not None

    @pytest.mark.parametrize("picked", ["Restart it now", "OK", "Yes, restart it now"])
    def test_a_pick_that_reads_as_a_go_lifts_it(self, general_pack: None, tmp_path: Path, picked: str) -> None:
        assert self.kill(tmp_path, tap_stubs(tap_answer(picked=(picked,)))) is None

    @pytest.mark.parametrize(
        ("verdict", "fields", "lifted"),
        [
            pytest.param("approved", {}, True, id="approved"),
            pytest.param("rejected", {}, False, id="rejected"),
            pytest.param("cleared", {}, False, id="cleared"),
            pytest.param("approved", {"note": "wait for the release"}, False, id="approved_with_a_hold"),
        ],
    )
    def test_an_approval_card_lifts_on_his_approval(
        self, general_pack: None, tmp_path: Path, verdict: str, fields: dict[str, str], lifted: bool
    ) -> None:
        answer = tap_answer(picked=(), kind="decision.created", verdict=verdict, **fields)
        assert (self.kill(tmp_path, tap_stubs(answer)) is None) is lifted

    @pytest.mark.parametrize(
        "words",
        [
            pytest.param("meta-desk restarts the recorder.", id="no_process"),
            pytest.param(f"meta-desk read #{RECORDER}.", id="a_number_that_is_no_pid"),
            pytest.param("meta-desk restarts its python3.", id="only_the_interpreter"),
            pytest.param("meta-desk restarts board_watch.py.bak.", id="a_longer_name"),
        ],
    )
    def test_a_card_that_does_not_name_the_process_lifts_nothing(
        self, general_pack: None, tmp_path: Path, words: str
    ) -> None:
        stubs = tap_stubs(tap_answer(words, ("Yes",)))
        stubs["ccn -R /c/d1 answer search python3"] = stubs[f"ccn -R /c/d1 answer search {RECORDER}"]
        assert self.kill(tmp_path, stubs) is not None

    @pytest.mark.parametrize(
        ("named", "lane", "lifted"),
        [
            pytest.param("meta-desk", "meta-desk", True, id="the_lane_itself"),
            pytest.param("meta-desk", "meta-desk-3", True, id="a_successor"),
            pytest.param("meta-desk-3", "meta-desk-3", True, id="a_numbered_lane_itself"),
            pytest.param("meta-desk-3", "meta-desk-9", True, id="a_later_successor"),
            pytest.param("meta-desk-9", "meta-desk-3", False, id="a_predecessor"),
            pytest.param("meta-desk-9", "meta-desk", False, id="the_first_of_a_later_lane"),
            pytest.param("meta-desk", "release-2", False, id="another_lane"),
            pytest.param("meta-desk", "desk", False, id="a_stem_inside_a_name"),
            pytest.param("meta-desk", "meta-desk-runner", False, id="a_longer_name"),
        ],
    )
    def test_only_the_lane_on_the_card_and_its_successors_may_use_his_tap(
        self, general_pack: None, tmp_path: Path, named: str, lane: str, lifted: bool
    ) -> None:
        words = f"May I restart it? {named} restarts pid {RECORDER}. Yes No"
        assert (self.kill(tmp_path, tap_stubs(tap_answer(words, ("Yes",))), lane=lane) is None) is lifted
        assert bool(minted("sessions.kill.tap")) is lifted

    def test_a_session_in_no_lane_takes_no_lift(self, general_pack: None, tmp_path: Path) -> None:
        message = self.kill(tmp_path, tap_stubs(tap_answer()), lane=None)
        assert message is not None
        assert "board card" not in message

    def test_a_subagent_of_the_lane_takes_no_lift(self, general_pack: None, tmp_path: Path) -> None:
        stubs = tap_stubs(tap_answer())
        assert self.kill(tmp_path, stubs, agent_id="sub-1", root_transcript=INLINE_TRANSCRIPT) is not None

    def test_one_tap_covers_one_process(self, general_pack: None, tmp_path: Path) -> None:
        stubs = tap_stubs(tap_answer(SCRIPT_WORDS, ("Yes",)))
        assert self.kill(tmp_path, stubs) is None
        assert self.kill(tmp_path, stubs, OTHER_RECORDER) is not None
        assert self.kill(tmp_path, stubs) is None
        assert [ticket.place["process"].partition("@")[0] for ticket in store.tickets("sessions.kill.tap")] == [
            str(RECORDER)
        ]

    def test_a_new_tap_lifts_a_new_kill(self, general_pack: None, tmp_path: Path) -> None:
        first = tap_answer(SCRIPT_WORDS, ("Yes",))
        assert self.kill(tmp_path, tap_stubs(first)) is None
        again = tap_answer(SCRIPT_WORDS, ("Yes",), at=tap_time(2))
        assert self.kill(tmp_path, tap_stubs(again, first), OTHER_RECORDER) is None

    def test_a_process_started_after_his_tap_is_not_the_one_he_saw(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        started = tap_time(1).astimezone(UTC).replace(tzinfo=None).isoformat()
        fake_table["table"] = table(
            *DRIVE_LEFTOVERS.rows.values(), row(RECORDER, 1, "python3 board_watch.py", started=started)
        )
        assert self.kill(tmp_path, tap_stubs(tap_answer())) is not None

    @pytest.mark.parametrize(
        ("zone", "hours_before_tap", "lifted"),
        [
            pytest.param("America/Los_Angeles", 2, True, id="west_of_utc_started_before"),
            pytest.param("Europe/Berlin", -0.05, False, id="east_of_utc_started_after"),
        ],
    )
    def test_start_times_compare_in_utc_whatever_the_macs_zone(
        self,
        general_pack: None,
        fake_table: dict[str, ProcessTable | None],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        zone: str,
        hours_before_tap: float,
        lifted: bool,
    ) -> None:
        at = tap_time()
        started = (at - timedelta(hours=hours_before_tap)).astimezone(UTC).replace(tzinfo=None).isoformat()
        fake_table["table"] = table(
            *DRIVE_LEFTOVERS.rows.values(), row(RECORDER, 1, "python3 board_watch.py", started=started)
        )
        with monkeypatch.context() as zoned:
            zoned.setenv("TZ", zone)
            time.tzset()
            try:
                assert (self.kill(tmp_path, tap_stubs(tap_answer(at=at))) is None) is lifted
            finally:
                zoned.undo()
                time.tzset()

    @pytest.mark.parametrize(
        ("pid", "refusal"),
        [
            pytest.param(14575, "is an agent session", id="live_claude"),
            pytest.param(8311, "is an agent session", id="lone_codex"),
            pytest.param(1743, "the Orca PTY daemon", id="orca_host"),
            pytest.param(14550, "an ancestor of a protected process", id="agent_ancestor"),
            pytest.param(15000, "is a terminal host", id="orca_terminal"),
            pytest.param(52004, "no agent ancestor to vouch for it", id="agent_shim"),
        ],
    )
    def test_what_the_guard_protects_stays_protected_whatever_a_card_says(
        self, general_pack: None, tmp_path: Path, pid: int, refusal: str
    ) -> None:
        stubs = tap_stubs(tap_answer(f"meta-desk ends pid {pid}.", ("Yes",)))
        stubs[f"ccn -R /c/d1 answer search {pid}"] = json.dumps([TAP_ROW])
        assert refusal in (self.kill(tmp_path, stubs, pid) or "")
        assert minted("sessions.kill.tap") == []

    def test_a_board_it_cannot_read_lifts_nothing(self, general_pack: None, tmp_path: Path) -> None:
        assert self.kill(tmp_path, tap_stubs(board=(1, "no such board"))) is not None

    def test_a_ledger_it_cannot_read_lifts_nothing(self, general_pack: None, tmp_path: Path) -> None:
        stubs = tap_stubs(tap_answer()) | {f"ccn -R /c/d1 answer search {RECORDER}": (1, "no ledger")}
        assert self.kill(tmp_path, stubs) is not None


OBSERVED_LANE = "aig-no-delete-plan@session-67c0e5da"
RESUMED_SESSION = "900424b6-7393-480c-a26a-f1bd21da6e57"
STOP_DENIED = (
    "BLOCKED: `TaskStop` on task `wcn64vfub` cannot be verified as a disposable shell task rather than a workflow, "
    "agent, or teammate session, its own children included. Let it finish, ask the root session to stop it, or have "
    "the owner name it in a cc-notes answer for a later session."
)


def lane_stop(tool_input: dict[str, Any], **fields: Any) -> Input:
    return stop(tool_input, **{"agent_id": "lane-1", **fields})


class TestStopTool:
    @pytest.mark.parametrize(
        "tool_input",
        [
            pytest.param({"task_id": "wcn64vfub"}, id="bare"),
            pytest.param({"task_id": "wf_f79d45a5-908"}, id="workflow"),
            pytest.param({"task_id": "a17c64e0029f60e11"}, id="background_agent"),
            pytest.param({"task_id": OBSERVED_LANE}, id="teammate"),
            pytest.param({"shell_id": "bash_3"}, id="shell_alias"),
            pytest.param({}, id="unnamed"),
        ],
    )
    def test_the_root_stops_any_task_unchecked(
        self, general_pack: None, tmp_path: Path, tool_input: dict[str, Any]
    ) -> None:
        assert decide_input(stop(tool_input), tmp_path) is None
        assert decide_input(stop(tool_input), tmp_path, event=Event.PermissionRequest) is None
        assert minted("sessions.task-stop") == []

    def test_an_orca_lane_session_is_no_root(self, general_pack: None, tmp_path: Path) -> None:
        lane = stop({"task_id": "wcn64vfub"}, root_transcript=INLINE_TRANSCRIPT)
        assert decide_input(lane, tmp_path) == STOP_DENIED

    def test_the_observed_bare_task_id_is_denied_to_a_lane(self, general_pack: None, tmp_path: Path) -> None:
        assert decide_input(lane_stop({"task_id": "wcn64vfub"}), tmp_path) == STOP_DENIED

    @pytest.mark.parametrize(
        "task_id",
        [
            pytest.param("wf_f79d45a5-908", id="workflow"),
            pytest.param("a7e6a10ac1f61999f", id="background_agent"),
            pytest.param("daemonkit-cache-impl", id="teammate"),
            pytest.param("b1a2c3d4", id="shell_like"),
        ],
    )
    def test_no_id_shape_proves_a_disposable_shell(self, general_pack: None, tmp_path: Path, task_id: str) -> None:
        assert decide_input(lane_stop({"task_id": task_id}), tmp_path) == STOP_DENIED.replace("wcn64vfub", task_id)

    def test_the_retired_shell_id_alias_is_denied(self, general_pack: None, tmp_path: Path) -> None:
        assert decide_input(lane_stop({"shell_id": "bash_3"}), tmp_path) == STOP_DENIED.replace(
            "task `wcn64vfub`", "shell `bash_3`"
        )

    def test_a_stop_naming_no_target_is_denied(self, general_pack: None, tmp_path: Path) -> None:
        assert decide_input(lane_stop({}), tmp_path) == STOP_DENIED.replace("task `wcn64vfub`", "an unnamed target")

    def test_permission_request_denies_too(self, general_pack: None, tmp_path: Path) -> None:
        denied = decide_input(lane_stop({"task_id": "wcn64vfub"}), tmp_path, event=Event.PermissionRequest)
        assert denied == STOP_DENIED

    def test_the_verdict_needs_no_process_table(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        fake_table["table"] = None
        assert decide_input(lane_stop({"task_id": "wcn64vfub"}), tmp_path) == STOP_DENIED

    def test_reading_task_output_is_allowed(self, general_pack: None, tmp_path: Path) -> None:
        output = Input(tool="TaskOutput", tool_input={"task_id": "wcn64vfub"}, cwd="/w", session_id="s1")
        assert decide_input(output, tmp_path) is None
        assert decide_input(output, tmp_path, event=Event.PermissionRequest) is None

    def test_an_owner_ruling_lifts_one_stop_and_records_its_spend(
        self, general_pack: None, tmp_path: Path, rulings: dict[str, list[dict[str, Any]]]
    ) -> None:
        rulings["wcn64vfub"] = ruling("Stop wcn64vfub; it hung on a dead socket.")
        assert decide_input(lane_stop({"task_id": "wcn64vfub"}), tmp_path) is None
        assert minted("sessions.task-stop") == [("ruling", ["ccn:0207568"])]
        denied = decide_input(lane_stop({"task_id": "b1a2c3d4"}), tmp_path)
        assert denied == STOP_DENIED.replace("wcn64vfub", "b1a2c3d4")

    def test_a_ruling_written_during_the_session_never_lifts_a_stop(
        self, general_pack: None, tmp_path: Path, rulings: dict[str, list[dict[str, Any]]]
    ) -> None:
        rulings["wcn64vfub"] = ruling("Stop wcn64vfub.", written=INLINE_STARTED + timedelta(seconds=1))
        assert decide_input(lane_stop({"task_id": "wcn64vfub"}), tmp_path) == STOP_DENIED
        assert minted("sessions.task-stop") == []

    def test_a_lane_stops_the_teammate_it_spawned_and_records_its_spend(
        self, general_pack: None, tmp_path: Path
    ) -> None:
        own = lane_stop({"task_id": OBSERVED_LANE}, session_id=RESUMED_SESSION, transcript=inline_spawn(OBSERVED_LANE))
        assert decide_input(own, tmp_path) is None
        assert decide_input(own, tmp_path, event=Event.PermissionRequest) is None
        assert minted("sessions.task-stop") == [("spawned-here", [f"teammate:{OBSERVED_LANE}"])]

    @pytest.mark.parametrize(
        ("task_id", "fields"),
        [
            pytest.param(OBSERVED_LANE, {}, id="never-spawned"),
            pytest.param(
                OBSERVED_LANE, {"transcript": inline_spawn("aig-no-delete-plan-2@session-67c0e5da")}, id="another-name"
            ),
            pytest.param(
                "aig-no-delete-plan@session-756e25cc", {"transcript": inline_spawn(OBSERVED_LANE)}, id="foreign-session"
            ),
            pytest.param(
                OBSERVED_LANE,
                {"agent_id": "sibling", "root_transcript": inline_spawn(OBSERVED_LANE)},
                id="root-spawned-it",
            ),
            pytest.param("aig-no-delete-plan", {"transcript": inline_spawn("aig-no-delete-plan")}, id="bare-id"),
            pytest.param(
                OBSERVED_LANE, {"transcript": inline_spawn(OBSERVED_LANE, status="async_launched")}, id="not-a-teammate"
            ),
        ],
    )
    def test_a_task_this_lane_did_not_spawn_as_its_teammate_stays_protected(
        self, general_pack: None, tmp_path: Path, task_id: str, fields: dict[str, Any]
    ) -> None:
        denied = decide_input(lane_stop({"task_id": task_id}, session_id=RESUMED_SESSION, **fields), tmp_path)
        assert denied == STOP_DENIED.replace("wcn64vfub", task_id)
        assert minted("sessions.task-stop") == []


class TestTerminalClose:
    def test_an_owner_ruling_closes_the_terminal_it_names(
        self,
        general_pack: None,
        agent_table: None,
        tmp_path: Path,
        rulings: dict[str, list[dict[str, Any]]],
    ) -> None:
        rulings[INLINE_OWNER_TERMINAL] = ruling(f"Owner: close exactly {INLINE_OWNER_TERMINAL}.")
        with stubbed_commands(INLINE_COMMANDS | inline_tab(INLINE_OWNER_TERMINAL, INLINE_OWNER_TERMINAL)):
            assert decide(OWNER_CLOSE, tmp_path) is None
            again = envelope_of(bash(f"{OWNER_CLOSE} --tab"), tmp_path)
        assert minted("sessions.close") == [("ruling", ["ccn:0207568"]), ("ruling", ["ccn:0207568"])]
        assert again is None

    def test_a_close_a_ruling_names_never_waits_on_orca_for_the_process_tree(
        self,
        general_pack: None,
        agent_table: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        rulings: dict[str, list[dict[str, Any]]],
    ) -> None:
        rulings["term_agent"] = ruling("Owner: close exactly term_agent.")
        asked: list[tuple[str, ...]] = []
        document = _sessions.orca_document

        def recorded(argv: tuple[str, ...], unset: tuple[str, ...], ceiling: float = _sessions.PROBE_TIMEOUT) -> Any:
            asked.append(argv)
            return document(argv, unset, ceiling)

        monkeypatch.setattr(_sessions, "orca_document", recorded)
        with stubbed_commands(INLINE_COMMANDS):
            assert decide(AGENT_CLOSE, tmp_path) is None
        assert ("orca", "terminal", "show", "--terminal", "term_agent", "--json") not in asked

    def test_a_ruling_naming_another_terminal_keeps_the_block(
        self,
        general_pack: None,
        agent_table: None,
        tmp_path: Path,
        rulings: dict[str, list[dict[str, Any]]],
    ) -> None:
        rulings["term_agent"] = ruling("Close term_agent2 and nothing else.")
        with stubbed_commands(INLINE_COMMANDS):
            message = decide(AGENT_CLOSE, tmp_path)
        assert message is not None
        assert "where pid 16002 (`claude" in message
        assert CLOSE_FIX in message

    def test_a_ruling_written_after_the_session_started_keeps_the_block(
        self,
        general_pack: None,
        agent_table: None,
        tmp_path: Path,
        rulings: dict[str, list[dict[str, Any]]],
    ) -> None:
        rulings["term_agent"] = ruling("Close term_agent.", written=INLINE_STARTED + timedelta(minutes=1))
        with stubbed_commands(INLINE_COMMANDS):
            message = decide(AGENT_CLOSE, tmp_path)
        assert message is not None
        assert CLOSE_FIX in message
        assert minted("sessions.close") == []

    def test_an_unreadable_ruling_keeps_the_block_and_tells_the_user(
        self,
        general_pack: None,
        agent_table: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(context_module, "ccn_answers", REAL_CCN_ANSWERS)
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))
        orca = {argv: stdout for argv, stdout in INLINE_COMMANDS.items() if not argv.startswith("ccn")}
        with stubbed_commands(orca):
            envelope = envelope_of(bash(AGENT_CLOSE), tmp_path)
        assert envelope is not None
        assert CLOSE_FIX in (reason(envelope) or "")
        assert "sessions: The ticket check failed (FileNotFoundError" in envelope["systemMessage"]

    def test_a_batch_of_closes_stays_blocked_when_a_ruling_names_both(
        self,
        general_pack: None,
        agent_table: None,
        tmp_path: Path,
        rulings: dict[str, list[dict[str, Any]]],
    ) -> None:
        both = ruling(f"Close {INLINE_OWNER_TERMINAL} and term_agent.")
        rulings[INLINE_OWNER_TERMINAL] = rulings["term_agent"] = both
        with stubbed_commands(INLINE_COMMANDS):
            message = decide(f"{OWNER_CLOSE}; {AGENT_CLOSE}", tmp_path)
            assert decide(OWNER_CLOSE, tmp_path) is None
            assert decide(AGENT_CLOSE, tmp_path) is None
        assert message is not None
        assert "leave ending them to the owner" in message
        assert [ticket.place["terminal"] for ticket in store.tickets("sessions.close")] == [
            INLINE_OWNER_TERMINAL,
            "term_agent",
        ]

    def test_a_class_ruling_closes_every_settled_dispatchs_idle_terminal_from_one_ticket(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        fake_table["table"] = TWO_AGENT_TERMINALS
        for handle, status, stage in (
            ("term_agent", "completed", "settled"),
            ("term_shim", "failed", "agent_readiness"),
        ):
            with stubbed_commands(SETTLED | {"orca orchestration worker-list": inline_worker(handle, status, stage)}):
                close = bash(f"orca terminal close --terminal {handle} --json", llm=CLASS_ALLOW)
                assert decide_input(close, tmp_path, env=ROOT) is None
        [ticket] = store.tickets("sessions.close-settled")
        assert ticket.reach is Reach.PLACE and ticket.place == {}
        assert [item.id for item in ticket.evidence] == ["ccn:c9b27c1"]
        assert minted("sessions.close") == []

    @pytest.mark.parametrize(
        ("commands", "llm", "env", "fields"),
        [
            pytest.param(
                {"orca orchestration worker-list": inline_worker("term_agent", "dispatched", "input_accepted")},
                CLASS_ALLOW,
                ROOT,
                {},
                id="live-dispatch",
            ),
            pytest.param({"orca terminal read": INLINE_BUSY}, CLASS_ALLOW, ROOT, {}, id="busy-agent"),
            pytest.param(
                inline_class_rulings(written=INLINE_STARTED + timedelta(minutes=1)),
                CLASS_ALLOW,
                ROOT,
                {},
                id="stale-ruling",
            ),
            pytest.param({}, {}, ROOT, {}, id="judge-declines"),
            pytest.param(
                {"orca orchestration worker-list": _sessions.inline_workers()}, CLASS_ALLOW, ROOT, {}, id="no-dispatch"
            ),
            pytest.param({}, CLASS_ALLOW, {"ORCA_TERMINAL_HANDLE": "term_lane"}, {}, id="sibling-lane-terminal"),
            pytest.param({}, CLASS_ALLOW, ROOT, {"agent_id": "sibling-lane"}, id="in-process-lane"),
            pytest.param({}, CLASS_ALLOW, {}, {}, id="outside-orca"),
            pytest.param(inline_run("term_other_root"), CLASS_ALLOW, ROOT, {}, id="another-runs-root"),
        ],
    )
    def test_a_class_ruling_never_lifts_a_close_the_orca_record_or_the_judge_rules_out(
        self,
        general_pack: None,
        agent_table: None,
        tmp_path: Path,
        commands: dict[str, str],
        llm: dict[str, Any],
        env: dict[str, str],
        fields: dict[str, Any],
    ) -> None:
        settled = {"orca orchestration worker-list": inline_worker("term_agent", "completed")}
        with stubbed_commands(SETTLED | settled | commands):
            message = decide_input(bash(AGENT_CLOSE, llm=llm, **fields), tmp_path, env=env)
        assert message is not None
        assert "where pid 16002" in message or _sessions.SETTLED_CLOSE.deny in message
        assert store.tickets("sessions.close-settled") == []

    @pytest.mark.parametrize(
        ("idles", "workers"),
        [
            pytest.param([True, False], [SETTLED_WORKER, SETTLED_WORKER], id="goes-busy"),
            pytest.param(
                [True, True],
                [SETTLED_WORKER, {**SETTLED_WORKER, "dispatchId": "ctx_next", "dispatchStatus": "dispatched"}],
                id="re-dispatched",
            ),
            pytest.param(
                [True, True], [SETTLED_WORKER, {**SETTLED_WORKER, "dispatchId": "ctx_next"}], id="another-dispatch"
            ),
        ],
    )
    def test_a_class_lift_rechecks_the_orca_record_after_the_judge_allows(
        self,
        general_pack: None,
        agent_table: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        idles: list[bool],
        workers: list[dict[str, str]],
    ) -> None:
        monkeypatch.setattr(_sessions, "idle", lambda handle, seen=iter(idles): next(seen))
        monkeypatch.setattr(_sessions, "worker_of", lambda handle, run=None, seen=iter(workers): next(seen))
        monkeypatch.setattr(orca_tickets, "resolve", lambda handle: (None, None))
        with stubbed_commands(SETTLED):
            envelope = envelope_of(bash(AGENT_CLOSE, llm=CLASS_ALLOW), tmp_path, env=ROOT)
        assert envelope is not None
        assert "where pid 16002" in (reason(envelope) or "")
        assert "changed while the judge decided" in envelope["systemMessage"]
        assert [ticket.rule for ticket in store.tickets("sessions.close-settled")] == ["class-ruling"]

    @pytest.mark.parametrize(
        "command",
        [
            f"{ORCA_GC} --run run_7715a23a5657 --dispatch ctx_d83bbb927995",
            f"{ORCA_GC} --run run_7715a23a5657 --dispatch ctx_1 --dispatch ctx_2",
            f"/Users/dev/.claude/plugins/cache/skills/long-running/0.7.1/bin/{ORCA_GC} --run run_1",
            f"cd /Users/dev/monorepo && {ORCA_GC} --run run_1 --dispatch ctx_1 2>&1 | tail -20",
        ],
    )
    def test_the_roots_orca_gc_run_is_not_refused(
        self, general_pack: None, agent_table: None, tmp_path: Path, command: str
    ) -> None:
        with stubbed_commands(INLINE_COMMANDS):
            assert decide(command, tmp_path) is None

    def test_an_idle_terminal_this_session_created_closes_and_records_its_spend(
        self, general_pack: None, agent_table: None, tmp_path: Path
    ) -> None:
        with stubbed_commands(INLINE_COMMANDS):
            assert decide_input(bash(AGENT_CLOSE, transcript=inline_create("term_agent")), tmp_path) is None
        assert minted("sessions.close") == [("created-here", ["created:term_agent"])]

    @pytest.mark.parametrize(
        ("transcript", "commands"),
        [
            pytest.param(inline_create("term_other"), {}, id="another-terminal"),
            pytest.param(INLINE_TRANSCRIPT, {}, id="never-created"),
            pytest.param(inline_create("term_agent"), {"orca terminal read": INLINE_BUSY}, id="busy"),
            pytest.param(
                inline_create("term_agent"),
                {"orca orchestration worker-list": _sessions.inline_workers("term_agent")},
                id="dispatched",
            ),
            pytest.param(
                inline_create("term_agent"),
                {"orca terminal wait": json.dumps({"ok": True, "result": {"wait": {"satisfied": False}}})},
                id="not-tui-idle",
            ),
        ],
    )
    def test_a_terminal_that_is_no_idle_terminal_this_session_created_keeps_the_block(
        self,
        general_pack: None,
        agent_table: None,
        tmp_path: Path,
        transcript: list[dict[str, Any]],
        commands: dict[str, str],
    ) -> None:
        with stubbed_commands(INLINE_COMMANDS | commands):
            message = decide_input(bash(AGENT_CLOSE, transcript=transcript), tmp_path)
        assert message is not None
        assert "where pid 16002" in message
        assert minted("sessions.close") == []

    @pytest.mark.parametrize(
        ("tree", "allowed"), [("s1", True), ("s2", False)], ids=["this-session", "another-session"]
    )
    def test_a_terminal_another_lane_of_this_session_created_closes_from_any_of_its_agents(
        self,
        general_pack: None,
        fake_table: dict[str, ProcessTable | None],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        tree: str,
        allowed: bool,
    ) -> None:
        opened = (datetime.now(UTC) - timedelta(minutes=1)).replace(tzinfo=None).isoformat()
        fake_table["table"] = table(
            *MAC.rows.values(),
            row(16000, 1743, f"{INLINE_LOGIN} /opt/homebrew/bin/fish", uid=0, started=opened),
            row(16001, 16000, "-/opt/homebrew/bin/fish -l", started=opened),
            row(16002, 16001, "claude --dangerously-skip-permissions --effort xhigh", started=opened),
        )
        root = tmp_path / "projects" / "s1.jsonl"
        lane = tmp_path / "projects" / tree / "subagents" / "agent-builder.jsonl"
        lane.parent.mkdir(parents=True)
        lane.write_bytes(fixture_file(inline_create("term_agent")).read_bytes())
        evt = input_to_event(Event.PreToolUse, bash(AGENT_CLOSE, agent_id="closer"))
        evt._raw["transcript_path"] = str(root)
        with monkeypatch.context() as zone:
            zone.setenv("TZ", "America/Los_Angeles")
            time.tzset()
            with stubbed_commands(INLINE_COMMANDS), reqenv.use_request(overrides()):
                message = reason(dispatch(Event.PreToolUse, evt, session_dir=tmp_path))
        time.tzset()
        assert (message is None) is allowed
        details = [item.detail for ticket in store.tickets("sessions.close") for item in ticket.evidence]
        assert [str(lane) in detail for detail in details] == ([True] if allowed else [])

    @pytest.mark.parametrize(("offset", "allowed"), [(60, True), (600, False)], ids=["during", "after"])
    def test_an_orca_launch_receipt_written_during_the_call_proves_creation(
        self,
        general_pack: None,
        agent_table: None,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        offset: int,
        allowed: bool,
    ) -> None:
        home = tmp_path / "home"
        receipt = home / ".claude/scratch/orca-launch/run_1/lane-a.terminal.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({"ok": True, "result": {"terminal": {"handle": "term_agent"}}}))
        written = (INLINE_STARTED + timedelta(seconds=offset)).timestamp()
        os.utime(receipt, (written, written))
        monkeypatch.setenv("HOME", str(home))
        launch = inline_create("term_agent", "ORCA_LAUNCH_RUN=run_1 orca-launch.sh lane-a opus xhigh /tmp/brief.md")
        launch[1]["timestamp"] = (INLINE_STARTED + timedelta(seconds=10)).isoformat()
        launch[2]["timestamp"] = (INLINE_STARTED + timedelta(seconds=120)).isoformat()
        launch[2]["message"]["content"][0]["content"] = (
            "lane-a failed worker-start: boom; rollback left terminal=term_agent"
        )
        with stubbed_commands(INLINE_COMMANDS):
            message = decide_input(bash(AGENT_CLOSE, transcript=launch), tmp_path)
        assert (message is None) is allowed

    def test_a_terminal_hosting_no_agent_closes(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        fake_table["table"] = TERMINALS
        with stubbed_commands(INLINE_COMMANDS):
            assert decide(IDLE_CLOSE, tmp_path) is None
            fake_table["table"] = table(*TERMINALS.rows.values(), row(15002, 15001, "codex exec review"))
            message = decide(IDLE_CLOSE, tmp_path)
        assert message is not None
        assert "where pid 15002 (`codex exec review`) runs" in message
        assert CLOSE_FIX in message

    def test_a_bare_shell_pid_still_needs_creation_proof(
        self, general_pack: None, fake_table: dict[str, ProcessTable | None], tmp_path: Path
    ) -> None:
        fake_table["table"] = TERMINALS
        message = decide("kill 15001", tmp_path)
        assert message is not None
        assert "no agent ancestor to vouch for it" in message

    def test_an_unavailable_orca_denies(
        self,
        general_pack: None,
        fake_table: dict[str, ProcessTable | None],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_table["table"] = TERMINALS
        failing_run(monkeypatch, "orca", FileNotFoundError("orca"))
        message = decide(IDLE_CLOSE, tmp_path)
        assert message is not None
        assert "terminal `term_idle` (`orca` is not installed on the hook's PATH)" in message

    def test_an_orca_lookup_outlasting_the_call_blocks_it_and_answers_the_retry(
        self, general_pack: None, agent_table: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answered = threading.Event()
        asked: list[tuple[str, ...]] = []
        document = _sessions.orca_document

        def slow(argv: tuple[str, ...], unset: tuple[str, ...], ceiling: float = _sessions.PROBE_TIMEOUT) -> Any:
            asked.append(argv)
            answered.wait()
            return document(argv, unset, ceiling)

        monkeypatch.setattr(_sessions, "orca_document", slow)
        evt = input_to_event(Event.PreToolUse, bash(AGENT_CLOSE))
        left = SYNC_DEADLINE_MARGIN_SECONDS + _sessions.ORCA_VERDICT_RESERVE + 1.0
        with stubbed_commands(INLINE_COMMANDS):
            with reqenv.use_request(overrides()), reqenv.deadline_in(left):
                first = reason(dispatch(Event.PreToolUse, evt, session_dir=tmp_path))
            answered.set()
            retry = decide(AGENT_CLOSE, tmp_path)
        assert first is not None
        assert "closes terminal `term_agent` (Orca did not answer in " in first
        assert "Retry the same call" in first
        assert retry is not None
        assert "where pid 16002" in retry
        assert asked.count(("orca", "terminal", "show", "--terminal", "term_agent", "--json")) == 1

    def test_an_unreadable_process_table_denies_without_asking_orca(
        self,
        general_pack: None,
        fake_table: dict[str, ProcessTable | None],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_table["table"] = None
        failing_run(monkeypatch, "", AssertionError("no subprocess may run"))
        message = decide(IDLE_CLOSE, tmp_path)
        assert message is not None
        assert "the process table could not be read" in message

    def test_a_deadline_too_close_denies_without_probing(
        self,
        general_pack: None,
        fake_table: dict[str, ProcessTable | None],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        failing_run(monkeypatch, "", AssertionError("no subprocess may run"))
        evt = input_to_event(Event.PreToolUse, bash(OWNER_CLOSE))
        with reqenv.use_request(overrides()), reqenv.deadline_in(SYNC_DEADLINE_MARGIN_SECONDS + 0.45):
            message = reason(dispatch(Event.PreToolUse, evt, session_dir=tmp_path))
        assert message is not None
        assert "the caller deadline is too close to read the process table" in message


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

    def test_interleaved_payloads_each_keep_their_own_scan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[float] = []
        monkeypatch.setattr(proc, "process_table", lambda **kw: (seen.append(kw["timeout"]), MAC)[1])
        first = input_to_event(Event.PreToolUse, Input(command="kill 31337", cwd="/w", session_id="s1"))
        other = input_to_event(Event.PreToolUse, Input(command="renice -n 5 -p 31337", cwd="/w", session_id="s1"))
        with reqenv.use_request(overrides()):
            scans = [Scan.of(first), Scan.of(other), Scan.of(first)]
            for scan in scans:
                assert scan.facts.ownership is scan.facts.ownership
        assert scans[0] is scans[2]
        assert scans[1] is not scans[0]
        assert seen == [2.0, 2.0]


class ScanPayload(dict[str, Any]):
    pass


class TestScanLifetime:
    def test_owned_resources_do_not_change_event_construction_repr_or_equality(self) -> None:
        evt = input_to_event(Event.PreToolUse, Input(command="kill 31337", cwd="/w", session_id="s1"))
        other = type(evt)(evt._raw, evt.ctx)
        Scan.of(evt)
        assert tuple(signature(type(evt)).parameters) == ("_raw", "ctx")
        assert evt == other
        assert "_resources=" not in repr(evt)

    def test_changing_a_forks_raw_payload_keeps_both_live_scans_separate(self) -> None:
        evt = input_to_event(Event.PreToolUse, Input(command="kill 31337", cwd="/w", session_id="s1"))
        fork = copy(evt)
        first = Scan.of(evt)
        fork._raw = input_to_event(
            Event.PreToolUse, Input(command="renice -n 5 -p 31337", cwd="/w", session_id="s1")
        )._raw
        fork.__dict__.pop("input", None)
        fork.__dict__.pop("cmd", None)
        second = Scan.of(fork)
        assert first is not second
        assert first.raw is evt._raw
        assert second.raw is fork._raw
        assert Scan.of(evt) is first
        assert Scan.of(fork) is second

    def test_scan_ownership_does_not_form_an_event_reference_cycle(self) -> None:
        evt = input_to_event(Event.PreToolUse, Input(command="kill 31337", cwd="/w", session_id="s1"))
        event = weakref.ref(evt)
        scan = weakref.ref(Scan.of(evt))
        del evt
        assert event() is None
        assert scan() is None

    @pytest.mark.parametrize("bound", [False, True], ids=["unbound", "request"])
    def test_serial_forks_share_the_parent_operation_owner(self, bound: bool) -> None:
        evt = input_to_event(Event.PreToolUse, Input(command="kill 31337", cwd="/w", session_id="s1"))
        with reqenv.use_request(overrides()) if bound else nullcontext():
            first = copy(evt)
            retained = weakref.ref(Scan.of(first))
            del first
            gc.collect()
            assert retained() is not None
            second = copy(evt)
            assert Scan.of(second) is retained()
            del second
        del evt
        gc.collect()
        assert retained() is None

    def test_shallow_forks_own_the_shared_scan_until_the_last_event_is_released(self) -> None:
        evt = input_to_event(Event.PreToolUse, Input(command="kill 31337", cwd="/w", session_id="s1"))
        scan = Scan.of(evt)
        retained = weakref.ref(scan)
        fork = copy(evt)
        del evt, scan
        gc.collect()
        assert retained() is not None
        assert Scan.of(fork) is retained()
        del fork
        gc.collect()
        assert retained() is None

    @pytest.mark.parametrize("bound", [False, True], ids=["unbound", "request"])
    def test_finished_events_do_not_retain_payloads(self, bound: bool) -> None:
        def finish() -> weakref.ReferenceType[ScanPayload]:
            evt = input_to_event(Event.PreToolUse, Input(command="kill 31337", cwd="/w", session_id="s1"))
            raw = ScanPayload(evt._raw, unused="x" * 1024 * 1024)
            evt._raw = raw
            payload = weakref.ref(raw)
            with reqenv.use_request(overrides()) if bound else nullcontext():
                assert Scan.of(evt).raw is evt._raw
            return payload

        payload = finish()
        gc.collect()
        assert payload() is None

    @pytest.mark.parametrize("bound", [False, True], ids=["unbound", "request"])
    def test_live_events_keep_their_scan_without_count_eviction(
        self, bound: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parsed: list[str] = []
        original = _sessions.safe_parse_command_line

        def parse(command: str) -> CommandLine | None:
            parsed.append(command)
            return original(command)

        monkeypatch.setattr(_sessions, "safe_parse_command_line", parse)
        events = [
            input_to_event(Event.PreToolUse, Input(command=f"kill {31337 + index}", cwd="/w", session_id="s1"))
            for index in range(32)
        ]
        with reqenv.use_request(overrides()) if bound else nullcontext():
            first = Scan.of(events[0])
            for evt in events[1:]:
                Scan.of(evt)
            assert Scan.of(events[0]) is first
        assert len(parsed) == len(events)

    @pytest.mark.parametrize("bound", [False, True], ids=["unbound", "request"])
    def test_concurrent_forks_share_one_scan_and_guarded_fact_read(
        self, bound: bool, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parsed: list[str] = []
        observed: list[float] = []
        original = _sessions.safe_parse_command_line
        entered, release = threading.Event(), threading.Event()

        def parse(command: str) -> CommandLine | None:
            parsed.append(command)
            entered.set()
            assert release.wait(10)
            return original(command)

        monkeypatch.setattr(_sessions, "safe_parse_command_line", parse)
        monkeypatch.setattr(proc, "process_table", lambda **kw: (observed.append(kw["timeout"]), MAC)[1])
        evt = input_to_event(Event.PreToolUse, Input(command="kill 31337", cwd="/w", session_id="s1"))
        forks = [copy(evt) for _ in range(8)]
        with (
            reqenv.use_request(overrides()) if bound else nullcontext(),
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            pending = [pool.submit(copy_context().run, Scan.of, fork) for fork in forks]
            assert entered.wait(10)
            release.set()
            scans = [future.result(timeout=10) for future in pending]
            for scan in scans:
                assert scan.facts.ownership is scan.facts.ownership
        assert all(scan is scans[0] for scan in scans)
        assert parsed == ["kill 31337"]
        assert observed == [2.0]

    def test_request_payloads_remain_independent_during_fanout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(proc, "process_table", lambda **kw: MAC)
        events = [
            input_to_event(Event.PreToolUse, Input(command=command, cwd="/w", session_id=session))
            for command, session in [("kill 31337", "s1"), ("renice -n 5 -p 31337", "s2")]
        ]

        def scan(evt: BaseHookEvent) -> Scan:
            with reqenv.use_request(overrides()):
                result = Scan.of(evt)
                assert result.facts.ownership is result.facts.ownership
                return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(scan, events))
        assert results[0] is not results[1]
        assert [result.facts.session for result in results] == ["s1", "s2"]
        assert [result.raw for result in results] == [evt._raw for evt in events]


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


class TestLaunchdDashboard:
    @pytest.mark.parametrize(
        "label",
        [
            "com.long-running.durable-agents-rebuild.dashboard",
            "gui/$(id -u)/com.long-running.durable-agents-rebuild.dashboard",
        ],
    )
    def test_a_dashboard_kickstart_names_the_verb_that_restarts_it(
        self, general_pack: None, tmp_path: Path, label: str
    ) -> None:
        target = label if label.startswith("gui/") else f"gui/501/{label}"
        message = decide(f"launchctl kickstart -k {target}", tmp_path)
        assert message is not None
        assert "`lr dashboard restart`" in message

    @pytest.mark.parametrize(
        "label",
        [
            "com.example.orca-serve",
            "com.apple.Terminal",
            "com.long-running.durable-agents-rebuild.supervisor",
            "com.long-running.durable-agents-rebuild.dashboard-desk",
        ],
    )
    def test_any_other_kickstart_still_blocks_without_the_verb(
        self, general_pack: None, tmp_path: Path, label: str
    ) -> None:
        message = decide(f"launchctl kickstart -k gui/501/{label}", tmp_path)
        assert message is not None
        assert "may host sessions" in message
        assert "lr dashboard restart" not in message


SERVE_KICK = "launchctl kickstart -k system/com.example.orca-serve"
SERVE_WEDGED = "kickstart com.example.orca-serve, it is wedged"
OWNER_AT = INLINE_STARTED + timedelta(minutes=5)


def owner_turn(text: str) -> list[dict[str, Any]]:
    stamp = OWNER_AT.isoformat().replace("+00:00", "Z")
    return [{"type": "user", "timestamp": stamp, "message": {"role": "user", "content": text}}]


def owner_allows(text: str) -> dict[str, Any]:
    return {"allow": True, "reason": "The owner named it.", "relied_on": [said_evidence(text, OWNER_AT, "prompt").id]}


class TestOwnerNamedLifts:
    def test_the_root_restarts_a_service_the_owner_named(self, general_pack: None, tmp_path: Path) -> None:
        said = said_evidence(SERVE_WEDGED, OWNER_AT, "prompt").id
        kick = bash(SERVE_KICK, transcript=owner_turn(SERVE_WEDGED), llm=owner_allows(SERVE_WEDGED))
        assert decide_input(kick, tmp_path) is None
        assert minted("sessions.launchctl.owner") == [("user-named", [said])]
        bootout = bash(
            "launchctl bootout system/com.example.orca-serve",
            transcript=owner_turn(SERVE_WEDGED),
            llm=owner_allows(SERVE_WEDGED),
        )
        assert decide_input(bootout, tmp_path) is not None
        assert minted("sessions.launchctl.owner") == [("user-named", [said])]

    def test_a_lane_never_takes_the_owner_named_lift(self, general_pack: None, tmp_path: Path) -> None:
        lane = bash(
            SERVE_KICK,
            agent_id="lane-1",
            transcript=INLINE_TRANSCRIPT,
            root_transcript=owner_turn(SERVE_WEDGED),
            llm=owner_allows(SERVE_WEDGED),
        )
        assert decide_input(lane, tmp_path) is not None
        assert minted("sessions.launchctl.owner") == []

    def test_words_naming_another_service_lift_nothing(self, general_pack: None, tmp_path: Path) -> None:
        other = "kickstart com.example.orca-serve-2"
        kick = bash(SERVE_KICK, transcript=owner_turn(other), llm=owner_allows(other))
        assert decide_input(kick, tmp_path) is not None
        assert minted("sessions.launchctl.owner") == []

    def test_a_judge_refusal_reaches_the_user(self, general_pack: None, tmp_path: Path) -> None:
        verdict = {
            "allow": False,
            "reason": "describes",
            "refusal": "The owner described the service but never asked to restart it.",
        }
        kick = bash(SERVE_KICK, transcript=owner_turn(f"{SERVE_WEDGED}?"), llm=verdict)
        envelope = envelope_of(kick, tmp_path)
        assert envelope is not None and "may host sessions" in (reason(envelope) or "")
        assert "sessions.launchctl.owner: user-named: nothing found" in envelope["systemMessage"]


ORCA_SERVER = 3853
ORCA_SERVER_TABLE = table(
    *MAC.rows.values(),
    row(77636, 1, "bash /Users/yasyf/.local/bin/orca-serve-supervisor --pairing-address 100.113.56.52"),
    row(
        3832,
        77636,
        "/Applications/Orca.app/Contents/MacOS/Orca "
        "/Applications/Orca.app/Contents/Resources/app.asar.unpacked/out/cli/index.js serve --pairing-address 1.1.1.1",
    ),
    row(
        ORCA_SERVER,
        3832,
        "/Applications/Orca.app/Contents/MacOS/Orca --user-data-dir=/Users/yasyf/Library/Application Support/orca "
        "--serve --serve-pairing-address 100.1.1.1",
    ),
)
SERVER_ORDER = (
    "Also restart the orca server (app server, with the launch daemon, not sipervisor or any sessions or agents)"
)


def ordered_turns(words: str) -> list[dict[str, Any]]:
    said = datetime.now(UTC) - timedelta(minutes=10)
    return [
        {
            "type": "user",
            "timestamp": (said - timedelta(hours=1)).isoformat(),
            "message": {"role": "user", "content": "Hi."},
        },
        {
            "type": "assistant",
            "timestamp": (said - timedelta(minutes=30)).isoformat(),
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello."}]},
        },
        {"type": "user", "timestamp": said.isoformat(), "message": {"role": "user", "content": words}},
    ]


class TestOrderedServerRestart:
    @pytest.fixture(autouse=True)
    def server(
        self, home: Path, fake_table: dict[str, ProcessTable | None], rulings: dict[str, list[dict[str, Any]]]
    ) -> None:
        fake_table["table"] = ORCA_SERVER_TABLE
        lock = home / "Library" / "Application Support" / "orca" / "SingletonLock"
        lock.parent.mkdir(parents=True)
        lock.symlink_to(f"Mac.local-{ORCA_SERVER}")
        body = f'Terminal: "{SERVER_ORDER}". The Orca app server is pid {ORCA_SERVER}, holding the profile lock.'
        rulings[str(ORCA_SERVER)] = ruling(body, written=datetime.now(UTC) - timedelta(minutes=5))

    def kill(self, pid: int, words: str) -> Input:
        return bash(
            f"kill -TERM {pid}", transcript=ordered_turns(words), llm={"allow": True, "relied_on": [SERVER_ORDER]}
        )

    def test_his_recorded_order_lifts_the_kill_of_the_lock_holder(self, general_pack: None, tmp_path: Path) -> None:
        assert decide_input(self.kill(ORCA_SERVER, f"Also merge it. {SERVER_ORDER}"), tmp_path) is None
        assert [rule for rule, _ in minted("sessions.kill.owner")] == ["order-recorded"]

    def test_a_note_without_his_words_keeps_the_block(self, general_pack: None, tmp_path: Path) -> None:
        message = decide_input(self.kill(ORCA_SERVER, "Also merge it."), tmp_path)
        assert message is not None and "name this pid in a cc-notes answer quoting the owner verbatim" in message
        assert minted("sessions.kill.owner") == []

    def test_any_other_orca_process_keeps_the_block(self, general_pack: None, tmp_path: Path) -> None:
        message = decide_input(self.kill(3832, SERVER_ORDER), tmp_path)
        assert message is not None and "Ask the owner to end it." in message
        assert minted("sessions.kill.owner") == []


def finished(inp: Input, response: dict[str, Any], tmp_path: Path) -> None:
    evt = input_to_event(Event.PostToolUse, inp)
    evt._raw["tool_response"] = response
    with reqenv.use_request(overrides()):
        dispatch(Event.PostToolUse, evt, session_dir=tmp_path)


class TestRecordedChildren:
    def test_a_teammate_spawn_recorded_before_compaction_still_lifts_its_stop(
        self, general_pack: None, tmp_path: Path
    ) -> None:
        spawn = Input(
            tool="Agent",
            tool_input={"name": "aig-no-delete-plan", "prompt": "plan"},
            session_id="s1",
            cwd="/w",
            agent_id="lane-1",
        )
        finished(spawn, {"status": "teammate_spawned", "teammate_id": OBSERVED_LANE}, tmp_path)
        assert decide_input(lane_stop({"task_id": OBSERVED_LANE}), tmp_path) is None
        assert decide_input(lane_stop({"task_id": OBSERVED_LANE}, agent_id="lane-2"), tmp_path) is not None

    def test_a_background_shell_this_lane_started_stops(self, general_pack: None, tmp_path: Path) -> None:
        shell = Input(command="sleep 600", session_id="s1", cwd="/w", agent_id="lane-1")
        finished(shell, {"stdout": "", "backgroundTaskId": "blmtrzuxz"}, tmp_path)
        assert decide_input(lane_stop({"task_id": "blmtrzuxz"}), tmp_path) is None
        assert decide_input(lane_stop({"task_id": "blmtrzuxz"}, agent_id="lane-2"), tmp_path) is not None
        assert minted("sessions.task-stop") == [("shell", ["shell:blmtrzuxz"])]

    def test_a_monitor_this_lane_armed_stops(self, general_pack: None, tmp_path: Path) -> None:
        monitor = Input(
            tool="Monitor",
            tool_input={"command": "tail -f run.log", "description": "run log"},
            session_id="s1",
            cwd="/w",
            agent_id="lane-1",
        )
        finished(monitor, {"taskId": "b166f8m04", "timeoutMs": 1800000, "persistent": False}, tmp_path)
        assert decide_input(lane_stop({"task_id": "b166f8m04"}), tmp_path) is None
        assert decide_input(lane_stop({"task_id": "b166f8m04"}, agent_id="lane-2"), tmp_path) is not None
        assert minted("sessions.task-stop") == [("shell", ["shell:b166f8m04"])]

    def test_a_monitor_stops_after_its_lane_moves_to_another_coordinator(
        self, general_pack: None, tmp_path: Path
    ) -> None:
        root = fixture_file(INLINE_TRANSCRIPT)
        monitor = Input(
            tool="Monitor",
            tool_input={"command": "cci watch", "description": "bus"},
            session_id="s1",
            cwd="/w",
            root_transcript=root,
        )
        finished(monitor, {"taskId": "bxaw18znb", "timeoutMs": 1800000, "persistent": False}, tmp_path)
        moved = stop({"task_id": "bxaw18znb"}, root_transcript=root)
        assert decide_input(moved, tmp_path) is None
        foreign = stop({"task_id": "bxaw18znb"}, session_id="s2", root_transcript=root)
        assert decide_input(foreign, tmp_path) is not None
        assert minted("sessions.task-stop") == [("shell", ["shell:bxaw18znb"])]

    def test_an_agent_payload_naming_a_task_id_records_no_shell(self, general_pack: None, tmp_path: Path) -> None:
        spawn = Input(
            tool="Agent", tool_input={"name": "lane", "prompt": "plan"}, session_id="s1", cwd="/w", agent_id="lane-1"
        )
        finished(spawn, {"status": "async_launched", "taskId": "b166f8m04"}, tmp_path)
        assert decide_input(lane_stop({"task_id": "b166f8m04"}), tmp_path) is not None
