"""Thin, stdlib-only hook client — forwards an event to the resident daemon, or runs cold.

The ``capt-hook-client`` console script sits in the wired hook-command slot. It hand-rolls
the ``[--root R] run EVENT [--async]`` / ``ping`` grammar the daemon serves and
``os.execv``-passes-through everything else to ``python -m captain_hook`` untouched: non-daemon
commands (``review run`` …) and every invocation carrying an explicit ``--hooks``. The daemon
discovers hooks from the root's own dir, so a custom-hooks request cannot be served warm without
diverging from cold; passing it through keeps it byte-identical by construction. For a recognized
event it reads stdin, connects to (or spawns) this project's warm worker over a Unix socket, and
writes the worker's response bytes back verbatim. ``ping`` is connect-only: it reports on an
existing worker and never spawns one, so inspecting a project cannot boot a daemon.

Whether the request reached the socket decides the fallback. A failure BEFORE the bytes are sent —
no reachable worker, a spawn that dies at startup, a flock contention timeout, a pre-send deadline,
or any OSError in the connect/spawn machinery — takes the cold ``python -m captain_hook`` fallback
(``CAPT_HOOK_DAEMON_FALLBACK``; cold by default), so a gate hook's deny envelope is never lost. A
failure AFTER the bytes are sent — an EOF or reset awaiting the response, a malformed response, or a
post-send deadline — fails OPEN (exit 0, no output): the worker may already have dispatched, and a
cold rerun would double-fire side-effecting hooks. A ``status=error`` response (the worker
dispatched and hit an uncaught error) is relayed verbatim, never rerun cold; only ``status=rejected``
(a provable non-dispatch on the protocol gate) falls back cold.

Never imports :mod:`captain_hook`; ``subprocess`` is imported lazily on the spawn/cold
paths only.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import socket
import sys
import time

from capt_hook_client import key

CONNECT_ATTEMPTS = 3
CONNECT_DELAY = 0.025
SPAWN_POLL = 0.01
LOSER_POLL = 0.025
RECV_CHUNK = 65536

DEFAULT_DEADLINE = 30.0
UPS_DEADLINE = 20.0


class DaemonUnavailable(Exception):
    """Pre-send: no worker was reachable or spawnable, or the connect/spawn machinery failed. Apply the fallback."""


class DeadlineExpired(Exception):
    """Pre-send: the deadline elapsed before the request bytes were sent. Apply the fallback (cold by default)."""


class PostSendFailure(Exception):
    """Post-send: the request was sent and the worker may have dispatched — fail open, never cold."""


class BadResponse(PostSendFailure):
    """Post-send: the worker's response was malformed, truncated, or protocol-mismatched — fail open."""


def main() -> None:
    """Entry point for the ``capt-hook-client`` console script."""
    args = sys.argv[1:]
    match parse_argv(args):
        case None | {"hooks": str()}:
            os.execv(sys.executable, [sys.executable, "-m", "captain_hook", *args])
        case {"verb": "ping", "root": root}:
            sys.exit(do_ping(resolve_root(root)))
        case {"verb": "run", "event": event, "async": async_, "root": root}:
            sys.exit(do_run(event, resolve_root(root), args, async_=async_))


def parse_argv(argv: list[str]) -> dict[str, object] | None:
    root: str | None = None
    hooks: str | None = None
    i = 0
    while i < len(argv):
        match argv[i]:
            case "--root":
                if i + 1 >= len(argv):
                    return None
                root, i = argv[i + 1], i + 2
            case "--hooks":
                if i + 1 >= len(argv):
                    return None
                hooks, i = argv[i + 1], i + 2
            case opt if opt.startswith("--root="):
                root, i = opt.removeprefix("--root="), i + 1
            case opt if opt.startswith("--hooks="):
                hooks, i = opt.removeprefix("--hooks="), i + 1
            case _:
                break
    if i >= len(argv):
        return None
    match argv[i], argv[i + 1 :]:
        case "ping", []:
            return {"verb": "ping", "root": root, "hooks": hooks}
        case "run", [event] if not event.startswith("-"):
            return {"verb": "run", "event": event, "async": False, "root": root, "hooks": hooks}
        case "run", [event, "--async"] if not event.startswith("-"):
            return {"verb": "run", "event": event, "async": True, "root": root, "hooks": hooks}
        case _:
            return None


