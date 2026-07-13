from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from capt_hook_client.key import request_env, worker_key
from captain_hook.daemon.protocol import PROTOCOL

if TYPE_CHECKING:
    from collections.abc import Iterator

HOOK_SRC = """
from __future__ import annotations

import sys
import time
from pathlib import Path

from captain_hook import Event, Tool, hook, on

hook(Event.PreToolUse, message="pre-tool warning", only_if=[Tool("Edit")])


@on(Event.PostToolUse)
def slow(evt):
    if ms := evt._raw.get("tool_input", {}).get("sleep_ms"):
        time.sleep(ms / 1000)
    return None


@on(Event.PostToolUse)
def printer(evt):
    if evt._raw.get("tool_input", {}).get("say"):
        print("HELLO_FROM_HOOK")
    return None


@on(Event.PostToolUse)
def side_effect(evt):
    ti = evt._raw.get("tool_input", {})
    if (sink := ti.get("sink")) and (marker := ti.get("marker")):
        Path(sink).open("a").write(marker + "\\n")
    return None


@on(Event.PostToolUse)
def show_root(evt):
    if evt._raw.get("tool_input", {}).get("show_root"):
        print(str(evt.ctx.project_root))
    return None


@on(Event.PostToolUse)
def exiter(evt):
    if code := evt._raw.get("tool_input", {}).get("exit_code"):
        print("BEFORE_EXIT")
        sys.exit(code)
    return None
"""


# A hook whose only_if condition raises: run_handler swallows a handler body's exception, but a
# condition raising in matches_conditions propagates uncaught out of dispatch — the daemon's
# status=error path. PreToolUse is decision-exempt from the once-guard, so warm and cold each
# dispatch the same payload and both crash on the ValueError.
RAISING_HOOK_SRC = """
from __future__ import annotations

from captain_hook import CustomCondition, Event, hook


class Boom(CustomCondition):
    def check(self, evt) -> bool:
        if evt._raw.get("tool_input", {}).get("boom"):
            raise ValueError("boom from condition")
        return False


hook(Event.PreToolUse, only_if=[Boom()], message="never", block=True)
"""


def make_project(root: Path) -> Path:
    (hooks := root / ".claude" / "hooks").mkdir(parents=True)
    (hooks / "h.py").write_text(HOOK_SRC)
    return root


def daemon_env_for(root: Path, dirs: dict[str, Path]) -> dict[str, str]:
    return os.environ | {
        "CAPT_HOOK_RUN_DIR": str(dirs["run"]),
        "CAPTAIN_HOOK_STATE_DIR": str(dirs["state"]),
        "XDG_CACHE_HOME": str(dirs["cache"]),
        "CAPTAIN_HOOK_LOG_DIR": str(dirs["logs"]),
        "CAPT_HOOK_DECISIONS_DB": str(dirs["decisions"] / "d.db"),
        "CLAUDE_PROJECT_DIR": str(root),
    }


def sock_for(root: Path, env: dict[str, str]) -> str:
    return str(Path(env["CAPT_HOOK_RUN_DIR"]) / f"{worker_key(str(root), env)}.sock")


def spawn_daemon(root: Path, env: dict[str, str], boot_log: Path) -> subprocess.Popen:
    handle = boot_log.open("wb")
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "captain_hook", "daemon", "run", "--root", str(root)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
        )
    finally:
        handle.close()


def wait_ready(sock_path: str, proc: subprocess.Popen, boot_log: Path, *, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"daemon exited early (code {proc.returncode}); boot log:\n{boot_log.read_text()}")
        if os.path.exists(sock_path):
            with contextlib.suppress(OSError):
                if send(sock_path, control_req("ping"))["status"] == "ok":
                    return
        time.sleep(0.05)
    raise RuntimeError(f"daemon never became ready; boot log:\n{boot_log.read_text()}")


def stop_daemon(sock_path: str, proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        with contextlib.suppress(OSError):
            send(sock_path, control_req("shutdown"), timeout=5)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)


def client_meta(build: str = "b1") -> dict[str, Any]:
    return {"version": "", "build": build, "pid": os.getpid(), "ppid": os.getppid()}


def event_req(
    event: str, payload: dict | str, root: Path, env: dict[str, str], *, extra_env: dict | None = None
) -> dict:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return {
        "v": PROTOCOL,
        "kind": "event",
        "client": client_meta(),
        "event": event,
        "async": False,
        "root": str(root),
        "cwd": str(root),
        "hooks": None,
        "env": request_env(env) | (extra_env or {}),
        "payload_raw": raw,
    }


def control_req(kind: str, *, v: int = PROTOCOL) -> dict:
    return {"v": v, "kind": kind, "client": client_meta()}


def send(sock_path: str, request: dict, *, read: bool = True, timeout: float = 15.0):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(sock_path)
    sock.sendall((json.dumps(request) + "\n").encode())
    if not read:
        return sock
    buf = b""
    while b"\n" not in buf:
        if not (chunk := sock.recv(65536)):
            break
        buf += chunk
    sock.close()
    return json.loads(buf.split(b"\n")[0])


def cold_run(
    event: str, payload_raw: str, root: Path, env: dict[str, str], *, extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "captain_hook",
            "--root",
            str(root),
            "--hooks",
            str(root / ".claude" / "hooks"),
            "run",
            event,
        ],
        input=payload_raw.encode(),
        capture_output=True,
        env=env | (extra_env or {}),
        cwd=str(root),
        timeout=60,
        check=False,
    )


