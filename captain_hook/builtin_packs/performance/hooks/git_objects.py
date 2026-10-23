from __future__ import annotations

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


def probes_lazily(arguments: Arguments) -> bool:
    return (
        arguments.values.get("verb") == ("cat-file",)
        and bool(arguments.values.get("exists"))
        and not arguments.values.get("no_lazy_fetch")
        and all(name != "GIT_NO_LAZY_FETCH" for name, _ in arguments.call.source.env)
    )


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
