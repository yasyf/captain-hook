from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from cc_transcript.ids import SessionId

from captain_hook.app import on
from captain_hook.cli import dispatch_event
from captain_hook.grants.evidence import tree_of
from captain_hook.orca import coordinator_transcript
from captain_hook.session import ensure_session
from captain_hook.snapshots.client import CURRENT_CLIENT
from captain_hook.testing.helpers import stubbed_commands
from captain_hook.testing.snapshots import FixtureOwner
from captain_hook.types import Event
from captain_hook.util import reqenv
from tests.helpers import raw_text

HANDLE = "term_lane"
DISPATCH = "ctx_lane"
TASK = "task_lane"
WORKER_PANE = "tab-w:pane-w"
COORDINATOR_PANE = "tab-c:pane-c"
WORKER = "worker-session"
COORDINATOR = "coordinator-session"


def orca_commands(*, dispatch: str | None = DISPATCH, **record: object) -> dict[str, str]:
    checked = {"runId": "run_1", "messages": [], "count": 0} | ({"dispatchId": dispatch} if dispatch else {})
    shown = {
        "id": DISPATCH,
        "assignee_handle": HANDLE,
        "status": "dispatched",
        "assignee_pane_key": WORKER_PANE,
        "creator_pane_key": COORDINATOR_PANE,
    } | record
    return {
        f"orca orchestration check --terminal {HANDLE} --peek --json": json.dumps({"ok": True, "result": checked}),
        f"orca orchestration worker-show --dispatch {DISPATCH} --json": json.dumps(
            {"ok": True, "result": {"dispatch": {"id": DISPATCH, "taskId": TASK}}}
        ),
        f"orca orchestration dispatch-show --task {TASK} --json": json.dumps(
            {"ok": True, "result": {"dispatch": shown}}
        ),
    }


def claude_pane(session: str, path: Path, source: str = "claude") -> dict[str, object]:
    return {"source": source, "providerSession": {"key": "session_id", "id": session, "transcriptPath": str(path)}}


def write_panes(user_data: Path, entries: dict[str, object]) -> None:
    status = user_data / "agent-hooks" / "last-status.json"
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({"version": 2, "entries": entries}))


@contextmanager
def in_worker(user_data: Path, session: str = WORKER) -> Iterator[None]:
    env = {"ORCA_TERMINAL_HANDLE": HANDLE, "ORCA_USER_DATA_PATH": str(user_data)}
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
    with in_worker(user_data), stubbed_commands(orca_commands()):
        assert coordinator_transcript(WORKER) == tmp_path / f"{COORDINATOR}.jsonl"


@pytest.mark.parametrize(
    "commands",
    [
        pytest.param(orca_commands(dispatch=None), id="not-a-worker"),
        pytest.param(orca_commands(status="completed"), id="settled"),
        pytest.param(orca_commands(assignee_handle="term_other"), id="another-assignee"),
        pytest.param(orca_commands(id="ctx_retry"), id="redispatched"),
        pytest.param(orca_commands(creator_pane_key="tab-x:pane-x"), id="unknown-coordinator-pane"),
    ],
)
def test_a_terminal_without_a_live_dispatch_of_its_own_resolves_nothing(
    user_data: Path, commands: dict[str, str]
) -> None:
    with in_worker(user_data), stubbed_commands(commands):
        assert coordinator_transcript(WORKER) is None


def test_another_session_in_the_worker_terminal_resolves_nothing(user_data: Path) -> None:
    with in_worker(user_data, "nested-print"), stubbed_commands(orca_commands()):
        assert coordinator_transcript("nested-print") is None


@pytest.mark.parametrize(
    "entries",
    [
        pytest.param(None, id="no-status-file"),
        pytest.param({}, id="no-panes"),
        pytest.param({WORKER_PANE: claude_pane(WORKER, Path("/w.jsonl"))}, id="no-coordinator-pane"),
        pytest.param(
            {
                WORKER_PANE: claude_pane(WORKER, Path("/w.jsonl")),
                COORDINATOR_PANE: claude_pane(COORDINATOR, Path("/c.jsonl"), source="codex"),
            },
            id="codex-coordinator",
        ),
    ],
)
def test_a_coordinator_pane_without_a_claude_session_resolves_nothing(
    tmp_path: Path, entries: dict[str, object] | None
) -> None:
    if entries is not None:
        write_panes(tmp_path / "orca", entries)
    with in_worker(tmp_path / "orca"), stubbed_commands(orca_commands()):
        assert coordinator_transcript(WORKER) is None


def test_outside_orca_resolves_nothing() -> None:
    assert coordinator_transcript(WORKER) is None


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
