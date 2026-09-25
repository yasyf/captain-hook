from __future__ import annotations

import json
import struct
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

from captain_hook.snapshots.validation import checked, native_numbers, parse_exact

if TYPE_CHECKING:
    from typing import BinaryIO

PROTOCOL = 1
MAX_EVENT_INPUT = 32 * 1024 * 1024
MAX_EVENT_ENVELOPE = 1 * 1024 * 1024
MAX_HOST_PAYLOAD = 2 * MAX_EVENT_INPUT + MAX_EVENT_ENVELOPE
MAX_FRAME = MAX_HOST_PAYLOAD + 4 * 1024
MAX_SNAPSHOT_FRAME = 1024 * 1024

OP_HELLO = "hello"
OP_EVENT = "event"
OP_RESULT = "result"
OP_ERROR = "error"
OP_ADOPT = "adopt"
OP_SNAPSHOT_REQUEST = "snapshot_request"
OP_SNAPSHOT_RESULT = "snapshot_result"
OP_SNAPSHOT_CANCEL = "snapshot_cancel"
SNAPSHOT_FRAME_FIELDS = frozenset({"snapshot", "snapshot_context", "snapshot_config", "parent_id"})

HELLO_KEYS = frozenset({"protocol", "op", "build"})
EVENT_FRAME_KEYS = frozenset({"protocol", "op", "id", "request"})
EVENT_REQUEST_KEYS = frozenset(
    {
        "schema",
        "event",
        "root",
        "cwd",
        "env",
        "payload_raw",
        "client_pid",
        "client_ppid",
        "deadline_unix_ms",
    }
)


class ProtocolError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Hello:
    build: str


@dataclass(frozen=True, slots=True)
class EventRequest:
    id: int
    event: str
    root: str
    cwd: str
    env: dict[str, str]
    payload_raw: str
    client_pid: int
    client_ppid: int
    deadline_unix_ms: int
    received: float = field(default_factory=time.perf_counter, compare=False)

    def deadline_passed(self) -> bool:
        return 0 < self.deadline_unix_ms <= time.time() * 1000


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


def read_message(stream: BinaryIO, *, max_frame: int = MAX_FRAME) -> dict[str, Any] | None:
    first = stream.read(1)
    if first == b"":
        return None
    try:
        header = first + _read_exact(stream, 3)
    except ProtocolError as exc:
        raise ProtocolError("truncated frame header") from exc
    length = struct.unpack(">I", header)[0]
    if length == 0 or length > max_frame:
        raise ProtocolError(f"invalid frame length: {length}")
    payload = _read_exact(stream, length)
    try:
        message: dict[str, Any] = parse_exact(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"malformed frame JSON: {exc}") from exc
    if type(message) is not dict:
        raise ProtocolError(f"invalid message shape: {message!r}")
    if message.get("op") in {OP_SNAPSHOT_REQUEST, OP_SNAPSHOT_RESULT, OP_SNAPSHOT_CANCEL, OP_ERROR}:
        if length > MAX_SNAPSHOT_FRAME:
            raise ProtocolError("snapshot frame exceeds frame bound")
        return message
    return native_numbers(message)


def write_message(stream: BinaryIO, message: dict[str, object], *, max_frame: int = MAX_FRAME) -> None:
    payload = json.dumps(message, sort_keys=True, separators=(",", ":")).encode()
    if len(payload) > max_frame:
        raise ProtocolError(f"frame exceeds {max_frame} bytes")
    _write_all(stream, struct.pack(">I", len(payload)))
    _write_all(stream, payload)
    stream.flush()


def decode_hello(message: dict[str, Any]) -> Hello:
    if (
        set(message) != HELLO_KEYS
        or type(message["protocol"]) is not int
        or message["protocol"] != PROTOCOL
        or message["op"] != OP_HELLO
        or type(message["build"]) is not str
        or message["build"] == ""
    ):
        raise ProtocolError(f"invalid hello shape: {message!r}")
    return Hello(build=message["build"])


