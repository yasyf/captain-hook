from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass
from pathlib import Path

from captain_hook.command_schema_core import PathMatches
from captain_hook.util import reqenv

PROTECTED_ROOTS = "HOOKS_LAYOUT_PROTECTED_ROOTS"
SEARCHABLE_UNITS = "HOOKS_LAYOUT_SEARCHABLE_UNITS"
CHECKOUT_COLLECTIONS = "HOOKS_LAYOUT_CHECKOUT_COLLECTIONS"
WORKTREE_POOLS = "HOOKS_LAYOUT_WORKTREE_POOLS"
KEYS = (PROTECTED_ROOTS, SEARCHABLE_UNITS, CHECKOUT_COLLECTIONS, WORKTREE_POOLS)


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


def paths(key: str) -> tuple[str, ...]:
    match json.loads(reqenv.getenv(key) or "[]"):
        case list() as values if all(isinstance(value, str) for value in values):
            return tuple(values)
        case value:
            raise ValueError(f"`{key}` must hold a JSON list of paths, not {value!r}")


def depths(key: str) -> tuple[tuple[str, int], ...]:
    match json.loads(reqenv.getenv(key) or "{}"):
        case dict() as pools if all(isinstance(levels, int) and levels >= 0 for levels in pools.values()):
            return tuple(pools.items())
        case value:
            raise ValueError(f"`{key}` must hold a JSON object mapping each pool to its lane depth, not {value!r}")


def load() -> Layout:
    return Layout(
        protected_roots=tuple(map(expand, paths(PROTECTED_ROOTS))),
        searchable=PathMatches(tuple(pattern for unit in paths(SEARCHABLE_UNITS) for pattern in unit_globs(unit))),
        collections=tuple(map(expand, paths(CHECKOUT_COLLECTIONS))),
        pools=tuple((expand(pool), levels) for pool, levels in depths(WORKTREE_POOLS)),
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
