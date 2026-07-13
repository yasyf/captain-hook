from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from capt_hook_client.key import ENV_EXACT, ENV_PREFIXES

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


@dataclass(frozen=True, slots=True)
class RequestOverrides:
    env: Mapping[str, str]
    cwd: str
    client_ppid: int
    session_id: str


_OVERRIDES: ContextVar[RequestOverrides | None] = ContextVar("captain_hook_request", default=None)


def is_whitelisted(key: str) -> bool:
    return key in ENV_EXACT or key.startswith(ENV_PREFIXES)


def current() -> RequestOverrides | None:
    return _OVERRIDES.get()


@contextmanager
def use_request(overrides: RequestOverrides) -> Iterator[RequestOverrides]:
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
