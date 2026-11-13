from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from captain_hook.util.caching import LRUDict
from captain_hook.util.vcs import common_git_dir, git_dir

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

PROBE_CACHE_SIZE = 512
INCLUDE_DEPTH = 10
ABBREV_HEAD = ("rev-parse", "--abbrev-ref", "HEAD")
TOPLEVEL = ("rev-parse", "--show-toplevel")
SYMBOLIC_HEADS = frozenset({("symbolic-ref", "--short", "HEAD"), ("symbolic-ref", "--short", "-q", "HEAD")})
ORIGIN_HEAD = ("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
VERIFY_FLAGS = ("rev-parse", "--verify", "--quiet")
LITERAL_BRANCH = re.compile(r"refs/heads/(?!.*\.\.)(?!.*\.lock$)[A-Za-z0-9_][A-Za-z0-9_./-]*(?<![./])")
CONFIG_GETS = frozenset({"--get", "--get-all", "--get-regexp"})
CONFIG_READ_FLAGS = CONFIG_GETS | {"--local"}
CONFIG_PATH = re.compile(r"^\s*path\s*=\s*\"?(.*?)\"?\s*$", re.MULTILINE | re.IGNORECASE)
LOCATION_ENV = frozenset({"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES"})

type Stamp = tuple[int, int, int] | None

LOCK = threading.Lock()
PROBES: LRUDict[tuple[str, tuple[str, ...], tuple[Stamp, ...]], str | None] = LRUDict(PROBE_CACHE_SIZE)


def stamp(path: Path) -> Stamp:
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_ino, info.st_mtime_ns, info.st_size


def located(cwd: Path, args: Sequence[str]) -> tuple[Path, Path | None, tuple[str, ...]]:
    match args:
        case ["-C", where, *rest]:
            return cwd / where, None, tuple(rest)
        case [flag, *rest] if flag.startswith("--git-dir="):
            return cwd, cwd / flag.removeprefix("--git-dir="), tuple(rest)
        case _:
            return cwd, None, tuple(args)


def global_configs(env: Mapping[str, str]) -> tuple[Path, ...]:
    home = Path(env.get("HOME") or Path.home())
    return home / ".gitconfig", Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "git" / "config"


def included(sources: Iterable[Path]) -> tuple[Path, ...]:
    seen: list[Path] = []
    pending = [(source, 0) for source in sources]
    while pending:
        source, depth = pending.pop(0)
        if source in seen:
            continue
        seen.append(source)
        try:
            text = source.read_text(errors="replace")
        except OSError:
            continue
        if depth < INCLUDE_DEPTH:
            pending.extend((source.parent / Path(value).expanduser(), depth + 1) for value in CONFIG_PATH.findall(text))
    return tuple(seen)


def config_sources(gitdir: Path, common: Path, env: Mapping[str, str]) -> tuple[Path, ...]:
    return included((common / "config", gitdir / "config.worktree", *global_configs(env)))


def config_read(rest: Sequence[str]) -> bool:
    flags = {arg for arg in rest if arg.startswith("-")}
    return bool(flags & CONFIG_GETS) and flags <= CONFIG_READ_FLAGS


def dependencies(gitdir: Path, probe: tuple[str, ...], env: Mapping[str, str]) -> tuple[Path, ...] | None:
    common = common_git_dir(gitdir)
    if (common / "reftable").exists():
        return None
    match probe:
        case _ if probe == ABBREV_HEAD:
            return (
                gitdir / "HEAD",
                common / "packed-refs",
                common / "refs",
                common / "refs/tags",
                common / "refs/remotes",
            )
        case _ if probe == TOPLEVEL:
            return gitdir / "HEAD", *config_sources(gitdir, common, env)
        case _ if probe in SYMBOLIC_HEADS:
            return (gitdir / "HEAD",)
        case _ if probe == ORIGIN_HEAD:
            return (common / "refs/remotes/origin/HEAD",)
        case (*flags, ref) if tuple(flags) == VERIFY_FLAGS and LITERAL_BRANCH.fullmatch(ref):
            return common / ref, common / "packed-refs"
        case ("config", *rest) if config_read(rest):
            return config_sources(gitdir, common, env)
        case _:
            return None


def overridden(env: Mapping[str, str]) -> bool:
    return any(name in LOCATION_ENV or name.startswith("GIT_CONFIG") for name in env)


def cached_git(cwd: Path, args: Sequence[str], env: Mapping[str, str], run: Callable[[], str | None]) -> str | None:
    start, explicit, probe = located(cwd, args)
    if overridden(env) or not start.is_dir():
        return run()
    if (gitdir := explicit or git_dir(start)) is None or (paths := dependencies(gitdir, probe, env)) is None:
        return run()
    key = (os.fspath(start), tuple(args), tuple(map(stamp, paths)))
    with LOCK:
        if key in PROBES:
            return PROBES[key]
    if (result := run()) is None and probe == ABBREV_HEAD:
        return result
    with LOCK:
        PROBES[key] = result
    return result
