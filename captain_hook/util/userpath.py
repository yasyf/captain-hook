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


class LoginShellError(Exception):
    """The user's login shell could not be asked for its ``PATH``."""


def login_shell() -> str:
    """The user's shell from the passwd database — a daemon has no ``SHELL`` to read."""
    return pwd.getpwuid(os.getuid()).pw_shell


def login_path() -> str:
    """The ``PATH`` the user's login shell exports.

    Probes ``printenv PATH`` rather than echoing ``$PATH``: ``printenv`` is an external command, so
    the answer is the exported value in every shell dialect (fish expands a quoted ``$PATH`` to a
    space-joined list). The last non-empty line is the answer, since a login shell may print a
    banner first. Raises :class:`LoginShellError` when the shell cannot be run, times out, exits
    nonzero, or prints nothing.
    """
    shell = login_shell()
    try:
        probe = subprocess.run(
            [shell, "-l", "-c", "printenv PATH"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise LoginShellError(f"login shell {shell!r} could not be probed: {e}") from e
    if probe.returncode != 0:
        raise LoginShellError(f"login shell {shell!r} exited {probe.returncode}: {probe.stderr.strip()}")
    if (path := next((line for line in reversed(probe.stdout.splitlines()) if line.strip()), None)) is None:
        raise LoginShellError(f"login shell {shell!r} exported no PATH")
    return path


def merged_path(inherited: str, login: str) -> str:
    """``login`` first, then every inherited entry it does not already carry."""
    entries = login.split(os.pathsep)
    return os.pathsep.join([*entries, *(e for e in inherited.split(os.pathsep) if e and e not in entries)])
