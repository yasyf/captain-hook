from __future__ import annotations

import io
import json
import os
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from captain_hook.worker.protocol import (
    EVENT_FRAME_KEYS,
    EVENT_REQUEST_KEYS,
    HELLO_KEYS,
    MAX_EVENT_ENVELOPE,
    MAX_EVENT_INPUT,
    MAX_FRAME,
    MAX_HOST_PAYLOAD,
    OP_ERROR,
    OP_EVENT,
    OP_HELLO,
    OP_RESULT,
    PROTOCOL,
    EventRequest,
    EventResponse,
    ProtocolError,
    decode_event,
    decode_hello,
    error_response,
    hello_response,
    read_message,
    result_response,
    write_message,
)

if TYPE_CHECKING:
    pass

ROOT = Path(__file__).parents[1]
GO = shutil.which("go")
PACKAGE = "./internal/hookd"


def python_descriptor() -> dict[str, object]:
    return {
        "protocol": PROTOCOL,
        "limits": {
            "event_input": MAX_EVENT_INPUT,
            "event_envelope": MAX_EVENT_ENVELOPE,
            "host_payload": MAX_HOST_PAYLOAD,
            "worker_frame": MAX_FRAME,
        },
        "ops": {"hello": OP_HELLO, "event": OP_EVENT, "result": OP_RESULT, "error": OP_ERROR},
        "fields": {
            "worker_frame": sorted(
                HELLO_KEYS
                | EVENT_FRAME_KEYS
                | set(result_response(1, EventResponse()))
                | set(error_response(1, "boom"))
            ),
            "event_request": sorted(EVENT_REQUEST_KEYS),
            "event_response": sorted(EventResponse().message()),
        },
    }


def drift(go: Any, python: Any, path: str = "") -> dict[str, tuple[Any, Any]]:
    match (go, python):
        case (dict(), dict()):
            return {
                key: pair
                for name in go.keys() | python.keys()
                for key, pair in drift(go.get(name), python.get(name), f"{path}.{name}" if path else name).items()
            }
        case (list(), list()) if go != python:
            return {path: (sorted(set(go) - set(python)), sorted(set(python) - set(go)))}
        case _:
            return {} if go == python else {path: (go, python)}


def event_frame(request: EventRequest) -> dict[str, object]:
    return {
        "protocol": PROTOCOL,
        "op": OP_EVENT,
        "id": request.id,
        "request": {
            "schema": PROTOCOL,
            "event": request.event,
            "async": request.async_,
            "root": request.root,
            "cwd": request.cwd,
            "env": request.env,
            "payload_raw": request.payload_raw,
            "python": request.python,
            "build": request.build,
            "client_pid": request.client_pid,
            "client_ppid": request.client_ppid,
        },
    }


def judge(payload: bytes) -> tuple[str, dict[str, Any] | None]:
    try:
        message = read_message(io.BytesIO(payload))
        assert message is not None
        decode_event(message) if message.get("op") == OP_EVENT else decode_hello(message)
    except ProtocolError:
        return "reject", None
    return "accept", message


def reencode(message: dict[str, Any]) -> bytes:
    output = io.BytesIO()
    if message["op"] == OP_EVENT:
        write_message(output, event_frame(decode_event(message)))
    else:
        write_message(output, hello_response(decode_hello(message).build))
    return output.getvalue()


def framed(message: dict[str, object]) -> bytes:
    output = io.BytesIO()
    write_message(output, message)
    return output.getvalue()


def result_message(**overrides: object) -> dict[str, object]:
    message = result_response(1, EventResponse(status="ok", stdout="out", stderr="err", exit=0, elapsed_ms=12.5))
    response = message["response"]
    assert isinstance(response, dict)
    return message | {"response": response | overrides}


def python_corpus() -> dict[str, tuple[str, bytes]]:
    return {
        "hello_response": ("accept", framed(hello_response("12.9.1"))),
        "result_response": ("accept", framed(result_message())),
        "error_response": ("accept", framed(error_response(2, "RuntimeError: boom"))),
        "result_bad_status": ("reject", framed(result_message(status="bogus"))),
        "result_wrong_schema": ("reject", framed(result_message(schema=PROTOCOL + 1))),
        "result_unknown_response_field": ("reject", framed(result_message(legacy=True))),
        "frame_unknown_field": ("reject", framed(hello_response("12.9.1") | {"legacy": True})),
        "frame_over_cap": ("reject", struct.pack(">I", MAX_FRAME + 1) + b"{}"),
    }


