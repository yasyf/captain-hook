"""Pack-to-repo routing for hook-misfire fix candidates: where a misfiring hook's PR belongs.

A hook that lives in the watched repo itself is fixed in place. A hook shipped by a
pack is fixed in the *pack's* repo, so its PR — and the candidate that produces it —
routes there instead: a builtin pack maps to captain-hook, and an external GitHub pack
declared in the project's ``.claude/capt-hook.toml`` maps to ``github.com/<owner>/<repo>``.
A pack discovered on an enabled Claude Code plugin has no repo target this wave, so its
misfires route nowhere and are dropped rather than misfiled. The mapping resolves once per
scan with no network — an external pack routes only when its content is already cached, so a
cache miss leaves the misfire repo-local rather than guessing a destination.
"""

from __future__ import annotations

import time
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from captain_hook.review.repo import RepoKey

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
class ExternalRoute:
    """A cached external pack's canonical name and home repo.

    Attributes:
        pack_name: The pack's canonical ``capt-hook.toml`` name (hyphens intact).
        repo: The pack's ``github.com/<owner>/<repo>`` home repo key.
    """

    pack_name: str
    repo: RepoKey


@dataclass(frozen=True, slots=True)
class PackIndex:
    """A project's pack-name → home-repo map, resolved once with no network.

    Builtin packs map to captain-hook itself (:data:`CAPTAIN_HOOK_REPO`); external
    packs declared in the project's ``.claude/capt-hook.toml`` map to their
    ``github.com/<owner>/<repo>`` key — but only when their content is already cached,
    so resolution never touches the network. Packs discovered on the project's enabled
    Claude Code plugins have no repo target this wave and land in ``plugins``, so a
    misfire attributed to one is dropped rather than misrouted. A pack absent from the
    index routes nowhere, so its misfires stay repo-local.

    Attributes:
        builtins: The builtin pack names mapped to their on-disk directories.
        externals: Cached external packs keyed by the sanitized runtime module prefix a
            hook loads under (``ccx-rel`` → ``ccx_rel``) — the form a firing decision's
            ``kind`` carries — mapping to the pack's canonical name and home repo key.
        plugins: The sanitized module prefixes of packs shipped on the project's enabled
            plugins that no declared ``[packs.*]`` entry shadows — a misfire whose ``kind``
            prefix is one of these routes to no repo this wave and is dropped.
    """

    builtins: Mapping[str, Path]
    externals: Mapping[str, ExternalRoute]
    plugins: frozenset[str] = frozenset()

    @classmethod
    def load(cls, root: Path | None) -> PackIndex:
        """Builds the index for the project at ``root`` (builtins only when ``root`` is ``None``).

        Reads ``root``'s ``.claude/capt-hook.toml`` and resolves every declared external pack
        to its home repo, dropping any whose content is not cached under a network-free commit
        — a pinned commit or a within-TTL moving-ref sidecar. Discovered plugin packs come from
        the read-only ``.plugins`` snapshot (no CLI refresh, no subprocess), probed for a
        ``[pack]`` manifest at each plugin's root then under ``hooks/``, minus any name a
        declared ``[packs.*]`` entry shadows. A mined repo may be unmigrated or half-installed,
        so every read here is fail-soft: an unreadable config or plugin dir yields no entries
        rather than crashing the scan.
        """
        from captain_hook.packs import plugins as plugin_discovery
        from captain_hook.packs.manager import (
            ExternalPack,
            PackError,
            builtin_packs,
            cached_commit,
            config_path,
            find_cached,
            pack_module_name,
            read_entries,
        )

        builtins = builtin_packs()
        if root is None:
            return cls(builtins=builtins, externals={}, plugins=frozenset())
        now = time.time()
        try:
            entries = read_entries(config_path(root))
        except (PackError, tomllib.TOMLDecodeError, OSError):
            return cls(builtins=builtins, externals={}, plugins=frozenset())
        externals = {
            pack_module_name(entry.name): ExternalRoute(
                entry.name, RepoKey(f"github.com/{entry.source.owner}/{entry.source.repo}".lower())
            )
            for entry in entries
            if isinstance(entry, ExternalPack)
            and (sha := cached_commit(entry, now)) is not None
            and find_cached(entry.name, sha) is not None
        }
        declared = {pack_module_name(entry.name) for entry in entries}
        discovered: set[str] = set()
        snapshot = plugin_discovery.PluginSnapshot.load(plugin_discovery.snapshot_path(root))
        for plugin in snapshot.plugins if snapshot else ():
            try:
                probed = plugin_discovery.probe_plugin_pack(plugin)
            except (PackError, tomllib.TOMLDecodeError, OSError):
                continue
            if probed is not None:
                discovered.add(pack_module_name(probed[1].name))
        return cls(builtins=builtins, externals=externals, plugins=frozenset(discovered) - declared)
