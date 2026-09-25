import io
import socket
import threading
import time
from concurrent.futures import Future
from contextvars import copy_context

import pytest

from captain_hook.snapshots.client import CURRENT_CLIENT, EvidenceIncomplete
from captain_hook.snapshots.validation import validator
from captain_hook.snapshots.worker import empty_usage
from captain_hook.worker.protocol import (
    MAX_SNAPSHOT_FRAME,
    EventResponse,
    ProtocolError,
    decode_snapshot_reply,
    read_message,
    write_message,
)
from captain_hook.worker.runtime import _run_detached
from captain_hook.worker.service import WorkerService
from tests.test_worker_protocol import event


def snapshot_reply(frame, *, error=None):
    response = {"protocol": 1, "id": frame["id"]}
    if "parent_id" in frame:
        response["parent_id"] = frame["parent_id"]
    if error is not None:
        return response | {"op": "error", "error": error}
    request = frame["snapshot"]["request"]
    return response | {
        "op": "snapshot_result",
        "snapshot": {
            "schema": "captain.transcript/1",
            "response": {
                "schema": "cc-transcript.snapshot/1",
                "id": request["id"],
                "status": "ok",
                "complete": True,
                "cursor": None,
                "data": {
                    "kind": "stats",
                    "counters": empty_usage(),
                    "gauges": dict.fromkeys(validator("host-response").schema["$defs"]["Core_Gauges"]["properties"], 0),
                },
                "reason": None,
                "usage": empty_usage(),
            },
        },
    }


@pytest.fixture
def transport():
    peers = []
    runners = []

    def start(dispatch):
        worker, host = socket.socketpair()
        worker_input = worker.makefile("rb", buffering=0)
        worker_output = worker.makefile("wb", buffering=0)
        host.settimeout(3)
        host_input = host.makefile("rb", buffering=0)
        host_output = host.makefile("wb", buffering=0)
        service = WorkerService(worker_input, worker_output, dispatch=dispatch)
        completed = Future()

        def run():
            try:
                service.run()
                completed.set_result(None)
            except BaseException as exc:
                completed.set_exception(exc)

        runner = threading.Thread(target=run)
        runner.start()
        peers.append((worker, host, worker_input, worker_output, host_input, host_output))
        runners.append((runner, completed))
        return service, host, host_input, host_output, completed

    yield start
    for (_, host, *_), (_, completed) in zip(peers, runners, strict=True):
        if not completed.done():
            host.shutdown(socket.SHUT_WR)
    for runner, completed in runners:
        runner.join(timeout=3)
        assert not runner.is_alive()
        completed.result()
    for resources in peers:
        for resource in resources:
            resource.close()


def test_reverse_foreground_and_explicit_background_use_separate_admission(transport):
    background_done = threading.Event()
    observed = []

    def dispatch(request):
        observed.append(CURRENT_CLIENT.get().call("stats"))
        context = copy_context()

        def background():
            observed.append(CURRENT_CLIENT.get().call("stats"))
            background_done.set()

        return EventResponse(), lambda: context.run(_run_detached, background)

    service, _, incoming, outgoing, _ = transport(dispatch)
    write_message(outgoing, event(41))
    foreground = read_message(incoming)
    assert foreground["op"] == "snapshot_request"
    assert foreground["parent_id"] == 41
    write_message(outgoing, snapshot_reply(foreground))
    assert read_message(incoming) == {"protocol": 1, "op": "background_begin", "id": 41}
    assert read_message(incoming)["op"] == "result"
    background = read_message(incoming)
    assert background["op"] == "snapshot_request"
    assert background.get("parent_id", 0) == 0
    assert background["id"] > foreground["id"]
    write_message(outgoing, snapshot_reply(background))
    assert background_done.wait(timeout=3)
    assert read_message(incoming) == {"protocol": 1, "op": "background_end", "id": 41}
    assert len(observed) == 2
    assert service._snapshot_pending == {}


def test_eof_fails_reverse_waiter_before_draining_dispatch(transport):
    finished = threading.Event()

    def dispatch(request):
        with pytest.raises(EvidenceIncomplete, match="transport closed before reply"):
            CURRENT_CLIENT.get().call("stats")
        finished.set()
        return EventResponse(), None

    service, host, incoming, outgoing, completed = transport(dispatch)
    write_message(outgoing, event(1))
    assert read_message(incoming)["op"] == "snapshot_request"
    host.shutdown(socket.SHUT_WR)
    assert finished.wait(timeout=3)
    completed.result(timeout=3)
    assert service._snapshot_pending == {}


def test_cancelled_waiter_keeps_slot_until_its_terminal_reply(transport):
    finished = threading.Event()

    def dispatch(request):
        value = {
            "schema": "captain.transcript/1",
            "request": {
                "schema": "cc-transcript.snapshot/1",
                "id": "budget",
                "operation": "stats",
                "deadline_unix_ms": int(time.time() * 1000) + 80,
            },
        }
        with pytest.raises(EvidenceIncomplete, match="deadline elapsed"):
            service.snapshot_exchange(request.id, value)
        finished.set()
        return EventResponse(), None

    service, _, incoming, outgoing, _ = transport(dispatch)
    write_message(outgoing, event(7))
    snapshot = read_message(incoming)
    cancelled = read_message(incoming)
    assert cancelled == {"protocol": 1, "op": "snapshot_cancel", "id": snapshot["id"], "parent_id": 7}
    assert finished.wait(timeout=3)
    assert service._snapshot_pending[snapshot["id"]][1].cancelled()
    write_message(outgoing, snapshot_reply(snapshot, error="cancelled"))
    assert read_message(incoming)["op"] == "result"