def assert_matches_cold(
    resp: dict, event: str, payload_raw: str, root: Path, env: dict[str, str], *, extra_env: dict | None = None
) -> None:
    cold = cold_run(event, payload_raw, root, env, extra_env=extra_env)
    assert resp["stdout"] == cold.stdout.decode(), "stdout diverged from cold"
    assert resp["stderr"] == cold.stderr.decode(), "stderr diverged from cold"
    assert resp["exit"] == cold.returncode, "exit code diverged from cold"


@pytest.fixture(scope="module")
def dirs() -> Iterator[dict[str, Path]]:
    run = tempfile.mkdtemp(dir="/tmp", prefix="chd")
    base = Path(tempfile.mkdtemp(prefix="chd-base"))
    mapping = {"run": Path(run)} | {name: base / name for name in ("state", "cache", "logs", "decisions")}
    for path in mapping.values():
        path.mkdir(parents=True, exist_ok=True)
    yield mapping
    shutil.rmtree(run, ignore_errors=True)
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(scope="module")
def worker(dirs: dict[str, Path]) -> Iterator[tuple[str, Path, dict[str, str]]]:
    root = make_project(Path(tempfile.mkdtemp(prefix="chd-proj")))
    env = daemon_env_for(root, dirs)
    sock_path = sock_for(root, env)
    proc = spawn_daemon(root, env, dirs["run"] / "boot.log")
    try:
        wait_ready(sock_path, proc, dirs["run"] / "boot.log")
        yield sock_path, root, env
    finally:
        stop_daemon(sock_path, proc)
        shutil.rmtree(root, ignore_errors=True)


class TestControl:
    def test_ping_roundtrip(self, worker: tuple[str, Path, dict]) -> None:
        sock, _, _ = worker
        resp = send(sock, control_req("ping"))
        assert resp["status"] == "ok"
        assert resp["v"] == PROTOCOL

    def test_sun_path_stays_under_the_limit(self, worker: tuple[str, Path, dict]) -> None:
        sock, _, _ = worker
        assert len(sock) < 100

    def test_protocol_mismatch_is_rejected(self, worker: tuple[str, Path, dict]) -> None:
        sock, _, _ = worker
        assert send(sock, control_req("ping", v=99))["status"] == "rejected"

    def test_status_reports_daemon_metadata(self, worker: tuple[str, Path, dict]) -> None:
        sock, _, _ = worker
        resp = send(sock, control_req("status"))
        assert resp["status"] == "ok"
        assert "build" in json.loads(resp["stdout"])


class TestEventParity:
    def test_pretooluse_matches_cold(self, worker: tuple[str, Path, dict]) -> None:
        sock, root, env = worker
        payload = json.dumps(
            {
                "session_id": "s1",
                "tool_name": "Edit",
                "tool_input": {"file_path": "a.py", "old_string": "x", "new_string": "y"},
            }
        )
        resp = send(sock, event_req("PreToolUse", payload, root, env))
        assert json.loads(resp["stdout"])["hookSpecificOutput"]["additionalContext"] == "pre-tool warning"
        assert_matches_cold(resp, "PreToolUse", payload, root, env)

    def test_symlinked_root_flows_verbatim_matching_cold(self, worker: tuple[str, Path, dict], tmp_path: Path) -> None:
        # The request's literal --root (a symlink) must reach HookContext.project_root verbatim; only
        # the worker key canonicalizes. A hook echoing project_root proves warm == cold on the literal.
        sock, root, env = worker
        link = tmp_path / "root-link"
        link.symlink_to(root)
        payload = json.dumps({"session_id": "sl", "tool_name": "Bash", "tool_input": {"show_root": True}})
        extra = {"CAPT_HOOK_ONCE_TTL": "0"}
        resp = send(sock, event_req("PostToolUse", payload, link, env, extra_env=extra))
        assert resp["stdout"] == f"{link}\n"
        assert_matches_cold(resp, "PostToolUse", payload, link, env, extra_env=extra)

    def test_hook_sys_exit_matches_cold(self, worker: tuple[str, Path, dict]) -> None:
        # A hook that prints then sys.exit(7): the warm response carries the printed output and exit 7,
        # byte-parity with cold (where the SystemExit still delivers the output and the code).
        sock, root, env = worker
        payload = json.dumps({"session_id": "sx7", "tool_name": "Bash", "tool_input": {"exit_code": 7}})
        extra = {"CAPT_HOOK_ONCE_TTL": "0"}
        resp = send(sock, event_req("PostToolUse", payload, root, env, extra_env=extra))
        assert resp["stdout"] == "BEFORE_EXIT\n"
        assert resp["exit"] == 7
        assert resp["status"] == "ok"
        assert_matches_cold(resp, "PostToolUse", payload, root, env, extra_env=extra)

    def test_malformed_payload_matches_cold(self, worker: tuple[str, Path, dict]) -> None:
        sock, root, env = worker
        raw = "{not valid json"
        resp = send(sock, event_req("PostToolUse", raw, root, env, extra_env={"CAPT_HOOK_ONCE_TTL": "0"}))
        assert resp["stderr"].startswith("Malformed stdin:")
        assert resp["exit"] == 0
        assert_matches_cold(resp, "PostToolUse", raw, root, env, extra_env={"CAPT_HOOK_ONCE_TTL": "0"})

    def test_invalid_event_matches_cold(self, worker: tuple[str, Path, dict]) -> None:
        sock, root, env = worker
        payload = json.dumps({"session_id": "s1"})
        resp = send(sock, event_req("Nonsense", payload, root, env))
        assert resp["stderr"].startswith("Invalid event type: 'Nonsense'.")
        assert resp["exit"] == 1
        assert_matches_cold(resp, "Nonsense", payload, root, env)

    def test_empty_stdin_is_a_silent_ok(self, worker: tuple[str, Path, dict]) -> None:
        sock, root, env = worker
        resp = send(sock, event_req("PostToolUse", "", root, env))
        assert (resp["status"], resp["stdout"], resp["stderr"], resp["exit"]) == ("ok", "", "", 0)


