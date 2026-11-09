from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from captain_hook import orca_socket
from captain_hook.builtin_packs.general.hooks._sessions import TimedOut, orca_json, worker_of
from captain_hook.testing.helpers import stubbed_commands

RUNTIME = "runtime-1"
TOKEN = "token-1"
STATUS = {
    "runtimeProtocolVersion": 3,
    "minCompatibleRuntimeClientVersion": 2,
    "capabilities": ["orchestration.contract.v1"],
}
Answer = Callable[[dict[str, Any]], dict[str, Any] | None]


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[Answer], list[dict[str, Any]]]]:
    orca_socket.ADMITTED.clear()
    directory = Path(tempfile.mkdtemp(prefix="orca"))
    endpoint = directory / "o.sock"
    (directory / orca_socket.RUNTIME_FILE).write_text(
        json.dumps(
            {
                "runtimeId": RUNTIME,
                "authToken": TOKEN,
                "transports": [{"kind": "unix", "endpoint": str(endpoint)}],
            }
        )
    )
    monkeypatch.setenv("ORCA_USER_DATA_PATH", str(directory))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(endpoint))
    server.listen()

    def serve(answer: Answer) -> list[dict[str, Any]]:
        seen: list[dict[str, Any]] = []

        def loop() -> None:
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:
                    return
                with conn, conn.makefile("rb") as frames:
                    request = json.loads(frames.readline())
                    seen.append(request)
                    if request["method"] == "status.get":
                        reply = {"ok": True, "result": STATUS}
                    elif (reply := answer(request)) is None:
                        threading.Event().wait(0.5)
                        continue
                    conn.sendall(json.dumps(reply | {"id": request["id"]}).encode() + b"\n")

        threading.Thread(target=loop, daemon=True).start()
        return seen

    yield serve
    server.close()
    orca_socket.ADMITTED.clear()


CHECK = ("orca", "orchestration", "check", "--terminal", "term_a", "--peek", "--json")


def test_a_read_answers_over_the_socket_with_the_full_record(runtime: Callable[[Answer], list[dict[str, Any]]]) -> None:
    seen = runtime(lambda _: {"ok": True, "result": {"dispatchId": "ctx_1", "messages": []}})

    assert orca_json(CHECK, "result", "dispatchId") == "ctx_1"

    method = seen[-1]
    assert method["method"] == "orchestration.check"
    assert method["authToken"] == TOKEN
    assert method["orchestrationContractVersion"] == 1
    assert method["params"] == {
        "terminal": "term_a",
        "peek": True,
        "unread": False,
        "compatibilityCliCommand": "orca",
    }


def test_a_keepalive_frame_is_skipped() -> None:
    endpoint = Path(tempfile.mkdtemp(prefix="orca")) / "o.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(endpoint))
    server.listen()

    def serve() -> None:
        conn, _ = server.accept()
        with conn, conn.makefile("rb") as frames:
            request = json.loads(frames.readline())
            conn.sendall(b'{"_keepalive": true}\n')
            conn.sendall(json.dumps({"id": request["id"], "ok": True, "result": {"found": 1}}).encode() + b"\n")

    threading.Thread(target=serve, daemon=True).start()
    try:
        assert orca_socket.exchange(str(endpoint), TOKEN, "terminal.show", {}, time.monotonic() + 5) == {"found": 1}
    finally:
        server.close()


def test_a_slow_runtime_reads_as_timed_out_without_the_cli(
    runtime: Callable[[Answer], list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime(lambda _: None)
    monkeypatch.setattr(
        "captain_hook.builtin_packs.general.hooks._sessions.probe_timeout",
        lambda *_: 0.05,
    )
    orca_socket.ADMITTED[RUNTIME] = True

    assert isinstance(orca_json(CHECK, "result", "dispatchId"), TimedOut)


@pytest.mark.parametrize(
    "status",
    [
        pytest.param({**STATUS, "capabilities": []}, id="no-contract"),
        pytest.param({**STATUS, "minCompatibleRuntimeClientVersion": 4}, id="client-too-old"),
        pytest.param({**STATUS, "runtimeProtocolVersion": 2}, id="runtime-too-old"),
    ],
)
def test_a_runtime_on_another_protocol_is_not_admitted(status: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    orca_socket.ADMITTED.clear()
    monkeypatch.setattr(orca_socket, "exchange", lambda *_: status)

    assert orca_socket.admitted(RUNTIME, "/nowhere", TOKEN, 0.0) is False
    orca_socket.ADMITTED.clear()


def test_a_paired_runtime_goes_through_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCA_PAIRING_CODE", "code")

    with pytest.raises(orca_socket.OrcaUnavailable):
        orca_socket.call("terminal.show", {}, timeout=1)


def test_no_runtime_file_goes_through_the_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCA_USER_DATA_PATH", str(tmp_path))

    with pytest.raises(orca_socket.OrcaUnavailable):
        orca_socket.call("terminal.show", {}, timeout=1)


def test_the_cli_fallback_asks_for_full_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCA_USER_DATA_PATH", str(tmp_path))
    full = {" ".join(CHECK) + " --full": json.dumps({"result": {"dispatchId": "ctx_1"}}), " ".join(CHECK): "{}"}

    with stubbed_commands(full):
        assert orca_json(CHECK, "result", "dispatchId") == "ctx_1"


def test_a_worker_row_is_found_over_the_socket(runtime: Callable[[Answer], list[dict[str, Any]]]) -> None:
    row = {"dispatchId": "ctx_1", "runId": "run_1", "dispatchStatus": "dispatched", "agentTerminalHandle": "term_a"}
    seen = runtime(lambda _: {"ok": True, "result": {"workers": [row], "page": {}}})

    assert worker_of("term_a") == row
    assert seen[-1]["method"] == "orchestration.workerList"
    assert seen[-1]["params"]["paginate"] is True


@pytest.mark.parametrize(
    ("argv", "method"),
    [
        (("orca", "orchestration", "worker-show", "--dispatch", "d", "--json"), "orchestration.workerShow"),
        (("orca", "orchestration", "run-show", "--id", "r", "--json"), "orchestration.runShow"),
        (("orca", "orchestration", "worker-list", "--limit", "5", "--json"), "orchestration.workerList"),
        (
            ("orca", "orchestration", "worker-list", "--limit", "5", "--cursor", "c", "--json"),
            "orchestration.workerList",
        ),
        (("orca", "terminal", "show", "--terminal", "t", "--json"), "terminal.show"),
    ],
)
def test_read_only_spellings_map_to_socket_methods(argv: tuple[str, ...], method: str) -> None:
    read = orca_socket.read_call(argv)
    assert read is not None
    assert read[0] == method


def test_other_spellings_stay_on_the_cli() -> None:
    assert orca_socket.read_call(("orca", "terminal", "close", "--terminal", "t", "--json")) is None
    assert orca_socket.read_call(("orca", "terminal", "read", "--terminal", "t", "--screen", "--json")) is None
