"""Per-request I/O and env scope for the resident daemon.

Each event runs in a worker thread. :func:`request_scope` binds the request's env, cwd, and client
identity (via :mod:`captain_hook.util.reqenv`), a pair of capture buffers, and a loguru context
carrying the per-session log path, all for one dispatch. :class:`ContextIO` sits in
``sys.stdout``/``sys.stderr`` so a hook's ``print`` lands in the bound request's buffer — never on
the wire between the daemon and another session's client — and falls back to the daemon's own stream
when no request is bound (a stray thread, or the daemon's own startup output). The belt to that
suspenders is a ``dup2`` of fds 1/2 onto the daemon log at server startup, for C-level writes.
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from loguru import logger

from captain_hook.util import reqenv
from captain_hook.util.paths import resolve_log_dir

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from typing import TextIO


class _ClientRequest(Protocol):
    @property
    def ppid(self) -> int: ...


class RequestContext(Protocol):
    @property
    def env(self) -> Mapping[str, str]: ...

    @property
    def cwd(self) -> str | None: ...

    @property
    def client(self) -> _ClientRequest: ...


@dataclass(slots=True)
class RequestBuffers:
    stdout: io.StringIO = field(default_factory=io.StringIO)
    stderr: io.StringIO = field(default_factory=io.StringIO)


_BOUND: ContextVar[RequestBuffers | None] = ContextVar("captain_hook_daemon_io", default=None)


def bound_buffers() -> RequestBuffers | None:
    return _BOUND.get()


@contextmanager
def request_scope(req: RequestContext, session_id: str | None) -> Iterator[RequestBuffers]:
    sid = session_id or "unknown"
    overrides = reqenv.RequestOverrides(
        env=req.env,
        cwd=req.cwd or os.getcwd(),
        client_ppid=req.client.ppid,
        session_id=sid,
    )
    with reqenv.use_request(overrides):
        session_log_path = str(resolve_log_dir() / f"{sid}.log")
        buffers = RequestBuffers()
        token = _BOUND.set(buffers)
        try:
            with logger.contextualize(session_id=sid, session_log_path=session_log_path):
                yield buffers
        finally:
            _BOUND.reset(token)


@contextmanager
def capture_output() -> Iterator[RequestBuffers]:
    """Bind a fresh capture for the duration, isolating stdout/stderr written within it from any outer request's.

    The registry builds a discovered hook set once and replays its diagnostics on every later cache hit;
    this captures that build's output so it never lands only in the buffer of the request that happened to
    trigger the build.
    """
    buffers = RequestBuffers()
    token = _BOUND.set(buffers)
    try:
        yield buffers
    finally:
        _BOUND.reset(token)


class ContextIO(io.TextIOBase):
    def __init__(self, stream: str, fallback: TextIO) -> None:
        self._stream = stream
        self._fallback = fallback

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        if (buffers := _BOUND.get()) is not None:
            return (buffers.stdout if self._stream == "stdout" else buffers.stderr).write(s)
        return self._fallback.write(s)

    def flush(self) -> None:
        if _BOUND.get() is None:
            self._fallback.flush()


def install_context_io() -> None:
    if isinstance(sys.stdout, ContextIO):
        return
    sys.stdout = ContextIO("stdout", sys.stdout)
    sys.stderr = ContextIO("stderr", sys.stderr)
