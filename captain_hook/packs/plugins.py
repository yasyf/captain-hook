"""Discovery of packs shipped on the enabled Claude Code plugins of a project.

A pack-shipping Claude plugin carries a ``[pack]`` manifest (``capt-hook.toml``) at its root
or under ``hooks/``; the dispatcher loads every enabled plugin whose root ships one. Enabled
plugins come from ``claude plugin list --json`` — the sanctioned interface, which resolves
``enabled`` against the project's settings stack — but that CLI spawns Node (~1s), far too slow
for the ~3ms dispatch hot path. So the roster is cached per project in a ``<key>.plugins``
snapshot beside the resolve fastpath, invalidated by the stat tuple of the files that shape it:
Claude Code's ``installed_plugins.json`` (mtime only — undocumented, never parsed), the user
settings, and the project's two settings files. A watched-file change (a plugin install, an
enable or disable) re-runs the CLI once; every other event reads the snapshot. The ``[pack]``
probe itself runs live per discovery (cheap stats), so a pack edit inside a plugin dir needs no
snapshot invalidation.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from collections.abc import Set
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from shutil import which

from filelock import FileLock
from loguru import logger

from captain_hook.packs import manager
from captain_hook.util.paths import resolve_claude_config_dir

CLI_TIMEOUT_SECONDS = 60
# Enterprise managed settings can enable/disable plugins; Claude Code reads them from a fixed OS
# location (macOS/Linux) plus the config dir, so a change to any must re-run the roster CLI.
MANAGED_SETTINGS_PATHS = (
    Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
    Path("/etc/claude-code/managed-settings.json"),
)

type StatRecord = tuple[str, int, int, int] | tuple[str, None]


class PluginListError(Exception):
    """``claude plugin list --json`` exited nonzero or returned unusable output."""


def installed_plugins_path() -> Path:
    return resolve_claude_config_dir() / "plugins" / "installed_plugins.json"


def watched_paths(root: Path) -> tuple[Path, ...]:
    config = resolve_claude_config_dir()
    return (
        installed_plugins_path(),
        config / "settings.json",
        config / "managed-settings.json",
        *MANAGED_SETTINGS_PATHS,
        root / ".claude" / "settings.json",
        root / ".claude" / "settings.local.json",
    )


def stat_record(path: Path) -> StatRecord:
    try:
        st = path.stat()
    except OSError:
        return (str(path), None)
    # ctime is folded in alongside mtime+size: a same-size, mtime-restored rewrite moves only ctime,
    # and without it the roster snapshot would stay "fresh" across such an edit.
    return (str(path), st.st_mtime_ns, st.st_ctime_ns, st.st_size)


def stat_records(root: Path) -> tuple[StatRecord, ...]:
    return tuple(stat_record(p) for p in watched_paths(root))


def snapshot_path(root: Path) -> Path:
    return manager.packs_cache_root() / f"{sha256(str(root.resolve()).encode()).hexdigest()[:16]}.plugins"


@dataclass(frozen=True, slots=True)
class EnabledPlugin:
    id: str
    version: str
    root: str


@dataclass(frozen=True, slots=True)
class PluginSnapshot:
    stat: tuple[StatRecord, ...]
    plugins: tuple[EnabledPlugin, ...]

    @classmethod
    def load(cls, path: Path) -> PluginSnapshot | None:
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text())
            return cls(
                stat=tuple(tuple(rec) for rec in data["stat"]),
                plugins=tuple(EnabledPlugin(id=p["id"], version=p["version"], root=p["root"]) for p in data["plugins"]),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def write(self, path: Path) -> None:
        manager.atomic_write(
            path,
            json.dumps(
                {
                    "stat": [list(rec) for rec in self.stat],
                    "plugins": [{"id": p.id, "version": p.version, "root": p.root} for p in self.plugins],
                }
            ),
        )

    def fresh(self, records: tuple[StatRecord, ...]) -> bool:
        return self.stat == records


def parse_plugin_entry(entry: object) -> EnabledPlugin | None:
    """One roster entry → an :class:`EnabledPlugin`, or ``None`` to skip it (unusable, not enabled).

    A non-object entry, or one whose ``id``/``installPath`` is missing or not a string, is skipped with
    a debug line so a single malformed sibling never suppresses the valid ones; an entry Claude Code did
    not report ``enabled`` with an install path is skipped silently (the ordinary not-enabled case).
    """
    if not isinstance(entry, dict):
        logger.debug(f"skipping non-object plugin roster entry {entry!r}")
        return None
    if entry.get("enabled") is not True or not entry.get("installPath"):
        return None
    if not (isinstance(pid := entry.get("id"), str) and pid and isinstance(path := entry.get("installPath"), str)):
        logger.bind(id=entry.get("id"), installPath=entry.get("installPath")).debug(
            "skipping plugin roster entry lacking a string id/installPath"
        )
        return None
    version = entry.get("version", "")
    return EnabledPlugin(id=pid, version=version if isinstance(version, str) else "", root=path)


def list_plugins_cli(root: Path) -> tuple[EnabledPlugin, ...]:
    """Run ``claude plugin list --json`` in ``root`` and return its enabled, installed plugins.

    The single subprocess boundary of discovery. Keeps only entries Claude Code reports as ``enabled``
    with a string ``installPath``. Raises :class:`PluginListError` on a nonzero exit and on any roster
    shape it cannot use — a non-array top level, unparseable JSON, or an entry that trips the parser —
    so every failure lands on ``enabled_plugins``' warning-and-empty-snapshot path, never crashing
    dispatch. A subprocess timeout still surfaces as the native ``subprocess.TimeoutExpired``.
    """
    result = subprocess.run(
        ["claude", "plugin", "list", "--json"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=CLI_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise PluginListError(f"claude plugin list exited {result.returncode}: {result.stderr.strip()}")
    try:
        roster = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise PluginListError(f"claude plugin list returned unparseable JSON: {e}") from e
    if not isinstance(roster, list):
        raise PluginListError(f"claude plugin list returned {type(roster).__name__}, expected a JSON array")
    try:
        return tuple(plugin for entry in roster if (plugin := parse_plugin_entry(entry)) is not None)
    except (TypeError, AttributeError, KeyError) as e:
        raise PluginListError(f"claude plugin list returned an unusable roster shape: {e!r}") from e


def enabled_plugins(root: Path) -> tuple[EnabledPlugin, ...]:
    """The plugins Claude Code has enabled for ``root``, cached per project with stat invalidation.

    Returns ``()`` without spawning the CLI when Claude Code has no ``installed_plugins.json``
    (the existence gate that keeps discovery — and every test — hermetic) or when ``claude`` is
    off PATH. A fresh snapshot is served directly; otherwise the CLI re-runs once under a file
    lock with a re-stat double-check inside it, so a burst of concurrent events pays a single
    spawn. A CLI failure caches an empty roster against the current stat tuple, so a persistently
    broken CLI costs nothing per event until a watched file changes and re-triggers it.
    """
    if not installed_plugins_path().is_file():
        return ()
    records = stat_records(root)
    path = snapshot_path(root)
    if (snap := PluginSnapshot.load(path)) and snap.fresh(records):
        return snap.plugins
    if which("claude") is None:
        logger.debug("claude not on PATH; skipping plugin discovery")
        return ()
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path.with_name(path.name + ".lock"))):
        records = stat_records(root)
        if (snap := PluginSnapshot.load(path)) and snap.fresh(records):
            return snap.plugins
        try:
            plugins = list_plugins_cli(root)
        except (PluginListError, subprocess.TimeoutExpired, ValueError, KeyError):
            logger.opt(exception=True).warning(
                "claude plugin list failed; caching an empty plugin roster until a watched file changes"
            )
            PluginSnapshot(stat=records, plugins=()).write(path)
            return ()
        PluginSnapshot(stat=records, plugins=plugins).write(path)
        return plugins


def probe_plugin_pack(plugin: EnabledPlugin) -> tuple[Path, manager.PackManifest] | None:
    for probe in (Path(plugin.root), Path(plugin.root) / "hooks"):
        if (manifest := manager.load_pack_manifest(probe)) is not None:
            return probe, manifest
    return None


def resolve_plugin_packs(root: Path, declared: Set[str]) -> list[manager.ResolvedPack]:
    """Resolve packs shipped on ``root``'s enabled plugins, minus any name the project declares.

    Each enabled plugin is probed — at its root, then under ``hooks/`` (both attach-era layouts) —
    for a ``[pack]`` manifest, in plugin-id order. A plugin with no manifest merely consumes packs
    and is skipped silently; a malformed manifest is skipped fail-soft so one rotted plugin cannot
    kill discovery. ``declared`` is every ``[packs.*]`` entry name — including disabled entries and
    uncached or missing externals — so a project entry always shadows a same-name plugin pack. When
    two plugins ship the same pack name the lower plugin id wins, with a warning naming both.
    """
    resolved: list[manager.ResolvedPack] = []
    winners: dict[str, str] = {}
    for plugin in sorted(enabled_plugins(root), key=lambda p: p.id):
        try:
            probed = probe_plugin_pack(plugin)
        except (manager.PackError, tomllib.TOMLDecodeError, OSError):
            logger.bind(plugin=plugin.id).opt(exception=True).debug("skipped plugin with a malformed [pack] manifest")
            continue
        if probed is None:
            continue
        probe, manifest = probed
        if manifest.name in declared:
            logger.bind(plugin=plugin.id, pack=manifest.name).debug(
                "plugin pack shadowed by a declared [packs.*] entry"
            )
            continue
        if (winner := winners.get(manifest.name)) is not None:
            logger.bind(pack=manifest.name).warning(
                f"duplicate plugin pack {manifest.name!r}: plugin {winner!r} wins over {plugin.id!r}"
            )
            continue
        winners[manifest.name] = plugin.id
        resolved.append(
            manager.ResolvedPack(
                manager.PluginPack(name=manifest.name, plugin_id=plugin.id, dir=str(probe), version=manifest.version),
                manifest.hooks_dir(probe),
                manifest,
            )
        )
    return resolved
