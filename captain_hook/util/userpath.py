"""The user's own command ``PATH``, resolved without asking the parent process.

launchd starts a LaunchAgent with ``PATH=/usr/bin:/bin:/usr/sbin:/sbin`` and daemonkit hands that
same value to the worker it spawns, so every user-installed CLI the product shells out to —
``claude``, ``codex``, ``gh`` — is invisible to the daemon and to everything it spawns: plugin
discovery skips a ``claude`` it cannot find and spawnllm's backend selection raises
``BackendUnavailable`` in the detached reviewer, while the identical probe from a terminal
succeeds. The user's login shell is the one authority on where their commands live that a daemon
can still ask, and it is asked through the passwd database rather than ``$SHELL``, which launchd
does not set. Asking is also the most expensive thing a worker does before it is ready, so the
answer is cached and the shell is run only when no fresh record exists.
"""

from __future__ import annotations

import json
import os
import pwd
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger

from captain_hook.util.fs import atomic_write, read_json
from captain_hook.util.paths import resolve_cache_dir

# Inside internal/hookd's 10s workerReadinessTimeout: the probe precedes the readiness
# handshake, so a slower shell costs the daemon a whole worker, not just the user's PATH.
# Only a worker with no cached answer to fall back to spends this much.
PROBE_TIMEOUT_SECONDS = 5

# A bound under the shell's real cost never completes: it times out, falls back, and leaves the
# record stale for the next worker to retry. A login fish measures 1.43-1.50s.
REFRESH_TIMEOUT_SECONDS = 3

CACHE_TTL = timedelta(hours=12)

PATH_TAG = "capt-hook-login-path:"


class LoginShellError(Exception):
    """The user's login shell could not be asked for its ``PATH``."""


def login_shell() -> str:
    """The user's shell from the passwd database — a daemon has no ``SHELL`` to read."""
    return pwd.getpwuid(os.getuid()).pw_shell


def login_path(timeout: float) -> str:
    """The ``PATH`` the user's login shell exports, within *timeout* seconds.

    Probes ``printenv PATH`` rather than echoing ``$PATH``: ``printenv`` is an external command, so
    the answer is the exported value in every shell dialect (fish expands a quoted ``$PATH`` to a
    space-joined list). The value is tagged and found by its tag, not by position: a login shell
    prints banners before the command and ``.zlogout`` prints after it, so neither the first line
    nor the last is reliably the answer. Raises :class:`LoginShellError` when the shell cannot be
    run, times out, exits nonzero, or prints no tagged line.
    """
    shell = login_shell()
    try:
        probe = subprocess.run(
            [shell, "-l", "-c", f"printenv PATH | sed 's/^/{PATH_TAG}/'"],
            capture_output=True,
            text=True,
            timeout=timeout,
            # An rc file that reads stdin would otherwise consume hookd's length-prefixed hello frame.
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise LoginShellError(f"login shell {shell!r} could not be probed: {e}") from e
    if probe.returncode != 0:
        raise LoginShellError(f"login shell {shell!r} exited {probe.returncode}: {probe.stderr.strip()}")
    tagged = (line for line in probe.stdout.splitlines() if line.startswith(PATH_TAG))
    if (path := next(tagged, None)) is None:
        raise LoginShellError(f"login shell {shell!r} exported no PATH")
    return path.removeprefix(PATH_TAG)


def cache_file() -> Path:
    return resolve_cache_dir() / "login-path.json"


@dataclass(frozen=True)
class CachedPath:
    """A ``PATH`` an earlier probe returned, and whether it is still within :data:`CACHE_TTL`."""

    value: str
    fresh: bool


def read_cache(shell: str) -> CachedPath | None:
    """The cached ``PATH`` for *shell*, or ``None`` when nothing usable is on disk.

    A record naming a different shell is a miss rather than a fallback: the user changed login
    shells, so the recorded ``PATH`` is another shell's answer and no longer theirs.
    """
    if (data := read_json(cache_file())) is None or data.get("shell") != shell:
        return None
    try:
        value, written = str(data["path"]), datetime.fromisoformat(data["at"])
    except (KeyError, TypeError, ValueError):
        return None
    return CachedPath(value, datetime.now(UTC) - written < CACHE_TTL)


def write_cache(shell: str, value: str) -> None:
    """Record *value* as *shell*'s exported ``PATH``.

    A cache that cannot be written costs the next worker a probe, never the worker itself, so the
    failure is logged rather than raised over a ``PATH`` that has already been resolved.
    """
    payload = {"shell": shell, "path": value, "at": datetime.now(UTC).isoformat()}
    try:
        atomic_write(cache_file(), json.dumps(payload))
    except OSError:
        logger.opt(exception=True).debug("login PATH cache write failed")


def user_path() -> str:
    """The user's login ``PATH``, without re-paying for an answer already on disk.

    Sourcing a user's profile is the most expensive thing a worker does before it is ready —
    measured at 1.5s idle and past :data:`PROBE_TIMEOUT_SECONDS` under a parallel agent fleet,
    against 0.45s for the worker's whole import graph — and it runs ahead of hookd's readiness
    handshake, so a shell that answers late costs the daemon a whole worker. The answer changes
    about as often as the user edits their profile, so a fresh record is adopted without running
    the shell at all, and a stale one bounds its refresh to :data:`REFRESH_TIMEOUT_SECONDS`
    because a refresh that fails behind a usable record loses nothing. Raises
    :class:`LoginShellError` only when the probe fails with no record to fall back to.
    """
    shell = login_shell()
    cached = read_cache(shell)
    if cached is not None and cached.fresh:
        return cached.value
    try:
        value = login_path(REFRESH_TIMEOUT_SECONDS if cached else PROBE_TIMEOUT_SECONDS)
    except LoginShellError:
        if cached is None:
            raise
        return cached.value
    write_cache(shell, value)
    return value


def usable_entries(value: str) -> list[str]:
    """The entries of ``value`` a daemon may search, in order.

    POSIX resolves an empty entry against the current directory, and a worker's cwd is the session's
    own repository — so an empty or relative entry anywhere in the user's PATH would let a file
    committed to the repository under review answer a bare ``claude`` or ``gh`` lookup. Only
    absolute entries survive.
    """
    return [e for e in value.split(os.pathsep) if e and os.path.isabs(e)]


def merged_path(inherited: str, login: str) -> str:
    """``login`` first, then every inherited entry it does not already carry."""
    entries = usable_entries(login)
    return os.pathsep.join([*entries, *(e for e in usable_entries(inherited) if e not in entries)])
