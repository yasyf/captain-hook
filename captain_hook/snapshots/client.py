from __future__ import annotations

import contextvars
import hashlib
import importlib.metadata
import json
import os
import re
import struct
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field, replace
from functools import lru_cache
from itertools import accumulate, islice
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

from capt_hook_client.client import HOST

if TYPE_CHECKING:
    from cc_transcript.activity import ToolUse
    from cc_transcript.ids import ToolUseId
    from cc_transcript.query import PredicateInputs
    from cc_transcript.render import Budget

CORE_SCHEMA = "cc-transcript.snapshot/1"
HOST_SCHEMA = "captain.transcript/1"
MAX_FRAME_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 16 * 1024 * 1024
CLEANUP_SECONDS = 5
CURSOR_CONSUMING_OPERATIONS = frozenset({"resume", "submit_classifier"})
GRAPH_WORK_SECONDS = 0.75
GATE_EVENTS = frozenset({"Stop", "SubagentStop", "UserPromptSubmit"})
GATE_WORK_SECONDS = 5.0
MANDATORY_WORK_SECONDS = 15.0
HOOK_TAIL_BYTES = 4 * 1024 * 1024
GRAPH_DISCOVERY_ENTRIES = 50_000
GRAPH_SOURCE_LIMIT = 4096
DEFAULT_LIMITS = {
    "max_read_bytes": 512 * 1024 * 1024,
    "max_source_read_bytes": 512 * 1024 * 1024,
    "max_events": 1_000_000,
    "max_items": 65_536,
    "max_output_bytes": MAX_RESULT_BYTES,
    "max_discovery_entries": 1_000_000,
    "max_sources": 65_536,
}
NATIVE_CLASSIFIER = {"id": "native", "version": "1"}
SOURCE_FIELDS = ("path", "direct_paths", "roots", "session_ids", "thread_ids")
HANDLE_FIELDS = ("handles", "handle", "view", "cursor", "token")
WARM_STEP_BYTES = 8 * 1024 * 1024
WARM_STEP_SECONDS = 3
WARM_INTERVAL_SECONDS = 2
WARM_IO_COUNTERS = ("source_bytes_read", "prepared_cache_bytes_read")
OBSERVABLE_KINDS = frozenset(
    {
        "has_tool",
        "has_read",
        "has_command",
        "has_skill",
        "has_skill_suffix",
        "has_override",
        "has_error",
        "has_edit",
    }
)
OBSERVER_LIMIT = 32
OBSERVER_BYTES = 16 * 1024
OBSERVER_PLAN: contextvars.ContextVar[ObserverPlan | None] = contextvars.ContextVar("observer_plan", default=None)


def monotonic_deadline(deadline_unix_ms: int) -> float:
    return time.monotonic() + deadline_unix_ms / 1000 - time.time()


def warm_interval(usage: Mapping[str, int]) -> float:
    return WARM_INTERVAL_SECONDS if any(usage.get(name, 0) for name in WARM_IO_COUNTERS) else 0


def summed_usage(first: Mapping[str, int], second: Mapping[str, int]) -> dict[str, int]:
    return dict(first) | {name: first.get(name, 0) + count for name, count in second.items()}


def with_observed_usage(result: dict[str, Any], observed: Mapping[str, int]) -> dict[str, Any]:
    return result | {"usage": summed_usage(result["usage"], observed)} if observed else result


def foreground_seconds(event: str) -> float:
    """The seconds one dispatch may spend on evidence; turn-level gates fire rarely and get more."""
    return GATE_WORK_SECONDS if event in GATE_EVENTS else GRAPH_WORK_SECONDS


SCALAR_QUERY_KINDS = frozenset(
    {
        "user_text",
        "first_prompt",
        "event_count",
        "turn_count",
        "tool_count",
        "message_count",
        "unresolved_tools",
        "failures",
        "workflow_text",
        "pending_named_task",
    }
)
SCALAR_MISS = object()
type ScalarKey = tuple[
    str, tuple[tuple[str, str], ...], SnapshotClient, tuple[PreparedGraphEvidence, int] | None, str, str
]
type ScalarValue = bool | int | str | None


def graph_deadline(client: SnapshotClient) -> int:
    from captain_hook.util import reqenv

    deadline = int((time.time() + GRAPH_WORK_SECONDS) * 1000)
    if client.foreground_deadline_unix_ms is not None:
        deadline = min(deadline, client.foreground_deadline_unix_ms)
    if (request := reqenv.current()) is not None and request.deadline_unix_ms:
        deadline = min(deadline, request.deadline_unix_ms - 1000)
    if deadline <= int(time.time() * 1000):
        raise EvidenceIncomplete("deadline", "no time remains for prepared graph evidence")
    return deadline


def graph_limits() -> dict[str, int]:
    limits = DEFAULT_LIMITS.copy()
    limits["max_discovery_entries"] = min(limits["max_discovery_entries"], GRAPH_DISCOVERY_ENTRIES)
    limits["max_sources"] = min(limits["max_sources"], GRAPH_SOURCE_LIMIT)
    return limits


def output_limit_kind(reason: object) -> str | None:
    if not isinstance(reason, str):
        return None
    return next(
        (
            label
            for prefix, label in (
                ("projection exceeds serialized output limit", "serialized_projection"),
                ("activity window of ", "activity_window"),
                ("encoded reply exceeds page bound", "encoded_page"),
                ("response data exceeds output budget", "response_data"),
                ("reservation output budget exhausted", "output_reservation"),
                ("graph record exceeds remaining output budget", "graph_record"),
                ("projection exceeds request output budget", "request_projection"),
                ("snapshot record needs ", "snapshot_record"),
                ("snapshot page needs ", "snapshot_page"),
                ("snapshot page of ", "snapshot_page"),
            )
            if reason.startswith(prefix)
        ),
        None,
    )


def request_metadata(request: Mapping[str, object]) -> dict[str, object]:
    sources = {key: value for key in SOURCE_FIELDS if (value := request.get(key))}
    resource = (
        {
            "resource_kind": "+".join(sources),
            "resource_count": sum(len(value) if isinstance(value, list) else 1 for value in sources.values()),
            "resource_sha256": hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()[:16],
        }
        if sources
        else {
            "resource_kind": next((key for key in HANDLE_FIELDS if request.get(key)), None),
            "resource_count": None,
            "resource_sha256": None,
        }
    )
    return {
        "operation": request.get("operation"),
        "request_id": request.get("id"),
        "deadline_unix_ms": request.get("deadline_unix_ms"),
    } | resource


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def tool_predicate(kind: Literal["has_tool", "has_read"], pattern: str, *, subagents: bool) -> dict[str, object]:
    return {"kind": kind, "pattern": pattern, "subagents": subagents}


def list_predicate(
    kind: Literal["has_command", "has_edit_to", "has_skill", "has_skill_suffix", "has_read_glob"],
    values: Sequence[str],
    *,
    subagents: bool,
) -> dict[str, object]:
    return {"kind": kind, "values": list(values), "subagents": subagents}


def override_predicate(token: str, invalidated_by: Sequence[str], *, subagents: bool) -> dict[str, object]:
    return {"kind": "has_override", "token": token, "invalidated_by": list(invalidated_by), "subagents": subagents}


@lru_cache(maxsize=1024)
def observable(query: str) -> bool:
    from captain_hook.snapshots.validation import observer_validator

    return (predicate := json.loads(query))["kind"] in OBSERVABLE_KINDS and observer_validator().is_valid(predicate)


class EvidenceIncomplete(RuntimeError):
    def __init__(self, status: str, reason: str, *, usage: Mapping[str, int] | None = None) -> None:
        super().__init__(f"transcript evidence {status}: {reason}")
        self.status = status
        self.reason = reason
        self.usage = dict(usage or {})


class SnapshotProtocolError(EvidenceIncomplete):
    def __init__(self, reason: str) -> None:
        super().__init__("invalid_request", reason)


class GraphEvidenceExpired(EvidenceIncomplete):
    pass