def test_snapshot_reply_requires_matching_parent():
    service = WorkerService(io.BytesIO(), io.BytesIO(), dispatch=lambda _: (EventResponse(), None))
    pending = Future()
    service._snapshot_pending[2] = (1, pending)
    with pytest.raises(ProtocolError, match="does not match"):
        service._complete_snapshot({"protocol": 1, "op": "error", "id": 2, "parent_id": 99, "error": "failed"})
    assert not pending.done()
    service._close_snapshots()
    service.run()


@pytest.mark.parametrize(
    "change",
    [
        {"parent_id": True},
        {"parent_id": -1},
        {"id": True},
        {"unexpected": 1},
        {"protocol": True},
    ],
)
def test_snapshot_reply_rejects_non_exact_identity(change):
    with pytest.raises(ProtocolError, match="invalid snapshot reply"):
        decode_snapshot_reply({"protocol": 1, "op": "error", "id": 1, "error": "failed"} | change)


def test_dedicated_snapshot_frame_bound_does_not_change_event_bound():
    import struct

    with pytest.raises(ProtocolError, match="invalid frame length"):
        read_message(io.BytesIO(struct.pack(">I", MAX_SNAPSHOT_FRAME + 1)), max_frame=MAX_SNAPSHOT_FRAME)
    with pytest.raises(ProtocolError, match="frame exceeds"):
        write_message(io.BytesIO(), {"large": "x" * MAX_SNAPSHOT_FRAME}, max_frame=MAX_SNAPSHOT_FRAME)
    output = io.BytesIO()
    write_message(output, {"large": "x" * MAX_SNAPSHOT_FRAME})
    assert read_message(io.BytesIO(output.getvalue()))["large"] == "x" * MAX_SNAPSHOT_FRAME


def test_one_cancelled_reverse_request_does_not_cancel_another(transport):
    service, _, incoming, outgoing, _ = transport(lambda _: (EventResponse(), None))
    first_done = Future()
    second_done = Future()

    def request(parent_id, deadline_ms, result):
        try:
            value = service.snapshot_exchange(
                parent_id,
                {
                    "schema": "captain.transcript/1",
                    "request": {
                        "schema": "cc-transcript.snapshot/1",
                        "id": str(parent_id),
                        "operation": "stats",
                        "deadline_unix_ms": int(time.time() * 1000) + deadline_ms,
                    },
                },
            )
            result.set_result(value)
        except BaseException as exc:
            result.set_exception(exc)

    first = threading.Thread(target=request, args=(11, 100, first_done))
    second = threading.Thread(target=request, args=(12, 3000, second_done))
    first.start()
    second.start()
    frames = [read_message(incoming), read_message(incoming)]
    by_parent = {frame["parent_id"]: frame for frame in frames}
    cancel = read_message(incoming)
    assert cancel["op"] == "snapshot_cancel"
    assert cancel["parent_id"] == 11
    write_message(outgoing, snapshot_reply(by_parent[11], error="cancelled"))
    write_message(outgoing, snapshot_reply(by_parent[12]))
    with pytest.raises(EvidenceIncomplete, match="deadline elapsed"):
        first_done.result(timeout=3)
    assert second_done.result(timeout=3)["response"]["status"] == "ok"
    first.join(timeout=3)
    second.join(timeout=3)
    assert service._snapshot_pending == {}


def test_abandoned_foreground_can_release_through_parentless_cleanup(transport):
    from captain_hook.util import reqenv

    released = threading.Event()

    def dispatch(request):
        client = CURRENT_CLIENT.get()
        abandoned = threading.Event()
        abandoned.set()
        override = reqenv.RequestOverrides({}, "/fixture", 1, "fixture", deadline_unix_ms=1)
        with reqenv.use_request(override), reqenv.abandonable(abandoned):
            with pytest.raises(reqenv.Abandoned):
                client.call("stats")
            client.call("release", owner_epoch="owner", kind="lease", token="lease")
        released.set()
        return EventResponse(), None

    service, _, incoming, outgoing, _ = transport(dispatch)
    write_message(outgoing, event(41))
    cleanup = read_message(incoming)
    assert cleanup.get("parent_id", 0) == 0
    assert cleanup["snapshot"]["request"]["operation"] == "release"
    write_message(outgoing, snapshot_reply(cleanup))
    assert read_message(incoming)["op"] == "result"
    assert released.wait(timeout=2)
    assert service._snapshot_pending == {}


def test_cleanup_bypass_is_release_only_and_has_its_own_deadline(monkeypatch):
    import captain_hook.worker.service as module
    from captain_hook.snapshots.client import SnapshotProtocolError
    from captain_hook.util import reqenv

    service = WorkerService(io.BytesIO(), io.BytesIO(), dispatch=lambda _: (EventResponse(), None))
    monkeypatch.setattr(reqenv, "checkpoint", lambda: pytest.fail("cleanup touched abandoned scope"))
    try:
        with pytest.raises(SnapshotProtocolError):
            service.snapshot_exchange(0, {"request": {"operation": "stats"}}, cleanup=True)
        with pytest.raises(SnapshotProtocolError):
            service.snapshot_exchange(1, {"request": {"operation": "release"}}, cleanup=True)
        clock = iter([100.0, 106.0])
        monkeypatch.setattr(module.time, "time", lambda: next(clock))
        with pytest.raises(EvidenceIncomplete, match="deadline"):
            service.snapshot_exchange(0, {"request": {"operation": "release"}}, cleanup=True)
    finally:
        service._close_snapshots()
        service._executor.shutdown()
        service._background.shutdown()
