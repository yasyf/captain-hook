from __future__ import annotations

import glob
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from captain_hook import Allow, Block, CommandMatches, CommandSchema, Event, HookResult, Input, Rewrite, Tool, on
from captain_hook.cmd import Target
from captain_hook.command_schema import glob_prefix
from captain_hook.util import fs
from captain_hook.util.globbing import GLOB_LIMIT
from captain_hook.util.paths import resolve_target
from captain_hook.util.scratch import is_scratch_path
from captain_hook.util.shell import emit_token, unescape_shell
from captain_hook.util.vcs import contains_repo

if TYPE_CHECKING:
    from captain_hook.cmd import Call
    from captain_hook.events import PreToolUseEvent

SCRATCH_GLOB_LIMIT = 2000


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


def scratch_glob(target: Target) -> bool:
    if target.value is None or (prefix := resolve_target(str(glob_prefix(Path(target.value))), target.cwd)) is None:
        return False
    return is_scratch_path(prefix.resolve() / "_")


def check_target(
    evt: PreToolUseEvent, target: Target, cwd: Path | None, *, rewritable: bool
) -> HookResult | Recoverable | None:
    if not target.verified and glob.has_magic(target.raw):
        return evt.block(
            f"The glob '{target.raw}' is built at run time, so no expansion can be checked before deleting. "
            "Expand it to explicit paths first."
        )
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
        if not scratch_glob(target):
            return evt.block(
                f"The glob '{token}' matches more than {GLOB_LIMIT} files. "
                f"Run `ls {token}`, then narrow the pattern or run `rm -r <dir>` on a named directory."
            )
        expansion = target.expand(limit=SCRATCH_GLOB_LIMIT)
        if expansion.exhausted or len(expansion) > SCRATCH_GLOB_LIMIT:
            return evt.block(
                f"The glob '{token}' matches more than {SCRATCH_GLOB_LIMIT} files, too many to check before "
                "deleting. Run `rm -r <dir>` on a named scratch directory instead."
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
        Input(command="FOO=/outside/x; rm $FOO", cwd="/"): RECOVERABLE_RM,
        Input(command="FOO=/; rm -rf $FOO/x/..", cwd="/"): ROOT_RM,
        Input(command='rm -f "$(git rev-parse --git-dir)/REBASE_HEAD"', cwd="/"): Block(pattern="substitution"),
        Input(command="d=$(mktemp -d); rm -rf $d/*", cwd="/"): Block(pattern="built at run time"),
        Input(command="rm -rf $d/*", cwd="/"): Block(pattern="built at run time"),
        Input(command='X="/tmp/x /"; rm -rf $X', cwd="/"): ROOT_RM,
        Input(command='X="/tmp/x /outside"; rm -rf $X', cwd="/"): RECOVERABLE_RM,
        Input(command='X="/tmp/a /Users/yasyf /tmp/b"; rm -rf $X"$X"', cwd="/"): Block(pattern="repository"),
        Input(command='read IFS <<<:; X="/tmp/x:/Users/yasyf"; rm -rf $X', cwd="/"): Block(pattern="repository"),
        Input(command='IFS=:; X="/tmp/x:/Users/yasyf"; rm -rf $X', cwd="/"): Block(pattern="repository"),
        Input(command="rm -f /tmp/../etc/*", cwd="/"): Block(),
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
        Input(command="cd ~/.claude/scratch/release-v3/b2-net && rm -v exec-*.log && ls exec-* 2>/dev/null"): Allow(),
        Input(command="cd /Users/yasyf/.claude/scratch/pb && rm -f l*.md && ls | wc -l"): Allow(),
        Input(command="rm -f /tmp/.reap-*", cwd="/w"): Allow(),
        Input(command="d=~/.claude/scratch/release-v3/b2-net; rm -f $d/tmp-plat-g2-move.log", cwd="/"): Allow(),
        Input(command="S=/tmp/precompact; rm -rf $S/sessions/1111 $S/sessions/2222", cwd="/"): Allow(),
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
