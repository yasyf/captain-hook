from __future__ import annotations

import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from contextvars import ContextVar
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from captain_hook.worker.protocol import (
    MAX_SNAPSHOT_FRAME,
    OP_ERROR,
    OP_SNAPSHOT_RESULT,
    EventRequest,
    EventResponse,
    ProtocolError,
    adopt_message,
    background_begin_message,
    background_end_message,
    decode_event,
    decode_hello,
    decode_snapshot_reply,
    error_response,
    hello_response,
    read_message,
    result_response,
    snapshot_cancel_message,
    snapshot_request_message,
    write_message,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import BinaryIO

    type Background = Callable[[], None]
    type Dispatch = Callable[[EventRequest], tuple[EventResponse, Background | None]]

REQUEST_THREADS = 16
MAX_PENDING_SNAPSHOTS = 256
BACKGROUND_SNAPSHOT_CLIENT: ContextVar[Any] = ContextVar("background_snapshot_client", default=None)


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
    ) -> None:
        self._input = input_stream
        self._output = output_stream
        self._dispatch = dispatch
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="capt-hook-worker")
        self._background = ThreadPoolExecutor(max_workers=4, thread_name_prefix="capt-hook-async")
        self._write_guard = threading.Lock()
        self._guard = threading.Condition()
        self._outstanding = 0
        self._background_outstanding = 0
        self._failure: BaseException | None = None
        self._snapshot_guard = threading.Lock()
        self._snapshot_next_id = 0
        self._snapshot_pending: dict[int, tuple[int, Future[dict[str, Any]]]] = {}
        self._snapshot_closed = False

    def run(self) -> None:
        try:
            while (message := read_message(self._input)) is not None:
                if message.get("op") in {OP_SNAPSHOT_RESULT, OP_ERROR}:
                    self._complete_snapshot(message)
                else:
                    self._submit(decode_event(message))
        finally:
            self._close_snapshots()
            self._drain()
            self._executor.shutdown()
            self._background.shutdown(wait=True)
        if self._failure is not None:
            raise self._failure

    def adopt(self, pid: int, lifetime_ms: int) -> None:
        self._write(adopt_message(pid, lifetime_ms))

    def _submit(self, request: EventRequest) -> None:
        with self._guard:
            self._outstanding += 1
        future = self._executor.submit(self._serve, request)
        future.add_done_callback(self._done)

    def _serve(self, request: EventRequest) -> None:
        if request.deadline_passed():
            self._write(error_response(request.id, "deadline passed before dispatch"))
            return
        start = time.perf_counter()
        from captain_hook.snapshots.client import CURRENT_CLIENT, SnapshotClient

        def cleanup(value: dict[str, object]) -> dict[str, Any]:
            return self.snapshot_exchange(0, value, cleanup=True)

        foreground = SnapshotClient(lambda value: self.snapshot_exchange(request.id, value), cleanup_exchange=cleanup)
        background_client = SnapshotClient(lambda value: self.snapshot_exchange(0, value), cleanup_exchange=cleanup)
        token = CURRENT_CLIENT.set(foreground)
        background_token = BACKGROUND_SNAPSHOT_CLIENT.set(background_client)
        try:
            response, background = self._dispatch(request)
        except Exception:
            self._write(error_response(request.id, traceback.format_exc()))
            return
        finally:
            CURRENT_CLIENT.reset(token)
            BACKGROUND_SNAPSHOT_CLIENT.reset(background_token)
        elapsed_ms = (time.perf_counter() - start) * 1000
        result = result_response(request.id, replace(response, elapsed_ms=elapsed_ms))
        if background is None:
            self._write(result)
            return
        self._write(background_begin_message(request.id))
        with self._guard:
            self._background_outstanding += 1
        try:
            self._write(result)
            future = self._background.submit(background)
        except BaseException:
            self._end_background(request.id)
            raise
        future.add_done_callback(lambda completed: self._background_done(request.id, completed))

    def _background_done(self, request_id: int, future: Future[None]) -> None:
        try:
            if (exc := future.exception()) is not None:
                with self._guard:
                    if self._failure is None:
                        self._failure = exc
        finally:
            try:
                self._end_background(request_id)
            except BaseException as exc:
                with self._guard:
                    if self._failure is None:
                        self._failure = exc

    def _end_background(self, request_id: int) -> None:
        try:
            self._write(background_end_message(request_id))
        finally:
            with self._guard:
                self._background_outstanding -= 1
                self._guard.notify_all()

    def _done(self, future: Future[None]) -> None:
        with self._guard:
            if (exc := future.exception()) is not None and self._failure is None:
                self._failure = exc
            self._outstanding -= 1
            if self._outstanding == 0:
                self._guard.notify_all()

    def _write(self, message: dict[str, object], *, max_frame: int | None = None) -> None:
        with self._write_guard:
            if max_frame is None:
                write_message(self._output, message)
            else:
                write_message(self._output, message, max_frame=max_frame)

    def snapshot_exchange(self, parent_id: int, request: dict[str, Any], *, cleanup: bool = False) -> dict[str, Any]:
        from captain_hook.snapshots.client import EvidenceIncomplete, SnapshotProtocolError
        from captain_hook.util import reqenv

        nested: dict[str, Any] | None = request.get("request")
        if cleanup and (parent_id != 0 or not isinstance(nested, dict) or nested.get("operation") != "release"):
            raise SnapshotProtocolError("cleanup transport is reserved for parentless release requests")
        if not cleanup:
            reqenv.checkpoint()
        deadline = nested.get("deadline_unix_ms") if isinstance(nested, dict) else None
        expires = (
            time.time() + 5
            if cleanup
            else min(time.time() + 120, deadline / 1000)
            if type(deadline) is int
            else time.time() + 120
        )
        if parent_id and (remaining := reqenv.seconds_left()) is not None:
            expires = min(expires, time.time() + remaining)
        future: Future[dict[str, Any]] = Future()
        with self._snapshot_guard:
            if self._snapshot_closed:
                raise EvidenceIncomplete("cancelled", "snapshot transport is closed")
            if len(self._snapshot_pending) >= MAX_PENDING_SNAPSHOTS:
                raise EvidenceIncomplete("lease_limit", "snapshot transport has no available request slots")
            self._snapshot_next_id += 1
            request_id = self._snapshot_next_id
            self._snapshot_pending[request_id] = (parent_id, future)
        sent = False
        try:
            self._write(snapshot_request_message(request_id, parent_id, request), max_frame=MAX_SNAPSHOT_FRAME)
            sent = True
            while True:
                if not cleanup:
                    reqenv.checkpoint()
                left = expires - time.time()
                if left <= 0:
                    raise EvidenceIncomplete("deadline", "snapshot transport deadline elapsed")
                try:
                    return future.result(timeout=min(left, 0.05))
                except TimeoutError:
                    continue
        finally:
            if not sent:
                with self._snapshot_guard:
                    self._snapshot_pending.pop(request_id, None)
            else:
                with self._snapshot_guard:
                    cancel = future.cancel()
                    closed = self._snapshot_closed
                if cancel and not closed:
                    self._write(snapshot_cancel_message(request_id, parent_id), max_frame=MAX_SNAPSHOT_FRAME)

    def _complete_snapshot(self, message: dict[str, Any]) -> None:
        from captain_hook.snapshots.client import EvidenceIncomplete

        reply = decode_snapshot_reply(message)
        with self._snapshot_guard:
            pending = self._snapshot_pending.get(reply.id)
            if pending is None or pending[0] != reply.parent_id:
                raise ProtocolError("snapshot reply does not match a pending request")
            _, future = self._snapshot_pending.pop(reply.id)
            if future.cancelled():
                return
            if reply.error is not None:
                future.set_exception(EvidenceIncomplete("cancelled", reply.error))
            else:
                assert reply.snapshot is not None
                future.set_result(reply.snapshot)

    def _close_snapshots(self) -> None:
        from captain_hook.snapshots.client import EvidenceIncomplete

        with self._snapshot_guard:
            self._snapshot_closed = True
            pending, self._snapshot_pending = self._snapshot_pending, {}
            for _, future in pending.values():
                if not future.done():
                    future.set_exception(EvidenceIncomplete("cancelled", "snapshot transport closed before reply"))

    def _drain(self) -> None:
        with self._guard:
            self._guard.wait_for(lambda: self._outstanding == 0 and self._background_outstanding == 0)
