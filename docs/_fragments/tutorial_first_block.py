from captain_hook import block_command

block_command(
    ["git", "stash"],
    reason="git stash hides work the rest of the team can't see",
    hint="commit to a branch or use jj instead",
)
