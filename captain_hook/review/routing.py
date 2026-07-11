"""Pack-to-repo routing for hook-misfire fix candidates: where a misfiring hook's PR belongs.

A hook that lives in the watched repo itself is fixed in place. A hook shipped by a
pack is fixed in the *pack's* repo, so its PR — and the candidate that produces it —
routes there instead: a builtin pack maps to captain-hook, and an external GitHub pack
declared in the project's ``packs.toml`` maps to ``github.com/<owner>/<repo>``. The
mapping resolves once per scan with no network — an external pack routes only when its
content is already cached, so a cache miss leaves the misfire repo-local rather than
guessing a destination.
"""

from __future__ import annotations

import time
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
class PackIndex:
    """A project's pack-name → home-repo map, resolved once with no network.

    Builtin packs map to captain-hook itself (:data:`CAPTAIN_HOOK_REPO`); external
    packs declared in the project's ``packs.toml`` map to their
    ``github.com/<owner>/<repo>`` key — but only when their content is already cached,
    so resolution never touches the network. A pack absent from the index routes
    nowhere, so its misfires stay repo-local.

    Attributes:
        builtins: The builtin pack names mapped to their on-disk directories.
        externals: The cached external pack names mapped to their home repo keys.
    """

    builtins: Mapping[str, Path]
    externals: Mapping[str, RepoKey]

    @classmethod
    def load(cls, root: Path | None) -> PackIndex:
        """Builds the index for the project at ``root`` (builtins only when ``root`` is ``None``).

        Reads ``root``'s ``packs.toml`` and resolves every declared external pack to
        its home repo, dropping any whose content is not cached under a
        network-free commit — a pinned commit or a within-TTL moving-ref sidecar.
        """
        from captain_hook.packs.manager import (
            ExternalPack,
            builtin_packs,
            cached_commit,
            find_cached,
            packs_toml_path,
            read_entries,
        )

        now = time.time()
        externals = {
            entry.name: RepoKey(f"github.com/{entry.source.owner}/{entry.source.repo}".lower())
            for entry in (read_entries(packs_toml_path(root)) if root is not None else [])
            if isinstance(entry, ExternalPack)
            and (sha := cached_commit(entry, now)) is not None
            and find_cached(entry.name, sha) is not None
        }
        return cls(builtins=builtin_packs(), externals=externals)
