"""Duplicate-dispatch guard for ``capt-hook run <Event>``.

Claude Code runs the byte-identical ``uvx capt-hook run <Event>`` command once per
registering source (project settings plus each plugin), so one event fans out into
N sibling processes reading identical stdin within milliseconds of each other. A
side-effecting hook then fires N times. The guard lets the first sibling win an
``O_EXCL`` sentinel keyed by the event and its payload; the rest exit silently.

Best-effort only: no locking beyond the atomic create, no daemon. A TTL bounds how
long a claim suppresses siblings (covering uvx's multi-second startup skew) and how
long a crashed claim keeps blocking before the next process reclaims it.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
import time
from pathlib import Path

DIR_NAME = "capt-hook-once"
DEFAULT_TTL = 10.0
TTL_ENV = "CAPT_HOOK_ONCE_TTL"


def claim_once(event_name: str, payload: bytes) -> bool:
    """Claim the one-time dispatch of ``payload`` under ``event_name``.

    Returns True when this process won the claim and should dispatch, False when a
    still-fresh sibling already claimed it (a duplicate that must exit silently).
    ``CAPT_HOOK_ONCE_TTL=0`` disables the guard, so every call wins.
    """
    ttl = _ttl()
    if ttl <= 0:
        return True
    sentinel_dir = _sentinel_dir()
    _reap(sentinel_dir, ttl)
    key = hashlib.sha256(event_name.encode() + b"\0" + payload).hexdigest()
    return _try_claim(sentinel_dir / key, ttl)


def _ttl() -> float:
    return float(os.environ.get(TTL_ENV, DEFAULT_TTL))


def _sentinel_dir() -> Path:
    directory = Path(tempfile.gettempdir()) / DIR_NAME
    directory.mkdir(exist_ok=True)
    return directory


def _try_claim(sentinel: Path, ttl: float) -> bool:
    if _create(sentinel):
        return True
    if _fresh(sentinel, ttl):
        return False
    # A stale sentinel from a crashed sibling: drop it and retry the create once.
    # Losing that race to another reclaimer means we are the duplicate.
    with contextlib.suppress(OSError):
        sentinel.unlink()
    return _create(sentinel)


def _create(sentinel: Path) -> bool:
    try:
        os.close(os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return False
    return True


def _fresh(sentinel: Path, ttl: float) -> bool:
    try:
        return time.time() - sentinel.stat().st_mtime < ttl
    except FileNotFoundError:
        return False


def _reap(sentinel_dir: Path, ttl: float) -> None:
    cutoff = time.time() - max(3 * ttl, 60.0)
    try:
        entries = list(sentinel_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        with contextlib.suppress(OSError):
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