def go_test(root: Path, name: str) -> None:
    assert GO is not None
    completed = subprocess.run(
        [GO, "test", "-count=1", "-run", f"^{name}$", "-v", PACKAGE],
        cwd=ROOT,
        env=os.environ | {"CAPT_HOOK_CONFORMANCE_DIR": str(root)},
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    if completed.returncode != 0 or f"--- PASS: {name}" not in completed.stdout:
        pytest.fail(f"{name} did not pass:\n{completed.stdout}\n{completed.stderr}")


@dataclass(frozen=True, slots=True)
class Emitted:
    root: Path
    go: dict[str, Any]
    manifest: dict[str, str]


@dataclass(frozen=True, slots=True)
class Judged:
    emitted: Emitted
    verdicts: dict[str, str]


@pytest.fixture(scope="module")
def emitted(tmp_path_factory: pytest.TempPathFactory) -> Emitted:
    if GO is None:
        pytest.skip("the Go toolchain is required to extract the host half of the protocol")
    root = tmp_path_factory.mktemp("conformance")
    go_test(root, "TestConformanceEmit")
    return Emitted(
        root=root,
        go=json.loads((root / "go.json").read_text()),
        manifest={
            entry["name"]: entry["verdict"]
            for entry in json.loads((root / "go_to_python" / "manifest.json").read_text())
        },
    )


@pytest.fixture(scope="module")
def judged(emitted: Emitted) -> Judged:
    back = emitted.root / "go_to_python_back"
    back.mkdir()
    verdicts: dict[str, str] = {}
    for name in emitted.manifest:
        verdict, message = judge((emitted.root / "go_to_python" / f"{name}.bin").read_bytes())
        verdicts[name] = verdict
        if message is not None:
            (back / f"{name}.bin").write_bytes(reencode(message))
    return Judged(emitted=emitted, verdicts=verdicts)


@pytest.fixture(scope="module")
def returned(judged: Judged) -> dict[str, dict[str, Any]]:
    if judged.verdicts != judged.emitted.manifest:
        pytest.skip("the two halves disagree on the corpus; no round trip is meaningful until that is fixed")
    root = judged.emitted.root
    inbound = root / "python_to_go"
    inbound.mkdir()
    corpus = python_corpus()
    (inbound / "manifest.json").write_text(
        json.dumps([{"name": name, "verdict": verdict} for name, (verdict, _) in corpus.items()])
    )
    for name, (_, payload) in corpus.items():
        (inbound / f"{name}.bin").write_bytes(payload)

    go_test(root, "TestConformanceVerify")

    return {
        name: message
        for name, (verdict, _) in corpus.items()
        if verdict == "accept"
        and (message := read_message(io.BytesIO((root / "python_to_go_back" / f"{name}.bin").read_bytes()))) is not None
    }


def test_go_and_python_declare_the_same_wire_contract(emitted: Emitted) -> None:
    assert drift(emitted.go, python_descriptor()) == {}


def test_python_judges_the_go_corpus_exactly_as_go_does(judged: Judged) -> None:
    assert judged.verdicts == judged.emitted.manifest


def test_go_frames_survive_a_python_round_trip(returned: dict[str, dict[str, Any]], judged: Judged) -> None:
    accepted = {name for name, verdict in judged.emitted.manifest.items() if verdict == "accept"}
    assert accepted == {"hello", "event_minimal", "event_at_max_size"}
    assert {path.stem for path in (judged.emitted.root / "go_to_python_back").iterdir()} == accepted


def test_python_frames_survive_a_go_round_trip(returned: dict[str, dict[str, Any]]) -> None:
    corpus = python_corpus()
    assert set(returned) == {name for name, (verdict, _) in corpus.items() if verdict == "accept"}
    for name, message in returned.items():
        assert drift(message, json.loads(corpus[name][1][4:])) == {}, name


def frame_of_exactly(size: int) -> bytes:
    envelope = len(json.dumps(hello_response(""), sort_keys=True, separators=(",", ":")).encode())
    payload = json.dumps(hello_response("x" * (size - envelope)), sort_keys=True, separators=(",", ":")).encode()
    assert len(payload) == size
    return struct.pack(">I", size) + payload


def test_python_admits_exactly_the_frames_go_will_encode(emitted: Emitted) -> None:
    ceiling = emitted.go["limits"]["worker_frame"]
    message = read_message(io.BytesIO(frame_of_exactly(ceiling)))
    assert message is not None
    assert len(json.dumps(message, sort_keys=True, separators=(",", ":")).encode()) == ceiling
    with pytest.raises(ProtocolError, match="invalid frame length"):
        read_message(io.BytesIO(frame_of_exactly(ceiling + 1)))
    with pytest.raises(ProtocolError, match=f"frame exceeds {ceiling} bytes"):
        write_message(io.BytesIO(), hello_response("x" * ceiling))


def test_frame_ceiling_admits_every_event_the_host_can_produce() -> None:
    payload = framed(
        event_frame(
            EventRequest(
                id=1,
                event="PreToolUse",
                async_=False,
                root="/project",
                cwd="/project/subdir",
                env={"CLAUDE_PROJECT_DIR": "/project"},
                payload_raw='"' * MAX_EVENT_INPUT,
                python="/usr/bin/python3",
                build="12.9.1",
                client_pid=100,
                client_ppid=99,
            )
        )
    )
    assert len(payload) - 4 > MAX_EVENT_INPUT * 2
    message = read_message(io.BytesIO(payload))
    assert message is not None
    assert decode_event(message).payload_raw == '"' * MAX_EVENT_INPUT


def test_python_ci_job_can_reach_the_go_half() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()
    job = re.split(r"\n  \w[\w-]*:\n", workflow.split("\n  test:\n", 1)[1], maxsplit=1)[0]
    assert "actions/setup-go@v6" in job
