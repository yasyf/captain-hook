from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from captain_hook import Allow, Block, Event, HookResult, Input, Rewrite, Tool, on
from captain_hook.cmd import Target
from captain_hook.types import Command as CommandCondition
from captain_hook.util import fs
from captain_hook.util.globbing import GLOB_LIMIT
from captain_hook.util.shell import emit_token, unescape_shell
from captain_hook.util.vcs import contains_repo

if TYPE_CHECKING:
    from captain_hook.cmd import Call
    from captain_hook.events import PreToolUseEvent


@dataclass(frozen=True)
class Recoverable:
    block: HookResult
    note: str


def trash_binary() -> str | None:
    return fs.resolve_binary("trash") if sys.platform == "darwin" else None


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


def check_resolved(evt: PreToolUseEvent, target: Target, *, rewritable: bool) -> HookResult | Recoverable | None:
    if (path := target.path) is None or target.is_scratch:
        return None
    token = target.value or target.raw
    if target.is_repo_root:
        return evt.block(
            f"BLOCKED: '{token}' is a git/jj repository root — deleting it destroys the repo and its entire "
            "history. If this is really intended, ask the user to run it themselves."
        )
    if target.in_repo:
        return None
    if rewritable:
        if target.is_fs_root:
            return evt.block(
                f"BLOCKED: '{token}' is the filesystem root — deleting it destroys the entire "
                "system. If this is really intended, ask the user to run it themselves."
            )
        if target.is_home:
            return evt.block(
                f"BLOCKED: '{token}' is a home directory — deleting it destroys every file the "
                "user owns. If this is really intended, ask the user to run it themselves."
            )
        # A module-level contains_repo call, not target.contains_repo, so the no-scan guard tests
        # can monkeypatch this exact seam and assert the directory walk stays unentered.
        if (scan := Path(os.path.normpath(path))).is_dir(follow_symlinks=False) and contains_repo(scan):
            return evt.block(
                f"BLOCKED: '{token}' contains git/jj repositories — deleting it would destroy "
                "them and their entire history. Delete a narrower path instead, or ask the user "
                "to run it themselves."
            )
    return unrecoverable(evt, token)


def check_target(
    evt: PreToolUseEvent, target: Target, cwd: Path | None, *, rewritable: bool
) -> HookResult | Recoverable | None:
    if not target.verified:
        # Classify the unverified spelling as a literal path (a `$FOO` file under cwd), so a
        # scratch or in-repo cwd stays allowed; emittable() keeps the target out of any rewrite.
        target = Target(unescape_shell(target.raw), target.raw, cwd)
    if not target.has_glob:
        return check_resolved(evt, target, rewritable=rewritable)
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
        result = check_resolved(evt, Target(match, match, cwd), rewritable=rewritable)
        match result:
            case HookResult() as blocked:
                return blocked
            case Recoverable() as recoverable if recovery is None:
                recovery = recoverable
    return recovery


def check_call(evt: PreToolUseEvent, call: Call, *, rewritable: bool) -> HookResult | Recoverable | None:
    if not call.targets.complete:
        return evt.block(
            f"BLOCKED: a command substitution supplies rm targets in '{call.source.raw}', so they cannot be "
            "verified against any git/jj repository or scratch exemption. Expand the substitution to explicit "
            "paths first, or ask the user to run it themselves."
        )
    recovery: Recoverable | None = None
    for target in call.targets:
        match check_target(evt, target, call.cwd, rewritable=rewritable):
            case HookResult() as blocked:
                return blocked
            case Recoverable() as recoverable if recovery is None:
                recovery = recoverable
    return recovery


def emittable(target: Target) -> bool:
    # Safe to trash-rewrite only when re-emission matches what the classification assumed —
    # quoted glob/tilde/backslash and unexpanded $/`/{} spellings diverge, so they refuse.
    token = target.value if target.value is not None else target.raw
    return emit_token(token, plain_words=target.raw == target.value) is not None


RECOVERABLE_RM: Block | Rewrite = Rewrite(pattern="trash") if trash_binary() else Block(pattern="repository")
ROOT_RM: Block = Block(pattern="filesystem root") if trash_binary() else Block(pattern="repository")


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CommandCondition(r"(?i)\brm\b")],
    tests={
        Input(command="rm foo.txt", cwd="/"): RECOVERABLE_RM,
        Input(command="rm -rf /", cwd="/"): ROOT_RM,
        Input(command="''rm -rf /", cwd="/"): ROOT_RM,  # empty-quote concat still names rm in command position
        Input(command="bash -c 'rm -rf /'", cwd="/"): Block(pattern="repository"),
        Input(command="bash -lc 'rm -rf /'", cwd="/"): Block(pattern="repository"),
        Input(command="sh -xc 'rm -rf /'", cwd="/"): Block(pattern="repository"),
        Input(command='bash -c "rm $F"', cwd="/"): Block(pattern="repository"),
        Input(command='eval "rm $F"', cwd="/"): Block(pattern="repository"),
        Input(command="rm $FOO", cwd="/"): Block(pattern="repository"),
        Input(command="rm /outside/{a,b}", cwd="/"): Block(pattern="repository"),
        Input(command="rm foo\\\nbar", cwd="/"): Block(pattern="repository"),
        Input(command="rm $(ls)", cwd="/"): Block(pattern="repository"),
        Input(command="rm `ls`", cwd="/"): Block(pattern="repository"),
        Input(command="rm foo.txt $(ls)", cwd="/"): Block(pattern="repository"),
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
        # A backslash-newline continuation splits the continued word in the parse, so neither
        # classification nor re-emission can be trusted with a rewrite.
        rewritable = trash is not None and call.spliceable and not call.nested and "\\\n" not in call.source.raw
        match check_call(evt, call, rewritable=rewritable):
            case HookResult() as blocked:
                return blocked
            case Recoverable() as recovery:
                if (
                    rewritable
                    and all(emittable(target) for target in call.targets)
                    and (rewritten := call.sub("rm", shlex.quote(trash), args=call.targets, note=recovery.note))
                    is not None
                ):
                    result = rewritten
                    continue
                return recovery.block
    return result
