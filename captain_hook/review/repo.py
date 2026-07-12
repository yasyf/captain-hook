"""Worktree-safe repository identity: the normalized git ``origin`` remote, shared across a repo's worktrees."""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import NewType

from captain_hook.util import reqenv

RepoKey = NewType("RepoKey", str)


def git_output(cwd: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out if proc.returncode == 0 and (out := proc.stdout.strip()) else None


def normalize_origin(url: str) -> str:
    """Collapse SSH and HTTPS forms of one remote to a single ``host/path`` key (no scheme, user, or ``.git``)."""
    url = re.sub(r"^\w+://", "", url.strip().removesuffix(".git"))
    url = re.sub(r"^[^@/]+@", "", url)
    return url.replace(":", "/", 1).lower().rstrip("/")


@lru_cache(maxsize=2048)
def resolve_repo_key(cwd: str) -> RepoKey | None:
    if origin := git_output(cwd, "remote", "get-url", "origin"):
        return RepoKey(normalize_origin(origin))
    return None


def repo_key(start: Path | None = None) -> RepoKey | None:
    """Worktree-safe key for the repo at ``start`` (cwd default); ``None`` without a git repo or ``origin`` remote."""
    return resolve_repo_key(str((start or reqenv.cwd()).resolve()))
