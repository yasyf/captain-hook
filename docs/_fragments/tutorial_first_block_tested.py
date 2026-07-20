from captain_hook import Allow, Block, Input, block_command

block_command(
    ["git", "stash"],
    reason="Stashing loses uncommitted agent work",
    hint="Commit to a WIP branch instead",
    tests={
        Input(command="git stash"): Block(),
        Input(command="cd /tmp && git stash"): Block(),
        Input(command='echo "git stash"'): Block(),  # conservative by design
        Input(command="git status"): Allow(),
        Input(command='git commit -m "wip"'): Allow(),
    },
)
