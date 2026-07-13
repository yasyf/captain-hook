"""Daemon lifecycle: build-id watchdog, idle exit, and signal-driven cache/stack callables.

The server owns the socket and the signal registrations; this module owns the decisions behind
them, kept pure so they can be unit-tested without threads or real timeouts. :func:`build_id`
identifies the running code — a wheel's version, or that version plus a content digest over the two
source trees for an editable checkout, so a dogfood edit is detectable. :class:`Watchdog` restarts
the daemon (via ``execv``) only after the build mismatches on two consecutive ticks, debouncing a
torn ``git`` checkout. Idle exit and the SIGHUP cache drop / SIGUSR1 stack dump round out the set.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from captain_hook.util.caching import ttl_cache

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from captain_hook.daemon.registry import Registry

DIST_NAME = "capt-hook"
BUILD_SCAN_TTL = 2.0
TICK_SECONDS = 2.0
DEBOUNCE_TICKS = 2
DEFAULT_IDLE_S = 120 * 60


def source_roots() -> tuple[Path, ...]:
    import capt_hook_client
    import captain_hook

    return (Path(captain_hook.__file__).resolve().parent, Path(capt_hook_client.__file__).resolve().parent)


def is_checkout(root: Path) -> bool:
    return not any(part in ("site-packages", "dist-packages") for part in root.parts)


def source_digest(roots: Sequence[Path]) -> str:
    entries = sorted(
        (root.name, str(path.relative_to(root)), st.st_mtime_ns, st.st_ctime_ns, st.st_size)
        for root in roots
        for path in root.rglob("*.py")
        if (st := path.stat())
    )
    return hashlib.sha256(repr(entries).encode()).hexdigest()[:16]


@ttl_cache(BUILD_SCAN_TTL)
def build_id() -> str:
    version = importlib.metadata.version(DIST_NAME)
    roots = source_roots()
    return f"{version}+{source_digest(roots)}" if is_checkout(roots[0]) else version


def should_restart(recorded: str, current: str, consecutive: int) -> tuple[int, bool]:
    if current == recorded:
        return 0, False
    return (nxt := consecutive + 1), nxt >= DEBOUNCE_TICKS


def idle_limit() -> float:
    return float(os.environ.get("HOOKS_DAEMON_IDLE_S") or DEFAULT_IDLE_S)


def idle_expired(last_activity: float, now: float, limit: float) -> bool:
    return now - last_activity >= limit


def drop_caches(registry: Registry) -> None:
    from captain_hook.daemon import transcache
    from captain_hook.review.repo import resolve_repo_key
    from captain_hook.signals.nlp import parse
    from captain_hook.util.http import github_token
    from captain_hook.util.proc import _SKIP_CACHE

    registry.drop_all()
    transcache.cache_clear()
    github_token.cache_clear()
    resolve_repo_key.cache_clear()
    _SKIP_CACHE.cache_clear()
    parse.cache_clear()


def format_stacks() -> str:
    import sys
    import traceback

    frames = sys._current_frames()
    chunks: list[str] = []
    for thread in threading.enumerate():
        chunks.append(f"# thread {thread.name} ({thread.ident})\n")
        if (frame := frames.get(thread.ident)) is not None:
            chunks.extend(traceback.format_stack(frame))
    return "".join(chunks)


def dump_stacks() -> None:
    logger.warning("SIGUSR1 stack dump:\n{stacks}", stacks=format_stacks())


def reexec(argv: Sequence[str]) -> None:
    os.execv(argv[0], list(argv))


class Watchdog:
    def __init__(self, recorded: str, on_restart: Callable[[], None], *, interval: float = TICK_SECONDS) -> None:
        self._recorded = recorded
        self._on_restart = on_restart
        self._interval = interval
        self._consecutive = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="capt-hook-watchdog", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._consecutive, restart = should_restart(self._recorded, build_id(), self._consecutive)
            if restart:
                logger.warning("build changed for two consecutive ticks; restarting the daemon")
                self._on_restart()
                return
