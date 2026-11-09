from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from cc_transcript.ids import SessionId

from captain_hook.app import on
from captain_hook.builtin_packs.general.hooks import _sessions
from captain_hook.builtin_packs.general.hooks._sessions import TimedOut
from captain_hook.cli import dispatch_event
from captain_hook.orca import READINGS, coordinator_transcript
from captain_hook.session import ensure_session
from captain_hook.snapshots.client import CURRENT_CLIENT
from captain_hook.testing.helpers import stubbed_commands
from captain_hook.testing.snapshots import FixtureOwner
from captain_hook.tickets import orca as orca_tickets
from captain_hook.tickets import tree_of
from captain_hook.types import Event
from captain_hook.util import reqenv
from tests.helpers import raw_text

HANDLE = "term_lane"
COORDINATOR_HANDLE = "term_root"
DISPATCH = "ctx_lane"
RUN = "run_1"
WORKER_PANE = "tab-w:pane-w"
COORDINATOR_PANE = "tab-c:pane-c"
WORKER = "worker-session"
COORDINATOR = "coordinator-session"


def orca_commands(
    *, dispatch: str | None = DISPATCH, terminal: dict[str, object] | None = None, **record: object
) -> dict[str, str]:
    checked = {"runId": RUN, "messages": [], "count": 0} | ({"dispatchId": dispatch} if dispatch else {})
    shown = {"id": DISPATCH, "runId": RUN, "assigneeHandle": HANDLE, "status": "dispatched"} | record
    tab, leaf = COORDINATOR_PANE.split(":")
    shown_terminal = {"handle": COORDINATOR_HANDLE, "tabId": tab, "leafId": leaf} if terminal is None else terminal
    return {
        f"orca orchestration check --terminal {HANDLE} --peek --json": json.dumps({"ok": True, "result": checked}),
        f"orca orchestration worker-show --dispatch {DISPATCH} --json": json.dumps(
            {"ok": True, "result": {"dispatch": shown}}
        ),
        f"orca orchestration run-show --id {RUN} --json": json.dumps(
            {"ok": True, "result": {"run": {"id": RUN, "coordinator_handle": COORDINATOR_HANDLE}}}
        ),
        f"orca terminal show --terminal {COORDINATOR_HANDLE} --json": json.dumps(
            {"ok": True, "result": {"terminal": shown_terminal}}
        ),
    }


def claude_pane(session: str, path: Path, source: str = "claude") -> dict[str, object]:
    return {"source": source, "providerSession": {"key": "session_id", "id": session, "transcriptPath": str(path)}}


def write_panes(user_data: Path, entries: dict[str, object]) -> None:
    status = user_data / "agent-hooks" / "last-status.json"
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"version": 2, "entries": entries}))


@contextmanager
def in_worker(user_data: Path, session: str = WORKER, *, attended: bool = True) -> Iterator[None]:
    env = {"ORCA_TERMINAL_HANDLE": HANDLE, "ORCA_USER_DATA_PATH": str(user_data)}
    if attended:
        env["CLAUDE_CODE_SESSION_ATTENDED"] = "1"
    with reqenv.use_request(reqenv.RequestOverrides(env, str(user_data), 0, session)):
        yield


@pytest.fixture
def user_data(tmp_path: Path) -> Path:
    data = tmp_path / "orca"
    write_panes(
        data,
        {
            WORKER_PANE: claude_pane(WORKER, tmp_path / f"{WORKER}.jsonl"),
            COORDINATOR_PANE: claude_pane(COORDINATOR, tmp_path / f"{COORDINATOR}.jsonl"),
        },
    )
    return data


def test_a_live_worker_resolves_its_coordinators_transcript(tmp_path: Path, user_data: Path) -> None:
    (tmp_path / f"{COORDINATOR}.jsonl").touch()
    with in_worker(user_data), stubbed_commands(orca_commands()):
        assert coordinator_transcript() == tmp_path / f"{COORDINATOR}.jsonl"


def test_a_live_worker_resolves_through_orcas_full_records_not_its_concise_output(
    tmp_path: Path, user_data: Path
) -> None:
    (tmp_path / f"{COORDINATOR}.jsonl").touch()
    concise = {
        f"orca orchestration check --terminal {HANDLE} --peek --json": json.dumps(
            {"ok": True, "result": {"messages": []}}
        ),
        f"orca orchestration worker-show --dispatch {DISPATCH} --json": json.dumps(
            {"ok": True, "result": {"dispatchId": DISPATCH, "runId": RUN, "state": "ready"}}
        ),
        f"orca orchestration run-show --id {RUN} --json": json.dumps(
            {"ok": True, "result": {"runId": RUN, "objective": "a drive"}}
        ),
    }
    full = {
        (f"{command} --full" if "orchestration" in command else command): shown
        for command, shown in orca_commands().items()
    }
    with in_worker(user_data), stubbed_commands(concise | full):
        assert coordinator_transcript() == tmp_path / f"{COORDINATOR}.jsonl"


