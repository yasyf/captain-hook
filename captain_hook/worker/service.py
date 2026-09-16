from __future__ import annotations

import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from typing import TYPE_CHECKING

from captain_hook.worker.protocol import (
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
    from collections.abc import Callable
    from typing import BinaryIO

    type Background = Callable[[], None]
    type Dispatch = Callable[[EventRequest], tuple[EventResponse, Background | None]]

REQUEST_THREADS = 16
BACKGROUND_THREADS = 4


def handshake(input_stream: BinaryIO, output_stream: BinaryIO, *, build: str) -> bool:
    if (first := read_message(input_stream)) is None:
        return False
    hello = decode_hello(first)
    if hello.build != build:
        raise ProtocolError(f"worker build {build!r} does not match host build {hello.build!r}")
    write_message(output_stream, hello_response(build))
    return True


class WorkerService:
    def __init__(
        self,
        input_stream: BinaryIO,
        output_stream: BinaryIO,
        *,
        dispatch: Dispatch,
        max_workers: int = REQUEST_THREADS,
        margin: float = 0.0,
    ) -> None:
        self._input = input_stream
        self._output = output_stream
        self._dispatch = dispatch
        self._margin = margin
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="capt-hook-worker")
        self._background = ThreadPoolExecutor(max_workers=BACKGROUND_THREADS, thread_name_prefix="capt-hook-async")
        self._write_guard = threading.Lock()
        self._guard = threading.Condition()
        self._outstanding = 0
        self._failure: BaseException | None = None

    def run(self) -> None:
        try:
            while (message := read_message(self._input)) is not None:
                self._submit(decode_event(message))
        finally:
            self._drain()
            self._executor.shutdown()
            self._background.shutdown(wait=False, cancel_futures=True)
        if self._failure is not None:
            raise self._failure

    def _submit(self, request: EventRequest) -> None:
        with self._guard:
            self._outstanding += 1
        future = self._executor.submit(self._serve, request)
        future.add_done_callback(self._done)

    def _serve(self, request: EventRequest) -> None:
        if request.deadline_within(self._margin):
            self._write(result_response(request.id, EventResponse(stderr=self._shed_note())))
            return
        start = time.perf_counter()
        try:
            response, background = self._dispatch(request)
        except Exception:
            self._write(error_response(request.id, traceback.format_exc()))
            return
        elapsed_ms = (time.perf_counter() - start) * 1000
        self._write(result_response(request.id, replace(response, elapsed_ms=elapsed_ms)))
        if background is not None:
            self._background.submit(background)

    def _shed_note(self) -> str:
        return f"capt-hook: the caller deadline is inside the {self._margin:g}s hook margin at dispatch; no verdict\n"

    def _done(self, future: Future[None]) -> None:
        with self._guard:
            if (exc := future.exception()) is not None and self._failure is None:
                self._failure = exc
            self._outstanding -= 1
            if self._outstanding == 0:
                self._guard.notify_all()

    def _write(self, message: dict[str, object]) -> None:
        with self._write_guard:
            write_message(self._output, message)

    def _drain(self) -> None:
        with self._guard:
            self._guard.wait_for(lambda: self._outstanding == 0)
