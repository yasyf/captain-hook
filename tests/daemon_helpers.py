"""Shared machinery for the daemon parity and behavior suites: spawn a real worker, drive it
over its socket or through the installed ``hook``, and run the cold CLI with the
same environment so the two paths can be compared byte-for-byte.

The daemon is a per-project subprocess keyed on a short ``/tmp`` run dir (macOS caps
``sun_path`` at 104 bytes) and an isolated state/cache/log/decisions base. Passing the exact
same ``env`` dict to :func:`spawn_daemon`, :func:`run_client`, and :func:`run_cold` keeps the
worker key aligned across all three, so the client always reaches the worker the daemon bound.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from capt_hook_client.key import request_env, worker_key
from captain_hook.daemon.protocol import PROTOCOL

if TYPE_CHECKING:
    from collections.abc import Iterator

CLIENT_BIN = str(Path(sys.executable).parent / "hook")
RECV_CHUNK = 65536

# Env vars that steer daemon/client behavior; scrubbed from the inherited environment so an
# ambient value (a dogfood session, CI) can never perturb a test — each test sets what it needs.
SCRUB_ENV = (
    "CAPT_HOOK_NO_DAEMON",
    "CAPT_HOOK_DAEMON_FALLBACK",
    "CAPT_HOOK_ONCE_TTL",
    "CAPT_HOOK_CLIENT_BUILD",
    "CAPT_HOOK_CLIENT_TIMEOUT",
    "CAPT_HOOK_DAEMON_DEBUG",
    "HOOKS_DAEMON_IDLE_S",
)


def make_project(root: Path, hook_src: str) -> Path:
    (hooks := root / ".claude" / "hooks").mkdir(parents=True)
    (hooks / "__init__.py").write_text("")
    (hooks / "h.py").write_text(hook_src)
    return root


def short_run_dir() -> Path:
    run = Path(tempfile.mkdtemp(dir="/tmp", prefix="chd-"))
    assert len(str(run)) < 70, run
    return run


def daemon_dirs() -> dict[str, Path]:
    dirs = {"run": short_run_dir()} | {
        name: Path(tempfile.mkdtemp(prefix="chd-base-")) / name for name in ("state", "cache", "logs", "decisions")
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def cleanup_dirs(dirs: dict[str, Path]) -> None:
    for path in {dirs["run"], *(p.parent for name, p in dirs.items() if name != "run")}:
        shutil.rmtree(path, ignore_errors=True)


def daemon_env(root: Path, dirs: dict[str, Path], **overrides: str) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
    return (
        base
        | {
            "CAPT_HOOK_RUN_DIR": str(dirs["run"]),
            "CAPTAIN_HOOK_STATE_DIR": str(dirs["state"]),
            "XDG_CACHE_HOME": str(dirs["cache"]),
            "CAPTAIN_HOOK_LOG_DIR": str(dirs["logs"]),
            "CAPT_HOOK_DECISIONS_DB": str(dirs["decisions"] / "d.db"),
            "CLAUDE_PROJECT_DIR": str(root),
        }
        | overrides
    )


def worker_sock(root: Path, env: dict[str, str]) -> str:
    sock = str(Path(env["CAPT_HOOK_RUN_DIR"]) / f"{worker_key(str(root), env)}.sock")
    assert len(sock) < 100, sock
    return sock


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


@contextlib.contextmanager
def running_daemon(root: Path, env: dict[str, str]) -> Iterator[str]:
    """Spawn a worker for ``root``/``env``, yield its socket path, and tear it down after."""
    boot_log = Path(env["CAPT_HOOK_RUN_DIR"]) / "boot.log"
    sock_path = worker_sock(root, env)
    proc = spawn_daemon(root, env, boot_log)
    try:
        wait_ready(sock_path, proc, boot_log)
        yield sock_path
    finally:
        stop_daemon(sock_path, proc)


def client_meta(build: str = "b1") -> dict[str, Any]:
    return {"version": "", "build": build, "pid": os.getpid(), "ppid": os.getppid()}


def control_req(kind: str, *, v: int = PROTOCOL) -> dict[str, Any]:
    return {"v": v, "kind": kind, "client": client_meta()}


def event_req(
    event: str,
    payload: dict | str,
    root: Path,
    env: dict[str, str],
    *,
    async_: bool = False,
    extra_env: dict | None = None,
) -> dict[str, Any]:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return {
        "v": PROTOCOL,
        "kind": "event",
        "client": client_meta(),
        "event": event,
        "async": async_,
        "root": str(root),
        "cwd": str(root),
        "hooks": None,
        "env": request_env(env) | (extra_env or {}),
        "payload_raw": raw,
    }


def send(sock_path: str, request: dict, *, read: bool = True, timeout: float = 15.0):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(sock_path)
    sock.sendall((json.dumps(request) + "\n").encode())
    if not read:
        return sock
    buf = b""
    while b"\n" not in buf:
        if not (chunk := sock.recv(RECV_CHUNK)):
            break
        buf += chunk
    sock.close()
    return json.loads(buf.split(b"\n")[0])


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_meta(root: Path, env: dict[str, str]) -> dict[str, Any]:
    key = worker_key(str(root), env)
    return json.loads((Path(env["CAPT_HOOK_RUN_DIR"]) / f"{key}.json").read_text())


def shutdown_worker(sock_path: str, *, timeout: float = 5.0) -> None:
    """Best-effort shut a worker down over its socket and wait for the socket to vanish."""
    with contextlib.suppress(OSError):
        send(sock_path, control_req("shutdown"), timeout=timeout)
    deadline = time.monotonic() + timeout
    while os.path.exists(sock_path) and time.monotonic() < deadline:
        time.sleep(0.02)


def run_cold(*args: str, env: dict[str, str], stdin: bytes = b"", cwd: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "captain_hook", *args],
        input=stdin,
        capture_output=True,
        env=env,
        cwd=cwd,
        timeout=60,
        check=False,
    )


def run_client(*args: str, env: dict[str, str], stdin: bytes = b"", cwd: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [CLIENT_BIN, *args],
        input=stdin,
        capture_output=True,
        env=env,
        cwd=cwd,
        timeout=60,
        check=False,
    )