FAIL_OPEN_EVIDENCE_STATUSES = frozenset(
    {
        "incomplete",
        "source_limit",
        "entry_limit",
        "output_limit",
        "deadline",
        "cancelled",
        "changed",
        "missing",
        "retained_limit",
        "lease_limit",
        "stale_handle",
        "stale_cursor",
    }
)


def fails_open(exc: EvidenceIncomplete) -> bool:
    return exc.status in FAIL_OPEN_EVIDENCE_STATUSES


def encode_frame(message: Mapping[str, object]) -> bytes:
    raw = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    for part in encoder.iterencode(message):
        if len(part) > MAX_FRAME_BYTES - len(raw):
            raise EvidenceIncomplete("output_limit", "snapshot frame exceeds encoded byte bound")
        chunk = part.encode()
        if len(chunk) > MAX_FRAME_BYTES - len(raw):
            raise EvidenceIncomplete("output_limit", "snapshot frame exceeds encoded byte bound")
        raw.extend(chunk)
    return struct.pack(">I", len(raw)) + raw


def read_exact(stream: Any, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        block = stream.read(count - len(chunks))
        if not block:
            raise SnapshotProtocolError("snapshot connection closed during frame")
        chunks.extend(block)
    return bytes(chunks)


def read_frame(stream: Any) -> dict[str, Any]:
    (size,) = struct.unpack(">I", read_exact(stream, 4))
    if size > MAX_FRAME_BYTES:
        raise SnapshotProtocolError("snapshot frame exceeds encoded byte bound")
    from captain_hook.snapshots.validation import parse_exact

    value = parse_exact(read_exact(stream, size))
    if not isinstance(value, dict):
        raise SnapshotProtocolError("snapshot frame must be an object")
    return value


class Bridge:
    def __init__(self, command: Sequence[str] | None = None, *, event: int = 0) -> None:
        if command is None and os.environ.get("CAPT_HOOK_TEST_NO_LIVE") == "1":
            raise SnapshotProtocolError("tests must supply an isolated snapshot transport")
        self._command = tuple(command or (HOST, "transcript-client"))
        self._event = event
        self._process: subprocess.Popen[bytes] | None = None
        self._guard = threading.Lock()
        self._id = 0

    def __call__(self, request: dict[str, object]) -> dict[str, Any]:
        with self._guard:
            if self._process is None:
                self._process = subprocess.Popen(self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                if self._process.stdin is None or self._process.stdout is None:
                    raise SnapshotProtocolError("snapshot bridge did not create pipes")
                hello = {"protocol": 1, "op": "hello", "build": importlib.metadata.version("capt-hook")}
                self._process.stdin.write(encode_frame(hello))
                self._process.stdin.flush()
                if read_frame(self._process.stdout) != hello:
                    raise SnapshotProtocolError("snapshot bridge rejected exact build handshake")
            self._id += 1
            frame = {"protocol": 1, "op": "snapshot_request", "id": self._id, "snapshot": request}
            if self._event:
                frame["parent_id"] = self._event
            process = self._process
            if process.stdin is None or process.stdout is None:
                raise SnapshotProtocolError("snapshot bridge did not create pipes")
            process.stdin.write(encode_frame(frame))
            process.stdin.flush()
            response = read_frame(process.stdout)
            if response.get("op") == "error" and response.get("id") == self._id:
                nested = request.get("request")
                deadline = nested.get("deadline_unix_ms") if isinstance(nested, dict) else None
                if type(deadline) is int and time.time() * 1000 >= deadline:
                    raise EvidenceIncomplete("deadline", "snapshot transport deadline elapsed")
                raise EvidenceIncomplete("cancelled", f"snapshot host failed the request: {response.get('error')}")
            if (
                type(response.get("protocol")) is not int
                or type(response.get("id")) is not int
                or response.get("protocol") != 1
                or response.get("op") != "snapshot_result"
                or response.get("id") != self._id
            ):
                raise SnapshotProtocolError("snapshot bridge response does not match request")
            result = response.get("snapshot")
            if not isinstance(result, dict):
                raise SnapshotProtocolError("snapshot result must be an object")
            return result

    def close(self) -> None:
        with self._guard:
            if self._process is not None:
                if self._process.stdin is not None:
                    self._process.stdin.close()
                self._process.wait(timeout=5)
                if self._process.stdout is not None:
                    self._process.stdout.close()
                self._process = None


class SnapshotClient:
    def __init__(
        self,
        exchange: Callable[[dict[str, object]], dict[str, Any]],
        *,
        cleanup_exchange: Callable[[dict[str, object]], dict[str, Any]] | None = None,
        preparation_seconds: float = 120,
        root_warm_scheduler: Callable[[SnapshotClient, Path, Mapping[str, str], int | None], None] | None = None,
        defer_cleanup: bool = False,
        foreground_seconds: float | None = None,
    ) -> None:
        self._exchange = exchange
        self._cleanup_exchange = cleanup_exchange or exchange
        self._leases: set[Lease] = set()
        self._graphs: set[PreparedGraphEvidence] = set()
        self._preparation_seconds = preparation_seconds
        self._root_warm_scheduler = root_warm_scheduler
        self._defer_cleanup = defer_cleanup
        self._deferred_cursors: set[tuple[str, str | None]] = set()
        self._foreground_seconds = foreground_seconds
        self.foreground_deadline_unix_ms: int | None = None
        self._prefix = uuid.uuid4().hex
        self._counter = 0
        self._guard = threading.Lock()
        self._tool_registry: tuple[tuple[str, str, tuple[tuple[str, str], ...] | None], ...] | None = None

    def bind_tool_registry(self, tools: Mapping[str, tuple[str, Mapping[str, str] | None]]) -> None:
        if len(tools) > 256:
            raise EvidenceIncomplete("source_limit", "tool registry exceeds its entry bound")
        for name, (kind, fields) in tools.items():
            if not 0 < len(name) <= 256 or not 0 < len(kind) <= 256:
                raise EvidenceIncomplete("entry_limit", "tool registry name exceeds its field bound")
            if fields is not None and (
                len(fields) > 3 or any(len(key) > 256 or len(value) > 256 for key, value in fields.items())
            ):
                raise EvidenceIncomplete("entry_limit", "tool registry span mapping exceeds its field bound")
        frozen = tuple(
            sorted(
                (name, kind, tuple(sorted(fields.items())) if fields is not None else None)
                for name, (kind, fields) in tools.items()
            )
        )
        with self._guard:
            if self._tool_registry is not None and self._tool_registry != frozen:
                raise EvidenceIncomplete("changed", "tool registry changed during one evidence preparation")
            self._tool_registry = frozen

    def tool_registry(self) -> list[dict[str, object]]:
        if self._tool_registry is None:
            from captain_hook import cli

            with cli._registry_lock:
                self.bind_tool_registry(cli._registered_tools)
        if self._tool_registry is None:
            raise SnapshotProtocolError("tool registry preparation did not initialize its scope")
        return [
            {
                "name": name,
                "behaves_like": kind,
                "span_edit": (dict(fields) | {"delete": dict(fields).get("delete")}) if fields is not None else None,
            }
            for name, kind, fields in self._tool_registry
        ]

    def root_warm_demand(self, path: Path, classifier: Mapping[str, str], tail_bytes: int | None) -> dict[str, object]:
        return {
            "schema": HOST_SCHEMA,
            "request": {
                "schema": CORE_SCHEMA,
                "operation": "warm_root",
                "path": str(path),
                "classifier": dict(classifier),
                "tail_bytes": tail_bytes,
                "limits": graph_limits(),
            },
            "tool_registry": self.tool_registry(),
        }

    def schedule_root_warm(self, path: str | Path, classifier: Mapping[str, str], tail_bytes: int | None) -> None:
        if classifier["id"] != "captain-configured" and self._root_warm_scheduler is not None:
            self._root_warm_scheduler(self, Path(path).absolute(), classifier, tail_bytes)

    @contextmanager
    def foreground_at_least(self, seconds: float) -> Iterator[None]:
        if self._foreground_seconds is None:
            yield
            return
        with self._guard:
            previous = self.foreground_deadline_unix_ms
            self.foreground_deadline_unix_ms = max(previous or 0, int((time.time() + seconds) * 1000))
        try:
            yield
        finally:
            with self._guard:
                self.foreground_deadline_unix_ms = previous

    def call(self, operation: str, *, domain: bool = False, **arguments: object) -> dict[str, Any]:
        observed: dict[str, int] = {}
        try:
            return self._call(operation, domain, arguments, observed)
        except EvidenceIncomplete as exc:
            exc.usage = summed_usage(exc.usage, observed)
            raise

    def _call(
        self, operation: str, domain: bool, arguments: dict[str, object], observed: dict[str, int]
    ) -> dict[str, Any]:
        from jsonschema import ValidationError

        started = time.monotonic()
        retry_until = started + CLEANUP_SECONDS if operation == "release" else None
        admission_deadline_unix_ms = int((time.time() + self._preparation_seconds) * 1000)
        retry_delay = 0.01
        while True:
            with self._guard:
                self._counter += 1
                request_id = f"{self._prefix}-{self._counter}"
                if self._foreground_seconds is not None and self.foreground_deadline_unix_ms is None:
                    self.foreground_deadline_unix_ms = int((time.time() + self._foreground_seconds) * 1000)
            if (
                self.foreground_deadline_unix_ms is not None
                and operation != "release"
                and int(time.time() * 1000) >= self.foreground_deadline_unix_ms
            ):
                logger.bind(
                    **request_metadata({"operation": operation, "id": request_id} | arguments),
                    status="deadline",
                    foreground_deadline_unix_ms=self.foreground_deadline_unix_ms,
                    foreground_seconds=self._foreground_seconds,
                    elapsed_ms=round((time.monotonic() - started) * 1000, 1),
                ).info("foreground transcript deadline exhausted")
                raise EvidenceIncomplete("deadline", "foreground transcript deadline exhausted")
            request: dict[str, object] = {
                "schema": HOST_SCHEMA if domain else CORE_SCHEMA,
                "id": request_id,
                "operation": operation,
                **arguments,
            }
            if operation not in {"resume", "release", "retain", "renew", "describe", "stats", "submit_classifier"}:
                request.setdefault("deadline_unix_ms", admission_deadline_unix_ms)
                request.setdefault("limits", DEFAULT_LIMITS.copy())
            if self.foreground_deadline_unix_ms is not None and "deadline_unix_ms" in request:
                request["deadline_unix_ms"] = min(int(request["deadline_unix_ms"]), self.foreground_deadline_unix_ms)
            exchange = self._cleanup_exchange if operation == "release" else self._exchange
            try:
                response = exchange({"schema": HOST_SCHEMA, "request": request, "tool_registry": self.tool_registry()})
            except (OSError, ValueError, ValidationError) as exc:
                raise SnapshotProtocolError(str(exc)) from exc
            if response.get("schema") != HOST_SCHEMA or not isinstance(response.get("response"), dict):
                raise SnapshotProtocolError("snapshot response has invalid transport envelope")
            result = response["response"]
            if result.get("schema") != CORE_SCHEMA or result.get("id") != request_id:
                raise SnapshotProtocolError("snapshot response schema or id mismatch")
            if (
                result.get("status") == "stale_handle"
                and operation != "release"
                and self.foreground_deadline_unix_ms is not None
                and int(time.time() * 1000) >= self.foreground_deadline_unix_ms
            ):
                observed |= summed_usage(observed, result["usage"])
                raise EvidenceIncomplete("deadline", "lease expired at the foreground transcript deadline")
            if result.get("status") != "retained_limit" or operation in CURSOR_CONSUMING_OPERATIONS:
                return with_observed_usage(result, observed)
            if retry_until is None:
                horizon = int(request.get("deadline_unix_ms", admission_deadline_unix_ms))
                if self.foreground_deadline_unix_ms is not None:
                    horizon = min(horizon, self.foreground_deadline_unix_ms)
                retry_until = monotonic_deadline(horizon)
            if (remaining := retry_until - time.monotonic()) <= 0:
                return with_observed_usage(result, observed)
            observed |= summed_usage(observed, result["usage"])
            time.sleep(min(retry_delay, remaining))
            retry_delay = min(retry_delay * 2, 0.1)

    def pages(self, operation: str, *, domain: bool = False, **arguments: object) -> Iterator[dict[str, Any]]:
        result = self.call(operation, domain=domain, **arguments)
        output_bytes = 0
        cursor = result.get("cursor")
        try:
            while True:
                status = result.get("status")
                if status not in {"ok", "incomplete"}:
                    logger.bind(
                        **request_metadata({"operation": operation, "id": result.get("id")} | arguments),
                        status=status,
                        output_limit_kind=output_limit_kind(result.get("reason")) if status == "output_limit" else None,
                        projection_kind=(
                            next((kind for kind in ("turns", "events") if query.get("kind") == kind), None)
                            if isinstance(query := arguments.get("query"), dict)
                            else None
                        ),
                        foreground_deadline_unix_ms=self.foreground_deadline_unix_ms,
                        output_bytes=output_bytes,
                        usage=result.get("usage"),
                    ).info("snapshot request failed")
                    raise EvidenceIncomplete(str(status), str(result.get("reason")))
                if status == "ok":
                    if result.get("complete") is not True or cursor is not None:
                        raise SnapshotProtocolError("complete snapshot result has invalid continuation")
                elif not isinstance(cursor, str) or not cursor:
                    raise EvidenceIncomplete("incomplete", str(result.get("reason")))
                if (data := result.get("data")) is not None:
                    if not isinstance(data, dict):
                        raise SnapshotProtocolError("snapshot data must be an object")
                    output_bytes += len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())
                    if output_bytes > MAX_RESULT_BYTES:
                        raise EvidenceIncomplete("output_limit", "snapshot result exceeds cumulative projection bound")
                    yield data
                if status == "ok":
                    return
                operation, arguments = "resume", {"cursor": cursor}
                result = self.call("resume", cursor=cursor)
                cursor = result.get("cursor")
        finally:
            if cursor is not None:
                if self._defer_cleanup:
                    self._deferred_cursors.add((cursor, None))
                else:
                    self.call("release", kind="cursor", token=cursor)

    def acquire(
        self, path: str | Path, *, classifier: Mapping[str, str] = NATIVE_CLASSIFIER, tail_bytes: int | None = None
    ) -> RemoteSession:
        description = self.acquired(path, classifier=classifier, tail_bytes=tail_bytes)
        return RemoteSession(
            self,
            Lease(self, description, tail_bytes=tail_bytes),
            Path(description["canonical_path"]),
            description["classifier"],
            tail_bytes=tail_bytes,
        )

    def acquired(self, path: str | Path, *, classifier: Mapping[str, str], tail_bytes: int | None) -> dict[str, Any]:
        description = None
        for data in self.pages(
            "acquire", path=str(Path(path).absolute()), classifier=dict(classifier), tail_bytes=tail_bytes
        ):
            if data.get("kind") == "acquired":
                description = data["description"]
        if not isinstance(description, dict):
            raise SnapshotProtocolError("acquire completed without a snapshot description")
        return description

    def classify(
        self, session: RemoteSession, predicate: Callable[[Any], bool], policy: Mapping[str, str]
    ) -> RemoteSession:
        from cc_transcript.models import UserEvent
        from cc_transcript.snapshots import SnapshotIncomplete, decode_projection

        result = self.call("prepare_classifier", domain=True, handle=session.lease.require(), classifier=dict(policy))
        cursor = result.get("cursor")
        try:
            while not result["complete"]:
                if result["status"] != "incomplete" or not result["cursor"] or not result["data"]:
                    raise EvidenceIncomplete(result["status"], result["reason"])
                data = result["data"]
                if data["kind"] != "classification":
                    raise SnapshotProtocolError("classifier preparation returned an unexpected projection")
                try:
                    events = decode_projection(
                        data["record_schema"], data["records_json"], tool_registry=self.tool_registry()
                    )
                except SnapshotIncomplete as exc:
                    raise EvidenceIncomplete(exc.status, exc.reason) from exc
                labels = []
                for event in events:
                    if not isinstance(event, UserEvent):
                        raise SnapshotProtocolError("classifier page contains a non-user event")
                    labels.append(bool(predicate(event)))
                result = self.call("submit_classifier", domain=True, cursor=result["cursor"], labels=labels)
                cursor = result.get("cursor")
        finally:
            if cursor is not None:
                owner_epoch = session.lease.handle["owner_epoch"]
                if self._defer_cleanup:
                    self._deferred_cursors.add((cursor, owner_epoch))
                else:
                    self.call("release", owner_epoch=owner_epoch, kind="cursor", token=cursor)
        data = result["data"]
        if result["status"] != "ok" or data["kind"] != "acquired":
            raise SnapshotProtocolError("classifier completed without a leased description")
        description = data["description"]
        classified = RemoteSession(
            self,
            Lease(self, description, tail_bytes=session.tail_bytes),
            session.path,
            description["classifier"],
            tail_bytes=session.tail_bytes,
        )
        session.release()
        return classified

    def close_pending(self) -> None:
        self._defer_cleanup = False
        errors = []
        for cursor, owner_epoch in tuple(self._deferred_cursors):
            try:
                arguments = {"kind": "cursor", "token": cursor}
                if owner_epoch is not None:
                    arguments["owner_epoch"] = owner_epoch
                result = self.call("release", **arguments)
                if result.get("status") not in {"ok", "stale_handle"}:
                    raise EvidenceIncomplete(str(result.get("status")), str(result.get("reason")))
                self._deferred_cursors.discard((cursor, owner_epoch))
            except Exception as exc:
                errors.append(exc)
        for graph in tuple(self._graphs):
            try:
                graph.retry_release()
            except Exception as exc:
                errors.append(exc)
        for lease in tuple(self._leases):
            if not lease.cleanup_pending:
                continue
            try:
                lease.release()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("snapshot cleanup failed", errors)

    def close(self) -> None:
        self.close_pending()
        errors = []
        for lease in tuple(self._leases):
            try:
                lease.release()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("snapshot lease cleanup failed", errors)


class Lease:
    def __init__(
        self, client: SnapshotClient, description: Mapping[str, Any], *, tail_bytes: int | None = None
    ) -> None:
        self.client = client
        client._leases.add(self)
        self.description = dict(description)
        self.handle = dict(description["handle"])
        self.expires_unix_ms = description["lease_expires_unix_ms"]
        self.tail_bytes = tail_bytes
        self.released = False
        self.closed = False
        self.cleanup_pending = False
        self.graph_released = False
        self.guard = threading.Lock()
        self.subagent_guard = threading.Lock()
        self.subagent_views: dict[str, RemoteSubagentIndex] = {}
        self.scalar_results: dict[ScalarKey, Any] = {}

    def renewable_by(self, client: SnapshotClient) -> bool:
        return (deadline := client.foreground_deadline_unix_ms) is None or self.expires_unix_ms < deadline

    def require(self, client: SnapshotClient | None = None) -> dict[str, str]:
        client = client or self.client
        with self.guard:
            if self.closed or self.released:
                raise EvidenceIncomplete("stale_handle", "preparation lease was already released")
            if self.renewable_by(client) and self.expires_unix_ms <= time.time() * 1000 + 1000:
                result = client.call("renew", handle=self.handle)
                if result["status"] == "stale_handle":
                    self._reacquire(client)
                elif result["status"] != "ok":
                    raise EvidenceIncomplete(result["status"], result["reason"])
                else:
                    self.expires_unix_ms = result["data"]["expires_unix_ms"]
            return self.handle.copy()

    def reclaim(self, client: SnapshotClient, stale: Mapping[str, str]) -> dict[str, str]:
        with self.guard:
            if self.closed or self.released:
                raise EvidenceIncomplete("stale_handle", "preparation lease was already released")
            if self.handle == stale:
                self._reacquire(client)
            return self.handle.copy()

    def _reacquire(self, client: SnapshotClient) -> None:
        """Replace a lease the owner no longer holds with a fresh one on the same transcript, under *client*'s window.

        A lease is capped at the foreground deadline it was acquired under, so once a hook widens that window
        (:func:`foreground_evidence`) and retries, the owner has already dropped the lease and answers its renewal
        or retain with ``stale_handle``.
        """
        description = client.acquired(
            self.description["canonical_path"], classifier=self.description["classifier"], tail_bytes=self.tail_bytes
        )
        if (
            description["source_id"] != self.description["source_id"]
            or description["committed_bytes"] < self.description["committed_bytes"]
        ):
            Lease(client, description).release()
            raise EvidenceIncomplete("changed", "transcript was rewritten while its lease was expired")
        self.client._leases.discard(self)
        client._leases.add(self)
        self.client = client
        self.description = dict(description)
        self.handle = dict(description["handle"])
        self.expires_unix_ms = description["lease_expires_unix_ms"]
        self.scalar_results.clear()

    def release(self) -> None:
        with self.subagent_guard:
            try:
                with ExitStack() as cleanup:
                    cleanup.callback(self._release)
                    for index in self.subagent_views.values():
                        for item in index:
                            cleanup.callback(item.session.release)
            finally:
                self.subagent_views.clear()
                self.scalar_results.clear()

    def _release(self) -> None:
        with self.guard:
            if self.released:
                return
            self.closed = True
            if self.client._defer_cleanup:
                self.cleanup_pending = True
                return
            result = self.client.call(
                "release", owner_epoch=self.handle["owner_epoch"], kind="lease", token=self.handle["lease_id"]
            )
            if result.get("status") == "retained_limit":
                logger.bind(status="retained_limit", reason=result.get("reason")).warning(
                    "snapshot lease cleanup deferred"
                )
                self.cleanup_pending = True
                return
            if result.get("status") not in {"ok", "stale_handle"}:
                raise EvidenceIncomplete(str(result.get("status")), str(result.get("reason")))
            self.released = True
            self.cleanup_pending = False
            self.client._leases.discard(self)


@dataclass(frozen=True)
class GraphSources:
    thread_ids: tuple[str, ...] = ()
    roots: tuple[Path, ...] = ()
    direct_paths: tuple[Path, ...] = ()
    active_since_unix_ms: int | None = None

    def wire(self) -> dict[str, object]:
        return {
            "thread_ids": list(self.thread_ids),
            "roots": [str(root) for root in self.roots],
            "direct_paths": [str(path) for path in self.direct_paths],
            "active_since_unix_ms": self.active_since_unix_ms,
        }


@dataclass
class RegisteredWarmState:
    sources: GraphSources
    client: SnapshotClient
    start_index: int = 0
    owner_epoch: str | None = None
    membership_revision: str | None = None
    fact_cache_bytes: int = 0
    fact_cache_write_bytes: int = 0
    fact_cache_writes: int = 0
    source_offset: int = 0
    source_size: int = 0
    stalled_steps: int = 0
    last_usage: dict[str, int] = field(default_factory=dict)

    def step(self, *, read_bytes: int, deadline_seconds: float) -> bool:
        limits = graph_limits() | {"max_source_read_bytes": read_bytes}
        self.last_usage = {}
        result = self.client.call(
            "warm_registered",
            classifier=NATIVE_CLASSIFIER,
            **self.sources.wire(),
            start_index=self.start_index,
            membership_revision=self.membership_revision,
            deadline_unix_ms=int((time.time() + deadline_seconds) * 1000),
            limits=limits,
        )
        usage = result["usage"]
        self.last_usage = dict(usage)
        if result["status"] == "changed":
            self.start_index = 0
            self.membership_revision = None
            self.source_offset = 0
            self.source_size = 0
            self.stalled_steps = 0
            return False
        if result["status"] in {"incomplete", "deadline", "retained_limit", "lease_limit", "cancelled"}:
            self.stalled_steps += 1
            if self.stalled_steps >= 2:
                raise EvidenceIncomplete("incomplete", "registered transcript warming made no progress")
            return False
        if result["status"] != "ok":
            raise EvidenceIncomplete(str(result["status"]), str(result.get("reason")))
        data = result["data"]
        if not isinstance(data, dict) or data.get("kind") != "warmed_registry":
            raise SnapshotProtocolError("registered transcript warming returned invalid progress")
        changed = (self.owner_epoch is not None and data["owner_epoch"] != self.owner_epoch) or (
            self.membership_revision is not None and data["membership_revision"] != self.membership_revision
        )
        progress = (
            changed
            or data["next_index"] > self.start_index
            or data["source_offset"] > self.source_offset
            or usage.get("source_bytes_read", 0) > 0
            or usage.get("events_parsed", 0) > 0
        )
        self.stalled_steps = 0 if progress else self.stalled_steps + 1
        self.start_index = 0 if changed else data["next_index"]
        self.owner_epoch = data["owner_epoch"]
        self.membership_revision = data["membership_revision"]
        self.fact_cache_bytes = data["fact_cache_bytes"]
        self.fact_cache_write_bytes = data["fact_cache_write_bytes"]
        self.fact_cache_writes = data["fact_cache_writes"]
        self.source_offset = 0 if changed else data["source_offset"]
        self.source_size = 0 if changed else data["source_size"]
        if self.stalled_steps >= 2 and not data["complete"]:
            raise EvidenceIncomplete("incomplete", "registered transcript warming made no progress")
        return data["complete"] and not changed


@dataclass
class RootWarmState:
    path: Path
    classifier: Mapping[str, str]
    client: SnapshotClient
    tail_bytes: int | None = None
    owner_epoch: str | None = None
    source_revision: str | None = None
    source_offset: int = 0
    source_size: int = 0
    facts_complete: bool = False
    stalled_steps: int = 0
    last_usage: dict[str, int] = field(default_factory=dict)

    def step(self, *, read_bytes: int, deadline_seconds: float) -> bool:
        self.last_usage = {}
        result = self.client.call(
            "warm_root",
            path=str(self.path),
            classifier=dict(self.classifier),
            tail_bytes=self.tail_bytes,
            deadline_unix_ms=int((time.time() + deadline_seconds) * 1000),
            limits=graph_limits() | {"max_source_read_bytes": read_bytes},
        )
        self.last_usage = dict(result["usage"])
        if result["status"] in {"incomplete", "deadline", "retained_limit", "lease_limit", "cancelled"}:
            self.stalled_steps += 1
            if self.stalled_steps >= 2:
                raise EvidenceIncomplete("incomplete", "root transcript warming made no progress")
            return False
        if result["status"] != "ok":
            raise EvidenceIncomplete(str(result["status"]), str(result.get("reason")))
        data = result["data"]
        if not isinstance(data, dict) or data.get("kind") != "warmed_root":
            raise SnapshotProtocolError("root transcript warming returned invalid progress")
        changed = (self.owner_epoch is not None and data["owner_epoch"] != self.owner_epoch) or (
            self.source_revision is not None and data["source_revision"] != self.source_revision
        )
        progress = (
            changed
            or data["source_offset"] > self.source_offset
            or (data["facts_complete"] and not self.facts_complete)
            or self.last_usage.get("source_bytes_read", 0) > 0
            or self.last_usage.get("events_parsed", 0) > 0
        )
        self.stalled_steps = 0 if progress else self.stalled_steps + 1
        self.owner_epoch = data["owner_epoch"]
        self.source_revision = data["source_revision"]
        self.source_offset = data["source_offset"]
        self.source_size = data["source_size"]
        self.facts_complete = data["facts_complete"]
        complete = data["complete"] and data["facts_complete"]
        if self.stalled_steps >= 2 and not complete:
            raise EvidenceIncomplete("incomplete", "root transcript warming made no progress")
        return complete


@dataclass(frozen=True, slots=True)
class PreparedGraphHandle:
    graph_id: str
    owner_epoch: str
    revision: str
    complete: Literal[True] = True

    @classmethod
    def from_wire(cls, value: Mapping[str, object]) -> PreparedGraphHandle:
        if value.get("complete") is not True or not all(
            isinstance(value.get(name), str) and value[name] for name in ("graph_id", "owner_epoch", "revision")
        ):
            raise SnapshotProtocolError("graph preparation returned an incomplete handle")
        return cls(value["graph_id"], value["owner_epoch"], value["revision"])

    def wire(self) -> dict[str, object]:
        return {
            "graph_id": self.graph_id,
            "owner_epoch": self.owner_epoch,
            "revision": self.revision,
            "complete": self.complete,
        }


class PreparedGraphEvidence:
    def __init__(self, sources: GraphSources) -> None:
        self.sources = sources
        self.guard = threading.RLock()
        self.references = 1
        self.outcome: PreparedGraphHandle | EvidenceIncomplete | None = None
        self.classifier: tuple[tuple[str, str], ...] | None = None
        self.client: SnapshotClient | None = None
        self.native_released = False
        self.lapses = 0

    def require_unprepared_classifier_change(self) -> None:
        with self.guard:
            if self.outcome is not None or self.references != 1:
                raise EvidenceIncomplete("changed", "classifier changed after graph preparation or sharing")

    def retain(self) -> PreparedGraphEvidence:
        with self.guard:
            if self.references == 0:
                raise EvidenceIncomplete("stale_handle", "prepared graph is closed")
            self.references += 1
        return self

    def require(self, session: RemoteSession) -> PreparedGraphHandle:
        with self.guard:
            if self.references == 0:
                raise EvidenceIncomplete("stale_handle", "prepared graph is closed")
            classifier = tuple(sorted(session.classifier.items()))
            if self.classifier is not None and self.classifier != classifier:
                raise EvidenceIncomplete("changed", "prepared graph classifier does not match this branch")
            if isinstance(self.outcome, EvidenceIncomplete):
                raise self.outcome
            if self.outcome is None:
                self.classifier = classifier
                try:
                    pages = list(
                        session.client.pages(
                            "prepare_graph",
                            view=session.view() | {"selectors": []},
                            thread_ids=list(self.sources.thread_ids),
                            roots=[str(path) for path in self.sources.roots],
                            direct_paths=[str(path) for path in self.sources.direct_paths],
                            active_since_unix_ms=self.sources.active_since_unix_ms,
                            deadline_unix_ms=graph_deadline(session.client),
                            limits=graph_limits(),
                        )
                    )
                    if len(pages) != 1 or pages[0].get("kind") != "prepared_graph":
                        raise SnapshotProtocolError("graph preparation returned an unexpected result")
                    self.outcome = PreparedGraphHandle.from_wire(pages[0]["handle"])
                    self.client = session.client
                    self.client._graphs.add(self)
                except EvidenceIncomplete as exc:
                    failure = (
                        GraphEvidenceExpired(exc.status, exc.reason)
                        if exc.status in {"stale_handle", "stale_cursor"}
                        else exc
                    )
                    self.outcome = failure
                    if failure is exc:
                        raise
                    raise failure from exc
            return self.outcome

    def answer(
        self, session: RemoteSession, query: Mapping[str, object], *, observers: Sequence[Mapping[str, object]] = ()
    ) -> list[dict[str, Any]]:
        return list(self._answer_pages(session, query, observers=observers))

    def _answer_pages(
        self, session: RemoteSession, query: Mapping[str, object], *, observers: Sequence[Mapping[str, object]] = ()
    ) -> Iterator[dict[str, Any]]:
        handle = self.require(session)
        try:
            yield from session.client.pages(
                "query_graph",
                handle=handle.wire(),
                selectors=[dict(selector) for selector in session.selectors],
                query=dict(query),
                deadline_unix_ms=graph_deadline(session.client),
                limits=graph_limits(),
                **({"observers": [dict(observer) for observer in observers]} if observers else {}),
            )
        except EvidenceIncomplete as exc:
            if exc.status == "stale_handle":
                with self.guard:
                    if self.outcome is handle:
                        assert self.client is not None
                        self.outcome = None
                        self.lapses += 1
                        self.client._graphs.discard(self)
            raise

    def any_input(self, session: RemoteSession, predicate: Callable[[PredicateInputs], bool]) -> bool:
        from cc_transcript.snapshots import SnapshotIncomplete, decode_projection

        def attempt() -> bool:
            with closing(self._answer_pages(session, {"kind": "deep_predicate_inputs", "order": "forward"})) as pages:
                for data in pages:
                    if data.get("kind") != "records":
                        raise SnapshotProtocolError("predicate inputs returned an unexpected projection")
                    try:
                        inputs = decode_projection(
                            data["record_schema"], data["records_json"], tool_registry=session.client.tool_registry()
                        )
                    except SnapshotIncomplete as exc:
                        raise EvidenceIncomplete(exc.status, exc.reason) from exc
                    if any(predicate(window) for window in inputs):
                        return True
            return False

        try:
            try:
                return attempt()
            except EvidenceIncomplete as exc:
                if exc.status != "stale_handle":
                    raise
                return attempt()
        except EvidenceIncomplete as exc:
            if exc.status in {"incomplete", "deadline"}:
                session.client.schedule_root_warm(session.path, session.classifier, session.tail_bytes)
            if exc.status in {"stale_handle", "stale_cursor"}:
                raise GraphEvidenceExpired(exc.status, exc.reason) from exc
            raise

    def query_pages(
        self, session: RemoteSession, query: Mapping[str, object], *, observers: Sequence[Mapping[str, object]] = ()
    ) -> Iterator[dict[str, Any]]:
        try:
            try:
                yield from self.answer(session, query, observers=observers)
            except EvidenceIncomplete as exc:
                if exc.status != "stale_handle":
                    raise
                yield from self.answer(session, query, observers=observers)
        except EvidenceIncomplete as exc:
            if exc.status in {"incomplete", "deadline"}:
                session.client.schedule_root_warm(session.path, session.classifier, session.tail_bytes)
            if exc.status in {"stale_handle", "stale_cursor"}:
                raise GraphEvidenceExpired(exc.status, exc.reason) from exc
            raise

    def release(self) -> None:
        with self.guard:
            if self.references == 0:
                return
            self.references -= 1
        self.retry_release()

    def retry_release(self) -> None:
        with self.guard:
            if self.references != 0 or self.native_released or not isinstance(self.outcome, PreparedGraphHandle):
                return
            client = self.client
            assert client is not None
            if client._defer_cleanup:
                return
            result = client.call(
                "release", kind="graph", token=self.outcome.graph_id, owner_epoch=self.outcome.owner_epoch
            )
            if result.get("status") == "retained_limit":
                logger.bind(status="retained_limit", reason=result.get("reason")).warning(
                    "prepared graph cleanup deferred"
                )
                return
            if result.get("status") not in {"ok", "stale_handle"}:
                raise EvidenceIncomplete(str(result.get("status")), str(result.get("reason")))
            self.native_released = True
            client._graphs.discard(self)


@dataclass(frozen=True)
class ObserverPlan:
    predicates: tuple[tuple[str, str], ...]
    outcomes: dict[ScalarKey, ScalarValue] = field(default_factory=dict)

    def observers(self, key: ScalarKey) -> tuple[str, ...]:
        candidates = [
            query
            for selectors, query in self.predicates
            if selectors == key[4]
            and query != key[5]
            and (*key[:5], query) not in self.outcomes
            and len(query) <= OBSERVER_BYTES
            and observable(query)
        ]
        return tuple(
            query
            for query, size in islice(zip(candidates, accumulate(map(len, candidates)), strict=True), OBSERVER_LIMIT)
            if size <= OBSERVER_BYTES
        )


@dataclass(frozen=True)
class RemoteSession:
    client: SnapshotClient
    lease: Lease
    path: Path
    classifier: Mapping[str, str]
    selectors: tuple[Mapping[str, object], ...] = ()
    graph: PreparedGraphEvidence = field(default_factory=lambda: PreparedGraphEvidence(GraphSources()))
    tail_bytes: int | None = None

    @property
    def window_start(self) -> int:
        return self.lease.description["window_start"]

    @property
    def model(self) -> str | None:
        return self.lease.description["model"]

    def view(self) -> dict[str, object]:
        return {
            "handle": self.lease.require(),
            "classifier": dict(self.classifier),
            "selectors": [dict(s) for s in self.selectors],
            "attachments": [],
        }

    def with_registered_sources(self, sources: GraphSources) -> RemoteSession:
        window = replace(sources, active_since_unix_ms=self.lease.description["window_started_unix_ms"])
        return replace(self, graph=PreparedGraphEvidence(window))

    def selected(self, **selector: object) -> RemoteSession:
        return replace(self, selectors=(*self.selectors, selector))

    @property
    def current_turn(self) -> RemoteSession:
        return self.selected(kind="current_turn")

    def prior(self) -> RemoteSession:
        return self.selected(kind="prior")

    def recent(self, n: int) -> RemoteSession:
        return self.selected(kind="recent_events", count=n)

    def recent_messages(self, n: int) -> RemoteSession:
        return self.selected(kind="recent_messages", count=n)

    def after(self, *, tool: str, file: str | None = None) -> RemoteSession:
        return self.selected(kind="after_last_tool", name=tool, file=file)

    def before(self, *, tool: str) -> RemoteSession:
        return self.selected(kind="before_last_tool", name=tool, file=None)

    def retain(self) -> RemoteSession:
        client = CURRENT_CLIENT.get() or self.client
        handle = self.lease.require(client)
        try:
            data = list(client.pages("retain", handle=handle))
        except EvidenceIncomplete as exc:
            if exc.status != "stale_handle":
                raise
            data = list(client.pages("retain", handle=self.lease.reclaim(client, handle)))
        if len(data) != 1 or data[0].get("kind") != "acquired":
            raise SnapshotProtocolError("retain completed without one leased description")
        lease = Lease(client, data[0]["description"], tail_bytes=self.tail_bytes)
        try:
            graph = self.graph.retain()
        except BaseException:
            lease.release()
            raise
        return replace(self, client=client, lease=lease, graph=graph)

    def with_classifier(self, classifier: Mapping[str, str]) -> RemoteSession:
        if classifier == self.classifier:
            return self
        with self.graph.guard:
            self.graph.require_unprepared_classifier_change()
            self.lease.require()
            client = CURRENT_CLIENT.get() or self.client
            try:
                classified = client.acquire(self.path, classifier=classifier, tail_bytes=self.tail_bytes)
            except EvidenceIncomplete as exc:
                if exc.status in {"incomplete", "deadline"}:
                    client.schedule_root_warm(self.path, classifier, self.tail_bytes)
                raise
            try:
                before, after = self.lease.description, classified.lease.description
                if after["source_id"] != before["source_id"] or after["committed_bytes"] < before["committed_bytes"]:
                    raise EvidenceIncomplete("changed", "transcript was rewritten during classifier selection")
            except BaseException:
                classified.release()
                raise
            graph = self.graph.retain()
            try:
                self.release()
            except BaseException:
                graph.release()
                classified.release()
                raise
            classified.graph.release()
            return replace(classified, selectors=self.selectors, graph=graph)

    def release(self) -> None:
        with self.lease.guard:
            release_graph = not self.lease.graph_released
            self.lease.graph_released = True
        with ExitStack() as cleanup:
            cleanup.callback(self.lease.release)
            if release_graph:
                cleanup.callback(self.graph.release)

    @property
    def evidence_ref(self) -> str:
        handle = self.lease.handle
        return f"transcript:{handle['owner_epoch']}:{handle['snapshot_id']}:{handle['generation']}"

    def query(self, query: Mapping[str, object]) -> Any:
        deep = query.get("subagents") is True or query["kind"] in {
            "deep_predicate_inputs",
            "sidechain_membership",
        }
        if deep and query["kind"] == "sidechain_membership":
            raise EvidenceIncomplete("invalid_request", "sidechain membership has no prepared graph projection")
        scalar = query["kind"] in SCALAR_QUERY_KINDS or str(query["kind"]).startswith("has_")
        if not scalar:
            return self._query(query, deep=deep)
        key: ScalarKey = (
            self.evidence_ref,
            tuple(sorted(self.classifier.items())),
            self.client,
            (self.graph, self.graph.lapses) if deep else None,
            canonical_json(self.selectors),
            canonical_json(query),
        )
        self.lease.require()
        if (cached := self.lease.scalar_results.get(key, SCALAR_MISS)) is not SCALAR_MISS:
            if deep:
                self.graph.require(self)
            return cached
        plan = OBSERVER_PLAN.get() if deep and str(query["kind"]).startswith("has_") else None
        value = (
            self._scalar(query, deep=deep)
            if plan is None
            else self._planned(plan, (self.evidence_ref, *key[1:]), query)
        )
        with self.lease.guard:
            if not self.lease.closed:
                self.lease.scalar_results[key] = value
        return value

    def _scalar(self, query: Mapping[str, object], *, deep: bool) -> ScalarValue:
        values = self._query(query, deep=deep)
        if len(values) != 1:
            raise SnapshotProtocolError("scalar query returned a non-scalar result")
        return values[0]

    def _planned(self, plan: ObserverPlan, key: ScalarKey, query: Mapping[str, object]) -> ScalarValue:
        if key in plan.outcomes:
            self.graph.require(self)
            return plan.outcomes[key]
        value, observations = (
            self._observed(key, query, observers)
            if (observers := plan.observers(key))
            else (self._scalar(query, deep=True), {})
        )
        if self.evidence_ref == key[0] and (self.graph, self.graph.lapses) == key[3]:
            plan.outcomes.update(observations | ({key: value} if key[4:] in plan.predicates else {}))
        return value

    def _observed(
        self, key: ScalarKey, query: Mapping[str, object], observers: tuple[str, ...]
    ) -> tuple[ScalarValue, dict[ScalarKey, ScalarValue]]:
        match list(self.graph.query_pages(self, query, observers=[json.loads(observer) for observer in observers])):
            case [{"kind": "scalar", "value": value}]:
                return value, {}
            case [{"kind": "observed_scalar", "value": value, "observers": [*outcomes]}]:
                if len(outcomes) != len(observers):
                    raise SnapshotProtocolError("observed scalar query answered a different observer count")
                return value, {
                    (*key[:5], observer): outcome
                    for observer, outcome in zip(observers, outcomes, strict=True)
                    if isinstance(outcome, bool)
                }
            case _:
                raise SnapshotProtocolError("observed scalar query returned an unexpected projection")

    def _query(self, query: Mapping[str, object], *, deep: bool) -> list[Any]:
        values: list[Any] = []
        pages = (
            self.graph.query_pages(self, query)
            if deep
            else self.client.pages("query", view=self.view(), query=dict(query))
        )
        for data in pages:
            match data.get("kind"):
                case "scalar":
                    values.append(data["value"])
                case "strings":
                    values.extend(data["values"])
                case "records":
                    from cc_transcript.snapshots import SnapshotIncomplete, decode_projection

                    try:
                        values.extend(
                            decode_projection(
                                data["record_schema"], data["records_json"], tool_registry=self.client.tool_registry()
                            )
                        )
                    except SnapshotIncomplete as exc:
                        raise EvidenceIncomplete(exc.status, exc.reason) from exc
                case _:
                    raise SnapshotProtocolError("query returned an unexpected projection")
        return values

    def __len__(self) -> int:
        return self.query({"kind": "event_count"})

    @property
    def user_text(self) -> str:
        return self.query({"kind": "user_text"})

    @property
    def first_prompt(self) -> str | None:
        return self.query({"kind": "first_prompt"})

    def prompts(self, selection: Literal["first", "last", "current"], count: int) -> list[str]:
        return self.query({"kind": "prompts", "selection": selection, "count": count})

    def assistant_text(self, n: int = 10, *, max_per_msg: int = 500) -> str:
        return "\n---\n".join(self.query({"kind": "assistant_text", "count": n, "max_per_message": max_per_msg}))

    def signal_texts(
        self, window: int | Literal["turn"], origin: Literal["assistant", "any"], thinking: bool
    ) -> list[str]:
        return self.query(
            {
                "kind": "signal_texts",
                "window": "current_turn" if window == "turn" else window,
                "origin": origin,
                "thinking": thinking,
            }
        )

    def render(self, *, budget: Budget, tool_results: bool = False) -> str:
        return "".join(
            self.query(
                {
                    "kind": "render",
                    "budget": {"turn_chars": budget.turn_chars, "tool_chars": budget.tool_chars},
                    "tool_results": tool_results,
                }
            )
        )

    def activity_probe(
        self, *, waiting_tools: Collection[str], tool_registry_generation: str, human_facing_tools: Collection[str] = ()
    ) -> bool:
        data = list(
            self.client.pages(
                "activity_probe",
                view=self.view(),
                waiting_tools=sorted(waiting_tools),
                human_facing_tools=sorted(human_facing_tools),
                tool_registry_generation=tool_registry_generation,
                policy_version="1",
            )
        )
        if len(data) != 1 or data[0].get("kind") != "activity_probe":
            raise SnapshotProtocolError("activity probe returned invalid result")
        return data[0]["waiting"]

    def count_failures(self) -> int:
        return self.query({"kind": "failures"})

    def matches(self, *patterns: object) -> bool:
        from captain_hook.signals.nlp import scan_text

        return scan_text(self.user_text, patterns)

    @property
    def tool_calls(self) -> RemoteToolCalls:
        return RemoteToolCalls(self)

    @property
    def subagents(self) -> RemoteSubagentIndex:
        key = canonical_json(self.selectors)
        with self.lease.subagent_guard:
            self.lease.require()
            if key in self.lease.subagent_views:
                return self.lease.subagent_views[key]
            index = self._subagents()
            if index:
                self.lease.subagent_views[key] = index
            return index

    def _subagents(self) -> RemoteSubagentIndex:
        from cc_transcript.snapshots import SnapshotIncomplete, decode_projection
        from cc_transcript.tools import TaskCall

        dispatches = tuple(
            use
            for use in self.query({"kind": "tool_calls", "order": "forward", "name": "Task"})
            if isinstance(use.call, TaskCall) and use.call.agent_type and use.ref.tool_use_id is not None
        )
        if not dispatches:
            return RemoteSubagentIndex(())
        members: list[tuple[Mapping[str, Any], RemoteSession]] = []
        with ExitStack() as cleanup:
            dispatch_ids = list(dict.fromkeys(use.ref.tool_use_id for use in dispatches))
            for offset in range(0, len(dispatch_ids), 256):
                for data in self.client.pages(
                    "query",
                    view=self.view(),
                    query={
                        "kind": "direct_sidechains",
                        "order": "forward",
                        "dispatch_ids": dispatch_ids[offset : offset + 256],
                    },
                ):
                    if data["kind"] != "records" or data["record_schema"] != "cc-transcript.sidechain/1":
                        raise SnapshotProtocolError("sidechain query returned an unexpected projection")
                    try:
                        records = decode_projection(
                            data["record_schema"], data["records_json"], tool_registry=self.client.tool_registry()
                        )
                    except SnapshotIncomplete as exc:
                        raise EvidenceIncomplete(exc.status, exc.reason) from exc
                    for record in records:
                        description = record["description"]
                        child = RemoteSession(
                            self.client,
                            Lease(self.client, description),
                            Path(record["path"]),
                            description["classifier"],
                        )
                        cleanup.callback(child.release)
                        members.append((record, child))
            children = {
                record["spawned_by"]: child
                for record, child in members
                if record["depth"] == 1 and record["spawned_by"] is not None
            }
            items = tuple(
                RemoteSubagentSession(use.ref.tool_use_id, use.call.agent_type, children[use.ref.tool_use_id], use)
                for use in dispatches
                if use.ref.tool_use_id in children
            )
            selected = {id(item.session) for item in items}
            for _, child in members:
                if id(child) not in selected:
                    child.release()
            cleanup.pop_all()
            return RemoteSubagentIndex(items)

    @property
    def events(self) -> tuple[Any, ...]:
        return self._windowed("events", 0, None, lambda left, right: left + right)

    @property
    def turns(self) -> tuple[Any, ...]:
        return self._windowed("turns", 0, None, joined_turns)

    def _windowed(
        self,
        kind: str,
        start: int,
        stop: int | None,
        join: Callable[[tuple[Any, ...], tuple[Any, ...]], tuple[Any, ...]],
    ) -> tuple[Any, ...]:
        window = self if stop is None else self.selected(kind="event_range", start=start, stop=stop)
        try:
            return tuple(window.query({"kind": kind, "order": "forward"}))
        except EvidenceIncomplete as exc:
            if exc.status != "output_limit":
                raise
            stop = len(self) if stop is None else stop
            if stop - start < 2:
                skip_oversized(self.path, exc)
                return ()
            middle = (start + stop) // 2
            return join(self._windowed(kind, start, middle, join), self._windowed(kind, middle, stop, join))

    @property
    def files_touched(self) -> tuple[Any, ...]:
        return tuple(self.query({"kind": "files_touched", "order": "forward"}))

    @property
    def edited_files(self) -> tuple[Any, ...]:
        return tuple(self.query({"kind": "edited_files", "order": "forward"}))

    def deep_inputs(self) -> Iterator[Any]:
        yield from self.query({"kind": "deep_predicate_inputs", "order": "forward"})

    def any_deep_input(self, predicate: Callable[[PredicateInputs], bool]) -> bool:
        return self.graph.any_input(self, predicate)

    def has_tool(self, name: str, *, subagents: bool = True) -> bool:
        return self.query(tool_predicate("has_tool", name, subagents=subagents))

    def has_command(self, *argv: str, subagents: bool = True) -> bool:
        return self.query(list_predicate("has_command", argv, subagents=subagents))

    def has_edit_to(self, *globs: str, subagents: bool = True) -> bool:
        return self.query(list_predicate("has_edit_to", globs, subagents=subagents))

    def has_read(self, pattern: str, *, subagents: bool = True) -> bool:
        return self.query(tool_predicate("has_read", pattern, subagents=subagents))

    def has_skill(self, *names: str, subagents: bool = True) -> bool:
        return self.query(list_predicate("has_skill", names, subagents=subagents))

    def has_override(
        self, token: str, *, invalidated_by: Sequence[str] = ("Edit", "Write"), subagents: bool = True
    ) -> bool:
        return self.query(override_predicate(token, invalidated_by, subagents=subagents))


def skip_oversized(path: Path, exc: EvidenceIncomplete) -> None:
    from captain_hook.util import reqenv

    logger.bind(path=str(path), reason=exc.reason).warning("transcript event over the record bound skipped")
    reqenv.evidence_gaps().append(f"transcript event: {exc.status}: {exc.reason}")


def joined_turns(left: tuple[Any, ...], right: tuple[Any, ...]) -> tuple[Any, ...]:
    if not left or not right or left[-1].index != right[0].index:
        return left + right
    head, tail = left[-1], right[0]
    events = head.events + tail.events
    turn = replace(
        head,
        ended_at=tail.ended_at or head.ended_at,
        events=events,
        tool_uses=paired(head.tool_uses + tail.tool_uses, events),
    )
    return (*left[:-1], turn, *right[1:])


def paired(uses: tuple[ToolUse, ...], events: tuple[Any, ...]) -> tuple[ToolUse, ...]:
    from cc_transcript.models import ToolResultBlock

    results = {
        block.tool_use_id: (block, event.meta.timestamp)
        for event in events
        for block in getattr(event, "blocks", ())
        if isinstance(block, ToolResultBlock)
    }
    return tuple(
        replace(use, result=found[0], result_ts=found[1])
        if use.result is None and (found := results.get(use.ref.tool_use_id)) is not None
        else use
        for use in uses
    )


CURRENT_CLIENT: contextvars.ContextVar[SnapshotClient | None] = contextvars.ContextVar("snapshot_client", default=None)


@dataclass(frozen=True)
class RemoteSubagentSession:
    id: ToolUseId
    type: str
    session: RemoteSession
    parent: ToolUse

    @property
    def tool_calls(self) -> RemoteToolCalls:
        return self.session.tool_calls

    @property
    def failed(self) -> bool:
        return bool((result := self.parent.result) and result.is_error) or self.session.count_failures() > 0


@dataclass(frozen=True)
class RemoteSubagentIndex:
    items: tuple[RemoteSubagentSession, ...]

    def with_type(self, pattern: str) -> tuple[RemoteSubagentSession, ...]:
        names = set(pattern.split("|"))
        return tuple(subagent for subagent in self.items if subagent.type in names)

    def __iter__(self) -> Iterator[RemoteSubagentSession]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)


