from __future__ import annotations

import hashlib
import json
import threading
import time
import traceback
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from contextvars import ContextVar
from dataclasses import dataclass, replace
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
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from typing import BinaryIO

    from captain_hook.snapshots.client import GraphSources, RegisteredWarmState, RootWarmState, SnapshotClient

    type Background = Callable[[], None]
    type Dispatch = Callable[[EventRequest], tuple[EventResponse, Background | None]]

REQUEST_THREADS = 16
MAX_PENDING_SNAPSHOTS = 256
MAX_PENDING_CLEANUPS = 64
MAX_WARM_JOBS = 32
WARM_STEP_BYTES = 8 * 1024 * 1024
WARM_STEP_SECONDS = 3
WARM_INTERVAL_SECONDS = 2
BACKGROUND_SNAPSHOT_CLIENT: ContextVar[Any] = ContextVar("background_snapshot_client", default=None)


@dataclass
class WarmJob:
    owner_key: str
    fingerprint: str
    state: RegisteredWarmState | RootWarmState
    admitted_order: int = 0
    last_served: int = 0


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
        self._cleanup = ThreadPoolExecutor(max_workers=2, thread_name_prefix="capt-hook-cleanup")
        self._cleanup_slots = threading.BoundedSemaphore(MAX_PENDING_CLEANUPS)
        self._warm_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="capt-hook-graph-warm")
        self._warm_guard = threading.Lock()
        self._warm_jobs: dict[str, WarmJob] = {}
        self._warm_queue: deque[str] = deque()
        self._warm_queued: set[str] = set()
        self._warm_running = False
        self._warm_order = 0
        self._warm_stop = threading.Event()
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
            with self._warm_guard:
                self._warm_stop.set()
            self._close_snapshots()
            self._drain()
            self._executor.shutdown()
            self._background.shutdown(wait=True)
            self._cleanup.shutdown(wait=True)
            self._warm_executor.shutdown(wait=True)
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
        from loguru import logger

        start = time.perf_counter()
        from captain_hook.snapshots.client import CURRENT_CLIENT, GRAPH_READ_BYTES, GRAPH_WORK_SECONDS, SnapshotClient

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
            foreground_seconds=GRAPH_WORK_SECONDS,
            foreground_read_bytes=GRAPH_READ_BYTES,
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
        from captain_hook.snapshots.client import RegisteredWarmState

        if not sources.thread_ids and not sources.direct_paths:
            return
        descriptor = {
            "thread_ids": sources.thread_ids,
            "roots": [str(root) for root in sources.roots],
            "direct_paths": [str(path) for path in sources.direct_paths],
            "tool_registry": client.tool_registry(),
        }
        fingerprint = hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        owner_key = sources.session_key or fingerprint
        warm_client = client.clone_for_exchange(lambda value: self.snapshot_exchange(0, value))
        self._schedule_warm_job(owner_key, fingerprint, RegisteredWarmState(sources, warm_client))

    def schedule_root_warm(self, client: SnapshotClient, path: Path, classifier: Mapping[str, str]) -> None:
        from captain_hook.snapshots.client import RootWarmState

        descriptor = {
            "path": str(path.absolute()),
            "classifier": dict(classifier),
            "tool_registry": client.tool_registry(),
        }
        fingerprint = hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        owner_key = f"root:{descriptor['path']}:{classifier['id']}:{classifier['version']}"
        warm_client = client.clone_for_exchange(lambda value: self.snapshot_exchange(0, value))
        self._schedule_warm_job(owner_key, fingerprint, RootWarmState(path, dict(classifier), warm_client))

    def _schedule_warm_job(self, owner_key: str, fingerprint: str, state: RegisteredWarmState | RootWarmState) -> None:
        with self._warm_guard:
            if self._warm_stop.is_set():
                return
            previous = self._warm_jobs.get(owner_key)
            if previous is not None and previous.fingerprint == fingerprint:
                return
            if previous is None and len(self._warm_jobs) >= MAX_WARM_JOBS:
                victim = max(self._warm_jobs.values(), key=lambda job: (job.last_served, job.admitted_order))
                del self._warm_jobs[victim.owner_key]
                if victim.owner_key in self._warm_queued:
                    self._warm_queue.remove(victim.owner_key)
                    self._warm_queued.remove(victim.owner_key)
            self._warm_order += 1
            self._warm_jobs[owner_key] = WarmJob(owner_key, fingerprint, state, admitted_order=self._warm_order)
            if owner_key not in self._warm_queued:
                self._warm_queue.append(owner_key)
                self._warm_queued.add(owner_key)
            if not self._warm_running:
                self._warm_running = True
                self._warm_executor.submit(self._run_warm_loop)

    def _run_warm_loop(self) -> None:
        from loguru import logger

        from captain_hook.snapshots.client import EvidenceIncomplete

        while not self._warm_stop.is_set():
            with self._warm_guard:
                if not self._warm_queue:
                    self._warm_running = False
                    return
                owner_key = self._warm_queue.popleft()
                self._warm_queued.remove(owner_key)
                job = self._warm_jobs[owner_key]
            try:
                complete = self._warm_step(job)
                failed = False
            except EvidenceIncomplete as exc:
                logger.bind(status=exc.status, reason=exc.reason).warning("transcript warming stopped")
                complete = True
                failed = True
            except Exception:
                logger.exception("transcript warming failed")
                complete = True
                failed = True
            with self._warm_guard:
                current = self._warm_jobs.get(owner_key) is job
                if current:
                    if complete:
                        del self._warm_jobs[owner_key]
                    elif owner_key not in self._warm_queued:
                        self._warm_order += 1
                        job.last_served = self._warm_order
                        self._warm_queue.append(owner_key)
                        self._warm_queued.add(owner_key)
            if current and complete and not failed:
                logger.bind(warm_key=job.fingerprint[:16], kind=type(job.state).__name__).info(
                    "transcript warming complete"
                )
            interval = WARM_INTERVAL_SECONDS if job.state.last_usage.get("source_bytes_read", 0) else 0
            if self._warm_stop.wait(interval):
                break
        with self._warm_guard:
            self._warm_running = False

    def _warm_step(self, job: WarmJob) -> bool:
        return job.state.step(read_bytes=WARM_STEP_BYTES, deadline_seconds=WARM_STEP_SECONDS)

    def _write(self, message: dict[str, object], *, max_frame: int | None = None) -> None:
        with self._write_guard:
            if max_frame is None:
                write_message(self._output, message)
            else:
                write_message(self._output, message, max_frame=max_frame)

    def snapshot_exchange(
        self, parent_id: int, request: dict[str, Any], *, cleanup: bool = False, expires_unix_ms: int | None = None
    ) -> dict[str, Any]:
        from captain_hook.snapshots.client import CLEANUP_SECONDS, EvidenceIncomplete, SnapshotProtocolError
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
