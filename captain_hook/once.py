"""LEGACY SHIM — duplicate-dispatch guard for ``capt-hook run <Event>``.

Claude Code dedups hook commands per registering *source*, not globally (anthropics/
claude-code#76297): a pack-shipping consumer plugin that mirrors the canonical
``uvx --isolated capt-hook run <Event>`` command already wired by the captain-hook plugin
registers a second, byte-identical source, so one event fans out into N sibling processes
reading identical stdin within milliseconds. A side-effecting hook then fires N times. This
guard lets the first sibling win an ``O_EXCL`` sentinel keyed by the event, its sync/async
variant, and its payload; the rest exit silently. :func:`once_guard` wraps the claim and adds
the ``DECISION_EVENTS`` exemption; it is deliberately dumb — the claim is never released on a
raised dispatch, so a failure stays fail-closed for the TTL window rather than re-firing hooks
whose side effects may have already landed.

Best-effort only: no locking beyond the atomic create, no daemon. A TTL bounds how long a
claim suppresses siblings (covering uvx's multi-second startup skew) and how long a crashed
claim keeps blocking before the next process reclaims it.

The sentinel dir lives under the user-stable cache root (:func:`resolve_cache_dir`,
``$XDG_CACHE_HOME/captain-hook/once``), not ``$TMPDIR``: siblings that spawn with divergent
temp dirs — a resident daemon vs a cold CLI, an IDE vs a terminal vs launchd — must resolve
the same sentinel to dedupe the same claim, and ``XDG_CACHE_HOME`` is both reqenv-routed and
part of the daemon worker key, so every process of one worker agrees by construction.

DELETION MANIFEST — the attach-only pack contract makes this shim the ONLY thing collapsing
the duplicate dispatch a legacy consumer's mirrored ``run`` entries still cause. It exists
solely until the upstream per-source dedup fix (anthropics/claude-code#76297) ships at the
fleet-minimum Claude Code version AND legacy consumer plugin caches (which still mirror
``run`` entries) have decayed. When both hold, delete in one change:
  - this module (``captain_hook/once.py``) and ``tests/test_once.py``;
  - both call sites — the ``with once_guard(...)`` blocks in ``cli.run_event`` and
    ``daemon.server.CaptHookServer._run_event`` (which then dispatch unconditionally);
  - the ``DECISION_EVENTS`` exemption wiring, which lives here in :func:`once_guard` (the
    guard is the only reason dispatch knows about decision events; ``DECISION_EVENTS`` the
    frozenset stays in ``cli`` for the async-decision registration guard);
  - the ``CAPT_HOOK_ONCE_TTL`` env knob (:data:`TTL_ENV`) and its docs.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from captain_hook.util import reqenv
from captain_hook.util.paths import resolve_cache_dir

if TYPE_CHECKING:
    from captain_hook.types import Event

DIR_NAME = "once"
DEFAULT_TTL = 10.0
TTL_ENV = "CAPT_HOOK_ONCE_TTL"


@contextlib.contextmanager
def once_guard(event: Event, event_name: str, payload: bytes, *, async_: bool) -> Iterator[bool]:
    """Yield whether this process should dispatch ``event``, collapsing byte-identical siblings.

    Decision-capable events (``DECISION_EVENTS``) are exempt — swallowing a sibling there could
    bypass a gate, which outweighs a duplicated side effect — so they always dispatch and claim
    no sentinel. Every other event runs the once-guard: the first sibling to claim yields True
    and dispatches, the rest yield False and must exit silently. The claim is deliberately never
    released on a raised dispatch — the shim stays dumb: first claimer wins and a failure stays
    fail-closed for the TTL window, because releasing could re-run a legacy sibling whose earlier
    hooks already completed their side effects, or unlink a claim a slower sibling re-took after
    the TTL (there is no ownership check).
    """
    from captain_hook.cli import DECISION_EVENTS

    if event in DECISION_EVENTS:
        yield True
        return
    yield claim_once(event_name, payload, async_=async_)


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
