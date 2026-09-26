from __future__ import annotations

import contextvars
import importlib.metadata
import json
import os
import struct
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

if TYPE_CHECKING:
    from cc_transcript.activity import ToolUse
    from cc_transcript.ids import ToolUseId
    from cc_transcript.render import Budget

CORE_SCHEMA = "cc-transcript.snapshot/1"
HOST_SCHEMA = "captain.transcript/1"
MAX_FRAME_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 16 * 1024 * 1024
MAX_VIEW_ATTACHMENTS = 1024
CLEANUP_SECONDS = 5
GRAPH_WORK_SECONDS = 0.75
GRAPH_READ_BYTES = 1024 * 1024
GRAPH_DISCOVERY_ENTRIES = 50_000
GRAPH_SOURCE_LIMIT = 4096
DEFAULT_LIMITS = {
    "max_read_bytes": 512 * 1024 * 1024,
    "max_events": 1_000_000,
    "max_items": 65_536,
    "max_output_bytes": MAX_RESULT_BYTES,
    "max_discovery_entries": 1_000_000,
    "max_sources": 65_536,
}
NATIVE_CLASSIFIER = {"id": "native", "version": "1"}


def graph_limits() -> dict[str, int]:
    limits = DEFAULT_LIMITS.copy()
    limits["max_read_bytes"] = min(limits["max_read_bytes"], GRAPH_READ_BYTES)
    limits["max_discovery_entries"] = min(limits["max_discovery_entries"], GRAPH_DISCOVERY_ENTRIES)
    limits["max_sources"] = min(limits["max_sources"], GRAPH_SOURCE_LIMIT)
    return limits


class EvidenceIncomplete(RuntimeError):
    def __init__(self, status: str, reason: str) -> None:
        super().__init__(f"transcript evidence {status}: {reason}")
        self.status = status
        self.reason = reason


class SnapshotProtocolError(EvidenceIncomplete):
    def __init__(self, reason: str) -> None:
        super().__init__("invalid_request", reason)


class AttachmentLimit(EvidenceIncomplete):
    def __init__(self) -> None:
        super().__init__("source_limit", f"registered transcript attachments exceed {MAX_VIEW_ATTACHMENTS}")


