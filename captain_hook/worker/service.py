from __future__ import annotations

import hashlib
import json
import threading
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from contextvars import ContextVar
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from captain_hook.worker.protocol import (
    MAX_SNAPSHOT_FRAME,
    OP_ABANDON,
    OP_ERROR,
    OP_SNAPSHOT_RESULT,
    EventRequest,
    EventResponse,
    ProtocolError,
    adopt_message,
    background_begin_message,
    background_end_message,
    decode_abandon,
    decode_event,
    decode_hello,
    decode_snapshot_reply,
    error_response,
    hello_response,
    read_message,
    result_response,
    snapshot_cancel_message,
    snapshot_request_message,
    snapshot_warm_message,
    write_message,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from typing import BinaryIO

    from captain_hook.snapshots.client import GraphSources, SnapshotClient

    type Background = Callable[[], None]
    type Dispatch = Callable[[EventRequest], tuple[EventResponse, Background | None]]

REQUEST_THREADS = 16
MANDATORY_REQUEST_THREADS = 4
MAX_PENDING_SNAPSHOTS = 256
MAX_PENDING_CLEANUPS = 64
MAX_WARM_DEMANDS = 256
MAX_WARM_DEMAND_BYTES = MAX_SNAPSHOT_FRAME - 512
WARM_DEMAND_SECONDS = 10
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
        guarded: Callable[[EventRequest], bool] = lambda request: request.mandatory,
        max_workers: int = REQUEST_THREADS,
    ) -> None:
        self._input = input_stream
        self._output = output_stream
        self._dispatch = dispatch
        self._guarded = guarded
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="capt-hook-worker")
        self._mandatory_executor = ThreadPoolExecutor(
            max_workers=MANDATORY_REQUEST_THREADS, thread_name_prefix="capt-hook-mandatory-request"
        )
        self._background = ThreadPoolExecutor(max_workers=4, thread_name_prefix="capt-hook-async")
        self._cleanup = ThreadPoolExecutor(max_workers=2, thread_name_prefix="capt-hook-cleanup")
        self._cleanup_slots = threading.BoundedSemaphore(MAX_PENDING_CLEANUPS)
        self._warm_guard = threading.Lock()
        self._warm_demands: dict[str, float] = {}
        self._write_guard = threading.Lock()
        self._guard = threading.Condition()
        self._serving: dict[int, EventRequest] = {}
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
                match message.get("op"):
                    case op if op in {OP_SNAPSHOT_RESULT, OP_ERROR}:
                        self._complete_snapshot(message)
                    case op if op == OP_ABANDON:
                        self._abandon(decode_abandon(message))
                    case _:
                        self._submit(decode_event(message))
        finally:
            self._close_snapshots()
            self._drain()
            self._executor.shutdown()
            self._mandatory_executor.shutdown()
            self._background.shutdown(wait=True)
            self._cleanup.shutdown(wait=True)
        if self._failure is not None:
            raise self._failure

    def adopt(self, pid: int, lifetime_ms: int) -> None:
        self._write(adopt_message(pid, lifetime_ms))

    def _submit(self, request: EventRequest) -> None:
        with self._guard:
            self._outstanding += 1
            self._serving[request.id] = request
        executor = self._mandatory_executor if self._guarded(request) else self._executor
        future = executor.submit(self._serve, request)
        future.add_done_callback(self._done)

    def _abandon(self, request_id: int) -> None:
        with self._guard:
            if (request := self._serving.get(request_id)) is not None:
                request.abandon.set()

    def _serve(self, request: EventRequest) -> None:
        try:
            self._reply(request)
        finally:
            with self._guard:
                self._serving.pop(request.id, None)

    def _reply(self, request: EventRequest) -> None:
        if request.deadline_passed():
            self._write(error_response(request.id, "deadline passed before dispatch"))
            return
        from loguru import logger

        start = time.perf_counter()
        from captain_hook.snapshots.client import CURRENT_CLIENT, SnapshotClient, foreground_seconds

        def cleanup(value: dict[str, object]) -> dict[str, Any]:
            return self.snapshot_exchange(0, value, cleanup=True)

        foreground = SnapshotClient(
            lambda value: self.snapshot_exchange(
                request.id, value, expires_unix_ms=foreground.foreground_deadline_unix_ms
            ),
            cleanup_exchange=cleanup,
            warm_scheduler=self.schedule_graph_warm,
            root_warm_scheduler=self.schedule_root_warm,
            defer_cleanup=True,
            foreground_seconds=foreground_seconds(request.event),
        )
        background_client = SnapshotClient(
            lambda value: self.snapshot_exchange(0, value),
            cleanup_exchange=cleanup,
            warm_scheduler=self.schedule_graph_warm,
            root_warm_scheduler=self.schedule_root_warm,
        )

        def retry_pending_cleanup() -> None:
            for client in (foreground, background_client):
                try:
                    client.close_pending()
                except Exception:
                    logger.exception("snapshot client cleanup failed")

        token = CURRENT_CLIENT.set(foreground)
        background_token = BACKGROUND_SNAPSHOT_CLIENT.set(background_client)
        try:
            response, background = self._dispatch(request)
        except Exception:
            try:
                self._write(error_response(request.id, traceback.format_exc()))
            finally:
                self._schedule_cleanup(retry_pending_cleanup)
            return
        finally:
            CURRENT_CLIENT.reset(token)
            BACKGROUND_SNAPSHOT_CLIENT.reset(background_token)
        elapsed_ms = (time.perf_counter() - start) * 1000
        result = result_response(request.id, replace(response, elapsed_ms=elapsed_ms))
        if background is None:
            try:
                self._write(result)
            finally:
                self._schedule_cleanup(retry_pending_cleanup)
            return
        self._write(background_begin_message(request.id))
        with self._guard:
            self._background_outstanding += 1
        try:
            self._write(result)
            future = self._background.submit(background)
        except BaseException:
            retry_pending_cleanup()
            try:
                self._end_background(request.id)
            except Exception:
                logger.exception("background completion failed")
            raise
        future.add_done_callback(lambda completed: self._background_done(request.id, completed, retry_pending_cleanup))

    def _background_done(
        self, request_id: int, future: Future[None], retry_pending_cleanup: Callable[[], None]
    ) -> None:
        try:
            if (exc := future.exception()) is not None:
                with self._guard:
                    if self._failure is None:
                        self._failure = exc
        finally:
            retry_pending_cleanup()
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

    def _schedule_cleanup(self, cleanup: Callable[[], None]) -> None:
        if not self._cleanup_slots.acquire(blocking=False):
            cleanup()
            return

        def run() -> None:
            try:
                cleanup()
            finally:
                self._cleanup_slots.release()

        self._cleanup.submit(run)

    def _done(self, future: Future[None]) -> None:
        with self._guard:
            if (exc := future.exception()) is not None and self._failure is None:
                self._failure = exc
            self._outstanding -= 1
            if self._outstanding == 0:
                self._guard.notify_all()

    def schedule_graph_warm(self, client: SnapshotClient, sources: GraphSources) -> None:
        if not sources.thread_ids and not sources.direct_paths:
            return
        self._post_warm_demand(client.registered_warm_demand(sources))

    def schedule_root_warm(
        self, client: SnapshotClient, path: Path, classifier: Mapping[str, str], tail_bytes: int | None
    ) -> None:
        self._post_warm_demand(client.root_warm_demand(path, classifier, tail_bytes))

    def _post_warm_demand(self, demand: dict[str, object]) -> None:
        from loguru import logger

        encoded = json.dumps(demand, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        with self._warm_guard:
            now = time.monotonic()
            if (posted := self._warm_demands.get(fingerprint)) is not None and now - posted < WARM_DEMAND_SECONDS:
                return
            self._warm_demands.pop(fingerprint, None)
            if len(self._warm_demands) >= MAX_WARM_DEMANDS:
                del self._warm_demands[next(iter(self._warm_demands))]
            self._warm_demands[fingerprint] = now
        if len(encoded) > MAX_WARM_DEMAND_BYTES:
            logger.bind(demand_bytes=len(encoded)).warning("transcript warm demand exceeds its frame bound")
            return
        self._write(snapshot_warm_message(demand), max_frame=MAX_SNAPSHOT_FRAME)

    def _write(self, message: dict[str, object], *, max_frame: int | None = None) -> None:
        with self._write_guard:
            if max_frame is None:
                write_message(self._output, message)
            else:
                write_message(self._output, message, max_frame=max_frame)

    def snapshot_exchange(
        self, parent_id: int, request: dict[str, Any], *, cleanup: bool = False, expires_unix_ms: int | None = None
    ) -> dict[str, Any]:
        from loguru import logger

        from captain_hook.snapshots.client import (
            CLEANUP_SECONDS,
            EvidenceIncomplete,
            SnapshotProtocolError,
            request_metadata,
        )
        from captain_hook.util import reqenv

        nested: dict[str, Any] | None = request.get("request")
        if cleanup and (parent_id != 0 or not isinstance(nested, dict) or nested.get("operation") != "release"):
            raise SnapshotProtocolError("cleanup transport is reserved for parentless release requests")
        if not cleanup:
            reqenv.checkpoint()
        deadline = nested.get("deadline_unix_ms") if isinstance(nested, dict) else None
        expires = (
            time.time() + CLEANUP_SECONDS
            if cleanup
            else min(time.time() + 120, deadline / 1000)
            if type(deadline) is int
            else time.time() + 120
        )
        if parent_id and (remaining := reqenv.seconds_left()) is not None:
            expires = min(expires, time.time() + remaining)
        if expires_unix_ms is not None:
            expires = min(expires, expires_unix_ms / 1000)
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
            started = time.monotonic()
            self._write(snapshot_request_message(request_id, parent_id, request), max_frame=MAX_SNAPSHOT_FRAME)
            sent = True
            while True:
                if not cleanup:
                    reqenv.checkpoint()
                left = expires - time.time()
                if left <= 0:
                    logger.bind(
                        **request_metadata(nested or {}),
                        status="deadline",
                        parent_id=parent_id,
                        frame_id=request_id,
                        foreground_deadline_unix_ms=expires_unix_ms,
                        expires_unix_ms=round(expires * 1000),
                        elapsed_ms=round((time.monotonic() - started) * 1000, 1),
                    ).info("snapshot transport deadline elapsed")
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
