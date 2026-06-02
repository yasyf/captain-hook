from __future__ import annotations

from captain_hook.primitives.commands import block_command
from captain_hook.testing import Allow, Block, Input

block_command(
    ["git", "stash"],
    reason="use the team VCS workflow",
    hint="commit a WIP instead",
    tests={
        Input(command="git stash"): Block(pattern="use the team VCS workflow"),
        Input(command="git status"): Allow(),
        Input(command="echo hi"): Allow(),
    },
)
