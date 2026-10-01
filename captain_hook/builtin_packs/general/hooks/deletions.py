from __future__ import annotations

import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from captain_hook import Allow, Block, CommandMatches, CommandSchema, Event, HookResult, Input, Rewrite, Tool, on
from captain_hook.cmd import Target
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


def unrecoverable(evt: PreToolUseEvent) -> Recoverable:
    return Recoverable(
        evt.block("`rm` cannot be undone outside a git/jj repository. Run `trash <path>` instead."),
        "Rewrote `rm` to `trash` because the target is outside any git/jj repository. "
        "Restore it from the Trash in Finder.",
    )


def check_resolved(evt: PreToolUseEvent, target: Target, *, rewritable: bool) -> HookResult | Recoverable | None:
    if (path := target.path) is None or target.is_scratch:
        return None
    token = target.value or target.raw
    if target.is_repo_root:
        return evt.block(
            f"'{token}' is a git/jj repository root, and deleting it destroys the repo and its history. "
            "Delete a path inside it, or ask the user to run the `rm` themselves."
        )
    if target.in_repo:
        return None
    if rewritable:
        if target.is_fs_root:
            return evt.block(
                f"'{token}' is the filesystem root, and deleting it destroys the system. "
                "Ask the user to run the `rm` themselves."
            )
        if target.is_home:
            return evt.block(
                f"'{token}' is a home directory, and deleting it destroys every file the user owns. "
                "Ask the user to run the `rm` themselves."
            )
        if (scan := Path(os.path.normpath(path))).is_dir(follow_symlinks=False) and contains_repo(scan):
            return evt.block(
                f"'{token}' contains git/jj repositories, and deleting it destroys them with their history. "
                "Delete a narrower path instead."
            )
    return unrecoverable(evt)


def literal_spelling(target: Target, cwd: Path | None) -> Target:
    return target if target.verified else Target(unescape_shell(target.raw), target.raw, cwd)


def check_target(
    evt: PreToolUseEvent, target: Target, cwd: Path | None, *, rewritable: bool
) -> HookResult | Recoverable | None:
    target = literal_spelling(target, cwd)
    if not target.has_glob:
        return check_resolved(evt, target, rewritable=rewritable)
    expansion = target.expand()
    token = target.value or target.raw
    if expansion.exhausted:
        return evt.block(
            f"The glob '{token}' is too broad to verify before deleting. "
            "Narrow the pattern or run `rm -r <dir>` on a specific directory."
        )
    if len(expansion) > GLOB_LIMIT:
        return evt.block(
            f"The glob '{token}' matches more than {GLOB_LIMIT} files. "
            f"Run `ls {token}`, then narrow the pattern or run `rm -r <dir>` on a named directory."
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


def splits_a_word(call: Call) -> bool:
    return "\\\n" in call.source.raw


def check_call(evt: PreToolUseEvent, call: Call, *, rewritable: bool) -> HookResult | Recoverable | None:
    if not call.targets.complete:
        return evt.block(
            "A command substitution supplies the `rm` targets, so no git/jj repository check can verify them. "
            "Expand it to explicit paths first."
        )
    recovery: Recoverable | None = None
    for target in call.targets:
        match check_target(evt, target, call.cwd, rewritable=rewritable):
            case HookResult() as blocked:
                return blocked
            case Recoverable() as recoverable if recovery is None:
                recovery = recoverable
    return recovery


def reemits_as_classified(target: Target) -> bool:
    token = target.value if target.value is not None else target.raw
    return emit_token(token, plain_words=target.raw == target.value) is not None


RECOVERABLE_RM: Block | Rewrite = Rewrite(pattern="trash") if trash_binary() else Block(pattern="repository")
ROOT_RM: Block = Block(pattern="filesystem root") if trash_binary() else Block(pattern="repository")


@on(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CommandMatches(CommandSchema("rm"))],
    tests={
        Input(command="rm foo.txt", cwd="/"): RECOVERABLE_RM,
        Input(command="rm -rf /", cwd="/"): ROOT_RM,
        Input(command="''rm -rf /", cwd="/"): ROOT_RM,
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
        rewritable = trash is not None and call.spliceable and not call.nested and not splits_a_word(call)
        match check_call(evt, call, rewritable=rewritable):
            case HookResult() as blocked:
                return blocked
            case Recoverable() as recovery:
                if (
                    rewritable
                    and all(reemits_as_classified(target) for target in call.targets)
                    and (rewritten := call.sub("rm", shlex.quote(trash), args=call.targets, note=recovery.note))
                    is not None
                ):
                    result = rewritten
                    continue
                return recovery.block
    return result