def test_a_coordinator_whose_transcript_is_gone_resolves_nothing(user_data: Path) -> None:
    with in_worker(user_data), stubbed_commands(orca_commands()):
        assert coordinator_transcript() is None


@pytest.mark.parametrize(
    "commands",
    [
        pytest.param(orca_commands(dispatch=None), id="not-a-worker"),
        pytest.param(orca_commands(status="completed"), id="settled"),
        pytest.param(orca_commands(assigneeHandle="term_other"), id="another-assignee"),
        pytest.param(orca_commands(id="ctx_retry"), id="redispatched"),
        pytest.param(orca_commands(terminal={"handle": COORDINATOR_HANDLE}), id="coordinator-terminal-without-pane"),
        pytest.param(
            orca_commands(terminal={"handle": COORDINATOR_HANDLE, "tabId": "tab-x", "leafId": "pane-x"}),
            id="unknown-coordinator-pane",
        ),
    ],
)
def test_a_terminal_without_a_live_dispatch_of_its_own_resolves_nothing(
    tmp_path: Path, user_data: Path, commands: dict[str, str]
) -> None:
    (tmp_path / f"{COORDINATOR}.jsonl").touch()
    with in_worker(user_data), stubbed_commands(commands):
        assert coordinator_transcript() is None


def test_an_unattended_session_in_the_worker_terminal_resolves_nothing(tmp_path: Path, user_data: Path) -> None:
    (tmp_path / f"{COORDINATOR}.jsonl").touch()
    with in_worker(user_data, "nested-print", attended=False), stubbed_commands(orca_commands()):
        assert coordinator_transcript() is None


def test_a_desk_launched_lane_resolves_its_run_coordinator(tmp_path: Path) -> None:
    (tmp_path / f"{COORDINATOR}.jsonl").touch()
    write_panes(
        tmp_path / "orca",
        {
            WORKER_PANE: claude_pane("codex-child", tmp_path / "rollout.jsonl", source="codex"),
            "tab-d:pane-d": claude_pane("desk-runner", tmp_path / "desk-runner.jsonl"),
            COORDINATOR_PANE: claude_pane(COORDINATOR, tmp_path / f"{COORDINATOR}.jsonl"),
        },
    )
    with in_worker(tmp_path / "orca"), stubbed_commands(orca_commands()):
        assert coordinator_transcript() == tmp_path / f"{COORDINATOR}.jsonl"


@pytest.mark.parametrize("coordinator", [None, "claude-elsewhere", "codex"])
def test_a_coordinator_pane_without_a_claude_session_resolves_nothing(tmp_path: Path, coordinator: str | None) -> None:
    transcript = tmp_path / f"{COORDINATOR}.jsonl"
    transcript.touch()
    entries: dict[str, object] = {WORKER_PANE: claude_pane(WORKER, tmp_path / f"{WORKER}.jsonl")}
    if coordinator == "claude-elsewhere":
        entries["tab-x:pane-x"] = claude_pane(COORDINATOR, transcript)
    elif coordinator == "codex":
        entries[COORDINATOR_PANE] = claude_pane(COORDINATOR, transcript, source="codex")
    write_panes(tmp_path / "orca", entries)
    with in_worker(tmp_path / "orca"), stubbed_commands(orca_commands()):
        assert coordinator_transcript() is None


def test_a_missing_pane_status_resolves_nothing(tmp_path: Path) -> None:
    (tmp_path / f"{COORDINATOR}.jsonl").touch()
    with in_worker(tmp_path / "orca"), stubbed_commands(orca_commands()):
        assert coordinator_transcript() is None


@pytest.mark.parametrize("status", ["[]", '{"entries": []}', "{"], ids=["list", "entries-list", "truncated"])
def test_a_malformed_pane_status_resolves_nothing(tmp_path: Path, status: str) -> None:
    (tmp_path / f"{COORDINATOR}.jsonl").touch()
    path = tmp_path / "orca" / "agent-hooks" / "last-status.json"
    path.parent.mkdir(parents=True)
    path.write_text(status)
    with in_worker(tmp_path / "orca"), stubbed_commands(orca_commands()):
        assert coordinator_transcript() is None


def refresh_settled() -> None:
    deadline = time.monotonic() + 10
    while any(reading.refreshing for reading in READINGS.values()):
        assert time.monotonic() < deadline
        time.sleep(0.001)


