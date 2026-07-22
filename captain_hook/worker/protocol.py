from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from typing import BinaryIO

PROTOCOL = 1
MAX_FRAME = 64 * 1024 * 1024
MAX_EVENT_INPUT = 32 * 1024 * 1024


class ProtocolError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Hello:
    build: str


@dataclass(frozen=True, slots=True)
class EventRequest:
    id: int
    event: str
    async_: bool
    root: str
    cwd: str
    env: dict[str, str]
    payload_raw: str
    python: str
    build: str
    client_pid: int
    client_ppid: int


@dataclass(frozen=True, slots=True)
class EventResponse:
    status: Literal["ok", "error"] = "ok"
    stdout: str = ""
    stderr: str = ""
    exit: int = 0
    elapsed_ms: float = 0.0

    def message(self) -> dict[str, object]:
        return {
            "schema": PROTOCOL,
            "status": self.status,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit": self.exit,
            "elapsed_ms": self.elapsed_ms,
        }


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    first = stream.read(1)
    if first == b"":
        return None
    try:
        header = first + _read_exact(stream, 3)
    except ProtocolError as exc:
        raise ProtocolError("truncated frame header") from exc
    length = struct.unpack(">I", header)[0]
    if length == 0 or length > MAX_FRAME:
        raise ProtocolError(f"invalid frame length: {length}")
    payload = _read_exact(stream, length)
    try:
        message = cast(object, json.loads(payload))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"malformed frame JSON: {exc}") from exc
    if type(message) is not dict:
        raise ProtocolError(f"invalid message shape: {message!r}")
    return cast(dict[str, Any], message)


def write_message(stream: BinaryIO, message: dict[str, object]) -> None:
    payload = json.dumps(message, sort_keys=True, separators=(",", ":")).encode()
    if len(payload) > MAX_FRAME:
        raise ProtocolError(f"frame exceeds {MAX_FRAME} bytes")
    _write_all(stream, struct.pack(">I", len(payload)))
    _write_all(stream, payload)
    stream.flush()


def decode_hello(message: dict[str, Any]) -> Hello:
    if (
        set(message) != {"protocol", "op", "build"}
        or type(message["protocol"]) is not int
        or message["protocol"] != PROTOCOL
        or message["op"] != "hello"
        or type(message["build"]) is not str
        or message["build"] == ""
    ):
        raise ProtocolError(f"invalid hello shape: {message!r}")
    return Hello(build=message["build"])


def decode_event(message: dict[str, Any]) -> EventRequest:
    if (
        set(message) != {"protocol", "op", "id", "request"}
        or type(message["protocol"]) is not int
        or message["protocol"] != PROTOCOL
        or message["op"] != "event"
        or type(message["id"]) is not int
        or message["id"] <= 0
        or type(message["request"]) is not dict
    ):
        raise ProtocolError(f"invalid event frame: {message!r}")
    request = cast(dict[str, object], message["request"])
    keys = {
        "schema",
        "event",
        "async",
        "root",
        "cwd",
        "env",
        "payload_raw",
        "python",
        "build",
        "client_pid",
        "client_ppid",
    }
    env = request.get("env")
    if (
        set(request) != keys
        or type(request["schema"]) is not int
        or request["schema"] != PROTOCOL
        or type(request["event"]) is not str
        or request["event"] == ""
        or type(request["async"]) is not bool
        or type(request["root"]) is not str
        or request["root"] == ""
        or type(request["cwd"]) is not str
        or request["cwd"] == ""
        or type(env) is not dict
        or not all(type(key) is str and type(value) is str for key, value in cast(dict[object, object], env).items())
        or type(request["payload_raw"]) is not str
        or len(request["payload_raw"].encode()) > MAX_EVENT_INPUT
        or type(request["python"]) is not str
        or request["python"] == ""
        or type(request["build"]) is not str
        or request["build"] == ""
        or type(request["client_pid"]) is not int
        or request["client_pid"] <= 1
        or type(request["client_ppid"]) is not int
        or request["client_ppid"] <= 0
    ):
        raise ProtocolError(f"invalid event request: {request!r}")
    return EventRequest(
        id=message["id"],
        event=request["event"],
        async_=request["async"],
        root=request["root"],
        cwd=request["cwd"],
        env=cast(dict[str, str], env),
        payload_raw=request["payload_raw"],
        python=request["python"],
        build=request["build"],
        client_pid=request["client_pid"],
        client_ppid=request["client_ppid"],
    )


def hello_response(build: str) -> dict[str, object]:
    return {"protocol": PROTOCOL, "op": "hello", "build": build}


def result_response(request_id: int, response: EventResponse) -> dict[str, object]:
    return {"protocol": PROTOCOL, "op": "result", "id": request_id, "response": response.message()}


def error_response(request_id: int, error: str) -> dict[str, object]:
    return {"protocol": PROTOCOL, "op": "error", "id": request_id, "error": error}


def _read_exact(stream: BinaryIO, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = stream.read(length - len(chunks))
        if chunk == b"":
            raise ProtocolError("truncated frame payload")
        chunks.extend(chunk)
    return bytes(chunks)


def _write_all(stream: BinaryIO, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = stream.write(view)
        if written <= 0:
            raise ProtocolError("short frame write")
        view = view[written:]
