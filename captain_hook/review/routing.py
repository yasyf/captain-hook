"""Pack-to-repo routing for hook-misfire fix candidates: where a misfiring hook's PR belongs.

A hook that lives in the watched repo itself is fixed in place. A hook shipped by a pack is fixed in
the *pack's* repo, so its PR — and the candidate that produces it — routes there instead: a builtin
pack maps to captain-hook, and a pack shipped by an enabled Claude Code plugin maps to its
``plugin.json`` ``repository`` (normalized to a ``github.com/<owner>/<repo>`` key). A plugin pack
whose plugin declares no repository has no target, so its misfires route nowhere and are dropped
rather than misfiled repo-local. The mapping resolves once per scan with no network, from the
read-only plugin roster snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from captain_hook.review.repo import RepoKey, normalize_origin

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

CAPTAIN_HOOK_REPO = RepoKey("github.com/yasyf/captain-hook")
"""The repo every builtin pack's fixes route to — captain-hook itself, which ships them."""


@dataclass(frozen=True, slots=True)
class Target:
    """The resolved PR target for one hook-misfire complaint.

    Attributes:
        source_file: The hook file to amend — relative to ``repo`` for a pack hook,
            or the watched-repo path for a repo-local hook.
        hook_name: The hook's registered name (the firing decision's ``kind``).
        repo: The repo the fix PR belongs to — the pack's home repo for a pack hook,
            ``None`` for a hook living in the watched repo itself.
        pack: The pack the hook belongs to, or ``None`` for a repo-local hook.
    """

    source_file: str
    hook_name: str
    repo: RepoKey | None
    pack: str | None


@dataclass(frozen=True, slots=True)
class PluginRoute:
    """A plugin pack's home repo and install root, for routing one of its misfires.

    Attributes:
        repo: The plugin's ``plugin.json`` repository, normalized to a ``github.com/<owner>/<repo>``
            key, or ``None`` when the plugin declares no repository (its misfires are dropped).
        root: The plugin's install dir; a firing hook's ``source_file`` relativizes against it to the
            path within the plugin — a best effort, since a plugin rooted below its repo root (a
            ``plugin/`` subdir) carries that offset.
    """

    repo: RepoKey | None
    root: str


@dataclass(frozen=True, slots=True)
class PackIndex:
    """A project's pack → home-repo map, resolved once with no network.

    Builtin packs map to captain-hook itself (:data:`CAPTAIN_HOOK_REPO`); plugin packs map to their
    plugin's repository via :class:`PluginRoute`. A misfire attributed to a plugin pack with no
    repository is dropped rather than misrouted; one attributed to no known pack stays repo-local.

    Attributes:
        builtins: The builtin pack names mapped to their on-disk ``hooks/`` dirs.
        plugin_prefixes: The sanitized runtime module prefix a plugin pack's hooks load under
            (``pack_module_name(plugin_id)`` — the form a firing decision's ``kind`` carries) mapped
            to its :class:`PluginRoute`. Membership marks a kind as a plugin-pack misfire.
        plugin_dirs: The ``hooks/`` dir of each plugin pack mapped to its route — the source arm for a
            hook fired under a bare ``@on`` name (no ``<pack>.`` prefix in its ``kind``).
    """

    builtins: Mapping[str, Path]
    plugin_prefixes: Mapping[str, PluginRoute] = field(default_factory=dict)
    plugin_dirs: Mapping[str, PluginRoute] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path | None) -> PackIndex:
        """Builds the index for the project at ``root`` (builtins only when ``root`` is ``None``).

        Plugin packs come from the read-only ``.plugins`` snapshot (no CLI refresh, no subprocess),
        each probed for the fixed ``capt-hook/hooks/`` dir. A half-installed plugin dir is fail-soft:
        an unreadable roster yields no plugin routes rather than crashing the scan.
        """
        from captain_hook.packs import manager
        from captain_hook.packs import plugins as plugin_discovery

        builtins = {name: manager.resolve_builtin(name).path for name in manager.builtin_names()}
        if root is None:
            return cls(builtins=builtins)
        snapshot = plugin_discovery.PluginSnapshot.load(plugin_discovery.snapshot_path(root))
        prefixes: dict[str, PluginRoute] = {}
        dirs: dict[str, PluginRoute] = {}
        for plugin in snapshot.plugins if snapshot else ():
            if not plugin_discovery.has_plugin_pack(plugin):
                continue
            repository = plugin_discovery.plugin_repository(plugin)
            route = PluginRoute(RepoKey(normalize_origin(repository)) if repository else None, plugin.root)
            prefixes[manager.pack_module_name(plugin.id)] = route
            dirs[str(plugin_discovery.plugin_pack_root(plugin) / manager.HOOKS_DIRNAME)] = route
        return cls(builtins=builtins, plugin_prefixes=prefixes, plugin_dirs=dirs)
