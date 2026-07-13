"""Fingerprinted registry cache: reuse a discovered hook set until its inputs change.

Cold dispatch re-runs :meth:`CliState.discover` on every event — the ~50ms the resident daemon
exists to delete. This module caches the discovered :class:`~captain_hook.app.State` keyed by a
cheap :class:`Fingerprint` over everything discovery reads: the local hook tree, ``packs.toml``
and its resolve sidecars, ``.gitignore``, and the session's attached-pack overlay. An unchanged
tree hits; any edit, add, or removal (or a moving-ref pack past its refresh horizon) misses and
rebuilds. Snapshots are built under one lock so concurrent requests never race ``sys.modules``.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from captain_hook import app
from captain_hook.packs import manager
from captain_hook.util.caching import LRUDict

if TYPE_CHECKING:
    from collections.abc import Iterator

    from captain_hook.cli import CliState

MAX_SNAPSHOTS = 8

StatPair = tuple[int, int]
HookEntry = tuple[str, int, int, int]
MetaEntry = tuple[str, StatPair | None]


def _stat_pair(path: Path) -> StatPair | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return ""


def _iter_tree(root: Path, base: Path) -> Iterator[HookEntry]:
    with os.scandir(root) as it:
        for entry in it:
            if entry.name == "__pycache__":
                continue
            if entry.is_dir(follow_symlinks=False):
                yield from _iter_tree(Path(entry.path), base)
            elif entry.is_file(follow_symlinks=False) and not entry.name.endswith((".pyc", ".pyo")):
                st = entry.stat(follow_symlinks=False)
                yield (os.path.relpath(entry.path, base), st.st_mtime_ns, st.st_ctime_ns, st.st_size)


def _hooks_tree(hooks: str) -> tuple[HookEntry, ...]:
    return tuple(sorted(_iter_tree(root, root))) if (root := Path(hooks)).is_dir() else ()


def _moving_metas(entries: list[manager.PackEntry]) -> tuple[tuple[MetaEntry, ...], float | None]:
    metas: list[MetaEntry] = []
    horizon: float | None = None
    for entry in entries:
        if not (isinstance(entry, manager.ExternalPack) and entry.commit is None):
            continue
        meta_file = manager.meta_path(entry.name)
        metas.append((entry.name, _stat_pair(meta_file)))
        if (loaded := manager.PackMeta.load(meta_file)) is not None:
            expiry = loaded.checked_at + manager.REFRESH_TTL_SECONDS
            horizon = expiry if horizon is None else min(horizon, expiry)
    return tuple(metas), horizon


@dataclass(frozen=True, slots=True)
class Fingerprint:
    digest: str
    horizon: float | None

    def expired(self, now: float) -> bool:
        return self.horizon is not None and now >= self.horizon

    @classmethod
    def compute(cls, cli_state: CliState, session_dir: Path | None) -> Fingerprint:
        root = cli_state.root
        pack_meta, horizon = _moving_metas(manager.read_entries(manager.packs_toml_path(root)))
        material = (
            manager.toml_hash(root),
            _read_text(manager.fastpath_path(root)),
            pack_meta,
            _hooks_tree(cli_state.hooks),
            _stat_pair(root / ".gitignore"),
            _sha256_file(manager.attached_path(session_dir)) if session_dir is not None else "",
        )
        return cls(digest=hashlib.sha256(repr(material).encode()).hexdigest(), horizon=horizon)


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    fingerprint: Fingerprint
    state: app.State
    resolved: list[manager.ResolvedPack]
    discovery_stderr: str = ""


class Registry:
    def __init__(self, cli_state: CliState, *, maxsize: int = MAX_SNAPSHOTS) -> None:
        self._cli_state = cli_state
        self._cache: LRUDict[Fingerprint, RegistrySnapshot] = LRUDict(maxsize)
        self._build_lock = threading.Lock()

    def get(self, session_dir: Path | None) -> RegistrySnapshot:
        now = time.time()
        if (hit := self._lookup(Fingerprint.compute(self._cli_state, session_dir), now)) is not None:
            return hit
        with self._build_lock:
            # Re-fingerprint inside the lock: a peer's build may have just written the resolve
            # sidecar, so the pre-lock fingerprint is stale — recomputing lets us hit its work.
            if (hit := self._lookup(Fingerprint.compute(self._cli_state, session_dir), now)) is not None:
                return hit
            snapshot = self._build(session_dir)
            self._cache[snapshot.fingerprint] = snapshot
            return snapshot

    def drop_all(self) -> None:
        self._cache.cache_clear()

    def _lookup(self, fingerprint: Fingerprint, now: float) -> RegistrySnapshot | None:
        hit = self._cache.get(fingerprint)
        return hit if hit is not None and not fingerprint.expired(now) else None

    def _build(self, session_dir: Path | None) -> RegistrySnapshot:
        from captain_hook.daemon.context import capture_output

        state = app.State()
        # Capture discovery's diagnostics (e.g. "packs unavailable ...") into the snapshot instead of
        # letting them land only in the buffer of the request that triggered the build; the server
        # replays them into every request served from this snapshot, mirroring cold's per-invocation print.
        with capture_output() as captured, app.use_state(state):
            resolved = self._cli_state.discover(session_dir=session_dir)
        # Fingerprint after discover: its resolve wrote the fastpath sidecar, so this captures
        # the post-write inputs the next request will see and stores the snapshot under them.
        return RegistrySnapshot(
            fingerprint=Fingerprint.compute(self._cli_state, session_dir),
            state=state,
            resolved=resolved,
            discovery_stderr=captured.stderr.getvalue(),
        )
