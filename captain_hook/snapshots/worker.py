from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import struct
import sys
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, BinaryIO

from captain_hook.snapshots.client import (
    CORE_SCHEMA,
    HOST_SCHEMA,
    MAX_FRAME_BYTES,
    EvidenceIncomplete,
    SnapshotProtocolError,
    encode_frame,
    read_exact,
)
from captain_hook.snapshots.validation import checked, parse_exact, validator

if TYPE_CHECKING:
    from cc_transcript.snapshots import CallContext


@dataclass(frozen=True)
class Pending:
    request_id: str
    context: CallContext
    token: Any
    future: Future[dict[str, Any]]


def read_frame(stream: BinaryIO, record_bytes: Callable[[int], None] | None = None) -> dict[str, Any] | None:
    first = stream.read(1)
    if not first:
        return None
    length = struct.unpack(">I", first + read_exact(stream, 3))[0]
    if not 0 < length <= MAX_FRAME_BYTES:
        raise SnapshotProtocolError("snapshot frame exceeds encoded byte bound")
    payload = read_exact(stream, length)
    if record_bytes is not None:
        record_bytes(length + 4)
    result = parse_exact(payload)
    if not isinstance(result, dict):
        raise SnapshotProtocolError("snapshot frame must be an object")
    return result


def empty_usage() -> dict[str, int]:
    return dict.fromkeys(validator("response").schema["$defs"]["Usage"]["properties"], 0)


def failure(request_id: str, status: str, reason: str, usage: dict[str, int] | None = None) -> dict[str, Any]:
    counts = empty_usage() | (usage or {})
    counter = "requests_cancelled" if status == "cancelled" else "requests_failed"
    counts[counter] = max(counts[counter], 1)
    return {
        "schema": CORE_SCHEMA,
        "id": request_id,
        "status": status,
        "complete": False,
        "data": None,
        "cursor": None,
        "reason": reason[:4096],
        "usage": counts,
    }


