from __future__ import annotations

import itertools
import os
from collections.abc import Iterator
from pathlib import Path

GRAPHITE_MARKER = ".graphite_repo_config"


def in_vcs_repo(directory: Path) -> bool:
    return any(
        (ancestor / marker).exists() for ancestor in (directory, *directory.parents) for marker in (".git", ".jj")
    )


def git_dir(directory: Path) -> Path | None:
    for dot_git in (ancestor / ".git" for ancestor in (directory, *directory.parents)):
        if dot_git.is_dir():
            return dot_git
        if dot_git.is_file():
            pointer = Path(dot_git.read_text().removeprefix("gitdir:").strip())
            return pointer if pointer.is_absolute() else (dot_git.parent / pointer).resolve()
    return None


def common_git_dir(git: Path) -> Path:
    return (git / (git / "commondir").read_text().strip()).resolve() if (git / "commondir").is_file() else git


def is_graphite_repo(directory: Path) -> bool:
    return (git := git_dir(directory)) is not None and (common_git_dir(git) / GRAPHITE_MARKER).is_file()


def is_repo_root(resolved: Path) -> bool:
    return any((resolved / marker).exists() for marker in (".git", ".jj"))


def scanned_names(anchor: Path) -> Iterator[str]:
    for _root, directories, files in os.walk(anchor, followlinks=False):
        yield from itertools.chain(directories, files)


def contains_repo(resolved: Path) -> bool:
    return any(
        name in (".git", ".jj") or scanned >= 20_000 for scanned, name in enumerate(scanned_names(resolved), start=1)
    )
