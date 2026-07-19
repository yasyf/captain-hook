from __future__ import annotations

import glob
import itertools
import os
import re
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

GLOB_LIMIT = 10


class GlobExpansion(NamedTuple):
    matches: tuple[str, ...]
    too_broad: bool


class GlobWalkBudgetExceeded(Exception):
    pass


def static_prefix_dir(token: str, cwd: Path) -> Path:
    prefix = "/".join(itertools.takewhile(lambda segment: not glob.has_magic(segment), token.split("/")))
    return (Path(prefix or "/") if os.path.isabs(token) else cwd / (prefix or ".")).resolve()


def walked_paths(anchor: Path) -> Iterator[Path]:
    for root, directories, files in os.walk(anchor, followlinks=False):
        directories[:] = [directory for directory in directories if not directory.startswith(".")]
        yield from (Path(root) / name for name in itertools.chain(directories, files) if not name.startswith("."))


def recursive_glob_matches(pattern: str, anchor: Path, format_path: Callable[[Path], str]) -> Iterator[str]:
    matcher = re.compile(glob.translate(pattern, recursive=True, include_hidden=False))
    for walked, path in enumerate(walked_paths(anchor), start=1):
        if matcher.match(candidate := format_path(path)):
            yield candidate
        if walked >= 20_000:
            raise GlobWalkBudgetExceeded


def glob_matches(token: str, cwd: Path | None, *, limit: int) -> GlobExpansion:
    pattern = os.path.expanduser(token)
    match os.path.isabs(pattern), cwd:
        case True, _:
            root = Path("/")
            root_dir = None
            format_path: Callable[[Path], str] = str
        case False, Path() as root:
            root_dir = str(root)
            resolved_root = root.resolve()
            format_path = partial(os.path.relpath, start=resolved_root)
        case False, None:
            return GlobExpansion((), False)
    if "**" not in pattern.split("/"):
        return GlobExpansion(
            tuple(
                itertools.islice(
                    glob.iglob(pattern, root_dir=root_dir, recursive=False, include_hidden=False),
                    limit + 1,
                )
            ),
            False,
        )
    try:
        return GlobExpansion(
            tuple(
                itertools.islice(
                    recursive_glob_matches(pattern, static_prefix_dir(pattern, root), format_path),
                    limit + 1,
                )
            ),
            False,
        )
    except GlobWalkBudgetExceeded:
        return GlobExpansion((), True)
