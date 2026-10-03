"""Live dispatch through the supervised Linux host: plugin shim → capt-hookd → Python worker.

CI installs the host with ``capt-hookd package-install``, starts it under the workspace
supervisor, and names a second build of the same client in ``CAPTAIN_TEST_LINUX_HOST_MISMATCH``.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
HOOK = ROOT / "captain_hook/bin/hook"
MISMATCH = os.environ.get("CAPTAIN_TEST_LINUX_HOST_MISMATCH", "")
ASYNC_DELAY = 3.0

pytestmark = pytest.mark.skipif(not MISMATCH, reason="needs the supervised Linux host CI installs")

HOOKS = """
import time
from pathlib import Path

from captain_hook import Event, PostToolUseEvent, block_command, on

block_command(["rm", "-rf", "*"], reason="Recursive force-delete is forbidden")


@on(Event.PostToolUse, async_=True)
def record(event: PostToolUseEvent) -> None:
    time.sleep({delay})
    Path(event.tool_response).write_text("recorded")
"""


def make_project(root: Path) -> Path:
    hooks = root / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "linux_host.py").write_text(textwrap.dedent(HOOKS.format(delay=ASYNC_DELAY)))
    return root


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return make_project(tmp_path / "project")


def environment(root: Path) -> dict[str, str]:
    return {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "CLAUDE_PROJECT_DIR": str(root)}


def dispatch(root: Path, event: str, session: str, **payload: Any) -> subprocess.CompletedProcess[str]:
    transcript = root / f"{session}.jsonl"
    transcript.touch()
    body = {
        "session_id": session,
        "transcript_path": str(transcript),
        "cwd": str(root),
        "hook_event_name": event,
        **payload,
    }
    return subprocess.run(
        [HOOK, "run", event],
        input=json.dumps(body),
        env=environment(root),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def bash(root: Path, session: str, command: str) -> subprocess.CompletedProcess[str]:
    return dispatch(root, "PreToolUse", session, tool_name="Bash", tool_input={"command": command})


def denied(result: subprocess.CompletedProcess[str]) -> bool:
    assert result.returncode == 0, result.stderr
    if not result.stdout.strip():
        return False
    return json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def worker_pids(root: Path) -> set[int]:
    result = subprocess.run(
        [HOOK, "status"], env=environment(root), capture_output=True, text=True, check=True, timeout=30
    )
    return {worker["pid"] for worker in json.loads(result.stdout)["workers"] if worker["root"] == str(root)}


def test_allowed_and_blocked_verdicts(project: Path) -> None:
    session = uuid.uuid4().hex
    assert not denied(bash(project, session, "ls"))
    blocked = bash(project, session, "rm -rf build/")
    assert denied(blocked)
    assert "Recursive force-delete is forbidden" in blocked.stdout


def test_worker_is_reused_across_events() -> None:
    with TemporaryDirectory(prefix="captain-hook-reuse-", dir=Path.home()) as directory:
        project = make_project(Path(directory))
        session = uuid.uuid4().hex
        assert not denied(bash(project, session, "ls"))
        first = worker_pids(project)
        assert first
        for _ in range(3):
            assert not denied(bash(project, session, "ls"))
        assert worker_pids(project) == first


def test_concurrent_sessions_each_get_their_verdict(project: Path, tmp_path: Path) -> None:
    other = make_project(tmp_path / "other")
    cases = [
        (root, uuid.uuid4().hex, command, command.startswith("rm"))
        for root in (project, other)
        for _ in range(4)
        for command in ("ls", "rm -rf dist/")
    ]
    with ThreadPoolExecutor(len(cases)) as pool:
        results = list(pool.map(lambda case: bash(case[0], case[1], case[2]), cases))
    assert [denied(result) for result in results] == [case[3] for case in cases]


def test_async_hook_finishes_after_the_event_returns(project: Path, tmp_path: Path) -> None:
    marker = tmp_path / "async-marker"
    started = time.monotonic()
    result = dispatch(
        project,
        "PostToolUse",
        uuid.uuid4().hex,
        tool_name="Bash",
        tool_input={"command": "ls"},
        tool_response=str(marker),
    )
    returned = time.monotonic() - started
    assert result.returncode == 0, result.stderr
    assert returned < ASYNC_DELAY
    deadline = started + ASYNC_DELAY + 30
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert marker.read_text() == "recorded"


def test_a_mismatched_build_is_fenced_from_the_host(project: Path) -> None:
    assert not denied(bash(project, uuid.uuid4().hex, "ls"))
    before = worker_pids(project)
    assert before
    refused = subprocess.run(
        [MISMATCH, "restart-workers"], env=environment(project), capture_output=True, text=True, timeout=30
    )
    assert refused.returncode == 1
    assert "requires the exact runtime build" in refused.stderr
    assert worker_pids(project) == before
    restarted = subprocess.run(
        [HOOK, "restart-workers"], env=environment(project), capture_output=True, text=True, timeout=30
    )
    assert restarted.returncode == 0, restarted.stderr
    assert worker_pids(project).isdisjoint(before)
