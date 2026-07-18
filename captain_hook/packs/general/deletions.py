from __future__ import annotations

import glob
import itertools
import os
import re
import shlex
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from captain_hook import Allow, Block, Event, HookResult, Input, Rewrite, Tool, on
from captain_hook.types import Command as CommandCondition
from captain_hook.util import fs
from captain_hook.util.scratch import is_scratch_path
from captain_hook.util.shell import normalize_executable, unescape_shell

if TYPE_CHECKING:
    from captain_hook.events import PreToolUseEvent

GLOB_LIMIT = 10
SAFE_WORD = re.compile(r"[^\s'\"\\$`;&|<>(){}#]+")


class GlobExpansion(NamedTuple):
    matches: tuple[str, ...]
    too_broad: bool


class GlobWalkBudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class Recoverable:
    block: HookResult
    note: str


@dataclass(frozen=True)
class Trashed:
    replacement: str
    note: str


def trash_binary() -> str | None:
    return fs.resolve_binary("trash") if sys.platform == "darwin" else None


def in_vcs_repo(directory: Path) -> bool:
    return any(
        (ancestor / marker).exists() for ancestor in (directory, *directory.parents) for marker in (".git", ".jj")
    )


def is_repo_root(resolved: Path) -> bool:
    return any((resolved / marker).exists() for marker in (".git", ".jj"))


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


def emit_target(raw: str, *, plain_words: bool) -> str | None:
    if SAFE_WORD.fullmatch(raw):
        if (glob.has_magic(raw) or raw.startswith("~")) and not plain_words:
            return None
        return f"./{raw}" if raw.startswith("-") else raw
    if (
        not any(char in raw for char in "'\"$`{}")
        and ("\\" not in raw or plain_words)
        and not glob.has_magic(target := unescape_shell(raw))
        and not target.startswith("~")
    ):
        return shlex.quote(target)
    return None


def check_rm_target(
    evt: PreToolUseEvent,
    token: str,
    cwd: Path | None,
) -> HookResult | Recoverable | None:
    if (resolved := resolve_target(token, cwd)) is None or is_scratch_path(resolved):
        return None
    if is_repo_root(resolved):
        return evt.block(
            f"BLOCKED: '{token}' is a git/jj repository root — deleting it destroys the repo and its entire "
            "history. If this is really intended, ask the user to run it themselves."
        )
    if not in_vcs_repo(resolved):
        return Recoverable(
            evt.block(
                f"BLOCKED: rm target '{token}' resolves outside any git/jj repository, so nothing can restore it "
                "after deletion. Move it to the trash instead, or stop and ask the user to confirm this deletion. "
                "(Temp and scratch paths are exempt.)"
            ),
            f"Rewrote rm to trash: '{token}' resolves outside any git/jj repository, so rm would be "
            "unrecoverable. The targets were moved to the macOS Trash instead — restorable via Finder (Put Back). "
            "If permanent deletion is truly intended, ask the user to run the rm themselves.",
        )
    return None


def handle_rm(
    evt: PreToolUseEvent,
    args: tuple[str, ...],
    cwd: Path | None,
    *,
    trash: str | None,
    plain_words: bool,
) -> HookResult | Trashed | None:
    recovery: Recoverable | None = None
    targets = rm_targets(args)
    for raw in targets:
        token = unescape_shell(raw)
        if glob.has_magic(token):
            expansion = glob_matches(token, cwd, limit=GLOB_LIMIT)
            if expansion.too_broad:
                return evt.block(
                    f"BLOCKED: the glob '{token}' is too broad to verify safely (the scan budget was exhausted "
                    "before it completed). Narrow the pattern or delete a specific directory with rm -r <dir>."
                )
            if len(expansion.matches) > GLOB_LIMIT:
                return evt.block(
                    f"BLOCKED: the glob '{token}' matches more than {GLOB_LIMIT} files — an easy way to delete far "
                    f"more than intended. List the matches first (ls {token}), narrow the pattern, or name a "
                    "directory explicitly with rm -r <dir>."
                )
            for matched in expansion.matches:
                match check_rm_target(evt, matched, cwd):
                    case HookResult() as blocked:
                        return blocked
                    case Recoverable() as recoverable if recovery is None:
                        recovery = recoverable
            continue
        match check_rm_target(evt, token, cwd):
            case HookResult() as blocked:
                return blocked
            case Recoverable() as recoverable if recovery is None:
                recovery = recoverable
    if recovery is None:
        return None
    if trash is None:
        return recovery.block
    emitted: list[str] = []
    for target in targets:
        if (value := emit_target(target, plain_words=plain_words)) is None:
            return recovery.block
        emitted.append(value)
    return Trashed(f"{shlex.quote(trash)} {' '.join(emitted)}", recovery.note)


RECOVERABLE_RM: Block | Rewrite = Rewrite(pattern="trash") if trash_binary() else Block(pattern="repository")


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CommandCondition(r"(?i)\brm\b")],
    tests={
        Input(command="rm foo.txt", cwd="/"): RECOVERABLE_RM,
        Input(command="rm -- data.txt", cwd="/"): RECOVERABLE_RM,
        Input(command="sudo rm /foo.txt", cwd="/"): RECOVERABLE_RM,
        Input(command=r"\rm /foo.txt", cwd="/"): RECOVERABLE_RM,
        Input(command="'rm' /foo.txt", cwd="/"): RECOVERABLE_RM,
        Input(command="rm /tmp/x.py", cwd="/"): Allow(),
        Input(command="rm -rf /tmp/scratch/build", cwd="/"): Allow(),
        Input(command="rm foo.txt"): Allow(),
        Input(command="git rm foo.txt"): Allow(),
        Input(command="echo rm foo.txt"): Allow(),
        Input(command="ls"): Allow(),
        Input(command="git status"): Allow(),
    },
)
def block_risky_rm(evt: PreToolUseEvent) -> HookResult | None:
    if (cl := evt.command_line) is None:
        return None
    effective_cwd = evt.cwd
    trash = trash_binary()
    replacements: dict[int, str] = {}
    notes: list[str] = []
    for occurrence in cl.occurrences:
        command = occurrence.command
        unwrapped = command.unwrapped
        match normalize_executable(unwrapped.executable):
            case "cd":
                effective_cwd = resolve_cd(unwrapped.args, effective_cwd)
            case "rm":
                match handle_rm(
                    evt,
                    unwrapped.args,
                    effective_cwd,
                    trash=trash if command.span is not None and "\\\n" not in command.raw else None,
                    plain_words="'" not in command.raw and '"' not in command.raw,
                ):
                    case HookResult() as blocked:
                        return blocked
                    case Trashed(replacement, note):
                        replacements[occurrence.index] = replacement
                        notes.append(note)
    return evt.rewrite_command(cl.splice(replacements), note="\n".join(dict.fromkeys(notes))) if replacements else None
