from __future__ import annotations

import itertools
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

from captain_hook.util.caching import ttl_cache

GRAPHITE_MARKER = ".graphite_repo_config"
NOGT_KEY = "ccx.nogt"
NOGT_TTL = 300.0
NOGT_TIMEOUT = 5
# ccx gates its own gt lane on this key parsed by Go's strconv.ParseBool, so these are
# the exact spellings it accepts. `git config --type=bool` would also take yes/on/off,
# which would silence a hook here while ccx still rode the gt lane.
NOGT_TRUE = frozenset({"1", "t", "T", "TRUE", "true", "True"})


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


def graphite_common_dir(directory: Path) -> Path | None:
    if (git := git_dir(directory)) is None:
        return None
    common = common_git_dir(git)
    return common if (common / GRAPHITE_MARKER).is_file() else None


def is_graphite_repo(directory: Path) -> bool:
    return graphite_common_dir(directory) is not None


@ttl_cache(NOGT_TTL)
def gt_disabled(common: Path) -> bool:
    try:
        probe = subprocess.run(
            ["git", "--git-dir", str(common), "config", "--get", NOGT_KEY],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=NOGT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.stdout.strip() in NOGT_TRUE


def graphite_lane(directory: Path) -> bool:
    common = graphite_common_dir(directory)
    return common is not None and not gt_disabled(common)


def is_repo_root(resolved: Path) -> bool:
    return any((resolved / marker).exists() for marker in (".git", ".jj"))


def scanned_names(anchor: Path) -> Iterator[str]:
    for _root, directories, files in os.walk(anchor, followlinks=False):
        yield from itertools.chain(directories, files)


def contains_repo(resolved: Path) -> bool:
    return any(
        name in (".git", ".jj") or scanned >= 20_000 for scanned, name in enumerate(scanned_names(resolved), start=1)
    )
