from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping, Sequence

    from captain_hook.types import HookResult


@dataclass(slots=True)
class MandatoryPhase:
    """How the bound request's mandatory phase ended: ``settled`` with every verdict in hand, or ``failed``.

    Concluded exactly once, after the phase's cutoff closed, so the worker reads the phase's own
    verdict rather than inferring one from the completions a racing hook may still publish. It
    keeps the verdict the settled hooks reached, the first block, else the first rewrite, else the
    first allow, so a failure after the verdict cannot drop it or the grant a rewrite carries, and
    ``unchecked``, which maps the hooks that never decided and why to the result letting the call
    through without them.
    """

    outcome: Literal["", "settled", "failed"] = ""
    verdict: HookResult | None = None
    unchecked: Callable[[Sequence[str], str], HookResult | None] | None = None

    @property
    def failed(self) -> bool:
        return self.outcome == "failed"

    def conclude(self, outcome: Literal["settled", "failed"], *, verdict: HookResult | None = None) -> None:
        self.outcome = outcome
        self.verdict = verdict


@dataclass(slots=True)
class RequestMemo:
    lock: threading.Lock = field(default_factory=threading.Lock)
    claude_argv: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class RequestOverrides:
    env: Mapping[str, str]
    cwd: str
    client_ppid: int
    session_id: str
    deadline_unix_ms: int = 0
    abandon: threading.Event = field(default_factory=threading.Event)
    abandoned: list[str] = field(default_factory=list[str])
    evidence_gaps: list[str] = field(default_factory=list[str])
    warmups: list[str] = field(default_factory=list[str])
    mandatory_completed: list[str] = field(default_factory=list[str])
    memo: RequestMemo = field(default_factory=RequestMemo)
    mandatory_phase: MandatoryPhase = field(default_factory=MandatoryPhase)


class Abandoned(BaseException):
    """Raised at a :func:`checkpoint` once dispatch has stopped waiting for the running hook's verdict.

    A ``BaseException``, like ``asyncio.CancelledError``, so a handler's own ``except Exception``
    cannot swallow the unwind.
    """


class Cutoff(threading.Event):
    """The signal that nobody waits on the running hooks any more: closed by the collector, or reached on the clock.

    A :func:`checkpoint` under it unwinds once the collector gave up, or once the absolute
    *deadline_unix_ms* passed while a descheduled collector had not yet said so. :meth:`close`
    and :meth:`publish` share one lock, so what a hook publishes (its completion, its settled
    future) either lands whole before the closure or not at all; the ledger row and the fire
    slot follow an accepted publication on the hook's own thread, so nothing under the lock
    blocks. Two checks keep what the closer counts and what a publisher keeps in agreement.
    Under the lock, a publication whose settlement ends past the cutoff, whether the clock or
    the closer got there first, is marked late and refused, so a delayed closer that finds the
    lock free still sees it. After the lock, a publication that left it once the closure began
    waits for the closer's outcome and is refused unless the closer took the lock and found
    nothing late, so a publisher the closer gave up on while it still held the lock is refused
    too. A refused publication never keeps a row or a slot. :meth:`close` waits for a
    publication in flight no longer than its *timeout*, sets the flag either way, and returns
    whether everything it closed over settled in time: ``False`` when a publisher outlived the
    bound inside the lock or settled late before it, so the closer must not count what it then
    finds settled.
    """

    def __init__(self, deadline_unix_ms: int | None = None) -> None:
        super().__init__()
        self.deadline_unix_ms = deadline_unix_ms
        self._closure = threading.Lock()
        self._late = False
        self._closing = threading.Event()
        self._decided = threading.Event()
        self._counted = True

    def is_set(self) -> bool:
        return super().is_set() or (self.deadline_unix_ms is not None and time.time() * 1000 >= self.deadline_unix_ms)

    def seconds_left(self) -> float | None:
        return None if self.deadline_unix_ms is None else max(0.0, self.deadline_unix_ms / 1000 - time.time())

    def close(self, *, timeout: float | None = None) -> bool:
        self._closing.set()
        held = self._closure.acquire(timeout=-1 if timeout is None else timeout)
        self._counted = held and not self._late
        self.set()
        self._decided.set()
        if held:
            self._closure.release()
        return self._counted

    def publish[T](self, fn: Callable[[], T]) -> T:
        with self._closure:
            if self.is_set():
                raise Abandoned
            published = fn()
            if self.is_set():
                self._late = True
                raise Abandoned
        if self._closing.is_set():
            self._decided.wait()
            if not self._counted:
                raise Abandoned
        return published


_OVERRIDES: ContextVar[RequestOverrides | None] = ContextVar("captain_hook_request", default=None)
_ABANDONED: ContextVar[Cutoff | None] = ContextVar("captain_hook_abandoned", default=None)


def is_whitelisted(key: str) -> bool:
    return key in {"XDG_CACHE_HOME", "CEREBRAS_API_KEY"} or key.startswith(
        ("CAPT_HOOK_", "CAPTAIN_HOOK_", "HOOKS_", "CLAUDE_", "FACTORY_", "ORCA_")
    )


def current() -> RequestOverrides | None:
    return _OVERRIDES.get()


