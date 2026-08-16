"""Fingerprinted registry cache: reuse a discovered hook set until its inputs change.

Cold dispatch re-runs :meth:`CliState.discover` on every event — the ~50ms the resident daemon
exists to delete. This module caches the discovered :class:`~captain_hook.app.State` keyed by a
cheap :class:`Fingerprint` over everything discovery reads: the local hook tree, the recursive
language markers that drive builtin activation, ``.gitignore``, the stat tuple of the files that
shape plugin discovery (Claude Code's ``installed_plugins.json`` and the settings stack), and the
fixed ``capt-hook/`` pack tree of every enabled Claude Code plugin. An unchanged tree hits; any edit,
add, or removal — a new ``go.mod``/``pyproject.toml``, a plugin enable/disable, or a hook edit inside
a plugin pack — misses and rebuilds. Snapshots are built under one lock so concurrent requests never
race ``sys.modules``. The plugin roster is read from the discovery snapshot as-is: the fingerprint
path never spawns ``claude plugin list`` — only :meth:`CliState.discover` refreshes it.

The language-marker input walks the repo once per fingerprint; the walk prunes VCS and gitignored
dirs and short-circuits per language, so it stays far cheaper than the module imports and plugin CLI
subprocess the daemon exists to skip (see :func:`captain_hook.packs.manager.detect_languages`).
"""

from __future__ import annotations

import hashlib
import os
import threading
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

StatEntry = tuple[int, int, int]
HookEntry = tuple[str, int, int, int]
PluginTree = tuple[str, tuple[HookEntry, ...]]


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


def _language_markers(root: Path) -> tuple[str, ...]:
    # The recursive, non-ignored markers driving go/python activation; a new or removed one flips it.
    return tuple(sorted(manager.detect_languages(root)))


def _claude_stats(root: Path) -> tuple[plugins.StatRecord, ...]:
    # The stat tuple whose change means a plugin was installed, enabled, or disabled — discovery then
    # re-runs the CLI and rewrites the roster snapshot the next fingerprint reads.
    return plugins.stat_records(root)


def _plugin_trees(root: Path) -> tuple[PluginTree, ...]:
    # Each plugin's capt-hook/ pack digested WHOLE (pack.toml + hooks/), in plugin-id order: the loader
    # keys on the whole dir, so watching only hooks/ would let warm ignore a fatal hooks-less pack.
    if (snapshot := plugins.PluginSnapshot.load(plugins.snapshot_path(root))) is None:
        return ()
    trees: list[PluginTree] = []
    for plugin in sorted(snapshot.plugins, key=lambda p: p.id):
        pack_root = plugins.plugin_pack_root(plugin)
        if not pack_root.is_dir():  # no capt-hook/ dir = no pack; a dir vanishing mid-walk skips below
            continue
        try:
            trees.append((plugin.id, _hooks_tree(str(pack_root))))
        except OSError:
            continue
    return tuple(trees)


@dataclass(frozen=True, slots=True)
class Fingerprint:
    digest: str

    @classmethod
    def _inputs(cls, cli_state: CliState) -> tuple[tuple[object, ...], tuple[str, ...]]:
        # ``brackets`` holds the plugin roster snapshot — the one sidecar discovery writes — so a build
        # can bracket its own refresh without the write skewing before/after stable digests.
        root = cli_state.root
        stable = (
            _language_markers(root),
            _hooks_tree(cli_state.hooks_dir),
            _stat_entry(root / ".gitignore"),
            _claude_stats(root),
            _plugin_trees(root),
        )
        brackets = (_read_text(plugins.snapshot_path(root)),)
        return stable, brackets

    @classmethod
    def compute(cls, cli_state: CliState) -> Fingerprint:
        stable, brackets = cls._inputs(cli_state)
        return cls(digest=hashlib.sha256(repr((stable, brackets)).encode()).hexdigest())

    @classmethod
    def stable_digest(cls, cli_state: CliState) -> str:
        stable, _brackets = cls._inputs(cli_state)
        return hashlib.sha256(repr(stable).encode()).hexdigest()


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
            # Re-fingerprint inside the lock: a peer's build may have written the roster snapshot, so
            # the pre-lock fingerprint is stale — recomputing lets us hit its work.
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
            before = Fingerprint.stable_digest(self._cli_state)
            latest = self._discover_once()
            if Fingerprint.stable_digest(self._cli_state) == before:
                return latest
        # Every retry churned: serve the freshest attempt but mark it non-cacheable so a possibly-torn
        # snapshot cannot poison the next request.
        return replace(latest, cacheable=False)

    def _discover_once(self) -> RegistrySnapshot:
        from captain_hook.cli import PLUGIN_ROSTER_SOURCE, pack_tool_specs
        from captain_hook.daemon.context import capture_output

        state = app.State()
        # Capture discovery's output on BOTH streams (stderr notices, stdout import-time prints); the
        # server replays it per request so warm mirrors cold's per-invocation print.
        with capture_output() as captured, app.use_state(state):
            resolved = self._cli_state.discover()
        # Fingerprint after discover: it may have refreshed the plugin snapshot, so this stores the
        # snapshot under the post-write inputs the next request sees.
        return RegistrySnapshot(
            fingerprint=Fingerprint.compute(self._cli_state),
            state=state,
            resolved=resolved,
            tools=pack_tool_specs(resolved),
            discovery_stdout=captured.stdout.getvalue(),
            discovery_stderr=captured.stderr.getvalue(),
            cacheable=not any(e.source == PLUGIN_ROSTER_SOURCE for e in state.load_errors),
        )
