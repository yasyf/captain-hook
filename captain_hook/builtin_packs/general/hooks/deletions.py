from __future__ import annotations

import glob
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from captain_hook import (
    Allow,
    Block,
    HookResult,
    Input,
    Occurrence,
    Rewrite,
    Rewritten,
    WalkContext,
    rewrite_command_occurrences,
)
from captain_hook.types import Command as CommandCondition
from captain_hook.util import fs
from captain_hook.util.globbing import GLOB_LIMIT, glob_matches
from captain_hook.util.paths import resolve_target
from captain_hook.util.scratch import is_scratch_path
from captain_hook.util.shell import (
    NESTED_COMMAND_DEPTH,
    emit_token,
    nested_command_string,
    normalize_executable,
    resolve_cd,
    safe_parse_command_line,
    unescape_shell,
)
from captain_hook.util.vcs import contains_repo, in_vcs_repo, is_repo_root

if TYPE_CHECKING:
    from captain_hook.events import PreToolUseEvent


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


def rm_targets(args: tuple[str, ...]) -> list[str]:
    separator = args.index("--") if "--" in args else len(args)
    return [arg for arg in args[:separator] if arg == "-" or not arg.startswith("-")] + list(args[separator + 1 :])


def blast_radius_block(evt: PreToolUseEvent, token: str, resolved: Path) -> HookResult | None:
    resolved = Path(os.path.normpath(resolved))
    match resolved:
        case root if root == Path("/"):
            return evt.block(
                f"BLOCKED: '{token}' is the filesystem root — deleting it destroys the entire "
                "system. If this is really intended, ask the user to run it themselves."
            )
        case home if home == Path.home() or Path("/Users") in (home, home.parent):
            return evt.block(
                f"BLOCKED: '{token}' is a home directory — deleting it destroys every file the "
                "user owns. If this is really intended, ask the user to run it themselves."
            )
        case directory if directory.is_dir(follow_symlinks=False) and contains_repo(directory):
            return evt.block(
                f"BLOCKED: '{token}' contains git/jj repositories — deleting it would destroy "
                "them and their entire history. Delete a narrower path instead, or ask the user "
                "to run it themselves."
            )
        case _:
            return None


def check_rm_target(
    evt: PreToolUseEvent,
    token: str,
    cwd: Path | None,
    *,
    rewritable: bool,
) -> HookResult | Recoverable | None:
    if (resolved := resolve_target(token, cwd)) is None or is_scratch_path(resolved):
        return None
    if is_repo_root(resolved):
        return evt.block(
            f"BLOCKED: '{token}' is a git/jj repository root — deleting it destroys the repo and its entire "
            "history. If this is really intended, ask the user to run it themselves."
        )
    if not in_vcs_repo(resolved):
        if rewritable and (blocked := blast_radius_block(evt, token, resolved)) is not None:
            return blocked
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
                match check_rm_target(evt, matched, cwd, rewritable=trash is not None):
                    case HookResult() as blocked:
                        return blocked
                    case Recoverable() as recoverable if recovery is None:
                        recovery = recoverable
            continue
        match check_rm_target(evt, token, cwd, rewritable=trash is not None):
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
        if (value := emit_token(target, plain_words=plain_words)) is None:
            return recovery.block
        emitted.append(value)
    return Trashed(f"{shlex.quote(trash)} {' '.join(emitted)}", recovery.note)


RECOVERABLE_RM: Block | Rewrite = Rewrite(pattern="trash") if trash_binary() else Block(pattern="repository")
ROOT_RM: Block = Block(pattern="filesystem root") if trash_binary() else Block(pattern="repository")


def descend_rm(evt: PreToolUseEvent, payload: str, cwd: Path | None, depth: int) -> HookResult | None:
    if depth <= 0 or (cl := safe_parse_command_line(payload)) is None:
        return None
    effective_cwd = cwd
    for occurrence in cl.occurrences:
        unwrapped = occurrence.command.unwrapped
        match normalize_executable(unwrapped.executable):
            case "cd" if not occurrence.piped:
                effective_cwd = resolve_cd(unwrapped.args, effective_cwd)
            case "rm":
                match handle_rm(evt, unwrapped.args, effective_cwd, trash=None, plain_words=False):
                    case HookResult() as blocked:
                        return blocked
            case program:
                if (nested := nested_command_string(program, unwrapped.args)) is not None and (
                    blocked := descend_rm(evt, nested, effective_cwd, depth - 1)
                ) is not None:
                    return blocked
    return None


def visit_rm(evt: PreToolUseEvent, occurrence: Occurrence, ctx: WalkContext) -> Rewritten | HookResult | None:
    unwrapped = occurrence.command.unwrapped
    match normalize_executable(unwrapped.executable):
        case "rm":
            match handle_rm(
                evt,
                unwrapped.args,
                ctx.cwd,
                trash=trash_binary() if ctx.spliceable else None,
                plain_words=ctx.plain_words,
            ):
                case HookResult() as blocked:
                    return blocked
                case Trashed(replacement, note):
                    return Rewritten(replacement, note)
                case _:
                    return None
        case program:
            if (payload := nested_command_string(program, unwrapped.args)) is not None:
                return descend_rm(evt, payload, ctx.cwd, NESTED_COMMAND_DEPTH)
            return None


rewrite_command_occurrences(
    visit=visit_rm,
    only_if=[CommandCondition(r"(?i)\brm\b")],
    tests={
        Input(command="rm foo.txt", cwd="/"): RECOVERABLE_RM,
        Input(command="rm -rf /", cwd="/"): ROOT_RM,
        Input(command="bash -c 'rm -rf /'", cwd="/"): Block(pattern="repository"),
        Input(command="bash -lc 'rm -rf /'", cwd="/"): Block(pattern="repository"),
        Input(command="sh -xc 'rm -rf /'", cwd="/"): Block(pattern="repository"),
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