def decode_event(message: dict[str, Any]) -> EventRequest:
    if (
        set(message) != EVENT_FRAME_KEYS
        or type(message["protocol"]) is not int
        or message["protocol"] != PROTOCOL
        or message["op"] != OP_EVENT
        or type(message["id"]) is not int
        or message["id"] <= 0
        or type(message["request"]) is not dict
    ):
        raise ProtocolError(f"invalid event frame: {message!r}")
    request = cast(dict[str, object], message["request"])
    env = request.get("env")
    if (
        set(request) != EVENT_REQUEST_KEYS
        or type(request["schema"]) is not int
        or request["schema"] != PROTOCOL
        or type(request["event"]) is not str
        or request["event"] == ""
        or type(request["root"]) is not str
        or request["root"] == ""
        or type(request["cwd"]) is not str
        or request["cwd"] == ""
        or type(env) is not dict
        or not all(type(key) is str and type(value) is str for key, value in cast(dict[object, object], env).items())
        or type(request["payload_raw"]) is not str
        or len(request["payload_raw"].encode()) > MAX_EVENT_INPUT
        or type(request["client_pid"]) is not int
        or request["client_pid"] <= 1
        or type(request["client_ppid"]) is not int
        or request["client_ppid"] <= 0
        or type(request["deadline_unix_ms"]) is not int
        or request["deadline_unix_ms"] < 0
    ):
        raise ProtocolError(f"invalid event request: {request!r}")
    return EventRequest(
        id=message["id"],
        event=request["event"],
        root=request["root"],
        cwd=request["cwd"],
        env=cast(dict[str, str], env),
        payload_raw=request["payload_raw"],
        client_pid=request["client_pid"],
        client_ppid=request["client_ppid"],
        deadline_unix_ms=request["deadline_unix_ms"],
    )


def hello_response(build: str) -> dict[str, object]:
    return {"protocol": PROTOCOL, "op": OP_HELLO, "build": build}


def result_response(request_id: int, response: EventResponse) -> dict[str, object]:
    return {"protocol": PROTOCOL, "op": OP_RESULT, "id": request_id, "response": response.message()}


def error_response(request_id: int, error: str) -> dict[str, object]:
    return {"protocol": PROTOCOL, "op": OP_ERROR, "id": request_id, "error": error}


def adopt_message(pid: int, lifetime_ms: int) -> dict[str, object]:
    return {"protocol": PROTOCOL, "op": OP_ADOPT, "adopt": {"pid": pid, "lifetime_ms": lifetime_ms}}


@dataclass(frozen=True, slots=True)
class SnapshotReply:
    id: int
    parent_id: int
    snapshot: dict[str, Any] | None
    error: str | None


def decode_snapshot_reply(message: dict[str, Any]) -> SnapshotReply:
    op = message.get("op")
    keys = {"protocol", "op", "id", "snapshot" if op == OP_SNAPSHOT_RESULT else "error"}
    if "parent_id" in message:
        keys.add("parent_id")
    parent_id = message.get("parent_id", 0)
    if (
        set(message) != keys
        or type(message.get("protocol")) is not int
        or message["protocol"] != PROTOCOL
        or op not in {OP_SNAPSHOT_RESULT, OP_ERROR}
        or type(message.get("id")) is not int
        or message["id"] <= 0
        or type(parent_id) is not int
        or parent_id < 0
        or (op == OP_SNAPSHOT_RESULT and type(message.get("snapshot")) is not dict)
        or (op == OP_ERROR and type(message.get("error")) is not str)
    ):
        raise ProtocolError("invalid snapshot reply")
    snapshot = message.get("snapshot")
    if snapshot is not None:
        from jsonschema import ValidationError

        try:
            snapshot = checked("host-response", snapshot)
        except (ValueError, ValidationError) as exc:
            raise ProtocolError(f"invalid snapshot response: {exc}") from exc
        message = message | {"snapshot": snapshot}
    if len(json.dumps(message, separators=(",", ":")).encode()) > MAX_SNAPSHOT_FRAME:
        raise ProtocolError("snapshot reply exceeds frame bound")
    return SnapshotReply(message["id"], parent_id, snapshot, message.get("error"))


def snapshot_request_message(request_id: int, parent_id: int, snapshot: dict[str, object]) -> dict[str, object]:
    return {
        "protocol": PROTOCOL,
        "op": OP_SNAPSHOT_REQUEST,
        "id": request_id,
        **({"parent_id": parent_id} if parent_id else {}),
        "snapshot": snapshot,
    }


def snapshot_cancel_message(request_id: int, parent_id: int) -> dict[str, object]:
    return {
        "protocol": PROTOCOL,
        "op": OP_SNAPSHOT_CANCEL,
        "id": request_id,
        **({"parent_id": parent_id} if parent_id else {}),
    }


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
