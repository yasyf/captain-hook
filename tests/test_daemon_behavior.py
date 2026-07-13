"""End-to-end daemon behaviors that the socket-level suite (``test_daemon_server``) and the
client-fallback suite (``test_client``) don't cover: a worker picks up an edited hook without a
restart, recovers from a ``SIGKILL``, exits when idle and respawns on the next event, collapses a
storm of concurrent clients to exactly one worker, serves version-skewed clients without churning,
and dedupes byte-identical siblings (daemon-to-daemon and daemon-to-cold) through the shared
once-sentinel.

Two behaviors from the plan's list are already proven elsewhere and are not duplicated here:
cross-session concurrency (``test_daemon_server.TestConcurrency.test_slow_hook_in_one_session_does_not_block_another``)
and the deadline-undercut fail-open with no cold rerun
(``test_client.TestSendBoundary.test_post_send_stall_fails_open_no_cold``).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.daemon_helpers import (
    cleanup_dirs,
    control_req,
    daemon_dirs,
    daemon_env,
    event_req,
    make_project,
    pid_alive,
    read_meta,
    run_client,
    run_cold,
    running_daemon,
    send,
    shutdown_worker,
    spawn_daemon,
    wait_ready,
    worker_sock,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

NOOP_HOOK = """
from __future__ import annotations

from captain_hook import Event, on


@on(Event.PostToolUse)
def noop(evt):
    return None
"""

SINK_HOOK = """
from __future__ import annotations

from pathlib import Path

from captain_hook import Event, on


@on(Event.PostToolUse)
def sink(evt):
    ti = evt._raw.get("tool_input", {})
    if (path := ti.get("sink")) and (marker := ti.get("marker")):
        Path(path).open("a").write(marker + "\\n")
    return None