class TestDispatchBehaviour:
    def test_hook_print_lands_in_response_not_daemon_log(
        self, worker: tuple[str, Path, dict], dirs: dict[str, Path]
    ) -> None:
        sock, root, env = worker
        payload = json.dumps({"session_id": "sp", "tool_name": "Bash", "tool_input": {"say": True}})
        resp = send(sock, event_req("PostToolUse", payload, root, env, extra_env={"CAPT_HOOK_ONCE_TTL": "0"}))
        assert "HELLO_FROM_HOOK\n" in resp["stdout"]
        key = worker_key(str(root), env)
        assert "HELLO_FROM_HOOK" not in (dirs["logs"] / f"daemon-{key}.log").read_text()

    def test_claim_once_collapses_duplicate_pair(self, worker: tuple[str, Path, dict], tmp_path: Path) -> None:
        sock, root, env = worker
        sink = tmp_path / "sink.txt"
        marker = f"once-{os.getpid()}-{time.monotonic_ns()}"
        payload = json.dumps(
            {"session_id": "sd", "tool_name": "Bash", "tool_input": {"sink": str(sink), "marker": marker}}
        )
        req = event_req("PostToolUse", payload, root, env)
        assert send(sock, req)["status"] == "ok"
        assert send(sock, req)["status"] == "ok"
        assert sink.read_text() == marker + "\n"

    def test_client_disconnect_still_completes_dispatch(self, worker: tuple[str, Path, dict], tmp_path: Path) -> None:
        sock_path, root, env = worker
        sink = tmp_path / "disc.txt"
        marker = f"kept-{time.monotonic_ns()}"
        payload = json.dumps(
            {
                "session_id": "sx",
                "tool_name": "Bash",
                "tool_input": {"sleep_ms": 200, "sink": str(sink), "marker": marker},
            }
        )
        conn = send(
            sock_path, event_req("PostToolUse", payload, root, env, extra_env={"CAPT_HOOK_ONCE_TTL": "0"}), read=False
        )
        conn.close()  # disconnect before reading the response
        deadline = time.monotonic() + 3
        while not sink.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert sink.read_text() == marker + "\n"


