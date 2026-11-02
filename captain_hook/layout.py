from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from captain_hook.command_schema_core import PathMatches
from captain_hook.util import reqenv

ENV_PREFIX = "HOOKS_LAYOUT_"


class LayoutSettings(BaseModel):
    """This machine's own directory layout, read per request from ``HOOKS_LAYOUT_*`` variables holding JSON.

    Every field defaults to empty, so the performance pack's search and install guards cover only the
    generic roots until a machine names its own. ``~`` expands to the hook run's home directory.

    Attributes:
        protected_roots: Directories a recursive search may not walk, whatever its filename filter.
            ``HOOKS_LAYOUT_PROTECTED_ROOTS='["~/src", "~/.local/share/jobs"]'``.
        searchable_units: Path globs under a protected root that stay searchable, with everything below
            them. ``HOOKS_LAYOUT_SEARCHABLE_UNITS='["~/.local/share/jobs/*/payload"]'``.
        checkout_collections: Directories whose children are checkouts. A ``find`` at one is blocked, a
            checkout inside one stays searchable. ``HOOKS_LAYOUT_CHECKOUT_COLLECTIONS='["~/src"]'``.
        worktree_pools: Worktree pool directories mapped to their aggregate depth: ``1`` when lanes sit
            at ``<pool>/<repo>/<lane>``. The search guards treat a pool and its aggregates as broad, and
            the install guard blocks dependency installs in a lane whose checkout carries
            ``tools/hooks/warm-lane.sh``. ``HOOKS_LAYOUT_WORKTREE_POOLS='{"~/src/worktrees": 1}'``.
    """

    protected_roots: list[str] = Field(default_factory=list)
    searchable_units: list[str] = Field(default_factory=list)
    checkout_collections: list[str] = Field(default_factory=list)
    worktree_pools: dict[str, int] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Layout:
    protected_roots: tuple[Path, ...]
    searchable: PathMatches
    collections: tuple[Path, ...]
    pools: tuple[tuple[Path, int], ...]


def expand(path: str) -> Path:
    return Path(os.path.normpath(os.path.expanduser(path)))


def unit_globs(unit: str) -> tuple[str, ...]:
    parts = expand(unit).parts
    fixed = next((index for index, part in enumerate(parts) if glob.has_magic(part)), len(parts))
    resolved = str(Path(*parts[:fixed]).resolve().joinpath(*parts[fixed:]))
    return tuple(dict.fromkeys(pattern for root in (unit, resolved) for pattern in (root, f"{root}/**")))


def load() -> Layout:
    settings = LayoutSettings.model_validate(
        {
            name: json.loads(value)
            for name in LayoutSettings.model_fields
            if (value := reqenv.getenv(f"{ENV_PREFIX}{name.upper()}"))
        }
    )
    return Layout(
        protected_roots=tuple(map(expand, settings.protected_roots)),
        searchable=PathMatches(tuple(pattern for unit in settings.searchable_units for pattern in unit_globs(unit))),
        collections=tuple(map(expand, settings.checkout_collections)),
        pools=tuple((expand(pool), levels) for pool, levels in settings.worktree_pools.items()),
    )


def layout() -> Layout:
    return reqenv.shared("captain-hook.layout", load)


def forms(path: Path) -> tuple[Path, ...]:
    return tuple(dict.fromkeys((Path(os.path.normpath(path)), path.resolve())))


def within(path: Path, base: Path) -> bool:
    return any(form == anchor or anchor in form.parents for form in forms(path) for anchor in forms(base))


def depth_below(path: Path, base: Path) -> int | None:
    return min(
        (
            len(form.relative_to(anchor).parts)
            for form in forms(path)
            for anchor in forms(base)
            if form == anchor or anchor in form.parents
        ),
        default=None,
    )


def lane_checkout(path: Path) -> Path | None:
    return next(
        (
            anchor.joinpath(*parts[: levels + 1])
            for pool, levels in layout().pools
            for form in forms(path)
            for anchor in forms(pool)
            if anchor in form.parents and len(parts := form.relative_to(anchor).parts) > levels
        ),
        None,
    )


def in_checkout(path: Path) -> bool:
    current = layout()
    if pooled(path) or any(within(pool, path) for pool, _ in current.pools):
        return False
    return lane_checkout(path) is not None or any(
        (depth := depth_below(path, collection)) is not None and depth > 0 for collection in current.collections
    )


def pooled(path: Path) -> bool:
    return any((depth := depth_below(path, pool)) is not None and depth <= levels for pool, levels in layout().pools)


def locally_protected(path: Path) -> bool:
    current = layout()
    return any(within(path, root) for root in current.protected_roots) and not in_checkout(path)
