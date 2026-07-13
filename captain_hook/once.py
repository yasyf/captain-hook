"""Duplicate-dispatch guard for ``capt-hook run <Event>``.

Claude Code runs the byte-identical ``uvx --isolated capt-hook run <Event>`` command once per
registering source (project settings plus each plugin), so one event fans out into
N sibling processes reading identical stdin within milliseconds of each other. A
side-effecting hook then fires N times. The guard lets the first sibling win an
``O_EXCL`` sentinel keyed by the event, its sync/async variant, and its payload; the
rest exit silently.

Best-effort only: no locking beyond the atomic create, no daemon. A TTL bounds how
long a claim suppresses siblings (covering uvx's multi-second startup skew) and how
long a crashed claim keeps blocking before the next process reclaims it.

The sentinel dir lives under the user-stable cache root (:func:`resolve_cache_dir`,
``$XDG_CACHE_HOME/captain-hook/once``), not ``$TMPDIR``: siblings that spawn with divergent
temp dirs — a resident daemon vs a cold CLI, an IDE vs a terminal vs launchd — must resolve
the same sentinel to dedupe the same claim, and ``XDG_CACHE_HOME`` is both reqenv-routed and
part of the daemon worker key, so every process of one worker agrees by construction.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import time
from pathlib import Path

from captain_hook.util import reqenv
from captain_hook.util.paths import resolve_cache_dir

DIR_NAME = "once"
DEFAULT_TTL = 10.0
TTL_ENV = "CAPT_HOOK_ONCE_TTL"


def claim_once(event_name: str, payload: bytes, *, async_: bool) -> bool:
    """Claim the one-time dispatch of ``payload`` under ``event_name``'s ``async_`` variant.

    Returns True when this process won the claim and should dispatch, False when a
    still-fresh sibling already claimed it (a duplicate that must exit silently). The sync
    and async passes of one event dispatch disjoint hook sets (``dispatch`` filters on
    ``spec.async_``), so ``async_`` is part of the key: each variant claims its own token and
    both run even on byte-identical stdin. ``CAPT_HOOK_ONCE_TTL=0`` disables the guard, so
    every call wins.
    """
    ttl = _ttl()
    if ttl <= 0:
        return True
    sentinel_dir = _sentinel_dir()
    if sentinel_dir is None:
        return True
    _reap(sentinel_dir, ttl)
    variant = b"async" if async_ else b"sync"
    key = hashlib.sha256(event_name.encode() + b"\0" + variant + b"\0" + payload).hexdigest()
    return _try_claim(sentinel_dir / key, ttl)


def _ttl() -> float:
    return float(reqenv.getenv(TTL_ENV, DEFAULT_TTL))


def _sentinel_dir() -> Path | None:
    """The sentinel dir under the cache root, or None when it is unsafe to trust.

    Created 0o700; if the existing entry is a symlink or owned by another uid, the guard
    is skipped (None → the caller dispatches; fail-open) rather than trusting a dir a
    hostile local user may have planted.
    """
    directory = resolve_cache_dir() / DIR_NAME
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
        return None
    return directory


def _try_claim(sentinel: Path, ttl: float) -> bool:
    if _create(sentinel):
        return True
    try:
        prior = sentinel.stat()
    except FileNotFoundError:
        return _create(sentinel)
    if time.time() - prior.st_mtime < ttl:
        return False
    return _reclaim(sentinel, prior)


def _create(sentinel: Path) -> bool:
    try:
        os.close(os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return False
    return True


def _reclaim(sentinel: Path, prior: os.stat_result) -> bool:
    """Drop a stale sentinel from a crashed sibling and retry the claim once.

    Re-stat and unlink only when the sentinel is still the exact stale file judged stale
    (same st_ino and st_mtime), so a sentinel a racing claimant just recreated is never
    removed. A microsecond stat->unlink window remains; its failure mode is fail-open
    (both siblings dispatch), never fail-closed (a real event swallowed). On a lost race
    to recreate (EEXIST) we are the duplicate.
    """
    try:
        current = sentinel.stat()
    except FileNotFoundError:
        return False
    if (current.st_ino, current.st_mtime) != (prior.st_ino, prior.st_mtime):
        return False
    try:
        sentinel.unlink()
    except OSError:
        return False
    return _create(sentinel)


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
