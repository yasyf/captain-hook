"""Daemon-wide loguru configuration: per-session files, a rotated daemon log, and a stderr tee.

Cold, :func:`captain_hook.log.setup_logging` reconfigures loguru per process — one file per session
plus warnings to ``stderr``. The resident daemon serves many sessions from one process, so it
configures loguru once: :class:`SessionFileRouter` fans records out to per-session files by the
``session_log_path`` bound in :func:`captain_hook.daemon.context.request_scope` (byte-identical
format, so ``capt-hook logs`` keeps reading them), a rotated ``daemon-<key>.log`` catches the
daemon's own and unbound records, and :class:`RequestStderrTee` mirrors ``WARNING+`` into the bound
request's stderr buffer — the cold ``stderr`` sink's parity. ``enqueue=False`` throughout, because
the tee reads the emitting thread's request ContextVar.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from typing import TYPE_CHECKING

from loguru import logger

from captain_hook.daemon import context
from captain_hook.log import FILE_FORMAT, MAX_BOUND_VALUE, STDERR_FORMAT
from captain_hook.util.paths import resolve_log_dir

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import TextIO

    from loguru import Message, Record

MAX_SESSION_HANDLES = 64
DAEMON_LOG_ROTATION = "10 MB"
DAEMON_LOG_RETENTION = 5

# Keys bound only for routing/context by request_scope — never rendered into a line (cold logs
# carry neither) and never truncated (the router reads session_log_path to pick the target file).
ROUTING_KEYS = frozenset({"session_id", "session_log_path"})


def truncate_daemon_bound_values(record: Record) -> None:
    for key, value in record["extra"].items():
        if key not in ROUTING_KEYS and isinstance(value, str) and len(value) > MAX_BOUND_VALUE:
            record["extra"][key] = value[:MAX_BOUND_VALUE] + "…"


def daemon_format(template: str) -> Callable[[Record], str]:
    def formatter(record: Record) -> str:
        rendered = {k: v for k, v in record["extra"].items() if k not in ROUTING_KEYS}
        suffix = " | " + str(rendered).replace("{", "{{").replace("}", "}}") if rendered else ""
        return template + suffix + "\n{exception}"

    return formatter


class SessionFileRouter:
    def __init__(self, *, maxsize: int = MAX_SESSION_HANDLES) -> None:
        self._maxsize = maxsize
        self._handles: OrderedDict[str, TextIO] = OrderedDict()
        self._lock = threading.Lock()

    def __call__(self, message: Message) -> None:
        if not (path := message.record["extra"].get("session_log_path")):
            return
        with self._lock:
            handle = self._handle(path)
            handle.write(str(message))
            handle.flush()

    def close(self) -> None:
        with self._lock:
            for handle in self._handles.values():
                handle.close()
            self._handles.clear()

    def _handle(self, path: str) -> TextIO:
        from pathlib import Path

        if (handle := self._handles.get(path)) is not None:
            self._handles.move_to_end(path)
            return handle
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._handles[path] = handle = open(path, "a", encoding="utf-8")
        while len(self._handles) > self._maxsize:
            self._handles.popitem(last=False)[1].close()
        return handle


class RequestStderrTee:
    def __call__(self, message: Message) -> None:
        if (buffers := context.bound_buffers()) is not None:
            buffers.stderr.write(str(message))


def daemon_log_path(key: str) -> Path:
    return resolve_log_dir() / f"daemon-{key}.log"


def create_private_log(path: Path) -> None:
    # 0600 file, O_NOFOLLOW refuses a pre-planted symlink; the log persists hook tracebacks that can
    # carry prompt/tool content, so it is same-user-only like cold's per-session logs.
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def configure_daemon_logging(key: str) -> SessionFileRouter:
    router = SessionFileRouter()
    daemon_log = daemon_log_path(key)
    daemon_log.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(daemon_log.parent, 0o700)
    create_private_log(daemon_log)
    logger.configure(
        patcher=truncate_daemon_bound_values,
        handlers=[
            {"sink": router, "level": "DEBUG", "format": daemon_format(FILE_FORMAT), "enqueue": False},
            {
                "sink": str(daemon_log),
                "level": "DEBUG",
                "format": daemon_format(FILE_FORMAT),
                "filter": lambda record: not record["extra"].get("session_log_path"),
                "rotation": DAEMON_LOG_ROTATION,
                "retention": DAEMON_LOG_RETENTION,
                "encoding": "utf-8",
                "enqueue": False,
            },
            {"sink": RequestStderrTee(), "level": "WARNING", "format": daemon_format(STDERR_FORMAT), "enqueue": False},
        ],
    )
    return router
