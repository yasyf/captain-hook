from __future__ import annotations

from captain_hook import (
    Allow,
    Block,
    CommandMatches,
    Event,
    Input,
    OptionIs,
    PathMatches,
    PathsMatch,
    Tool,
    hook,
)
from captain_hook.command_schemas import FIND

hook(
    Event.PreToolUse,
    only_if=[
        Tool("Bash"),
        CommandMatches(
            FIND,
            only_if=(
                PathsMatch(
                    "roots",
                    PathMatches(
                        (
                            "/",
                            "~",
                            "/Users",
                            "/Users/*",
                            "/home",
                            "/home/*",
                            "/System/Volumes/Data",
                            "**/.claude/worktrees",
                        ),
                        unresolved=True,
                    ),
                ),
            ),
            skip_if=(OptionIs("max_depth", range(3)),),
        ),
    ],
    message=(
        "BLOCKED: broad filesystem search. Searching from /, a home directory, or the entire "
        "worktree pool traverses dependency and build trees and slows terminal commands and file "
        "operations. A head pipeline, name filter, or hidden stderr does not bound the traversal. "
        "Use `ccx repo locate <name>` for a repository or dependency, package-manager metadata "
        "for an installed module, or search an explicit repository or directory. Unknown shell "
        "expansions cannot establish a scoped root. Use `-maxdepth 1` or `-maxdepth 2` for a shallow listing. "
        "User feedback 2026-09-17: 'and kill the finds'."
    ),
    block=True,
    tests={
        Input(
            command="cd ~/.claude/worktrees/captain-hook/dispatch-stall && "
            'grep -n "daemonkit" go.mod go.sum 2>/dev/null | head -5; '
            'find / -type d -iname "daemonkit*" 2>/dev/null | grep -v Trash | head -10'
        ): Block(pattern="broad filesystem search"),
        Input(command='find / -type d -iname "daemonkit*" -not -path "*/Trash/*" 2>/dev/null | head -10'): Block(),
        Input(command="find / -name daemonkit"): Block(),
        Input(command="find ~ -type d -name daemonkit"): Block(),
        Input(command='find "$HOME" -type d -name daemonkit'): Block(),
        Input(command='find "${HOME}" -type d -name daemonkit'): Block(),
        Input(command="find /Users -name daemonkit"): Block(),
        Input(command="find /Users/alice -name daemonkit"): Block(),
        Input(command="find /home/alice -name daemonkit"): Block(),
        Input(command="find ~/.claude/worktrees -name daemonkit"): Block(),
        Input(command="find $HOME/.claude/worktrees -name daemonkit"): Block(),
        Input(command="find /System/Volumes/Data -name daemonkit"): Block(),
        Input(command="find -H / -name daemonkit"): Block(),
        Input(command="find -HL / -name daemonkit", cwd="/repo"): Block(),
        Input(command="find $(printf /) -name daemonkit", cwd="/repo"): Block(),
        Input(command="find / -maxdepth 1 $EXPR"): Block(),
        Input(command="find / -maxdepth 1 -name $EXPR"): Block(),
        Input(command=r"find / -exec echo + -maxdepth 1 \;"): Block(),
        Input(command="find -f / -name daemonkit"): Block(),
        Input(command="find src / -name daemonkit"): Block(),
        Input(command="sudo /usr/bin/find / -name daemonkit"): Block(),
        Input(command="sh -c 'find / -name daemonkit'"): Block(),
        Input(command="cd / && find . -name daemonkit"): Block(),
        Input(command="find . -name daemonkit", cwd="/"): Block(),
        Input(command="find -name daemonkit", cwd="/"): Block(),
        Input(command="find / -maxdepth 99 -name daemonkit"): Block(),
        Input(command="find / -maxdepth 1 -type d"): Allow(),
        Input(command="find ~ -maxdepth 2 -type d -name daemonkit"): Allow(),
        Input(command="find / -maxdepth 0"): Allow(),
        Input(command="find . -type d -name daemonkit", cwd="/repo"): Allow(),
        Input(command="find ~/Code/captain-hook -type d -name daemonkit"): Allow(),
        Input(command="find ~/.claude/worktrees/captain-hook/dispatch-stall -name daemonkit"): Allow(),
        Input(command="find src -name daemonkit"): Allow(),
        Input(command="find src -newermt 2026-09-17", cwd="/repo"): Allow(),
        Input(command="find src -path / -prune"): Allow(),
        Input(command="echo 'find / -name daemonkit'"): Allow(),
        Input(command="cat <<'EOF'\nfind / -name daemonkit\nEOF"): Allow(),
        Input(command="ccx repo locate daemonkit"): Allow(),
    },
)
