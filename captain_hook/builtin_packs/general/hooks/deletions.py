from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from captain_hook import Allow, Block, Event, HookResult, Input, Rewrite, Tool, on
from captain_hook.types import Command as CommandCondition
from captain_hook.util import fs
from captain_hook.util.globbing import GLOB_LIMIT
from captain_hook.util.paths import resolve_target
from captain_hook.util.scratch import is_scratch_path
from captain_hook.util.vcs import contains_repo, in_vcs_repo, is_repo_root

if TYPE_CHECKING:
    from captain_hook.cmd import Call, Target
    from captain_hook.events import PreToolUseEvent


@dataclass(frozen=True)
class Recoverable:
    block: HookResult
    note: str


def trash_binary() -> str | None:
    return fs.resolve_binary("trash") if sys.platform == "darwin" else None


def blast_radius_block(evt: PreToolUseEvent, token: str, resolved: Path) -> HookResult | None:
    match Path(os.path.normpath(resolved)):
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


def unrecoverable(evt: PreToolUseEvent, token: str) -> Recoverable:
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


def check_resolved(
    evt: PreToolUseEvent, token: str, resolved: Path | None, *, rewritable: bool
) -> HookResult | Recoverable | None:
    if resolved is None or is_scratch_path(resolved):
        return None
    if is_repo_root(resolved):
        return evt.block(
            f"BLOCKED: '{token}' is a git/jj repository root — deleting it destroys the repo and its entire "
            "history. If this is really intended, ask the user to run it themselves."
        )
    if in_vcs_repo(resolved):
        return None
    if rewritable and (blocked := blast_radius_block(evt, token, resolved)) is not None:
        return blocked
    return unrecoverable(evt, token)


def check_target(
    evt: PreToolUseEvent, target: Target, cwd: Path | None, *, rewritable: bool
) -> HookResult | Recoverable | None:
    if not target.verified:
        return unrecoverable(evt, target.raw)
    if not target.has_glob:
        return check_resolved(evt, target.value or target.raw, target.path, rewritable=rewritable)
    expansion = target.expand()
    token = target.value or target.raw
    if expansion.exhausted:
        return evt.block(
            f"BLOCKED: the glob '{token}' is too broad to verify safely (the scan budget was exhausted "
            "before it completed). Narrow the pattern or delete a specific directory with rm -r <dir>."
        )
    if len(expansion) > GLOB_LIMIT:
        return evt.block(
            f"BLOCKED: the glob '{token}' matches more than {GLOB_LIMIT} files — an easy way to delete far "
            f"more than intended. List the matches first (ls {token}), narrow the pattern, or name a "
            "directory explicitly with rm -r <dir>."
        )
    recovery: Recoverable | None = None
    for match in expansion:
        result = check_resolved(evt, match, resolve_target(match, cwd), rewritable=rewritable)
        match result:
            case HookResult() as blocked:
                return blocked
            case Recoverable() as recoverable if recovery is None:
                recovery = recoverable
    return recovery


def check_call(evt: PreToolUseEvent, call: Call, *, rewritable: bool) -> HookResult | Recoverable | None:
    recovery: Recoverable | None = None
    for target in call.targets:
        match check_target(evt, target, call.cwd, rewritable=rewritable):
            case HookResult() as blocked:
                return blocked
            case Recoverable() as recoverable if recovery is None:
                recovery = recoverable
    return recovery


RECOVERABLE_RM: Block | Rewrite = Rewrite(pattern="trash") if trash_binary() else Block(pattern="repository")
ROOT_RM: Block = Block(pattern="filesystem root") if trash_binary() else Block(pattern="repository")


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CommandCondition(r"(?i)\brm\b")],
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
def guard_rm(evt: PreToolUseEvent) -> HookResult | None:
    trash = trash_binary()
    result: HookResult | None = None
    for call in evt.cmd.calls("rm"):
        rewritable = trash is not None and call.spliceable and not call.nested
        match check_call(evt, call, rewritable=rewritable):
            case HookResult() as blocked:
                return blocked
            case Recoverable() as recovery:
                if (
                    rewritable
                    and (rewritten := call.sub("rm", shlex.quote(trash), args=call.targets, note=recovery.note))
                    is not None
                ):
                    result = rewritten
                    continue
                return recovery.block
    return result
