from __future__ import annotations

import ctypes
import hashlib
import math
import sys
import threading
import time
from dataclasses import dataclass, replace
from importlib.metadata import version as installed_version
from pathlib import Path
from typing import TYPE_CHECKING

from filelock import FileLock
from pydantic import BaseModel, ConfigDict

from captain_hook import app
from captain_hook.daemon.tree import DirectoryTreeCache
from captain_hook.durable import project_key
from captain_hook.packs import manager, plugins
from captain_hook.util import reqenv
from captain_hook.util.caching import LRUDict, StampedCache, once
from captain_hook.util.fs import atomic_write
from captain_hook.util.paths import resolve_state_dir

if TYPE_CHECKING:
    from captain_hook.cli import CliState, ToolReg

BUILD_RETRIES = 3
MARKER_TTL = 30.0
PLUGIN_TTL = 30.0
MAX_WALK_ROOTS = 32
ANY_AGE = -math.inf
LINUX_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")

StatEntry = tuple[int, int, int]
HookEntry = tuple[str, int, int, int]
PluginTree = tuple[str, str, tuple[HookEntry, ...]]
MarkerStamp = tuple[StatEntry | None, StatEntry | None]
RosterEntry = StatEntry | int | None
RosterStamp = tuple[tuple[Path, RosterEntry], ...]

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


class LanguageGeneration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)

    stamp: MarkerStamp
    computed_at: float
    languages: tuple[str, ...]

    def current(self, stamp: MarkerStamp, floor: float, now: float) -> bool:
        return self.stamp == stamp and floor <= self.computed_at and now - self.computed_at < MARKER_TTL


@once
def marker_build() -> str:
    return installed_version("capt-hook")


@once
def boot_id() -> str:
    match sys.platform:
        case "darwin":
            return darwin_boot_session()
        case "linux":
            return LINUX_BOOT_ID.read_text().strip()
        case platform:
            raise RuntimeError(f"capt-hook has no boot id source on {platform}")


def darwin_boot_session() -> str:
    libc = ctypes.CDLL(None, use_errno=True)
    size = ctypes.c_size_t(64)
    session = ctypes.create_string_buffer(size.value)
    if libc.sysctlbyname(b"kern.bootsessionuuid", session, ctypes.byref(size), None, ctypes.c_size_t(0)):
        raise OSError(ctypes.get_errno(), "sysctlbyname(kern.bootsessionuuid) failed")
    return session.value.decode()


def marker_path(root: Path) -> Path:
    return resolve_state_dir() / "hooks" / "language-markers" / marker_build() / boot_id() / f"{project_key(root)}.json"


def marker_lock(path: Path) -> FileLock:
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(path.with_name(f"{path.name}.lock")))


def request_stopped() -> bool:
    return reqenv.abandon_signal().is_set() or reqenv.deadline_within(0)


def shared_generation(path: Path, stamp: MarkerStamp, floor: float) -> LanguageGeneration | None:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    entry = LanguageGeneration.model_validate_json(text)
    return entry if entry.current(stamp, floor, time.monotonic()) else None


def _language_markers(root: Path, *, since: float) -> tuple[str, ...]:
    stamp = (_stat_entry(root), _stat_entry(root / ".gitignore"))
    if (hit := shared_generation(path := marker_path(root), stamp, since)) is not None:
        return hit.languages
    with marker_lock(path).acquire(cancel_check=request_stopped):
        if (hit := shared_generation(path, stamp, since)) is not None:
            return hit.languages
        computed_at = time.monotonic()
        entry = LanguageGeneration(
            stamp=stamp, computed_at=computed_at, languages=tuple(sorted(manager.detect_languages(root)))
        )
        atomic_write(path, entry.model_dump_json())
    return entry.languages


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
    return PLUGIN_WALKS.get(
        root, _roster_stamp(root), PLUGIN_TTL, lambda: _plugin_trees(root, fresh=fresh), fresh=fresh
    )


@dataclass(frozen=True, slots=True)
class Fingerprint:
    digest: str

    @classmethod
    def compute(cls, cli_state: CliState, *, fresh: bool = False) -> Fingerprint:
        since = time.monotonic() if fresh else ANY_AGE
        return cls.with_languages(cli_state, _language_markers(cli_state.root, since=since), fresh=fresh)

    @classmethod
    def with_languages(cls, cli_state: CliState, languages: tuple[str, ...], *, fresh: bool) -> Fingerprint:
        root = cli_state.root
        inputs = (
            languages,
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
        requested = time.monotonic()
        if self._cache and (hit := self._cache.get(Fingerprint.compute(self._cli_state))) is not None:
            self._reconcile_tools(hit)
            return hit
        with self._build_lock:
            if self._cache and (hit := self._cache.get(Fingerprint.compute(self._cli_state))) is not None:
                reqenv.warmed("registry")
                self._reconcile_tools(hit)
                return hit
            snapshot = self._build(requested)
            if snapshot.cacheable:
                self._cache[snapshot.fingerprint] = snapshot
                reqenv.warmed("registry")
            return snapshot

    def drop_all(self) -> None:
        self._cache.cache_clear()

    def _reconcile_tools(self, snapshot: RegistrySnapshot) -> None:
        # A cache hit skips discover(), so align cc-transcript's process-global tool registry to this
        # snapshot's specs — a no-op unless a prior request left another config's tools registered.
        from captain_hook.cli import reconcile_pack_tools

        reconcile_pack_tools(snapshot.tools)

    def _build(self, requested: float) -> RegistrySnapshot:
        # Retry a discovery whose stable inputs moved under it (a torn mid-rewrite read); serve the latest.
        for attempt in range(BUILD_RETRIES):
            languages = _language_markers(self._cli_state.root, since=time.monotonic() if attempt else requested)
            before = Fingerprint.with_languages(self._cli_state, languages, fresh=True)
            latest = self._discover_once(before, languages)
            if Fingerprint.with_languages(self._cli_state, languages, fresh=True) == before:
                return latest
        # Every retry churned: serve the freshest attempt but mark it non-cacheable so a possibly-torn
        # snapshot cannot poison the next request.
        return replace(latest, cacheable=False)

    def _discover_once(self, fingerprint: Fingerprint, languages: tuple[str, ...]) -> RegistrySnapshot:
        from captain_hook.cli import PLUGIN_ROSTER_SOURCE, pack_tool_specs
        from captain_hook.daemon.context import capture_output

        state = app.State(registry_fingerprint=fingerprint.digest)
        # Capture discovery's output on BOTH streams (stderr notices, stdout import-time prints); the
        # server replays it per request so warm mirrors cold's per-invocation print.
        with capture_output() as captured, app.use_state(state):
            resolved = self._cli_state.discover(languages=languages)
        return RegistrySnapshot(
            fingerprint=fingerprint,
            state=state,
            resolved=resolved,
            tools=pack_tool_specs(resolved),
            discovery_stdout=captured.stdout.getvalue(),
            discovery_stderr=captured.stderr.getvalue(),
            cacheable=not any(e.source == PLUGIN_ROSTER_SOURCE for e in state.load_errors),
        )
