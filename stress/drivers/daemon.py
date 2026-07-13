"""Daemon driving: spawn and drive the resident worker through the installed ``capt-hook-client``.

Each scenario runs inside a :class:`DaemonWorld` — a short ``/tmp`` run dir (macOS caps ``sun_path``
at 104 bytes, so the sandbox's own long ``/tmp/capt-stress`` root cannot hold the socket), the
sandbox env extended with the daemon vars plus an isolated ``XDG_CACHE_HOME`` (so the once-guard
sentinel never touches the user's real cache), and a hook project the scenario plants in the sandbox
repo. Clients run under ``CAPT_HOOK_DAEMON_FALLBACK=closed`` by default: a client that never reaches
a worker exits 1, so a silent cold fallback can never mask a missing warm dispatch. Teardown stops
every worker the scenario spawned (SIGTERM by recorded pid, then SIGKILL any survivor) and the run
dir is removed, so no daemon outlives its scenario.

Workers are matched only by the sandbox repo path — every world roots a unique
``/tmp/capt-stress/<run>/<name>/repo`` — so ``stop`` and the pid scans never touch a peer scenario's
worker or the user's real daemons.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from capt_hook_client.key import worker_key
from stress.sandbox import CHECKOUT

if TYPE_CHECKING:
    from collections.abc import Iterator

    from stress.sandbox import Sandbox

CLIENT_BIN = CHECKOUT / ".venv" / "bin" / "capt-hook-client"
COLD_BIN = CHECKOUT / ".venv" / "bin" / "capt-hook"
DAEMON_RUN_PATTERN = "captain_hook daemon run"
STOP_TIMEOUT = 10.0
STOP_POLL = 0.02


@dataclass(frozen=True, slots=True)
class DaemonWorld:
    sandbox: Sandbox
    run: Path
    base_env: dict[str, str]

    @property
    def root(self) -> Path:
        return self.sandbox.repo

    def env(self, *, fallback: str = "closed", **overrides: str) -> dict[str, str]:
        return self.base_env | {"CAPT_HOOK_DAEMON_FALLBACK": fallback} | overrides

    def key(self, env: dict[str, str]) -> str:
        return worker_key(str(self.root), env)

    def sock_path(self, env: dict[str, str]) -> Path:
        return self.run / f"{self.key(env)}.sock"

    def meta_path(self, env: dict[str, str]) -> Path:
        return self.run / f"{self.key(env)}.json"

    def run_client(
        self, event: str, payload: bytes, *, env: dict[str, str], async_: bool = False, timeout: int = 60
    ) -> subprocess.CompletedProcess[bytes]:
        args = ["--root", str(self.root), "run", event, *(("--async",) if async_ else ())]
        return subprocess.run(
            [str(CLIENT_BIN), *args], input=payload, capture_output=True, env=env, cwd=str(self.root), timeout=timeout
        )

    def run_cold(
        self, event: str, payload: bytes, *, env: dict[str, str], timeout: int = 60
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [str(COLD_BIN), "--root", str(self.root), "run", event],
            input=payload,
            capture_output=True,
            env=env,
            cwd=str(self.root),
            timeout=timeout,
        )

    def time_client(self, event: str, payload: bytes, *, env: dict[str, str]) -> tuple[float, int]:
        start = time.perf_counter()
        proc = self.run_client(event, payload, env=env)
        return (time.perf_counter() - start) * 1000, proc.returncode

    def time_cold(self, event: str, payload: bytes, *, env: dict[str, str]) -> tuple[float, int]:
        start = time.perf_counter()
        proc = self.run_cold(event, payload, env=env)
        return (time.perf_counter() - start) * 1000, proc.returncode

    def sockets(self) -> list[Path]:
        return sorted(self.run.glob("*.sock"))

    def metas(self) -> list[Path]:
        return sorted(self.run.glob("*.json"))

    def meta_pid(self, env: dict[str, str]) -> int | None:
        return read_meta_pid(self.meta_path(env))

    def meta_pids(self) -> list[int]:
        return sorted(pid for path in self.metas() if (pid := read_meta_pid(path)) is not None)

    def worker_pids(self) -> list[int]:
        pids: set[int] = set()
        for root in {str(self.root), os.path.realpath(self.root)}:
            proc = subprocess.run(["pgrep", "-f", f"{DAEMON_RUN_PATTERN}.*{root}"], capture_output=True, text=True)
            pids |= {int(line) for line in proc.stdout.split() if line.isdigit()}
        return sorted(pids)

    def live_sockets(self) -> list[str]:
        return [str(path) for path in self.sockets() if connectable(str(path))]

    def stop(self) -> None:
        pids = set(self.meta_pids()) | set(self.worker_pids())
        for pid in pids:
            send_signal(pid, signal.SIGTERM)
        deadline = time.monotonic() + STOP_TIMEOUT
        while time.monotonic() < deadline and any(pid_alive(pid) for pid in pids):
            time.sleep(STOP_POLL)
        for pid in pids:
            if pid_alive(pid):
                send_signal(pid, signal.SIGKILL)
        for path in self.sockets():
            unlink_path(path)


@contextlib.contextmanager
def daemon_world(sandbox: Sandbox) -> Iterator[DaemonWorld]:
    run = Path(tempfile.mkdtemp(dir="/tmp", prefix="chd-f20-"))
    assert len(str(run)) < 70, run
    (cache := sandbox.root / "cache").mkdir(parents=True, exist_ok=True)
    base_env = sandbox.env(CAPT_HOOK_RUN_DIR=str(run), XDG_CACHE_HOME=str(cache))
    world = DaemonWorld(sandbox=sandbox, run=run, base_env=base_env)
    try:
        yield world
    finally:
        world.stop()
        shutil.rmtree(run, ignore_errors=True)


def plant_hooks(sandbox: Sandbox, source: str) -> Path:
    (directory := sandbox.repo / ".claude" / "hooks").mkdir(parents=True, exist_ok=True)
    (directory / "h.py").write_text(source)
    return directory


def read_meta_pid(path: Path) -> int | None:
    try:
        return int(json.loads(path.read_text())["pid"])
    except (OSError, ValueError, KeyError):
        return None


def connectable(sock_path: str) -> bool:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect(sock_path)
        return True
    except OSError:
        return False
    finally:
        sock.close()


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def send_signal(pid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, sig)


def unlink_path(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
