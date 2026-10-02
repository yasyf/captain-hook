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
        "A `find` rooted at /, a home directory, or the worktree pool is a broad filesystem search that walks "
        "every dependency tree. Run `ccx repo locate <name>` instead, or bound the listing with `-maxdepth 2`."
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
        Input(
            command="ls ~/.claude/plugins/cache | head; find / -maxdepth 8 -type d -name captain_hook | head -5"
        ): Block(),
        Input(command="find ~ -maxdepth 4 -path '*cc-slack*' -name 'state.db*' 2>/dev/null | head"): Block(),
        Input(command="d=$(ls -d */*/ | sort -V | tail -1); find $d -name '*poll*'", cwd="/p"): Block(),
        Input(command="S=$(mktemp -d); cd $S; for d in a1 b2; do find ecr-$d -maxdepth 4 | head -8; done"): Block(),
        Input(command='outd=$(mktemp -d -t move-exec.XXXXXX) && find "$outd" -type f | head -5'): Block(),
        Input(command="latest=$(ls -d getaway/getaway/*/ | tail -1); find $latest -name hooks.json", cwd="/p"): Block(),
        Input(command="P=$(ls -d ~/.daemonkit/tools/capt-hook/*/ | tail -1); find $P -name '*.py'"): Block(),
        Input(command="W=~/.claude/worktrees; find $W -name daemonkit"): Block(),
        Input(command="for w in ~ /; do find $w -name daemonkit; done"): Block(),
        Input(command="cd / && for d in *; do find $d -name daemonkit; done"): Block(),
        Input(command="find ~/.claude/worktrees/*/ -name daemonkit"): Block(),
        Input(command="find /Users/* -maxdepth 3 -name daemonkit"): Block(),
        Input(command='D=$(printf /); cd "$D" && find . -name daemonkit'): Block(),
        Input(command="find . -name daemonkit"): Block(),
        Input(
            command=(
                "for d in go/ci/internal/release/*/; do "
                "echo \"$(find $d -name '*.go' ! -name '*_test.go' | xargs cat | wc -l) $d\"; done"
            ),
            cwd="/Users/yasyf/.claude/worktrees/monorepo/ethos-audit",
        ): Block(),
        Input(
            command=(
                "cd /tmp && for d in a b c d e f g h i j k l m n o p q r s /; do find $d -name '*.go'; done "
                "2>/dev/null | head"
            )
        ): Block(),
        Input(
            command=(
                "cd /Users/yasyf/.claude/worktrees/monorepo/parity-audit-dev/go/ci/internal/release && for d in "
                "selection dag judge trailers targets stacks images pipeline prcheck reach reads record platy render "
                "slack hold bake ledger canvas finish; do echo \"== $d\"; find $d -name '*.go' ! -name '*_test.go' | "
                "xargs wc -l | sort -n | tail -25; done 2>/dev/null | grep -v ' total$'"
            ),
            cwd="/Users/yasyf/.orca/workspaces/monorepo/monorepo/sole",
        ): Allow(),
        Input(command="find / -maxdepth 1 -type d"): Allow(),
        Input(command="find ~ -maxdepth 2 -type d -name daemonkit"): Allow(),
        Input(command="find / -maxdepth 0"): Allow(),
        Input(command="find . -type d -name daemonkit", cwd="/repo"): Allow(),
        Input(command="find ~/Code/captain-hook -type d -name daemonkit"): Allow(),
        Input(command="find ~/.claude/worktrees/captain-hook/dispatch-stall -name daemonkit"): Allow(),
        Input(
            command=(
                "R=/Users/yasyf/.claude/worktrees/monorepo/parity-audit-dev; ls $R/go/ci; "
                "find $R -name 'targets.yaml' -not -path '*/node_modules/*' | head"
            )
        ): Allow(),
        Input(
            command="for v in 0.2.9 0.2.11; do find $v -maxdepth 3; done",
            cwd="/Users/yasyf/.claude/plugins/cache/forge",
        ): Allow(),
        Input(
            command=(
                "I=~/.local/share/uv/tools/capt-hook/lib/python3.13/site-packages/captain_hook; "
                "find $I/builtin_packs -name '*.py' -o -name '*.md' | sort | xargs wc -l"
            )
        ): Allow(),
        Input(
            command="for d in active-alert lane-comms; do find ~/.claude/worktrees/cc-skills/$d -name 'brief.md'; done"
        ): Allow(),
        Input(
            command="for d in selection dag judge; do find $d -name '*.go' ! -name '*_test.go' | xargs wc -l; done",
            cwd="/Users/yasyf/.claude/worktrees/monorepo/parity-audit-dev/go/ci/internal/release",
        ): Allow(),
        Input(command="find $HOME/.claude/worktrees/captain-hook/x -name daemonkit"): Allow(),
        Input(command="find ~/.claude/worktrees/captain-hook/* -name daemonkit"): Allow(),
        Input(command="find src -name daemonkit", cwd="/repo"): Allow(),
        Input(command="find src -newermt 2026-09-17", cwd="/repo"): Allow(),
        Input(command="find src -path / -prune", cwd="/repo"): Allow(),
        Input(command="echo 'find / -name daemonkit'"): Allow(),
        Input(command="cat <<'EOF'\nfind / -name daemonkit\nEOF"): Allow(),
        Input(command="ccx repo locate daemonkit"): Allow(),
    },
)
