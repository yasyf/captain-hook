from __future__ import annotations

import glob
import itertools
import os
import re
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import NamedTuple

from captain_hook import Allow, BaseHookEvent, Block, Event, HookResult, Input, Tool, on
from captain_hook.types import Command as CommandCondition
from captain_hook.util.scratch import is_scratch_path

GLOB_LIMIT = 10
WALK_BUDGET = 20_000
BACKSLASH_ESCAPE = re.compile(r"\\(.)")
EXECUTABLE_QUOTES = frozenset({"'", '"'})
GLOB_CHARS = frozenset("*?[")
VCS_MARKERS = (".git", ".jj")


class GlobExpansion(NamedTuple):
    matches: tuple[str, ...]
    too_broad: bool


class GlobWalkBudgetExceeded(Exception):
    pass


def unescape_shell(raw: str) -> str:
    return BACKSLASH_ESCAPE.sub(r"\1", raw)


def normalize_executable(raw: str) -> str:
    return Path(
        unescape_shell(raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in EXECUTABLE_QUOTES else raw)
    ).name.casefold()


def in_vcs_repo(directory: Path) -> bool:
    return any((ancestor / marker).exists() for ancestor in (directory, *directory.parents) for marker in VCS_MARKERS)


def is_repo_root(resolved: Path) -> bool:
    return any((resolved / marker).exists() for marker in VCS_MARKERS)


def rm_targets(args: tuple[str, ...]) -> list[str]:
    separator = args.index("--") if "--" in args else len(args)
    return [arg for arg in args[:separator] if arg == "-" or not arg.startswith("-")] + list(args[separator + 1 :])


def resolve_target(token: str, cwd: Path | None) -> Path | None:
    path = Path(os.path.expanduser(token))
    if not path.is_absolute():
        if cwd is None:
            return None
        path = cwd / path
    return path.parent.resolve() / path.name


def resolve_cd(args: tuple[str, ...], cwd: Path | None) -> Path | None:
    match args:
        case (token,) if "$" not in token and token != "-":
            path = Path(os.path.expanduser(unescape_shell(token)))
            return path.resolve() if path.is_absolute() else (cwd / path).resolve() if cwd is not None else None
        case _:
            return None


def static_prefix_dir(token: str, cwd: Path) -> Path:
    prefix = "/".join(itertools.takewhile(lambda segment: not GLOB_CHARS & set(segment), token.split("/")))
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
        if walked >= WALK_BUDGET:
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


def block_rm_target(evt: BaseHookEvent, token: str, cwd: Path | None) -> HookResult | None:
    if (resolved := resolve_target(token, cwd)) is None or is_scratch_path(resolved):
        return None
    if is_repo_root(resolved):
        return evt.block(
            f"BLOCKED: '{token}' is a git/jj repository root — deleting it destroys the repo and its entire "
            "history. If this is really intended, ask the user to run it themselves."
        )
    if not in_vcs_repo(resolved):
        return evt.block(
            f"BLOCKED: rm target '{token}' resolves outside any git/jj repository, so nothing can restore it "
            "after deletion. Move it to the trash instead, or stop and ask the user to confirm this deletion. "
            "(Temp and scratch paths are exempt.)"
        )
    return None


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CommandCondition(r"(?i)\brm\b")],
    tests={
        Input(command="rm foo.txt", cwd="/"): Block(pattern="repository"),
        Input(command="rm -- data.txt", cwd="/"): Block(pattern="repository"),
        Input(command="sudo rm /foo.txt", cwd="/"): Block(pattern="repository"),
        Input(command=r"\rm /foo.txt", cwd="/"): Block(pattern="repository"),
        Input(command="'rm' /foo.txt", cwd="/"): Block(pattern="repository"),
        Input(command="rm /tmp/x.py", cwd="/"): Allow(),
        Input(command="rm -rf /tmp/scratch/build", cwd="/"): Allow(),
        Input(command="rm foo.txt"): Allow(),
        Input(command="git rm foo.txt"): Allow(),
        Input(command="echo rm foo.txt"): Allow(),
        Input(command="ls"): Allow(),
        Input(command="git status"): Allow(),
    },
)
def block_risky_rm(evt: BaseHookEvent) -> HookResult | None:
    if evt.command_line is None:
        return None
    effective_cwd = evt.cwd
    for command in evt.command_line.commands:
        unwrapped = command.unwrapped
        match normalize_executable(unwrapped.executable):
            case "cd":
                effective_cwd = resolve_cd(unwrapped.args, effective_cwd)
            case "rm":
                for token in map(unescape_shell, rm_targets(unwrapped.args)):
                    if GLOB_CHARS & set(token):
                        expansion = glob_matches(token, effective_cwd, limit=GLOB_LIMIT)
                        if expansion.too_broad:
                            return evt.block(
                                f"BLOCKED: the glob '{token}' is too broad to verify safely (scanning stopped after "
                                f"{WALK_BUDGET} entries). Narrow the pattern or delete a specific directory with "
                                "rm -r <dir>."
                            )
                        if len(expansion.matches) > GLOB_LIMIT:
                            return evt.block(
                                f"BLOCKED: the glob '{token}' matches more than {GLOB_LIMIT} files — an easy way to "
                                f"delete far more than intended. List the matches first (ls {token}), narrow the "
                                "pattern, or name a directory explicitly with rm -r <dir>."
                            )
                        if (
                            blocked := next(
                                (
                                    result
                                    for matched in expansion.matches
                                    if (result := block_rm_target(evt, matched, effective_cwd)) is not None
                                ),
                                None,
                            )
                        ) is not None:
                            return blocked
                        continue
                    if (blocked := block_rm_target(evt, token, effective_cwd)) is not None:
                        return blocked
    return None
