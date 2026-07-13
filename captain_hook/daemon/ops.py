"""Ops surface for the resident daemon: enumerate, probe, stop, restart, and tail per-project workers.

The ``hookd {status,stop,restart,logs}`` subcommands drive these helpers. Workers are
matched by the ``root`` recorded in their meta file, never by recomputing a worker key: the ops
shell's environment differs from the worker's, so a recomputed key would miss a running daemon.
Every probe is connect-only — inspecting, stopping, or restarting a project never spawns a worker.
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from capt_hook_client.key import PROTOCOL, log_path, meta_path, run_dir
from captain_hook.daemon.logsink import daemon_log_path
from captain_hook.util.paths import resolve_project_dir
from captain_hook.util.proc import process_start_time

PROBE_TIMEOUT = 2.0
SHUTDOWN_POLL = 0.02
SHUTDOWN_WAIT = 10.0


@dataclass(frozen=True, slots=True)
class Worker:
    key: str
    pid: int
    root: str
    build: str
    version: str
    socket: str
    started_at: float
    proc_start: str | None = None

    @classmethod
    def from_meta(cls, path: Path) -> Worker | None:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        match data:
            case {
                "pid": int() as pid,
                "root": str() as root,
                "build": str() as build,
                "version": str() as version,
                "socket": str() as sock,
                "started_at": (int() | float()) as started,
            }:
                return cls(
                    key=path.stem,
                    pid=pid,
                    root=root,
                    build=build,
                    version=version,
                    socket=sock,
                    started_at=float(started),
                    proc_start=data.get("proc_start"),
                )
            case _:
                return None


@dataclass(frozen=True, slots=True)
class WorkerStatus:
    worker: Worker
    alive: bool

    @property
    def uptime_s(self) -> float | None:
        return time.time() - self.worker.started_at if self.alive else None


@dataclass(frozen=True, slots=True)
class Outcome:
    worker: Worker
    action: str


def scan_workers() -> list[Worker]:
    directory = run_dir()
    if not directory.exists():
        return []
    return sorted(
        (worker for path in directory.glob("*.json") if (worker := Worker.from_meta(path)) is not None),
        key=lambda worker: (worker.root, worker.key),
    )


def match_workers(root: str | None, *, all_: bool) -> list[Worker]:
    workers = scan_workers()
    if all_:
        return workers
    target = os.path.realpath(root or resolve_project_dir() or os.getcwd())
    return [worker for worker in workers if os.path.realpath(worker.root) == target]


def is_alive(worker: Worker) -> bool:
    return send_control(worker.socket, "ping")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def status_workers(root: str | None, *, all_: bool) -> list[WorkerStatus]:
    return [WorkerStatus(worker, is_alive(worker)) for worker in match_workers(root, all_=all_)]


def stop_workers(root: str | None, *, all_: bool) -> list[Outcome]:
    return [_stop_one(worker) for worker in match_workers(root, all_=all_)]


def restart_workers(root: str | None) -> list[Outcome]:
    return [
        Outcome(worker, "draining" if send_control(worker.socket, "drain") else "not running")
        for worker in match_workers(root, all_=False)
    ]


def log_sources(worker: Worker) -> list[tuple[str, Path]]:
    return [("boot", log_path(worker.key)), ("daemon", daemon_log_path(worker.key))]


def read_tail(path: Path, tail: int | None) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return "\n".join(text.splitlines()[-tail:] if tail else text.splitlines())


def status_json(status: WorkerStatus) -> dict[str, object]:
    return {
        "key": status.worker.key,
        "root": status.worker.root,
        "pid": status.worker.pid,
        "build": status.worker.build,
        "version": status.worker.version,
        "socket": status.worker.socket,
        "started_at": status.worker.started_at,
        "uptime_s": status.uptime_s,
        "alive": status.alive,
    }


def format_status_table(statuses: list[WorkerStatus]) -> list[str]:
    header = ("ROOT", "PID", "BUILD", "UPTIME", "STATE")
    rows = [
        (
            status.worker.root,
            str(status.worker.pid),
            status.worker.build[:16],
            format_uptime(status.uptime_s) if status.uptime_s is not None else "-",
            "alive" if status.alive else "stale",
        )
        for status in statuses
    ]
    widths = [max(len(cell) for cell in column) for column in zip(header, *rows, strict=False)]
    return ["  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)) for line in (header, *rows)]


def format_uptime(seconds: float) -> str:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


def send_control(sock_path: str, kind: str) -> bool:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(PROBE_TIMEOUT)
    try:
        sock.connect(sock_path)
        sock.sendall((json.dumps({"v": PROTOCOL, "kind": kind, "client": _ops_client()}) + "\n").encode())
        line = _read_line(sock)
    except OSError:
        return False
    finally:
        _close(sock)
    try:
        return json.loads(line).get("status") == "ok"
    except ValueError:
        return False


def _stop_one(worker: Worker) -> Outcome:
    if send_control(worker.socket, "shutdown"):
        _await_shutdown(worker.socket)
        _cleanup(worker)
        return Outcome(worker, "stopped")
    if worker_process_live(worker):
        return Outcome(worker, "unreachable")
    _cleanup(worker)
    return Outcome(worker, "cleaned")


def worker_process_live(worker: Worker) -> bool:
    # A pid alive but with a start-time that no longer matches the meta was recycled to an unrelated
    # process; the daemon is gone, so its files are stale and cleanable — never signal a recycled pid.
    if not pid_alive(worker.pid):
        return False
    if worker.proc_start is None:
        return True
    # start-time unknown (ps timed out/transient) is not a mismatch: treat as live, never clean.
    if (current := process_start_time(worker.pid)) is None:
        return True
    return current == worker.proc_start


def _await_shutdown(sock_path: str) -> None:
    deadline = time.monotonic() + SHUTDOWN_WAIT
    while time.monotonic() < deadline and os.path.exists(sock_path):
        time.sleep(SHUTDOWN_POLL)


def _cleanup(worker: Worker) -> None:
    _unlink(worker.socket)
    _unlink(str(meta_path(worker.key)))


def _ops_client() -> dict[str, object]:
    return {"version": "", "build": "ops", "pid": os.getpid(), "ppid": os.getppid()}


def _read_line(sock: socket.socket) -> bytes:
    buf = bytearray()
    while b"\n" not in buf:
        if not (chunk := sock.recv(65536)):
            break
        buf.extend(chunk)
    return bytes(buf)


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _close(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass
