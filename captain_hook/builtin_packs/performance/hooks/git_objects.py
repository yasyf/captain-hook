from __future__ import annotations

import re
from typing import TYPE_CHECKING

from captain_hook import Allow, CommandMatches, CommandSchema, Event, Input, Operand, Option, Tool, Warn, hook
from captain_hook.cmd import COMMAND_VALUE_FLAGS

if TYPE_CHECKING:
    from captain_hook import Arguments

GIT_CAT_FILE = CommandSchema(
    "git",
    operands=(Operand("verb"), Operand("rest", count="*")),
    options=(
        Option("environment", COMMAND_VALUE_FLAGS["git"]),
        Option("no_lazy_fetch", ("--no-lazy-fetch",), bool),
        Option("exists", ("-e",), bool),
        Option("path", ("--path",)),
        Option(
            "mode",
            (
                "-h",
                "--help",
                "-v",
                "--version",
                "-p",
                "--paginate",
                "-P",
                "--no-pager",
                "--bare",
                "--html-path",
                "--man-path",
                "--info-path",
                "--no-replace-objects",
                "--no-optional-locks",
                "--no-advice",
                "--literal-pathspecs",
                "--glob-pathspecs",
                "--noglob-pathspecs",
                "--icase-pathspecs",
                "-t",
                "-s",
                "-Z",
                "--batch",
                "--batch-check",
                "--batch-command",
                "--batch-all-objects",
                "--buffer",
                "--follow-symlinks",
                "--unordered",
                "--allow-unknown-type",
                "--textconv",
                "--filters",
                "--mailmap",
                "--no-mailmap",
                "--use-mailmap",
                "--no-use-mailmap",
            ),
            bool,
        ),
    ),
)

COUNT_LIMIT = re.compile(r"-\d+|-n\d*|--max-count(=\d+)?")


def probes_lazily(arguments: Arguments) -> bool:
    return (
        arguments.values.get("verb") == ("cat-file",)
        and bool(arguments.values.get("exists"))
        and not arguments.values.get("no_lazy_fetch")
        and all(name != "GIT_NO_LAZY_FETCH" for name, _ in arguments.call.source.env)
    )


def limits_pathspecs(arguments: Arguments) -> bool:
    match arguments.call.verb_argv:
        case ("git", "log", *rest) if "--" in rest:
            return any(COUNT_LIMIT.fullmatch(word) for word in rest[rest.index("--") + 1 :])
        case _:
            return False


hook(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CommandMatches(GIT_CAT_FILE, only_if=(probes_lazily,))],
    message=(
        "`git cat-file -e` can lazily fetch each missing object from a promisor remote, one process per probe. "
        "Probe objects in one batch with `git --no-lazy-fetch cat-file --batch-check` on stdin, and check "
        "filesystem paths with `test -e`."
    ),
    max_fires=1,
    tests={
        Input(command="git cat-file -e HEAD~1"): Warn(pattern="--batch-check"),
        Input(command="git -C ~/Code/monorepo cat-file -e HEAD"): Warn(),
        Input(command="git -c core.pager=cat cat-file -e HEAD"): Warn(),
        Input(command='for s in one two; do git cat-file -e "$s" || echo "$s"; done'): Warn(),
        Input(command="sh -c 'git cat-file -e HEAD'"): Warn(),
        Input(command="git cat-file -e HEAD && echo present"): Warn(),
        Input(command="sudo git cat-file -e HEAD"): Warn(),
        Input(command="git --no-lazy-fetch cat-file -e HEAD"): Allow(),
        Input(command="GIT_NO_LAZY_FETCH=1 git cat-file -e HEAD"): Allow(),
        Input(command="git --no-lazy-fetch cat-file --batch-check <objects.txt"): Allow(),
        Input(command="git cat-file -p HEAD"): Allow(),
        Input(command="git cat-file -t HEAD"): Allow(),
        Input(command="git fetch origin main"): Allow(),
        Input(command="git grep -e needle"): Allow(),
        Input(command="test -e path/to/file"): Allow(),
        Input(command="echo 'git cat-file -e HEAD'"): Allow(),
    },
)

hook(
    Event.PreToolUse,
    only_if=[Tool("Bash"), CommandMatches(CommandSchema("git"), only_if=(limits_pathspecs,))],
    message=(
        "`git log` reads a count written after `--` as a pathspec, not a limit, so it walks the path's whole "
        "history and emits every matching commit. Put the limit before `--`: "
        "`git log -1 origin/main --format=%H -- <path>`."
    ),
    max_fires=1,
    tests={
        Input(
            command=(
                "git fetch -q origin main && git log origin/main --format=%H -- "
                "plugins/long-running/.claude-plugin/plugin.json -1 | "
                "xargs -I{} git merge-base --is-ancestor 8cba356e {}"
            )
        ): Warn(pattern="before `--`"),
        Input(
            command=(
                "while true; do git fetch -q origin main && git log origin/main --format=%H -- "
                "plugins/long-running/.claude-plugin/plugin.json -1 | xargs -I{} git merge-base --is-ancestor "
                "8cba356e {} && break; sleep 5; done"
            )
        ): Warn(),
        Input(command="sh -c 'git log -- README.md -1'"): Warn(),
        Input(command="git -C ~/Code/captain-hook log --oneline -- README.md -n 1"): Warn(),
        Input(command="git log -- README.md --max-count=1"): Warn(),
        Input(command="git --no-pager log -- README.md -3 | head"): Warn(),
        Input(
            command=(
                "git log -1 origin/main --format=%H -- plugins/long-running/.claude-plugin/plugin.json | "
                "xargs -I{} git merge-base --is-ancestor 8cba356e {}"
            )
        ): Allow(),
        Input(command="git log origin/main --format=%H -1 -- plugins/long-running/.claude-plugin/plugin.json"): Allow(),
        Input(command="git log -n 1 -- README.md"): Allow(),
        Input(command="git log --max-count=1 -- README.md"): Allow(),
        Input(command="git log -- README.md"): Allow(),
        Input(command="git fetch -q origin main"): Allow(),
        Input(command="git merge-base --is-ancestor 8cba356e HEAD"): Allow(),
        Input(command="git diff HEAD -- README.md"): Allow(),
        Input(command="git rev-list --count HEAD -- README.md"): Allow(),
        Input(command="echo 'git log -- README.md -1'"): Allow(),
    },
)
