"""The resident worker: a Unix-socket server that dispatches hook events warm.

One request per connection, newline-JSON (:mod:`captain_hook.daemon.protocol`). Events run on a
16-thread pool, control kinds (ping/status/drain/shutdown) on a separate 2-thread pool so a ping
never starves behind a slow hook. Each event is served under a per-session lock — same session
serializes, different sessions interleave — and inside a :func:`request_scope` that binds the
request's env, captures its stdout/stderr, and routes its logs to the session file. The event flow
mirrors cold ``run_event`` byte-for-byte (event validation, empty stdin, the once-guard, malformed
stdin, then dispatch), so the client's response is indistinguishable from a cold run. Dispatch
always runs to completion, even if the client has disconnected, so no side effect is half-applied.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import os
import signal
import socket
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import replace
from typing import TYPE_CHECKING

from loguru import logger

from captain_hook import app, decisions
from captain_hook.cli import DECISION_EVENTS, EVENT_NAMES, CliState, dispatch_event
from captain_hook.daemon import decision_writer, lifecycle, transcache
from captain_hook.daemon.context import install_context_io, request_scope
from captain_hook.daemon.logsink import configure_daemon_logging
from captain_hook.daemon.protocol import (
    CONTROL_KINDS,
    PROTOCOL,
    DaemonInfo,
    ProtocolError,
    Response,
    decode_request,
    encode_response,
    log_path,
    meta_path,
    read_line,
    run_dir,
    socket_path,
    validate_sun_path,
    worker_key,
)
from captain_hook.daemon.registry import Registry
from captain_hook.once import claim_once
from captain_hook.session import ensure_session
from captain_hook.types import Event

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from captain_hook.daemon.context import RequestBuffers
    from captain_hook.daemon.protocol import Request

DIST_NAME = "capt-hook"
EVENT_POOL_SIZE = 16
CONTROL_POOL_SIZE = 2
ACCEPT_TIMEOUT = 2.0
READ_TIMEOUT = 30.0
LISTEN_BACKLOG = 128
INFLIGHT_DRAIN_S = 10.0
WRITER_DRAIN_S = 5.0


def default_argv(root: Path, *, foreground: bool) -> list[str]:
    argv = [sys.executable, "-m", "captain_hook", "daemon", "run", "--root", str(root)]
    return [*argv, "--foreground"] if foreground else argv


class Server:
    def __init__(self, root: Path, *, foreground: bool = False, argv: Sequence[str] | None = None) -> None:
        self.root = root
        self.foreground = foreground
        self.argv = list(argv) if argv is not None else default_argv(root, foreground=foreground)
        self.key = worker_key(str(root), os.environ)
        self.cli_state = CliState(root=root, hooks=str(root / ".claude" / "hooks"))
        self.registry = Registry(self.cli_state)
        self.build = lifecycle.build_id()
        self.version = importlib.metadata.version(DIST_NAME)
        self.started_at = time.time()
        self.event_pool = ThreadPoolExecutor(max_workers=EVENT_POOL_SIZE, thread_name_prefix="capt-hook-event")
        self.control_pool = ThreadPoolExecutor(max_workers=CONTROL_POOL_SIZE, thread_name_prefix="capt-hook-control")
        self.locks: dict[str, threading.Lock] = {}
        self.locks_guard = threading.Lock()
        self.inflight: set[Future[None]] = set()
        self.inflight_guard = threading.Lock()
        self.stop_event = threading.Event()
        self.restart = False
        self.client_build: str | None = None
        self.last_activity = time.monotonic()
        self.listener: socket.socket | None = None
        self.router = None
        self.writer: decision_writer.DecisionWriter | None = None
        self.watchdog: lifecycle.Watchdog | None = None

    def run(self) -> None:
        self.start()
        self.register_signals()
        try:
            self.serve_forever()
        finally:
            self.teardown()

    def start(self) -> None:
        self.router = configure_daemon_logging(self.key)
        if not self.foreground:
            self._redirect_stdio()
        install_context_io()
        self.writer = decision_writer.install()
        self.listener = self._bind()
        self._write_meta()
        self.watchdog = lifecycle.Watchdog(self.build, lambda: self._shutdown(restart=True))
        self.watchdog.start()
        logger.info("daemon up: key={} root={} build={} pid={}", self.key, self.root, self.build, os.getpid())

    def register_signals(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self._shutdown(restart=False))
        signal.signal(signal.SIGHUP, lambda *_: lifecycle.drop_caches(self.registry))
        signal.signal(signal.SIGUSR1, lambda *_: lifecycle.dump_stacks())

    def serve_forever(self) -> None:
        while not self.stop_event.is_set():
            try:
                conn, _ = self.listener.accept()
            except TimeoutError:
                self._maybe_idle_exit()
                continue
            except OSError:
                break
            conn.settimeout(READ_TIMEOUT)
            self._intake(conn)

    def teardown(self) -> None:
        if self.watchdog is not None:
            self.watchdog.stop()
        self._wait_inflight(INFLIGHT_DRAIN_S)
        self.event_pool.shutdown(wait=False, cancel_futures=True)
        self.control_pool.shutdown(wait=False, cancel_futures=True)
        if self.writer is not None:
            decisions._WRITER = None
            self.writer.drain(WRITER_DRAIN_S)
        if self.router is not None:
            self.router.close()
        logger.remove()
        if self.restart:
            lifecycle.reexec(self.argv)

    def _intake(self, conn: socket.socket) -> None:
        try:
            req = decode_request(read_line(conn))
        except (ProtocolError, OSError) as exc:
            logger.debug("dropping undecodable request: {}", exc)
            _close(conn)
            return
        self.last_activity = time.monotonic()
        pool = self.control_pool if req.kind in CONTROL_KINDS else self.event_pool
        future = pool.submit(self._process, conn, req)
        with self.inflight_guard:
            self.inflight.add(future)
        future.add_done_callback(self._retire)

    def _retire(self, future: Future[None]) -> None:
        with self.inflight_guard:
            self.inflight.discard(future)

    def _process(self, conn: socket.socket, req: Request) -> None:
        start = time.perf_counter()
        after: Callable[[], None] | None = None
        try:
            response, after = self._route(req)
        except Exception:
            logger.opt(exception=True).error("unhandled error serving a {} request", req.kind)
            response = self._response("error", exit_code=1)
        self._reply(conn, replace(response, elapsed_ms=(time.perf_counter() - start) * 1000))
        if after is not None:
            after()

    def _route(self, req: Request) -> tuple[Response, Callable[[], None] | None]:
        if req.v != PROTOCOL:
            return self._response("rejected"), None
        match req.kind:
            case "ping":
                return self._response("ok"), None
            case "status":
                return self._response("ok", stdout=json.dumps(self._status())), None
            case "drain" | "shutdown":
                return self._response("ok"), lambda: self._shutdown(restart=False)
            case _:
                return self._serve_event(req)

    def _serve_event(self, req: Request) -> tuple[Response, Callable[[], None] | None]:
        after = self._note_client_build(req.client.build)
        return self._run_event(req), after

    def _note_client_build(self, build: str) -> Callable[[], None] | None:
        if self.client_build is None:
            self.client_build = build
            return None
        return (lambda: self._shutdown(restart=True)) if build != self.client_build else None

    def _run_event(self, req: Request) -> Response:
        event_name = req.event or ""
        try:
            event = Event[event_name]
        except KeyError:
            stderr = f"Invalid event type: {event_name!r}. Valid event names are: {EVENT_NAMES}\n"
            return self._response("ok", stderr=stderr, exit_code=1)
        raw_text = req.payload_raw
        if not raw_text.strip():
            return self._response("ok")
        parsed, parse_error = _decode_payload(raw_text)
        session_id = parsed.get("session_id") if isinstance(parsed, dict) else None
        with request_scope(req, session_id) as buffers:
            if event not in DECISION_EVENTS and not claim_once(event_name, raw_text.encode(), async_=req.async_):
                return self._from_buffers(buffers)
            if parse_error is not None:
                print(f"Malformed stdin: {parse_error}", file=sys.stderr)
                return self._from_buffers(buffers)
            self._dispatch(event, parsed, session_id, req)
            return self._from_buffers(buffers)

    def _dispatch(self, event: Event, raw: dict, session_id: str | None, req: Request) -> None:
        with self._session_lock(session_id):
            session_dir = ensure_session(_session(session_id)) if session_id else None
            snapshot = self.registry.get(session_dir)
            with app.use_state(snapshot.state):
                output = dispatch_event(
                    self.cli_state.root,
                    event,
                    raw,
                    session_dir=session_dir,
                    async_=req.async_,
                    transcript_loader=transcache.load,
                )
        if output:
            print(json.dumps(output))

    def _session_lock(self, session_id: str | None) -> contextlib.AbstractContextManager[object]:
        if session_id is None:
            return contextlib.nullcontext()
        with self.locks_guard:
            return self.locks.setdefault(session_id, threading.Lock())

    def _bind(self) -> socket.socket:
        directory = run_dir()
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        sock_path = validate_sun_path(socket_path(self.key))
        if os.path.exists(sock_path):
            if _is_live(sock_path):
                logger.warning("another daemon already owns {}; exiting", sock_path)
                raise SystemExit(0)
            _unlink(sock_path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(sock_path)
        os.chmod(sock_path, 0o600)
        listener.listen(LISTEN_BACKLOG)
        listener.settimeout(ACCEPT_TIMEOUT)
        return listener

    def _redirect_stdio(self) -> None:
        fd = os.open(str(log_path(self.key)), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        try:
            os.dup2(fd, 1)
            os.dup2(fd, 2)
        finally:
            os.close(fd)

    def _write_meta(self) -> None:
        meta_path(self.key).write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "root": str(self.root),
                    "build": self.build,
                    "version": self.version,
                    "protocol": PROTOCOL,
                    "socket": str(socket_path(self.key)),
                    "started_at": self.started_at,
                }
            )
        )

    def _status(self) -> dict[str, object]:
        return {
            "pid": os.getpid(),
            "build": self.build,
            "version": self.version,
            "root": str(self.root),
            "uptime_s": time.time() - self.started_at,
        }

    def _maybe_idle_exit(self) -> None:
        if lifecycle.idle_expired(self.last_activity, time.monotonic(), lifecycle.idle_limit()):
            logger.info("idle past the limit; shutting down")
            self._shutdown(restart=False)

    def _shutdown(self, *, restart: bool) -> None:
        if self.stop_event.is_set():
            return
        self.restart = restart
        self.stop_event.set()
        _unlink(str(socket_path(self.key)))
        if self.listener is not None:
            self.listener.close()

    def _wait_inflight(self, timeout: float) -> None:
        with self.inflight_guard:
            pending = set(self.inflight)
        if pending:
            wait(pending, timeout=timeout)

    def _reply(self, conn: socket.socket, response: Response) -> None:
        try:
            conn.sendall(encode_response(response))
        except OSError:
            logger.debug("client disconnected before the response was sent")
        finally:
            _close(conn)

    def _from_buffers(self, buffers: RequestBuffers) -> Response:
        return self._response("ok", stdout=buffers.stdout.getvalue(), stderr=buffers.stderr.getvalue())

    def _response(self, status: str, *, stdout: str = "", stderr: str = "", exit_code: int = 0) -> Response:
        return Response(
            v=PROTOCOL,
            daemon=DaemonInfo(version=self.version, build=self.build, pid=os.getpid()),
            status=status,
            stdout=stdout,
            stderr=stderr,
            exit=exit_code,
        )


def _decode_payload(raw_text: str) -> tuple[dict | None, Exception | None]:
    try:
        return json.loads(raw_text), None
    except (json.JSONDecodeError, ValueError) as exc:
        return None, exc


def _session(session_id: str):
    from cc_transcript.ids import SessionId

    return SessionId(session_id)


def _is_live(sock_path: str) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(sock_path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _unlink(sock_path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(sock_path)


def _close(conn: socket.socket) -> None:
    with contextlib.suppress(OSError):
        conn.close()
