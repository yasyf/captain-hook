from __future__ import annotations

import contextvars
import json
import threading
import time
import traceback
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from loguru import logger

from captain_hook import app
from captain_hook.cli import EVENT_NAMES, dispatch_event
from captain_hook.daemon import decision_writer
from captain_hook.daemon.context import RequestBuffers, capture_output, request_scope
from captain_hook.daemon.registry import Registry
from captain_hook.dispatch import denies, envelope_text, format_output, mandatory_completions
from captain_hook.session import ensure_session
from captain_hook.snapshots.client import CURRENT_CLIENT, EvidenceIncomplete, fails_open
from captain_hook.state import RESOURCES
from captain_hook.transcripts import load_transcript
from captain_hook.types import Event, RegisteredHook
from captain_hook.util import reqenv
from captain_hook.worker.fail_open import ALL_HOOKS, fail_open_envelope, tally_fail_open, with_warning
from captain_hook.worker.protocol import GUARD_COMPLETED, EventRequest, EventResponse, GuardCompletion
from captain_hook.worker.service import BACKGROUND_SNAPSHOT_CLIENT

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from typing import Protocol

    type Background = Callable[[], None]

    from captain_hook.cli import CliState
    from captain_hook.dispatch import Envelope

    class RegistryLike(Protocol):
        def get(self) -> Any: ...


GUARD_PACKS = frozenset({"general"})


@dataclass(frozen=True, slots=True)
class _Client:
    ppid: int


@dataclass(frozen=True, slots=True)
class _ScopedRequest:
    env: dict[str, str]
    cwd: str
    client: _Client
    deadline_unix_ms: int
    abandon: threading.Event


class ProductRuntime:
    def __init__(
        self,
        *,
        registry_factory: Callable[[CliState], RegistryLike] = Registry,
        dispatcher: Callable[..., tuple[Envelope | None, Background]] = dispatch_event,
        transcript_loader: Callable[..., Any] = load_transcript,
        install_writer: bool = True,
        nlp_warmer: Callable[[], None] = RESOURCES.warm,
    ) -> None:
        self._registry_factory = registry_factory
        self._dispatcher = dispatcher
        self._transcript_loader = transcript_loader
        self._registries: dict[str, RegistryLike] = {}
        self._registries_guard = threading.Lock()
        self._guarded_events: dict[str, frozenset[str]] = {}
        self._writer = decision_writer.install() if install_writer else None
        self._nlp_warmup = threading.Thread(
            target=_warm_nlp, args=(nlp_warmer,), name="capt-hook-nlp-warm", daemon=True
        )
        self._nlp_warmup_guard = threading.Lock()

    def guarded(self, request: EventRequest) -> bool:
        """Whether *request* takes the reserved lane: the client flagged it, or the loaded registry guards its event."""
        return request.mandatory or request.event in self._guarded_events.get(request.root, frozenset())

    def dispatch(self, request: EventRequest) -> tuple[EventResponse, Background | None]:
        started = time.perf_counter()
        abandoned: list[str] = []
        warmups: list[str] = []
        try:
            response, background = self._respond(request, abandoned, warmups)
            return replace(response, warmup=bool(warmups)), background
        finally:
            logger.bind(
                event=request.event,
                root=request.root,
                client_pid=request.client_pid,
                queue_ms=round((started - request.received) * 1000, 1),
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                abandoned=abandoned,
                warmups=warmups,
            ).info("dispatch")

    def _respond(
        self, request: EventRequest, abandoned: list[str], warmups: list[str]
    ) -> tuple[EventResponse, Background | None]:
        try:
            event = Event[request.event]
        except KeyError:
            return EventResponse(
                stderr=f"Invalid event type: {request.event!r}. Valid event names are: {EVENT_NAMES}\n",
                exit=1,
            ), None
        if not request.payload_raw.strip():
            return EventResponse(), None
        try:
            raw = cast(object, json.loads(request.payload_raw))
            parse_error = None
        except (json.JSONDecodeError, ValueError) as exc:
            raw = None
            parse_error = exc
        raw_dict = cast(dict[str, Any], raw) if isinstance(raw, dict) else None
        session_id = raw_dict.get("session_id") if raw_dict is not None else None
        session_id = session_id if isinstance(session_id, str) else None
        scoped = _ScopedRequest(
            env=request.env,
            cwd=request.cwd,
            client=_Client(request.client_ppid),
            deadline_unix_ms=request.deadline_unix_ms,
            abandon=request.abandon,
        )
        with request_scope(scoped, session_id) as buffers:
            if parse_error is not None:
                buffers.stderr.write(f"Malformed stdin: {parse_error}\n")
                return self._response(buffers), None
            try:
                background, guard = self._dispatch(request, event, raw, session_id, buffers)
            except SystemExit as exc:
                return self._response(buffers, exit_code=_exit_code(exc.code)), None
            except EvidenceIncomplete as exc:
                if fails_open(exc):
                    warning = (
                        tally_fail_open(event, session_id, [f"{ALL_HOOKS}: {exc.status}: {exc.reason}"])
                        if session_id
                        else None
                    )
                    envelope = fail_open_envelope(event, warning) if warning else None
                    return EventResponse(stdout=envelope_text(envelope) + "\n" if envelope else ""), None
                buffers.stderr.write(traceback.format_exc())
                return self._response(buffers, status="error", exit_code=1), None
            except Exception:
                buffers.stderr.write(traceback.format_exc())
                return self._response(buffers, status="error", exit_code=1), None
            finally:
                abandoned.extend(reqenv.abandoned())
                warmups.extend(reqenv.warmups())
            return self._response(buffers, guard=guard), background

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
    ) -> tuple[Background, GuardCompletion]:
        session_dir = ensure_session(_session(session_id)) if session_id else None
        snapshot = self._registry(request.root).get()
        if (foreground := CURRENT_CLIENT.get()) is not None:
            foreground.bind_tool_registry(snapshot.tools)
        if (background_client := BACKGROUND_SNAPSHOT_CLIENT.get()) is not None:
            background_client.bind_tool_registry(snapshot.tools)
        buffers.stdout.write(snapshot.discovery_stdout)
        buffers.stderr.write(snapshot.discovery_stderr)
        with app.use_state(snapshot.state):
            self._guarded_events[request.root] = guarded_events(snapshot.state)
            required = mandatory_completions(event)
            try:
                output, background = self._dispatcher(
                    Path(request.root),
                    event,
                    raw,
                    session_dir=session_dir,
                    transcript_loader=self._transcript_loader,
                )
            except (Exception, SystemExit) as exc:
                unfinished = unfinished_mandatory(required)
                blocked = reqenv.mandatory_phase().blocked
                if not unfinished and blocked is None:
                    raise
                logger.bind(hooks=[hook.name for hook in unfinished]).opt(exception=True).error(
                    "dispatch failed; keeping the mandatory verdict and skipping unfinished hooks"
                )
                buffers.stderr.write(traceback.format_exc())
                verdict = format_output(event, blocked) if blocked is not None else None
                cause = f"{type(exc).__name__}: {exc}"
                output = mandatory_skip(event, verdict, unfinished, cause) if unfinished else verdict
                background = _nothing
            else:
                if unfinished := unfinished_mandatory(required):
                    output = mandatory_skip(event, output, unfinished, "left unrun")
            context = contextvars.copy_context()
            guard = _guard_completion(event) if request.mandatory and not unfinished else ""
        if session_id and (gaps := reqenv.evidence_gaps()) and (warning := tally_fail_open(event, session_id, gaps)):
            output = with_warning(event, output, warning)
        if output:
            buffers.stdout.write(envelope_text(output) + "\n")
        return lambda: self._after_reply(context, background, session_id), guard

    def _after_reply(self, context: contextvars.Context, background: Background, session_id: str | None) -> None:
        with self._nlp_warmup_guard:
            if self._nlp_warmup.ident is None:
                self._nlp_warmup.start()
        context.run(_run_detached, background, session_id)

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
        guard: GuardCompletion = "",
    ) -> EventResponse:
        return EventResponse(
            status=status,
            stdout=buffers.stdout.getvalue(),
            stderr=buffers.stderr.getvalue(),
            exit=exit_code,
            guard=guard,
        )