class Owner:
    def __init__(self, config: dict[str, Any]) -> None:
        from cc_transcript.snapshots import CancellationToken, SnapshotIncomplete, TranscriptStore

        from captain_hook.classifiers.conductor import classifier as conductor
        from captain_hook.classifiers.lane import classifier as lane
        from captain_hook.snapshots.review import ReviewPolicy

        self.store = TranscriptStore(
            config,
            policies={
                ("captain-lane", "1"): lane,
                ("captain-conductor", "1"): conductor,
            },
        )
        self.token_type = CancellationToken
        self.incomplete_type = SnapshotIncomplete
        self.policy = ReviewPolicy()
        self.runner: asyncio.Runner | None = None

    def _run_policy(self, snapshot: Any, request: dict[str, Any]) -> dict[str, Any]:
        if self.runner is None:
            self.runner = asyncio.Runner()
        handler = getattr(self.policy, request["operation"])
        return self.runner.run(handler(snapshot, request))

    def _page(self, page: dict[str, Any], request_id: str) -> dict[str, Any]:
        field = page["field"]
        records = page["records_json"]
        if field == "corrections":
            records = [json.loads(record) for record in records]
        data = page["metadata"] | {field: records}
        return {
            "schema": CORE_SCHEMA,
            "id": request_id,
            "status": "ok" if page["complete"] else "incomplete",
            "complete": page["complete"],
            "data": data,
            "cursor": page["cursor"],
            "reason": None if page["complete"] else "projection page bound",
            "usage": page["usage"],
        }

    def _classification(self, page: dict[str, Any], request_id: str) -> dict[str, Any]:
        data = (
            {"kind": "acquired", "description": page["description"]}
            if page["complete"]
            else {
                "kind": "classification",
                "record_schema": "cc-transcript.event/1",
                "records_json": page["records_json"],
                "event_start": page["event_start"],
            }
        )
        return {
            "schema": CORE_SCHEMA,
            "id": request_id,
            "status": "ok" if page["complete"] else "incomplete",
            "complete": page["complete"],
            "data": data,
            "cursor": page["cursor"],
            "reason": None if page["complete"] else "caller classification required",
            "usage": page["usage"],
        }

    def _hook_classifier(self, snapshot: Any, request: dict[str, Any]) -> dict[str, str]:
        from pathlib import Path

        from cc_transcript.discovery import is_subagent_path

        path = snapshot.description["canonical_path"]
        if is_subagent_path(Path(path)):
            return {"id": "captain-lane", "version": "1"}
        facts = snapshot.classifier_facts("<system_instruction>", event_limit=50)
        if facts["has_users"] and facts["all_users_sidechain"]:
            return {"id": "captain-lane", "version": "1"}
        conductor = (
            bool(request["cwd"] and "conductor/workspaces" in request["cwd"])
            or "conductor-workspaces" in path
            or facts["has_user_prefix"]
        )
        if conductor and not request["droid"]:
            return {"id": "captain-conductor", "version": "1"}
        return {"id": "native", "version": "1"}

    def call(
        self, request: dict[str, Any], context: CallContext, token: Any, tool_registry: list[dict[str, Any]]
    ) -> dict[str, Any]:
        usage: dict[str, int] | None = None
        try:
            context["registry_generation"] = self.store.register_tool_registry(tool_registry)
            if request["schema"] == CORE_SCHEMA:
                if request["operation"] == "resume":
                    page = self.store.resume_projection(request["cursor"], context=context, cancellation=token)
                    if page is not None:
                        return self._page(page, request["id"])
                return dict(self.store.request(request, context=context, cancellation=token))
            if request["operation"] == "prepare_classifier":
                return self._classification(
                    self.store.prepare_classifier(
                        request["handle"],
                        request["classifier"],
                        context=context,
                        cancellation=token,
                        limits=request["limits"],
                        deadline_unix_ms=request["deadline_unix_ms"],
                    ),
                    request["id"],
                )
            if request["operation"] == "submit_classifier":
                return self._classification(
                    self.store.submit_classifier(
                        request["cursor"],
                        request["labels"],
                        context=context,
                        cancellation=token,
                    ),
                    request["id"],
                )
            if request["operation"] == "prepare_hook_view":
                with self.store.borrow_snapshot(
                    request["view"]["handle"],
                    context=context,
                    cancellation=token,
                    limits=request["limits"],
                    deadline_unix_ms=request["deadline_unix_ms"],
                ) as snapshot:
                    try:
                        classifier = self._hook_classifier(snapshot, request)
                    finally:
                        usage = dict(snapshot.usage)
                return {
                    "schema": CORE_SCHEMA,
                    "id": request["id"],
                    "status": "ok",
                    "complete": True,
                    "data": {"kind": "classifier", "classifier": classifier},
                    "cursor": None,
                    "reason": None,
                    "usage": usage,
                }
            if request["policy"] != {"id": "captain-review", "version": "1"}:
                raise EvidenceIncomplete("invalid_request", "unregistered Captain evidence policy")
            with self.store.borrow_snapshot(
                request["view"]["handle"],
                context=context,
                cancellation=token,
                limits=request["limits"],
                deadline_unix_ms=request["deadline_unix_ms"],
            ) as snapshot:
                try:
                    result = self._run_policy(snapshot, request)
                finally:
                    usage = dict(snapshot.usage)
                field = "candidates_json" if result["kind"] == "review" else "corrections"
                records = result.pop(field)
                if field == "corrections":
                    records = [json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in records]
                page = self.store.publish_projection(
                    request,
                    context=context,
                    cancellation=token,
                    metadata=result,
                    field=field,
                    records_json=records,
                    usage=usage,
                    work=dict(snapshot.work),
                )
            return self._page(page, request["id"])
        except self.incomplete_type as exc:
            return failure(request["id"], exc.status, exc.reason, dict(exc.usage))
        except EvidenceIncomplete as exc:
            return failure(request["id"], exc.status, exc.reason, usage)

    def discard(self, response: dict[str, Any], context: CallContext) -> None:
        self.store.discard_response(response, context=context)

    def record_transport(self, count: int) -> None:
        self.store.record_transport(count)

    def close(self) -> None:
        if self.runner is not None:
            self.runner.run(self.policy.close())
            self.runner.close()


