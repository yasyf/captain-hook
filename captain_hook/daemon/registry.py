"""Fingerprinted registry cache: reuse a discovered hook set until its inputs change.

Cold dispatch re-runs :meth:`CliState.discover` on every event — the ~50ms the resident daemon
exists to delete. This module caches the discovered :class:`~captain_hook.app.State` keyed by a
cheap :class:`Fingerprint` over everything discovery reads: the local hook tree, the recursive
language markers that drive builtin activation, ``.gitignore``, and the enabled plugin roster with
the fixed ``capt-hook/`` pack tree of each plugin, read from Claude Code's files. An unchanged tree
hits; any edit, add, or removal — a new ``go.mod``/``pyproject.toml``, a plugin enable/disable, or a
hook edit inside a plugin pack — misses and rebuilds. Snapshots are built under one lock so
concurrent requests never race ``sys.modules``.

The language-marker input walks the repo (see :func:`captain_hook.packs.manager.detect_languages`),
which costs over 100ms on a large monorepo, so the fingerprint reuses the last walk per root while
that root's stamp holds: the root directory's own stat plus its ``.gitignore``'s. A marker created,
removed, or renamed directly under the root, or a root ``.gitignore`` edit, moves the stamp and
re-walks on the next event; a change deeper in the tree lands within :data:`MARKER_TTL` seconds. A
build always re-walks, so a snapshot is stored under the language set its own discovery saw.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from captain_hook import app
from captain_hook.packs import manager, plugins
from captain_hook.util.caching import LRUDict

if TYPE_CHECKING:
    from collections.abc import Iterator

    from captain_hook.cli import CliState, ToolReg

MAX_SNAPSHOTS = 8
BUILD_RETRIES = 3
MARKER_TTL = 30.0
MAX_MARKER_ROOTS = 32
MARKER_WALKS: LRUDict[Path, MarkerWalk] = LRUDict(MAX_MARKER_ROOTS)
MARKER_LOCK = threading.Lock()

StatEntry = tuple[int, int, int]
HookEntry = tuple[str, int, int, int]
PluginTree = tuple[str, str, tuple[HookEntry, ...]]
MarkerStamp = tuple[StatEntry | None, StatEntry | None]


@dataclass(frozen=True, slots=True)
class MarkerWalk:
    stamp: MarkerStamp
    walked_at: float
    markers: tuple[str, ...]


def _stat_entry(path: Path) -> StatEntry | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_ctime_ns, st.st_size)


def _read_text(path: Path) -> str:
    try:
        return path.read_text()
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


def _language_markers(root: Path, *, fresh: bool) -> tuple[str, ...]:
    stamp = (_stat_entry(root), _stat_entry(root / ".gitignore"))
    with MARKER_LOCK:
        cached = MARKER_WALKS.get(root)
    if not fresh and cached is not None and cached.stamp == stamp and time.monotonic() - cached.walked_at < MARKER_TTL:
        return cached.markers
    walked_at = time.monotonic()
    markers = tuple(sorted(manager.detect_languages(root)))
    with MARKER_LOCK:
        MARKER_WALKS[root] = MarkerWalk(stamp, walked_at, markers)
    return markers


def _plugin_trees(root: Path) -> tuple[PluginTree, ...] | str:
    try:
        roster = plugins.enabled_plugins(root)
    except plugins.PluginListError as exc:
        return str(exc)
    trees: list[PluginTree] = []
    for plugin in roster:
        pack_root = plugins.plugin_pack_root(plugin)
        try:
            trees.append((plugin.id, plugin.root, _hooks_tree(str(pack_root))))
        except OSError:
            continue
    return tuple(trees)


@dataclass(frozen=True, slots=True)
class Fingerprint:
    digest: str

    @classmethod
    def compute(cls, cli_state: CliState, *, fresh: bool = False) -> Fingerprint:
        root = cli_state.root
        inputs = (
            _language_markers(root, fresh=fresh),
            _hooks_tree(cli_state.hooks_dir),
            _stat_entry(root / ".gitignore"),
            _plugin_trees(root),
        )
        return cls(digest=hashlib.sha256(repr(inputs).encode()).hexdigest())


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    fingerprint: Fingerprint
    state: app.State
    resolved: list[manager.ResolvedPack]
    tools: dict[str, ToolReg]
    discovery_stdout: str = ""
    discovery_stderr: str = ""
    cacheable: bool = True


class Registry:
    def __init__(self, cli_state: CliState, *, maxsize: int = MAX_SNAPSHOTS) -> None:
        self._cli_state = cli_state
        self._cache: LRUDict[Fingerprint, RegistrySnapshot] = LRUDict(maxsize)
        self._build_lock = threading.Lock()

    def get(self) -> RegistrySnapshot:
        if (hit := self._cache.get(Fingerprint.compute(self._cli_state))) is not None:
            self._reconcile_tools(hit)
            return hit
        with self._build_lock:
            if (hit := self._cache.get(Fingerprint.compute(self._cli_state))) is not None:
                self._reconcile_tools(hit)
                return hit
            snapshot = self._build()
            if snapshot.cacheable:
                self._cache[snapshot.fingerprint] = snapshot
            return snapshot

    def drop_all(self) -> None:
        self._cache.cache_clear()

    def _reconcile_tools(self, snapshot: RegistrySnapshot) -> None:
        # A cache hit skips discover(), so align cc-transcript's process-global tool registry to this
        # snapshot's specs — a no-op unless a prior request left another config's tools registered.
        from captain_hook.cli import reconcile_pack_tools

        reconcile_pack_tools(snapshot.tools)

    def _build(self) -> RegistrySnapshot:
        # Retry a discovery whose stable inputs moved under it (a torn mid-rewrite read); serve the latest.
        for _ in range(BUILD_RETRIES):
            before = Fingerprint.compute(self._cli_state, fresh=True)
            latest = self._discover_once(before)
            if Fingerprint.compute(self._cli_state, fresh=True) == before:
                return latest
        # Every retry churned: serve the freshest attempt but mark it non-cacheable so a possibly-torn
        # snapshot cannot poison the next request.
        return replace(latest, cacheable=False)

    def _discover_once(self, fingerprint: Fingerprint) -> RegistrySnapshot:
        from captain_hook.cli import PLUGIN_ROSTER_SOURCE, pack_tool_specs
        from captain_hook.daemon.context import capture_output

        state = app.State()
        # Capture discovery's output on BOTH streams (stderr notices, stdout import-time prints); the
        # server replays it per request so warm mirrors cold's per-invocation print.
        with capture_output() as captured, app.use_state(state):
            resolved = self._cli_state.discover()
        return RegistrySnapshot(
            fingerprint=fingerprint,
            state=state,
            resolved=resolved,
            tools=pack_tool_specs(resolved),
            discovery_stdout=captured.stdout.getvalue(),
            discovery_stderr=captured.stderr.getvalue(),
            cacheable=not any(e.source == PLUGIN_ROSTER_SOURCE for e in state.load_errors),
        )
