"""The user's own command ``PATH``, resolved without asking the parent process.

launchd starts a LaunchAgent with ``PATH=/usr/bin:/bin:/usr/sbin:/sbin`` and daemonkit hands that
same value to the worker it spawns, so every user-installed CLI the product shells out to —
``claude``, ``codex``, ``gh`` — is invisible to the daemon and to everything it spawns: plugin
discovery skips a ``claude`` it cannot find and spawnllm's backend selection raises
``BackendUnavailable`` in the detached reviewer, while the identical probe from a terminal
succeeds. The user's login shell is the one authority on where their commands live that a daemon
can still ask, and it is asked through the passwd database rather than ``$SHELL``, which launchd
does not set.
"""

from __future__ import annotations

import os
import pwd
import subprocess

# Inside internal/hookd's 10s workerReadinessTimeout: the probe precedes the readiness
# handshake, so a slower shell costs the daemon a whole worker, not just the user's PATH.
PROBE_TIMEOUT_SECONDS = 5

PATH_TAG = "capt-hook-login-path:"


class LoginShellError(Exception):
    """The user's login shell could not be asked for its ``PATH``."""


def login_shell() -> str:
    """The user's shell from the passwd database — a daemon has no ``SHELL`` to read."""
    return pwd.getpwuid(os.getuid()).pw_shell


def login_path() -> str:
    """The ``PATH`` the user's login shell exports.

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
            timeout=PROBE_TIMEOUT_SECONDS,
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
