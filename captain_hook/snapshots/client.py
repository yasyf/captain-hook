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
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from cc_transcript.render import Budget

CORE_SCHEMA = "cc-transcript.snapshot/1"
HOST_SCHEMA = "captain.transcript/1"
MAX_FRAME_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 16 * 1024 * 1024
DEFAULT_LIMITS = {
    "max_read_bytes": 512 * 1024 * 1024,
    "max_events": 1_000_000,
    "max_items": 65_536,
    "max_output_bytes": MAX_RESULT_BYTES,
    "max_discovery_entries": 1_000_000,
    "max_sources": 65_536,
}
NATIVE_CLASSIFIER = {"id": "native", "version": "1"}


class EvidenceIncomplete(RuntimeError):
    def __init__(self, status: str, reason: str) -> None:
        super().__init__(f"transcript evidence {status}: {reason}")
        self.status = status
        self.reason = reason


class SnapshotProtocolError(EvidenceIncomplete):
    def __init__(self, reason: str) -> None:
        super().__init__("invalid_request", reason)


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
    ) -> None:
        self._exchange = exchange
        self._cleanup_exchange = cleanup_exchange or exchange
        self._leases: set[Lease] = set()
        self._preparation_seconds = preparation_seconds
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

    def call(self, operation: str, *, domain: bool = False, **arguments: object) -> dict[str, Any]:
        with self._guard:
            self._counter += 1
            request_id = f"{self._prefix}-{self._counter}"
        request: dict[str, object] = {
            "schema": HOST_SCHEMA if domain else CORE_SCHEMA,
            "id": request_id,
            "operation": operation,
            **arguments,
        }
        if operation not in {"resume", "release", "retain", "renew", "describe", "stats", "submit_classifier"}:
            request["deadline_unix_ms"] = int((time.time() + self._preparation_seconds) * 1000)
            request["limits"] = DEFAULT_LIMITS.copy()
        exchange = self._cleanup_exchange if operation == "release" else self._exchange
        from jsonschema import ValidationError

        from captain_hook.snapshots.validation import checked

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
        return result

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
                self.call("release", owner_epoch=session.lease.handle["owner_epoch"], kind="cursor", token=cursor)
        data = result["data"]
        if result["status"] != "ok" or data["kind"] != "acquired":
            raise SnapshotProtocolError("classifier completed without a leased description")
        description = data["description"]
        classified = RemoteSession(self, Lease(self, description), session.path, description["classifier"])
        session.release()
        return classified

    def close(self) -> None:
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
        self.handle = dict(description["handle"])
        self.expires_unix_ms = description["lease_expires_unix_ms"]
        self.released = False
        self.guard = threading.Lock()

    def require(self) -> dict[str, str]:
        with self.guard:
            if self.released:
                raise EvidenceIncomplete("stale_handle", "preparation lease was already released")
            if self.expires_unix_ms <= time.time() * 1000 + 1000:
                result = self.client.call("renew", handle=self.handle)
                if result["status"] != "ok":
                    raise EvidenceIncomplete(result["status"], result["reason"])
                self.expires_unix_ms = result["data"]["expires_unix_ms"]
            return self.handle.copy()

    def release(self) -> None:
        with self.guard:
            if self.released:
                return
            result = self.client.call(
                "release", owner_epoch=self.handle["owner_epoch"], kind="lease", token=self.handle["lease_id"]
            )
            if result.get("status") not in {"ok", "stale_handle"}:
                raise EvidenceIncomplete(str(result.get("status")), str(result.get("reason")))
            self.released = True
            self.client._leases.discard(self)


@dataclass(frozen=True)
class RemoteSession:
    client: SnapshotClient
    lease: Lease
    path: Path
    classifier: Mapping[str, str]
    selectors: tuple[Mapping[str, object], ...] = ()
    attachments: tuple[Path, ...] = ()

    def view(self) -> dict[str, object]:
        return {
            "handle": self.lease.require(),
            "classifier": dict(self.classifier),
            "selectors": [dict(s) for s in self.selectors],
            "attachments": [str(p) for p in self.attachments],
        }

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
        data = list(self.client.pages("retain", handle=self.lease.require()))
        if len(data) != 1 or data[0].get("kind") != "acquired":
            raise SnapshotProtocolError("retain completed without one leased description")
        return replace(self, lease=Lease(self.client, data[0]["description"]))

    def release(self) -> None:
        self.lease.release()

    @property
    def evidence_ref(self) -> str:
        handle = self.lease.handle
        return f"transcript:{handle['owner_epoch']}:{handle['snapshot_id']}:{handle['generation']}"

    def query(self, query: Mapping[str, object]) -> Any:
        values: list[Any] = []
        for data in self.client.pages("query", view=self.view(), query=dict(query)):
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
class RemoteToolCalls:
    session: RemoteSession
    name: str = ".*"
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
