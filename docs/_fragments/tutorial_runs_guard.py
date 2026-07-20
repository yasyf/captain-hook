from captain_hook import Event, Runs, hook

hook(
    Event.PreToolUse,
    "BLOCKED: run git stash through a branch, not directly.",
    only_if=[Runs("git", "stash")],
    block=True,
)
