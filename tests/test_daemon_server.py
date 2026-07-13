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
            client_bin = str(Path(sys.executable).parent / "capt-hook-client")
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
