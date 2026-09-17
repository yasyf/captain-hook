from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

ENV_PREFIXES = ("CAPT_HOOK_", "CAPTAIN_HOOK_", "HOOKS_", "CLAUDE_", "FACTORY_")
ENV_EXACT = frozenset({"XDG_CACHE_HOME", "CEREBRAS_API_KEY"})

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping


@dataclass(frozen=True, slots=True)
class RequestOverrides:
    env: Mapping[str, str]
    cwd: str
    client_ppid: int
    session_id: str
    deadline_unix_ms: int = 0
    abandoned: list[str] = field(default_factory=list)


class Abandoned(BaseException):
    """Raised at a :func:`checkpoint` once dispatch has stopped waiting for the running hook's verdict.

    A ``BaseException``, like ``asyncio.CancelledError``, so a handler's own ``except Exception``
    cannot swallow the unwind.
    """


_OVERRIDES: ContextVar[RequestOverrides | None] = ContextVar("captain_hook_request", default=None)
_ABANDONED: ContextVar[threading.Event | None] = ContextVar("captain_hook_abandoned", default=None)


def is_whitelisted(key: str) -> bool:
    return key in ENV_EXACT or key.startswith(ENV_PREFIXES)


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


def abandoned() -> list[str]:
    """The hooks whose verdicts the bound request's dispatch gave up on; a scratch list for the cold CLI."""
    return [] if (ov := _OVERRIDES.get()) is None else ov.abandoned


@contextmanager
def abandonable(flag: threading.Event) -> Generator[None]:
    """Bind *flag* as the signal that stops the hooks running in this context at their next :func:`checkpoint`."""
    token = _ABANDONED.set(flag)
    try:
        yield
    finally:
        _ABANDONED.reset(token)


def checkpoint() -> None:
    """Unwind the running hook once its verdict can no longer be delivered; a no-op outside a hook fan-out."""
    if (flag := _ABANDONED.get()) is not None and flag.is_set():
        raise Abandoned


def is_headless() -> bool:
    """True in a headless ``claude -p`` / SDK run (``CLAUDE_CODE_ENTRYPOINT`` in the ``sdk-*`` family)."""
    return (getenv("CLAUDE_CODE_ENTRYPOINT") or "").startswith("sdk")
