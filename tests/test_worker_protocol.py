from __future__ import annotations

import importlib.metadata
import io
import json
import os
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Buffer, Callable

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
from captain_hook.worker.service import WorkerService, handshake


def frame(message: dict[str, object]) -> bytes:
    payload = json.dumps(message, sort_keys=True, separators=(",", ":")).encode()
    return struct.pack(">I", len(payload)) + payload


def hello(build: str = "12.9.1") -> dict[str, object]:
    return {"protocol": 1, "op": "hello", "build": build}


def event(request_id: int, *, payload_raw: str | None = None, deadline_unix_ms: int = 0) -> dict[str, object]:
    return {
        "protocol": 1,
        "op": "event",
        "id": request_id,
        "request": {
            "schema": 1,
            "event": "PreToolUse",
            "root": "/project",
            "cwd": "/project/subdir",
            "env": {"CLAUDE_PROJECT_DIR": "/project"},
            "payload_raw": payload_raw or '{"session_id":"session-1"}',
            "client_pid": 100,
            "client_ppid": 99,
            "deadline_unix_ms": deadline_unix_ms,
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
        root="/project",
        cwd="/project/subdir",
        env={"CLAUDE_PROJECT_DIR": "/project"},
        payload_raw='{"session_id":"session-1"}',
        client_pid=100,
        client_ppid=99,
        deadline_unix_ms=0,
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


def served(
    response: EventResponse, background: Callable[[], None] | None = None
) -> tuple[EventResponse, Callable[[], None] | None]:
    return response, background


def test_handshake_answers_hello_and_graceful_eof() -> None:
    output_stream = io.BytesIO()
    assert handshake(io.BytesIO(frame(hello())), output_stream, build="12.9.1")
    assert responses(output_stream.getvalue()) == [{"protocol": 1, "op": "hello", "build": "12.9.1"}]
    assert not handshake(io.BytesIO(b""), io.BytesIO(), build="12.9.1")


def test_service_nested_result_and_graceful_eof() -> None:
    output_stream = io.BytesIO()

    WorkerService(
        io.BytesIO(frame(event(1))), output_stream, dispatch=lambda _: served(EventResponse(stdout="ok\n"))
    ).run()

    (received,) = responses(output_stream.getvalue())
    assert received["protocol"] == 1
    assert received["op"] == "result"
    assert received["id"] == 1
    nested = received["response"]
    assert isinstance(nested, dict)
    assert nested["schema"] == 1
    assert nested["status"] == "ok"
    assert nested["stdout"] == "ok\n"
    assert isinstance(nested["elapsed_ms"], float)


def test_service_announces_an_adopted_process_in_one_exact_frame() -> None:
    output_stream = io.BytesIO()
    service = WorkerService(io.BytesIO(), output_stream, dispatch=lambda _: served(EventResponse()))

    service.adopt(4242, 7_500_000)
    service.run()

    assert responses(output_stream.getvalue()) == [
        {"protocol": 1, "op": "adopt", "adopt": {"pid": 4242, "lifetime_ms": 7_500_000}}
    ]


def test_background_work_runs_after_the_reply_is_written() -> None:
    class RecordingOutput(io.BytesIO):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[str] = []

        def write(self, data: Buffer, /) -> int:
            self.events.append("reply")
            return super().write(data)

    output_stream = RecordingOutput()
    ran = threading.Event()

    def background() -> None:
        output_stream.events.append("background")
        ran.set()

    WorkerService(
        io.BytesIO(frame(event(1))), output_stream, dispatch=lambda _: served(EventResponse(), background)
    ).run()

    assert ran.is_set()
    index = output_stream.events.index("background")
    assert "reply" in output_stream.events[:index]
    assert "reply" in output_stream.events[index + 1 :]
    assert [message["op"] for message in responses(output_stream.getvalue())] == [
        "background_begin", "result", "background_end",
    ]


def test_slow_background_work_does_not_hold_the_reply() -> None:
    release = threading.Event()
    replied = threading.Event()

    class Output(io.BytesIO):
        def flush(self) -> None:
            if any(message["op"] == "result" for message in responses(self.getvalue())):
                replied.set()

    output_stream = Output()

    def background() -> None:
        assert replied.wait(timeout=5)
        release.wait(timeout=5)

    service = WorkerService(
        io.BytesIO(frame(event(1))), output_stream, dispatch=lambda _: served(EventResponse(), background)
    )
    runner = threading.Thread(target=service.run)
    runner.start()
    assert replied.wait(timeout=5)
    assert [(message["op"], message["id"]) for message in responses(output_stream.getvalue())] == [
        ("background_begin", 1), ("result", 1),
    ]
    release.set()
    runner.join(timeout=5)
    assert not runner.is_alive()


def test_requests_dispatch_concurrently() -> None:
    input_stream = io.BytesIO(frame(event(1)) + frame(event(2)))
    output_stream = io.BytesIO()
    barrier = threading.Barrier(2)
    guard = threading.Lock()
    active = 0
    peak = 0

    def dispatch(_: EventRequest) -> tuple[EventResponse, None]:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=2)
        with guard:
            active -= 1
        return EventResponse(), None

    WorkerService(input_stream, output_stream, dispatch=dispatch, max_workers=2).run()

    assert peak == 2
    assert {message["id"] for message in responses(output_stream.getvalue())} == {1, 2}


def test_dispatch_failure_is_top_level_error_with_same_id() -> None:
    output_stream = io.BytesIO()

    def dispatch(_: EventRequest) -> tuple[EventResponse, None]:
        raise RuntimeError("boom")

    WorkerService(io.BytesIO(frame(event(7))), output_stream, dispatch=dispatch).run()
    (response,) = responses(output_stream.getvalue())
    assert response["op"] == "error"
    assert response["id"] == 7
    assert isinstance(response["error"], str)
    assert "RuntimeError: boom" in response["error"]
    assert "response" not in response


def test_expired_deadline_is_refused_without_dispatch() -> None:
    input_stream = io.BytesIO(frame(event(3, deadline_unix_ms=1)) + frame(event(4)))
    output_stream = io.BytesIO()
    served_ids: list[int] = []

    def dispatch(request: EventRequest) -> tuple[EventResponse, None]:
        served_ids.append(request.id)
        return EventResponse(), None

    WorkerService(input_stream, output_stream, dispatch=dispatch).run()

    assert served_ids == [4]
    by_id = {message["id"]: message for message in responses(output_stream.getvalue())}
    assert by_id[3]["op"] == "error"
    assert by_id[3]["error"] == "deadline passed before dispatch"
    assert by_id[4]["op"] == "result"


def test_build_mismatch_fails_the_handshake() -> None:
    output_stream = io.BytesIO()
    with pytest.raises(ProtocolError, match="does not match host build"):
        handshake(io.BytesIO(frame(hello("old"))), output_stream, build="12.9.1")
    assert output_stream.getvalue() == b""


def test_protocol_failure_drains_already_accepted_work() -> None:
    input_stream = io.BytesIO(frame(event(1)) + frame({"bad": True}))
    output_stream = io.BytesIO()
    served_ids: list[int] = []

    def dispatch(request: EventRequest) -> tuple[EventResponse, None]:
        time.sleep(0.01)
        served_ids.append(request.id)
        return EventResponse(), None

    with pytest.raises(ProtocolError, match="invalid event frame"):
        WorkerService(input_stream, output_stream, dispatch=dispatch).run()

    assert served_ids == [1]
    assert responses(output_stream.getvalue())[0]["id"] == 1


def test_module_entrypoint_reserves_stdout_for_protocol() -> None:
    build = importlib.metadata.version("capt-hook")
    completed = subprocess.run(
        [sys.executable, "-m", "captain_hook.worker"],
        input=frame(hello(build)),
        capture_output=True,
        check=True,
        timeout=5,
        env={**os.environ, "CAPT_HOOK_WORKER_SHARD": "0"},
    )

    response = responses(completed.stdout)
    assert response == [{"protocol": 1, "op": "hello", "build": build}]


def test_module_entrypoint_imports_the_runtime_only_after_the_handshake() -> None:
    probe = (
        "import runpy, sys\n"
        "runpy.run_module('captain_hook.worker', run_name='__main__')\n"
        "print(sorted(m for m in ('captain_hook.cli', 'captain_hook.worker.runtime', 'loguru') if m in sys.modules))\n"
    )
    completed = subprocess.run([sys.executable, "-c", probe], input=b"", capture_output=True, check=True, timeout=30)

    assert completed.stderr.decode().strip().splitlines()[-1] == "[]"