def resolve_root(root_opt: str | None) -> str:
    return root_opt or os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("FACTORY_PROJECT_DIR") or os.getcwd()


def deadline_seconds(event: str | None) -> float:
    if override := os.environ.get("CAPT_HOOK_CLIENT_TIMEOUT"):
        return float(override)
    return UPS_DEADLINE if event == "UserPromptSubmit" else DEFAULT_DEADLINE


def build_request(event: str, root: str, payload_raw: str, *, async_: bool) -> dict[str, object]:
    return {
        "v": key.PROTOCOL,
        "client": client_meta(),
        "kind": "event",
        "event": event,
        "async": async_,
        "root": root,
        "cwd": os.getcwd(),
        "hooks": None,
        "env": key.request_env(),
        "payload_raw": payload_raw,
    }


def ping_request() -> dict[str, object]:
    return {"v": key.PROTOCOL, "client": client_meta(), "kind": "ping"}


def client_meta() -> dict[str, object]:
    return {"version": "", "build": key.build_fingerprint(), "pid": os.getpid(), "ppid": os.getppid()}


def do_run(event: str, root: str, args: list[str], *, async_: bool) -> int:
    payload = sys.stdin.buffer.read()
    if os.environ.get("CAPT_HOOK_NO_DAEMON") == "1":
        return cold(args, payload)
    deadline_at = time.monotonic() + deadline_seconds(event)
    request = build_request(event, root, payload.decode("utf-8", "surrogateescape"), async_=async_)
    try:
        response = round_trip(request, root, deadline_at)
    except (DaemonUnavailable, DeadlineExpired) as exc:
        return on_daemon_failure(args, payload, str(exc))
    except PostSendFailure as exc:
        breadcrumb(f"{exc}; failing open")
        return 0
    match response.get("status"):
        case "ok" | "error":
            return emit_response(response)
        case "rejected":
            return on_daemon_failure(args, payload, "worker rejected the request protocol version; running cold")
        case status:
            return on_daemon_failure(args, payload, f"worker returned unexpected status {status!r}; running cold")


def do_ping(root: str) -> int:
    sock = try_connect(str(key.socket_path(key.worker_key(root, os.environ))))
    if sock is None:
        breadcrumb("no reachable daemon; ping does not spawn")
        return 1
    deadline_at = time.monotonic() + deadline_seconds(None)
    try:
        response = exchange(sock, ping_request(), deadline_at)
    except (DaemonUnavailable, DeadlineExpired, PostSendFailure) as exc:
        breadcrumb(str(exc))
        return 1
    finally:
        close(sock)
    sys.stdout.write(json.dumps(response) + "\n")
    return 0 if response.get("status") == "ok" else 1


def round_trip(request: dict[str, object], root: str, deadline_at: float) -> dict[str, object]:
    sock = connect_or_spawn(root, deadline_at)
    try:
        return exchange(sock, request, deadline_at)
    finally:
        close(sock)


def connect_or_spawn(root: str, deadline_at: float) -> socket.socket:
    try:
        worker = key.worker_key(root, os.environ)
        sock_path = str(key.socket_path(worker))
        if (sock := try_connect(sock_path)) is not None:
            return sock
        return spawn_and_wait(worker, root, sock_path, deadline_at)
    except OSError as exc:
        raise DaemonUnavailable(f"daemon attempt failed before send: {exc}") from exc


def try_connect(sock_path: str, attempts: int = CONNECT_ATTEMPTS) -> socket.socket | None:
    for attempt in range(attempts):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(sock_path)
            return sock
        except OSError:
            close(sock)
            if attempt + 1 < attempts:
                time.sleep(CONNECT_DELAY)
    return None