"""


def version_hook(marker: str) -> str:
    return (
        "from __future__ import annotations\n\n"
        "from captain_hook import Event, on\n\n\n"
        "@on(Event.PostToolUse)\n"
        "def show(evt):\n"
        f"    print({marker!r})\n"
        "    return None\n"
    )


def payload_bytes(session: str) -> bytes:
    return json.dumps({"session_id": session, "tool_name": "Bash", "tool_input": {}}).encode()


def wait_until(predicate: Callable[[], bool], *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


@pytest.fixture
def project(tmp_path: Path) -> Iterator[tuple[Path, dict[str, Path]]]:
    dirs = daemon_dirs()
    root = tmp_path / "proj"
    try:
        yield root, dirs
    finally:
        cleanup_dirs(dirs)


def test_hot_reload_picks_up_an_edited_hook(project: tuple[Path, dict[str, Path]]) -> None:
    root, dirs = project
    make_project(root, version_hook("VERSION_1"))
    env = daemon_env(root, dirs)
    payload = {"session_id": "hr", "tool_name": "Bash", "tool_input": {}}
    extra = {"CAPT_HOOK_ONCE_TTL": "0"}
    with running_daemon(root, env) as sock:
        first = send(sock, event_req("PostToolUse", payload, root, env, extra_env=extra))
        assert "VERSION_1" in first["stdout"]
        (root / ".claude" / "hooks" / "h.py").write_text(version_hook("VERSION_2_RELOADED"))
        second = send(sock, event_req("PostToolUse", payload, root, env, extra_env=extra))
        assert "VERSION_2_RELOADED" in second["stdout"]
        assert "VERSION_1" not in second["stdout"]


def test_sigkill_respawns_exactly_one_worker(project: tuple[Path, dict[str, Path]]) -> None:
    root, dirs = project
    make_project(root, NOOP_HOOK)
    env = daemon_env(root, dirs, CAPT_HOOK_DAEMON_FALLBACK="cold")
    run = Path(env["CAPT_HOOK_RUN_DIR"])
    sock = worker_sock(root, env)
    try:
        first = run_client("--root", str(root), "run", "PostToolUse", env=env, stdin=payload_bytes("k1"), cwd=str(root))
        assert first.returncode == 0, first.stderr
        pid_a = read_meta(root, env)["pid"]
        assert pid_alive(pid_a)

        os.kill(pid_a, signal.SIGKILL)
        wait_until(lambda: not pid_alive(pid_a), timeout=5)

        # With fallback=cold the next client exits 0 no matter what; it also detects the dead socket
        # and respawns exactly one fresh worker (same key → same socket/meta, overwritten in place).
        second = run_client(
            "--root", str(root), "run", "PostToolUse", env=env, stdin=payload_bytes("k2"), cwd=str(root)
        )
        assert second.returncode == 0, second.stderr
        pid_b = read_meta(root, env)["pid"]
        assert pid_b != pid_a
        assert pid_alive(pid_b)
        assert len(list(run.glob("*.sock"))) == 1
        assert len(list(run.glob("*.json"))) == 1
    finally:
        shutdown_worker(sock)


def test_idle_timeout_exits_then_respawns(project: tuple[Path, dict[str, Path]]) -> None:
    root, dirs = project
    make_project(root, NOOP_HOOK)
    env = daemon_env(root, dirs, HOOKS_DAEMON_IDLE_S="0.5", CAPT_HOOK_DAEMON_FALLBACK="closed")
    sock = worker_sock(root, env)
    boot_log = Path(env["CAPT_HOOK_RUN_DIR"]) / "boot.log"
    proc = spawn_daemon(root, env, boot_log)
    try:
        wait_ready(sock, proc, boot_log)
        # No traffic: the accept loop's idle check fires within a couple of accept timeouts and exits.
        assert proc.wait(timeout=12) == 0
        assert not os.path.exists(sock)

        respawn = run_client(
            "--root", str(root), "run", "PostToolUse", env=env, stdin=payload_bytes("idle"), cwd=str(root)
        )
        assert respawn.returncode == 0, respawn.stderr
        assert pid_alive(read_meta(root, env)["pid"])
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        shutdown_worker(sock)


def test_client_storm_spawns_exactly_one_worker(project: tuple[Path, dict[str, Path]]) -> None:
    root, dirs = project
    make_project(root, NOOP_HOOK)
    env = daemon_env(root, dirs, CAPT_HOOK_DAEMON_FALLBACK="closed")
    run = Path(env["CAPT_HOOK_RUN_DIR"])
    results: dict[int, int] = {}

    def fire(i: int) -> None:
        proc = run_client(
            "--root", str(root), "run", "PostToolUse", env=env, stdin=payload_bytes(f"storm{i}"), cwd=str(root)
        )
        results[i] = proc.returncode

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(12)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=40)
        assert results == dict.fromkeys(range(12), 0), results
        assert len(list(run.glob("*.sock"))) == 1
        assert len(list(run.glob("*.json"))) == 1
    finally:
        shutdown_worker(worker_sock(root, env))


def test_client_build_skew_served_without_restart(project: tuple[Path, dict[str, Path]]) -> None:
    root, dirs = project
    make_project(root, NOOP_HOOK)
    env = daemon_env(root, dirs, CAPT_HOOK_DAEMON_FALLBACK="closed")
    with running_daemon(root, env) as sock:
        started = read_meta(root, env)["started_at"]
        for i, build in enumerate(("skew-a", "skew-b", "skew-a")):
            skewed = env | {"CAPT_HOOK_CLIENT_BUILD": build}
            proc = run_client(
                "--root", str(root), "run", "PostToolUse", env=skewed, stdin=payload_bytes(f"skew{i}"), cwd=str(root)
            )
            assert proc.returncode == 0, (build, proc.stderr)
        # No real build change occurred, so the header skew never armed a drain-restart: same worker,
        # same start time, still serving.
        assert send(sock, control_req("ping"))["status"] == "ok"
        assert read_meta(root, env)["started_at"] == started


def test_concurrent_identical_siblings_dispatch_once(project: tuple[Path, dict[str, Path]]) -> None:
    root, dirs = project
    make_project(root, SINK_HOOK)
    env = daemon_env(root, dirs)
    sink = dirs["run"] / "sink.txt"
    payload = {"session_id": "once", "tool_name": "Bash", "tool_input": {"sink": str(sink), "marker": "M"}}
    request = event_req("PostToolUse", payload, root, env, extra_env={"CAPT_HOOK_ONCE_TTL": "30"})
    responses: dict[int, dict] = {}

    def fire(i: int) -> None:
        responses[i] = send(sock, request)

    with running_daemon(root, env) as sock:
        threads = [threading.Thread(target=fire, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert all(r["status"] == "ok" for r in responses.values()), responses
    assert sink.read_text() == "M\n"  # the once-guard collapsed the byte-identical pair to one dispatch


def test_daemon_and_cold_sibling_dedupe_via_shared_sentinel(project: tuple[Path, dict[str, Path]]) -> None:
    root, dirs = project
    make_project(root, SINK_HOOK)
    env = daemon_env(root, dirs, CAPT_HOOK_ONCE_TTL="30")
    sink = dirs["run"] / "sink.txt"
    raw = json.dumps({"session_id": "cd", "tool_name": "Bash", "tool_input": {"sink": str(sink), "marker": "X"}})
    daemon_resp: list[dict] = []
    cold_resp: list[subprocess.CompletedProcess[bytes]] = []

    with running_daemon(root, env) as sock:

        def via_daemon() -> None:
            daemon_resp.append(send(sock, event_req("PostToolUse", raw, root, env)))

        def via_cold() -> None:
            cold_resp.append(
                run_cold("--root", str(root), "run", "PostToolUse", env=env, stdin=raw.encode(), cwd=str(root))
            )

        threads = [threading.Thread(target=via_daemon), threading.Thread(target=via_cold)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert daemon_resp[0]["status"] == "ok"
    assert cold_resp[0].returncode == 0
    # A daemon dispatch and a concurrent cold CLI share the XDG_CACHE_HOME-rooted sentinel, so exactly
    # one of them ran the side-effecting hook.
    assert sink.read_text() == "X\n"