@dataclass(frozen=True)
class RemoteToolCalls:
    session: RemoteSession
    name: str | None = None
    input_regex: Mapping[str, object] | None = None
    errors: Literal["exclude", "include", "only"] = "exclude"

    @property
    def items(self) -> tuple[ToolUse, ...]:
        from cc_transcript.query import ToolCallQuery

        query = ToolCallQuery(
            tuple(self.session.query({"kind": "tool_calls", "name": self.name, "order": "forward"})),
            include_errors=self.errors != "exclude",
        )
        if self.errors == "only":
            query = query.failed()
        if self.input_regex is not None:
            query = query.where_input(
                **{self.input_regex["field"]: re.compile(self.input_regex["pattern"], self.input_regex["flags"])}
            )
        return query.items

    def named(self, name: str) -> RemoteToolCalls:
        return replace(self, name=name)

    def where_input(self, **patterns: Any) -> RemoteToolCalls:
        if len(patterns) != 1:
            raise EvidenceIncomplete("invalid_request", "tool count requires one explicit input-field predicate")
        field, pattern = next(iter(patterns.items()))
        return replace(self, input_regex={"field": field, "pattern": pattern.pattern, "flags": pattern.flags})

    def count(self) -> int:
        return self.session.query(
            {"kind": "tool_count", "name": self.name, "input_regex": self.input_regex, "errors": self.errors}
        )

    def count_failures(self) -> int:
        return replace(self, errors="only").count()

    def failed(self) -> RemoteToolCalls:
        return replace(self, errors="only")

    def __len__(self) -> int:
        return self.count()


@contextmanager
def foreground_evidence(seconds: float) -> Iterator[None]:
    if (client := CURRENT_CLIENT.get()) is None:
        yield
        return
    with client.foreground_at_least(seconds):
        yield


@contextmanager
def observing(predicates: Iterable[tuple[str, str]]) -> Iterator[ObserverPlan | None]:
    if not (planned := tuple(dict.fromkeys(predicates))):
        yield None
        return
    token = OBSERVER_PLAN.set(plan := ObserverPlan(planned))
    try:
        yield plan
    finally:
        OBSERVER_PLAN.reset(token)


@contextmanager
def client_scope(*, defer_cleanup: bool = False, event: int = 0) -> Iterator[SnapshotClient]:
    from captain_hook.snapshots.validation import checked

    if (current := CURRENT_CLIENT.get()) is not None:
        yield current
        return
    bridge = Bridge(event=event)
    client = SnapshotClient(lambda value: checked("host-response", bridge(value)), defer_cleanup=defer_cleanup)
    token = CURRENT_CLIENT.set(client)
    try:
        yield client
    finally:
        try:
            client.close()
        finally:
            CURRENT_CLIENT.reset(token)
            bridge.close()
