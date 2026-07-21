"""Newline-delimited JSON wire protocol between the thin client and the resident daemon.

One request per connection: the client sends a single newline-terminated JSON object encoding a
:class:`Request`, and the daemon replies with a newline-terminated :class:`Response`. Worker
identity and on-disk paths come from :mod:`capt_hook_client.key` and are re-exported here
unchanged, so the two processes agree byte-for-byte on which worker serves a request and where
its socket lives. This module is the daemon's decode/encode half; the client owns the mirror.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from capt_hook_client.key import (
    PROTOCOL,
    build_fingerprint,
    lock_path,
    log_path,
    meta_path,
    request_env,
    run_dir,
    socket_path,
    worker_key,
)

if TYPE_CHECKING:
    import socket

__all__ = [
    "CONTROL_KINDS",
    "MAX_LINE",
    "PROTOCOL",
    "REQUEST_KINDS",
    "SUN_PATH_MAX",
    "ClientInfo",
    "DaemonInfo",
    "LineTooLong",
    "ProtocolError",
    "Request",
    "RequestKind",
    "Response",
    "ResponseStatus",
    "build_fingerprint",
    "decode_request",
    "encode_response",
    "lock_path",
    "log_path",
    "meta_path",
    "read_line",
    "request_env",
    "run_dir",
    "socket_path",
    "validate_sun_path",
    "worker_key",
]

# AF_UNIX sun_path is a fixed 104-byte buffer on macOS (108 on Linux); the socket path plus its
# NUL terminator must fit, so a path is usable only strictly under this bound.
SUN_PATH_MAX = 104
# One request must arrive within this many bytes; a client that streams more without a newline is
# hung or hostile and its connection is dropped rather than buffered unboundedly.
MAX_LINE = 32 * 1024 * 1024
RECV_CHUNK = 65536

RequestKind = Literal["event", "ping", "status", "drain", "shutdown"]
ResponseStatus = Literal["ok", "error", "rejected"]
CONTROL_KINDS: frozenset[RequestKind] = frozenset({"ping", "status", "drain", "shutdown"})
REQUEST_KINDS: frozenset[RequestKind] = CONTROL_KINDS | {"event"}


class ProtocolError(Exception):
    """A request could not be decoded — malformed JSON, wrong shape, or an over-long line."""


class LineTooLong(ProtocolError):
    """The request line exceeded :data:`MAX_LINE` before a newline arrived."""


@dataclass(frozen=True, slots=True)
class ClientInfo:
    version: str
    build: str
    pid: int
    ppid: int


@dataclass(frozen=True, slots=True)
class DaemonInfo:
    version: str
    build: str
    pid: int


@dataclass(frozen=True, slots=True)
class Request:
    v: int
    kind: RequestKind
    client: ClientInfo
    event: str | None = None
    async_: bool = False
    root: str | None = None
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    payload_raw: str = ""


@dataclass(frozen=True, slots=True)
class Response:
    v: int
    daemon: DaemonInfo
    status: ResponseStatus
    stdout: str = ""
    stderr: str = ""
    exit: int = 0
    elapsed_ms: float = 0.0


def validate_sun_path(path: str | os.PathLike[str]) -> str:
    text = os.fspath(path)
    if len(os.fsencode(text)) >= SUN_PATH_MAX:
        raise ProtocolError(f"socket path exceeds the {SUN_PATH_MAX}-byte sun_path limit: {text!r}")
    return text


def decode_request(line: bytes) -> Request:
    try:
        data = json.loads(line)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"malformed request JSON: {exc}") from exc
    if type(data) is not dict:
        raise ProtocolError(f"invalid request shape: {data!r}")
    kind = data.get("kind")
    keys = {"v", "kind", "client"}
    if kind == "event":
        keys |= {"event", "async", "root", "cwd", "env", "payload_raw"}
    if kind not in REQUEST_KINDS or set(data) != keys:
        raise ProtocolError(f"invalid request shape: {data!r}")
    client = data["client"]
    if type(client) is not dict or set(client) != {"version", "build", "pid", "ppid"}:
        raise ProtocolError(f"invalid request shape: {data!r}")
    if not (
        type(data["v"]) is int
        and type(client["version"]) is str
        and type(client["build"]) is str
        and type(client["pid"]) is int
        and type(client["ppid"]) is int
    ):
        raise ProtocolError(f"invalid request shape: {data!r}")
    info = ClientInfo(version=client["version"], build=client["build"], pid=client["pid"], ppid=client["ppid"])
    if kind != "event":
        return Request(v=data["v"], kind=kind, client=info)
    env = data["env"]
    if not (
        type(data["event"]) is str
        and type(data["async"]) is bool
        and type(data["root"]) is str
        and type(data["cwd"]) is str
        and type(env) is dict
        and all(type(key) is str and type(value) is str for key, value in env.items())
        and type(data["payload_raw"]) is str
    ):
        raise ProtocolError(f"invalid request shape: {data!r}")
    return Request(
        v=data["v"],
        kind=kind,
        client=info,
        event=data["event"],
        async_=data["async"],
        root=data["root"],
        cwd=data["cwd"],
        env=env,
        payload_raw=data["payload_raw"],
    )


def encode_response(response: Response) -> bytes:
    return (
        json.dumps(
            {
                "v": response.v,
                "daemon": {
                    "version": response.daemon.version,
                    "build": response.daemon.build,
                    "pid": response.daemon.pid,
                },
                "status": response.status,
                "stdout": response.stdout,
                "stderr": response.stderr,
                "exit": response.exit,
                "elapsed_ms": response.elapsed_ms,
            }
        )
        + "\n"
    ).encode()


def read_line(sock: socket.socket, *, deadline: float | None = None) -> bytes:
    buf = bytearray()
    while True:
        if deadline is not None:
            if (remaining := deadline - time.monotonic()) <= 0:
                raise ProtocolError("request line did not arrive before the read deadline")
            sock.settimeout(remaining)
        if not (chunk := sock.recv(RECV_CHUNK)):
            raise ProtocolError("connection closed before a full request line arrived")
        buf.extend(chunk)
        if (nl := buf.find(b"\n")) != -1:
            if nl > MAX_LINE:
                raise LineTooLong(f"request line exceeded {MAX_LINE} bytes")
            return bytes(buf[:nl])
        if len(buf) > MAX_LINE:
            raise LineTooLong(f"request line exceeded {MAX_LINE} bytes")
