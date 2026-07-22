from __future__ import annotations

import importlib.metadata
import io
import json
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Buffer

import pytest

from captain_hook.worker.protocol import (
    MAX_FRAME,
    EventRequest,
    EventResponse,
    ProtocolError,
    decode_event,
    decode_hello,
    read_message,
    write_message,
)
from captain_hook.worker.service import WorkerService


def frame(message: dict[str, object]) -> bytes:
    payload = json.dumps(message, sort_keys=True, separators=(",", ":")).encode()
    return struct.pack(">I", len(payload)) + payload


def hello(build: str = "12.9.1") -> dict[str, object]:
    return {"protocol": 1, "op": "hello", "build": build}


def event(request_id: int, *, build: str = "12.9.1", payload_raw: str | None = None) -> dict[str, object]:
    return {
        "protocol": 1,
        "op": "event",
        "id": request_id,
        "request": {
            "schema": 1,
            "event": "PreToolUse",
            "async": False,
            "root": "/project",
            "cwd": "/project/subdir",
            "env": {"CLAUDE_PROJECT_DIR": "/project"},
            "payload_raw": payload_raw or '{"session_id":"session-1"}',
            "python": "/usr/bin/python3",
            "build": build,
            "client_pid": 100,
            "client_ppid": 99,
        },
    }


def responses(raw: bytes) -> list[dict[str, object]]:
    stream = io.BytesIO(raw)
    found: list[dict[str, object]] = []
    while (message := read_message(stream)) is not None:
        found.append(message)
    return found


def test_hello_frame_is_exact_golden() -> None:
    output = io.BytesIO()
    write_message(output, {"protocol": 1, "op": "hello", "build": "12.9.1"})
    payload = b'{"build":"12.9.1","op":"hello","protocol":1}'
    assert output.getvalue() == struct.pack(">I", len(payload)) + payload


def test_event_frame_decodes_exact_go_envelope() -> None:
    request = decode_event(event(7))
    assert request == EventRequest(
        id=7,
        event="PreToolUse",
        async_=False,
        root="/project",
        cwd="/project/subdir",
        env={"CLAUDE_PROJECT_DIR": "/project"},
        payload_raw='{"session_id":"session-1"}',
        python="/usr/bin/python3",
        build="12.9.1",
        client_pid=100,
        client_ppid=99,
    )


def test_read_message_reassembles_short_reads() -> None:
    class ShortReader(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            return super().read(min(size, 3) if size is not None and size >= 0 else 3)

    message = event(1)
    assert read_message(ShortReader(frame(message))) == message


def test_write_message_completes_short_writes() -> None:
    class ShortWriter(io.BytesIO):
        def write(self, data: Buffer, /) -> int:
            return super().write(bytes(memoryview(data)[:3]))

    output = ShortWriter()
    write_message(output, hello())
    assert output.getvalue() == frame(hello())


@pytest.mark.parametrize(
    "raw,match",
    [
        (b"\x00\x00", "truncated frame header"),
        (struct.pack(">I", 0), "invalid frame length"),
        (struct.pack(">I", 4) + b"{}", "truncated frame payload"),
        (struct.pack(">I", MAX_FRAME + 1), "invalid frame length"),
        (frame({"not": "an event"}), "invalid event frame"),
    ],
)
def test_protocol_rejects_non_exact_frames(raw: bytes, match: str) -> None:
    if match == "invalid event frame":
        message = read_message(io.BytesIO(raw))
        assert message is not None
        with pytest.raises(ProtocolError, match=match):
            decode_event(message)
    else:
        with pytest.raises(ProtocolError, match=match):
            read_message(io.BytesIO(raw))


def test_decoders_reject_extra_fields_and_wrong_schema() -> None:
    with pytest.raises(ProtocolError, match="invalid hello shape"):
        decode_hello(hello() | {"extra": True})
    wrong = event(1)
    nested = wrong["request"]
    assert isinstance(nested, dict)
    nested["schema"] = 2
    with pytest.raises(ProtocolError, match="invalid event request"):
        decode_event(wrong)


def test_service_handshake_nested_result_and_graceful_eof() -> None:
    input_stream = io.BytesIO(frame(hello()) + frame(event(1)))
    output_stream = io.BytesIO()

    def dispatch(_: EventRequest) -> EventResponse:
        return EventResponse(stdout="ok\n")

    WorkerService(input_stream, output_stream, build="12.9.1", dispatch=dispatch).run()

    received = responses(output_stream.getvalue())
    assert received[0] == {"protocol": 1, "op": "hello", "build": "12.9.1"}
    assert received[1]["protocol"] == 1
    assert received[1]["op"] == "result"
    assert received[1]["id"] == 1
    nested = received[1]["response"]
    assert isinstance(nested, dict)
    assert nested["schema"] == 1
    assert nested["status"] == "ok"
    assert nested["stdout"] == "ok\n"
    assert isinstance(nested["elapsed_ms"], float)


def test_requests_dispatch_concurrently() -> None:
    input_stream = io.BytesIO(frame(hello()) + frame(event(1)) + frame(event(2)))
    output_stream = io.BytesIO()
    barrier = threading.Barrier(2)
    guard = threading.Lock()
    active = 0
    peak = 0

    def dispatch(_: EventRequest) -> EventResponse:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=2)
        with guard:
            active -= 1
        return EventResponse()

    WorkerService(input_stream, output_stream, build="12.9.1", dispatch=dispatch, max_workers=2).run()

    assert peak == 2
    assert {message["id"] for message in responses(output_stream.getvalue())[1:]} == {1, 2}


def test_dispatch_failure_is_top_level_error_with_same_id() -> None:
    input_stream = io.BytesIO(frame(hello()) + frame(event(7)))
    output_stream = io.BytesIO()

    def dispatch(_: EventRequest) -> EventResponse:
        raise RuntimeError("boom")

    WorkerService(input_stream, output_stream, build="12.9.1", dispatch=dispatch).run()
    response = responses(output_stream.getvalue())[1]
    assert response["op"] == "error"
    assert response["id"] == 7
    assert isinstance(response["error"], str)
    assert "RuntimeError: boom" in response["error"]
    assert "response" not in response


def test_build_mismatch_fails_before_event_admission() -> None:
    output_stream = io.BytesIO()
    with pytest.raises(ProtocolError, match="does not match host build"):
        WorkerService(
            io.BytesIO(frame(hello("old"))),
            output_stream,
            build="12.9.1",
            dispatch=lambda _: EventResponse(),
        ).run()
    assert output_stream.getvalue() == b""


def test_protocol_failure_drains_already_accepted_work() -> None:
    input_stream = io.BytesIO(frame(hello()) + frame(event(1)) + frame({"bad": True}))
    output_stream = io.BytesIO()
    served: list[int] = []

    def dispatch(request: EventRequest) -> EventResponse:
        time.sleep(0.01)
        served.append(request.id)
        return EventResponse()

    with pytest.raises(ProtocolError, match="invalid event frame"):
        WorkerService(input_stream, output_stream, build="12.9.1", dispatch=dispatch).run()

    assert served == [1]
    assert responses(output_stream.getvalue())[1]["id"] == 1


def test_module_entrypoint_reserves_stdout_for_protocol() -> None:
    build = importlib.metadata.version("capt-hook")
    completed = subprocess.run(
        [sys.executable, "-m", "captain_hook.worker"],
        input=frame(hello(build)),
        capture_output=True,
        check=True,
        timeout=5,
    )

    response = responses(completed.stdout)
    assert response == [{"protocol": 1, "op": "hello", "build": build}]