@contextmanager
def use_request(overrides: RequestOverrides) -> Generator[RequestOverrides]:
    token = _OVERRIDES.set(overrides)
    try:
        yield overrides
    finally:
        _OVERRIDES.reset(token)


def getenv[T](key: str, default: str | T | None = None) -> str | T | None:
    if (ov := _OVERRIDES.get()) is not None and is_whitelisted(key):
        return ov.env.get(key, default)
    return os.environ.get(key, default)


def provider() -> Literal["claude", "codex"]:
    match getenv("CAPT_HOOK_PROVIDER", "claude"):
        case "claude":
            return "claude"
        case "codex":
            return "codex"
        case value:
            raise ValueError(f"unsupported hook provider: {value!r}")


def env_map() -> Mapping[str, str]:
    if (ov := _OVERRIDES.get()) is None:
        return os.environ
    return {k: v for k, v in os.environ.items() if not is_whitelisted(k)} | dict(ov.env)


def cwd() -> Path:
    return Path.cwd() if (ov := _OVERRIDES.get()) is None else Path(ov.cwd)


def seconds_left() -> float | None:
    """Seconds until the caller deadline; ``None`` for the cold CLI or an unbounded request."""
    if (ov := _OVERRIDES.get()) is None or ov.deadline_unix_ms == 0:
        return None
    return ov.deadline_unix_ms / 1000 - time.time()


def deadline_within(seconds: float) -> bool:
    """True once the caller deadline is *seconds* away or closer; never for the cold CLI or an unbounded request."""
    return (left := seconds_left()) is not None and left <= seconds


def clamp_timeout(timeout: int) -> int:
    """*timeout* cut down to the whole seconds left before the caller deadline, never below one."""
    return timeout if (left := seconds_left()) is None else max(1, min(timeout, int(left)))


@contextmanager
def deadline_in(seconds: float) -> Generator[None]:
    """Rebind the current request's deadline to *seconds* from now; the cold CLI stays unbounded."""
    if (ov := _OVERRIDES.get()) is None:
        yield
        return
    with use_request(replace(ov, deadline_unix_ms=int((time.time() + seconds) * 1000))):
        yield


@contextmanager
def deadline_at(unix_ms: int) -> Generator[None]:
    """Rebind the current request's deadline to the absolute *unix_ms*; the cold CLI stays unbounded."""
    if (ov := _OVERRIDES.get()) is None:
        yield
        return
    with use_request(replace(ov, deadline_unix_ms=unix_ms)):
        yield


def abandon_signal() -> threading.Event:
    """The flag the host sets once it stops waiting on the bound request's reply; a fresh flag for the cold CLI."""
    return threading.Event() if (ov := _OVERRIDES.get()) is None else ov.abandon


def abandoned() -> list[str]:
    """The hooks whose verdicts the bound request's dispatch gave up on; a scratch list for the cold CLI."""
    return [] if (ov := _OVERRIDES.get()) is None else ov.abandoned


def evidence_gaps() -> list[str]:
    """The hooks the bound request skipped for incomplete transcript evidence; a scratch list for the cold CLI."""
    return [] if (ov := _OVERRIDES.get()) is None else ov.evidence_gaps


def warmups() -> list[str]:
    """The one-time loads the bound request paid for, a registry build or an NLP resource; scratch for the cold CLI."""
    return [] if (ov := _OVERRIDES.get()) is None else ov.warmups


def warmed(resource: str) -> None:
    warmups().append(resource)


def mandatory_completed() -> list[str]:
    """The state keys of the ``mandatory=True`` hooks the bound request ran to a verdict; scratch for the cold CLI."""
    return [] if (ov := _OVERRIDES.get()) is None else ov.mandatory_completed


def note_mandatory_completed(state_key: str) -> None:
    mandatory_completed().append(state_key)


def mandatory_phase() -> MandatoryPhase:
    """The bound request's mandatory phase outcome; a scratch record for the cold CLI."""
    return MandatoryPhase() if (ov := _OVERRIDES.get()) is None else ov.mandatory_phase


@contextmanager
def abandonable(flag: Cutoff) -> Generator[None]:
    """Bind *flag* as the signal that stops the hooks running in this context at their next :func:`checkpoint`."""
    token = _ABANDONED.set(flag)
    try:
        yield
    finally:
        _ABANDONED.reset(token)


def checkpoint() -> None:
    """Unwind the running hook once its fan-out or host request stops waiting."""
    if (flag := _ABANDONED.get()) is not None and flag.is_set():
        raise Abandoned
    if (ov := _OVERRIDES.get()) is not None and ov.abandon.is_set():
        raise Abandoned


def publish[T](fn: Callable[[], T]) -> T:
    """Run *fn* while the running hook's verdict can still be delivered, atomically with that decision.

    Under a bound :class:`Cutoff` the check and the call share the cutoff's closure lock, so a
    closure cannot slip between them; outside a hook fan-out *fn* simply runs.
    """
    flag = _ABANDONED.get()
    return fn() if flag is None else flag.publish(fn)


def is_headless() -> bool:
    """True in a headless ``claude -p`` / SDK run (``CLAUDE_CODE_ENTRYPOINT`` in the ``sdk-*`` family)."""
    return (getenv("CLAUDE_CODE_ENTRYPOINT") or "").startswith("sdk")