def _guard_completion(event: Event) -> GuardCompletion:
    state = app.current_state()
    guard_packs = GUARD_PACKS | {hook.pack_name for hook in state.hooks if hook.spec.mandatory and hook.pack_name}
    if any(error.pack in guard_packs for error in state.load_errors):
        return ""
    guards = app.get_mandatory_hooks(event)
    if not any(hook.pack_name in GUARD_PACKS for hook in guards):
        return ""
    completed = Counter(reqenv.mandatory_completed())
    required = mandatory_completions(event)
    return GUARD_COMPLETED if all(completed[key] == 1 for key in required) else ""


def unfinished_mandatory(required: Mapping[str, RegisteredHook]) -> list[RegisteredHook]:
    completed = Counter(reqenv.mandatory_completed())
    unfinished = [hook for key, hook in required.items() if completed[key] != 1]
    return unfinished or (list(required.values()) if reqenv.mandatory_phase().failed else [])


def mandatory_skip(event: Event, output: Envelope | None, unfinished: Sequence[RegisteredHook], cause: str) -> Envelope:
    names = ", ".join(hook.name for hook in unfinished)
    message = (
        f"capt-hook: the mandatory hook {names} did not complete ({cause}) and did not check this call. "
        "Run `capt-hook logs` to see why."
    )
    return with_warning(event, output, message) if denies(output) else fail_open_envelope(event, message)


def guarded_events(state: app.State) -> frozenset[str]:
    return frozenset(event.name for hook in state.hooks if hook.spec.mandatory for event in hook.spec.events)


def _nothing() -> None:
    return None


def _run_detached(background: Background, session_id: str | None) -> None:
    token = CURRENT_CLIENT.set(BACKGROUND_SNAPSHOT_CLIENT.get())
    replied = len(reqenv.evidence_gaps())
    late: list[str] = []
    try:
        with capture_output():
            try:
                background()
            except EvidenceIncomplete as exc:
                if not fails_open(exc):
                    logger.bind(status=exc.status, reason=exc.reason).error("post-reply evidence incomplete")
                    raise
                late.append(f"{ALL_HOOKS}: {exc.status}: {exc.reason}")
            except Exception:
                logger.exception("post-reply dispatch failed")
    finally:
        CURRENT_CLIENT.reset(token)
    late = reqenv.evidence_gaps()[replied:] + late
    if session_id and late:
        tally_fail_open(None, session_id, late)


def _warm_nlp(warmer: Callable[[], None]) -> None:
    try:
        warmer()
    except Exception:
        logger.exception("NLP warm-up failed")


def _session(session_id: str):
    from cc_transcript.ids import SessionId

    return SessionId(session_id)


def _exit_code(code: object) -> int:
    return code if type(code) is int else 0 if code is None else 1
