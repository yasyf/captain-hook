from __future__ import annotations

import io
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import pytest

from captain_hook.worker.protocol import (
    EventResponse,
    background_begin_message,
    background_end_message,
    read_message,
    write_message,
)
from captain_hook.worker.service import WorkerService


def request_frame(request_id: int) -> dict[str, object]:
    return {
        'protocol': 1, 'op': 'event', 'id': request_id,
        'request': {
            'schema': 1, 'event': 'PreToolUse', 'root': '/fixture', 'cwd': '/fixture',
            'env': {}, 'payload_raw': '{}', 'client_pid': 100, 'client_ppid': 99, 'deadline_unix_ms': 0,
        },
    }


def input_stream(*request_ids: int) -> io.BytesIO:
    stream = io.BytesIO()
    for request_id in request_ids:
        write_message(stream, request_frame(request_id))
    stream.seek(0)
    return stream


class RecordingOutput(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.frames: list[dict[str, Any]] = []
        self.changed = threading.Condition()

    def flush(self) -> None:
        stream = io.BytesIO(self.getvalue())
        frames = []
        while (frame := read_message(stream)) is not None:
            frames.append(frame)
        with self.changed:
            self.frames = frames
            self.changed.notify_all()

    def wait_for_begins(self, count: int) -> bool:
        with self.changed:
            return self.changed.wait_for(
                lambda: sum(frame['op'] == 'background_begin' for frame in self.frames) == count, timeout=3,
            )


def start(service: WorkerService) -> tuple[threading.Thread, Future[None]]:
    result: Future[None] = Future()

    def run() -> None:
        try:
            service.run()
            result.set_result(None)
        except BaseException as exc:
            result.set_exception(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def test_background_frames_have_exact_shape() -> None:
    assert background_begin_message(7) == {'protocol': 1, 'op': 'background_begin', 'id': 7}
    assert background_end_message(7) == {'protocol': 1, 'op': 'background_end', 'id': 7}


def test_begin_precedes_result_and_callback_precedes_end() -> None:
    output = RecordingOutput()
    seen = []

    def callback() -> None:
        seen.extend(frame['op'] for frame in output.frames)

    service = WorkerService(input_stream(7), output, dispatch=lambda _: (EventResponse(), callback))
    service.run()
    assert seen == ['background_begin', 'result']
    assert [frame['op'] for frame in output.frames] == ['background_begin', 'result', 'background_end']
    assert [frame['id'] for frame in output.frames] == [7, 7, 7]
    assert service._background_outstanding == 0


def test_no_callback_has_no_background_ticket() -> None:
    output = RecordingOutput()
    service = WorkerService(input_stream(1), output, dispatch=lambda _: (EventResponse(), None))
    service.run()
    assert [frame['op'] for frame in output.frames] == ['result']
    assert service._background_outstanding == 0


def test_callback_failure_releases_ticket_and_is_observable() -> None:
    output = RecordingOutput()

    def callback() -> None:
        raise RuntimeError('callback failed')

    service = WorkerService(input_stream(1), output, dispatch=lambda _: (EventResponse(), callback))
    with pytest.raises(RuntimeError, match='callback failed'):
        service.run()
    assert [frame['op'] for frame in output.frames] == ['background_begin', 'result', 'background_end']
    assert service._background_outstanding == 0


def test_submission_failure_releases_ticket() -> None:
    output = RecordingOutput()
    invoked = []
    service = WorkerService(input_stream(1), output, dispatch=lambda _: (EventResponse(), lambda: invoked.append(True)))
    service._background.shutdown()
    with pytest.raises(RuntimeError, match='cannot schedule new futures after shutdown'):
        service.run()
    assert [frame['op'] for frame in output.frames] == ['background_begin', 'result', 'background_end']
    assert invoked == []
    assert service._background_outstanding == 0


def test_eof_drains_running_and_queued_callbacks_without_cancellation() -> None:
    output = RecordingOutput()
    release = threading.Event()
    started = threading.Event()
    callbacks = []

    def dispatch(request):
        def callback() -> None:
            started.set()
            assert release.wait(timeout=3)
            assert service._snapshot_closed
            callbacks.append(request.id)
        return EventResponse(), callback

    service = WorkerService(input_stream(1, 2, 3), output, dispatch=dispatch)
    service._background.shutdown()
    service._background = ThreadPoolExecutor(max_workers=1)
    thread, result = start(service)
    try:
        assert started.wait(timeout=3)
        assert output.wait_for_begins(3)
        with service._guard:
            assert service._guard.wait_for(lambda: service._background_outstanding == 3, timeout=3)
        assert not result.done()
    finally:
        release.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    result.result()
    assert sorted(callbacks) == [1, 2, 3]
    assert service._background_outstanding == 0
    for request_id in (1, 2, 3):
        assert [frame['op'] for frame in output.frames if frame['id'] == request_id] == [
            'background_begin', 'result', 'background_end',
        ]
