from __future__ import annotations

import json
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from captain_hook import app
from captain_hook.cli import EVENT_NAMES, dispatch_event
from captain_hook.daemon import decision_writer, transcache
from captain_hook.daemon.context import RequestBuffers, request_scope
from captain_hook.daemon.registry import Registry
from captain_hook.session import ensure_session
from captain_hook.types import Event
from captain_hook.worker.protocol import EventRequest, EventResponse

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Protocol

    from captain_hook.cli import CliState

    class RegistryLike(Protocol):
        def get(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class _Client:
    ppid: int


@dataclass(frozen=True, slots=True)
class _ScopedRequest:
    env: dict[str, str]
    cwd: str
    client: _Client


class ProductRuntime:
    def __init__(
        self,
        *,
        registry_factory: Callable[[CliState], RegistryLike] = Registry,
        dispatcher: Callable[..., dict[str, Any] | None] = dispatch_event,
        transcript_loader: Callable[..., Any] = transcache.load,
        install_writer: bool = True,
    ) -> None:
        self._registry_factory = registry_factory
        self._dispatcher = dispatcher
        self._transcript_loader = transcript_loader
        self._registries: dict[str, RegistryLike] = {}
        self._registries_guard = threading.Lock()
        self._writer = decision_writer.install() if install_writer else None

    def dispatch(self, request: EventRequest) -> EventResponse:
        try:
            event = Event[request.event]
        except KeyError:
            return EventResponse(
                stderr=f"Invalid event type: {request.event!r}. Valid event names are: {EVENT_NAMES}\n",
                exit=1,
            )
        if not request.payload_raw.strip():
            return EventResponse()
        try:
            raw = cast(object, json.loads(request.payload_raw))
            parse_error = None
        except (json.JSONDecodeError, ValueError) as exc:
            raw = None
            parse_error = exc
        raw_dict = cast(dict[str, Any], raw) if isinstance(raw, dict) else None
        session_id = raw_dict.get("session_id") if raw_dict is not None else None
        session_id = session_id if isinstance(session_id, str) else None
        scoped = _ScopedRequest(env=request.env, cwd=request.cwd, client=_Client(request.client_ppid))
        with request_scope(scoped, session_id) as buffers:
            if parse_error is not None:
                buffers.stderr.write(f"Malformed stdin: {parse_error}\n")
                return self._response(buffers)
            try:
                self._dispatch(request, event, raw, session_id, buffers)
            except SystemExit as exc:
                return self._response(buffers, exit_code=_exit_code(exc.code))
            except Exception:
                buffers.stderr.write(traceback.format_exc())
                return self._response(buffers, status="error", exit_code=1)
            return self._response(buffers)

    def close(self) -> None:
        if self._writer is None:
            return
        decision_writer.uninstall(self._writer)
        self._writer = None

    def _dispatch(
        self,
        request: EventRequest,
        event: Event,
        raw: Any,
        session_id: str | None,
        buffers: RequestBuffers,
    ) -> None:
        session_dir = ensure_session(_session(session_id)) if session_id else None
        snapshot = self._registry(request.root).get()
        buffers.stdout.write(snapshot.discovery_stdout)
        buffers.stderr.write(snapshot.discovery_stderr)
        with app.use_state(snapshot.state):
            output = self._dispatcher(
                Path(request.root),
                event,
                raw,
                session_dir=session_dir,
                async_=request.async_,
                transcript_loader=self._transcript_loader,
            )
        if output:
            buffers.stdout.write(json.dumps(output) + "\n")

    def _registry(self, root: str) -> RegistryLike:
        with self._registries_guard:
            if (registry := self._registries.get(root)) is None:
                from captain_hook.cli import CliState

                registry = self._registry_factory(CliState(root=Path(root)))
                self._registries[root] = registry
            return registry

    @staticmethod
    def _response(
        buffers: RequestBuffers,
        *,
        status: Literal["ok", "error"] = "ok",
        exit_code: int = 0,
    ) -> EventResponse:
        return EventResponse(
            status=status,
            stdout=buffers.stdout.getvalue(),
            stderr=buffers.stderr.getvalue(),
            exit=exit_code,
        )


def _session(session_id: str):
    from cc_transcript.ids import SessionId

    return SessionId(session_id)


def _exit_code(code: object) -> int:
    return code if type(code) is int else 0 if code is None else 1
