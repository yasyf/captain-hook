"""Discovery of the pack shipped on each enabled Claude Code plugin of a project.

A pack-shipping Claude plugin carries its pack at the fixed path ``capt-hook/{pack.toml, hooks/}``
under the plugin root; the dispatcher loads every enabled plugin whose root ships one. The roster is
read straight from Claude Code's files on every discovery: ``installed_plugins.json`` names each
install (``plugins.<id>[].installPath`` with its ``scope`` and, for a ``project`` or ``local``
install, the ``projectPath`` it belongs to), and the ``enabledPlugins`` maps of the settings stack
decide which ids are on for the discovering root.

Pack load is all-or-nothing: a plugin advertising ``capt-hook/`` with a missing descriptor or hooks
dir raises rather than silently dropping guards. One plugin id may be installed at several scopes; the
highest-precedence scope governing the root wins (``local`` > ``project`` > ``user``), and two install
paths at the same scope of one project is a corrupt roster that raises :class:`PluginListError`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from captain_hook.packs import manager
from captain_hook.util.fs import read_json
from captain_hook.util.paths import resolve_claude_config_dir

SCOPE_PRECEDENCE = ("local", "project", "user")
MANAGED_SETTINGS_DIRS = (
    Path("/Library/Application Support/ClaudeCode"),
    Path("/etc/claude-code"),
)


class PluginListError(Exception):
    """Claude Code's plugin roster is unreadable or contradicts itself."""


@dataclass(frozen=True, slots=True)
class EnabledPlugin:
    id: str
    root: str
    scope: str | None = None
    project_path: str | None = None


def installed_plugins_path() -> Path:
    return resolve_claude_config_dir() / "plugins" / "installed_plugins.json"


def settings_stack(root: Path) -> tuple[Path, ...]:
    """The settings files that decide enablement for ``root``, lowest precedence first."""
    config = resolve_claude_config_dir()
    managed = (config, *MANAGED_SETTINGS_DIRS)
    return (
        config / "settings.json",
        root / ".claude" / "settings.json",
        root / ".claude" / "settings.local.json",
        *(d / "managed-settings.json" for d in managed),
        *(p for d in managed for p in sorted((d / "managed-settings.d").glob("*.json"))),
    )


def enablement(root: Path) -> dict[str, bool]:
    """Each plugin id's ``enabledPlugins`` value for ``root``, the highest-precedence settings file winning."""
    merged: dict[str, bool] = {}
    for path in settings_stack(root):
        if isinstance(data := read_json(path), dict) and isinstance(enabled := data.get("enabledPlugins"), dict):
            merged.update((pid, value) for pid, value in enabled.items() if isinstance(value, bool))
    return merged


def parse_install(pid: str, entry: object) -> EnabledPlugin | None:
    """One ``installed_plugins.json`` install record, or ``None`` when it names no install directory."""
    if not isinstance(entry, dict) or not isinstance(path := entry.get("installPath"), str) or not path:
        logger.bind(id=pid).debug(f"skipping plugin install record without an installPath: {entry!r}")
        return None
    if not Path(path).is_dir():
        return None
    scope = entry.get("scope")
    project = entry.get("projectPath")
    return EnabledPlugin(
        id=pid,
        root=path,
        scope=scope if isinstance(scope, str) else None,
        project_path=project if isinstance(project, str) and project else None,
    )


def scope_rank(scope: str | None) -> int:
    return SCOPE_PRECEDENCE.index(scope) if scope in SCOPE_PRECEDENCE else len(SCOPE_PRECEDENCE)


def governs(plugin: EnabledPlugin, root: Path) -> bool:
    """Whether an install governs ``root``: a ``user`` install governs every root, a scoped one only its project."""
    return plugin.project_path is None or Path(plugin.project_path).resolve() == root


def pick_scoped(pid: str, entries: list[EnabledPlugin]) -> EnabledPlugin:
    """The install a plugin id resolves to: the highest-precedence scope, raising on a same-scope conflict."""
    roots_by_scope: dict[str | None, set[str]] = {}
    for plugin in entries:
        roots_by_scope.setdefault(plugin.scope, set()).add(plugin.root)
    for scope, roots in roots_by_scope.items():
        if len(roots) > 1:
            raise PluginListError(f"plugin {pid!r} has {len(roots)} install paths at scope {scope!r}: {sorted(roots)}")
    return min(entries, key=lambda p: scope_rank(p.scope))