class OwnerService:
    def __init__(self, input_stream: BinaryIO, output_stream: BinaryIO, owner: Any) -> None:
        self.input = input_stream
        self.output = output_stream
        self.owner = owner
        self.guard = threading.RLock()
        self.write_guard = threading.Lock()
        self.pending: dict[int, Pending] = {}
        self.executors = {
            "hook": ThreadPoolExecutor(max_workers=4, thread_name_prefix="capt-evidence-hook"),
            "review": ThreadPoolExecutor(max_workers=1, thread_name_prefix="capt-evidence-review"),
            "release": ThreadPoolExecutor(max_workers=4, thread_name_prefix="capt-evidence-release"),
        }
        self.slots = {
            "hook": threading.BoundedSemaphore(4),
            "review": threading.BoundedSemaphore(1),
            "release": threading.BoundedSemaphore(4),
        }
        self.failed: BaseException | None = None

    def write(self, message: dict[str, Any]) -> None:
        frame = encode_frame(message)
        with self.write_guard:
            remaining = memoryview(frame)
            while remaining:
                written = self.output.write(remaining)
                if written is None or written <= 0:
                    raise SnapshotProtocolError("snapshot output closed during frame")
                self.owner.record_transport(written)
                remaining = remaining[written:]
            self.output.flush()

    def result(self, frame_id: int, response: dict[str, Any], context: CallContext) -> None:
        try:
            wrapped = checked("host-response", {"schema": HOST_SCHEMA, "response": response})
            message = {"protocol": 1, "op": "snapshot_result", "id": frame_id, "snapshot": wrapped}
            try:
                self.write(message)
            except EvidenceIncomplete as exc:
                if exc.status != "output_limit":
                    raise
                self.owner.discard(response, context)
                response = failure(
                    response["id"], "output_limit", "whole snapshot response exceeds frame bound", response["usage"]
                )
                message["snapshot"] = {"schema": HOST_SCHEMA, "response": response}
                self.write(message)
        except BaseException:
            self.owner.discard(response, context)
            raise

    def serve(
        self,
        frame_id: int,
        request: dict[str, Any],
        context: CallContext,
        token: Any,
        tool_registry: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self.owner.call(request, context, token, tool_registry)

    def done(self, frame_id: int, lane: str, future: Future[dict[str, Any]]) -> None:
        try:
            try:
                response = future.result()
            except (asyncio.CancelledError, CancelledError):
                with self.guard:
                    request_id = self.pending[frame_id].request_id
                response = failure(request_id, "cancelled", "snapshot preparation was cancelled")
            with self.guard:
                context = self.pending[frame_id].context
            self.result(frame_id, response, context)
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            try:
                self.write({"protocol": 1, "op": "error", "id": frame_id, "error": str(exc)[:4096]})
            except Exception as transport_error:
                self.failed = transport_error
        finally:
            with self.guard:
                self.pending.pop(frame_id, None)
            self.slots[lane].release()

    def submit(self, frame: dict[str, Any]) -> None:
        if (
            type(frame.get("protocol")) is not int
            or frame["protocol"] != 1
            or type(frame.get("id")) is not int
            or frame["id"] <= 0
        ):
            raise SnapshotProtocolError("invalid snapshot owner frame identity")
        frame_id = frame["id"]
        if frame.get("op") == "snapshot_cancel":
            if set(frame) != {"protocol", "op", "id"}:
                raise SnapshotProtocolError("invalid snapshot cancellation fields")
            with self.guard:
                if pending := self.pending.get(frame_id):
                    pending.token.cancel()
            return
        if frame.get("op") != "snapshot_request" or set(frame) != {
            "protocol",
            "op",
            "id",
            "snapshot",
            "snapshot_context",
        }:
            raise SnapshotProtocolError("invalid snapshot request frame fields")
        wrapper = checked("host-request", frame["snapshot"])
        request = wrapper["request"]
        context = checked("context", frame["snapshot_context"])
        lane = (
            "release"
            if request["operation"] == "release"
            else "review"
            if request["operation"] in {"prepare_review", "prepare_corrections"}
            else context["admission"]
        )
        with self.guard:
            if frame_id in self.pending:
                raise SnapshotProtocolError("duplicate live snapshot frame identity")
            if not self.slots[lane].acquire(blocking=False):
                self.result(
                    frame_id,
                    failure(request["id"], "retained_limit", "snapshot owner admission slots are occupied"),
                    context,
                )
                return
            token = self.owner.token_type()
            future = self.executors[lane].submit(
                self.serve, frame_id, request, context, token, wrapper["tool_registry"]
            )
            self.pending[frame_id] = Pending(request["id"], context, token, future)
            future.add_done_callback(lambda done: self.done(frame_id, lane, done))

    def run(self) -> None:
        try:
            while (frame := read_frame(self.input, self.owner.record_transport)) is not None:
                if self.failed is not None:
                    raise self.failed
                self.submit(frame)
        finally:
            with self.guard:
                for pending in self.pending.values():
                    pending.token.cancel()
            for name in ("hook", "release"):
                self.executors[name].shutdown(wait=True)
            self.executors["review"].submit(self.owner.close).result()
            self.executors["review"].shutdown(wait=True)


def handshake(input_stream: BinaryIO, output_stream: BinaryIO, *, build: str) -> dict[str, Any] | None:
    first = read_frame(input_stream)
    if first is None:
        return None
    if (
        set(first) != {"protocol", "op", "build", "snapshot_config"}
        or type(first["protocol"]) is not int
        or first["protocol"] != 1
        or first["op"] != "hello"
        or first["build"] != build
    ):
        raise SnapshotProtocolError("snapshot owner requires exact build handshake")
    config = checked("config", first["snapshot_config"])
    output_stream.write(encode_frame({"protocol": 1, "op": "hello", "build": build}))
    output_stream.flush()
    return config


def main() -> None:
    output = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    try:
        config = handshake(sys.stdin.buffer, output, build=importlib.metadata.version("capt-hook"))
        if config is not None:
            os.environ.setdefault("CC_TRANSCRIPT_PARSE_THREADS", "4")
            OwnerService(sys.stdin.buffer, output, Owner(config)).run()
    finally:
        output.close()


if __name__ == "__main__":
    main()
