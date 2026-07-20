from captain_hook import Allow, Block, Event, Input, Runs, hook

hook(
    Event.PreToolUse,
    "BLOCKED: run git stash through a branch, not directly.",
    only_if=[Runs("git", "stash")],
    block=True,
    tests={
        Input(command="git stash"): Block(),
        Input(command="sudo git stash"): Allow(),
        Input(command='sh -c "git stash"'): Block(),
        Input(command="echo git stash"): Allow(),
    },
)
