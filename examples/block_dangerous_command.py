# Example: Blocking a dangerous command
#
# Demonstrates how to use `block_command` to prevent the agent from
# running destructive shell commands.  The hook intercepts PreToolUse
# events for Bash and denies execution when the command matches.

from captain_hook import block_command

# Block recursive force-delete commands.
block_command(
    ["rm", "-rf", "*"],
    reason="Recursive force-delete is forbidden",
    hint="Delete files individually or use a safer alternative",
)

# Block force-push (regex pattern).
block_command(
    r"git\s+push\s+--force",
    reason="Force-push rewrites remote history",
    hint="Use --force-with-lease for a safer push",
)