def test_a_due_reading_answers_at_once_and_refreshes_off_the_dispatch(
    tmp_path: Path, user_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = tmp_path / f"{COORDINATOR}.jsonl"
    transcript.touch()
    monkeypatch.setattr("captain_hook.orca.DISPATCH_TTL", 0.0)
    with in_worker(user_data), stubbed_commands(orca_commands()):
        assert coordinator_transcript() == transcript
    released = threading.Event()

    def busy_orca(*_: object, **__: object) -> None:
        assert released.wait(10)

    monkeypatch.setattr("captain_hook.builtin_packs.general.hooks._sessions.orca_json", busy_orca)
    with in_worker(user_data):
        assert coordinator_transcript() == transcript
        released.set()
        refresh_settled()
        assert coordinator_transcript() is None
        refresh_settled()


def test_a_refresh_orca_never_answered_keeps_the_last_reading(
    tmp_path: Path, user_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = tmp_path / f"{COORDINATOR}.jsonl"
    transcript.touch()
    monkeypatch.setattr("captain_hook.orca.DISPATCH_TTL", 0.0)
    with in_worker(user_data), stubbed_commands(orca_commands()):
        assert coordinator_transcript() == transcript
    monkeypatch.setattr(
        "captain_hook.builtin_packs.general.hooks._sessions.orca_json",
        lambda *_, **__: TimedOut("`orca` timed out after 2s", 2.0),
    )
    with in_worker(user_data):
        assert coordinator_transcript() == transcript
        refresh_settled()
        assert coordinator_transcript() == transcript
        refresh_settled()


def test_a_first_reading_orca_never_answered_resolves_nothing(user_data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "captain_hook.builtin_packs.general.hooks._sessions.orca_json",
        lambda *_, **__: TimedOut("`orca` timed out after 2s", 2.0),
    )
    with in_worker(user_data):
        assert coordinator_transcript() is None


def asked_orca(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    asked: list[tuple[str, ...]] = []
    real = _sessions.orca_json

    def recording(argv: tuple[str, ...], *path: str, **kwargs: tuple[str, ...]) -> object:
        asked.append(argv[1:3])
        return real(argv, *path, **kwargs)

    monkeypatch.setattr(_sessions, "orca_json", recording)
    return asked


def test_a_lanes_ticket_binding_reads_its_own_dispatch_and_never_lists_every_worker(
    user_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = asked_orca(monkeypatch)
    with in_worker(user_data), stubbed_commands(orca_commands()):
        assert orca_tickets.resolve(HANDLE) == (RUN, COORDINATOR_HANDLE)
    assert asked == [
        ("orchestration", "check"),
        ("orchestration", "worker-show"),
        ("orchestration", "run-show"),
        ("terminal", "show"),
    ]


def test_a_terminal_with_no_dispatch_of_its_own_binds_no_coordinator_after_one_check(
    user_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = asked_orca(monkeypatch)
    with in_worker(user_data), stubbed_commands(orca_commands(dispatch=None)):
        assert orca_tickets.resolve(HANDLE) == (None, None)
    assert asked == [("orchestration", "check")]


def test_a_due_ticket_binding_answers_from_the_last_reading_while_orca_is_busy(
    user_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("captain_hook.orca.DISPATCH_TTL", 0.0)
    with in_worker(user_data), stubbed_commands(orca_commands()):
        assert orca_tickets.resolve(HANDLE) == (RUN, COORDINATOR_HANDLE)
    released = threading.Event()

    def busy_orca(*_: object, **__: object) -> None:
        assert released.wait(10)

    monkeypatch.setattr(_sessions, "orca_json", busy_orca)
    with in_worker(user_data):
        assert orca_tickets.resolve(HANDLE) == (RUN, COORDINATOR_HANDLE)
        released.set()
        refresh_settled()
        assert orca_tickets.resolve(HANDLE) == (None, None)
        refresh_settled()


def test_outside_orca_resolves_nothing() -> None:
    assert coordinator_transcript() is None


def test_a_worker_event_reads_its_coordinator_as_its_root(tmp_path: Path, user_data: Path) -> None:
    (tmp_path / f"{COORDINATOR}.jsonl").write_text(json.dumps(raw_text("user", "invite the bot to #alerts")) + "\n")
    (tmp_path / f"{WORKER}.jsonl").write_text(json.dumps(raw_text("user", "the brief")) + "\n")
    observed: list[tuple[Path | None, str, str]] = []

    @on(Event.PreToolUse)
    def lane_root(evt):
        observed.append((evt.ctx.root_path, tree_of(evt), evt.ctx.root_transcript_block(window=5)))

    raw = {
        "session_id": WORKER,
        "transcript_path": str(tmp_path / f"{WORKER}.jsonl"),
        "cwd": str(tmp_path),
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "pwd"},
    }
    fixture = FixtureOwner()
    token = CURRENT_CLIENT.set(fixture.client)
    try:
        with in_worker(user_data), stubbed_commands(orca_commands()):
            dispatch_event(tmp_path, Event.PreToolUse, raw, session_dir=ensure_session(SessionId(WORKER)))
        dispatch_event(tmp_path, Event.PreToolUse, raw, session_dir=ensure_session(SessionId(WORKER)))
    finally:
        CURRENT_CLIENT.reset(token)
        fixture.close()
    assert observed == [
        (
            tmp_path / f"{COORDINATOR}.jsonl",
            COORDINATOR,
            "<root_transcript>\nuser: invite the bot to #alerts\n</root_transcript>",
        ),
        (None, WORKER, ""),
    ]
