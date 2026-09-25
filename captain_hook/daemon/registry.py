from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from captain_hook import app
from captain_hook.daemon.tree import DirectoryTreeCache
from captain_hook.packs import manager, plugins
from captain_hook.util.caching import LRUDict, StampedCache

if TYPE_CHECKING:
    from captain_hook.cli import CliState, ToolReg

BUILD_RETRIES = 3
MARKER_TTL = 30.0
PLUGIN_TTL = 30.0
MAX_WALK_ROOTS = 32

StatEntry = tuple[int, int, int]
HookEntry = tuple[str, int, int, int]
PluginTree = tuple[str, str, tuple[HookEntry, ...]]
MarkerStamp = tuple[StatEntry | None, StatEntry | None]
RosterEntry = StatEntry | int | None
RosterStamp = tuple[tuple[Path, RosterEntry], ...]

MARKER_WALKS: StampedCache[Path, MarkerStamp, tuple[str, ...]] = StampedCache(MAX_WALK_ROOTS)
PLUGIN_WALKS: StampedCache[Path, RosterStamp, tuple[PluginTree, ...] | str] = StampedCache(MAX_WALK_ROOTS)
HOOK_TREES = DirectoryTreeCache(MAX_WALK_ROOTS)


def _stat_entry(path: Path) -> StatEntry | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_ctime_ns, st.st_size)


def _hooks_tree(hooks: str, *, fresh: bool = False) -> tuple[HookEntry, ...]:
    return HOOK_TREES.entries(root, fresh=fresh) if (root := Path(hooks)).is_dir() else ()


def _language_markers(root: Path, *, fresh: bool) -> tuple[str, ...]:
    stamp = (_stat_entry(root), _stat_entry(root / ".gitignore"))
    return MARKER_WALKS.get(root, stamp, MARKER_TTL, lambda: tuple(sorted(manager.detect_languages(root))), fresh=fresh)


def _plugin_trees(root: Path, *, fresh: bool = False) -> tuple[PluginTree, ...] | str:
    try:
        roster = plugins.enabled_plugins(root)
    except plugins.PluginListError as exc:
        return str(exc)
    trees: list[PluginTree] = []
    for plugin in roster:
        pack_root = plugins.plugin_pack_root(plugin)
        try:
            trees.append((plugin.id, plugin.root, _hooks_tree(str(pack_root), fresh=fresh)))
        except OSError:
            continue
    return tuple(trees)


def _roster_entry(path: Path) -> RosterEntry:
    try:
        return _stat_entry(path)
    except OSError as exc:
        return exc.errno


def _roster_stamp(root: Path) -> RosterStamp:
    paths = (plugins.installed_plugins_path(), *plugins.settings_stack(root))
    return tuple((path, _roster_entry(path)) for path in paths)


def _plugin_inputs(root: Path, *, fresh: bool) -> tuple[PluginTree, ...] | str:
    return PLUGIN_WALKS.get(root, _roster_stamp(root), PLUGIN_TTL, lambda: _plugin_trees(root, fresh=fresh), fresh=fresh)


@dataclass(frozen=True, slots=True)
class Fingerprint:
    digest: str

    @classmethod
    def compute(cls, cli_state: CliState, *, fresh: bool = False) -> Fingerprint:
        root = cli_state.root
        inputs = (
            _language_markers(root, fresh=fresh),
            _hooks_tree(cli_state.hooks_dir, fresh=fresh),
            _stat_entry(root / ".gitignore"),
            _plugin_inputs(root, fresh=fresh),
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
    def __init__(self, cli_state: CliState, *, maxsize: int = 8) -> None:
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

        state = app.State(registry_fingerprint=fingerprint.digest)
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