class GraphEvidenceExpired(EvidenceIncomplete):
    pass


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
    def __init__(self, command: Sequence[str] | None = None) -> None:
        if command is None and os.environ.get("CAPT_HOOK_TEST_NO_LIVE") == "1":
            raise SnapshotProtocolError("tests must supply an isolated snapshot transport")
        self._command = tuple(
            command
            or (str(Path.home() / "Applications/Captain Hook.app/Contents/Helpers/capt-hookd"), "transcript-client")
        )
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
            process = self._process
            if process.stdin is None or process.stdout is None:
                raise SnapshotProtocolError("snapshot bridge did not create pipes")
            process.stdin.write(encode_frame(frame))
            process.stdin.flush()
            response = read_frame(process.stdout)
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
        warm_scheduler: Callable[[SnapshotClient, GraphSources], None] | None = None,
        root_warm_scheduler: Callable[[SnapshotClient, Path, Mapping[str, str]], None] | None = None,
        defer_cleanup: bool = False,
        foreground_seconds: float | None = None,
        foreground_read_bytes: int | None = None,
    ) -> None:
        self._exchange = exchange
        self._cleanup_exchange = cleanup_exchange or exchange
        self._leases: set[Lease] = set()
        self._graphs: set[PreparedGraphEvidence] = set()
        self._preparation_seconds = preparation_seconds
        self._warm_scheduler = warm_scheduler
        self._root_warm_scheduler = root_warm_scheduler
        self._defer_cleanup = defer_cleanup
        self._deferred_cursors: set[tuple[str, str | None]] = set()
        self._foreground_seconds = foreground_seconds
        self._foreground_read_bytes = foreground_read_bytes
        self._foreground_source_bytes = 0
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

    def clone_for_exchange(self, exchange: Callable[[dict[str, object]], dict[str, Any]]) -> SnapshotClient:
        clone = SnapshotClient(exchange)
        clone._tool_registry = self._tool_registry
        return clone

    def schedule_graph_warm(self, sources: GraphSources) -> None:
        if self._warm_scheduler is not None:
            self._warm_scheduler(self, sources)

    def schedule_root_warm(self, path: str | Path, classifier: Mapping[str, str]) -> None:
        if classifier["id"] != "captain-configured" and self._root_warm_scheduler is not None:
            self._root_warm_scheduler(self, Path(path).absolute(), classifier)

    def call(self, operation: str, *, domain: bool = False, **arguments: object) -> dict[str, Any]:
        from jsonschema import ValidationError

        from captain_hook.snapshots.validation import checked

        cleanup_deadline = time.monotonic() + CLEANUP_SECONDS if operation == "release" else None
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
                raise EvidenceIncomplete("deadline", "foreground transcript deadline exhausted")
            request: dict[str, object] = {
                "schema": HOST_SCHEMA if domain else CORE_SCHEMA,
                "id": request_id,
                "operation": operation,
                **arguments,
            }
            if operation not in {"resume", "release", "retain", "renew", "describe", "stats", "submit_classifier"}:
                request.setdefault("deadline_unix_ms", int((time.time() + self._preparation_seconds) * 1000))
                request.setdefault("limits", DEFAULT_LIMITS.copy())
            if self.foreground_deadline_unix_ms is not None and "deadline_unix_ms" in request:
                request["deadline_unix_ms"] = min(int(request["deadline_unix_ms"]), self.foreground_deadline_unix_ms)
            remaining_bytes = None
            if self._foreground_read_bytes is not None and operation != "release":
                with self._guard:
                    remaining_bytes = self._foreground_read_bytes - self._foreground_source_bytes
                if remaining_bytes <= 0:
                    raise EvidenceIncomplete("incomplete", "foreground transcript byte budget exhausted")
            if remaining_bytes is not None and "limits" in request:
                limits = dict(request["limits"])
                limits["max_read_bytes"] = min(int(limits["max_read_bytes"]), remaining_bytes)
                request["limits"] = limits
            exchange = self._cleanup_exchange if operation == "release" else self._exchange
            try:
                response = exchange({"schema": HOST_SCHEMA, "request": request, "tool_registry": self.tool_registry()})
                response = checked("host-response", response)
            except (OSError, ValueError, ValidationError) as exc:
                raise SnapshotProtocolError(str(exc)) from exc
            if response.get("schema") != HOST_SCHEMA or not isinstance(response.get("response"), dict):
                raise SnapshotProtocolError("snapshot response has invalid transport envelope")
            result = response["response"]
            if result.get("schema") != CORE_SCHEMA or result.get("id") != request_id:
                raise SnapshotProtocolError("snapshot response schema or id mismatch")
            if self._foreground_read_bytes is not None and operation != "release":
                with self._guard:
                    self._foreground_source_bytes += result["usage"]["source_bytes_read"]
                    if self._foreground_source_bytes > self._foreground_read_bytes:
                        raise SnapshotProtocolError("foreground transcript byte budget was exceeded")
            if cleanup_deadline is None or result.get("status") != "retained_limit":
                return result
            remaining = cleanup_deadline - time.monotonic()
            if remaining <= 0:
                return result
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
                    raise EvidenceIncomplete(str(status), str(result.get("reason")))
                if (data := result.get("data")) is not None:
                    if not isinstance(data, dict):
                        raise SnapshotProtocolError("snapshot data must be an object")
                    output_bytes += len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())
                    if output_bytes > MAX_RESULT_BYTES:
                        raise EvidenceIncomplete("output_limit", "snapshot result exceeds cumulative projection bound")
                    yield data
                if status == "ok":
                    if result.get("complete") is not True or cursor is not None:
                        raise SnapshotProtocolError("complete snapshot result has invalid continuation")
                    return
                if not isinstance(cursor, str) or not cursor:
                    raise EvidenceIncomplete("incomplete", str(result.get("reason")))
                result = self.call("resume", cursor=cursor)
                cursor = result.get("cursor")
        finally:
            if cursor is not None:
                if self._defer_cleanup:
                    self._deferred_cursors.add((cursor, None))
                else:
                    self.call("release", kind="cursor", token=cursor)

    def acquire(self, path: str | Path, *, classifier: Mapping[str, str] = NATIVE_CLASSIFIER) -> RemoteSession:
        description = None
        for data in self.pages("acquire", path=str(Path(path).absolute()), classifier=dict(classifier)):
            if data.get("kind") == "acquired":
                description = data["description"]
        if not isinstance(description, dict):
            raise SnapshotProtocolError("acquire completed without a snapshot description")
        return RemoteSession(
            self, Lease(self, description), Path(description["canonical_path"]), description["classifier"]
        )

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
        classified = RemoteSession(self, Lease(self, description), session.path, description["classifier"])
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
    def __init__(self, client: SnapshotClient, description: Mapping[str, Any]) -> None:
        self.client = client
        client._leases.add(self)
        self.description = dict(description)
        self.handle = dict(description["handle"])
        self.expires_unix_ms = description["lease_expires_unix_ms"]
        self.released = False
        self.closed = False
        self.cleanup_pending = False
        self.graph_released = False
        self.guard = threading.Lock()
        self.subagent_guard = threading.Lock()
        self.subagent_views: dict[str, RemoteSubagentIndex] = {}

    def require(self, client: SnapshotClient | None = None) -> dict[str, str]:
        with self.guard:
            if self.closed or self.released:
                raise EvidenceIncomplete("stale_handle", "preparation lease was already released")
            if self.expires_unix_ms <= time.time() * 1000 + 1000:
                result = (client or self.client).call("renew", handle=self.handle)
                if result["status"] != "ok":
                    raise EvidenceIncomplete(result["status"], result["reason"])
                self.expires_unix_ms = result["data"]["expires_unix_ms"]
            return self.handle.copy()

    def release(self) -> None:
        with self.subagent_guard:
            with ExitStack() as cleanup:
                cleanup.callback(self._release)
                for index in self.subagent_views.values():
                    for item in index:
                        cleanup.callback(item.session.release)
            self.subagent_views.clear()

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
    session_key: str | None = None


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
        limits = graph_limits() | {"max_read_bytes": read_bytes}
        result = self.client.call(
            "warm_registered",
            classifier=NATIVE_CLASSIFIER,
            thread_ids=list(self.sources.thread_ids),
            roots=[str(root) for root in self.sources.roots],
            direct_paths=[str(path) for path in self.sources.direct_paths],
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
    owner_epoch: str | None = None
    source_revision: str | None = None
    source_offset: int = 0
    source_size: int = 0
    facts_complete: bool = False
    stalled_steps: int = 0
    last_usage: dict[str, int] = field(default_factory=dict)

    def step(self, *, read_bytes: int, deadline_seconds: float) -> bool:
        result = self.client.call(
            "warm_root",
            path=str(self.path),
            classifier=dict(self.classifier),
            deadline_unix_ms=int((time.time() + deadline_seconds) * 1000),
            limits=graph_limits() | {"max_read_bytes": read_bytes},
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
        self.deadline_unix_ms: int | None = None

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
                    from captain_hook.util import reqenv

                    deadline = int((time.time() + GRAPH_WORK_SECONDS) * 1000)
                    if session.client.foreground_deadline_unix_ms is not None:
                        deadline = min(deadline, session.client.foreground_deadline_unix_ms)
                    if (request := reqenv.current()) is not None and request.deadline_unix_ms:
                        deadline = min(deadline, request.deadline_unix_ms - 1000)
                    if deadline <= int(time.time() * 1000):
                        raise EvidenceIncomplete("deadline", "no time remains for prepared graph evidence")
                    self.deadline_unix_ms = deadline
                    if (
                        len(self.sources.thread_ids) > 1024
                        or len(self.sources.direct_paths) > 1024
                        or len(self.sources.roots) > 64
                    ):
                        raise AttachmentLimit()
                    pages = list(
                        session.client.pages(
                            "prepare_graph",
                            view=session.view() | {"selectors": []},
                            thread_ids=list(self.sources.thread_ids),
                            roots=[str(path) for path in self.sources.roots],
                            direct_paths=[str(path) for path in self.sources.direct_paths],
                            deadline_unix_ms=deadline,
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

    def query_pages(self, session: RemoteSession, query: Mapping[str, object]) -> Iterator[dict[str, Any]]:
        try:
            handle = self.require(session)
            assert self.deadline_unix_ms is not None
            yield from session.client.pages(
                "query_graph",
                handle=handle.wire(),
                selectors=[dict(selector) for selector in session.selectors],
                query=dict(query),
                deadline_unix_ms=self.deadline_unix_ms,
                limits=graph_limits(),
            )
        except EvidenceIncomplete as exc:
            if exc.status in {"incomplete", "deadline"}:
                session.client.schedule_graph_warm(self.sources)
                session.client.schedule_root_warm(session.path, session.classifier)
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
class RemoteSession:
    client: SnapshotClient
    lease: Lease
    path: Path
    classifier: Mapping[str, str]
    selectors: tuple[Mapping[str, object], ...] = ()
    graph: PreparedGraphEvidence = field(default_factory=lambda: PreparedGraphEvidence(GraphSources()))

    def view(self) -> dict[str, object]:
        return {
            "handle": self.lease.require(),
            "classifier": dict(self.classifier),
            "selectors": [dict(s) for s in self.selectors],
            "attachments": [],
        }

    def with_registered_sources(self, sources: GraphSources) -> RemoteSession:
        return replace(self, graph=PreparedGraphEvidence(sources))

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
        data = list(client.pages("retain", handle=self.lease.require(client)))
        if len(data) != 1 or data[0].get("kind") != "acquired":
            raise SnapshotProtocolError("retain completed without one leased description")
        lease = Lease(client, data[0]["description"])
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
                classified = client.acquire(self.path, classifier=classifier)
            except EvidenceIncomplete as exc:
                if exc.status in {"incomplete", "deadline"}:
                    client.schedule_root_warm(self.path, classifier)
                raise
            try:
                if any(
                    self.lease.description[field] != classified.lease.description[field]
                    for field in (
                        "source_id",
                        "mtime_ns",
                        "ctime_ns",
                        "source_bytes",
                        "committed_bytes",
                        "provisional_tail",
                    )
                ):
                    raise EvidenceIncomplete("changed", "transcript changed during classifier selection")
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
        values: list[Any] = []
        deep = query.get("subagents") is True or query["kind"] in {
            "deep_predicate_inputs",
            "sidechain_membership",
        }
        if deep and query["kind"] == "sidechain_membership":
            raise EvidenceIncomplete("invalid_request", "sidechain membership has no prepared graph projection")
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
        if query["kind"] in {
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
        } or str(query["kind"]).startswith("has_"):
            if len(values) != 1:
                raise SnapshotProtocolError("scalar query returned a non-scalar result")
            return values[0]
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

    def signal_texts(self, window: int | Literal["turn"], origin: Literal["assistant", "any"]) -> list[str]:
        return self.query(
            {"kind": "signal_texts", "window": "current_turn" if window == "turn" else window, "origin": origin}
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
        key = json.dumps(self.selectors, sort_keys=True, separators=(",", ":"))
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
        return tuple(self.query({"kind": "events", "order": "forward"}))

    @property
    def turns(self) -> tuple[Any, ...]:
        return tuple(self.query({"kind": "turns", "order": "forward"}))

    @property
    def files_touched(self) -> tuple[Any, ...]:
        return tuple(self.query({"kind": "files_touched", "order": "forward"}))

    @property
    def edited_files(self) -> tuple[Any, ...]:
        return tuple(self.query({"kind": "edited_files", "order": "forward"}))

    def deep_inputs(self) -> Iterator[Any]:
        yield from self.query({"kind": "deep_predicate_inputs", "order": "forward"})

    def has_tool(self, name: str, *, subagents: bool = True) -> bool:
        return self.query({"kind": "has_tool", "pattern": name, "subagents": subagents})

    def has_command(self, *argv: str, subagents: bool = True) -> bool:
        return self.query({"kind": "has_command", "values": list(argv), "subagents": subagents})

    def has_edit_to(self, *globs: str, subagents: bool = True) -> bool:
        return self.query({"kind": "has_edit_to", "values": list(globs), "subagents": subagents})

    def has_read(self, pattern: str, *, subagents: bool = True) -> bool:
        return self.query({"kind": "has_read", "pattern": pattern, "subagents": subagents})

    def has_skill(self, *names: str, subagents: bool = True) -> bool:
        return self.query({"kind": "has_skill", "values": list(names), "subagents": subagents})

    def has_override(
        self, token: str, *, invalidated_by: Sequence[str] = ("Edit", "Write"), subagents: bool = True
    ) -> bool:
        return self.query(
            {"kind": "has_override", "token": token, "invalidated_by": list(invalidated_by), "subagents": subagents}
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
def client_scope() -> Iterator[SnapshotClient]:
    if (current := CURRENT_CLIENT.get()) is not None:
        yield current
        return
    bridge = Bridge()
    client = SnapshotClient(bridge)
    token = CURRENT_CLIENT.set(client)
    try:
        yield client
    finally:
        try:
            client.close()
        finally:
            CURRENT_CLIENT.reset(token)
            bridge.close()
