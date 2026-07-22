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


class WorkerService:
    def __init__(
        self,
        input_stream: BinaryIO,
        output_stream: BinaryIO,
        *,
        build: str,
        dispatch: Callable[[EventRequest], EventResponse],
        max_workers: int = 16,
    ) -> None:
        self._input = input_stream
        self._output = output_stream
        self._build = build
        self._dispatch = dispatch
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="capt-hook-worker")
        self._write_guard = threading.Lock()
        self._guard = threading.Condition()
        self._outstanding = 0
        self._failure: BaseException | None = None

    def run(self) -> None:
        try:
            first = read_message(self._input)
            if first is None:
                return
            hello = decode_hello(first)
            if hello.build != self._build:
                raise ProtocolError(f"worker build {self._build!r} does not match host build {hello.build!r}")
            self._write(hello_response(self._build))
            while (message := read_message(self._input)) is not None:
                request = decode_event(message)
                if request.build != self._build:
                    raise ProtocolError(f"event build {request.build!r} does not match worker build {self._build!r}")
                self._submit(request)
        finally:
            self._drain()
            self._executor.shutdown()
        if self._failure is not None:
            raise self._failure

    def _submit(self, request: EventRequest) -> None:
        with self._guard:
            self._outstanding += 1
        future = self._executor.submit(self._serve, request)
        future.add_done_callback(self._done)

    def _serve(self, request: EventRequest) -> None:
        start = time.perf_counter()
        try:
            response = self._dispatch(request)
        except Exception:
            self._write(error_response(request.id, traceback.format_exc()))
            return
        elapsed_ms = (time.perf_counter() - start) * 1000
        self._write(result_response(request.id, replace(response, elapsed_ms=elapsed_ms)))

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