class TestConcurrency:
    def _warm(self, sock: str, root: Path, env: dict) -> None:
        payload = json.dumps({"session_id": "warm", "tool_name": "Bash", "tool_input": {}})
        send(sock, event_req("PostToolUse", payload, root, env, extra_env={"CAPT_HOOK_ONCE_TTL": "0"}))

    def test_slow_hook_in_one_session_does_not_block_another(self, worker: tuple[str, Path, dict]) -> None:
        sock, root, env = worker
        self._warm(sock, root, env)
        elapsed: dict[str, float] = {}

        def fire(name: str, session: str, sleep_ms: int) -> None:
            payload = json.dumps({"session_id": session, "tool_name": "Bash", "tool_input": {"sleep_ms": sleep_ms}})
            start = time.perf_counter()
            send(sock, event_req("PostToolUse", payload, root, env, extra_env={"CAPT_HOOK_ONCE_TTL": "0"}))
            elapsed[name] = time.perf_counter() - start

        threads = [
            threading.Thread(target=fire, args=("slow", "A", 400)),
            threading.Thread(target=fire, args=("fast", "B", 0)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert elapsed["slow"] > elapsed["fast"] + 0.25

    def test_two_requests_in_one_session_serialize(self, worker: tuple[str, Path, dict]) -> None:
        sock, root, env = worker
        self._warm(sock, root, env)

        def fire() -> None:
            payload = json.dumps({"session_id": "same", "tool_name": "Bash", "tool_input": {"sleep_ms": 150}})
            send(sock, event_req("PostToolUse", payload, root, env, extra_env={"CAPT_HOOK_ONCE_TTL": "0"}))

        threads = [threading.Thread(target=fire), threading.Thread(target=fire)]
        start = time.perf_counter()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert time.perf_counter() - start >= 0.25

    def test_slowloris_does_not_block_a_normal_client(self, worker: tuple[str, Path, dict]) -> None:
        # A peer that connects and dribbles bytes without ever completing a request line must not
        # wedge the accept loop: the read runs off the accept thread, so a normal client is served.
        sock, root, env = worker
        self._warm(sock, root, env)
        slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        slow.connect(sock)
        slow.sendall(b'{"partial":')  # no newline; the request line never completes
        try:
            payload = json.dumps({"session_id": "normal", "tool_name": "Bash", "tool_input": {}})
            start = time.perf_counter()
            resp = send(sock, event_req("PostToolUse", payload, root, env, extra_env={"CAPT_HOOK_ONCE_TTL": "0"}))
            assert resp["status"] == "ok"
            assert time.perf_counter() - start < 5.0, "a slowloris peer wedged the accept loop"
        finally:
            slow.close()

    def test_many_slow_peers_do_not_starve_a_normal_client(self, worker: tuple[str, Path, dict]) -> None:
        # V3: more slow peers than the intake pool has threads, each dribbling without completing a request
        # line, must not delay a real request — the aggressive intake read deadline drops them fast.
        sock, root, env = worker
        self._warm(sock, root, env)
        slows: list[socket.socket] = []
        try:
            for _ in range(16):
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(sock)
                s.sendall(b'{"partial":')  # no newline; the request line never completes
                slows.append(s)
            start = time.perf_counter()
            resp = send(sock, control_req("ping"))
            elapsed = time.perf_counter() - start
            assert resp["status"] == "ok"
            assert elapsed < 10.0, f"16 slow peers starved a normal ping: {elapsed:.2f}s"
        finally:
            for s in slows:
                s.close()

    def test_same_session_flood_does_not_starve_another_session(self, worker: tuple[str, Path, dict]) -> None:
        # A session firing more requests than the event pool has threads must not starve other
        # sessions: same-session requests serialize in a per-session queue (one pool thread at a
        # time), leaving threads free, rather than each blocking a thread on the session's lock.
        sock, root, env = worker
        self._warm(sock, root, env)

        def flood() -> None:
            payload = json.dumps({"session_id": "floodA", "tool_name": "Bash", "tool_input": {"sleep_ms": 150}})
            send(
                sock,
                event_req("PostToolUse", payload, root, env, extra_env={"CAPT_HOOK_ONCE_TTL": "0"}),
                timeout=30,
            )

        threads = [threading.Thread(target=flood) for _ in range(16)]
        for thread in threads:
            thread.start()
        time.sleep(0.1)  # let the flood's reads land and queue behind session floodA's single slot
        try:
            payload_b = json.dumps({"session_id": "sessB", "tool_name": "Bash", "tool_input": {}})
            start = time.perf_counter()
            resp = send(sock, event_req("PostToolUse", payload_b, root, env, extra_env={"CAPT_HOOK_ONCE_TTL": "0"}))
            elapsed = time.perf_counter() - start
            assert resp["status"] == "ok"
            assert elapsed < 1.0, f"session B was starved by session A's flood: {elapsed:.2f}s"
        finally:
            for thread in threads:
                thread.join()


class TestStandaloneWorkers:
    @pytest.mark.parametrize("kind", ["drain", "shutdown"])
    def test_control_shutdown_stops_the_daemon(self, tmp_path: Path, dirs: dict[str, Path], kind: str) -> None:
        run = Path(tempfile.mkdtemp(dir="/tmp", prefix="chd4"))
        try:
            root = make_project(tmp_path / "proj")
            env = daemon_env_for(root, {**dirs, "run": run})
            sock_path = sock_for(root, env)
            proc = spawn_daemon(root, env, run / "boot.log")
            try:
                wait_ready(sock_path, proc, run / "boot.log")
                assert send(sock_path, control_req(kind))["status"] == "ok"
                deadline = time.monotonic() + 5
                while os.path.exists(sock_path) and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert not os.path.exists(sock_path)
                assert proc.wait(timeout=5) == 0
            finally:
                stop_daemon(sock_path, proc)
        finally:
            shutil.rmtree(run, ignore_errors=True)

    def test_stale_socket_takeover(self, tmp_path: Path, dirs: dict[str, Path]) -> None:
        run = Path(tempfile.mkdtemp(dir="/tmp", prefix="chd2"))
        try:
            root = make_project(tmp_path / "proj")
            env = daemon_env_for(root, {**dirs, "run": run})
            sock_path = sock_for(root, env)
            Path(sock_path).write_bytes(b"")  # a dead file squatting the socket path
            proc = spawn_daemon(root, env, run / "boot.log")
            try:
                wait_ready(sock_path, proc, run / "boot.log")
                assert send(sock_path, control_req("ping"))["status"] == "ok"
            finally:
                stop_daemon(sock_path, proc)
        finally:
            shutil.rmtree(run, ignore_errors=True)

    def test_client_roundtrip_matches_cold(self, tmp_path: Path, dirs: dict[str, Path]) -> None:
        run = Path(tempfile.mkdtemp(dir="/tmp", prefix="chd3"))
        try:
            root = make_project(tmp_path / "cproj")
            env = daemon_env_for(root, {**dirs, "run": run}) | {"CAPT_HOOK_DAEMON_FALLBACK": "closed"}
            raw = json.dumps(
                {
                    "session_id": "cx",
                    "tool_name": "Edit",
                    "tool_input": {"file_path": "a.py", "old_string": "x", "new_string": "y"},
                }
            )
            client_bin = str(Path(sys.executable).parent / "hook")
            proc = subprocess.run(
                [client_bin, "--root", str(root), "run", "PreToolUse"],
                input=raw.encode(),
                capture_output=True,
                env=env,
                cwd=str(root),
                timeout=60,
                check=False,
            )
            cold = cold_run("PreToolUse", raw, root, env)
            assert proc.returncode == cold.returncode
            assert proc.stdout == cold.stdout
            assert proc.stderr == cold.stderr
        finally:
            sock_path = sock_for(root, env)
            if os.path.exists(sock_path):
                with contextlib.suppress(OSError):
                    send(sock_path, control_req("shutdown"), timeout=5)
                deadline = time.monotonic() + 5
                while os.path.exists(sock_path) and time.monotonic() < deadline:
                    time.sleep(0.05)
            shutil.rmtree(run, ignore_errors=True)


class TestClientBuildRestart:
    def test_client_header_change_alone_does_not_arm_restart(self, tmp_path: Path) -> None:
        from captain_hook.daemon.server import Server

        root = make_project(tmp_path / "proj")
        server = Server(root, foreground=True)
        try:
            assert server._note_client_build("b1") is None  # first seen: record, no restart
            assert server._note_client_build("b1") is None  # unchanged header
            # A differing header with an unchanged daemon build_id (e.g. a CAPT_HOOK_CLIENT_BUILD
            # override) is not proof the install changed, so no restart is armed.
            assert server._note_client_build("b2") is None
        finally:
            server.event_pool.shutdown(wait=False)
            server.control_pool.shutdown(wait=False)

    def test_real_build_change_arms_a_restart(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from captain_hook.daemon import lifecycle
        from captain_hook.daemon.server import Server

        root = make_project(tmp_path / "proj")
        server = Server(root, foreground=True)
        try:
            assert server._note_client_build("b1") is None
            # Scripted build-id change: the daemon's own recomputation now differs from startup.
            monkeypatch.setattr(lifecycle, "build_id", lambda: f"{server.build}-moved")
            assert server._note_client_build("b2") is not None  # armed; never invoked (would execv)
        finally:
            server.event_pool.shutdown(wait=False)
            server.control_pool.shutdown(wait=False)


class TestRestartDrain:
    def test_restart_drains_inflight_before_reexec(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # A watchdog restart must wait for an in-flight dispatch (LLM gates run many seconds) to
        # finish before execv, not hard-kill it at a short cap and leave the client failing open.
        from captain_hook.daemon import lifecycle
        from captain_hook.daemon import server as server_mod

        assert server_mod.INFLIGHT_DRAIN_S >= 30, "the drain cap must cover realistic LLM-gate durations"
        root = make_project(tmp_path / "proj")
        server = server_mod.Server(root, foreground=True)
        monkeypatch.setattr(server_mod.logger, "remove", lambda *a, **k: None)
        gate = threading.Event()
        finished: list[bool] = []
        observed_finished_at_reexec: list[bool] = []
        monkeypatch.setattr(lifecycle, "reexec", lambda _argv: observed_finished_at_reexec.append(bool(finished)))

        def slow_dispatch() -> None:
            gate.wait(5)
            finished.append(True)

        future = server.event_pool.submit(slow_dispatch)
        with server.inflight_guard:
            server.inflight.add(future)
        server.restart = True
        threading.Timer(0.3, gate.set).start()
        server.teardown()
        assert finished == [True], "the in-flight dispatch was cut off before it completed"
        assert observed_finished_at_reexec == [True], "execv ran before the in-flight dispatch drained"

    def test_drain_waits_for_scheduler_queued_same_session_work(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # V5: request A running + B queued for the SAME session. The drain must not return when A's future
        # completes (before A's callback advances B) — that would drop B after the client already sent.
        from captain_hook.daemon import server as server_mod
        from captain_hook.daemon.protocol import decode_request

        root = make_project(tmp_path / "proj")
        server = server_mod.Server(root, foreground=True)
        try:
            started: list[object] = []
            gate_a = threading.Event()

            def fake_process(conn: socket.socket, req: object) -> None:
                started.append(req)
                if len(started) == 1:
                    gate_a.wait(5)  # A blocks so B queues behind it on the same session
                conn.close()

            monkeypatch.setattr(server, "_process", fake_process)
            payload = json.dumps({"session_id": "S", "tool_name": "Bash", "tool_input": {}})
            req = decode_request(json.dumps(event_req("PostToolUse", payload, root, os.environ)).encode())
            la, ra = socket.socketpair()
            server._begin_active()
            server._schedule_event(la, req)  # A: launched, running (blocked)
            deadline = time.monotonic() + 3
            while not started and time.monotonic() < deadline:
                time.sleep(0.01)
            lb, rb = socket.socketpair()
            server._begin_active()
            server._schedule_event(lb, req)  # B: queued behind A on session S
            assert server.sessions["S"].pending, "B was not queued behind A"
            threading.Timer(0.2, gate_a.set).start()
            server._wait_inflight(server_mod.INFLIGHT_DRAIN_S)
            assert len(started) == 2, "the drain returned before the scheduler-queued B dispatched"
            assert server.sessions == {}, "the session queue was not fully drained"
            assert not any(not f.done() for f in server.inflight), "a future was still running after the drain"
            ra.close()
            rb.close()
        finally:
            server.intake_pool.shutdown(wait=False)
            server.event_pool.shutdown(wait=False)
            server.control_pool.shutdown(wait=False)

    def test_teardown_at_drain_cap_does_not_deadlock_on_a_queued_successor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # At the drain cap, cancel_futures runs a cancelled future's callback under the executor lock;
        # advancing a queued successor there must not re-enter submit (which would deadlock teardown).
        from concurrent.futures import ThreadPoolExecutor

        from captain_hook.daemon import server as server_mod
        from captain_hook.daemon.protocol import decode_request

        root = make_project(tmp_path / "proj")
        server = server_mod.Server(root, foreground=True)
        monkeypatch.setattr(server_mod.logger, "remove", lambda *a, **k: None)
        monkeypatch.setattr(server_mod, "INFLIGHT_DRAIN_S", 0.3)
        server.event_pool.shutdown(wait=False)
        server.event_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-event")
        socks = [socket.socketpair() for _ in range(3)]
        gate = threading.Event()
        try:

            def blocking_process(conn: socket.socket, _req: object) -> None:
                gate.wait(30)  # A hangs past the drain cap; A2 stays queued behind it on the single thread
                conn.close()

            monkeypatch.setattr(server, "_process", blocking_process)

            def sched(session: str, sock: socket.socket) -> None:
                payload = json.dumps({"session_id": session, "tool_name": "Bash", "tool_input": {}})
                req = decode_request(json.dumps(event_req("PostToolUse", payload, root, os.environ)).encode())
                server._begin_active()
                server._schedule_event(sock, req)

            sched("S", socks[0][0])  # A: runs on the single thread, hangs
            deadline = time.monotonic() + 3
            while server.event_load < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            sched("T", socks[1][0])  # A2(T): queued in the executor behind A
            sched("T", socks[2][0])  # B2(T): scheduler-queued behind A2 on session T
            assert server.sessions["T"].pending, "B2 was not queued behind A2"

            done = threading.Event()
            threading.Thread(target=lambda: (server.teardown(), done.set()), daemon=True).start()
            assert done.wait(15), "teardown deadlocked advancing a queued successor at the drain cap"
            gate.set()  # release A so it too drains
            balanced = time.monotonic() + 5
            while (server.active or server.event_load or server.sessions) and time.monotonic() < balanced:
                time.sleep(0.02)
            assert server.active == 0 and server.event_load == 0 and server.sessions == {}
        finally:
            gate.set()
            server.event_pool.shutdown(wait=False, cancel_futures=True)
            server.intake_pool.shutdown(wait=False)
            server.control_pool.shutdown(wait=False)
            for left, right in socks:
                left.close()
                right.close()


class TestIdleExitRace:
    @contextlib.contextmanager
    def _bound_server(self, monkeypatch: pytest.MonkeyPatch):
        from captain_hook.daemon.protocol import socket_path
        from captain_hook.daemon.server import Server

        run = Path(tempfile.mkdtemp(dir="/tmp", prefix="chd-idle-"))
        monkeypatch.setenv("CAPT_HOOK_RUN_DIR", str(run))
        server = Server(Path(tempfile.mkdtemp(prefix="chd-idle-proj-")), foreground=True)
        server.listener = server._bind()
        sock_path = str(socket_path(server.key))
        try:
            yield server, sock_path
        finally:
            with contextlib.suppress(OSError):
                server.listener.close()
            server.intake_pool.shutdown(wait=False, cancel_futures=True)
            server.event_pool.shutdown(wait=False, cancel_futures=True)
            server.control_pool.shutdown(wait=False, cancel_futures=True)
            shutil.rmtree(run, ignore_errors=True)

    def test_idle_exit_unlinks_first_then_drains_backlog_before_closing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # V8: idle-exit unlinks the socket FIRST (a racing connect now fails pre-send → cold), then
        # serve_forever drains and serves the backlog before closing — a pre-unlink client is not dropped.
        from captain_hook.daemon import lifecycle

        with self._bound_server(monkeypatch) as (server, sock_path):
            drained: list[socket.socket] = []
            monkeypatch.setattr(server, "_intake", lambda conn: (drained.append(conn), conn.close()))
            monkeypatch.setattr(lifecycle, "idle_limit", lambda: 0.0)
            server.last_activity = time.monotonic() - 10_000
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(sock_path)  # lands in the backlog before idle-exit runs
            try:
                server._maybe_idle_exit()  # commits to exit: unlinks the socket, sets stop
                assert server.stop_event.is_set()
                assert not os.path.exists(sock_path), "the socket was not unlinked before the drain"
                server._drain_and_close()  # serve_forever's post-loop step: serve the backlog, then close
                assert len(drained) == 1, "the backlogged connection was dropped instead of drained"
            finally:
                client.close()

    def test_connect_after_idle_unlink_fails_pre_send(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # V8: once idle-exit unlinks the socket, a racing connect() fails pre-send → the client runs cold.
        from captain_hook.daemon import lifecycle

        with self._bound_server(monkeypatch) as (server, sock_path):
            monkeypatch.setattr(lifecycle, "idle_limit", lambda: 0.0)
            server.last_activity = time.monotonic() - 10_000
            server._maybe_idle_exit()
            late = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                with pytest.raises((FileNotFoundError, ConnectionRefusedError)):
                    late.connect(sock_path)
            finally:
                late.close()

    def test_idle_exit_deferred_while_a_connection_is_active(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Idle-exit must not fire while any connection is in flight (accepted but not yet completed).
        from captain_hook.daemon import lifecycle

        with self._bound_server(monkeypatch) as (server, _sock_path):
            monkeypatch.setattr(lifecycle, "idle_limit", lambda: 0.0)
            server.last_activity = time.monotonic() - 10_000
            server._begin_active()
            server._maybe_idle_exit()
            assert not server.stop_event.is_set(), "idle-exit fired while a connection was active"
            server._end_active()
            server._maybe_idle_exit()
            assert server.stop_event.is_set(), "idle-exit failed to fire once fully idle"


class TestSessionScheduler:
    def test_scheduler_evicts_session_and_balances_load_even_on_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The per-session queue and the backlog counter must return to empty after work drains —
        # including when a dispatch errors — so self.sessions cannot grow unboundedly across sessions.
        from captain_hook.daemon.protocol import decode_request
        from captain_hook.daemon.server import Server

        root = make_project(tmp_path / "proj")
        server = Server(root, foreground=True)
        try:
            calls = {"n": 0}

            def fake_process(conn: socket.socket, _req: object) -> None:
                conn.close()
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("simulated dispatch error")  # the first same-session dispatch errors

            monkeypatch.setattr(server, "_process", fake_process)
            payload = json.dumps({"session_id": "S", "tool_name": "Bash", "tool_input": {}})
            req = decode_request(json.dumps(event_req("PostToolUse", payload, root, os.environ)).encode())
            for _ in range(2):
                left, right = socket.socketpair()
                server._begin_active()  # mirror _intake's accounting for an accepted connection
                server._schedule_event(left, req)
                right.close()
            deadline = time.monotonic() + 5
            while (server.sessions or server.event_load or server.active) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.sessions == {}, "the session queue was not evicted after its work drained"
            assert server.event_load == 0, "the event backlog counter did not balance back to zero"
            assert server.active == 0, "the active-connection counter did not balance back to zero"
            assert calls["n"] == 2, "the second same-session request never advanced past the errored first"
        finally:
            server.intake_pool.shutdown(wait=False, cancel_futures=True)
            server.event_pool.shutdown(wait=False, cancel_futures=True)
            server.control_pool.shutdown(wait=False, cancel_futures=True)


class TestTeardownRace:
    def test_intake_that_cannot_schedule_after_shutdown_closes_and_balances(self, tmp_path: Path) -> None:
        # V4: an intake task that reaches scheduling after the event pool is shut must not leave the
        # accepted client hanging with a leaked active/event slot — it closes the connection and balances.
        from captain_hook.daemon.protocol import decode_request
        from captain_hook.daemon.server import Server

        root = make_project(tmp_path / "proj")
        server = Server(root, foreground=True)
        try:
            server.event_pool.shutdown(wait=False)  # the event pool is already down when scheduling runs
            left, right = socket.socketpair()
            server._begin_active()  # mirror _intake's accounting for an accepted connection
            payload = json.dumps({"session_id": "S", "tool_name": "Bash", "tool_input": {}})
            req = decode_request(json.dumps(event_req("PostToolUse", payload, root, os.environ)).encode())
            server._schedule_event(left, req)
            assert server.active == 0, "active leaked when the intake task could not schedule"
            assert server.event_load == 0, "event_load leaked"
            assert server.sessions == {}, "the session queue leaked"
            with pytest.raises(OSError):
                left.sendall(b"x")  # the connection was closed
            right.close()
        finally:
            server.intake_pool.shutdown(wait=False)
            server.control_pool.shutdown(wait=False)


class TestNonStrSessionId:
    def test_session_id_of_treats_non_str_as_absent(self) -> None:
        from captain_hook.daemon.protocol import decode_request
        from captain_hook.daemon.server import _session_id_of

        def sid(value: object) -> str | None:
            payload = json.dumps({"session_id": value, "tool_name": "Bash", "tool_input": {}})
            raw = json.dumps(event_req("PostToolUse", payload, Path("/x"), {})).encode()
            return _session_id_of(decode_request(raw))

        assert sid("real") == "real"
        assert sid(["bad"]) is None
        assert sid({"k": "v"}) is None
        assert sid(None) is None

    def test_non_str_session_id_does_not_leak_the_intake_slot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # V7: a non-str (unhashable) session_id must not crash the scheduler (sessions.setdefault raises),
        # which would leave active/event_load stuck and the client failing open. It schedules session-less.
        from captain_hook.daemon.protocol import decode_request
        from captain_hook.daemon.server import Server

        root = make_project(tmp_path / "proj")
        server = Server(root, foreground=True)
        try:
            monkeypatch.setattr(server, "_process", lambda conn, _req: conn.close())
            left, right = socket.socketpair()
            server._begin_active()  # mirror _intake's accounting
            payload = json.dumps({"session_id": ["bad"], "tool_name": "Bash", "tool_input": {}})
            req = decode_request(json.dumps(event_req("PostToolUse", payload, root, os.environ)).encode())
            server._schedule_event(left, req)  # must not raise despite the unhashable session_id
            deadline = time.monotonic() + 5
            while (server.active or server.event_load or server.sessions) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.active == 0, "the intake slot leaked on a non-str session_id"
            assert server.event_load == 0
            assert server.sessions == {}, "a non-str session_id must not create a session-queue entry"
            right.close()
        finally:
            server.intake_pool.shutdown(wait=False)
            server.event_pool.shutdown(wait=False)
            server.control_pool.shutdown(wait=False)

    def test_non_str_session_id_dispatch_matches_cold(self, worker: tuple[str, Path, dict]) -> None:
        # V7: with the scheduler no longer crashing, dispatch handles the bad session_id with cold parity
        # (the ensure_session TypeError, exit 1), and the worker keeps serving afterwards.
        sock, root, env = worker
        bad = json.dumps({"session_id": ["bad"], "tool_name": "Bash", "tool_input": {"command": "x"}})
        resp = send(sock, event_req("PreToolUse", bad, root, env))
        cold = cold_run("PreToolUse", bad, root, env)
        assert resp["status"] == "error"
        assert resp["exit"] == 1 == cold.returncode
        tail = "TypeError: unsupported operand type(s) for /: 'PosixPath' and 'list'"
        assert tail in resp["stderr"] and tail in cold.stderr.decode()
        ok = json.dumps({"session_id": "ok", "tool_name": "Bash", "tool_input": {}})
        assert send(sock, event_req("PreToolUse", ok, root, env))["status"] == "ok"


class TestPeerCredentialCheck:
    def test_same_uid_peer_reads_our_euid(self) -> None:
        from captain_hook.daemon.server import _peer_uid

        left, right = socket.socketpair()
        try:
            assert _peer_uid(left) == os.geteuid()
        finally:
            left.close()
            right.close()

    def test_untrusted_peer_uid_is_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # S1/S2: a peer whose uid does not match our euid is dropped at intake, never admitted.
        from captain_hook.daemon import server as server_mod

        root = make_project(tmp_path / "proj")
        server = server_mod.Server(root, foreground=True)
        try:
            monkeypatch.setattr(server_mod, "_peer_uid", lambda _conn: os.geteuid() + 1)
            left, right = socket.socketpair()
            server._intake(left)
            assert server.active == 0, "an untrusted peer was admitted"
            with pytest.raises(OSError):
                left.sendall(b"x")  # intake closed our side
            right.close()
        finally:
            server.intake_pool.shutdown(wait=False)
            server.event_pool.shutdown(wait=False)
            server.control_pool.shutdown(wait=False)


class TestMetaHardening:
    def test_write_meta_refuses_a_symlink(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # S1/S2: O_NOFOLLOW means a pre-planted symlink at the meta path is refused, not written through.
        from captain_hook.daemon.protocol import meta_path
        from captain_hook.daemon.server import Server

        run = Path(tempfile.mkdtemp(dir="/tmp", prefix="chd-meta-"))
        monkeypatch.setenv("CAPT_HOOK_RUN_DIR", str(run))
        server = Server(make_project(tmp_path / "proj"), foreground=True)
        try:
            target = run / "target.json"
            target.write_text("ORIGINAL")
            Path(meta_path(server.key)).symlink_to(target)
            with pytest.raises(OSError):
                server._write_meta()
            assert target.read_text() == "ORIGINAL", "meta write followed a symlink"
        finally:
            server.intake_pool.shutdown(wait=False)
            server.event_pool.shutdown(wait=False)
            server.control_pool.shutdown(wait=False)
            shutil.rmtree(run, ignore_errors=True)


class TestDiscoveryDiagnosticsReplay:
    def test_discovery_warning_replayed_on_every_request(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolate_modules: None
    ) -> None:
        # Cold re-prints discovery diagnostics ("packs unavailable ...") on every invocation. Warm
        # discovers once at build; the captured warning must replay into every request served from that
        # snapshot, so a cache hit (request 2) carries it byte-for-byte with the build (request 1).
        from captain_hook.daemon.context import install_context_io
        from captain_hook.daemon.protocol import decode_request
        from captain_hook.daemon.server import Server
        from captain_hook.packs import manager

        monkeypatch.setattr(manager, "resolve_enabled_packs", lambda _root: ([], ["ghost"]))
        root = make_project(tmp_path / "proj")
        env = os.environ | {"CAPT_HOOK_ONCE_TTL": "0"}
        payload = json.dumps({"tool_name": "Bash", "tool_input": {}})
        req = decode_request(json.dumps(event_req("PostToolUse", payload, root, env)).encode())

        saved = (sys.stdout, sys.stderr)
        install_context_io()
        server = Server(root, foreground=True)
        try:
            built = server._run_event(req)  # cache miss: builds and captures the discovery warning
            hit = server._run_event(req)  # cache hit: no fresh discover, replays the snapshot's warning
        finally:
            server.event_pool.shutdown(wait=False)
            server.control_pool.shutdown(wait=False)
            sys.stdout, sys.stderr = saved

        assert "packs unavailable (offline and not cached): ghost" in built.stderr
        assert built.stdout == "" and hit.stdout == ""
        assert hit.stderr == built.stderr  # the cache hit replays the build's diagnostics verbatim


def make_raising_project(root: Path) -> Path:
    (hooks := root / ".claude" / "hooks").mkdir(parents=True)
    (hooks / "h.py").write_text(RAISING_HOOK_SRC)
    return root


class TestErrorParity:
    def test_uncaught_dispatch_error_relays_the_traceback_like_cold(
        self, tmp_path: Path, dirs: dict[str, Path]
    ) -> None:
        run = Path(tempfile.mkdtemp(dir="/tmp", prefix="chderr"))
        try:
            root = make_raising_project(tmp_path / "boomproj")
            env = daemon_env_for(root, {**dirs, "run": run})
            sock_path = sock_for(root, env)
            proc = spawn_daemon(root, env, run / "boot.log")
            try:
                wait_ready(sock_path, proc, run / "boot.log")
                payload = json.dumps(
                    {"session_id": "boom", "tool_name": "Bash", "tool_input": {"boom": True, "command": "x"}}
                )
                resp = send(sock_path, event_req("PreToolUse", payload, root, env))
                cold = cold_run("PreToolUse", payload, root, env)

                assert resp["status"] == "error"
                assert resp["exit"] == 1 == cold.returncode
                assert "Traceback (most recent call last):" in resp["stderr"]
                assert "ValueError: boom from condition" in resp["stderr"]
                # The upper frames legitimately differ (daemon stack vs cold stack), but the tail — the
                # hook's own frame and the exception line — matches byte-for-byte (same project path, so
                # no absolute-path divergence between the two runs).
                warm_tail = resp["stderr"].rstrip().splitlines()[-3:]
                cold_tail = cold.stderr.decode().rstrip().splitlines()[-3:]
                assert warm_tail == cold_tail
                assert "in check" in "\n".join(warm_tail)
            finally:
                stop_daemon(sock_path, proc)
        finally:
            shutil.rmtree(run, ignore_errors=True)
