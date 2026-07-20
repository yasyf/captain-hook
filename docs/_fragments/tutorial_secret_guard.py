from captain_hook import Content, Event, FilePath, Tool, hook

hook(
    Event.PreToolUse,
    "BLOCKED: writing a live secret into a tracked file.",
    only_if=[Tool("Edit", "Write"), FilePath("**/.env*"), Content(r"sk-live-")],
    skip_if=[FilePath("**/.env.example")],
    block=True,
)
