from captain_hook import Content, Event, FilePath, Tool, hook

hook(
    Event.PreToolUse,
    "BLOCKED: That looks like a live secret headed for a dotenv file. Reference an env var instead.",
    # all three must match: a write, to a dotenv file, containing a secret-shaped assignment
    only_if=[Tool("Edit|Write"), FilePath("**/.env*"), Content(r"(KEY|TOKEN|SECRET|PASSWORD)\s*=\s*\S")],
    # the sanctioned exception: placeholder values in the committed example file
    skip_if=[FilePath("**/.env.example")],
    block=True,
)
