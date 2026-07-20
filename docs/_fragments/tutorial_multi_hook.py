from captain_hook import Event, block_command, warn_command

block_command(["git", "push", "--force"], reason="force-push rewrites shared history")
warn_command(["git", "push"], message="Double-check the branch before you push", events=Event.PreToolUse)