def spawn_and_wait(worker: str, root: str, sock_path: str, deadline_at: float) -> socket.socket:
    os.makedirs(str(key.run_dir()), exist_ok=True)
    lock_fd = os.open(str(key.lock_path(worker)), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while time.monotonic() < deadline_at:
            if (sock := try_connect(sock_path, attempts=1)) is not None:
                return sock
            if try_flock(lock_fd):
                return run_winner(worker, root, sock_path, deadline_at)
            time.sleep(LOSER_POLL)
        raise DeadlineExpired("deadline expired before a worker became reachable")
    finally:
        os.close(lock_fd)


def try_flock(lock_fd: int) -> bool:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
            return False
        raise


def run_winner(worker: str, root: str, sock_path: str, deadline_at: float) -> socket.socket:
    if (sock := try_connect(sock_path, attempts=1)) is not None:
        return sock
    unlink_stale(sock_path)
    child = spawn_daemon(worker, root)
    while time.monotonic() < deadline_at:
        if child.poll() is not None:
            raise DaemonUnavailable(f"worker exited early (code {child.returncode})")
        if (sock := try_connect(sock_path, attempts=1)) is not None:
            return sock
        time.sleep(SPAWN_POLL)
    raise DeadlineExpired("deadline expired waiting for the spawned worker")


def spawn_daemon(worker: str, root: str):
    import subprocess

    log = open(str(key.log_path(worker)), "ab")
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "captain_hook", "daemon", "run", "--root", root],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log.close()


def unlink_stale(sock_path: str) -> None:
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass


def exchange(sock: socket.socket, request: dict[str, object], deadline_at: float) -> dict[str, object]:
    if (remaining := deadline_at - time.monotonic()) <= 0:
        raise DeadlineExpired("deadline expired before the request was sent")
    sock.settimeout(remaining)
    try:
        sock.sendall((json.dumps(request) + "\n").encode())
        line = read_line(sock, deadline_at)
    except OSError as exc:
        raise PostSendFailure(f"socket error after send: {exc}") from exc
    return validate(line)


def read_line(sock: socket.socket, deadline_at: float) -> bytes:
    buf = bytearray()
    while b"\n" not in buf:
        if (remaining := deadline_at - time.monotonic()) <= 0:
            raise PostSendFailure("deadline expired awaiting the worker's response")
        sock.settimeout(remaining)
        if not (chunk := sock.recv(RECV_CHUNK)):
            raise PostSendFailure("worker closed the connection before responding")
        buf.extend(chunk)
    return bytes(buf)


def validate(line: bytes) -> dict[str, object]:
    try:
        response = json.loads(line)
    except ValueError as exc:
        raise BadResponse(f"malformed response: {exc}") from exc
    if not isinstance(response, dict):
        raise BadResponse("response was not a JSON object")
    if response.get("v") != key.PROTOCOL:
        raise BadResponse(f"protocol mismatch: worker v={response.get('v')!r} client v={key.PROTOCOL!r}")
    return response


def emit_response(response: dict[str, object]) -> int:
    if stdout := response.get("stdout"):
        sys.stdout.write(str(stdout))
        sys.stdout.flush()
    if stderr := response.get("stderr"):
        sys.stderr.write(str(stderr))
        sys.stderr.flush()
    exit_code = response.get("exit", 0)
    return exit_code if isinstance(exit_code, int) else 0


def on_daemon_failure(args: list[str], payload: bytes, reason: str) -> int:
    breadcrumb(reason)
    match os.environ.get("CAPT_HOOK_DAEMON_FALLBACK", "cold"):
        case "open":
            return 0
        case "closed":
            return 1
        case _:
            return cold(args, payload)


def cold(args: list[str], payload: bytes) -> int:
    import subprocess

    return subprocess.run([sys.executable, "-m", "captain_hook", *args], input=payload).returncode


def close(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass


def breadcrumb(message: str) -> None:
    print(f"capt-hook-client: {message}", file=sys.stderr)