def enabled_plugins(root: Path) -> tuple[EnabledPlugin, ...]:
    """The plugins Claude Code has installed and enabled for ``root``, one per plugin id, in id order.

    Returns ``()`` when Claude Code has no ``installed_plugins.json``. Raises :class:`PluginListError`
    when the file is not a JSON object with a ``plugins`` object, or when one id has two install paths
    at the same scope of this project.
    """
    if not (path := installed_plugins_path()).is_file():
        return ()
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(installs := data.get("plugins"), dict):
        raise PluginListError(f"{path} is not a Claude Code plugin roster")
    resolved = root.resolve()
    enabled = enablement(root)
    plugins: list[EnabledPlugin] = []
    for pid, entries in sorted(installs.items()):
        if not enabled.get(pid) or not isinstance(entries, list):
            continue
        governing = [
            plugin
            for entry in entries
            if (plugin := parse_install(pid, entry)) is not None and governs(plugin, resolved)
        ]
        if governing:
            plugins.append(pick_scoped(pid, governing))
    return tuple(plugins)


def plugin_pack_root(plugin: EnabledPlugin) -> Path:
    """The fixed ``capt-hook/`` dir under a plugin's install root — present iff the plugin ships a pack."""
    return Path(plugin.root) / manager.PLUGIN_PACK_DIRNAME


def has_plugin_pack(plugin: EnabledPlugin) -> bool:
    return plugin_pack_root(plugin).is_dir()


def plugin_repository(plugin: EnabledPlugin) -> str | None:
    """The ``repository`` url from a plugin's ``.claude-plugin/plugin.json``, or ``None``.

    Routing-only (a hook-misfire fix PR's destination), never a load gate, so an absent or unreadable
    plugin.json is fail-soft — a missing repository just leaves a misfire repo-local.
    """
    path = Path(plugin.root) / ".claude-plugin" / "plugin.json"
    if not path.is_file():
        return None
    try:
        repository = json.loads(path.read_text()).get("repository")
    except (json.JSONDecodeError, OSError):
        return None
    return repository if isinstance(repository, str) and repository else None


def resolve_plugin_pack(plugin: EnabledPlugin) -> manager.ResolvedPack:
    """Load the pack at ``<plugin.root>/capt-hook/{pack.toml, hooks/}``.

    All-or-nothing: a ``capt-hook/`` dir missing its ``pack.toml`` or ``hooks/`` raises
    :class:`~captain_hook.packs.manager.PackError`, never silently dropping the plugin's guards.
    """
    pack_root = plugin_pack_root(plugin)
    descriptor = pack_root / manager.PACK_DESCRIPTOR
    hooks = pack_root / manager.HOOKS_DIRNAME
    if not descriptor.is_file():
        raise manager.PackError(
            f"plugin {plugin.id!r} ships {manager.PLUGIN_PACK_DIRNAME}/ without a {manager.PACK_DESCRIPTOR}"
        )
    if not hooks.is_dir():
        raise manager.PackError(
            f"plugin {plugin.id!r} ships {manager.PLUGIN_PACK_DIRNAME}/ without a {manager.HOOKS_DIRNAME}/ dir"
        )
    return manager.ResolvedPack(
        manager.PluginPack(plugin_id=plugin.id, root=plugin.root, repository=plugin_repository(plugin)),
        hooks,
        manager.PackDescriptor.load(descriptor),
    )


def resolve_plugin_packs(root: Path) -> list[manager.ResolvedPack]:
    """Resolve the pack each of ``root``'s enabled plugins ships, in plugin-id order.

    A plugin with no ``capt-hook/`` dir merely consumes packs and is skipped. A plugin advertising a
    malformed one raises (all-or-nothing).
    """
    return [resolve_plugin_pack(plugin) for plugin in enabled_plugins(root) if has_plugin_pack(plugin)]
