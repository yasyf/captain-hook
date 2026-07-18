from __future__ import annotations

import itertools
import os
from collections.abc import Iterator
from pathlib import Path


def in_vcs_repo(directory: Path) -> bool:
    return any(
        (ancestor / marker).exists() for ancestor in (directory, *directory.parents) for marker in (".git", ".jj")
    )


def is_repo_root(resolved: Path) -> bool:
    return any((resolved / marker).exists() for marker in (".git", ".jj"))


def scanned_names(anchor: Path) -> Iterator[str]:
    for _root, directories, files in os.walk(anchor, followlinks=False):
        yield from itertools.chain(directories, files)


def contains_repo(resolved: Path) -> bool:
    return any(
        name in (".git", ".jj") or scanned >= 20_000 for scanned, name in enumerate(scanned_names(resolved), start=1)
    )
